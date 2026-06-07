"""
auto_learner.py — Autonomous Learning Engine for BB-450.

Architecture
────────────
  AutoLearner  :  Periodic analysis cycle that fetches REAL data from the
                  system and asks Gemini to produce teaching observations.

Cycle (when enabled):
  1. Fetch 200 1m candles from Binance (historical context)
  2. Collect: current snapshot + episodic memory + brain stats
  3. Gemini analyses: 200-candle history vs FAILED/SUCCESS patterns
  4. Structured JSON output → elegant log entries
  5. Optional: auto-generate *_lesson.md files
"""

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ── Candlestick field indices (Binance futures_klines) ─────────────────
KLINE_OPEN = 1
KLINE_HIGH = 2
KLINE_LOW = 3
KLINE_CLOSE = 4
KLINE_VOLUME = 5
KLINE_CLOSE_TIME = 6

_KLINE_CACHE: List = []
_KLINE_CACHE_TIME: float = 0.0
_KLINE_CACHE_TTL: float = 15.0  # refresh every 15s

_ANALYSIS_PROMPT = """\
Eres un Tutor de Trading Institucional Senior. Analiza datos REALES del sistema BB-450.

DATOS DEL MERCADO EN VIVO:
Precio: ${price:,.0f} | Delta: {delta:+.1f} | CVD: {cvd:+.1f}
RSI: {rsi:.1f} | Tick Speed: {tick_speed:.1f}/s | Trampa: {trap}
Cerebro predice: {brain_dir} @ {brain_conf:.0f}%
BB Position: {bb_pos:.1f}% | ATR: {atr:.1f} | B/A Ratio: {ba:.3f}x

HISTORIAL DE 200 VELAS (1m) — ÚLTIMAS ~3.3 HORAS:
{klines_summary}

PATRONES HISTÓRICOS REALES (MEMORIA EPISÓDICA):
{failed_success_text}

BLOQUES DE CONOCIMIENTO ACTIVOS: {kb_count} reglas
Último entrenamiento: accuracy {accuracy:.1f}%, loss {loss:.4f}
Inferencias totales: {total_calls}

INSTRUCCIONES:
1. Analiza el HISTORIAL DE 200 VELAS. ¿Qué estructura de mercado ves?
   - Tendencias, rangos, soportes/resistencias clave.
   - Zonas de acumulación / distribución.
   - Patrones de velas (envolventes, martillos, dojis, etc.).
2. Compara el patrón actual con FAILED/SUCCESS de la memoria episódica.
3. ¿Qué divergencias hay entre precio y flujo de órdenes que el cerebro debería aprender?
4. Da una recomendación concreta para que el cerebro mejore sus predicciones.

RESPONDE SOLO JSON (sin markdown, sin texto adicional):
{{
  "market_insight": "análisis de la estructura de 200 velas (máx 150 chars)",
  "pattern_match": "FAILED #N o 'ninguno'",
  "learning_observation": "qué aprendió el cerebro de este análisis (máx 250 chars)",
  "knowledge_gap": "qué brecha de conocimiento se identificó (máx 150 chars)",
  "recommendation": "recomendación concreta para mejorar (máx 200 chars)",
  "generate_lesson": true o false
}}
"""


# ── Klines fetcher (sync, cached) ──────────────────────────────────────

def fetch_klines(symbol: str = None, interval: str = "1m",
                 limit: int = 200) -> List:
    """Fetch klines from Binance futures with 15s cache.

    Returns list of klines (same format as binance futures_klines).
    Falls back to cached data on error.
    """
    global _KLINE_CACHE, _KLINE_CACHE_TIME
    if symbol is None:
        from config.settings import settings
        symbol = settings.get_symbol()

    now = time.time()
    if _KLINE_CACHE and (now - _KLINE_CACHE_TIME) < _KLINE_CACHE_TTL:
        return _KLINE_CACHE

    try:
        from binance.client import Client
        from config.settings import settings
        client = Client(settings.BINANCE_REAL_API_KEY,
                        settings.BINANCE_REAL_SECRET_KEY,
                        testnet=False)
        klines = client.futures_klines(
            symbol=symbol, interval=interval, limit=limit
        )
        if klines:
            _KLINE_CACHE = klines
            _KLINE_CACHE_TIME = now
        log.info("[AutoLearner] %d velas 1m obtenidas de Binance", len(klines))
        return klines
    except Exception as e:
        log.warning("[AutoLearner] Error fetching klines: %s", e)
        return _KLINE_CACHE  # stale cache better than nothing


