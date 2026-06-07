"""
narrative_journal.py — Automated Trading Journal (Bitácora Narrativa Autónoma).

Architecture
────────────
  NarrativeJournal  :  Detects post-volatility events, asks Gemini to write
                        a human-readable Markdown lesson, and saves it to
                        the CONCMT/ folder for future knowledge ingestion.

Flow
────
  1. evaluate_event() is called 5 min after a brain alert or trade close.
  2. If price moved > 0.5 % or trade was FAILED, crafts a structured prompt.
  3. Gemini 2.0 Flash writes a Markdown post-mortem.
  4. Saved as CONCMT/YYYYMMDD_HHMM_lesson.md → absorbed by KnowledgeParserWorker
     on next start / knowledge reload.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

CONCMT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "CONCMT")

_JOURNAL_PROMPT = """\
Eres un trader institucional escribiendo en tu bitácora personal después de una operación.

Basado en los datos de mercado y el resultado de la operación, redacta una entrada
de bitácora en formato Markdown con la siguiente estructura exacta:

## Contexto del Movimiento
- Precio: $X
- Predicción: [ALZA/BAJA] @ XX%
- Resultado: [ACIERTO / FALLO / PARCIAL]
- Delta: +X | CVD: X | Volumen: X | Trampa: X

## Análisis Post-Mortem
Explica en 3-4 oraciones QUÉ pasó en la microestructura:
- ¿Por qué falló o acertó el Delta?
- ¿Qué trampa de liquidez detectaste (spoofing, absorción, caza de stops)?
- ¿Había divergencia entre precio y flujo de órdenes?

## Lección Aprendida
Escribe 1-2 lecciones concretas y ACCIONABLES que evitarán repetir este error.

## Tags
`#leccion` `#[trampa/divergencia/acierto]` `#[direccion]`

