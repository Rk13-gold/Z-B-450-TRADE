"""
order_flow_animation.py — Animación de flujo de órdenes en tiempo real.

Arquitectura
────────────
  OrderFlowAnimation genera burbujas/partículas que representan órdenes
  de mercado ejecutándose en tiempo real sobre el gráfico de velas.

  Cada burbuja:
    - Aparece en el precio de ejecución
    - Tamaño proporcional al monto en BTC
    - Color: verde (buy) / rojo (sell)
    - Animación: aparece → se expande → se desvanece (3 segundos)
    - Tooltip: "Market Buy 0.05 BTC @ $68,450"

  CandleAnnotation:
    - Marca una operación institucional/ballena sobre una vela específica
    - Se dibuja como diamante sobre el cuerpo de la vela
    - Solo para trades ≥ WHALE_MIN_BTC
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

ANIMATION_DURATION = 3.0  # segundos que dura la animación
MAX_BUBBLES = 200
BUBBLE_MIN_SIZE = 5    # px
BUBBLE_MAX_SIZE = 40   # px
BTC_REFERENCE = 1.0    # 1 BTC = tamaño de referencia
WHALE_MIN_BTC = 0.5    # mínimo BTC para considerar ballena institucional
MAX_CANDLE_ANNOTATIONS = 200


@dataclass
class OrderBubble:
    id: str
    side: str  # "BUY" or "SELL"
    price: float
    quantity_btc: float
    timestamp: float
    order_type: str = "MARKET"  # "MARKET" or "LIMIT"

    COLOR_BUY = "#00ff88"
    COLOR_SELL = "#ff4444"

    @property
    def color(self) -> str:
        return self.COLOR_BUY if self.side == "BUY" else self.COLOR_SELL

    @property
    def age(self) -> float:
        return time.time() - self.timestamp

    @property
    def progress(self) -> float:
        """0.0 = recién apareció, 1.0 = debe desaparecer."""
        return min(self.age / ANIMATION_DURATION, 1.0)

    @property
    def opacity(self) -> float:
        """Opacidad: máxima al 30% de la animación, luego decae."""
        if self.progress < 0.3:
            return self.progress / 0.3
        return 1.0 - ((self.progress - 0.3) / 0.7)

    @property
    def size(self) -> float:
        """Tamaño proporcional a la cantidad en BTC."""
        ratio = self.quantity_btc / BTC_REFERENCE
        size = BUBBLE_MIN_SIZE + (math.log2(ratio + 1) * 10)
        return min(size, BUBBLE_MAX_SIZE)

    @property
    def y_offset(self) -> float:
        """Desplazamiento vertical durante la animación (flota hacia arriba/abajo)."""
        if self.side == "BUY":
            return -self.progress * 10  # flota hacia arriba
        return self.progress * 10       # flota hacia abajo

    @property
    def tooltip(self) -> str:
        return (
            f"{self.order_type} {self.side}\n"
            f"{self.quantity_btc:.4f} BTC @ ${self.price:,.0f}"
        )

    def is_expired(self) -> bool:
        return self.progress >= 1.0


@dataclass
class CandleAnnotation:
    """Marca institucional/ballena en una vela específica.

    Se dibuja como diamante coloreado sobre el cuerpo de la vela.
    """
    id: str
    side: str  # "BUY" or "SELL"
    price: float
    quantity_btc: float
    candle_open_time: int  # ms timestamp de la vela a la que pertenece
    trade_time_ms: int     # ms timestamp del trade
    timestamp: float = field(default_factory=time.time)

    COLOR_BUY = "#00ff88"
    COLOR_SELL = "#ff4444"
    ANNOTATION_LIFETIME = 60.0  # segundos visibles

    @property
    def color(self) -> str:
        return self.COLOR_BUY if self.side == "BUY" else self.COLOR_SELL

    @property
    def age(self) -> float:
        return time.time() - self.timestamp

    @property
    def opacity(self) -> float:
        elapsed = time.time() - self.timestamp
        if elapsed >= self.ANNOTATION_LIFETIME:
            return 0.0
        return 1.0 - (elapsed / self.ANNOTATION_LIFETIME)

    @property
    def diamond_size(self) -> float:
        ratio = self.quantity_btc / BTC_REFERENCE
        size = 6 + (math.log2(ratio + 1) * 4)
        return min(size, 20)

    @property
    def tooltip(self) -> str:
        return (
            f"{'🐋' if self.quantity_btc >= 1.0 else '📊'} "
            f"{self.side} {self.quantity_btc:.3f} BTC @ ${self.price:,.0f}"
        )

    def is_expired(self) -> bool:
        return self.age >= self.ANNOTATION_LIFETIME


class OrderFlowAnimationManager:
    """Gestiona las burbujas de animación del order flow + anotaciones en velas.

    Uso
    ---
        manager = OrderFlowAnimationManager()
        manager.add_trade(side="BUY", price=68450, quantity=0.05)
        bubbles = manager.get_active_bubbles()
        for b in bubbles:
            # dibujar en el chart
            pass
    """

    def __init__(self):
        self._bubbles: deque[OrderBubble] = deque(maxlen=MAX_BUBBLES)
        self._candle_annotations: deque[CandleAnnotation] = deque(maxlen=MAX_CANDLE_ANNOTATIONS)
        self._counter = 0
        self._min_btc_for_animation = WHALE_MIN_BTC  # solo ballenas (0.5 BTC)

    def set_min_btc(self, min_btc: float):
        """Configura el mínimo de BTC para mostrar animación."""
        self._min_btc_for_animation = max(0.001, min_btc)

    def add_trade(self, side: str, price: float, quantity: float,
                  order_type: str = "MARKET",
                  candle_open_time: int = 0,
                  trade_time_ms: int = 0) -> Optional[OrderBubble]:
        """Añade un trade a la animación.

        Parameters
        ----------
        side : str
            "BUY" or "SELL"
        price : float
            Precio de ejecución
        quantity : float
            Cantidad en BTC
        order_type : str
            "MARKET" or "LIMIT"
        candle_open_time : int
            Timestamp ms de la vela a la que pertenece este trade
        trade_time_ms : int
            Timestamp ms del trade

        Returns
        -------
        OrderBubble | None
            La burbuja creada, o None si el trade es muy pequeño.
        """
        if quantity < self._min_btc_for_animation:
            return None

        self._counter += 1
        now_ms = int(time.time() * 1000)
        bubble = OrderBubble(
            id=f"BUBBLE_{int(time.time() * 1000)}_{self._counter}",
            side=side.upper(),
            price=price,
            quantity_btc=quantity,
            timestamp=time.time(),
            order_type=order_type.upper(),
        )

        self._bubbles.append(bubble)

        # También crear una CandleAnnotation si tenemos información de vela
        if candle_open_time > 0:
            ann = CandleAnnotation(
                id=f"WHALE_{now_ms}_{self._counter}",
                side=side.upper(),
                price=price,
                quantity_btc=quantity,
                candle_open_time=candle_open_time,
                trade_time_ms=trade_time_ms if trade_time_ms > 0 else now_ms,
            )
            self._candle_annotations.append(ann)

        return bubble

    def add_trade_from_stream(self, trade_data: dict,
                               candle_open_time: int = 0) -> Optional[OrderBubble]:
        """Añade un trade desde el stream de aggTrades de Binance.

        trade_data format:
        {
            "price": 68450.0,
            "quantity": 0.05,
            "is_buyer_maker": False,  # False = aggressive buy
        }
        """
        price = trade_data.get("price", 0)
        quantity = trade_data.get("quantity", 0)
        is_buyer_maker = trade_data.get("is_buyer_maker", True)
        trade_time = trade_data.get("time", 0)

        if not price or not quantity:
            return None

        side = "SELL" if is_buyer_maker else "BUY"
        return self.add_trade(side, price, quantity, "MARKET",
                              candle_open_time=candle_open_time,
                              trade_time_ms=trade_time)

    def get_active_bubbles(self) -> list[OrderBubble]:
        """Retorna todas las burbujas activas (no expiradas)."""
        self._clean_expired()
        return list(self._bubbles)

    def _clean_expired(self):
        while self._bubbles and self._bubbles[0].is_expired():
            self._bubbles.popleft()

    # ── Métodos para CandleAnnotations ──

    def get_candle_annotations(self) -> list[CandleAnnotation]:
        """Retorna todas las anotaciones de vela activas."""
        self._clean_annotations()
        return list(self._candle_annotations)

    def get_annotations_for_candle(self, candle_open_time: int) -> list[CandleAnnotation]:
        """Retorna anotaciones para una vela específica (por su open_time ms)."""
        self._clean_annotations()
        return [
            ann for ann in self._candle_annotations
            if ann.candle_open_time == candle_open_time
        ]

    def _clean_annotations(self):
        while self._candle_annotations and self._candle_annotations[0].is_expired():
            self._candle_annotations.popleft()

    def get_bubbles_in_price_range(self, min_price: float,
                                    max_price: float) -> list[OrderBubble]:
        """Retorna burbujas dentro de un rango de precio."""
        return [
            b for b in self.get_active_bubbles()
            if min_price <= b.price <= max_price
        ]

    def clear(self):
        self._bubbles.clear()
        self._candle_annotations.clear()

    def get_stats(self) -> dict:
        return {
            "active_bubbles": len(self._bubbles),
            "candle_annotations": len(self._candle_annotations),
            "min_btc": self._min_btc_for_animation,
        }


order_flow_animation = OrderFlowAnimationManager()
