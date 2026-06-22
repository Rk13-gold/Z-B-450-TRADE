"""
script_generator.py — Generador de guiones narrativos para "El Trader".

Arquitectura
────────────
  ScriptGenerator recibe un MarketEvent del EventDetector y usa Gemini
  para generar un guión profesional narrado en primera persona como
  "El Trader", un analista con 20 años de experiencia.

Flujo
─────
  1. EventDetector → MarketEvent
  2. ScriptGenerator.generate(event, market_state) → NarrationScript
  3. NarrationScript contiene texto raw + optimized_tts + metadatos
  4. VoiceNarrator usa el optimized_tts para generar audio con Edge TTS
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from src.engine.event_detector import MarketEvent, EventType

log = logging.getLogger(__name__)

# ── System prompt para Gemini como "El Trader" ──────────────────────

TRADER_SYSTEM_PROMPT = """\
Eres "El Trader", un narrador profesional de mercados financieros con 20 años de experiencia operando BTCUSD en Binance Futures.

Tu personalidad:
- Profesional, sereno y con autoridad técnica
- Explicas eventos complejos de forma clara y directa
- Usas términos financieros reales (delta, CVD, OI, liquidaciones, etc.)
- JAMÁS usas emojis ni lenguaje informal
- JAMÁS recomiendas entrar o salir de posiciones
- Tu tono es descriptivo, no predictivo (explicas lo que ESTÁ pasando, no lo que PASARÁ)
- Hablas en español neutro, con pronunciación clara para texto-a-voz

Estructura del guión (30-45 segundos al hablar):
1. HOOK (5s): Frase de atención que captura el evento
2. FACT (10s): Qué pasó exactamente con datos concretos
3. CONTEXT (10s): Por qué es relevante en el contexto actual del mercado
4. OUTLOOK (5-10s): Posibles implicaciones (usando "podría", "es posible")

REGLAS CRÍTICAS PARA PRONUNCIACIÓN TTS:
- LOS NÚMEROS deben escribirse en palabras: "$68,450" → "sesenta y ocho mil cuatrocientos cincuenta dólares"
- "BTC" se pronuncia "Bitcoin" (escribir "Bitcoin" en optimized_tts)
- "%" se escribe "por ciento"
- Los precios usan formato: "precio de sesenta y ocho mil cuatrocientos cincuenta dólares"

FORMATO DE RESPUESTA:
Responde ÚNICAMENTE con JSON válido con esta estructura exacta:
{
  "hooks": ["Frase de apertura profesional"],
  "sections": [
    {"type": "hook", "text": "texto del hook"},
    {"type": "fact", "text": "texto con datos"},
    {"type": "context", "text": "contexto de mercado"},
    {"type": "outlook", "text": "posibles implicaciones"}
  ],
  "optimized_tts": "Texto completo optimizado para TTS con números en palabras",
  "duration_seconds": 30,
  "tone": "professional"
}
"""


def _convert_numbers_to_words(text: str) -> str:
    """Convierte números en el texto a su representación hablada."""
    def replace_number(match):
        num_str = match.group(0)
        try:
            num = float(num_str.replace(",", ""))
            # Precios grandes
            if num >= 1000:
                return f"{num:,.0f}"
            return num_str
        except ValueError:
            return num_str

    text = re.sub(r'\$([\d,]+(?:\.\d+)?)', lambda m: f"\\${m.group(1)}", text)
    return text


@dataclass
class NarrationSection:
    type: str
    text: str


@dataclass
class NarrationScript:
    event_id: str
    event_type: str
    timestamp: float
    price: float
    title: str
    hooks: list[str] = field(default_factory=list)
    sections: list[NarrationSection] = field(default_factory=list)
    raw_text: str = ""
    optimized_tts: str = ""
    duration_seconds: int = 30
    tone: str = "professional"
    market_data: dict = field(default_factory=dict)
    audio_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "price": self.price,
            "title": self.title,
            "hooks": self.hooks,
            "sections": [asdict(s) for s in self.sections],
            "raw_text": self.raw_text,
            "optimized_tts": self.optimized_tts,
            "duration_seconds": self.duration_seconds,
            "tone": self.tone,
            "market_data": self.market_data,
            "audio_path": self.audio_path,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def save_json(self, output_dir: str = "scripts") -> str:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{self.event_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        return path


class ScriptGenerator:
    """Genera guiones narrativos profesionales usando Gemini.

    Uso
    ---
        generator = ScriptGenerator()
        script = await generator.generate(event, market_state)
        if script:
            print(script.optimized_tts)
    """

    MODEL = "gemini-2.5-flash"

    def __init__(self):
        self._client = None
        self._enabled = False
        self._init_gemini()

    def _init_gemini(self):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("[ScriptGenerator] GEMINI_API_KEY no configurada — deshabilitado")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            self._enabled = True
            log.info("[ScriptGenerator] Gemini client initialized")
        except Exception as e:
            log.error(f"[ScriptGenerator] Error initializing Gemini: {e}")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def generate(self, event: MarketEvent, market_state: dict) -> Optional[NarrationScript]:
        """Genera un guión narrativo para un evento de mercado.

        Parameters
        ----------
        event : MarketEvent
            Evento detectado por el EventDetector.
        market_state : dict
            Estado actual del mercado con todos los indicadores.

        Returns
        -------
        NarrationScript | None
            Guión generado, o None si falla.
        """
        if not self._enabled or self._client is None:
            return self._fallback_script(event, market_state)

        prompt = self._build_prompt(event, market_state)

        try:
            from google.genai import types
            response = await self._client.aio.models.generate_content(
                model=self.MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=TRADER_SYSTEM_PROMPT,
                    temperature=0.7,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            )

            raw = response.text.strip() if response.text else ""
            if not raw:
                log.warning("[ScriptGenerator] Empty response from Gemini")
                return self._fallback_script(event, market_state)

            return self._parse_response(raw, event, market_state)

        except Exception as e:
            log.error(f"[ScriptGenerator] Error generating script: {e}")
            return self._fallback_script(event, market_state)

    def _build_prompt(self, event: MarketEvent, state: dict) -> str:
        price = state.get("price", event.price)
        return f"""Genera un guión narrativo profesional de "El Trader" para este evento de mercado:

