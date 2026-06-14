import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional
from config.settings import settings


class OrderFlowSRMap:
    """Mapa de Soportes y Resistencias basado en Order Flow.

    Memoria de corto/medio plazo (20 min) que registra dónde ocurrieron
    acumulaciones (whale bid wall + absorción de delta), distribuciones
    (whale ask wall + techo de delta), y liquidation sweeps (quiebre
    rápido con divergencia CVD-precio).
    """

    def __init__(self, window_minutes: int = 20, touch_pct: float = 0.05,
                 sweep_pct: float = 0.12, tick_speed_threshold: int = 8,
                 delta_history_len: int = 6, max_levels: int = 10):
        self.window_seconds = window_minutes * 60
        self.touch_pct = touch_pct
        self.sweep_pct = sweep_pct
        self.tick_speed_threshold = tick_speed_threshold
        self.max_levels = max_levels

        self._supports: List[Dict] = []
        self._resistances: List[Dict] = []
        self._delta_history: Deque[float] = deque(maxlen=delta_history_len)
        self._cvd_history: Deque[float] = deque(maxlen=delta_history_len)
        self._price_ts_buffer: Deque[tuple] = deque(maxlen=30)

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _within_pct(p1: float, p2: float, pct: float) -> bool:
        """True if |p1-p2|/p2*100 <= pct."""
        if p1 <= 0 or p2 <= 0:
            return False
        return abs(p1 - p2) / max(p2, 0.001) * 100 <= pct

    def _find_near(self, levels: List[Dict], price: float,
                   pct: float = None) -> Optional[Dict]:
        pct = pct or self.touch_pct
        for lv in levels:
            if self._within_pct(price, lv['price'], pct):
                return lv
        return None

    # ── core update ────────────────────────────────────────────────────

    def update(self, price: float, delta: float, cvd: float,
               tick_speed: float,
               whale_bid_walls: Optional[List[Dict]] = None,
               whale_ask_walls: Optional[List[Dict]] = None,
               current_time: float = None):
        """Llamar cada ciclo (~1s) con los datos de mercado actuales."""
        if current_time is None:
            current_time = time.time()

        self._delta_history.append(delta)
        self._cvd_history.append(cvd)
        self._price_ts_buffer.append((price, current_time))

        self._expire_levels(current_time)
        self._register_wall_candidates(
            whale_bid_walls or [], whale_ask_walls or [], current_time)
        self._detect_touches(price, delta, current_time)
        self._detect_sweeps(price, tick_speed, current_time)
        self._trim_levels()
        self._recalc_strengths(current_time)
        return self

    # ── expiración ─────────────────────────────────────────────────────

    def _expire_levels(self, now: float):
        cutoff = now - self.window_seconds
        self._supports = [lv for lv in self._supports
                          if lv['last_touch'] >= cutoff]
        self._resistances = [lv for lv in self._resistances
                             if lv['last_touch'] >= cutoff]

    # ── candidatos desde whale walls ───────────────────────────────────

    def _register_wall_candidates(self, bid_walls: List[Dict],
                                   ask_walls: List[Dict], now: float):
        for w in bid_walls:
            p = float(w.get('price', 0))
            q = float(w.get('quantity', 0))
            if p <= 0:
                continue
            if self._find_near(self._supports, p):
                continue
            lv = self._find_near(self._resistances, p)
            if lv:
                continue
            self._supports.append({
                'price': round(p, 2),
                'strength': 15.0,
                'level_type': 'candidate',
                'side': 'support',
                'detected_at': now,
                'last_touch': now,
                'touches': 0,
                'wall_size': q,
                'source_wall_price': p,
                'confirmed': False,
            })

        for w in ask_walls:
            p = float(w.get('price', 0))
            q = float(w.get('quantity', 0))
            if p <= 0:
                continue
            if self._find_near(self._resistances, p):
                continue
            lv = self._find_near(self._supports, p)
            if lv:
                continue
            self._resistances.append({
                'price': round(p, 2),
                'strength': 15.0,
                'level_type': 'candidate',
                'side': 'resistance',
                'detected_at': now,
                'last_touch': now,
                'touches': 0,
                'wall_size': q,
                'source_wall_price': p,
                'confirmed': False,
            })

    # ── toques + confirmación por absorción ───────────────────────────

    def _detect_touches(self, price: float, delta: float, now: float):
        dh = list(self._delta_history)
        delta_rising = len(dh) >= 3 and dh[-1] > dh[-2]
        delta_dropping = len(dh) >= 3 and dh[-1] < dh[-2]
        delta_stall = len(dh) >= 2 and abs(dh[-1] - dh[-2]) < abs(dh[-2]) * 0.01

        for lv in self._supports:
            if not self._within_pct(price, lv['price'], self.touch_pct):
                continue
            lv['last_touch'] = now
            lv['touches'] += 1
            lv['strength'] = min(100.0, lv['strength'] + 8.0)
            if not lv['confirmed'] and (delta_stall or delta_rising):
                was_dropping = len(dh) >= 4 and dh[-3] <= dh[-2] <= dh[-1]
                if was_dropping:
                    lv['confirmed'] = True
                    lv['level_type'] = 'accumulation'
                    lv['strength'] = min(100.0, lv['strength'] + 25.0)

        for lv in self._resistances:
            if not self._within_pct(price, lv['price'], self.touch_pct):
                continue
            lv['last_touch'] = now
            lv['touches'] += 1
            lv['strength'] = min(100.0, lv['strength'] + 8.0)
            if not lv['confirmed'] and (delta_stall or delta_dropping):
                was_rising = len(dh) >= 4 and dh[-3] >= dh[-2] >= dh[-1]
                if was_rising:
                    lv['confirmed'] = True
                    lv['level_type'] = 'distribution'
                    lv['strength'] = min(100.0, lv['strength'] + 25.0)

    # ── liquidation sweep ─────────────────────────────────────────────

    def _detect_sweeps(self, price: float, tick_speed: float, now: float):
        if tick_speed < self.tick_speed_threshold:
            return
        if len(self._cvd_history) < 3:
            return
        cvd_now = self._cvd_history[-1]
        cvd_before = self._cvd_history[0]
        cvd_delta = cvd_now - cvd_before

        for lv in self._supports:
            sp = lv['price']
            if price >= sp * (1 - self.sweep_pct / 100):
                continue
            lv['touches'] += 1
            lv['level_type'] = 'liquidation_sweep'
            lv['confirmed'] = True
            lv['strength'] = min(100.0, lv['strength'] + 35.0)
            lv['last_touch'] = now

        for lv in self._resistances:
            rp = lv['price']
            if price <= rp * (1 + self.sweep_pct / 100):
                continue
            lv['touches'] += 1
            lv['level_type'] = 'liquidation_sweep'
            lv['confirmed'] = True
            lv['strength'] = min(100.0, lv['strength'] + 35.0)
            lv['last_touch'] = now

    # ── trim ──────────────────────────────────────────────────────────

    def _trim_levels(self):
        self._supports.sort(
            key=lambda x: (-x['strength'], -x['touches'], x['price']))
        self._supports = self._supports[:self.max_levels]
        self._supports.sort(key=lambda x: x['price'], reverse=True)

        self._resistances.sort(
            key=lambda x: (-x['strength'], -x['touches'], x['price']))
        self._resistances = self._resistances[:self.max_levels]
        self._resistances.sort(key=lambda x: x['price'])

    # ── recalc strengths ──────────────────────────────────────────────

    def _recalc_strengths(self, now: float):
        age_weight = 1.0
        for lv in self._supports:
            age_secs = now - lv['detected_at']
            age_factor = max(0.1, 1 - age_secs / self.window_seconds)
            base = 15.0
            touch_bonus = min(40.0, lv['touches'] * 10.0)
            if lv['level_type'] == 'accumulation':
                type_bonus = 25.0
            elif lv['level_type'] == 'liquidation_sweep':
                type_bonus = 35.0
            elif lv['level_type'] == 'distribution':
                type_bonus = 15.0
            else:
                type_bonus = 0.0
            raw = (base + touch_bonus + type_bonus) * age_factor
            lv['strength'] = round(min(100.0, raw), 1)

        for lv in self._resistances:
            age_secs = now - lv['detected_at']
            age_factor = max(0.1, 1 - age_secs / self.window_seconds)
            base = 15.0
            touch_bonus = min(40.0, lv['touches'] * 10.0)
            if lv['level_type'] == 'distribution':
                type_bonus = 25.0
            elif lv['level_type'] == 'liquidation_sweep':
                type_bonus = 35.0
            elif lv['level_type'] == 'accumulation':
                type_bonus = 15.0
            else:
                type_bonus = 0.0
            raw = (base + touch_bonus + type_bonus) * age_factor
            lv['strength'] = round(min(100.0, raw), 1)

    # ── consulta pública ──────────────────────────────────────────────

    def get_supports(self) -> List[Dict]:
        return [dict(lv) for lv in self._supports]

    def get_resistances(self) -> List[Dict]:
        return [dict(lv) for lv in self._resistances]

    def get_nearest_support(self, price: float) -> Optional[Dict]:
        candidates = [lv for lv in self._supports if lv['price'] < price]
        return max(candidates, key=lambda x: x['price']) if candidates else None

    def get_nearest_resistance(self, price: float) -> Optional[Dict]:
        candidates = [lv for lv in self._resistances if lv['price'] > price]
        return min(candidates, key=lambda x: x['price']) if candidates else None

    def get_snapshot(self, price: float) -> Dict:
        ns = self.get_nearest_support(price)
        nr = self.get_nearest_resistance(price)
        return {
            'flow_supports': self.get_supports(),
            'flow_resistances': self.get_resistances(),
            'flow_nearest_support_price': round(ns['price'], 2) if ns else 0.0,
            'flow_nearest_support_strength': ns['strength'] if ns else 0.0,
            'flow_nearest_resistance_price': round(nr['price'], 2) if nr else 0.0,
            'flow_nearest_resistance_strength': nr['strength'] if nr else 0.0,
            'flow_support_count': len(self._supports),
            'flow_resistance_count': len(self._resistances),
        }


