"""
trader_narrator.py — Orquestador central "El Trader".

Arquitectura
────────────
  TraderNarratorOrchestrator coordina:
  1. EventDetector   → detecta eventos del mercado
  2. ScriptGenerator → genera guiones con Gemini
  3. VoiceNarrator   → genera audio con Edge TTS
  4. TraderPanel     → muestra en el dashboard

  También integra con position_marker_manager y
  projection_indicator_manager para marcar señales en el gráfico.

Flujo completo
──────────────
  market_state → EventDetector.analyze() → MarketEvent
    → ScriptGenerator.generate() → NarrationScript
      → VoiceNarrator.generate_audio() → .mp3
        → TraderPanel.on_event() → display + play

  Si es SIGNAL_EVENT:
    → ProjectionIndicatorManager.create_indicator()
    → PositionMarkerManager.add_marker()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from src.engine.event_detector import (
    EventDetector, EventType, MarketEvent, event_detector,
)
from src.engine.script_generator import (
    NarrationScript, ScriptGenerator, script_generator,
)
from src.engine.voice_narrator import (
    VoiceNarrator, voice_narrator,
)
from src.ui.projection_indicator import (
    ProjectionIndicator, ProjectionIndicatorManager,
    projection_indicator_manager,
)
from src.ui.position_marker import (
    PositionMarker, PositionMarkerManager,
    position_marker_manager,
)

log = logging.getLogger(__name__)


class TraderNarrator:
    """Orquestador central del sistema "El Trader".

    Uso
    ---
        narrator = TraderNarrator()
        narrator.set_panel_callback(lambda event, script: ...)
        # En el loop principal:
        await narrator.process_market_state(market_state)
    """

    def __init__(self):
        self._event_detector: EventDetector = event_detector
        self._script_generator: ScriptGenerator = script_generator
        self._voice_narrator: VoiceNarrator = voice_narrator
        self._panel_callback: Optional[Callable] = None
        self._enabled: bool = True
        self._last_process_time: float = 0.0
        self._process_interval: float = 1.0
        self._pending_script: Optional[NarrationScript] = None

    def set_panel_callback(self, callback: Callable):
        """Configura el callback para actualizar el panel UI.

        callback(event: MarketEvent, script: NarrationScript | None)
        """
        self._panel_callback = callback

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def set_interval(self, seconds: float):
        self._process_interval = max(0.5, seconds)

    async def process_market_state(self, market_state: dict) -> Optional[MarketEvent]:
        """Procesa un market_state y genera narración si es relevante.

        Parameters
        ----------
        market_state : dict
            Estado actual del mercado del AsyncDataEngine.

        Returns
        -------
        MarketEvent | None
            El evento detectado (si hay), None en caso contrario.
        """
        if not self._enabled:
            return None

        now = time.time()
        if now - self._last_process_time < self._process_interval:
            return None
        self._last_process_time = now

        event = self._event_detector.analyze(market_state)
        if not event:
            return None

        log.info(f"[TraderNarrator] Event detected: {event.event_type.value} - {event.title}")

        script = await self._script_generator.generate(event, market_state)
        if not script:
            script = self._script_generator._fallback_script(event, market_state)

        audio_path = await self._voice_narrator.generate_audio(script)
        if audio_path:
            script.audio_path = audio_path
            event.audio_path = audio_path
            event.was_narrated = True
            log.info(f"[TraderNarrator] Audio generated: {audio_path}")

        if event.event_type == EventType.SIGNAL:
            self._handle_signal_event(event, market_state)

        if self._panel_callback:
            try:
                self._panel_callback(event, script)
            except Exception as e:
                log.error(f"[TraderNarrator] Panel callback error: {e}")

        return event

    def _handle_signal_event(self, event: MarketEvent, state: dict):
        """Maneja eventos de señal: crea indicadores de proyección y marcadores."""
        md = event.market_data
        side = md.get("side", "LONG")
        price = event.price
        sl = md.get("stop_loss", price * 0.98 if side == "LONG" else price * 1.02)
        tp = md.get("take_profit", price * 1.02 if side == "LONG" else price * 0.98)
        qty = md.get("quantity_btc", 0)
        confidence = md.get("confidence", 0)

        tp2 = price * 1.03 if side == "LONG" else price * 0.97

        projection_indicator_manager.create_indicator(
            side=side,
            entry_price=price,
            stop_loss=sl,
            take_profit_1=tp,
            take_profit_2=tp2,
            quantity_btc=qty,
            confidence=confidence,
        )

        if qty > 0:
            position_marker_manager.add_marker(
                side=side,
                entry_price=price,
                quantity_btc=qty,
                order_type="MARKET",
                stop_loss=sl,
                take_profit=tp,
            )

    def get_stats(self) -> dict:
        return {
            "enabled": self._enabled,
            "event_detector": {
                "total_events": len(self._event_detector._event_history),
            },
            "projection": projection_indicator_manager.get_stats(),
            "position_markers": position_marker_manager.get_stats(),
            "voice": self._voice_narrator.get_stats(),
        }

    def get_recent_events(self, limit: int = 10) -> list[MarketEvent]:
        return self._event_detector.get_recent_events(limit)


trader_narrator = TraderNarrator()