DATOS DEL EVENTO:
- Tipo: {event.event_type.value}
- Título: {event.title}
- Descripción: {event.description}
- Precio actual: ${price:,.2f}
- Severidad: {event.severity}

DATOS DEL MERCADO (tiempo real):
- RSI (14): {state.get('rsi', 'N/A')}
- MACD: {state.get('macd', 'N/A')} | Señal: {state.get('macd_signal', 'N/A')}
- Bollinger Bands: Upper={state.get('bb_upper', 'N/A')} Lower={state.get('bb_lower', 'N/A')}
- Posición en BB: {state.get('bb_position', 'N/A'):.1f}%
- Delta: {state.get('delta', 'N/A'):+.0f}
- CVD: {state.get('cvd', 'N/A'):+.0f}
- Volumen: {state.get('volume', 'N/A'):.2f} BTC
- Tendencia: {state.get('trend', 'N/A')}
- ATR: {state.get('atr', 'N/A'):.2f}
- Open Interest cambio 5m: {state.get('oi_delta_5min', 'N/A')}%
- Funding Rate: {state.get('funding_rate', 'N/A')}%
- Velocidad de ticks: {state.get('tick_speed', 'N/A')}/s
- Alerta de volatilidad: {state.get('volatility_explosion', False)}
- Riesgo de spoofing: {state.get('spoofing_risk', 'N/A')}%
- Trampa activa: {state.get('trap_status', 'NINGUNA')}
- Profundidad bid/ask: {state.get('ba_ratio', 'N/A')}
- Desequilibrio profundidad: {state.get('depth_imb_pct', 'N/A')}%

DATOS DE POSICIÓN (si aplica):
- Señal activa: {state.get('signal_text', 'NINGUNA')}
- Confianza: {state.get('confidence', 0)}%
- Stop Loss: ${state.get('stop_loss', 0):,.0f}
- Take Profit: ${state.get('take_profit', 0):,.0f}