def compress_klines(klines: List, group_size: int = 10) -> str:
    """Compress N klines into a compact text summary.

    Groups candles (default 10 per group → ~20 lines for 200 candles)
    and computes: O, H, L, C, Δ%, volume, pattern detection.

    Returns a text block for the Gemini prompt.
    """
    if not klines:
        return "(No hay datos históricos disponibles)"

    groups = []
    for i in range(0, len(klines), group_size):
        chunk = klines[i:i + group_size]
        if len(chunk) < 3:
            continue

        opens = [float(c[KLINE_OPEN]) for c in chunk]
        highs = [float(c[KLINE_HIGH]) for c in chunk]
        lows = [float(c[KLINE_LOW]) for c in chunk]
        closes = [float(c[KLINE_CLOSE]) for c in chunk]
        volumes = [float(c[KLINE_VOLUME]) for c in chunk]

        o = opens[0]
        h = max(highs)
        l_val = min(lows)
        c = closes[-1]
        v = sum(volumes)
        change_pct = ((c - o) / o * 100) if o else 0.0
        body_range = abs(c - o)
        total_range = h - l_val
        candle_range_pct = (total_range / o * 100) if o else 0.0

        # Pattern detection
        body_to_range = body_range / total_range if total_range > 0 else 1
        upper_w = h - max(c, o)
        lower_w = min(c, o) - l_val
        pattern = ""

        if candle_range_pct > 1.5 and body_to_range < 0.2:
            pattern += "DOJI/HIGH WICK" if upper_w > lower_w else "DOJI/LONG TAIL"
        elif candle_range_pct > 1.5 and body_to_range > 0.7:
            if c > o and lower_w > total_range * 0.4:
                pattern = "MARTILLO"
            elif c < o and upper_w > total_range * 0.4:
                pattern = "SHOOTING STAR"
            else:
                pattern = "GRAN VELA DIRECCIONAL"
        elif upper_w > total_range * 0.6:
            pattern = "RECHAZO ALCISTA" if c > o else "TECHO"
        elif lower_w > total_range * 0.6:
            pattern = "SUELO" if c > o else "RECHAZO BAJISTA"
        else:
            pattern = "RANGO"

        if change_pct > 0.3:
            pattern += " ▲"
        elif change_pct < -0.3:
            pattern += " ▼"

        ts = datetime.fromtimestamp(chunk[0][KLINE_CLOSE_TIME] / 1000,
                                     tz=timezone.utc)
        time_label = ts.strftime('%H:%M')

        groups.append(
            f"[{time_label}] O={o:,.0f} H={h:,.0f} L={l_val:,.0f} "
            f"C={c:,.0f} Δ={change_pct:+.2f}% V={v:.1f} "
            f"RNG={candle_range_pct:.1f}% {pattern}"
        )

    return "\n".join(groups)


# ── Main class ─────────────────────────────────────────────────────────