class OrderFlowEngine:
    def __init__(self, window_seconds: int = 60):
        self.window_seconds = window_seconds

        self.trades_buffer: Deque[Dict] = deque(maxlen=10000)

        self.buy_volume = 0.0
        self.sell_volume = 0.0

        self.cumulative_delta = 0.0

        self.last_delta = 0.0
        self.delta_per_second = 0.0

        self.spoofing_threshold = 0.3
        self.order_book_imbalance = 0.0

        self.history: List[Dict] = []

    def add_trade(self, trade: Dict):
        price = trade['price']
        quantity = trade['quantity']
        is_buyer_maker = trade['is_buyer_maker']

        self.trades_buffer.append({
            'time': trade['time'],
            'price': price,
            'quantity': quantity,
            'is_buyer_maker': is_buyer_maker,
            'timestamp': datetime.now()
        })

        if is_buyer_maker:
            self.sell_volume += quantity * price
            self.cumulative_delta -= quantity
        else:
            self.buy_volume += quantity * price
            self.cumulative_delta += quantity

        self.last_delta = self.cumulative_delta

    def calculate_delta(self) -> Dict:
        now = datetime.now()
        cutoff_time = now.timestamp() - self.window_seconds

        buy_vol = 0.0
        sell_vol = 0.0

        temp_buffer = []
        while self.trades_buffer and self.trades_buffer[0]['timestamp'].timestamp() < cutoff_time:
            self.trades_buffer.popleft()

        for trade in self.trades_buffer:
            if trade['is_buyer_maker']:
                sell_vol += trade['quantity'] * trade['price']
            else:
                buy_vol += trade['quantity'] * trade['price']

        self.delta_per_second = self.cumulative_delta / self.window_seconds

        delta_strength = self._calculate_delta_strength()

        return {
            'delta': self.cumulative_delta,
            'delta_per_second': self.delta_per_second,
            'buy_volume': self.buy_volume,
            'sell_volume': self.sell_volume,
            'window_buy_volume': buy_vol,
            'window_sell_volume': sell_vol,
            'delta_strength': delta_strength,
            'total_trades': len(self.trades_buffer)
        }

    def _calculate_delta_strength(self) -> float:
        total = self.buy_volume + self.sell_volume
        if total == 0:
            return 0.0

        if self.cumulative_delta > 0:
            return self.cumulative_delta / total
        else:
            return self.cumulative_delta / total

    def analyze_order_book(self, depth_data: Dict) -> Dict:
        bids = depth_data.get('bids', [])
        asks = depth_data.get('asks', [])

        bid_volume = sum(vol for _, vol in bids)
        ask_volume = sum(vol for _, vol in asks)
        total = bid_volume + ask_volume

        if total == 0:
            return {'imbalance': 0.0, 'signal': 'neutral'}

        self.order_book_imbalance = (bid_volume - ask_volume) / total

        if self.order_book_imbalance > 0.3:
            signal = 'buy_wall'
        elif self.order_book_imbalance < -0.3:
            signal = 'sell_wall'
        else:
            signal = 'neutral'

        return {
            'imbalance': self.order_book_imbalance,
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'signal': signal
        }

    def detect_spoofing(self, depth_data: Dict) -> Dict:
        order_book = self.analyze_order_book(depth_data)
        delta = self.calculate_delta()

        spoofing_score = 0.0
        warnings = []

        if order_book['signal'] == 'buy_wall' and delta['delta'] < 0:
            imbalance_ratio = abs(order_book['imbalance'])
            if imbalance_ratio > self.spoofing_threshold:
                spoofing_score = imbalance_ratio
                warnings.append("📛 Posible Pared Falsa (Buy Wall sin Delta)")

        if order_book['signal'] == 'sell_wall' and delta['delta'] > 0:
            imbalance_ratio = abs(order_book['imbalance'])
            if imbalance_ratio > self.spoefing_threshold:
                spoofing_score = imbalance_ratio
                warnings.append("📛 Posible Pared Falsa (Sell Wall sin Delta)")

        return {
            'detected': spoofing_score > 0.5,
            'score': spoofing_score,
            'warnings': warnings
        }

    def reset(self):
        self.trades_buffer.clear()
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.cumulative_delta = 0.0
        self.last_delta = 0.0
        self.delta_per_second = 0.0

    def get_status(self) -> str:
        delta = self.calculate_delta()

        if delta['delta_strength'] > 0.3:
            return "🟢 COMPRA FUERTE"
        elif delta['delta_strength'] < -0.3:
            return "🔴 VENTA FUERTE"
        elif abs(delta['delta_strength']) > 0.1:
            return "🟡 DÉBIL" if delta['delta_strength'] > 0 else "🟠 DÉBIL"
        return "⚪ NEUTRAL"


order_flow_engine = OrderFlowEngine()