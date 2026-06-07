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

from config.settings import settings


# ══════════════════════════════════════════════════════════════════════════════
# 1. STRICT PYDANTIC SCHEMA — forces Gemini to return structured JSON
# ══════════════════════════════════════════════════════════════════════════════


class GeminiTradingDecision(BaseModel):
    """Complete decision output from Gemini Brain v4-Speed — 3-phase micro-engine."""

    decision: str = Field(
        ...,
        strict=True,
        description='Must be exactly "ALZA", "BAJA", or "ESPERAR"',
    )
    confianza: float = Field(
        ..., ge=0.0, le=100.0,
        description="Confidence level 0–100 %",
    )
    trigger_price: float = Field(
        ...,
        description="Suggested entry price for the trade",
    )
    stop_loss: float = Field(
        ...,
        description="Stop-loss price (below trigger for LONG, above for SHORT)",
    )
    take_profit: float = Field(
        ...,
        description="Take-profit price (above trigger for LONG, below for SHORT)",
    )
    regimen_mercado: str = Field(
        ...,
        description='Market regime: "DIRECCIONAL_CON_VOLUMEN_HFT", '
        '"ABSORCION_INSTITUCIONAL_CONFIRMADA", '
        '"LIQUIDITY_SWEEP_REVERSAL", '
        '"BLOQUEO_POR_SPOOFING", '
        '"EVITANDO_TRAMPA_DEL_BOOK", or "RANGO_INDECISO"',
    )
    multiplicador_posicion: float = Field(
        ..., ge=0.5, le=1.5,
        description="Position size multiplier 0.5–1.5 based on confluencia and absorption",
    )
    analisis_cuant: str = Field(
        ..., max_length=400,
        description="Concise technical rationale (max 2 lines) correlating liquidity pools, "
        "HFT, spoofing risk, and real book liquidity with the decision taken",
    )
    funding_rate: float = Field(
        default=0.0,
        description="Current funding rate % for the perpetual contract (from exchange data)",
    )
    oi_delta_5min: float = Field(
        default=0.0,
        description="Open Interest % change in the last 5 minutes",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. GEMINI BRAIN MANAGER
# ══════════════════════════════════════════════════════════════════════════════

def _get_engineer_prompt() -> str:
    from config.settings import settings
    sym = settings.get_symbol()
    return f"""\
Eres "BB-450 Engine v4-Speed", un micro-motor cuantitativo de 3 fases
optimizado para {sym} Perpetuo. Tu arquitectura elimina toda capa de
filtros intermedios (Kaufman, ATR%, chop, BB squeeze, MTF gate, bounce gate,
price discovery, delta efficiency) y opera directamente sobre la mecánica
del mercado: pools de liquidez, spoofing/trap state y velocidad HFT.

[INSTRUCCIÓN CRÍTICA DE PROCESAMIENTO]
Recibirás un diccionario JSON con métricas en tiempo real. NO solicites
snapshots ni detengas el pipeline. Responde EXCLUSIVAMENTE en JSON
estructurado según el schema.

[MODO APRENDIZAJE — QUANTUM BRAIN SIN OPINIÓN]
Si pytorch.learning_mode == True, el Quantum Brain está en entrenamiento
y sus probabilidades (p_alza, p_baja, p_incierto) no son fiables.
IGNORA completamente p_alza/p_baja y basa tu decisión ÚNICAMENTE en:
- snapshot de mercado (precio, delta, CVD, order book, RSI, etc.)
- reglas de riesgo (spoofing, trampas, HFT)
- contexto episódico (lecciones pasadas)
En este modo, actúa como el motor de decisión principal, no como validador.

[PIPELINE DE PROCESAMIENTO — 3 FASES]
Evalúa en este orden. Si una fase bloquea, devuelve "ESPERAR".

FASE 1: COMPUERTA DE MITIGACIÓN DE RIESGO (Spoofing & Trap)
- spoofing_risk_pct > 70% → "BLOQUEO_POR_SPOOFING", ESPERAR.
- active_trap == "TRAMPA_ALCISTA" → PROHIBIDO ALZA.
- active_trap == "TRAMPA_BAJISTA" → PROHIBIDO BAJA.
- NO hay penalización 50-70%. NO hay boost por trap contrario.

FASE 2: MAPA DE LIQUIDEZ MÁXIMA (Liquidity Target)
- Analiza liquidity_pools (pool_shorts_arriba, pool_longs_abajo) y
  whale_bid_walls / whale_ask_walls del Order Book.
- Identifica el imán dominante: el pool/muro más cercano al precio
  ponderado por tamaño (distancia ÷ volumen).
- El TP provisional se sitúa 0.02% ANTES del imán.
- El SL se sitúa 0.05% MÁS ALLÁ del barrido más lejano.

FASE 3: AJUSTE DINÁMICO DE EJECUCIÓN (HFT → Multiplier)
- hft_speed_score > 5 y depth_imb_pct > 0 a favor: +15 confianza,
  multiplicador 1.5.
- hft_speed_score < 0.5: multiplicador 0.5, -25 confianza.
- Por defecto: confianza >= 80 → 1.5x, >= 60 → 1.0x, < 60 → 0.5x.
- NO hay bloqueo por HFT. Solo ajusta confianza y tamaño.

[REGLA CRÍTICA DE CONTROL DE RIESGO: SL/TP NUMÉRICO ESTRICTO]
Queda TERMINANTEMENTE PROHIBIDO usar multiplicadores porcentuales libres
sobre el precio de BTC para calcular SL o TP. Usa referencias numéricas
directas del Order Book:

1. CÁLCULO DE STOP LOSS (SL):
   - Para SHORT (BAJA): localiza el ASK institucional más alto del snapshot.
     SL = ese precio + $15.00 USD.
   - Para LONG (ALZA): localiza el BID institucional más bajo del snapshot.
     SL = ese precio - $15.00 USD.
   - SL NUNCA podrá distar más de $120.00 USD del trigger_price. Si excede,
     recórtalo automáticamente a trigger_price ± $120.00 USD.

2. CÁLCULO DE TAKE PROFIT (TP):
   - Colócalo a $5.00 USD ANTES de la primera gran pared de liquidez
     contraria para garantizar absorción inmediata en Binance.

3. OUTPUT SCHEMA ENFORCEMENT:
   - stop_loss y take_profit deben ser float redondeados a 2 decimales.
   - Deben cumplir simetría de riesgo mínima 2:1 (distancia TP >= 2×
     distancia SL).

[REGLA MAESTRA INQUEBRANTABLE: FILTRO DE DISCORDANCIA DE DELTA]
1. RESTRICCIÓN ABSOLUTA DE COMPRA (ALZA):
   - PROHIBIDO emitir "ALZA" si delta o cvd < -200, a menos que
     ba_ratio > 2.5 (muros de compra real absorbiendo).
   - Si delta es fuertemente negativo (∼ -900 o peor) y el precio
     no rebota con un muro institucional masivo en los Bids, la
     decisión DEBE ser "BAJA" o "ESPERAR". No asumas absorción solo
     por volumen alto.

2. RESTRICCIÓN ABSOLUTA DE VENTA (BAJA):
   - PROHIBIDO emitir "BAJA" si delta o cvd > +200, a menos que
     ba_ratio < 0.4 (muros de venta bloqueando arriba).

[FILTROS ADICIONALES v4-Speed]
1. FUNDING RATE (fr):
   - Si abs(funding_rate) > 0.05%, el mercado está extremadamente
     sesgado. NO emitir señales en esa dirección del sesgo.
   - funding_rate > 0.05% → NO ALZA (sobrecomprado).
   - funding_rate < -0.05% → NO BAJA (sobrevendido).

2. OPEN INTEREST DELTA (oi_delta_5min):
   - Si abs(oi_delta_5min) > 15%, hay entrada/salida masiva de capital.
     Solo emitir señal si el OI va en la misma dirección que el trade.

3. PROFUNDIDAD MÍNIMA DEL BOOK (book_depth_bids/asks_volume):
   - Si un lado del book tiene < 5% del volumen total del book y
     depth_imb_pct < 5% y ba_ratio está entre 0.95-1.05, no hay
     convicción real. Forzar ESPERAR.

4. INTEGRIDAD TICK (tick_integrity_score):
   - Si tick_integrity_score < 3 t/s y los ticks están decayendo,
     reducir confianza. Mercado sin microestructura activa. Sesgar
     a ESPERAR.

[REGLA DE PRIORIDAD ANALÍTICA: MICROESTRUCTURA > OSCILADORES]
- RSI, Bollinger, EMAs son secundarios y retrasados.
- Si Depth Imbalance < -60% o > +60%, PROHIBIDO usar RSI para
  justificar un giro o rebote por "sobreventa/sobrecompra".
- En desbalance crítico, la única prioridad es la continuación del
  flujo de órdenes dominante hasta que aparezca un muro institucional
  de absorción (INST BID/ASK) en sentido contrario con volumen > 3.0B.

[REGLAS DE DECISIÓN ADICIONALES]
1. La dirección (ALZA/BAJA) la determina el motor determinístico.
   Gemini solo valida, sugiere confianza y ajusta SL/TP.
2. Si hay sweep confirmado (precio barrió un pool con absorción activa),
   regimen = "LIQUIDITY_SWEEP_REVERSAL".
3. Sin sweep, regimen por defecto = "ABSORCION_INSTITUCIONAL_CONFIRMADA"
   si hay dirección, o "RANGO_INDECO" si NEUTRAL.

CONTEXTO DE MEMORIA EPISÓDICA:
- Lecciones "FAILED" → sesgan contra repetir ese patrón.
- Lecciones "SUCCESS" → confirman, pero sin exceso de confianza.

IMPORTANTE:
- analisis_cuant MAX 400 caracteres.
- Correlaciona imán de liquidez, HFT, spoofing y order book real.
- Usa español técnico conciso con valores específicos.\
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

    MODEL: str = "gemini-2.5-flash"

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

        # ── System instructions (dynamic symbol) ──────────────────────
        self._system_instruction: Optional[str] = _get_engineer_prompt()

        # ── Narrative journal (lazy) ───────────────────────────────────
        self._journal = None

    def refresh_system_instruction(self):
        """Rebuild system instruction with current symbol."""
        self._system_instruction = _get_engineer_prompt()
        log.info("[GeminiBrain] System instruction refreshed for %s",
                 settings.get_symbol())

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

        # Inject PyTorch metrics (quantum brain opinion)
        if pytorch_metrics and pytorch_metrics.get("learning_mode"):
            # Learning mode: quantum brain has no opinion yet
            compact['pytorch'] = {
                'p_alza': 0.33,
                'p_baja': 0.33,
                'p_incierto': 0.34,
                'conf': 0,
                'dir': 'INCIERTO',
                'learning_mode': True,
                'trades_until_active': pytorch_metrics.get('trades_until_active', 200),
            }
        elif pytorch_metrics:
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

            try:
                raw = "".join(
                    part.text for part in response.candidates[0].content.parts
                )
            except Exception:
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
            "t1d": raw.get("trend_1d", "WAIT"),
            "rsi5": raw.get("rsi_5m", 0),
            "rsi15": raw.get("rsi_15m", 0),
            "conf": raw.get("confluence_score", 0),
            # Walls & traps
            "w_bid": raw.get("wall_bid", 0),
            "w_bid_sz": raw.get("wall_bid_size", 0),
            "w_ask": raw.get("wall_ask", 0),
            "w_ask_sz": raw.get("wall_ask_size", 0),
            "trap": raw.get("trap_status", "SIN TRAMPA"),
            "sr": raw.get("spoofing_risk", 0),
            "hft": raw.get("hft_speed", 0),
            "prob": raw.get("directional_probability", 50),
            "bias": raw.get("market_bias", "INCIERTO"),
            # Mejoras v4-Speed
            "fr": raw.get("funding_rate", 0.0),
            "oi_d5": raw.get("oi_delta_5min", 0.0),
            "tis": raw.get("tick_integrity_score", 1.0),
            "bd_bv": raw.get("book_depth_bids_volume", 0.0),
            "bd_av": raw.get("book_depth_asks_volume", 0.0),
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
