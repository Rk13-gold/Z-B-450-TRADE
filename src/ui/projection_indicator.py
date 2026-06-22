"""
projection_indicator.py — Indicador de proyección de señales LONG/SHORT.

Arquitectura
────────────
  ProjectionIndicator muestra en el gráfico la proyección de una señal
  de trading con:
    - Línea de entrada (sólida, color de la dirección)
    - Stop Loss (línea punteada roja/verde)
    - Take Profit 1 y 2 (líneas punteadas)
    - Área sombreada entre entrada y SL/TP
    - Flecha direccional animada
    - Texto informativo con detalles de la posición

  Cuando se cierra la posición o se cancela la señal, el indicador
  se desvanece gradualmente.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

FADE_DURATION = 60.0  # segundos para desvanecer indicador tras cierre


@dataclass
class ProjectionIndicator:
    id: str
    side: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    quantity_btc: float
    confidence: float
    timestamp: float
    is_active: bool = True
    is_filled: bool = False  # True si la orden ya se ejecutó
    fill_price: float = 0.0
    current_pnl_pct: float = 0.0
    close_time: float = 0.0

    COLOR_LONG = "#00ff88"
    COLOR_SHORT = "#ff4444"
    COLOR_SL = "#ff6666"
    COLOR_TP1 = "#88ff88"
    COLOR_TP2 = "#44cc44"

    @property
    def color(self) -> str:
        return self.COLOR_LONG if self.side == "LONG" else self.COLOR_SHORT

    @property
    def opacity(self) -> float:
        if self.is_active:
            return 1.0
        if not self.close_time:
            return 0.0
        elapsed = time.time() - self.close_time
        if elapsed >= FADE_DURATION:
            return 0.0
        return 1.0 - (elapsed / FADE_DURATION)

    @property
    def age(self) -> float:
        return time.time() - self.timestamp

    @property
    def sl_distance_pct(self) -> float:
        if self.side == "LONG":
            return ((self.entry_price - self.stop_loss) / self.entry_price) * 100
        return ((self.stop_loss - self.entry_price) / self.entry_price) * 100

    @property
    def tp1_distance_pct(self) -> float:
        if self.side == "LONG":
            return ((self.take_profit_1 - self.entry_price) / self.entry_price) * 100
        return ((self.entry_price - self.take_profit_1) / self.entry_price) * 100

    @property
    def risk_reward_1(self) -> float:
        sl_dist = abs(self.entry_price - self.stop_loss)
        tp_dist = abs(self.take_profit_1 - self.entry_price)
        if sl_dist == 0:
            return 0
        return tp_dist / sl_dist

    @property
    def label(self) -> str:
        icon = "🟢 LONG" if self.side == "LONG" else "🔴 SHORT"
        return (
            f"{icon}\n"
            f"Entry: ${self.entry_price:,.0f}\n"
            f"Size: {self.quantity_btc:.4f} BTC\n"
            f"SL: ${self.stop_loss:,.0f} ({self.sl_distance_pct:.2f}%)\n"
            f"TP1: ${self.take_profit_1:,.0f} ({self.tp1_distance_pct:.2f}%)\n"
            f"TP2: ${self.take_profit_2:,.0f}\n"
            f"R:R: 1:{self.risk_reward_1:.2f}\n"
            f"Conf: {self.confidence:.0f}%"
        )

    @property
    def short_label(self) -> str:
        icon = "▲" if self.side == "LONG" else "▼"
        return f"{icon} {self.side} {self.quantity_btc:.3f} BTC @ ${self.entry_price:,.0f}"

    def close(self):
        self.is_active = False
        self.close_time = time.time()

    def mark_filled(self, fill_price: float):
        self.is_filled = True
        self.fill_price = fill_price

    def update_pnl(self, pnl_pct: float):
        self.current_pnl_pct = pnl_pct


class ProjectionIndicatorManager:
    """Gestiona los indicadores de proyección en el gráfico.

    Uso
    ---
        manager = ProjectionIndicatorManager()
        indicator = manager.create_indicator(side="LONG", entry_price=68450, ...)
        indicators = manager.get_active_indicators()
        for ind in indicators:
            # dibujar líneas de proyección en el chart
            pass
    """

    def __init__(self, max_history: int = 20):
        self._indicators: dict[str, ProjectionIndicator] = {}
        self._counter = 0
        self._max_history = max_history

    def create_indicator(self, side: str, entry_price: float,
                          stop_loss: float, take_profit_1: float,
                          take_profit_2: float = 0.0,
                          quantity_btc: float = 0.0,
                          confidence: float = 0.0) -> ProjectionIndicator:
        """Crea un nuevo indicador de proyección."""
        self._counter += 1
        ind_id = f"PROJ_{int(time.time() * 1000)}_{self._counter}"

        if take_profit_2 <= 0:
            if side.upper() == "LONG":
                take_profit_2 = take_profit_1 + (take_profit_1 - entry_price)
            else:
                take_profit_2 = take_profit_1 - (entry_price - take_profit_1)

        indicator = ProjectionIndicator(
            id=ind_id,
            side=side.upper(),
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            quantity_btc=quantity_btc,
            confidence=confidence,
            timestamp=time.time(),
        )

        self._indicators[ind_id] = indicator
        log.info(
            f"[Projection] Created {side} indicator: "
            f"Entry=${entry_price:,.0f} SL=${stop_loss:,.0f} "
            f"TP1=${take_profit_1:,.0f} TP2=${take_profit_2:,.0f}"
        )
        return indicator

    def close_indicator(self, ind_id: str):
        """Cierra un indicador (inicia fade out)."""
        ind = self._indicators.get(ind_id)
        if ind:
            ind.close()
            self._prune_old()

    def close_all(self):
        for ind in self._indicators.values():
            if ind.is_active:
                ind.close()

    def mark_filled(self, ind_id: str, fill_price: float):
        ind = self._indicators.get(ind_id)
        if ind:
            ind.mark_filled(fill_price)

    def update_pnl(self, ind_id: str, pnl_pct: float):
        ind = self._indicators.get(ind_id)
        if ind:
            ind.update_pnl(pnl_pct)

    def get_active_indicators(self) -> list[ProjectionIndicator]:
        return [
            ind for ind in self._indicators.values()
            if ind.opacity > 0
        ]

    def get_visible_indicators(self, min_opacity: float = 0.1) -> list[ProjectionIndicator]:
        return [
            ind for ind in self._indicators.values()
            if ind.opacity > min_opacity
        ]

    def get_latest_active(self) -> Optional[ProjectionIndicator]:
        active = self.get_active_indicators()
        if not active:
            return None
        return max(active, key=lambda ind: ind.timestamp)

    def get_by_side(self, side: str) -> list[ProjectionIndicator]:
        return [
            ind for ind in self._indicators.values()
            if ind.side == side.upper()
        ]

    def _prune_old(self):
        fully_faded = [
            ind for ind in self._indicators.values()
            if ind.opacity <= 0
        ]
        for ind in fully_faded:
            del self._indicators[ind.id]

        if len(self._indicators) > self._max_history:
            sorted_inds = sorted(
                self._indicators.values(), key=lambda ind: ind.timestamp
            )
            excess = len(sorted_inds) - self._max_history
            for ind in sorted_inds[:excess]:
                del self._indicators[ind.id]

    def get_stats(self) -> dict:
        active = len(self.get_active_indicators())
        total = len(self._indicators)
        latest = self.get_latest_active()
        return {
            "active_indicators": active,
            "total_indicators": total,
            "latest_side": latest.side if latest else None,
            "latest_age_s": latest.age if latest else 0,
        }


projection_indicator_manager = ProjectionIndicatorManager()
