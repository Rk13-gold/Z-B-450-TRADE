#!/usr/bin/env python3
"""
gemini_brain.py — Institutional-Grade Hybrid Decision Engine for BB-450.

Architecture
────────────
  GeminiBrainManager   :  Async client wrapper around Gemini 2.0 Flash
  GeminiTradingDecision:  Pydantic schema → forces structured JSON output
  BracketRisk          :  Sub-schema for stop-loss / take-profit levels

Dialogue Protocol (Inter-Brain Communication)
──────────────────────────────────────────────
  Before each inference, GeminiBrainManager receives the raw PyTorch
  metrics + top-3 similar episodic memory records. These are injected
  into the _ENGINEER_PROMPT so Gemini acts as the Strategic Validator:
  it compares current space/time against past failures to dynamically
  re-calibrate probabilities and risk brackets.

  After high-conviction events, Gemini also writes a narrative journal
  entry to CONCMT/*_lesson.md.

Usage
─────
    manager = GeminiBrainManager()
    decision = await manager.execute_inference(snapshot_dict)
    if decision:
        print(decision.decision, decision.confidence)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 1. STRICT PYDANTIC SCHEMA — forces Gemini to return structured JSON
# ══════════════════════════════════════════════════════════════════════════════


class BracketRisk(BaseModel):
    """Stop-loss / take-profit bracket for a single trade decision."""

    entry: float = Field(..., description="Entry price for the position")
    stop_loss: float = Field(
        ..., alias="stop_loss",
        description="Stop-loss price (below entry for LONG, above for SHORT)",
    )
    take_profit_1: float = Field(
        ..., alias="take_profit_1",
        description="First take-profit target (1:1 risk/reward minimum)",
    )
    take_profit_2: float = Field(
        ..., alias="take_profit_2",
        description="Second take-profit target (3:1 risk/reward target)",
    )


class GeminiTradingDecision(BaseModel):
    """Complete decision output from Gemini — maps directly to UI panels."""

    decision: str = Field(
        ...,
        strict=True,
        description='Must be exactly "ALZA", "BAJA", or "INCIERTO"',
    )
    confidence: float = Field(
        ..., ge=0.0, le=100.0,
        description="Confidence level 0–100 %",
    )
    exhaustion_detected: str = Field(
        ...,
        description='Microstructural exhaustion: "▲ BRAIN BULLISH", '
        '"▼ BRAIN BEARISH", or "NONE"',
    )
    reasoning: str = Field(
        ...,
        max_length=1000,
        description="Analytical rationale with order-flow basis",
    )
    score_order_flow: float = Field(
        ..., ge=0.0, le=10.0,
        description="Order-flow score 0–10",
    )
    score_momentum: float = Field(
        ..., ge=0.0, le=10.0,
        description="Momentum score 0–10",
    )
    score_trend: float = Field(
        ..., ge=0.0, le=10.0,
        description="Multi-timeframe trend score 0–10",
    )
    bracket: BracketRisk = Field(
        ...,
        description="Risk bracket with entry, stop, and profit targets",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. GEMINI BRAIN MANAGER
# ══════════════════════════════════════════════════════════════════════════════

_ENGINEER_PROMPT = """\
Eres un Operador de Flujo de Órdenes Institucional y Gestor de Riesgo Frío.

REGLAS ESTRICTAS:
- Tu única función es analizar microestructura de mercado en BTCUSDT 1m.
- Eres ultra defensivo: ante la menor duda, desequilibrio contradictorio, o
  falta de convicción microestructural, debes emitir "INCIERTO" con confianza
  baja (< 55 %).
- NUNCA adivines dirección. Si el order flow no muestra agresión clara y
  sostenida, reporta INCIERTO.
- El bracket de riesgo debe ser realista respecto al ATR actual. SL no puede
  estar a menos de 0.5× ATR del entry.

