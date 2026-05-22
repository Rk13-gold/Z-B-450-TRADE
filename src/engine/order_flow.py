import asyncio
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List
from config.settings import settings


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