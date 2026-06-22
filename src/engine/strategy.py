"""
strategy.py — Estrategia de trading con sistema de scoring para BB-450.

Arquitectura
────────────
  TradingStrategy mantiene la interfaz original para compatibilidad.
  TradingStrategyV2 añade:
    - Sistema de scoring ponderado en lugar de condiciones binarias
    - Trailing stop dinámico para posiciones abiertas
    - Gestión multi-TP (take profit parcial)
    - Ponderación multi-timeframe

Scoring
───────
  Cada indicador aporta puntos a LONG (+) o SHORT (-):
    BB Zone:     ±20 pts (posición extrema en bandas)
    RSI:         ±15 pts (sobrecompra/venta)
    MACD:        ±15 pts (cruce/divergencia)
    Delta/CVD:   ±20 pts (flujo de órdenes)
    Order Book:  ±10 pts (imbalance profundidad)
    MTF Trend:   ±10 pts (alineación temporal)
    Volumen:     ±5 pts (confirmación)
    S/R Levels:  ±5 pts (distancia a niveles)
    ──────────────────────
    Total:       ±100 pts máx

  Score > 60 → LONG
  Score < -60 → SHORT
  Score entre -60 y 60 → WAIT
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
from config.settings import settings

log = logging.getLogger(__name__)


@dataclass
class SignalScore:
    signal: str  # "LONG", "SHORT", "WAIT"
    score: float
    confidence: float
    bb_score: float = 0.0
    rsi_score: float = 0.0
    macd_score: float = 0.0
    delta_score: float = 0.0
    book_score: float = 0.0
    mtf_score: float = 0.0
    volume_score: float = 0.0
    sr_score: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    reason: str = ""
    timestamp: float = 0.0


@dataclass
class ActivePosition:
    side: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    entry_time: float
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_activated: bool = False
    trailing_stop: float = 0.0

    def update_trailing(self, current_price: float, atr: float):
        """Actualiza trailing stop dinámico."""
        if self.side == "LONG":
            self.highest_price = max(self.highest_price, current_price)

            if not self.trailing_activated:
                if current_price >= self.take_profit_1:
                    self.trailing_activated = True
                    self.trailing_stop = current_price - (atr * 1.5)
            else:
                new_stop = current_price - (atr * 1.5)
                self.trailing_stop = max(self.trailing_stop, new_stop)

        else:
            self.lowest_price = min(self.lowest_price, current_price)

            if not self.trailing_activated:
                if current_price <= self.take_profit_1:
                    self.trailing_activated = True
                    self.trailing_stop = current_price + (atr * 1.5)
            else:
                new_stop = current_price + (atr * 1.5)
                self.trailing_stop = min(self.trailing_stop, new_stop)

    def check_exit(self, current_price: float, atr: float) -> Optional[str]:
        """Verifica si se debe cerrar la posición.

        Returns
        -------
        str | None
            "SL", "TP1", "TP2", "TRAILING", o None si ninguna.
        """
        if self.side == "LONG":
            if current_price <= self.stop_loss:
                return "SL"
            if current_price >= self.take_profit_2:
                return "TP2"
            if current_price >= self.take_profit_1:
                return "TP1" if not self.trailing_activated else None
            if self.trailing_activated and current_price <= self.trailing_stop:
                return "TRAILING"
        else:
            if current_price >= self.stop_loss:
                return "SL"
            if current_price <= self.take_profit_2:
                return "TP2"
            if current_price <= self.take_profit_1:
                return "TP1" if not self.trailing_activated else None
            if self.trailing_activated and current_price >= self.trailing_stop:
                return "TRAILING"

        return None


class TradingStrategyCore:
    """Núcleo de la estrategia: cálculo de indicadores y sistema de scoring."""

    def __init__(self):
        self.klines = []
        self.max_klines = 500
        self.bollinger_period = 20
        self.bollinger_std = 2
        self.rsi_period = 14
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9

        self.last_signal = None
        self.position_open = False
        self.position_side = None
        self._active_position: Optional[ActivePosition] = None

        # Scoring thresholds
        self.LONG_THRESHOLD = 60
        self.SHORT_THRESHOLD = -60
        self.MAX_SCORE = 100

    def add_kline(self, kline: dict):
        self.klines.append(kline)
        if len(self.klines) > self.max_klines:
            self.klines.pop(0)

    def get_dataframe(self) -> pd.DataFrame:
        if len(self.klines) < 50:
            return pd.DataFrame()
        df = pd.DataFrame(self.klines)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=True)
        return df

    def _calculate_bollinger(self, close: pd.Series) -> tuple:
        sma = close.rolling(window=self.bollinger_period).mean()
        std = close.rolling(window=self.bollinger_period).std()
        upper = sma + (std * self.bollinger_std)
        lower = sma - (std * self.bollinger_std)
        return upper, sma, lower

    def _calculate_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _calculate_macd(self, close: pd.Series) -> tuple:
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=self.macd_signal, adjust=False).mean()
        hist = macd - signal
        return macd, signal, hist

    def _calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=14).mean()
        return atr

    def calculate_indicators(self) -> dict:
        df = self.get_dataframe()
        if df.empty:
            return {}
        close = df['close']
        bb_upper, bb_middle, bb_lower = self._calculate_bollinger(close)
        df['bb_upper'] = bb_upper
        df['bb_middle'] = bb_middle
        df['bb_lower'] = bb_lower
        df['rsi'] = self._calculate_rsi(close)
        macd, macd_signal, macd_hist = self._calculate_macd(close)
        df['macd'] = macd
        df['macd_signal'] = macd_signal
        df['macd_hist'] = macd_hist
        df['atr'] = self._calculate_atr(df['high'], df['low'], close)
        return df.iloc[-1].to_dict() if not df.empty else {}

    def calculate_score(self, current_price: float, delta_info: dict,
                         order_book_info: dict, indicators: dict) -> SignalScore:
        """Calcula el score total usando el sistema de puntuación ponderada.

        Parameters
        ----------
        current_price : float
            Precio actual del mercado.
        delta_info : dict
            Información de delta y CVD del OrderFlowEngine.
        order_book_info : dict
            Información del order book.
        indicators : dict
            Indicadores técnicos calculados.

        Returns
        -------
        SignalScore
            Score con desglose por componente.
        """
        score = 0.0
        timestamp = time.time()

        # ── 1. BB Zone Score (±20 pts) ──────────────────────────────
        bb_upper = indicators.get('bb_upper')
        bb_lower = indicators.get('bb_lower')
        bb_score = 0.0
        if bb_upper and bb_lower and bb_upper != bb_lower:
            bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
            if bb_position < 0.2:
                bb_score = 20.0 * (1 - bb_position / 0.2)
            elif bb_position > 0.8:
                bb_score = -20.0 * ((bb_position - 0.8) / 0.2)
        score += bb_score

        # ── 2. RSI Score (±15 pts) ─────────────────────────────────
        rsi = indicators.get('rsi', 50)
        rsi_score = 0.0
        if not pd.isna(rsi):
            if rsi < 25:
                rsi_score = 15.0 * (1 - rsi / 25)
            elif rsi < 40:
                rsi_score = 5.0 * (1 - (rsi - 25) / 15)
            elif rsi > 75:
                rsi_score = -15.0 * ((rsi - 75) / 25)
            elif rsi > 60:
                rsi_score = -5.0 * ((rsi - 60) / 15)
        score += rsi_score

        # ── 3. MACD Score (±15 pts) ────────────────────────────────
        macd = indicators.get('macd', 0)
        macd_signal = indicators.get('macd_signal', 0)
        macd_hist = indicators.get('macd_hist', 0)
        macd_score = 0.0
        if not pd.isna(macd) and not pd.isna(macd_signal):
            if macd > macd_signal and macd_hist > 0:
                macd_score = 15.0 * min(abs(macd_hist) / 10, 1.0)
            elif macd < macd_signal and macd_hist < 0:
                macd_score = -15.0 * min(abs(macd_hist) / 10, 1.0)
            elif macd > macd_signal:
                macd_score = 5.0
            elif macd < macd_signal:
                macd_score = -5.0
        score += macd_score

        # ── 4. Delta/CVD Score (±20 pts) ───────────────────────────
        delta_strength = delta_info.get('delta_strength', 0)
        cvd = delta_info.get('cvd', 0)
        delta_score = 0.0
        if delta_strength > 0.3:
            delta_score = 20.0 * min(delta_strength, 1.0)
        elif delta_strength < -0.3:
            delta_score = -20.0 * min(abs(delta_strength), 1.0)
        else:
            delta_score = delta_strength * 20.0

        is_spoofing = delta_info.get('spoofing_detected', False)
        if is_spoofing:
            delta_score *= -0.5

        score += delta_score

        # ── 5. Order Book Score (±10 pts) ──────────────────────────
        imbalance = order_book_info.get('imbalance', 0)
        ba_ratio = order_book_info.get('ba_ratio', 1.0)
        book_score = 0.0
        if imbalance > 0.3:
            book_score = 10.0 * min(imbalance, 1.0)
        elif imbalance < -0.3:
            book_score = -10.0 * min(abs(imbalance), 1.0)

        if ba_ratio > 2.0:
            book_score += 5.0
        elif ba_ratio < 0.5:
            book_score -= 5.0

        book_score = max(-10, min(10, book_score))
        score += book_score

        # ── 6. MTF Trend Score (±10 pts) ───────────────────────────
        trend = indicators.get('trend', 'NEUTRAL')
        mtf_score = 0.0
        if trend == 'ALCISTA':
            mtf_score = 10.0
        elif trend == 'BAJISTA':
            mtf_score = -10.0
        score += mtf_score

        # ── 7. Volume Score (±5 pts) ───────────────────────────────
        volume = indicators.get('volume', 0)
        avg_volume = indicators.get('avg_volume', 1)
        volume_score = 0.0
        if avg_volume > 0 and volume > avg_volume * 2:
            volume_score = 5.0 * min(volume / avg_volume / 3, 1.0)
        elif avg_volume > 0 and volume < avg_volume * 0.5:
            volume_score = -3.0
        score += volume_score

        # ── 8. S/R Levels Score (±5 pts) ───────────────────────────
        sr_score = 0.0
        support_dist = indicators.get('nearest_support_dist_pct', 0.5)
        resistance_dist = indicators.get('nearest_resistance_dist_pct', 0.5)
        if support_dist < 0.3:
            sr_score = 5.0 * (1 - support_dist / 0.3)
        if resistance_dist < 0.3:
            sr_score = -5.0 * (1 - resistance_dist / 0.3)
        score += sr_score

        # ── Determinar señal basada en score ──────────────────────
        atr_val = indicators.get('atr', current_price * 0.005)
        if pd.isna(atr_val) or atr_val <= 0:
            atr_val = current_price * 0.005

        signal = "WAIT"
        confidence = 0.0
        sl = 0.0
        tp1 = 0.0
        tp2 = 0.0
        reason = "Score dentro de rango neutral"

        if score >= self.LONG_THRESHOLD:
            signal = "LONG"
            confidence = min(abs(score) / self.MAX_SCORE * 100, 95)
            sl = current_price - (atr_val * 1.5)
            tp1 = current_price + (atr_val * 2.5)
            tp2 = current_price + (atr_val * 4.0)
            reason = f"Score LONG: {score:.0f} pts"
        elif score <= self.SHORT_THRESHOLD:
            signal = "SHORT"
            confidence = min(abs(score) / self.MAX_SCORE * 100, 95)
            sl = current_price + (atr_val * 1.5)
            tp1 = current_price - (atr_val * 2.5)
            tp2 = current_price - (atr_val * 4.0)
            reason = f"Score SHORT: {score:.0f} pts"

        return SignalScore(
            signal=signal,
            score=score,
            confidence=confidence,
            bb_score=bb_score,
            rsi_score=rsi_score,
            macd_score=macd_score,
            delta_score=delta_score,
            book_score=book_score,
            mtf_score=mtf_score,
            volume_score=volume_score,
            sr_score=sr_score,
            entry_price=current_price,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            reason=reason,
            timestamp=timestamp,
        )

    def open_position(self, score: SignalScore, quantity: float) -> ActivePosition:
        """Abre una nueva posición basada en un score."""
        self.position_open = True
        self.position_side = score.signal
        self.last_signal = score.signal

        pos = ActivePosition(
            side=score.signal,
            entry_price=score.entry_price,
            quantity=quantity,
            stop_loss=score.stop_loss,
            take_profit_1=score.take_profit_1,
            take_profit_2=score.take_profit_2,
            entry_time=time.time(),
            highest_price=score.entry_price if score.signal == "LONG" else 0,
            lowest_price=score.entry_price if score.signal == "SHORT" else float('inf'),
        )

        self._active_position = pos
        return pos

    def close_position(self):
        """Cierra la posición activa."""
        self.position_open = False
        self.position_side = None
        self._active_position = None

    def get_active_position(self) -> Optional[ActivePosition]:
        return self._active_position

    def update_active_position(self, current_price: float, atr: float):
        """Actualiza trailing y verifica salidas de la posición activa."""
        if not self._active_position:
            return None

        self._active_position.update_trailing(current_price, atr)
        return self._active_position.check_exit(current_price, atr)

    def analyze(self, delta_info: dict, order_book_info: dict,
                 current_price: float) -> dict:
        """Interfaz compatible con la versión anterior.

        Retorna dict con 'signal', 'price', 'stop_loss', etc.
        """
        indicators = self.calculate_indicators()
        if not indicators:
            return {'signal': 'none', 'reason': 'Sin datos suficientes'}

        score = self.calculate_score(current_price, delta_info,
                                      order_book_info, indicators)

        result = {
            'signal': 'none',
            'price': current_price,
            'stop_loss': 0,
            'reason': score.reason,
            'score': score.score,
            'confidence': score.confidence,
        }

        if score.signal == "LONG" and not self.position_open:
            self.last_signal = 'long'
            result.update({
                'signal': 'long',
                'stop_loss': score.stop_loss,
                'take_profit': score.take_profit_1,
                'take_profit_2': score.take_profit_2,
                'indicators': indicators,
                'score_detail': {
                    'bb': score.bb_score,
                    'rsi': score.rsi_score,
                    'macd': score.macd_score,
                    'delta': score.delta_score,
                    'book': score.book_score,
                    'mtf': score.mtf_score,
                    'volume': score.volume_score,
                    'sr': score.sr_score,
                },
            })

        elif score.signal == "SHORT" and not self.position_open:
            self.last_signal = 'short'
            result.update({
                'signal': 'short',
                'stop_loss': score.stop_loss,
                'take_profit': score.take_profit_1,
                'take_profit_2': score.take_profit_2,
                'indicators': indicators,
                'score_detail': {
                    'bb': score.bb_score,
                    'rsi': score.rsi_score,
                    'macd': score.macd_score,
                    'delta': score.delta_score,
                    'book': score.book_score,
                    'mtf': score.mtf_score,
                    'volume': score.volume_score,
                    'sr': score.sr_score,
                },
            })

        return result

    def get_entry_price(self, side: str, price: float, atr: float) -> float:
        if side == 'long':
            return price - (atr * 0.5)
        return price + (atr * 0.5)

    def calculate_position_size(self, balance: float, entry: float, stop_loss: float) -> float:
        risk_amount = balance * settings.RISK_PER_TRADE
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit == 0:
            return 0.0
        return risk_amount / risk_per_unit


# Singleton para compatibilidad con el código existente
trading_strategy = TradingStrategyCore()