MOMENTUM DE RUPTURA (BREAKOUT MOMENTUM — PRIORIDAD MÁXIMA):
Si detectas un incremento súbito de volumen acompañado de aceleración de ticks
a favor del CVD, no lo clasifiques como incertidumbre. Clasifícalo como un
impulso institucional de alta probabilidad. En este escenario, genera el bracket
dinámico para montarse en la tendencia de inmediato (SL más ajustado, TP más
amplio). Ignora temporalmente divergencias MTF menores si el microestructura
confirma agresión institucional.

ANÁLISIS REQUERIDO (en orden):
1. Order Flow: delta, CVD, B/A ratio, cumulative delta, cancel_rate.
   ¿Hay divergencia entre precio y flujo de órdenes? ¿El tick_speed es
   anómalamente alto (> 3× promedio 5min)? Eso es institucional.
2. Momentum: kaufman_eff, tick_speed, spread_velocity, delta_accel.
   ¿El movimiento tiene tracción o es absorción?
3. Estructura MTF: tendencias en 5m/15m/1h/4h. ¿Hay confluencia?
4. Liquidez: muros bid/ask, depth_imb_pct. ¿Hay trampas / spoofing?
5. Agotamiento: ¿Precio en extremo de bandas? ¿CVD frenándose? ¿Rango?

NIVELES TÉCNICOS (SOPORTES / RESISTENCIAS / FIBONACCI):
Cuando recibas el campo "technical_levels", úsalo para afinar tu predicción:
- Si el precio está cerca de un nivel Fibonacci clave (0.382, 0.5, 0.618)
  combinado con S/R histórico, considera posible REVERSIÓN desde esa zona.
- Si el precio rompe un nivel Fibonacci + S/R con confluencia, confirma la
  tendencia (breakout con objetivo en el siguiente nivel).
- Las zonas de confluencia (score ≥ 0.7) son áreas de alta probabilidad
  donde múltiples tipos de nivel se solapan — presta atención especial.
- La estructura de mercado (UPTREND/DOWNTREND/RANGING) debe ser coherente
  con tu decisión: no des ALZA si la estructura es bajista cerca de una
  resistencia clave.
- Usa el soporte o resistencia más cercana para validar si el bracket de
  riesgo es razonable (SL no debe estar dentro de una zona de confluencia).

CONTEXTO DE MEMORIA EPISÓDICA (Análisis Inter-Cerebral):
Cuando recibas el campo "episodic_context", úsalo como VALIDADOR ESTRATÉGICO:
- Las lecciones del pasado con etiqueta "FAILED" deben sesgar tu decisión en
  contra de repetir ese error. Si el patrón actual es similar a fallos pasados,
  incrementa tu umbral de confianza requerido.
- Las lecciones con etiqueta "SUCCESS" pueden servir como confirmación, pero
  no confíes ciegamente — cada tick es único.
- Ajusta dinámicamente el bracket de riesgo según el contexto histórico:
   más tight si hay fallos similares, más amplio si hay aciertos similares.

IMPORTANTE — LÍMITE DE RAZONAMIENTO:
- El campo "reasoning" debe tener MÁXIMO 800 caracteres.
- Prioriza datos concretos sobre opiniones: menciona valores específicos
  (delta, CVD, RSI, niveles de precios) en vez de descripciones genéricas.\
