import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from config.settings import settings


class TradingStrategy:
    def __init__(self):
        self.klines: List[Dict] = []
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

    def add_kline(self, kline: Dict):
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

    def calculate_indicators(self) -> Dict:
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

    def check_bollinger_zone(self, current_price: float, indicators: Dict) -> bool:
        bb_upper = indicators.get('bb_upper')
        bb_lower = indicators.get('bb_lower')

        if bb_upper is None or bb_lower is None or bb_upper == bb_lower:
            return False

        position = (current_price - bb_lower) / (bb_upper - bb_lower)

        return position < 0.2 or position > 0.8

    def check_rsi_extreme(self, indicators: Dict) -> Optional[str]:
        rsi = indicators.get('rsi')

        if rsi is None or pd.isna(rsi):
            return None

        if rsi < 25:
            return "oversold"
        elif rsi > 75:
            return "overbought"

        return None

    def check_macd_cross(self, indicators: Dict) -> Optional[str]:
        macd = indicators.get('macd')
        macd_signal = indicators.get('macd_signal')

        if macd is None or macd_signal is None or pd.isna(macd) or pd.isna(macd_signal):
            return None

        if macd > macd_signal:
            return "bullish"
        elif macd < macd_signal:
            return "bearish"

        return None

    def analyze(self, delta_info: Dict, order_book_info: Dict, current_price: float) -> Dict:
        indicators = self.calculate_indicators()

        if not indicators:
            return {'signal': 'none', 'reason': 'Sin datos suficientes'}

        bb_zone = self.check_bollinger_zone(current_price, indicators)
        rsi_extreme = self.check_rsi_extreme(indicators)
        macd_signal = self.check_macd_cross(indicators)

        delta_confirms = False
        delta_direction = "neutral"

        delta_strength = delta_info.get('delta_strength', 0)
        if delta_strength > 0.3:
            delta_confirms = True
            delta_direction = "long"
        elif delta_strength < -0.3:
            delta_confirms = True
            delta_direction = "short"

        spoofing = delta_info.get('spoofing_detected', False)

        if spoofing:
            return {'signal': 'none', 'reason': 'Spoofing detectado'}

        long_conditions = (
            bb_zone and
            rsi_extreme == "oversold" and
            macd_signal == "bullish" and
            delta_confirms and
            delta_direction == "long"
        )

        short_conditions = (
            bb_zone and
            rsi_extreme == "overbought" and
            macd_signal == "bearish" and
            delta_confirms and
            delta_direction == "short"
        )

        if long_conditions and not self.position_open:
            self.last_signal = 'long'
            atr = indicators.get('atr', current_price * 0.005)
            if pd.isna(atr):
                atr = current_price * 0.005
            return {
                'signal': 'long',
                'price': current_price,
                'stop_loss': current_price - (atr * 2),
                'indicators': indicators
            }

        if short_conditions and not self.position_open:
            self.last_signal = 'short'
            atr = indicators.get('atr', current_price * 0.005)
            if pd.isna(atr):
                atr = current_price * 0.005
            return {
                'signal': 'short',
                'price': current_price,
                'stop_loss': current_price + (atr * 2),
                'indicators': indicators
            }

        return {'signal': 'none', 'reason': 'Condiciones no cumplidas'}

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


trading_strategy = TradingStrategy()