"""
position_marker.py — Marcadores de posiciones en velas para BB-450.

Arquitectura
────────────
  PositionMarkerManager gestiona una lista de PositionMarkers que se
  dibujan como overlays en el gráfico de velas del dashboard.

  Cada marcador muestra:
    - Icono: ▲ (long) / ▼ (short)
    - Cantidad en BTC
    - Precio de entrada
    - Tooltip con detalles (tipo orden, apalancamiento, PnL)

  Colores:
    - LONG: verde (#00ff88)
    - SHORT: rojo (#ff4444)

  Opacidad:
    - Más reciente = más brillante
    - Decae con el tiempo (fade out en 2 horas)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class PositionMarker:
    id: str
    side: str  # "LONG" or "SHORT"
    entry_price: float
    quantity_btc: float
    leverage: int
    order_type: str  # "MARKET" or "LIMIT"
    timestamp: float
    is_active: bool = True
    stop_loss: float = 0.0
    take_profit: float = 0.0
    current_pnl_pct: float = 0.0
    current_price: float = 0.0
    fade_start: float = 0.0  # timestamp when fade starts

    COLOR_LONG = "#00ff88"
    COLOR_SHORT = "#ff4444"
    FADE_DURATION = 7200  # 2 hours in seconds

    @property
    def age(self) -> float:
        return time.time() - self.timestamp

    @property
    def opacity(self) -> float:
        if not self.fade_start:
            return 1.0
        elapsed = time.time() - self.fade_start
        if elapsed >= self.FADE_DURATION:
            return 0.0
        return 1.0 - (elapsed / self.FADE_DURATION)

    @property
    def color(self) -> str:
        return self.COLOR_LONG if self.side == "LONG" else self.COLOR_SHORT

    @property
    def label(self) -> str:
        icon = "▲" if self.side == "LONG" else "▼"
        return f"{icon} {self.quantity_btc:.3f} BTC"

    @property
    def tooltip(self) -> str:
        return (
            f"{self.side} {self.quantity_btc:.4f} BTC\n"
            f"Entry: ${self.entry_price:,.0f}\n"
            f"Type: {self.order_type}\n"
            f"Leverage: {self.leverage}x\n"
            f"PnL: {self.current_pnl_pct:+.2f}%\n"
            f"Age: {self._format_age()}"
        )

    def _format_age(self) -> str:
        mins = int(self.age / 60)
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins}m"

    def update(self, current_price: float, pnl_pct: float):
        self.current_price = current_price
        self.current_pnl_pct = pnl_pct

    def close(self):
        self.is_active = False
        self.fade_start = time.time()


class PositionMarkerManager:
    """Gestiona todos los marcadores de posición en el gráfico.

    Uso
    ---
        manager = PositionMarkerManager()
        manager.add_marker(side="LONG", entry_price=68450, quantity_btc=0.05, ...)
        markers = manager.get_active_markers()
        for m in markers:
            # dibujar en el chart
            pass
    """

    def __init__(self, max_markers: int = 50):
        self._markers: dict[str, PositionMarker] = {}
        self._max_markers = max_markers
        self._counter = 0

    def add_marker(self, side: str, entry_price: float, quantity_btc: float,
                   leverage: int = 1, order_type: str = "MARKET",
                   stop_loss: float = 0.0, take_profit: float = 0.0) -> PositionMarker:
        """Añade un nuevo marcador de posición al gráfico."""
        self._counter += 1
        marker_id = f"POS_{int(time.time() * 1000)}_{self._counter}"

        marker = PositionMarker(
            id=marker_id,
            side=side.upper(),
            entry_price=entry_price,
            quantity_btc=quantity_btc,
            leverage=leverage,
            order_type=order_type.upper(),
            timestamp=time.time(),
            is_active=True,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        self._markers[marker_id] = marker
        self._prune_old()
        log.info(f"[PositionMarker] Added {side} {quantity_btc:.4f} BTC @ ${entry_price:,.0f}")
        return marker

    def close_marker(self, marker_id: str):
        """Cierra un marcador específico (inicia fade out)."""
        marker = self._markers.get(marker_id)
        if marker:
            marker.close()
            log.info(f"[PositionMarker] Closed {marker_id}")

    def close_all_active(self):
        """Cierra todos los marcadores activos."""
        for marker in self._markers.values():
            if marker.is_active:
                marker.close()

    def update_pnl(self, marker_id: str, current_price: float, pnl_pct: float):
        """Actualiza el PnL de un marcador."""
        marker = self._markers.get(marker_id)
        if marker:
            marker.update(current_price, pnl_pct)

    def get_active_markers(self) -> list[PositionMarker]:
        """Retorna todos los marcadores activos (no completamente desvanecidos)."""
        return [
            m for m in self._markers.values()
            if m.opacity > 0
        ]

    def get_visible_markers(self, min_opacity: float = 0.1) -> list[PositionMarker]:
        """Retorna marcadores con opacidad suficiente para ser visibles."""
        return [
            m for m in self._markers.values()
            if m.opacity > min_opacity
        ]

    def get_markers_on_candle(self, candle_time: float,
                               window_seconds: float = 60) -> list[PositionMarker]:
        """Retorna marcadores que estaban activos durante una vela específica."""
        return [
            m for m in self._markers.values()
            if abs(m.timestamp - candle_time) < window_seconds
        ]

    def get_all_markers(self) -> list[PositionMarker]:
        return list(self._markers.values())

    def _prune_old(self):
        """Elimina marcadores completamente desvanecidos si excedemos el máximo."""
        if len(self._markers) <= self._max_markers:
            return

        faded = [m for m in self._markers.values() if m.opacity <= 0]
        for m in faded:
            del self._markers[m.id]

        if len(self._markers) > self._max_markers:
            sorted_markers = sorted(
                self._markers.values(), key=lambda m: m.timestamp
            )
            excess = len(sorted_markers) - self._max_markers
            for m in sorted_markers[:excess]:
                del self._markers[m.id]

    def get_stats(self) -> dict:
        active = len(self.get_active_markers())
        total = len(self._markers)
        return {
            "active_markers": active,
            "total_markers": total,
            "faded_markers": total - active,
        }


position_marker_manager = PositionMarkerManager()