"""


class GeminiBrainManager:
    """Async wrapper around Gemini 2.0 Flash for trading decisions.

    Uses the official ``google.genai`` SDK with ``pydantic`` schema
    enforcement to guarantee structured JSON output.

    Inter-Brain Dialogue Protocol
    ─────────────────────────────
    Before each inference, receives PyTorch metrics + episodic memory
    context. Injects these into the prompt so Gemini acts as the
    Strategic Validator, comparing current space/time against past
    failures to dynamically re-calibrate probabilities and brackets.
    """

    MODEL: str = "gemini-2.0-flash"

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("[GeminiBrain] GEMINI_API_KEY no configurada — "
                        "manager deshabilitado")
        self._enabled = bool(api_key)
        self._client = None

        if self._enabled:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                log.info("[GeminiBrain] Cliente Gemini inicializado")
            except Exception as exc:
                log.error(f"[GeminiBrain] Error inicializando cliente: {exc}")
                self._enabled = False

        # ── System instructions (set once, cached) ─────────────────────
        self._system_instruction: Optional[str] = _ENGINEER_PROMPT

        # ── Narrative journal (lazy) ───────────────────────────────────
        self._journal = None

    # ── Public API ─────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def _get_journal(self):
        if self._journal is None and self._client is not None:
            from src.engine.narrative_journal import NarrativeJournal
            self._journal = NarrativeJournal(self._client)
        return self._journal

    async def execute_inference(
        self, snapshot_data: Dict[str, Any],
        episodic_context: Optional[List[dict]] = None,
        pytorch_metrics: Optional[dict] = None,
    ) -> Optional[GeminiTradingDecision]:
        """Run one inference cycle with inter-brain dialogue.

        Parameters
        ----------
        snapshot_data : dict
            Full dashboard snapshot with price, order-flow, indicator keys.
        episodic_context : list[dict] | None
            Top-3 similar episodic memory records from the quantum brain.
        pytorch_metrics : dict | None
            Raw PyTorch metrics (probabilities, hidden state norms, etc.)

        Returns
        -------
        GeminiTradingDecision | None
            ``None`` on any error so the UI never freezes.
        """
        if not self._enabled or self._client is None:
            log.debug("[GeminiBrain] Inferencia omitida — cliente deshabilitado")
            return None

        # ── Build payload with inter-brain context ────────────────────
        compact = self._compact_snapshot(snapshot_data)

        # Inject episodic context
        if episodic_context:
            context_lines = []
            for item in episodic_context:
                rec = item['record']
                sim = item['similarity']
                context_lines.append(
                    f"- [{rec.label}] {rec.direction} @ {rec.confidence:.0f}% "
                    f"(sim={sim:.2f}) | price=${rec.price:.0f} "
                    f"delta={rec.delta:+.0f} trap={rec.trap_status}"
                )
            compact['episodic_context'] = "\n".join(context_lines)

        # Inject technical levels (S/R, Fibonacci, confluence)
        tech_levels_raw = snapshot_data.get("technical_levels")
        if tech_levels_raw and tech_levels_raw.get("fib_retracement"):
            from src.engine.technical_levels import format_levels_for_prompt
            price = snapshot_data.get("price", 0)
            compact["technical_levels"] = format_levels_for_prompt(
                tech_levels_raw, price)

        # Inject PyTorch metrics
        if pytorch_metrics:
            compact['pytorch'] = {
                'p_alza': pytorch_metrics.get('prob_alza', 0),
                'p_baja': pytorch_metrics.get('prob_baja', 0),
                'p_incierto': pytorch_metrics.get('prob_incierto', 0),
                'conf': pytorch_metrics.get('confidence_pct', 0),
                'dir': pytorch_metrics.get('direction', 'INCIERTO'),
            }

        payload = json.dumps(compact, default=str)

        try:
            from google.genai import types

            response = await self._client.aio.models.generate_content(
                model=self.MODEL,
                contents=payload,
                config=types.GenerateContentConfig(
                    system_instruction=self._system_instruction,
                    temperature=0.1,
                    max_output_tokens=512,
                    response_mime_type="application/json",
                    response_schema=GeminiTradingDecision,
                ),
            )

            raw = response.text
            if not raw or not raw.strip():
                log.warning("[GeminiBrain] Respuesta vacía de Gemini")
                return None

            # ── Parse with pydantic validation ─────────────────────────
            decision = GeminiTradingDecision.model_validate_json(raw)
            return decision

        except Exception as exc:
            log.error(f"[GeminiBrain] Error en inferencia: {exc}")
            return None

    # ── Snapshot compaction (cost optimisation) ────────────────────────

    @staticmethod
    def _compact_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Map verbose UI key names to short symbols → lower token count.

        Example
        -------
        ``raw["price"]`` → ``compact["p"]``
        ``raw["cumulative_delta"]`` → ``compact["cvd"]``
        """
        return {
            "p": raw.get("price", 0),
            "chg": raw.get("change_pct", 0),
            "vwap": raw.get("vwap", 0),
            "rsi": raw.get("rsi", 50),
            "bb_pos": raw.get("bb_position", 50),
            "bb_sq": raw.get("bb_squeeze", "NORMAL"),
            "atr": raw.get("atr", 0),
            "ema20": raw.get("ema_20", 0),
            "ema50": raw.get("ema_50", 0),
            # Order flow
            "d": raw.get("delta", 0),
            "d_acc": raw.get("delta_accel", 0),
            "cvd": raw.get("cvd", 0),
            "bv": raw.get("buy_volume", 0),
            "sv": raw.get("sell_volume", 0),
            "vol": raw.get("volume", 0),
            "avg_vol": raw.get("avg_volume", 0),
            "ba": raw.get("ba_ratio", 1.0),
            "imb": raw.get("imbalance", 0),
            "d_imb": raw.get("depth_imb_pct", 0),
            "cum_d": raw.get("cumulative_delta", 0),
            # Microstructure
            "ke": raw.get("kaufman_eff", 0.5),
            "ts": raw.get("tick_speed", 0),
            "sv_sp": raw.get("spread_velocity", 0),
            "cr": raw.get("cancel_rate", 0),
            "skew": raw.get("skewness", 0),
            "pinam": raw.get("pinam", 0),
            # MTF
            "t5": raw.get("trend_5m", "WAIT"),
            "t15": raw.get("trend_15m", "WAIT"),
            "t1h": raw.get("trend_1h", "WAIT"),
            "t4h": raw.get("trend_4h", "WAIT"),
            "rsi5": raw.get("rsi_5m", 0),
            "rsi15": raw.get("rsi_15m", 0),
            "conf": raw.get("confluence_score", 0),
            # Walls & traps
            "w_bid": raw.get("wall_bid", 0),
            "w_bid_sz": raw.get("wall_bid_size", 0),
            "w_ask": raw.get("wall_ask", 0),
            "w_ask_sz": raw.get("wall_ask_size", 0),
            "trap": raw.get("trap_status", "SIN TRAMPA"),
            "prob": raw.get("directional_probability", 50),
            "bias": raw.get("market_bias", "INCIERTO"),
        }

    # ── Narrative journaling ────────────────────────────────────────────

    async def journal_event(self, snapshot_before: Dict[str, Any],
                             snapshot_now: Dict[str, Any],
                             brain_direction: Optional[str] = None,
                             brain_confidence: float = 0.0,
                             trade_result: Optional[str] = None) -> Optional[str]:
        """Trigger narrative journal entry for a significant event.

        Delegates to NarrativeJournal.evaluate_event() which asks Gemini to
        write a Markdown post-mortem → CONCMT/*_lesson.md.

        Parameters
        ----------
        snapshot_before : dict
            Market state when the alert was triggered.
        snapshot_now : dict
            Current state (N minutes later).
        brain_direction : str | None
        brain_confidence : float
        trade_result : str | None
            'SUCCESS', 'FAILED', 'PARTIAL'.

        Returns
        -------
        str | None
            Path to the .md file, or None.
        """
        journal = self._get_journal()
        if journal is None or not journal.is_enabled:
            return None
        return await journal.evaluate_event(
            snapshot_before, snapshot_now,
            trade_result=trade_result,
            brain_direction=brain_direction,
            brain_confidence=brain_confidence,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 3. CONVENIENCE FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def create_gemini_brain() -> GeminiBrainManager:
    """Factory: return ready-to-use ``GeminiBrainManager``."""
    return GeminiBrainManager()
