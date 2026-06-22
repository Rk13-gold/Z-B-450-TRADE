"""
event_detector.py — Motor de detección de eventos de mercado para "El Trader".

Arquitectura
────────────
  EventDetector analiza el market_state cada ~1s y cuando detecta un
  evento relevante, genera un MarketEvent estructurado que alimenta al
  ScriptGenerator y al VoiceNarrator.

Eventos detectables
───────────────────
  LIQUIDATION_EVENT : Liquidaciones grandes (>10 BTC acumulado)
  SIGNAL_EVENT      : Señal LONG/SHORT del sistema
  PULLBACK_EVENT    : Pullback en tendencia establecida
  BREAKOUT_EVENT    : Ruptura de soporte/resistencia
  TRAP_EVENT        : Falsa ruptura / trampa de mercado
  REGIME_CHANGE     : Cambio de régimen de mercado
  WHALE_EVENT       : Órdenes institucionales masivas
  SPOOFING_EVENT    : Manipulación de libro detectada
  VOLATILITY_EVENT  : Explosión de volatilidad
  IMBALANCE_EVENT   : Desequilibrio extremo bid/ask
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class EventType(Enum):
    LIQUIDATION = "LIQUIDATION_EVENT"
    SIGNAL = "SIGNAL_EVENT"
    PULLBACK = "PULLBACK_EVENT"
    BREAKOUT = "BREAKOUT_EVENT"
    TRAP = "TRAP_EVENT"
    REGIME_CHANGE = "REGIME_CHANGE"
    WHALE = "WHALE_EVENT"
    SPOOFING = "SPOOFING_EVENT"
    VOLATILITY = "VOLATILITY_EVENT"
    IMBALANCE = "IMBALANCE_EVENT"


@dataclass
class MarketEvent:
    event_id: str
    event_type: EventType
    timestamp: float
    price: float
    title: str
    description: str
    severity: str  # "info", "warning", "critical"
    market_data: dict = field(default_factory=dict)
    was_narrated: bool = False
    audio_path: Optional[str] = None

    @property
    def event_code(self) -> str:
        dt = datetime.fromtimestamp(self.timestamp, tz=timezone.utc)
        return f"{self.event_type.value}_{dt.strftime('%Y%m%d_%H%M%S')}_{int(self.timestamp * 1000) % 1000:03d}"


class EventDetector:
    """Analiza el market_state y detecta eventos relevantes para narrar.

    Uso
    ---
        detector = EventDetector()
        event = detector.analyze(market_state, previous_state)
        if event:
            await script_generator.generate(event)
    """

    def __init__(self):
        self._last_state: dict = {}
        self._last_regime: str = "RANGO_INDECISO"
        self._last_event_time: dict[EventType, float] = {}
        self._cooldown: dict[EventType, float] = {
            EventType.LIQUIDATION: 30.0,
            EventType.SIGNAL: 30.0,
            EventType.PULLBACK: 60.0,
            EventType.BREAKOUT: 60.0,
            EventType.TRAP: 120.0,
            EventType.REGIME_CHANGE: 300.0,
            EventType.WHALE: 30.0,
            EventType.SPOOFING: 120.0,
            EventType.VOLATILITY: 60.0,
            EventType.IMBALANCE: 30.0,
        }
        self._price_buffer: deque = deque(maxlen=60)
        self._delta_buffer: deque = deque(maxlen=60)
        self._volume_buffer: deque = deque(maxlen=60)
        self._event_history: deque = deque(maxlen=50)
        self._last_signal_side: Optional[str] = None

    @property
    def last_event(self) -> Optional[MarketEvent]:
        return self._event_history[-1] if self._event_history else None

    def get_recent_events(self, limit: int = 10) -> list[MarketEvent]:
        return list(self._event_history)[-limit:]

    def analyze(self, state: dict) -> Optional[MarketEvent]:
        """Analiza el estado actual del mercado y retorna el evento más relevante.

        Parameters
        ----------
        state : dict
            market_state del AsyncDataEngine con indicadores, order flow, etc.

        Returns
        -------
        MarketEvent | None
            El evento más relevante detectado, o None si no hay nada nuevo.
        """
        price = state.get("price", 0)
        if not price:
            return None

        self._price_buffer.append(price)
        self._delta_buffer.append(state.get("delta", 0))
        self._volume_buffer.append(state.get("volume", 0))
        prev = self._last_state
        now = time.time()

        events: list[tuple[EventType, MarketEvent]] = []

        # 1. LIQUIDATION EVENT
        liq_event = self._check_liquidation(state, prev, now)
        if liq_event:
            events.append((EventType.LIQUIDATION, liq_event))

        # 2. SIGNAL EVENT
        sig_event = self._check_signal(state, prev, now)
        if sig_event:
            events.append((EventType.SIGNAL, sig_event))

        # 3. VOLATILITY EVENT
        vol_event = self._check_volatility(state, prev, now)
        if vol_event:
            events.append((EventType.VOLATILITY, vol_event))

        # 4. IMBALANCE EVENT
        imb_event = self._check_imbalance(state, prev, now)
        if imb_event:
            events.append((EventType.IMBALANCE, imb_event))

        # 5. WHALE EVENT
        whale_event = self._check_whale(state, prev, now)
        if whale_event:
            events.append((EventType.WHALE, whale_event))

        # 6. TRAP EVENT
        trap_event = self._check_trap(state, prev, now)
        if trap_event:
            events.append((EventType.TRAP, trap_event))

        # 7. PULLBACK / BREAKOUT
        pb_event = self._check_price_action(state, prev, now)
        if pb_event:
            events.append((pb_event.event_type, pb_event))

        # 8. REGIME CHANGE
        regime_event = self._check_regime(state, prev, now)
        if regime_event:
            events.append((EventType.REGIME_CHANGE, regime_event))

        # 9. SPOOFING EVENT
        spoof_event = self._check_spoofing(state, prev, now)
        if spoof_event:
            events.append((EventType.SPOOFING, spoof_event))

        self._last_state = dict(state)

        if not events:
            return None

        events.sort(key=lambda e: self._severity_score(e[1]), reverse=True)

        self._event_history.append(events[0][1])
        return events[0][1]

    def _can_trigger(self, etype: EventType, now: float) -> bool:
        last = self._last_event_time.get(etype, 0.0)
        cooldown = self._cooldown.get(etype, 30.0)
        return (now - last) >= cooldown

    def _severity_score(self, event: MarketEvent) -> int:
        scores = {"critical": 100, "warning": 50, "info": 10}
        return scores.get(event.severity, 0)

    def _check_liquidation(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        liq = state.get("liquidation_data", {})
        if not liq or not self._can_trigger(EventType.LIQUIDATION, now):
            return None
        total_btc = abs(liq.get("total_btc", 0))
        if total_btc < 10:
            return None
        side = liq.get("side", "unknown")
        price = state.get("price", 0)
        severity = "critical" if total_btc > 100 else "warning" if total_btc > 30 else "info"
        return MarketEvent(
            event_id=f"LIQ_{int(now)}",
            event_type=EventType.LIQUIDATION,
            timestamp=now,
            price=price,
            title=f"Liquidación {side.upper()} de {total_btc:.1f} BTC",
            description=f"Liquidación masiva detectada en el lado {side}: {total_btc:.2f} BTC liquidados",
            severity=severity,
            market_data={"total_btc": total_btc, "side": side, "price": price},
        )

    def _check_signal(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        signal = state.get("signal_text", "WAIT")
        if signal not in ("LONG", "SHORT") or not self._can_trigger(EventType.SIGNAL, now):
            return None
        if signal == self._last_signal_side:
            return None
        self._last_signal_side = signal
        price = state.get("price", 0)
        confidence = state.get("confidence", 0)
        sl = state.get("stop_loss", 0)
        tp = state.get("take_profit", 0)
        qty = state.get("position_qty_btc", 0)
        return MarketEvent(
            event_id=f"SIG_{int(now)}",
            event_type=EventType.SIGNAL,
            timestamp=now,
            price=price,
            title=f"Señal {signal} detectada ({confidence:.0f}% confianza)",
            description=f"Oportunidad de {signal} a ${price:,.0f} con {confidence:.0f}% de confianza. SL: ${sl:,.0f} TP: ${tp:,.0f}",
            severity="warning" if confidence > 75 else "info",
            market_data={
                "side": signal, "confidence": confidence, "price": price,
                "stop_loss": sl, "take_profit": tp, "quantity_btc": qty,
            },
        )

    def _check_volatility(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        vol_exp = state.get("volatility_explosion", False)
        if not vol_exp or not self._can_trigger(EventType.VOLATILITY, now):
            return None
        tick_speed = state.get("tick_speed", 0)
        price = state.get("price", 0)
        return MarketEvent(
            event_id=f"VOL_{int(now)}",
            event_type=EventType.VOLATILITY,
            timestamp=now,
            price=price,
            title="Explosión de volatilidad detectada",
            description=f"Velocidad de ticks inusual: {tick_speed:.0f} t/s. Alta actividad de mercado en curso.",
            severity="warning",
            market_data={"tick_speed": tick_speed, "price": price},
        )

    def _check_imbalance(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        imb = state.get("depth_imb_pct", 0)
        if not self._can_trigger(EventType.IMBALANCE, now):
            return None
        if abs(imb) < 60:
            return None
        price = state.get("price", 0)
        side = "COMPRA" if imb > 0 else "VENTA"
        return MarketEvent(
            event_id=f"IMB_{int(now)}",
            event_type=EventType.IMBALANCE,
            timestamp=now,
            price=price,
            title=f"Desequilibrio extremo {side} ({abs(imb):.0f}%)",
            description=f"Presión de {side} dominando el libro con {abs(imb):.0f}% de desequilibrio en profundidad.",
            severity="info",
            market_data={"imbalance_pct": imb, "side": side, "price": price},
        )

    def _check_whale(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        delta_accel = state.get("delta_accel", 0)
        tick_speed = state.get("tick_speed", 0)
        if not self._can_trigger(EventType.WHALE, now):
            return None
        if abs(delta_accel) < 100 or tick_speed < 25:
            return None
        price = state.get("price", 0)
        side = "COMPRA" if delta_accel > 0 else "VENTA"
        btc_vol = abs(delta_accel) * 0.01
        return MarketEvent(
            event_id=f"WHL_{int(now)}",
            event_type=EventType.WHALE,
            timestamp=now,
            price=price,
            title=f"Ballena {side} detectada (~{btc_vol:.1f} BTC)",
            description=f"Flujo institucional masivo de {side} con aceleración de delta de {abs(delta_accel):.0f}.",
            severity="warning" if btc_vol > 50 else "info",
            market_data={"delta_accel": delta_accel, "side": side, "estimated_btc": btc_vol},
        )

    def _check_trap(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        trap = state.get("trap_status", "SIN TRAMPA")
        if trap == "SIN TRAMPA" or not self._can_trigger(EventType.TRAP, now):
            return None
        price = state.get("price", 0)
        trap_type = "alcista" if "ALCISTA" in trap else "bajista"
        return MarketEvent(
            event_id=f"TRP_{int(now)}",
            event_type=EventType.TRAP,
            timestamp=now,
            price=price,
            title=f"Trampa {trap_type} detectada",
            description=f"Posible trampa de mercado {trap_type} identificada en ${price:,.0f}. Operar con cautela.",
            severity="critical",
            market_data={"trap_type": trap, "price": price},
        )

    def _check_price_action(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        if not prev or not self._can_trigger(EventType.PULLBACK, now):
            return None
        prev_price = prev.get("price", 0)
        price = state.get("price", 0)
        if prev_price == 0:
            return None
        change_pct = ((price - prev_price) / prev_price) * 100
        if abs(change_pct) < 0.3:
            return None
        trend = state.get("trend", "NEUTRAL")
        bb_pos = state.get("bb_position", 50)
        is_pullback = (
            (trend == "ALCISTA" and change_pct < 0 and bb_pos < 50) or
            (trend == "BAJISTA" and change_pct > 0 and bb_pos > 50)
        )
        is_breakout = (
            (trend == "ALCISTA" and change_pct > 0 and bb_pos > 80) or
            (trend == "BAJISTA" and change_pct < 0 and bb_pos < 20)
        )
        price = state.get("price", 0)
        if is_pullback:
            return MarketEvent(
                event_id=f"PUL_{int(now)}",
                event_type=EventType.PULLBACK,
                timestamp=now,
                price=price,
                title=f"Pullback en tendencia {trend} ({change_pct:+.2f}%)",
                description=f"Movimiento de {abs(change_pct):.2f}% en dirección opuesta a la tendencia {trend}.",
                severity="info",
                market_data={"change_pct": change_pct, "trend": trend, "bb_position": bb_pos},
            )
        if is_breakout:
            return MarketEvent(
                event_id=f"BRK_{int(now)}",
                event_type=EventType.BREAKOUT,
                timestamp=now,
                price=price,
                title=f"Ruptura {trend} ({change_pct:+.2f}%)",
                description=f"Precio rompiendo en dirección {trend} con movimiento de {abs(change_pct):.2f}%.",
                severity="warning",
                market_data={"change_pct": change_pct, "trend": trend, "bb_position": bb_pos},
            )
        return None

    def _check_regime(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        current = state.get("regimen_mercado", "RANGO_INDECISO")
        if current == self._last_regime or not self._can_trigger(EventType.REGIME_CHANGE, now):
            return None
        old = self._last_regime
        self._last_regime = current
        price = state.get("price", 0)
        return MarketEvent(
            event_id=f"RGM_{int(now)}",
            event_type=EventType.REGIME_CHANGE,
            timestamp=now,
            price=price,
            title=f"Cambio de régimen: {old} → {current}",
            description=f"El mercado ha cambiado de régimen de '{old}' a '{current}'.",
            severity="warning",
            market_data={"old_regime": old, "new_regime": current, "price": price},
        )

    def _check_spoofing(self, state: dict, prev: dict, now: float) -> Optional[MarketEvent]:
        spoof = state.get("spoofing_risk", 0)
        if spoof < 70 or not self._can_trigger(EventType.SPOOFING, now):
            return None
        price = state.get("price", 0)
        return MarketEvent(
            event_id=f"SPF_{int(now)}",
            event_type=EventType.SPOOFING,
            timestamp=now,
            price=price,
            title=f"Spoofing detectado ({spoof:.0f}% riesgo)",
            description=f"Manipulación de libro detectada con {spoof:.0f}% de riesgo. Probable orden falsa en el book.",
            severity="warning",
            market_data={"spoofing_risk": spoof, "price": price},
        )


event_detector = EventDetector()