DATOS DE LA OPERACIÓN:
{data}
"""


class NarrativeJournal:
    """Post-event journaling engine.

    Usage
    -----
        journal = NarrativeJournal(gemini_client)
        await journal.evaluate_event(snapshot_before, snapshot_after, result)
    """

    def __init__(self, gemini_client=None):
        self._gemini_client = gemini_client
        self._gemini_enabled = gemini_client is not None
        self._last_check: Dict[str, float] = {}

        # Ensure CONCMT dir exists
        Path(CONCMT_DIR).mkdir(parents=True, exist_ok=True)

    @property
    def is_enabled(self) -> bool:
        return self._gemini_enabled

    def set_gemini_client(self, client):
        self._gemini_client = client
        self._gemini_enabled = client is not None

    async def evaluate_event(self, snapshot_before: Dict[str, Any],
                              snapshot_now: Dict[str, Any],
                              trade_result: Optional[str] = None,
                              brain_direction: Optional[str] = None,
                              brain_confidence: float = 0.0) -> Optional[str]:
        """Evaluate if a journal entry should be written.

        Parameters
        ----------
        snapshot_before : dict
            Market state when the alert was triggered.
        snapshot_now : dict
            Current market state (N minutes later).
        trade_result : str | None
            'SUCCESS', 'FAILED', 'PARTIAL', or None.
        brain_direction : str | None
            'ALZA', 'BAJA', 'INCIERTO'.
        brain_confidence : float
            Confidence 0-100.

        Returns
        -------
        str | None
            Path to the written .md file, or None if no entry.
        """
        if not self._gemini_enabled or not self._gemini_client:
            return None

        p_before = float(snapshot_before.get('price', 0))
        p_now = float(snapshot_now.get('price', 0))
        change_pct = ((p_now - p_before) / max(p_before, 1)) * 100

        # Determine if event is worth journaling
        if trade_result == 'FAILED':
            pass  # always journal failed trades
        elif abs(change_pct) < 0.5 and trade_result != 'FAILED':
            return None  # skip insignificant moves

        # Auto-detect success/failure if not provided
        if trade_result is None and brain_direction:
            if (brain_direction == 'ALZA' and change_pct > 0.3) or \
               (brain_direction == 'BAJA' and change_pct < -0.3):
                trade_result = 'SUCCESS'
            elif (brain_direction == 'ALZA' and change_pct < -0.3) or \
                 (brain_direction == 'BAJA' and change_pct > 0.3):
                trade_result = 'FAILED'
            else:
                trade_result = 'PARTIAL'

        # Build data context for the prompt
        data_lines = [
            f"Precio antes: ${p_before:,.0f}",
            f"Precio ahora: ${p_now:,.0f}",
            f"Cambio: {change_pct:+.2f}%",
            f"Predicción: {brain_direction or 'N/A'} @ {brain_confidence:.0f}%",
            f"Resultado: {trade_result}",
            f"Delta: {snapshot_before.get('delta', 0):+.0f}",
            f"CVD: {snapshot_before.get('cvd', 0):+.1f}",
            f"Delta Accel: {snapshot_before.get('delta_accel', 0):+.1f}",
            f"Volumen: {snapshot_before.get('volume', 0):.1f}",
            f"B/A Ratio: {snapshot_before.get('ba_ratio', 1):.3f}x",
            f"Trampa: {snapshot_before.get('trap_status', 'N/A')}",
            f"RSI: {snapshot_before.get('rsi', 50):.1f}",
            f"BB Posición: {snapshot_before.get('bb_position', 50):.1f}%",
            f"Tick Speed: {snapshot_before.get('tick_speed', 0):.1f}/s",
            f"Cancel Rate: {snapshot_before.get('cancel_rate', 0):.1f}%",
            f"Fuerza: {snapshot_before.get('force', 'N/A')}",
        ]
        data_str = "\n".join(data_lines)
        prompt = _JOURNAL_PROMPT.replace("{data}", data_str)

        try:
            markdown = await self._ask_gemini(prompt)
            if not markdown:
                return None

            ts = datetime.now(timezone.utc)
            filename = f"{ts.strftime('%Y%m%d_%H%M')}_lesson.md"
            path = os.path.join(CONCMT_DIR, filename)

            header = (
                f"# Lección: {ts.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"## Resultado: {trade_result}\n\n"
            )
            full = header + markdown
            with open(path, 'w', encoding='utf-8') as f:
                f.write(full)

            log.info("[NarrativeJournal] Bitácora guardada: %s", path)
            return path

        except Exception as e:
            log.warning("[NarrativeJournal] Error: %s", e)
            return None

    async def _ask_gemini(self, prompt: str) -> Optional[str]:
        """Call Gemini 2.0 Flash with the journaling prompt."""
        try:
            from google.genai import types
            resp = await self._gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=600,
                ),
            )
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text if resp else ""
            texto = texto_completo.strip()
            return texto if texto else None
        except Exception as e:
            log.warning("[NarrativeJournal] Gemini error: %s", e)
            return None

    async def evaluate_last_alert(self, alert_snapshot: Dict[str, Any],
                                   current_snapshot: Dict[str, Any],
                                   episodic_memory) -> Optional[str]:
        """Convenience: evaluate last brain alert using episodic memory.

        Looks up similar past events and includes them for richer context.
        """
        brain_dir = alert_snapshot.get('brain_direction',
                                        alert_snapshot.get('signal_text'))
        brain_conf = alert_snapshot.get('brain_confidence_pct',
                                         alert_snapshot.get('confidence', 0))

        # Determine trade result from price movement
        p_before = float(alert_snapshot.get('price', 0))
        p_now = float(current_snapshot.get('price', 0))
        change = ((p_now - p_before) / max(p_before, 1)) * 100

        if brain_dir == 'ALZA' and change > 0.3:
            result = 'SUCCESS'
        elif brain_dir == 'BAJA' and change < -0.3:
            result = 'SUCCESS'
        elif brain_dir in ('ALZA', 'BAJA'):
            result = 'FAILED'
        else:
            result = 'PARTIAL'

        # Find similar past events for richer context
        similar = []
        if episodic_memory is not None:
            similar = episodic_memory.search_from_snapshot(
                alert_snapshot, k=3, min_sim=0.12
            )

        return await self.evaluate_event(
            alert_snapshot, current_snapshot,
            trade_result=result,
            brain_direction=brain_dir,
            brain_confidence=float(brain_conf),
        )