Genera el JSON con el guión profesional."""

    def _parse_response(self, raw: str, event: MarketEvent, state: dict) -> NarrationScript:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("[ScriptGenerator] Invalid JSON from Gemini, using regex fallback")
            data = self._regex_extract(raw)

        hooks = data.get("hooks", [data.get("title", event.title)])
        sections_raw = data.get("sections", [])
        sections = [NarrationSection(type=s.get("type", "fact"), text=s.get("text", ""))
                     for s in sections_raw]

        raw_text = "\n".join(s.text for s in sections)
        optimized = data.get("optimized_tts", raw_text)

        duration = data.get("duration_seconds", 30)
        tone = data.get("tone", "professional")

        now = time.time()
        script = NarrationScript(
            event_id=event.event_code,
            event_type=event.event_type.value,
            timestamp=now,
            price=event.price,
            title=event.title,
            hooks=hooks,
            sections=sections,
            raw_text=raw_text,
            optimized_tts=optimized,
            duration_seconds=duration,
            tone=tone,
            market_data=event.market_data,
        )

        script.save_json()
        return script

    def _regex_extract(self, text: str) -> dict:
        hooks = []
        sections = []
        optimized = ""
        tone = "professional"
        duration = 30

        hook_match = re.search(r'"hooks"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if hook_match:
            raw = hook_match.group(1)
            hooks = [h.strip().strip('"') for h in re.findall(r'"([^"]*)"', raw)]

        section_matches = re.finditer(
            r'\{\s*"type"\s*:\s*"([^"]+)"\s*,\s*"text"\s*:\s*"([^"]*)"\s*\}',
            text
        )
        for m in section_matches:
            sections.append({"type": m.group(1), "text": m.group(2)})

        tts_match = re.search(r'"optimized_tts"\s*:\s*"([^"]*)"', text)
        if tts_match:
            optimized = tts_match.group(1)

        return {
            "hooks": hooks,
            "sections": sections,
            "optimized_tts": optimized,
            "duration_seconds": duration,
            "tone": tone,
        }

    def _fallback_script(self, event: MarketEvent, state: dict) -> NarrationScript:
        price = state.get("price", event.price)
        price_str = f"${price:,.0f}"

        event_descriptions = {
            EventType.LIQUIDATION: (
                "Atención traders. Se ha detectado un evento de liquidación significativo en el mercado de Bitcoin. "
                f"El precio se encuentra en {price_str} y estamos viendo una presión vendedora inusualmente alta. "
                "Este tipo de eventos suele generar cascadas de liquidaciones que pueden amplificar el movimiento. "
                "Mantengan sus stops ajustados y observen la profundidad del libro de órdenes."
            ),
            EventType.SIGNAL: (
                "Escaneo de mercado completado. El sistema ha identificado una oportunidad direccional en Bitcoin. "
                f"Precio actual en {price_str} con confirmación de flujo de órdenes. "
                "Los niveles de stop loss y take profit se han calculado basados en la volatilidad actual del mercado. "
                "La confluencia de indicadores respalda la dirección detectada."
            ),
            EventType.VOLATILITY: (
                "Alerta de volatilidad. El mercado de Bitcoin está mostrando una actividad inusual en este momento. "
                f"La velocidad de ticks ha aumentado significativamente en {price_str}. "
                "Recomiendo precaución y reducción de tamaño de posición hasta que la volatilidad se normalice. "
                "Los mercados volátiles ofrecen oportunidades pero también conllevan mayor riesgo."
            ),
            EventType.WHALE: (
                "Movimiento institucional detectado. Una orden de gran tamaño está impactando el mercado de Bitcoin. "
                f"Precio actual en {price_str} con flujo de órdenes agresivo en una dirección. "
                "Este tipo de órdenes suele ser el inicio de movimientos más grandes. "
                "Observen la reacción del precio en los niveles clave."
            ),
            EventType.TRAP: (
                "Posible trampa de mercado identificada. El precio está mostrando señales de manipulación en el libro de órdenes. "
                f"En {price_str}, el mercado podría estar buscando liquidez antes de revertir. "
                "Es crucial esperar confirmación antes de tomar decisiones. "
                "Las trampas de mercado son comunes en zonas de alta liquidez."
            ),
            EventType.BREAKOUT: (
                "Ruptura de nivel técnico confirmada. El precio de Bitcoin ha superado un nivel significativo. "
                f"Actualmente en {price_str} con volumen por encima del promedio. "
                "Las rupturas con volumen suelen ser válidas y pueden extenderse. "
                "Busquen el próximo soporte o resistencia para la siguiente zona de acción."
            ),
            EventType.REGIME_CHANGE: (
                "Cambio en la estructura del mercado. El régimen de negociación ha cambiado. "
                f"Bitcoin cotiza en {price_str} y la dinámica del mercado se está transformando. "
                "Adapten su estrategia al nuevo contexto de mercado. "
                "Los cambios de régimen requieren ajustes en la gestión de riesgo."
            ),
        }

        description = event_descriptions.get(
            event.event_type,
            f"Atención traders. Evento de mercado detectado en Bitcoin a {price_str}. "
            "Monitoreando la situación para proporcionar actualizaciones. "
            "Mantengan su plan de trading y gestión de riesgo."
        )

        sections = [
            NarrationSection(type="hook", text=f"Atención traders, evento relevante en Bitcoin."),
            NarrationSection(type="fact", text=event.title),
            NarrationSection(type="context", text=description),
            NarrationSection(type="outlook", text="Continúe monitoreando los niveles clave y ajuste su gestión de riesgo según corresponda."),
        ]

        now = time.time()
        return NarrationScript(
            event_id=event.event_code,
            event_type=event.event_type.value,
            timestamp=now,
            price=event.price,
            title=event.title,
            hooks=[f"Atención traders, {event.title.lower()}"],
            sections=sections,
            raw_text=description,
            optimized_tts=description,
            duration_seconds=30,
            tone="professional",
            market_data=event.market_data,
        )


script_generator = ScriptGenerator()