class AutoLearner:
    """Autonomous learning cycle — analyses market + brain state via Gemini.

    Usage
    -----
        learner = AutoLearner(gemini_client, episodic_memory)
        learner.start()    # enabled → auto-analysis every interval
        entries = learner.get_log(30)  # latest log entries for UI
    """

    def __init__(self, gemini_client=None, episodic_memory=None,
                 interval: float = 30.0):
        self._client = gemini_client
        self._memory = episodic_memory
        self._interval = interval  # seconds between analyses
        self._enabled = False
        self._last_analysis: float = 0.0
        self._analysis_count: int = 0
        self._observations: deque = deque(maxlen=500)
        self._log: deque = deque(maxlen=200)

    # ── Public API ─────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self):
        self._enabled = True
        self._analysis_count = 0
        log.info("[AutoLearner] Activado — análisis cada %.0fs", self._interval)

    def stop(self):
        self._enabled = False
        log.info("[AutoLearner] Desactivado — %d análisis realizados",
                 self._analysis_count)

    def toggle(self) -> bool:
        if self._enabled:
            self.stop()
        else:
            self.start()
        return self._enabled

    def set_gemini_client(self, client):
        self._client = client

    def set_episodic_memory(self, memory):
        self._memory = memory

    def should_analyze(self) -> bool:
        """Return True if enough seconds have passed since last analysis."""
        if not self._enabled or self._client is None:
            return False
        return (time.time() - self._last_analysis) >= self._interval

    def analyze(self, snapshot: Dict[str, Any],
                  brain_stats: Dict[str, Any],
                  memory_stats: Dict[str, Any],
                  knowledge_stats: Dict[str, Any],
                  training_stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run one analysis cycle with REAL system data (synchronous).

        Fetches 200 1m candles from Binance for historical context.

        Parameters
        ----------
        snapshot : dict — current market snapshot
        brain_stats : dict — from BrainAgent.get_stats()
        memory_stats : dict — from EpisodicMemory.stats()
        knowledge_stats : dict — from KnowledgeIndex.stats()
        training_stats : dict — from BrainAgent.get_training_stats()

        Returns
        -------
        dict with keys: market_insight, pattern_match, learning_observation,
                        knowledge_gap, recommendation, generate_lesson
        or None on error.
        """
        if not self._enabled or self._client is None:
            return None

        self._last_analysis = time.time()
        self._analysis_count += 1

        # ── Fetch 200 1m candles for historical context ────────────────
        raw_klines = fetch_klines()
        klines_summary = compress_klines(raw_klines)

        # ── Collect real data ──────────────────────────────────────────
        price = float(snapshot.get('price', 0))
        delta = float(snapshot.get('delta', 0))
        cvd = float(snapshot.get('cvd', 0))
        rsi = float(snapshot.get('rsi', 50))
        tick_speed = float(snapshot.get('tick_speed', 0))
        trap = str(snapshot.get('trap_status', 'SIN TRAMPA'))
        brain_dir = str(snapshot.get('brain_direction',
                                      snapshot.get('signal_text', 'WAIT')))
        brain_conf = float(snapshot.get('brain_confidence_pct',
                                         snapshot.get('confidence', 0)))
        bb_pos = float(snapshot.get('bb_position', 50))
        atr = float(snapshot.get('atr', 10))
        ba = float(snapshot.get('ba_ratio', 1.0))

        # Build FAILED/SUCCESS text from episodic memory
        failed_text = ""
        if self._memory is not None:
            try:
                failed_records = self._memory.get_failed()[-5:]
                success_records = self._memory.get_success()[-3:]
                lines = []
                for r in failed_records:
                    lines.append(
                        f"FAILED: {r.direction}@{r.confidence:.0f}% "
                        f"price=${r.price:.0f} delta={r.delta:+.0f} "
                        f"trap={r.trap_status}"
                    )
                for r in success_records:
                    lines.append(
                        f"SUCCESS: {r.direction}@{r.confidence:.0f}% "
                        f"price=${r.price:.0f} delta={r.delta:+.0f}"
                    )
                failed_text = "\n".join(lines) if lines else "No hay suficientes datos"
            except Exception:
                failed_text = "Error al leer memoria"

        prompt = _ANALYSIS_PROMPT.format(
            price=price, delta=delta, cvd=cvd, rsi=rsi,
            tick_speed=tick_speed, trap=trap,
            brain_dir=brain_dir, brain_conf=brain_conf,
            bb_pos=bb_pos, atr=atr, ba=ba,
            klines_summary=klines_summary,
            failed_success_text=failed_text,
            kb_count=knowledge_stats.get('blocks', 0),
            accuracy=training_stats.get('avg_accuracy', 0),
            loss=training_stats.get('avg_loss', 0),
            total_calls=brain_stats.get('total_calls', 0),
        )

        # ── Call Gemini ────────────────────────────────────────────────
        try:
            from google.genai import types
            resp = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=500,
                ),
            )
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text if resp else ""
            raw = texto_completo.strip()
            if not raw:
                return None

            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw[:-3]
            raw = raw.strip()

            data = json.loads(raw)
        except Exception as e:
            log.warning("[AutoLearner] Error en análisis: %s", e)
            return None

        # ── Generate lesson file if recommended ─────────────────────────
        lesson_path = None
        if data.get('generate_lesson') and data.get('learning_observation'):
            lesson_path = self._write_lesson(data, snapshot, klines_summary)

        # ── Store observation for the log ───────────────────────────────
        obs = {
            'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S'),
            'type': 'analysis' if not lesson_path else 'lesson',
            'market_insight': data.get('market_insight', ''),
            'pattern_match': data.get('pattern_match', ''),
            'learning_observation': data.get('learning_observation', ''),
            'knowledge_gap': data.get('knowledge_gap', ''),
            'recommendation': data.get('recommendation', ''),
            'lesson_path': lesson_path,
            'price': price,
            'delta': delta,
            'brain_dir': brain_dir,
            'brain_conf': brain_conf,
        }
        self._observations.append(obs)
        self._log.appendleft(self._format_log_entry(obs))

        log.info("[AutoLearner] #%d — %s", self._analysis_count,
                 data.get('market_insight', '')[:80])

        return data

    def get_log(self, count: int = 30) -> List[str]:
        """Return last N formatted log entries."""
        return list(self._log)[:count]

    def stats(self) -> dict:
        return {
            'enabled': self._enabled,
            'analysis_count': self._analysis_count,
            'last_analysis': self._last_analysis,
            'interval': self._interval,
            'log_size': len(self._log),
        }

    # ── Private ────────────────────────────────────────────────────────

    @staticmethod
    def _format_log_entry(obs: dict) -> str:
        """Format an observation as a beautiful HTML log line."""
        ts = obs['timestamp']
        otype = obs['type']

        if otype == 'lesson':
            return (
                f'<span style="color:#555;">[{ts}]</span> '
                f'<span style="color:#00FF88;">📘 LECCIÓN GENERADA</span>'
                f'<br><span style="color:#888;font-size:9px;">'
                f'  {obs.get("learning_observation", "")[:120]}</span>'
            )

        insight = obs.get('market_insight', '')
        pattern = obs.get('pattern_match', '')
        gap = obs.get('knowledge_gap', '')
        rec = obs.get('recommendation', '')
        brain_dir = obs.get('brain_dir', '')
        brain_conf = obs.get('brain_conf', 0)

        dir_color = '#00FF88' if brain_dir == 'ALZA' else '#BB00FF'
        brain_str = f'<span style="color:{dir_color};">{brain_dir} {brain_conf:.0f}%</span>'

        lines = [
            f'<span style="color:#555;">[{ts}]</span> '
            f'<span style="color:#00D4FF;">🔍 ANÁLISIS (200 VELAS)</span>',
        ]
        if insight:
            lines.append(
                f'<span style="color:#888;font-size:9px;">  📊 {insight}</span>')
        if pattern and pattern != 'ninguno':
            lines.append(
                f'<span style="color:#FFD700;font-size:9px;">  🔗 {pattern}</span>')
        lines.append(
            f'<span style="color:#888;font-size:9px;">  🧠 Cerebro: {brain_str}</span>')
        if gap:
            lines.append(
                f'<span style="color:#FF6B6B;font-size:9px;">  ⚠️ Brecha: {gap}</span>')
        if rec:
            lines.append(
                f'<span style="color:#00FF88;font-size:9px;">  💡 {rec}</span>')

        return '<br>'.join(lines)

    def _write_lesson(self, data: dict, snapshot: dict,
                      klines_summary: str = "") -> Optional[str]:
        """Write a *_lesson.md file to CONCMT/."""
        try:
            concmt = os.path.join(os.path.dirname(__file__),
                                  '..', '..', 'CONCMT')
            os.makedirs(concmt, exist_ok=True)

            ts = datetime.now(timezone.utc)
            filename = f"{ts.strftime('%Y%m%d_%H%M')}_lesson.md"
            path = os.path.join(concmt, filename)

            content = (
                f"# Lección: {ts.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"## Contexto del Mercado\n"
                f"- Precio: ${snapshot.get('price', 0):,.0f}\n"
                f"- Delta: {snapshot.get('delta', 0):+.0f} | "
                f"CVD: {snapshot.get('cvd', 0):+.1f}\n"
                f"- RSI: {snapshot.get('rsi', 50):.1f} | "
                f"Tick: {snapshot.get('tick_speed', 0):.1f}/s\n"
                f"- Trampa: {snapshot.get('trap_status', 'N/A')}\n\n"
                f"## Historial de 200 Velas (1m)\n"
                f"```\n{klines_summary[:500]}\n```\n\n"
                f"## Observación de Aprendizaje\n"
                f"{data.get('learning_observation', '')}\n\n"
                f"## Brecha de Conocimiento\n"
                f"{data.get('knowledge_gap', '')}\n\n"
                f"## Recomendación\n"
                f"{data.get('recommendation', '')}\n\n"
                f"## Tags\n"
                f"`#auto_learn` `#{data.get('pattern_match', 'analisis').lower().replace(' ', '_')}` `#200velas`\n"
            )

            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)

            log.info("[AutoLearner] Lección guardada: %s", path)
            return path
        except Exception as e:
            log.warning("[AutoLearner] Error escribiendo lección: %s", e)
            return None
