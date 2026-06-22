"""
indicator_engine.py — Motor de fórmulas para indicadores personalizados.

Permite definir indicadores usando una sintaxis simple:
  EMA(close, 50)
  MACD(close).hist
  (EMA(close,20) - EMA(close,50)) / ATR(high,low,close,14) * 100
  BB_UPPER(close, 20, 2)
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Optional

import numpy as np

log = logging.getLogger(__name__)


# ── Funciones nativas de indicador (operan sobre np.array) ──────────────

def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    result = np.empty_like(data)
    result[:] = np.nan
    if len(data) < period:
        return result
    alpha = 2.0 / (period + 1)
    result[period - 1] = np.mean(data[:period])
    for i in range(period, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def _sma(data: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average."""
    result = np.empty_like(data)
    result[:] = np.nan
    if len(data) < period:
        return result
    for i in range(period - 1, len(data)):
        result[i] = np.mean(data[i - period + 1:i + 1])
    return result


def _rsi(data: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    result = np.empty_like(data)
    result[:] = np.nan
    if len(data) < period + 1:
        return result
    deltas = np.diff(data)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(data)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))
    return result


def _macd(data: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD: line, signal, hist."""
    ema_fast = _ema(data, fast)
    ema_slow = _ema(data, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range."""
    result = np.empty_like(close)
    result[:] = np.nan
    if len(close) < 2:
        return result
    tr = np.empty_like(close)
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    result[0] = tr[0]
    for i in range(1, len(close)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def _bb_upper(data: np.ndarray, period: int = 20, std: float = 2.0) -> np.ndarray:
    """Bollinger Bands Upper."""
    ma = _sma(data, period)
    std_arr = np.empty_like(data)
    std_arr[:] = np.nan
    for i in range(period - 1, len(data)):
        std_arr[i] = np.std(data[i - period + 1:i + 1])
    return ma + std_arr * std


def _bb_lower(data: np.ndarray, period: int = 20, std: float = 2.0) -> np.ndarray:
    """Bollinger Bands Lower."""
    ma = _sma(data, period)
    std_arr = np.empty_like(data)
    std_arr[:] = np.nan
    for i in range(period - 1, len(data)):
        std_arr[i] = np.std(data[i - period + 1:i + 1])
    return ma - std_arr * std


def _bb_middle(data: np.ndarray, period: int = 20) -> np.ndarray:
    return _sma(data, period)


def _vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """Volume Weighted Average Price (cumulative)."""
    tp = (high + low + close) / 3.0
    cum_pv = np.cumsum(tp * volume)
    cum_v = np.cumsum(volume)
    return cum_pv / np.maximum(cum_v, 1e-10)


# ── Registro de funciones disponibles ───────────────────────────────────

FUNCTIONS = {
    "EMA": _ema,
    "SMA": _sma,
    "RSI": _rsi,
    "MACD": _macd,
    "ATR": _atr,
    "BB_UPPER": _bb_upper,
    "BB_LOWER": _bb_lower,
    "BB_MIDDLE": _bb_middle,
    "VWAP": _vwap,
}


# ── Tipos de salida ─────────────────────────────────────────────────────

OUTPUT_TYPES = ("line", "shaded", "signal", "histogram")


# ── Indicador compilado ─────────────────────────────────────────────────

class CompiledIndicator:
    """Indicador personalizado listo para evaluar.

    Attributes
    ----------
    name : str
        Nombre visible
    formula : str
        Fórmula original
    color : str
        Color HEX
    output_type : str
        line | shaded | signal | histogram
    timeframe : str
        "1m", "5m", ..., "Todos"
    """

    def __init__(self, name: str, formula: str, color: str = "#ffffff",
                 output_type: str = "line", timeframe: str = "Todos"):
        self.name = name
        self.formula = formula
        self.color = color
        self.output_type = output_type
        self.timeframe = timeframe

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "formula": self.formula,
            "color": self.color,
            "output_type": self.output_type,
            "timeframe": self.timeframe,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompiledIndicator":
        return cls(
            name=d.get("name", ""),
            formula=d.get("formula", ""),
            color=d.get("color", "#ffffff"),
            output_type=d.get("output_type", "line"),
            timeframe=d.get("timeframe", "Todos"),
        )


# ── Parser de fórmulas ─────────────────────────────────────────────────

TOKEN_RE = re.compile(
    r"(\d+\.?\d*|[A-Za-z_]\w*|[+\-*/()^,]|\.)"
)


class FormulaError(ValueError):
    pass


class FormulaParser:
    """Evalúa fórmulas de indicadores sobre arrays de klines.

    Uso::

        parser = FormulaParser()
        values = parser.eval("EMA(close, 50)", klines)
        # values es np.array del largo de klines
    """

    def __init__(self):
        self._cache: dict[str, np.ndarray] = {}

    def eval(self, formula: str, klines: list) -> np.ndarray:
        """Evalúa una fórmula y devuelve un array numpy."""
        self._cache.clear()
        # Pre-cargar OHLCV en cache
        closes = np.array([float(k[4]) for k in klines], dtype=float)
        highs = np.array([float(k[2]) for k in klines], dtype=float)
        lows = np.array([float(k[3]) for k in klines], dtype=float)
        opens = np.array([float(k[1]) for k in klines], dtype=float)
        volumes = np.array([float(k[5]) for k in klines], dtype=float)
        self._cache["close"] = closes
        self._cache["high"] = highs
        self._cache["low"] = lows
        self._cache["open"] = opens
        self._cache["volume"] = volumes

        # Parsear y evaluar
        tokens = self._tokenize(formula)
        ast = self._parse(tokens)
        result = self._evaluate(ast)
        return result

    def _tokenize(self, formula: str) -> list:
        tokens = []
        for match in TOKEN_RE.finditer(formula.replace(" ", "")):
            raw = match.group(0)
            if raw in ("+", "-", "*", "/", "(", ")", ",", "^"):
                tokens.append(raw)
            elif raw.replace(".", "", 1).isdigit():
                tokens.append(("num", float(raw)))
            else:
                tokens.append(("id", raw))
        return tokens

    def _parse(self, tokens: list):
        """Parser recursivo descendente (expr → term → factor).
        Soporta funciones, paréntesis, operadores básicos.
        """
        self._pos = 0
        self._tokens = tokens
        result = self._parse_expr()
        if self._pos < len(self._tokens):
            raise FormulaError(f"Token inesperado: {self._tokens[self._pos]}")
        return result

    def _peek(self):
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _consume(self):
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _parse_expr(self):
        left = self._parse_term()
        while self._peek() in ("+", "-"):
            op = self._consume()
            right = self._parse_term()
            left = (op, left, right)
        return left

    def _parse_term(self):
        left = self._parse_factor()
        while self._peek() in ("*", "/"):
            op = self._consume()
            right = self._parse_factor()
            left = (op, left, right)
        return left

    def _parse_factor(self):
        tok = self._peek()
        if tok is None:
            raise FormulaError("Final inesperado de fórmula")
        if tok == "(":
            self._consume()
            expr = self._parse_expr()
            if self._consume() != ")":
                raise FormulaError("Se esperaba )")
            return expr
        if isinstance(tok, tuple) and tok[0] == "num":
            self._consume()
            return ("num", tok[1])
        if isinstance(tok, tuple) and tok[0] == "id":
            name = tok[1]
            self._consume()
            if self._peek() == "(":
                return self._parse_function(name)
            # Referencia a array (close, high, etc.) o función sin args
            return ("ref", name)
        raise FormulaError(f"Token inesperado: {tok}")

    def _parse_function(self, name: str):
        self._consume()  # (
        args = []
        if self._peek() != ")":
            args.append(self._parse_expr())
            while self._peek() == ",":
                self._consume()
                args.append(self._parse_expr())
        if self._consume() != ")":
            raise FormulaError("Se esperaba )")
        # Soporte para .attr como MACD(...).hist
        if self._peek() == ".":
            self._consume()
            attr = self._consume()
            if not (isinstance(attr, tuple) and attr[0] == "id"):
                raise FormulaError("Se esperaba nombre de atributo después de .")
            return ("func_attr", name, attr[1], args)
        return ("func", name, args)

    def _evaluate(self, node):
        """Evalúa el AST y devuelve un np.array."""
        if isinstance(node, np.ndarray):
            return node
        if isinstance(node, (int, float)):
            return np.full(len(self._cache.get("close", [])), node, dtype=float)

        typ = node[0] if isinstance(node, tuple) else None

        if typ == "num":
            return np.full(len(self._cache.get("close", [])), node[1], dtype=float)

        if typ == "ref":
            name = node[1]
            if name in self._cache:
                return self._cache[name]
            if name.upper() in FUNCTIONS:
                return FUNCTIONS[name.upper()](self._cache.get("close", np.array([])))
            raise FormulaError(f"Variable desconocida: {name}")

        if typ in ("+", "-", "*", "/"):
            op = typ
            left = self._evaluate(node[1])
            right = self._evaluate(node[2])
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    result = np.divide(left, right)
                    result[np.isinf(result)] = np.nan
                    result[np.isnan(result)] = 0.0
                    return result

        if typ == "func":
            name = node[1].upper()
            args = [self._evaluate(a) for a in node[2]]
            if name not in FUNCTIONS:
                raise FormulaError(f"Función desconocida: {name}")
            fn = FUNCTIONS[name]
            # Convertir args numéricos a enteros donde corresponda
            converted = []
            for arg in args:
                if isinstance(arg, np.ndarray) and arg.size > 0 and np.all(arg == arg[0]):
                    val = arg[0]
                    if not np.isnan(val) and not np.isinf(val):
                        converted.append(int(val) if "." not in str(val) else float(val))
                    else:
                        converted.append(arg)
                else:
                    converted.append(arg)
            try:
                return fn(*converted)
            except Exception as exc:
                raise FormulaError(f"Error evaluando {name}: {exc}")

        if typ == "func_attr":
            name = node[1].upper()
            attr = node[2]
            args = [self._evaluate(a) for a in node[3]]
            if name not in FUNCTIONS:
                raise FormulaError(f"Función desconocida: {name}")
            fn = FUNCTIONS[name]
            converted = []
            for arg in args:
                if isinstance(arg, np.ndarray) and arg.size > 0 and np.all(arg == arg[0]):
                    val = arg[0]
                    if not np.isnan(val) and not np.isinf(val):
                        converted.append(int(val) if "." not in str(val) else float(val))
                    else:
                        converted.append(arg)
                else:
                    converted.append(arg)
            try:
                result_dict = fn(*converted)
            except Exception as exc:
                raise FormulaError(f"Error evaluando {name}: {exc}")
            if isinstance(result_dict, dict) and attr in result_dict:
                return result_dict[attr]
            raise FormulaError(f"Función {name} no tiene atributo '{attr}'")

        raise FormulaError(f"Nodo AST no reconocido: {node}")


# ── Helper para dibujar indicadores en el chart ────────────────────────

class CustomIndicatorRenderer:
    """Renderiza indicadores personalizados en el chart.

    Se llama desde paintEvent del BB450Chart.
    """

    def __init__(self, parser: Optional[FormulaParser] = None):
        self.parser = parser or FormulaParser()
        self._indicators: list[CompiledIndicator] = []
        self._cache: dict[str, np.ndarray] = {}

    def set_indicators(self, indicators: list[CompiledIndicator]):
        self._indicators = indicators
        self._cache.clear()

    def compute(self, klines: list, timeframe: str = "1m"):
        """Pre-computa todos los indicadores para el timeframe dado."""
        self._cache.clear()
        for ind in self._indicators:
            if ind.timeframe not in ("Todos", timeframe):
                continue
            try:
                values = self.parser.eval(ind.formula, klines)
                self._cache[ind.name] = values
            except Exception as exc:
                log.warning(f"[CustomIndicator] Error computing '{ind.name}': {exc}")

    def get_values(self, name: str) -> Optional[np.ndarray]:
        return self._cache.get(name)

    def get_all(self) -> list[CompiledIndicator]:
        return self._indicators
