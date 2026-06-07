"""
BB-450 Telegram Bot
===================
Refactored architecture (May 2026):

  - Own asyncio event loop in a daemon thread
  - Queue-fed from AsyncDataEngine - zero sync HTTP in alert dispatch
  - 500ms snapshot freshness guard
  - Spam filters: price-action divergence, low-confidence temporal block (20-35%)
  - Premium HTML templates with automatic tag closing
  - Gemini 2.0 Flash interactive chat with live market snapshot injection
"""

import asyncio, html, io, json, logging, os, re, threading, time, traceback, unicodedata
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import aiohttp, edge_tts
from google import genai

os.environ["MPLBACKEND"] = "Agg"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

from config.settings import settings
from src.engine.symbol_utils import fetch_perpetual_symbols, validate_symbol, get_top_symbols
from src.engine.binance_client import binance_client

log = logging.getLogger("TelegramBot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GEMINI_MODEL = "gemini-2.5-flash"

ALLOWED_HTML_TAGS = frozenset({'b', 'i', 'code', 'u', 's', 'pre', 'a', 'tg-spoiler', 'span'})
HTML_TAG_RE = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>')
SEP = "\u2500" * 28

# Helper functions

def _btn(text, callback_data):
    return {"text": text, "callback_data": callback_data}

def _row(*btns):
    return list(btns)

def _keyboard(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}

def _reply_kb(*rows, resize=True, persistent=True, one_time=False):
    return {
        "keyboard": [[{"text": c} for c in r] for r in rows],
        "resize_keyboard": resize,
        "is_persistent": persistent,
        "one_time_keyboard": one_time,
        "input_field_placeholder": "Toca un bot\u00f3n...",
    }



def _main_keyboard():
    """Root menu — informational commands only, no direct trade triggers."""
    return _reply_kb(
        ("\U0001f4ca Status", "\U0001f4c8 Signal", "\U0001f9e0 Brain"),
        ("\U0001f4c9 Chart",  "\u2699\ufe0f Config",  "\U0001f4b0 Balance"),
        ("\u26a1 OPERAR BB-450", "\U0001f4cd Positions"),
        ("\U0001f504 Símbolo",),
    )

def _trading_keyboard():
    """Sub-menu shown after the user taps ⚡ OPERAR BB-450.

    Isolates order-execution buttons to prevent accidental triggers.
    """
    return _reply_kb(
        ("\U0001f7e2 ABRIR LONG",  "\U0001f534 ABRIR SHORT"),
        ("\u274c CERRAR POSICI\u00d3N",),
        ("\u2b05\ufe0f Volver al Men\u00fa Principal",),
        one_time=True,
    )


def _sanitize_html(text: str) -> str:
    """Force-close unclosed HTML tags to avoid Telegram parse errors."""
    stack = []
    for m in HTML_TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in ALLOWED_HTML_TAGS: continue
        if m.group(0).startswith("</"):
            if stack and stack[-1] == tag: stack.pop()
        elif not m.group(0).endswith("/>"):
            stack.append(tag)
    for tag in reversed(stack):
        text += f"</{tag}>"
    return text


def _escape_html(text: str) -> str:
    """Escape HTML special characters — single canonical version."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_to_html(text: str) -> str:
    """Convert markdown bold/code/italic to HTML tags."""
    tag_pat = re.compile(r'</?(b|i|u|s|code|pre|a|tg-spoiler|span)\b[^>]*>')
    protected = {}
    def _protect(m):
        pid = f"\x00TAG{len(protected)}\x00"
        protected[pid] = m.group(0)
        return pid
    text = tag_pat.sub(_protect, text)
    text = _escape_html(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<b>\1</b>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'_(.+?)_', r'<i>\1</i>', text)
    for pid, tag in protected.items():
        text = text.replace(pid, tag)
    return text


def format_for_telegram(text: str, context: str = "message") -> str:
    """Unified text pipeline: markdown→HTML→sanitize→truncate.

    Contexts:
      "message" — full pipeline + 4096 truncation (default)
      "caption" — full pipeline, no truncation (Telegram handles it)
      "edit"    — full pipeline, no truncation
      "plain"   — strip all HTML tags, return raw text
    """
    if not text:
        return ""
    if context == "plain":
        return re.sub(r'<[^>]+>', '', text)
    text = _markdown_to_html(text)
    text = _sanitize_html(text)
    if context == "message":
        MAX_LEN = 4096
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN - 80] + (
                "\n\n<i>... ⚠️ Mensaje truncado por límite de Telegram"
                " (4096 caracteres)</i>"
            )
    return text


def _format_premium_message(title: str, body: str) -> str:
    """Build Telegram-safe HTML message with premium styling."""
    return (
        f"\U0001f30c <b>{_escape_html(title)}</b>\n\n"
        f"{body}\n\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"<i>BB-450 Trading System</i> | \u23f0 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


def _clean_ai_text(text: str) -> str:
    """Sanitize Gemini-generated text for safe Telegram HTML parsing."""
    ALLOWED = {"b", "i", "u", "s", "code", "pre"}
    tag_pat = re.compile(r'</?(' + '|'.join(ALLOWED) + r')\b[^>]*>', re.IGNORECASE)
    protected: dict[str, str] = {}
    def _protect(m: re.Match) -> str:
        pid = f"\x00TAG{len(protected)}\x00"
        protected[pid] = m.group(0).lower()
        return pid
    text = tag_pat.sub(_protect, text)
    text = re.sub(r'</?(\w+)[^>]*>', '', text, flags=re.IGNORECASE)
    text = _escape_html(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text, flags=re.DOTALL)
    text = re.sub(r'(?<!\*)\*(?!\*)', '', text)
    for pid, tag in protected.items():
        text = text.replace(pid, tag)
    text = re.sub(r'</?(?!b|i|u|s|code|pre)(\w+)[^>]*>', '', text, flags=re.IGNORECASE)
    text = _sanitize_html(text)
    return text.strip()


def limpiar_texto_telegram(texto: str) -> str:
    """Sanitize AI text for Telegram — broader allowed tag set."""
    if not texto:
        return ""
    # Unescape HTML entities the AI may return (&lt; → <, &amp; → &, etc.)
    texto = html.unescape(texto)
    ALLOWED = {'b', 'i', 'u', 's', 'code', 'pre', 'a', 'tg-spoiler', 'span', 'em'}
    tag_pat = re.compile(r'</?(' + '|'.join(ALLOWED) + r')\b[^>]*>', re.IGNORECASE)
    protected = {}
    def _protect(m):
        pid = f"\x00TAG{len(protected)}\x00"
        protected[pid] = m.group(0)
        return pid
    texto = tag_pat.sub(_protect, texto)
    # Strip any non-allowed HTML tags BEFORE escaping
    texto = re.sub(r'</?(\w+)[^>]*>', '', texto, flags=re.IGNORECASE)
    texto = texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for pid, tag in protected.items():
        texto = texto.replace(pid, tag)
    texto = re.sub(
        r'</?(?!' + '|'.join({t for t in ALLOWED}) + r')(\w+)[^>]*>',
        '', texto, flags=re.IGNORECASE
    )
    texto = _sanitize_html(texto)
    return texto.strip()


class AlertState:
    def __init__(self):
        self.sent_crash = False
        self.sent_pump = False
        self.sent_buy = False
        self.sent_sell = False
        self.sent_volume = False
        self.sent_whale = False
        self.sent_trap = False
        self.sent_radar = False
        self.prev_cum_delta: float = 0.0
    def reset(self):
        for attr in vars(self):
            if attr.startswith("sent_"):
                setattr(self, attr, False)
class TelegramBot:
    """
    Telegram bot that runs in a background thread.

    Queue-fed from AsyncDataEngine, zero sync HTTP in alert dispatch.
    """

    SCALP_CRASH_PCT = 0.35
    SCALP_WHALE_DELTA_ACCEL_THRESHOLD = 100
    SCALP_WHALE_TICK_SPEED_THRESHOLD = 25
    SCALP_TRAP_PROB_THRESHOLD = 60
    TENDENCIA_DELTA_MIN = 50

    def __init__(self, order_executor: Optional['OrderExecutor'] = None):
        self._bot_token: str = settings.TELEGRAM_BOT_TOKEN
        self._chat_id: int = int(settings.TELEGRAM_CHAT_ID) if settings.TELEGRAM_CHAT_ID else 0
        self.enabled: bool = settings.TELEGRAM_ENABLED

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

        self._state: dict = {}
        self._price_history: deque = deque(maxlen=300)

        self._last_signal: str = "WAIT"
        self._last_brain_direction: Optional[str] = None
        self._brain_flip_block_until: float = 0.0

        self._prev_conf: float = 0.0
        self._prev_conf_sig: str = "WAIT"

        self._alerts: deque = deque(maxlen=50)
        self._alert_state = AlertState()
        self._user_config = {
            "crash": True, "buy_sell": True, "volume": True, "rsi": True,
            "trend_change": True, "whale": True, "ai_trade": True, "radar": True,
            "capital": 100.0, "risk_pct": 1.0, "sl_pct": 0.5, "tp_pct": 1.5,
        }
        self._radar_cooldown: float = 0.0
        self._prev_trend: str = "NEUTRAL"
        self._last_trend_alert_ts: float = 0.0
        self._last_strength_alert_ts: float = 0.0
        self._prev_trap_type: Optional[str] = None

        self._last_update_id = 0

        self._gemini_key: str = settings.GEMINI_API_KEY
        self._gemini_enabled: bool = bool(self._gemini_key)
        self._gemini_history: list = []
        self._gemini_fallback_count: int = 0
        if self._gemini_enabled:
            try:
                self._gemini_client = genai.Client(api_key=self._gemini_key)
                log.info("Gemini client initialized")
            except Exception as e:
                log.warning("Failed to initialize Gemini client: %s", e)
                self._gemini_client = None
                self._gemini_enabled = False
        else:
            self._gemini_client = None
            log.warning("Gemini API key not set — Gemini features disabled")

        self._brain_block_log_counter: int = 0
        self._processed_callbacks: set = set()
        self._last_brain_alert_ts: float = 0.0
        self._last_brain_alert_direction: Optional[str] = None
        self.BRAIN_ALERT_COOLDOWN_SEC: int = 120
        self._pending_amount_change: dict = {}

        # Referencia única al OrderExecutor del dashboard (se pasa por constructor)
        self._order_executor: Optional['OrderExecutor'] = order_executor
        # Referencias para /symbol hot-swap (se inyectan después)
        self._data_engine: Optional['AsyncDataEngine'] = None

    def set_order_executor(self, executor):
        """Inject the dashboard's OrderExecutor for callback-based execution."""
        self._order_executor = executor

    def set_data_engine(self, engine):
        """Inject the AsyncDataEngine for hot symbol switching."""
        self._data_engine = engine

    # ─── Lifecycle ───────────────────────────────────────────────────

    def start(self):
        if not self.enabled:
            print("[TelegramBot] \u23f9 DESACTIVADO - TELEGRAM_ENABLED=false en .env")
            return
        if not self._bot_token:
            print("[TelegramBot] \u23f9 DESACTIVADO - TELEGRAM_BOT_TOKEN vac\u00edo en .env")
            return
        if not self._chat_id:
            print("[TelegramBot] \u23f9 DESACTIVADO - TELEGRAM_CHAT_ID vac\u00edo en .env")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TelegramBot")
        self._thread.start()
        print("[TelegramBot] \U0001f7e0 CONECTANDO...")

        def _watchdog():
            while self._running:
                time.sleep(30)
                if self._thread and self._thread.is_alive():
                    continue
                print("[TelegramBot] \U0001f534 THREAD MUERTO - reintentando...")
                self._running = False
                time.sleep(1)
                self.start()
                break
        wd = threading.Thread(target=_watchdog, daemon=True, name="BotWatchdog")
        wd.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def push_update(self, snapshot: dict):
        """Thread-safe: feed latest dashboard data."""
        try:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda: self._queue.put_nowait(snapshot) if not self._queue.full() else None)
            else:
                self._queue.put_nowait(snapshot)
        except Exception:
            pass
    def _run_loop(self):
        while self._running:
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._main())
            except Exception as e:
                print(f"[TelegramBot] \U0001f534 Error fatal: {e} - reiniciando en 5s")
                log.error(f"Fatal: {e}")
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None
                self._session = None
            if self._running:
                time.sleep(5)
                print(f"[TelegramBot] \U0001f504 Reconectando... (chat_id={self._chat_id})")

    async def _main(self):
        connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=60, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=65, sock_connect=15)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            self._session = session
            await self._verify_connection()
            await asyncio.gather(
                asyncio.create_task(self._process_queue()),
                asyncio.create_task(self._poll_updates()),
                asyncio.create_task(self._alert_loop()),
                asyncio.create_task(self._heartbeat()),
                return_exceptions=True,
            )

    async def _heartbeat(self):
        """Log cada 5 minutos confirmando que el bucle del bot sigue vivo."""
        while self._running:
            await asyncio.sleep(300)
            if self._running:
                print("💓 [HEARTBEAT] BB-450 operando con normalidad...")

    async def _verify_connection(self):
        try:
            url = API_BASE.format(token=self._bot_token, method="getMe")
            async with self._session.get(url) as resp:
                data = await resp.json()
                if data.get("ok"):
                    bot_user = data["result"].get("username", "?")
                    print(f"[TelegramBot] \u2705 Bot @{bot_user} autenticado correctamente")
                    print(f"[TelegramBot] \U0001f7e0 CONECTADO - enviando a chat_id={self._chat_id}")
                    if self._gemini_enabled:
                        print("[TelegramBot] \U0001f916 Gemini AI activado - escribe cualquier mensaje para chatear")
                    else:
                        print("[TelegramBot] \u26a0 Gemini AI desactivado - agrega GEMINI_API_KEY en .env")
                else:
                    print(f"[TelegramBot] \u274c Token inv\u00e1lido - {data}")
                    return
        except Exception as e:
            print(f"[TelegramBot] \u274c Error de conexi\u00f3n: {e}")
            return

        try:
            msg = _format_premium_message(
                "\U0001f916 BB-450 Bot Conectado",
                f"El dashboard est\u00e1 corriendo y monitoreando <code>{settings.get_symbol()}</code>.\n\n"
                "<b>Comandos r\u00e1pidos:</b>\n"
                "\U0001f4ca /status — Estado del mercado\n"
                "\U0001f4c8 /signal — \u00daltima se\u00f1al\n"
                "\U0001f9e0 /brain — Cerebro cu\u00e1ntico\n"
                "\U0001f4c9 /chart — Gr\u00e1fico t\u00e9cnico\n"
                "\U0001f916 /gemini — Consultar Gemini AI\n"
                "\U0001f7e2 /buy — Abrir LONG\n"
                "\U0001f534 /sell — Abrir SHORT\n"
                "\U0001f6aa /close_all — Cerrar todo\n"
                "\U0001f4b0 /balance — Balance\n"
                "\U0001f4cd /positions — Posiciones\n\n"
                "<i>Usa los botones de abajo para acceder r\u00e1pido</i>",
            )
            await self._send(msg, reply_markup=_main_keyboard())
            print(f"[TelegramBot] \U0001f4e8 Mensaje de bienvenida enviado al chat {self._chat_id}")
        except Exception as e:
            print(f"[TelegramBot] \u26a0 No se pudo enviar el mensaje de bienvenida: {e}")
            print(f"[TelegramBot] \U0001f4a1 El usuario debe iniciar el bot con /start primero")
    # ─── Queue consumer ─────────────────────────────────────────────

    async def _process_queue(self):
        while self._running:
            try:
                snapshot = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                self._state = snapshot
                p = snapshot.get("price", 0)
                if p > 0:
                    self._price_history.append((time.time(), p))

                # 500ms snapshot freshness check
                snap_time = snapshot.get('_snapshot_time', 0)
                if snap_time and (time.time() - snap_time) > 0.5:
                    continue

                # DEDICATED BRAIN OBJECT
                if snapshot.get('type') == 'brain_signal':
                    await self._handle_brain_signal(snapshot)
                    continue

                # ORDER EXECUTION RESULT (from OrderExecutor)
                if snapshot.get('type') == 'order_execution':
                    text = snapshot.get('text', '')
                    if text:
                        await self._send(text)
                    continue

                # POSITION TRACKING (single-order mode)
                if snapshot.get('type') == 'position_tracking':
                    text = snapshot.get('text', '')
                    if text:
                        await self._send(text)
                    continue

                # CANAL 1: ALERTAS DE MERCADO
                await self._check_crash_alert()
                await self._check_pump_alert()

                vol = snapshot.get("volume", 0)
                avg_vol = snapshot.get("avg_volume", 0)
                await self._check_volume_alert_inline(snapshot, vol, avg_vol, p)
                await self._check_whale_inline(snapshot, vol, avg_vol, p)
                await self._check_radar_alert()
                await self._check_trap_change(snapshot)

                # Cambio de senial LONG/SHORT
                sig = snapshot.get("signal_text", "WAIT")
                if sig != self._last_signal:
                    self._last_signal = sig
                    if sig in ("LONG", "SHORT") and self._user_config.get("buy_sell", True):
                        conf = snapshot.get("confidence", 0)
                        vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0
                        if conf > 55 and avg_vol > 0 and vol_ratio >= 1.0:
                            await self._send_signal_alert(snapshot)
                        elif self._brain_block_log_counter % 90 == 0:
                            reason = f"confianza ({conf:.0f}%) < umbral 55%"
                            if avg_vol > 0 and vol_ratio < 1.0:
                                reason += f" o vol_ratio ({vol_ratio:.1f}x) < 1.0x"
                            elif avg_vol <= 0:
                                reason = "avg_volume aun sin datos (inicializando)"
                            log.info(f"Alerta bloqueada: {reason}")

                # CANAL 2: SENIAL DEL CEREBRO CUANTICO (FIX 4 — cooldown)
                brain_dir = snapshot.get('brain_direction', '')
                conf = snapshot.get('brain_confidence_pct', 0.0)
                if brain_dir in ('ALZA', 'BAJA') and conf >= 60:
                    ahora = time.time()
                    misma_dir = (brain_dir == self._last_brain_alert_direction)
                    cooldown_ok = (ahora - self._last_brain_alert_ts) > self.BRAIN_ALERT_COOLDOWN_SEC

                    # Price Action Invalidation
                    price_above_vwap = snapshot.get('price_above_vwap', False)
                    buy_imb_5 = snapshot.get('buy_imbalance_count_5', 0)
                    if brain_dir == 'BAJA' and price_above_vwap and buy_imb_5 >= 3:
                        self._log_brain_block(
                            "divergencia BAJA - precio sobre VWAP + "
                            f"{buy_imb_5} desequilibrios de compra")
                        self._last_brain_direction = None
                        continue

                    if (cooldown_ok or not misma_dir):
                        await self._send_brain_alert(snapshot)
                        self._last_brain_alert_ts = ahora
                        self._last_brain_alert_direction = brain_dir

            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                log.error(f"[Queue] Error en _process_queue: {exc}", exc_info=True)
                continue

    async def _handle_brain_signal(self, snapshot):
        bdir = snapshot.get('direction', 'INCIERTO')
        bconf = snapshot.get('confidence_pct', 0.0)

        # Leer bracket desde múltiples formatos (risk_bracket dict, risk dict, o flat)
        bracket = snapshot.get('risk_bracket', {}) or snapshot.get('risk', {})
        if not isinstance(bracket, dict):
            bracket = {}
        sl = bracket.get('sl', 0) or snapshot.get('brain_bracket_sl', 0)
        tp1 = bracket.get('tp1', 0) or snapshot.get('brain_bracket_tp1', 0)
        tp2 = bracket.get('tp2', 0) or snapshot.get('brain_bracket_tp2', 0)
        trigger = bracket.get('trigger', 0) or snapshot.get('brain_bracket_trigger', 0)
        trap = snapshot.get('trap_status', 'SIN TRAMPA')

        if bdir == self._last_brain_direction:
            self._log_brain_block("direcci\u00f3n repetida")
            return

        if 50.0 <= bconf < 60.0:
            now = time.time()
            if now < self._brain_flip_block_until:
                self._log_brain_block("low-conf cooldown")
                return
            self._brain_flip_block_until = now + 180.0

        # ── SAFETY GUARD: SL/TP NUNCA pueden ser 0 o negativos ────────
        if sl <= 0 or tp1 <= 0:
            self._log_brain_block(
                f"SL/TP inválido (SL=${sl:.0f} TP1=${tp1:.0f}) — señal abortada"
            )
            print(f"[⚠️ SAFETY] Alerta de {bdir} ABORTADA: "
                  f"SL=${sl:.0f} TP1=${tp1:.0f} — riesgo de liquidación a 100x")
            log.error(f"Brain signal ABORTED — invalid bracket: "
                      f"SL={sl} TP1={tp1} TP2={tp2} trigger={trigger}")
            return

        # Reconstruir bracket limpio para _send_brain_alert
        snapshot['risk_bracket'] = {
            'sl': sl, 'tp1': tp1, 'tp2': tp2,
            'trigger': trigger or snapshot.get('price', 0),
            'status': 'LONG' if bdir == 'ALZA' else 'SHORT',
        }

        self._last_brain_direction = bdir
        await self._send_brain_alert(snapshot)


    # ─── Brain block log throttle ──────────────────────────────────────────

    def _log_brain_block(self, reason: str):
        self._brain_block_log_counter += 1
        if self._brain_block_log_counter >= 60:
            self._brain_block_log_counter = 0
            print(f"[TELEGRAM BOT] Brain alert bloqueada: {reason}")

    # ─── Alert loop (every 5s) ─────────────────────────────────────────────

    async def _alert_loop(self):
        while self._running:
            await asyncio.sleep(5)
            if not self._state:
                continue
            try:
                await self._check_crash_alert()
                await self._check_pump_alert()
                await self._check_volume_alert()
                await self._check_trap_alert()
                await self._check_trend_alert()
                await self._check_signal_strength()
                await self._check_whale_alert()
                await self._check_radar_alert()
                await self._check_trade_opportunity()
            except Exception:
                pass

    # ─── Alert checks ──────────────────────────────────────────────────────

    async def _check_crash_alert(self):
        if not self._user_config.get("crash", True) or len(self._price_history) < 30:
            return
        now = time.time()
        recent = [p for t, p in self._price_history if t >= now - 60]
        if len(recent) < 10:
            return
        current = recent[-1]
        peak_60s = max(recent)
        drop_pct = (peak_60s - current) / max(peak_60s, 1) * 100
        if drop_pct >= self.SCALP_CRASH_PCT and not self._alert_state.sent_crash:
            self._alert_state.sent_crash = True
            body = (
                f"  Precio: <code>${current:,.0f}</code>\n"
                f"  Caida: <code>{drop_pct:.2f}%</code> en 60s\n"
                f"  Pico: <code>${peak_60s:,.0f}</code>\n"
                f"  Delta: <code>{self._state.get('delta', 0):+.0f}</code> | CVD: <code>{self._state.get('cvd', 0):.0f}</code>\n"
                f"  Vol: <code>{self._state.get('volume', 0):.1f}</code> | B/A: <code>{self._state.get('ba_ratio', 1):.2f}x</code>\n\n"
                f"\U0001f6a8 <i>Revisar posiciones STOP-LOSS</i>"
            )
            await self._send(_format_premium_message("\U0001f4a5 ALERTA: FLASH CRASH", body))
        elif drop_pct < self.SCALP_CRASH_PCT / 2:
            self._alert_state.sent_crash = False

    async def _check_pump_alert(self):
        if not self._user_config.get("crash", True) or len(self._price_history) < 30:
            return
        now = time.time()
        recent = [p for t, p in self._price_history if t >= now - 60]
        if len(recent) < 10:
            return
        current = recent[-1]
        low_60s = min(recent)
        pump_pct = (current - low_60s) / max(low_60s, 1) * 100
        if pump_pct >= self.SCALP_CRASH_PCT and not self._alert_state.sent_pump:
            self._alert_state.sent_pump = True
            body = (
                f"  Precio: <code>${current:,.0f}</code>\n"
                f"  Subida: <code>{pump_pct:.2f}%</code> en 60s\n"
                f"  Minimo: <code>${low_60s:,.0f}</code>\n\n"
                f"\U0001f4a1 <i>Evaluar toma de ganancias parciales</i>"
            )
            await self._send(_format_premium_message("\U0001f4a5 ALERTA: FLASH PUMP", body))
        elif pump_pct < self.SCALP_CRASH_PCT / 2:
            self._alert_state.sent_pump = False

    async def _check_volume_alert(self):
        if not self._user_config.get("volume", True):
            return
        vol = self._state.get("volume", 0)
        avg_vol = self._state.get("avg_volume", 0)
        if avg_vol <= 0:
            return
        mult = vol / avg_vol
        if mult >= settings.ALERT_VOLUME_SPIKE and not self._alert_state.sent_volume:
            self._alert_state.sent_volume = True
            body = (
                f"  Multiplicador: <code>{mult:.1f}x</code>\n"
                f"  Vol: <code>{vol:.2f}</code> | Avg: <code>{avg_vol:.2f}</code>\n"
                f"  Precio: <code>${self._state.get('price', 0):,.0f}</code>"
            )
            await self._send(_format_premium_message("\U0001f4ca ALERTA: VOLUMEN ANORMAL", body))
        elif mult < settings.ALERT_VOLUME_SPIKE / 2:
            self._alert_state.sent_volume = False

    async def _check_volume_alert_inline(self, snapshot, vol, avg_vol, p):
        if avg_vol <= 0:
            return
        mult = vol / avg_vol
        if mult >= settings.ALERT_VOLUME_SPIKE and not self._alert_state.sent_volume:
            self._alert_state.sent_volume = True
            body = (
                f"  Multiplicador: <code>{mult:.1f}x</code>\n"
                f"  Vol: <code>{vol:.2f}</code> | Avg: <code>{avg_vol:.2f}</code>\n"
                f"  Precio: <code>${p:,.0f}</code>"
            )
            await self._send(_format_premium_message("\U0001f4ca ALERTA: VOLUMEN ANORMAL", body))
        elif mult < settings.ALERT_VOLUME_SPIKE / 2:
            self._alert_state.sent_volume = False

    async def _check_whale_inline(self, snapshot, vol, avg_vol, p):
        delta_accel_raw = snapshot.get('delta_accel', 0)
        tick_spd = snapshot.get('tick_speed', 0)
        if (abs(delta_accel_raw) > self.SCALP_WHALE_DELTA_ACCEL_THRESHOLD
                and tick_spd > self.SCALP_WHALE_TICK_SPEED_THRESHOLD):
            if not self._alert_state.sent_whale:
                self._alert_state.sent_whale = True
                side = "\U0001f7e2 COMPRADORA" if delta_accel_raw > 0 else "\U0001f7e3 VENDEDORA"
                body = (
                    f"Delta Accel: <code>{delta_accel_raw:+.1f}</code>\n"
                    f"Tick Speed: <code>{tick_spd:.1f} t/s</code>\n"
                    f"Precio: <code>${p:,.0f}</code>\n"
                    f"\u23f0 {snapshot.get('timestamp', '')}"
                )
                await self._send(_format_premium_message(f"\U0001f40b BALLENA {side} (INSTANT)", body))
        elif not self._alert_state.sent_whale:
            self._whale_cooldown = getattr(self, '_whale_cooldown', 0) + 1
            if self._whale_cooldown >= 12:
                self._alert_state.sent_whale = False
                self._whale_cooldown = 0

    async def _check_trap_change(self, snapshot):
        current_trap = snapshot.get('trap_status', 'SIN TRAMPA')
        prev_trap = getattr(self, '_prev_trap_status', 'SIN TRAMPA')
        if current_trap != prev_trap and 'SIN TRAMPA' not in current_trap:
            self._prev_trap_status = current_trap
            prob_dir = snapshot.get('directional_probability', 50.0)
            if prob_dir >= self.SCALP_TRAP_PROB_THRESHOLD:
                print(f"[TELEGRAM BOT] Cambio de trampa detectado: "
                      f"{prev_trap} -> {current_trap} - despachando INMEDIATO")
                await self._send_trap_change_alert(snapshot, current_trap)
        self._prev_trap_status = current_trap

    async def _check_trap_alert(self):
        if not self._user_config.get("rsi", True):
            return
        s = self._state
        if not s:
            return
        trap = s.get('trap_status', 'SIN TRAMPA')
        prob = s.get('directional_probability', 50.0)
        bias = s.get('market_bias', 'INCIERTO')
        price = s.get('price', 0)

        if prob < self.SCALP_TRAP_PROB_THRESHOLD or trap == 'SIN TRAMPA':
            self._alert_state.sent_trap = False
            return
        prev = getattr(self, '_prev_trap_type', None)
        if trap == prev and self._alert_state.sent_trap:
            return
        self._prev_trap_type = trap
        self._alert_state.sent_trap = True

        emoji = "\U0001f7e2" if bias == 'ALZA' else "\U0001f7e3"
        body = (
            f"{trap}\n\n"
            f"{emoji} <b>DIRECCION:</b> <code>{bias}</code> | Probabilidad: <code>{prob:.0f}%</code>\n"
            f"  Precio: <code>${price:,.0f}</code>\n"
            f"\u23f0 {s.get('timestamp', '')}"
        )
        await self._send(_format_premium_message("\u26a1 OPORTUNIDAD ASIMETRICA DETECTADA", body))

    async def _check_trend_alert(self):
        if not self._user_config.get("trend_change", True):
            return
        trend = self._state.get("trend", "NEUTRAL")
        if not hasattr(self, '_prev_trend'):
            self._prev_trend = trend
            self._last_trend_alert_ts = 0.0
            return

        if trend == self._prev_trend:
            return

        # ── FILTRO 1: Choppiness / Rango sucio ──────────────────────────
        signal_text = self._state.get("signal_text", "WAIT")
        trend_label = self._state.get("trend_label", "")
        chop_keywords = ("CHOP", "HFT", "SQUEEZE", "WIDE SPREAD", "NO EDGE", "NO CLEAR")
        is_choppy = (
            signal_text == "WAIT"
            or any(k in trend_label.upper() for k in chop_keywords)
        )
        if is_choppy:
            self._prev_trend = trend
            return

        # ── FILTRO 2: RSI suavizado (5m velas cerradas) ─────────────────
        rsi_5m = self._state.get("rsi_5m", 0)
        if not (50 < rsi_5m < 100 if trend == "ALCISTA" else 0 < rsi_5m < 50):
            if rsi_5m > 0:
                self._prev_trend = trend
            return

        # ── COOLDOWN 300s (salvo volumen extremo > 1000x) ───────────────
        now = time.time()
        volume = self._state.get("volume", 0)
        avg_vol = self._state.get("avg_volume", 0)
        vol_ratio = volume / max(avg_vol, 0.001)
        last_ts = getattr(self, '_last_trend_alert_ts', 0.0)
        sec_since_last = now - last_ts
        if sec_since_last < 300.0 and vol_ratio < 1000:
            self._prev_trend = trend
            return

        # ── FILTRO 3: Validación cruzada Delta / CVD ───────────────────
        delta = self._state.get("delta", 0)
        cvd = self._state.get("cvd", 0)
        if trend in ("ALCISTA", "LONG") and (delta < -self.TENDENCIA_DELTA_MIN or cvd < -self.TENDENCIA_DELTA_MIN):
            logger.warning("Alerta ALCISTA bloqueada por Delta/CVD negativos: Δ=%.0f CVD=%.0f", delta, cvd)
            self._prev_trend = trend
            return
        if trend in ("BAJISTA", "SHORT") and (delta > self.TENDENCIA_DELTA_MIN or cvd > self.TENDENCIA_DELTA_MIN):
            logger.warning("Alerta BAJISTA bloqueada por Delta/CVD positivos: Δ=%.0f CVD=%.0f", delta, cvd)
            self._prev_trend = trend
            return

        # ── Todos los filtros superados — enviar alerta ─────────────────
        self._prev_trend = trend
        self._last_trend_alert_ts = now
        emoji = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f7e3"
        body = (
            f"  Nueva tendencia: <code>{trend}</code>\n"
            f"  Precio: <code>${self._state.get('price', 0):,.0f}</code>\n"
            f"  RSI 5m: <code>{rsi_5m:.1f}</code>\n"
            f"  Δ: <code>{delta:+.0f}</code> | CVD: <code>{cvd:+.0f}</code>"
        )
        await self._send(_format_premium_message(f"{emoji} CAMBIO DE TENDENCIA", body))

    async def _check_signal_strength(self):
        """Alert when signal confidence crosses thresholds.

        Applies two anti-spam / divergence filters:

        1. **Price Action Invalidation**: If signal is SHORT but price is
           above VWAP *and* 3+ buy-imbalance candles in last 5, reset the
           alert state silently.

        2. **Temporal Spam Filter**: If confidence stays trapped in the
           20-35% low range, block duplicates - only send when confidence
           moves by >10% or the signal direction flips.
        """
        conf = self._state.get("confidence", 0)
        sig = self._state.get("signal_text", "WAIT")
        if not hasattr(self, '_prev_conf'):
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return
        if not hasattr(self, '_prev_conf_sig'):
            self._prev_conf_sig = sig

        # ── HARD CONFIDENCE GATE: ignore weak signals ─────────────────
        if conf < 45:
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return

        price_above_vwap = self._state.get('price_above_vwap', False)
        buy_imb_5 = self._state.get('buy_imbalance_count_5', 0)

        # ── FILTRO A: Choppiness (mercado en rango) ─────────────────────
        trend_label = self._state.get("trend_label", "")
        chop_keywords = ("CHOP", "HFT", "SQUEEZE", "WIDE SPREAD", "NO EDGE", "NO CLEAR")
        if sig == "WAIT" or any(k in trend_label.upper() for k in chop_keywords):
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return

        # ── FILTRO B: RSI 5m debe confirmar dirección ──────────────────
        rsi_5m = self._state.get("rsi_5m", 0)
        if sig == "LONG" and not (rsi_5m > 50):
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return
        if sig == "SHORT" and not (0 < rsi_5m < 50):
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return

        # ── FILTRO C: Cooldown 300s (salvo volumen extremo) ────────────
        now = time.time()
        volume = self._state.get("volume", 0)
        avg_vol = self._state.get("avg_volume", 0)
        vol_ratio = volume / max(avg_vol, 0.001)
        last_ts = getattr(self, '_last_strength_alert_ts', 0.0)
        if now - last_ts < 300.0 and vol_ratio < 1000:
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return

        # FILTER 1: Price Action Invalidation
        if sig == 'SHORT' and price_above_vwap and buy_imb_5 >= 3:
            self._prev_conf = conf
            self._prev_conf_sig = sig
            return

        # FILTER 2: Temporal Spam Filter (range expanded to 20-35%)
        diff = abs(conf - self._prev_conf)
        if 20 <= conf <= 35 and 20 <= self._prev_conf <= 35:
            if diff <= 10 and sig == self._prev_conf_sig:
                self._prev_conf = conf
                self._prev_conf_sig = sig
                return

        # Original threshold logic
        if diff >= 20 and sig in ("LONG", "SHORT") and self._user_config.get("buy_sell", True):
            self._last_strength_alert_ts = time.time()
            emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"
            body = (
                f"  Senal: <code>{sig}</code> | Confianza: <code>{conf:.0f}%</code>\n"
                f"  Delta: <code>{self._state.get('delta', 0):.0f}</code>\n"
                f"  Precio: <code>${self._state.get('price', 0):,.0f}</code>"
            )
            await self._send(_format_premium_message(f"{emoji} SENIAL FORTALECIDA", body))

        self._prev_conf = conf
        self._prev_conf_sig = sig

    async def _check_whale_alert(self):
        if not self._user_config.get("whale", True):
            return
        s = self._state
        if not s:
            return
        if self._alert_state.sent_whale:
            self._whale_cooldown = getattr(self, '_whale_cooldown', 0) + 1
            if self._whale_cooldown >= 12:
                self._alert_state.sent_whale = False
                self._whale_cooldown = 0
            return

        cum_delta = s.get('cumulative_delta', 0)
        vol = s.get('volume', 0)
        bv = s.get('buy_volume', 0)
        sv = s.get('sell_volume', 0)
        ts = s.get('tick_speed', 0)

        delta_accel = cum_delta - self._alert_state.prev_cum_delta
        self._alert_state.prev_cum_delta = cum_delta

        if not (abs(delta_accel) > 100 and vol > 5 and ts > 30):
            return

        self._alert_state.sent_whale = True
        side = "\U0001f7e2 COMPRADORA" if delta_accel > 0 else "\U0001f7e3 VENDEDORA"
        body = (
            f"Delta acum: <code>{cum_delta:+.1f}</code> (d <code>{delta_accel:+.1f}</code>)\n"
            f"Vol: <code>{vol:.1f}</code> | B/A: <code>{bv:.0f}/{sv:.0f}</code>\n"
            f"Tick Speed: <code>{ts:.1f} t/s</code>\n"
            f"Precio: <code>${s.get('price', 0):,.0f}</code>\n"
            f"\u23f0 {s.get('timestamp', '')}"
        )
        await self._send(_format_premium_message(f"\U0001f40b BALLENA {side}", body))

    async def _check_radar_alert(self):
        if not self._user_config.get("radar", True):
            return
        now = time.time()
        if now - self._radar_cooldown < 10:
            return
        s = self._state
        if not s:
            return

        p = s.get('price', 0)
        vol = s.get('volume', 0)
        avg_vol = s.get('avg_volume', 0)
        vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0
        tick_spd = s.get('tick_speed', 0)
        delta_accel = abs(s.get('delta_accel', 0))
        rsi = s.get('rsi', 50)
        bb_pos = s.get('bb_position', 50)
        change = s.get('change_pct', 0)
        delta = s.get('delta', 0)
        cvd = s.get('cvd', 0)

        triggers = []
        if vol_ratio >= 3.0:
            triggers.append(f"Vol {vol_ratio:.1f}x")
        if tick_spd > 30 and delta_accel > 100:
            triggers.append(f"Ticks {tick_spd:.0f}/s Dd {delta_accel:.0f}")
        if rsi > 75:
            triggers.append(f"RSI {rsi:.1f} SOBRECOMPRA")
        elif rsi < 25:
            triggers.append(f"RSI {rsi:.1f} SOBREVENTA")
        if bb_pos > 92:
            triggers.append(f"BB {bb_pos:.0f}% SUP")
        elif bb_pos < 8:
            triggers.append(f"BB {bb_pos:.0f}% INF")
        if abs(change) >= 0.3:
            triggers.append(f"D {change:+.2f}%")

        if not triggers:
            if self._alert_state.sent_radar:
                self._alert_state.sent_radar = False
            return

        if not self._alert_state.sent_radar:
            self._alert_state.sent_radar = True
            self._radar_cooldown = now

            # ── Delta/CVD divergence check ──────────────────────────
            divergence = False
            pct = change
            if pct >= 0.3 and (delta < -self.TENDENCIA_DELTA_MIN or cvd < -self.TENDENCIA_DELTA_MIN):
                divergence = True
            elif pct <= -0.3 and (delta > self.TENDENCIA_DELTA_MIN or cvd > self.TENDENCIA_DELTA_MIN):
                divergence = True

            reasons = " | ".join(triggers[:3])
            if divergence:
                label = "\u26a0\ufe0f <i>Divergencia detectada \u2014 NO entrar</i>"
                logger.warning("Radar divergence: Δ=%.0f CVD=%.0f change=%.2f%%", delta, cvd, pct)
            else:
                label = "\U0001f6a8 <i>Movimiento fuerte - monitorear entrada</i>"
            body = (
                f"  Precio: <code>${p:,.0f}</code>\n"
                f"  {reasons}\n"
                f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.0f}</code>\n\n"
                f"{label}"
            )
            await self._send(_format_premium_message("\U0001f4e1 ALERTA DE RADAR", body))
        elif vol_ratio < 1.5 and tick_spd < 20 and 30 < rsi < 70 and 15 < bb_pos < 85:
            self._alert_state.sent_radar = False

    async def _check_trade_opportunity(self):
        if not self._user_config.get("ai_trade", True):
            return
        s = self._state
        if not s:
            return
        conf = s.get("confidence", 0)
        direction = s.get("signal_text", "WAIT")
        if conf < 55 or direction not in ("LONG", "SHORT"):
            return
        if not hasattr(self, '_last_trade_check'):
            self._last_trade_check = 0
        now = time.time()
        if now - self._last_trade_check < 30:
            return
        self._last_trade_check = now
        if not self._gemini_enabled:
            return

        price = s.get("price", 0)
        delta = s.get("delta", 0)
        delta_accel = s.get("delta_accel", 0)
        cvd = s.get("cvd", 0)
        rsi = s.get("rsi", 50)
        trend_5m = s.get("trend_5m", "WAIT")
        trend_1h = s.get("trend_1h", "WAIT")
        cum_delta = s.get("cumulative_delta", 0)
        ba = s.get("ba_ratio", 1.0)
        vol = s.get("volume", 0)
        pinam = s.get("pinam", 0)
        cancel = s.get("cancel_rate", 0)
        wall_bid_sz = s.get("wall_bid_size", 0)
        wall_ask_sz = s.get("wall_ask_size", 0)
        sv_val = s.get("spread_velocity", 0)
        ts = s.get("tick_speed", 0)

        prompt = (
            f"Eres un trader profesional. Analiza si es seguro abrir una operacion.\n\n"
            f"SENIAL: {direction} | CONFIANZA: {conf:.0f}%\n"
            f"PRECIO: ${price:,.0f}\n"
            f"DELTA: {delta:+.1f} | ACCEL: {delta_accel:+.1f} | CVD: {cvd:+.1f} | CUM DELTA: {cum_delta:+.1f}\n"
            f"RSI: {rsi:.1f} | B/A RATIO: {ba:.3f}x | VOL: {vol:.1f}\n"
            f"PINAM: {pinam:.4f} | CANCEL RATE: {cancel:.1f}%\n"
            f"TREND 5M: {trend_5m} | TREND 1H: {trend_1h}\n"
            f"TICK: {ts:.1f}/s | SPREAD VEL: {sv_val:.1f}ms\n"
            f"WALL BID: {wall_bid_sz:.1f} BTC | WALL ASK: {wall_ask_sz:.1f} BTC\n\n"
            f"INSTRUCCIONES:\n"
            f"1) Decide si ENTRAR o NO ENTRAR.\n"
            f"2) Si es ENTRAR: da entry exacto, SL (por debajo de soporte), TP (1:2 riesgo/recompensa minimo).\n"
            f"3) Si es NO ENTRAR: explica por que (trampa, spoofing, poco volumen, divergencia).\n"
            f"4) Responde SOLO JSON con formato:\n"
            f'{{"decision":"ENTRAR"|"NO_ENTRAR","entry":precio,"sl":precio,"tp":precio,"razon":"texto"}}'
        )

        try:
            reply = await self._chat_gemini_raw(prompt)
            data = json.loads(reply)
        except Exception:
            return

        decision = data.get("decision", "NO_ENTRAR")
        razon = data.get("razon", "Sin analisis")
        entry = data.get("entry", price)
        sl = data.get("sl", price * 0.995)
        tp = data.get("tp", price * 1.01)
        capital = self._user_config.get("capital", 100)
        sl_pct = abs((entry - sl) / entry) * 100

        emoji = "\U0001f7e2" if direction == "LONG" else "\U0001f7e3"
        if decision == "ENTRAR":
            body = (
                f"<b>DIRECCION:</b> {direction} | Confianza: <code>{conf:.0f}%</code>\n"
                f"<b>PRECIO:</b> <code>${entry:,.1f}</code>\n\n"
                f"<b>\U0001f4b0 CAPITAL:</b> <code>${capital:.1f}</code> x <code>{settings.LEVERAGE}x</code>\n"
                f"<b>\U0001f6a9 STOP LOSS:</b> <code>${sl:,.1f}</code> ({sl_pct:.1f}%)\n"
                f"<b>\U0001f4c8 TAKE PROFIT:</b> <code>${tp:,.1f}</code>\n\n"
                f"<b>\U0001f4ac AI:</b> {razon}"
            )
            await self._send(_format_premium_message(f"{emoji} OPORTUNIDAD CONFIRMADA POR AI", body))
        else:
            body = (
                f"Senal: <code>{direction}</code> ({conf:.0f}%) | Precio: <code>${price:,.0f}</code>\n\n"
                f"<b>\U0001f4ac Analisis AI:</b>\n{razon}\n\n"
                f"<i>Esperando mejor oportunidad...</i>"
            )
            await self._send(_format_premium_message("\u26a0\ufe0f AI RECOMIENDA NO ENTRAR", body))

        if decision == "ENTRAR":
            klines = await self._fetch_klines(interval="5m", limit=100)
            if klines:
                png = await self._generate_brain_chart(klines, entry, sl, tp)
                if png:
                    caption = (
                        f"{emoji} <b>{direction} CONFIRMADO POR AI</b>\n"
                        f"SL: <code>${sl:,.0f}</code> | TP: <code>${tp:,.0f}</code>"
                    )
                    await self._send_photo(png, caption=caption)


    # ─── Send helpers ──────────────────────────────────────────────────────

    async def _send(self, text: str, chat_id: int = None,
                     reply_markup: Optional[dict] = None) -> bool:
        """Send HTML message — runs through format_for_telegram(context='message')."""
        cid = chat_id or self._chat_id
        if not cid:
            return False
        text = format_for_telegram(text, context="message")
        clean_preview = re.sub(r'<[^>]+>', '', text[:200]).replace('\n', ' | ')
        log.warning(
            "[FORENSIC] _send: text_len=%d preview=%s",
            len(text), clean_preview[:120])
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": cid, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                text_resp = await resp.text()
                if "can't parse entities" in text_resp.lower():
                    log.info("[Telegram] HTML parse error — resending as plain text")
                    plain = format_for_telegram(text, context="plain")
                    payload2 = {"chat_id": cid, "text": plain,
                                "disable_web_page_preview": True}
                    if reply_markup:
                        payload2["reply_markup"] = reply_markup
                    async with session.post(url, json=payload2,
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                        return resp2.status == 200
                log.warning("Telegram send error %s: %s", resp.status, text_resp)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram send exception: %s", e)
            return False
        finally:
            if self._session is None:
                await session.close()

    async def _send_photo(self, photo_bytes: bytes, caption: str = "",
                           chat_id: int = None,
                           reply_markup: Optional[dict] = None) -> bool:
        cid = chat_id or self._chat_id
        if not cid:
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        session = self._session or aiohttp.ClientSession()
        try:
            data = aiohttp.FormData()
            data.add_field("chat_id", str(cid))
            data.add_field("photo", photo_bytes,
                           filename="chart.png", content_type="image/png")
            if caption:
                caption = format_for_telegram(caption, context="caption")
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
            if reply_markup:
                data.add_field("reply_markup", json.dumps(reply_markup),
                               content_type="application/json")
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return True
                text_resp = await resp.text()
                if caption and "can't parse entities" in text_resp.lower():
                    log.info("[Telegram] sendPhoto HTML parse error — resending without caption HTML")
                    data2 = aiohttp.FormData()
                    data2.add_field("chat_id", str(cid))
                    data2.add_field("photo", photo_bytes,
                                    filename="chart.png", content_type="image/png")
                    plain_caption = format_for_telegram(caption, context="plain")
                    data2.add_field("caption", plain_caption)
                    if reply_markup:
                        data2.add_field("reply_markup", json.dumps(reply_markup),
                                        content_type="application/json")
                    async with session.post(url, data=data2,
                                            timeout=aiohttp.ClientTimeout(total=30)) as resp2:
                        if resp2.status == 200:
                            return True
                        text_resp2 = await resp2.text()
                        log.warning("Telegram sendPhoto fallback error %s: %s",
                                    resp2.status, text_resp2)
                        return False
                log.warning("Telegram sendPhoto error %s: %s", resp.status, text_resp)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram sendPhoto exception: %s", e)
            return False
        finally:
            if self._session is None:
                await session.close()

    async def _edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        text = format_for_telegram(text, context="edit")
        url = f"https://api.telegram.org/bot{self._bot_token}/editMessageText"
        payload = {"chat_id": chat_id, "message_id": message_id,
                    "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception:
            return False
        finally:
            if self._session is None:
                await session.close()

    # ─── Typing indicator ──────────────────────────────────────────────────

    async def _send_typing(self, chat_id: int = None):
        cid = chat_id or self._chat_id
        if not cid:
            return
        url = f"https://api.telegram.org/bot{self._bot_token}/sendChatAction"
        payload = {"chat_id": cid, "action": "typing"}
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                pass
        except Exception:
            pass
        finally:
            if self._session is None:
                await session.close()

    # ─── Alert senders ─────────────────────────────────────────────────────

    async def _send_signal_alert(self, snapshot: dict):
        p = snapshot.get("price", 0)
        sig = snapshot.get("signal_text", "WAIT")
        conf = snapshot.get("confidence", 0)
        delta = snapshot.get("delta", 0)
        cvd = snapshot.get("cvd", 0)
        ba = snapshot.get("ba_ratio", 1.0)
        vol = snapshot.get("volume", 0)
        avg_vol = snapshot.get("avg_volume", 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        rsi = snapshot.get("rsi", 50)
        trend = snapshot.get("trend_5m", "WAIT")
        trap = snapshot.get("trap_status", "N/A")
        emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"

        _dc = snapshot.get('debug_composite', '?')
        _dt = snapshot.get('debug_threshold', '?')
        _cvp = snapshot.get('debug_cvd_pct', '?')
        _dvp = snapshot.get('debug_delta_pct', '?')
        _cvr = snapshot.get('debug_cvd_raw', '?')
        _drw = snapshot.get('debug_delta_raw', '?')
        body = (
            f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n\n"
            f"<b>MERCADO:</b>\n"
            f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"  B/A: <code>{ba:.3f}x</code> | Vol: <code>{vol_r:.1f}x</code>\n"
            f"  RSI: <code>{rsi:.1f}</code> | Trend 5m: <code>{trend}</code>\n"
            f"  Trampa: <code>{trap}</code>\n"
            f"  Tick Int: <code>{snapshot.get('tick_integrity_score', 1.0):.1f}</code>\n"
            f"  Funding: <code>{snapshot.get('funding_rate', 0.0):+.4f}%</code>\n"
            f"  OI Δ5m: <code>{snapshot.get('oi_delta_5min', 0.0):+.1f}%</code>\n\n"
            f"<b>ESTRATEGIA:</b>\n"
            f"  1\u20e3 Confirmar con CVD/Delta\n"
            f"  2\u20e3 Esperar retroceso a soporte\n"
            f"  3\u20e3 Entrada con SL ajustado a la vela anterior\n\n"
            f"<code>DBG comp={_dc} thr={_dt} cvd_pct={_cvp} "
            f"Δpct={_dvp} cvd_raw={_cvr} Δraw={_drw}</code>\n"
            f"{'🧠 Cerebro en entrenamiento — ' + str(snapshot.get('trades_until_active', '?')) + ' trades para activación completa\n' if snapshot.get('learning_mode') else ''}"
            f"\u23f0 {snapshot.get('timestamp', '')}"
        )
        await self._send(_format_premium_message(f"{emoji} {sig} DETECTADO ({conf:.0f}%)", body),
                         reply_markup=_main_keyboard())

    async def _send_position_with_close(self, executor, chat_id: int):
        loop = asyncio.get_event_loop()
        pos = await loop.run_in_executor(None, executor.get_position_with_pnl)
        if pos is None:
            await self._send("\u2139\ufe0f No hay posici\u00f3n abierta actualmente.",
                             chat_id=chat_id, reply_markup=_main_keyboard())
            return
        emoji = "\U0001f7e2" if pos["direction"] == "LONG" else "\U0001f7e3"
        pnl = pos["pnl"]
        pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
        body = (
            f"{emoji} <b>{pos['direction']}</b> | "
            f"<code>{pos['entry_qty']} BTC</code>\n"
            f"Entrada: <code>${pos['entry_price']:,.0f}</code>\n"
            f"Mark: <code>${pos['mark_price']:,.0f}</code>\n"
            f"Liq: <code>${pos['liquidation_price']:,.0f}</code>\n"
            f"{pnl_emoji} P&L: "
            f"<code>{'$' if pnl >= 0 else '-$'}{abs(pnl):,.2f}</code> "
            f"({pos['pnl_pct']:+.2f}%)"
        )
        close_kb = {"inline_keyboard": [[
            _btn("\U0001f6aa Cerrar Posici\u00f3n", "close_position")
        ]]}
        await self._send(_format_premium_message(
            "\u26a0\ufe0f YA HAY UNA POSICI\u00d3N ABIERTA", body),
            chat_id=chat_id, reply_markup=close_kb)

    async def _send_brain_alert(self, snapshot: dict):
        bdir = snapshot.get('direction') or snapshot.get('brain_direction', 'INCIERTO')
        bconf = snapshot.get('confidence_pct') or snapshot.get('brain_confidence_pct', 0.0)
        p = snapshot.get('price', 0)
        delta = snapshot.get('delta', 0)
        cvd = snapshot.get('cvd', 0)
        rsi = snapshot.get('rsi', 50)
        vol = snapshot.get('volume', 0)
        avg_vol = snapshot.get('avg_volume', 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        bracket = snapshot.get('risk_bracket', {})
        if not isinstance(bracket, dict):
            bracket = {}
        sl = bracket.get('sl', 0) or snapshot.get('brain_bracket_sl', 0)
        tp1 = bracket.get('tp1', 0) or snapshot.get('brain_bracket_tp1', 0)
        tp2 = bracket.get('tp2', 0) or snapshot.get('brain_bracket_tp2', 0)

        if sl <= 0 or tp1 <= 0:
            log.error(f"[SAFETY] _send_brain_alert ABORTADO — "
                      f"SL=${sl:.0f} TP=${tp1:.0f}")
            return

        capital = self._user_config.get('capital', 100)
        sl_pct = abs((p - sl) / p * 100) if p > 0 else 0
        emoji = "\U0001f7e2" if bdir == 'ALZA' else "\U0001f7e3"

        r_perdida = sl_pct
        r_ganancia = abs((tp1 - sl) / (p - sl)) * r_perdida if abs(p - sl) > 0 else 0

        # ── 1. AI analysis via Gemini ─────────────────────────────────
        ai_text = ""
        if self._gemini_enabled:
            prompt = (
                f"Analiza esta señal de trading para {settings.get_symbol()}:\n"
                f"- Dirección: {bdir}\n- Confianza: {bconf:.1f}%\n"
                f"- Precio: ${p:,.0f}\n- Delta: {delta:+.0f}, CVD: {cvd:.1f}\n"
                f"- RSI: {rsi:.1f}, Vol relativo: {vol_r:.1f}x\n"
                f"- SL: ${sl:,.0f}, TP: ${tp1:,.0f}\n\n"
                f"Da un análisis técnico breve de 2 líneas explicando "
                f"por qué esta entrada tiene sentido. "
                f"Usa solo texto plano, sin asteriscos, sin HTML, sin formato."
            )
            try:
                raw = await self._chat_gemini_raw(prompt)
                if raw and "Gemini no disponible" not in raw and "Error Gemini" not in raw:
                    ai_text = raw.strip()
            except Exception:
                pass

        # ── 2. Generate chart with entry/SL/TP lines ─────────────────
        klines = await self._fetch_klines()
        chart_bytes = None
        if klines:
            chart_bytes = await self._generate_brain_chart(klines, p, sl, tp1)

        # ── 3. Build caption (matches the existing format) ───────────
        r_ratio = 0.0
        if r_perdida > 0:
            r_ratio = r_ganancia / r_perdida

        # New v4-Speed fields
        tick_int = snapshot.get('tick_integrity_score', 1.0)
        fr = snapshot.get('funding_rate', 0.0)
        oi_d5 = snapshot.get('oi_delta_5min', 0.0)
        bd_bv = snapshot.get('book_depth_bids_volume', 0.0)
        bd_av = snapshot.get('book_depth_asks_volume', 0.0)
        magnet = snapshot.get('liquidity_magnet', 'NONE')
        magnet_age = time.time() - snapshot.get('magnet_timestamp', 0) if snapshot.get('magnet_timestamp', 0) > 0 else 0

        caption = (
            f"{emoji} <b>{bdir} — Confianza {bconf:.1f}%</b>\n"
            f"<b>Precio:</b> <code>${p:,.0f}</code>\n"
            f"<b>SL:</b> <code>${sl:,.0f}</code> | "
            f"<b>TP:</b> <code>${tp1:,.0f}</code>\n"
            f"Delta: <code>{delta:+.0f}</code> CVD: <code>{cvd:.1f}</code> "
            f"RSI: <code>{rsi:.1f}</code>\n"
            f"Vol: <code>{vol_r:.1f}x</code> | "
            f"<b>R:</b> <code>{r_ratio:.1f}R</code>"
            f"{f'\nTick Int: <code>{tick_int:.1f}</code>' if tick_int < 5 else ''}"
            f"{f'\nFunding: <code>{fr:+.4f}%</code>' if abs(fr) > 0.01 else ''}"
            f"{f'\nOI Δ5m: <code>{oi_d5:+.1f}%</code>' if abs(oi_d5) > 5 else ''}"
            f"{f'\nMagnet: <code>{magnet}</code> ({magnet_age:.0f}s)' if magnet != 'NONE' else ''}"
            f"{f'\n\n🤖 <i>{limpiar_texto_telegram(ai_text)}</i>' if ai_text else ''}"
            f"{'🧠 Cerebro en entrenamiento — ' + str(snapshot.get('trades_until_active', '?')) + ' trades para activación completa' if snapshot.get('learning_mode') else ''}"
        )

        # ── 4. Inline keyboard ──────────────────────────────────────
        callback_data = (
            f"exec_order:{bdir}:{p}:{sl}:{tp1}"
        )
        keyboard = {
            "inline_keyboard": [[
                _btn("\U0001f680 Autorizar Orden a Mercado", callback_data)
            ]]
        }

        # ── 5. Send photo (with caption + inline button) or fallback ─
        caption_clean = re.sub(r'<[^>]+>', '', caption[:150]).replace('\n', ' | ')
        log.warning(
            "[FORENSIC] _send_brain_alert caption_len=%d caption_preview=%s "
            "has_chart=%s",
            len(caption), caption_clean[:100], bool(chart_bytes))
        if chart_bytes:
            ok = await self._send_photo(chart_bytes, caption=caption,
                                        reply_markup=keyboard)
            if not ok:
                log.error("[FORENSIC] _send_brain_alert: _send_photo FAILED")
        else:
            body = (
                f"<b>DIRECCION:</b> {emoji} {bdir}\n"
                f"<b>CONFIANZA:</b> <code>{bconf:.1f}%</code>\n"
                f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n\n"
                f"<b>MERCADO:</b>\n"
                f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
                f"  Vol: <code>{vol_r:.1f}x</code>\n\n"
                f"<b>\U0001f4b0 CAPITAL:</b> <code>${capital:.1f}</code> x "
                f"<code>{settings.LEVERAGE}x</code>\n"
                f"<b>\U0001f6a9 SL:</b> <code>${sl:,.0f}</code> ({sl_pct:.1f}% | "
                f"R {r_perdida:.1f}%)\n"
                f"<b>\U0001f4c8 TP1:</b> <code>${tp1:,.0f}</code> "
                f"(R {r_ganancia:.1f}%)\n"
                + (f"<b>\U0001f4c8 TP2:</b> <code>${tp2:,.0f}</code>\n" if tp2 else "")
                + (f"\n🤖 <i>{limpiar_texto_telegram(ai_text)}</i>\n" if ai_text else "")
                + f"\n\u23f0 {snapshot.get('timestamp', '')}"
            )
            await self._send(
                _format_premium_message(
                    "\U0001f9e0 SENIAL DEL CEREBRO CUANTICO", body
                ),
                reply_markup=keyboard,
            )

    def _get_levels_block(self) -> str:
        """Generates a formatted string of technical levels for inline use."""
        s = self._state or {}
        price = s.get("price", 0)
        tech = s.get("technical_levels", {})
        if not tech or not tech.get("fib_retracement"):
            return ""
        try:
            from src.engine.technical_levels import format_levels_for_telegram
            # Generar el bloque original
            msg = format_levels_for_telegram(tech, price, settings.get_symbol())
            # Reemplazar el emoji grande para que se vea bien como anexo
            msg = msg.replace("\U0001f4cf NIVELES T\u00c9CNICOS", "<b>NIVELES T\u00c9CNICOS</b>")
            return msg.strip()
        except ImportError:
            return "<i>(Niveles t\u00e9cnicos no disponibles)</i>"

    async def _send_trap_change_alert(self, snapshot: dict, trap: str):
        prob_dir = snapshot.get('directional_probability', 50.0)
        p = snapshot.get('price', 0)
        bias = snapshot.get('market_bias', 'INCIERTO')
        delta = snapshot.get('delta', 0)
        cvd = snapshot.get('cvd', 0)
        vol = snapshot.get('volume', 0)
        avg_vol = snapshot.get('avg_volume', 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        emoji = "\U0001f7e2" if bias == 'ALZA' else "\U0001f7e3"

        body = (
            f"<b>TRAMPA:</b> <code>{trap}</code>\n"
            f"<b>PROB:</b> <code>{prob_dir:.0f}%</code>\n"
            f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n\n"
            f"<b>MERCADO:</b>\n"
            f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"  Vol: <code>{vol_r:.1f}x</code> | Bias: {emoji} {bias}\n\n"
            f"\u23f0 {snapshot.get('timestamp', '')}"
        )
        await self._send(_format_premium_message("\u26a1 CAMBIO DE TRAMPA DETECTADO", body))

    # ─── HTML formatter ────────────────────────────────────────────────────

    async def _format_signal_long(self, snapshot: dict) -> str:
        p = snapshot.get('price', 0)
        conf = snapshot.get('confidence', 0)
        delta = snapshot.get('delta', 0)
        cvd = snapshot.get('cvd', 0)
        ba = snapshot.get('ba_ratio', 1.0)
        vol = snapshot.get('volume', 0)
        avg_vol = snapshot.get('avg_volume', 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        return (
            "\U0001f7e2 <b>LONG</b>\n\n"
            f"Precio: <code>${p:,.0f}</code>\n"
            f"Confianza: <code>{conf:.0f}%</code>\n"
            f"Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"B/A: <code>{ba:.3f}x</code> | Vol: <code>{vol_r:.1f}x</code>"
        )

    async def _format_signal_short(self, snapshot: dict) -> str:
        p = snapshot.get('price', 0)
        conf = snapshot.get('confidence', 0)
        delta = snapshot.get('delta', 0)
        cvd = snapshot.get('cvd', 0)
        ba = snapshot.get('ba_ratio', 1.0)
        vol = snapshot.get('volume', 0)
        avg_vol = snapshot.get('avg_volume', 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        return (
            "\U0001f7e3 <b>SHORT</b>\n\n"
            f"Precio: <code>${p:,.0f}</code>\n"
            f"Confianza: <code>{conf:.0f}%</code>\n"
            f"Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"B/A: <code>{ba:.3f}x</code> | Vol: <code>{vol_r:.1f}x</code>"
        )

    # ─── Gemini chat ───────────────────────────────────────────────────────

    async def _chat_gemini_raw(self, prompt: str) -> str:
        if not self._gemini_enabled or not self._gemini_client:
            self._gemini_fallback_count += 1
            if self._gemini_fallback_count % 12 == 1:
                log.warning("Gemini client not available (count=%d)", self._gemini_fallback_count)
            return json.dumps({"decision": "NO_ENTRAR", "razon": "Gemini no disponible"})
        try:
            log.warning(
                "[FORENSIC] _chat_gemini_raw: prompt_len=%d type=%s",
                len(prompt), "brain_alert")
            from google.genai import types as genai_types
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=8192,
                    )
                ))
            # Extracción segura: unir todos los parts del candidato
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text
            text = texto_completo.strip()
            try:
                reason = resp.candidates[0].finish_reason
            except Exception:
                reason = None
            FINISH_REASON_NAMES = {0: "STOP", 1: "MAX_TOKENS", 2: "SAFETY",
                                   3: "RECITATION", 4: "OTHER"}
            reason_name = FINISH_REASON_NAMES.get(reason, str(reason))
            try:
                usage = resp.usage_metadata
                prompt_tokens = usage.prompt_token_count if usage else "?"
                output_tokens = usage.candidates_token_count if usage else "?"
            except Exception:
                prompt_tokens, output_tokens = "?", "?"
            log.warning(
                "[FORENSIC] _chat_gemini_raw: text_len=%d preview=%s "
                "finish_reason=%s(%s) prompt_tokens=%s output_tokens=%s",
                len(text), text[:120].replace('\n', ' | '), reason, reason_name,
                prompt_tokens, output_tokens)
            if reason is not None and reason != 0:
                log.warning(
                    "*** TRUNCATION: _chat_gemini_raw finish_reason=%s(%s) *** "
                    "text_len=%d output_tokens=%s",
                    reason, reason_name, len(text), output_tokens)
            return text
        except Exception as e:
            log.warning("Gemini raw inference error: %s", e)
            return json.dumps({"decision": "NO_ENTRAR", "razon": f"Error Gemini: {e}"})

    # ─── Chart generation ──────────────────────────────────────────────────

    async def _fetch_klines(self, interval: str = "1m", limit: int = 120) -> list:
        # Intento 1: usar el binance_client compartido (ahora async nativo)
        try:
            from src.engine.binance_client import binance_client as bc
            if bc is not None:
                klines = await bc.get_historical_klines(interval=interval, limit=limit)
                if klines:
                    return klines
        except Exception as e:
            log.warning("Fetch klines (shared) error (%s): %s", interval, e)

        # Intento 2: fallback directo a la API vía order_executor._client (sync → thread)
        try:
            executor = self._order_executor
            if executor is not None and executor._client is not None:
                loop = asyncio.get_event_loop()
                klines = await loop.run_in_executor(
                    None, lambda: executor._client.futures_klines(
                        symbol=settings.get_symbol(), interval=interval, limit=limit
                    ))
                if klines:
                    return klines
        except Exception as e:
            log.warning("Fetch klines (executor fallback) error (%s): %s", interval, e)

        # Intento 3: HTTP directo a Binance público (sin API key)
        try:
            url = ("https://fapi.binance.com/fapi/v1/klines?"
                   f"symbol={settings.get_symbol()}&interval={interval}&limit={limit}")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            log.warning("Fetch klines (HTTP fallback) error (%s): %s", interval, e)

        return []

    async def _fetch_mtf_klines(self) -> dict:
        """Fetch klines for all strategy timeframes. Returns {tf: klines_list}."""
        timeframes = [("1m", 120), ("5m", 100), ("15m", 100), ("1h", 100), ("4h", 100), ("1d", 100)]
        result = {}
        for interval, limit in timeframes:
            klines = await self._fetch_klines(interval=interval, limit=limit)
            if klines and len(klines) >= 30:
                result[interval] = klines
                log.info("MTF klines fetched: %s (%d candles)", interval, len(klines))
            else:
                log.warning("MTF klines empty for %s", interval)
        return result

    async def _generate_brain_chart(self, klines: list, entry: float,
                                     sl: float, tp: float) -> Optional[bytes]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._generate_brain_chart_sync, klines, entry, sl, tp)

    def _generate_brain_chart_sync(self, klines: list, entry: float,
                                    sl: float, tp: float) -> Optional[bytes]:
        """Generate chart with entry / SL / TP horizontal lines."""
        UP_COLOR = "#00FF88"
        DOWN_COLOR = "#BB00FF"
        if not klines or len(klines) < 30:
            return None
        try:
            dates = [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc) for k in klines]
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
            )
            fig.patch.set_facecolor("#0B0B0B")
            ax1.set_facecolor("#0B0B0B")
            ax2.set_facecolor("#0B0B0B")

            for i in range(len(klines)):
                color = UP_COLOR if closes[i] >= float(klines[i][1]) else DOWN_COLOR
                ax1.plot([dates[i], dates[i]], [lows[i], highs[i]],
                         color=color, linewidth=0.8)
                ax1.plot([dates[i], dates[i]],
                         [min(closes[i], float(klines[i][1])),
                          max(closes[i], float(klines[i][1]))],
                         color=color, linewidth=3)

            ema9 = self._compute_ema(closes, 9)
            ema21 = self._compute_ema(closes, 21)
            if ema9:
                ax1.plot(dates[-len(ema9):], ema9, color="#FFD700",
                         linewidth=1, alpha=0.9, label="EMA 9")
            if ema21:
                ax1.plot(dates[-len(ema21):], ema21, color="#FF69B4",
                         linewidth=1, alpha=0.9, label="EMA 21")

            last_x = dates[-1]
            y_min, y_max = ax1.get_ylim()

            def draw_hline(price, color, style, label):
                ax1.axhline(y=price, color=color, linestyle=style,
                            linewidth=1.5, alpha=0.9)
                ax1.annotate(label, xy=(last_x, price),
                             xytext=(10, 0), textcoords="offset points",
                             color=color, fontsize=9,
                             bbox=dict(boxstyle="round,pad=0.2",
                                       facecolor="#0B0B0B", edgecolor=color,
                                       alpha=0.8))

            draw_hline(entry, "#00FF88", "--", f"ENTRY ${entry:,.0f}")
            draw_hline(sl, "#FF3366", ":", f"SL ${sl:,.0f}")
            draw_hline(tp, "#00CCFF", ":", f"TP ${tp:,.0f}")

            vol_colors = [
                UP_COLOR if closes[i] >= float(klines[i][1]) else DOWN_COLOR
                for i in range(len(klines))
            ]
            ax2.bar(dates, volumes, color=vol_colors, width=0.001, alpha=0.7)

            ax1.legend(loc="upper left", facecolor="#1A1A1A", edgecolor="none",
                       labelcolor="white", fontsize=8)
            for spine in ax1.spines.values():
                spine.set_color("#333333")
            for spine in ax2.spines.values():
                spine.set_color("#333333")
            ax1.tick_params(colors="#888888", labelsize=8)
            ax2.tick_params(colors="#888888", labelsize=8)
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=15))
            ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

            ax1.set_title(f"{settings.get_symbol()} — Señal del Cerebro Cuántico",
                          color="white", fontsize=12, pad=10)
            ax1.grid(True, alpha=0.15, color="#444444")
            ax2.grid(True, alpha=0.15, color="#444444")

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                        facecolor="#0B0B0B", edgecolor="none")
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()
        except Exception as e:
            log.warning("Brain chart generation error: %s", e)
            return None

    async def _generate_mtf_chart(self, klines: list, timeframe: str,
                                   trend: str = "NEUTRAL") -> Optional[bytes]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._generate_mtf_chart_sync, klines, timeframe, trend)

    def _generate_mtf_chart_sync(self, klines: list, timeframe: str,
                                  trend: str = "NEUTRAL") -> Optional[bytes]:
        """Generate chart in brain-alert style for a specific timeframe."""
        UP_COLOR = "#00FF88"
        DOWN_COLOR = "#BB00FF"
        if not klines or len(klines) < 30:
            return None
        try:
            dates = [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc) for k in klines]
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            opens = [float(k[1]) for k in klines]

            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
            )
            fig.patch.set_facecolor("#0B0B0B")
            ax1.set_facecolor("#0B0B0B")
            ax2.set_facecolor("#0B0B0B")

            for i in range(len(klines)):
                color = UP_COLOR if closes[i] >= opens[i] else DOWN_COLOR
                ax1.plot([dates[i], dates[i]], [lows[i], highs[i]],
                         color=color, linewidth=0.8)
                ax1.plot([dates[i], dates[i]],
                         [min(closes[i], opens[i]),
                          max(closes[i], opens[i])],
                         color=color, linewidth=3)

            ema9 = self._compute_ema(closes, 9)
            ema21 = self._compute_ema(closes, 21)
            if ema9:
                ax1.plot(dates[-len(ema9):], ema9, color="#FFD700",
                         linewidth=1, alpha=0.9, label="EMA 9")
            if ema21:
                ax1.plot(dates[-len(ema21):], ema21, color="#FF69B4",
                         linewidth=1, alpha=0.9, label="EMA 21")

            vol_colors = [
                UP_COLOR if closes[i] >= opens[i] else DOWN_COLOR
                for i in range(len(klines))
            ]
            ax2.bar(dates, volumes, color=vol_colors, width=0.001, alpha=0.7)

            ax1.legend(loc="upper left", facecolor="#1A1A1A", edgecolor="none",
                       labelcolor="white", fontsize=8)
            for spine in ax1.spines.values():
                spine.set_color("#333333")
            for spine in ax2.spines.values():
                spine.set_color("#333333")
            ax1.tick_params(colors="#888888", labelsize=8)
            ax2.tick_params(colors="#888888", labelsize=8)

            if timeframe in ("5m", "15m"):
                ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
            elif timeframe == "1h":
                ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
                ax1.xaxis.set_major_locator(mdates.HourLocator(interval=2))
            elif timeframe == "4h":
                ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
                ax1.xaxis.set_major_locator(mdates.HourLocator(interval=8))
            elif timeframe == "1d":
                ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
                ax1.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax2.xaxis.set_major_formatter(ax1.xaxis.get_major_formatter())
            ax2.xaxis.set_major_locator(ax1.xaxis.get_major_locator())

            trend_emoji = {"ALCISTA": "\U0001f7e2", "BAJISTA": "\U0001f534",
                           "NEUTRAL": "\u26aa", "WAIT": "\u26aa"}
            emoji = trend_emoji.get(trend, "\u26aa")
            ax1.set_title(f"{settings.get_symbol()} \u2014 {timeframe} {emoji} {trend}",
                          color="white", fontsize=12, pad=10)
            ax1.grid(True, alpha=0.15, color="#444444")
            ax2.grid(True, alpha=0.15, color="#444444")

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                        facecolor="#0B0B0B", edgecolor="none")
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()
        except Exception as e:
            log.warning("MTF chart generation error (%s): %s", timeframe, e)
            return None

    @staticmethod
    def _compute_ema(data: list, period: int) -> list:
        if len(data) < period:
            return []
        multiplier = 2 / (period + 1)
        ema = [data[0]]
        for i in range(1, len(data)):
            ema.append((data[i] - ema[-1]) * multiplier + ema[-1])
        return ema

    # ─── Interactive Gemini command ────────────────────────────────────────

    async def _handle_gemini_chat(self, message: dict):
        user_text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id", 0)
        user_name = message.get("from", {}).get("first_name", "Trader")

        if not self._gemini_enabled:
            await self._send(
                "El asistente AI no esta disponible. Configura GEMINI_API_KEY.",
                chat_id=chat_id)
            return

        await self._send_typing(chat_id)

        s = self._state or {}
        snapshot_json = json.dumps({
            "price": s.get("price", 0), "delta": s.get("delta", 0),
            "cvd": s.get("cvd", 0), "volume": s.get("volume", 0),
            "rsi": s.get("rsi", 50), "signal": s.get("signal_text", "WAIT"),
            "confidence": s.get("confidence", 0),
            "trend_5m": s.get("trend_5m", "WAIT"),
            "trend_1h": s.get("trend_1h", "WAIT"),
            "cumulative_delta": s.get("cumulative_delta", 0),
            "ba_ratio": s.get("ba_ratio", 1.0),
            "tick_speed": s.get("tick_speed", 0),
            "trap_status": s.get("trap_status", "N/A"),
            "timestamp": s.get("timestamp", ""),
        }, indent=2)

        prompt = (
            f"Eres un asistente experto en trading de futuros de Bitcoin.\n\n"
            f"Datos del mercado en vivo de {settings.get_symbol()}:\n"
            f"```json\n{snapshot_json}\n```\n\n"
            f"Pregunta del usuario ({user_name}): {user_text}\n\n"
            f"Responde de forma clara y concisa (max 350 tokens).\n"
            f"FORMATO OBLIGATORIO — Usa etiquetas HTML para dar estructura:\n"
            f"  - <b>texto</b> para títulos y palabras clave\n"
            f"  - <code>numero</code> para valores numéricos\n"
            f"  - Emojis profesionales: 📊 📈 📉 🟢 🔴 🟣 🎯 ⚡ 💡 📰 🧠\n"
            f"  - Separa secciones con \\n y usa viñetas con —\n"
            f"Ejemplo:\n"
            f"  <b>📊 PRECIO:</b> <code>$73,600</code>\n"
            f"  <b>📈 SEÑAL:</b> 🟡 NEUTRAL\n\n"
            f"NO des consejos financieros, solo analisis."
        )
        try:
            from google.genai import types as genai_types
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(max_output_tokens=8192)
                ))
            # Extracción segura: unir todos los parts del candidato
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text
            reply = texto_completo.strip()
            if not reply:
                try:
                    reason = resp.candidates[0].finish_reason
                    log.warning("Gemini chat returned empty — finish_reason=%s", reason)
                except Exception:
                    pass
        except Exception as e:
            reply = f"Error al consultar Gemini: {e}"
            log.warning("Gemini chat error: %s", e)

        clean_reply = _clean_ai_text(reply)
        await self._send(f"<b>Gemini AI ({user_name}):</b>\n\n{clean_reply}",
                         chat_id=chat_id, reply_markup=_main_keyboard())

    async def _get_trade_analysis(self, direction: str, entry_price: float,
                                    sl_price: float, tp_price: float,
                                    capital: float, leverage: int = 0,
                                    qty: float = 0) -> Optional[str]:
        if not self._gemini_enabled:
            return None
        await self._send_typing()
        s = self._state or {}
        lev = leverage or int(getattr(settings, 'LEVERAGE', 100))
        qty_str = f"{qty:.4f} BTC" if qty else f"${capital:,.0f} @ {lev}x"
        direction_upper = direction.upper()
        is_long = direction_upper in ("BUY", "LONG", "ALZA")
        sym = settings.get_symbol()
        side_label = f"LONG {sym}" if is_long else f"SHORT {sym}"
        direction_emoji = "\U0001f7e2" if is_long else "\U0001f534"
        target_text = "subida" if is_long else "caída"
        sl_valid = sl_price < entry_price if is_long else sl_price > entry_price
        snap = {
            "direction": direction_upper,
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "capital": capital,
            "leverage": lev,
            "qty": qty_str,
            "price": s.get("price", 0),
            "delta": s.get("delta", 0),
            "cvd": s.get("cvd", 0),
            "volume": s.get("volume", 0),
            "avg_volume": s.get("avg_volume", 0),
            "rsi": s.get("rsi", 50),
            "trend_5m": s.get("trend_5m", "NEUTRAL"),
            "vol_ratio": s.get("volume", 0) / max(s.get("avg_volume", 0), 0.001),
        }
        prompt = (
            f"Eres un gestor de riesgo evaluando una orden {side_label}.\n\n"
            f"* Tipo: {side_label}\n"
            f"* Precio de entrada: <code>${entry_price:,.0f}</code>\n"
            f"* Stop Loss: <code>${sl_price:,.0f}</code>\n"
            f"* Take Profit: <code>${tp_price:,.0f}</code>\n"
            f"* Capital: <code>${capital:,.1f}</code> | Apalancamiento: <code>{lev}x</code>\n"
            f"* Tamaño: <code>{qty_str}</code>\n"
            f"{'✅ SL es menor que entrada — lógico para LONG' if is_long and sl_valid else ''}"
            f"{'✅ SL es mayor que entrada — lógico para SHORT' if not is_long and sl_valid else ''}"
            f"{'⚠️ SL inválido — debería ser menor que entrada en LONG' if is_long and not sl_valid else ''}"
            f"{'⚠️ SL inválido — debería ser mayor que entrada en SHORT' if not is_long and not sl_valid else ''}\n\n"
            f"Datos de mercado:\n"
            f"Precio actual: ${entry_price:,.0f} | Delta: {snap['delta']:+.0f} | CVD: {snap['cvd']:.1f}\n"
            f"Volumen: {snap['volume']:.1f} | Ratio Vol: {snap['vol_ratio']:.1f}x | RSI: {snap['rsi']:.1f}\n"
            f"Tendencia 5m: {snap['trend_5m']}\n\n"
            f"REGLAS:\n"
            f"  - <b>titulo</b> y <code>valor</code>\n"
            f"  - Emojis: {direction_emoji} 💰 🎯 ⚠️\n"
            f"  - Máximo 3 líneas\n"
            f"  - Evalúa si el bracket SL/TP es coherente con la volatilidad actual\n"
            f"  - Menciona si el mercado soporta una {target_text}\n"
            f"  - NO des consejos financieros\n\n"
            f"Análisis de riesgo:"
        )
        try:
            from google.genai import types as genai_types
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(max_output_tokens=8192)
                ))
            # Extracción segura: unir todos los parts del candidato
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text
            text = texto_completo.strip()
            if not text:
                try:
                    reason = resp.candidates[0].finish_reason
                    log.warning("Trade analysis empty — finish_reason=%s", reason)
                except Exception:
                    pass
            return text
        except Exception as e:
            log.warning("Trade analysis error: %s", e)
            return None

    async def _get_ai_analysis(self, analysis_type: str, chat_id: int = None) -> Optional[str]:
        if not self._gemini_enabled:
            return None
        await self._send_typing(chat_id)
        s = self._state or {}

        precio = s.get("price", 0)
        delta = s.get("delta", 0)
        cvd = s.get("cvd", 0)
        volumen = s.get("volume", 0)
        rsi = s.get("rsi", 50)
        senial = s.get("signal_text", "WAIT")
        confianza = s.get("confidence", 0)
        brain_dir = s.get("brain_direction", "N/A")
        brain_conf = s.get("brain_confidence_pct", 0)
        trend_5m = s.get("trend_5m", "NEUTRAL")

        prompt_limpio = (
            "Actua como un analista experto en trading cuantitativo. "
            f"Analiza el par {settings.get_symbol()}. "
            f"Precio actual: {str(float(precio))}. "
            f"Delta: {str(float(delta))}, CVD: {str(float(cvd))}, "
            f"Volumen: {str(float(volumen))}. "
            f"RSI: {str(float(rsi))}. "
            f"Senial: {senial} ({str(float(confianza))}%). "
            f"Tendencia 5m: {trend_5m}. "
            f"Cerebro: {brain_dir} ({str(float(brain_conf))}%). "
            "Genera un analisis de 3 lineas con formato HTML para Telegram. "
            "Usa <b>texto</b> para titulos y palabras clave. "
            "Usa <code>numero</code> para precios, deltas y valores numericos. "
            "Incluye emojis relevantes como 📊 📈 📉 🟢 🔴 🟣 🎯 ⚡ 💡. "
            "Separa las lineas con saltos de linea \\n. "
            "NO uses asteriscos (*) ni guiones bajos (_). "
            "Usa unicamente tags HTML validos para Telegram."
        )

        log.warning(
            "[FORENSIC] POINT1 _get_ai_analysis type=%s prompt_len=%d prompt=%r",
            analysis_type, len(prompt_limpio), prompt_limpio)
        try:
            from google.genai import types as genai_types
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt_limpio,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=8192,
                    )
                ))
            # Extracción segura: unir todos los parts del candidato
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text
            text = texto_completo.strip()
            try:
                reason = resp.candidates[0].finish_reason
            except Exception:
                reason = None
            FINISH_REASON_NAMES = {0: "STOP", 1: "MAX_TOKENS", 2: "SAFETY",
                                   3: "RECITATION", 4: "OTHER"}
            reason_name = FINISH_REASON_NAMES.get(reason, str(reason))
            try:
                usage = resp.usage_metadata
                prompt_tokens = usage.prompt_token_count if usage else "?"
                output_tokens = usage.candidates_token_count if usage else "?"
            except Exception:
                prompt_tokens, output_tokens = "?", "?"
            log.warning(
                "[FORENSIC] POINT2 _get_ai_analysis type=%s text_len=%d "
                "first200=%r finish_reason=%s(%s) "
                "prompt_tokens=%s output_tokens=%s",
                analysis_type, len(text), text[:200],
                reason, reason_name, prompt_tokens, output_tokens)
            if reason is not None and reason != 0:
                log.warning(
                    "*** TRUNCATION: finish_reason=%s(%s) *** "
                    "text_len=%d output_tokens=%s",
                    reason, reason_name, len(text), output_tokens)
            return text
        except Exception as e:
            log.warning("AI analysis error (%s): %s", analysis_type, e)
            return None

    async def _get_mtf_analysis(self, chat_id: int = None) -> Optional[str]:
        """Generate multi-timeframe analysis with strategy-based suggestion."""
        if not self._gemini_enabled:
            return None
        await self._send_typing(chat_id)
        s = self._state or {}

        precio = s.get("price", 0)
        delta = s.get("delta", 0)
        cvd = s.get("cvd", 0)
        volumen = s.get("volume", 0)
        rsi = s.get("rsi", 50)

        trends = {
            "5m": s.get("trend_5m", "NEUTRAL"),
            "15m": s.get("trend_15m", "NEUTRAL"),
            "1h": s.get("trend_1h", "NEUTRAL"),
            "4h": s.get("trend_4h", "NEUTRAL"),
            "1d": s.get("trend_1d", "NEUTRAL"),
        }
        trend_lines = "\n".join(f"  - {tf}: {t}" for tf, t in trends.items())

        prompt = (
            f"Actua como analista cuantitativo experto en estrategia multi-timeframe. "
            f"Analiza {settings.get_symbol()}.\n\n"
            f"Datos del mercado:\n"
            f"  Precio: ${float(precio):,.0f}\n"
            f"  Delta: {delta:+.0f} | CVD: {cvd:.1f}\n"
            f"  Volumen: {float(volumen):,.0f} | RSI: {rsi:.1f}\n\n"
            f"Tendencias por temporalidad:\n{trend_lines}\n\n"
            f"Genera un analisis de EXACTAMENTE 5 parrafos cortos CON FORMATO HTML:\n"
            f"1. <b>Panorama MTF:</b> confluencia o divergencia entre temporalidades\n"
            f"2. <b>Accion del precio:</b> comportamiento actual y momentum\n"
            f"3. <b>Soportes y Resistencias:</b> niveles clave en las TFs principales\n"
            f"4. <b>Estrategia:</b> que hacer segun la confluencia de tendencias\n"
            f"5. <b>Recomendacion:</b> COMPRAR/VENDER/ESPERAR con justificacion\n\n"
            f"Usa <b>texto</b> para titulos. Usa <code>numero</code> para valores.\n"
            f"Incluye emojis: \U0001f4c8 \U0001f4c9 \U0001f7e2 \U0001f534 \U0001f7e1 \U0001f3af \u26a1 \U0001f4a1 \U0001f6e1\ufe0f\n"
            f"Separa cada parrafo con \\n\\n. NO uses asteriscos ni guiones bajos."
        )

        try:
            from google.genai import types as genai_types
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=8192,
                    )
                ))
            try:
                texto_completo = "".join(
                    part.text for part in resp.candidates[0].content.parts
                )
            except Exception:
                texto_completo = resp.text
            return texto_completo.strip()
        except Exception as e:
            log.warning("MTF analysis error: %s", e)
            return None

    # ─── Safe AI Insight sender ────────────────────────────────────────────

    async def _send_html(self, text: str, chat_id: int = None,
                          reply_markup: Optional[dict] = None) -> bool:
        text = format_for_telegram(text, context="message")
        return await self._send(text, chat_id, reply_markup)

    async def _send_ai_insight(self, ai_analysis: str, chat_id: int = None,
                                reply_markup: Optional[dict] = None) -> None:
        if not ai_analysis or not ai_analysis.strip():
            return

        texto_limpio = limpiar_texto_telegram(ai_analysis)
        cid = chat_id or self._chat_id
        label = "🧠 <b>AI Insight:</b>\n\n"
        full_msg = f"{label}{texto_limpio}"

        msg_clean = re.sub(r'<[^>]+>', '', full_msg[:200]).replace('\n', ' | ')
        log.warning(
            "[FORENSIC] POINT3 _send_ai_insight msg_len=%d preview=%s",
            len(full_msg), msg_clean[:120])

        # Intento 1: HTML con parse_mode
        try:
            ok = await self._send_html(full_msg, chat_id=cid, reply_markup=reply_markup)
            if ok:
                return
        except Exception as e:
            log.warning("[AI Insight] HTML send failed: %s", e)

        # Intento 2: fallback extremo — eliminar cualquier resto de HTML y reenviar
        log.warning("[AI Insight] HTML send failed — retrying with stripped text")
        try:
            stripped = re.sub(r'<[^>]+>', '', full_msg)
            await self._send_text_plain(stripped, chat_id=cid, reply_markup=reply_markup)
        except Exception as e:
            log.error("[AI Insight] Fallback also failed: %s", e)

    async def _send_text_plain(self, text: str, chat_id: int = None,
                                reply_markup: Optional[dict] = None) -> bool:
        cid = chat_id or self._chat_id
        if not cid:
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": cid, "text": text,
                    "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram _send_text_plain exception: %s", e)
            return False
        finally:
            if self._session is None:
                await session.close()

    # ─── Command handlers ──────────────────────────────────────────────────

    async def _handle_command(self, message: dict):
        text = message.get("text", "").strip().lower()
        chat_id = message.get("chat", {}).get("id", 0)
        user = message.get("from", {})

        if text == "/start":
            await self._send(
                f"\U0001f916 <b>BB-450 Trading Bot</b>\n\n"
                f"Bienvenido! Usa los botones o /help para ver todos los comandos.",
                chat_id=chat_id,
                reply_markup=_main_keyboard())
            return

        if text == "/help":
            await self._send(
                "\U0001f4d6 <b>COMANDOS DISPONIBLES</b>\n\n"
                "\U0001f4ca /status — Estado + niveles técnicos\n"
                "\U0001f4c8 /signal — Última señal de estrategia\n"
                "\U0001f9e0 /brain — Cerebro cuántico\n"
                "\U0001f4c9 /chart — Gráfico técnico\n"
                "\u2699\ufe0f /config — Configuración activa\n"
                "\U0001f4b0 /balance — Balance de cuenta\n"
                "\U0001f4cd /positions — Posición abierta\n"
                "\U0001f504 /symbol — Cambiar símbolo activo\n"
                "\u26a1 OPERAR BB-450 — Sub-menú de operación\n\n"
                "<b>Atajos rápidos:</b>\n"
                "  • Envía un <code>número</code> (ej: <code>2.5</code>) para fijar el lote global en USD\n"
                "  • Envía cualquier texto para consultar Gemini AI\n\n"
                "<i>Usa los botones de abajo para acceder rápido</i>",
                chat_id=chat_id,
                reply_markup=_main_keyboard())
            return

        if text.startswith("/symbol"):
            await self._handle_symbol_command(text, chat_id)
            return

        if text.startswith("/gemini"):
            user_text = text[len("/gemini"):].strip()
            if not user_text:
                await self._send(
                    "Debes incluir una pregunta. Ej: <code>/gemini que opinas del mercado?</code>",
                    chat_id=chat_id, reply_markup=_main_keyboard())
                return
            message["text"] = user_text
            await self._handle_gemini_chat(message)
            return

        if text == "/status":
            s = self._state or {}
            p = s.get("price", 0)
            chg = s.get("change_pct", 0)
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            delta = s.get("delta", 0)
            cvd = s.get("cvd", 0)
            vol = s.get("volume", 0)
            avg_vol = s.get("avg_volume", 0)
            vol_r = vol / avg_vol if avg_vol > 0 else 0
            rsi = s.get("rsi", 50)
            ba = s.get("ba_ratio", 1.0)
            brain_dir = s.get("brain_direction", "N/A")
            brain_conf = s.get("brain_confidence_pct", 0)
            trap = s.get("trap_status", "N/A")
            vwap = s.get("vwap", 0)
            vwap_dist = s.get("price_vwap_dist", 0)
            depth_imb = s.get("depth_imb_pct", 0)
            conf_score = s.get("confluence_score", 0)
            macro = s.get("global_macro", "NEUTRAL")

            def _te(dir_val):
                if dir_val in ("ALCISTA", "UP", "BULLISH"):
                    return "\U0001f7e2"
                if dir_val in ("BAJISTA", "DOWN", "BEARISH"):
                    return "\U0001f7e3"
                return "\U0001f7e1"

            sig_emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"
            if sig == "WAIT":
                sig_emoji = "\U0001f7e1"

            t5m  = _te(s.get("trend_5m", "WAIT"))
            t15m = _te(s.get("trend_15m", "WAIT"))
            t1h  = _te(s.get("trend_1h", "WAIT"))
            t4h  = _te(s.get("trend_4h", "WAIT"))
            t1d  = _te(s.get("trend_1d", "WAIT"))

            body = (
                f"<b>{settings.get_symbol()}</b> | <code>${p:,.0f}</code> "
                f"<code>({chg:+.2f}%)</code>\n\n"
                f"<b>SEÑAL:</b> {sig_emoji} {sig} <code>({conf:.0f}%)</code>\n\n"
                f"\U0001f4c8 <b>TENDENCIAS MTF</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"  5m  {t5m}  {s.get('trend_5m', 'WAIT')}\n"
                f"  15m {t15m} {s.get('trend_15m', 'WAIT')}\n"
                f"  1h  {t1h}  {s.get('trend_1h', 'WAIT')}\n"
                f"  4h  {t4h}  {s.get('trend_4h', 'WAIT')}\n"
                f"  1d  {t1d}  {s.get('trend_1d', 'WAIT')}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"  \U0001f3af Confluencia: <code>{conf_score:.0f}%</code> "
                f"| Macro: <code>{macro}</code>\n\n"
                f"\U0001f4ca <b>MERCADO:</b>\n"
                f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
                f"  B/A: <code>{ba:.3f}x</code> | Vol: <code>{vol_r:.1f}x</code>\n"
                f"  RSI: <code>{rsi:.1f}</code> | Depth: <code>{depth_imb:+.1f}%</code>\n"
                f"  VWAP: <code>${vwap:,.0f}</code> <code>({vwap_dist:+.2f}%)</code>\n\n"
                f"\U0001f9e0 <b>CEREBRO:</b> {brain_dir} <code>({brain_conf:.0f}%)</code>\n"
                f"  Trampa: <code>{trap}</code>\n\n"
                f"\u23f0 {s.get('timestamp', 'No data')}"
            )

            levels_block = self._get_levels_block()
            if levels_block:
                body += f"\n\n{levels_block}"

            await self._send(
                _format_premium_message("\U0001f4ca ESTADO DEL MERCADO",
                                         body + "\n\n_Usa /status para actualizar_"),
                chat_id=chat_id,
                reply_markup=_keyboard(
                    _row(_btn("\U0001f504 Refresh", "refresh_status")),
                ))
            ai = await self._get_ai_analysis("market", chat_id)
            if ai:
                await self._send_ai_insight(ai, chat_id=chat_id,
                                             reply_markup=_main_keyboard())
            return

        if text == "/niveles":
            # Comando explícito aún funciona, pero los niveles también
            # se muestran automáticamente en /status.
            levels_msg = self._get_levels_block()
            if levels_msg:
                await self._send(
                    f"<b>\U0001f4cf NIVELES TÉCNICOS</b>\n\n{levels_msg}",
                    chat_id=chat_id, reply_markup=_main_keyboard())
            else:
                await self._send(
                    "\u23f3 Calculando niveles técnicos... intenta de nuevo en 30 segundos.",
                    chat_id=chat_id)
            return

        if text == "/config" or text == "/settings":
            body = (
                f"<b>CONFIGURACION ACTIVA</b>\n\n"
                f"Símbolo: <code>{settings.get_symbol()}</code>\n"
                f"Apalancamiento: <code>{settings.LEVERAGE}x</code>\n"
                f"Capital: <code>${self._user_config.get('capital', 100):.1f}</code>\n"
                f"Risk por trade: <code>{settings.RISK_PER_TRADE:.1f}%</code>\n\n"
                f"<b>ALERTAS:</b>\n"
                f"  Crash/Pump: {'\u2705' if self._user_config.get('crash', True) else '\u274c'}\n"
                f"  Volumen: {'\u2705' if self._user_config.get('volume', True) else '\u274c'}\n"
                f"  Ballena: {'\u2705' if self._user_config.get('whale', True) else '\u274c'}\n"
                f"  Radar: {'\u2705' if self._user_config.get('radar', True) else '\u274c'}\n"
                f"  Buy/Sell: {'\u2705' if self._user_config.get('buy_sell', True) else '\u274c'}\n"
                f"  AI Trade: {'\u2705' if self._user_config.get('ai_trade', True) else '\u274c'}\n\n"
                f"Gemini AI: {'\u2705' if self._gemini_enabled else '\u274c'}"
            )
            await self._send(_format_premium_message("\u2699\ufe0f CONFIGURACION", body),
                             chat_id=chat_id, reply_markup=_main_keyboard())
            return

        if text == "/signal":
            s = self._state or {}
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            if sig in ("LONG", "SHORT"):
                await self._send_signal_alert(s)
                ai = await self._get_ai_analysis("signal", chat_id)
                if ai:
                    await self._send_ai_insight(ai, chat_id=chat_id,
                                                reply_markup=_main_keyboard())
            else:
                await self._send(f"\u23f3 No hay señal activa. Esperando...",
                                 chat_id=chat_id, reply_markup=_main_keyboard())
            return

        if text == "/brain":
            s = self._state or {}
            bdir = s.get("brain_direction", "N/A")
            bconf = s.get("brain_confidence_pct", 0)
            if bdir in ("ALZA", "BAJA"):
                await self._send_brain_alert(s)
                ai = await self._get_ai_analysis("brain", chat_id)
                if ai:
                    await self._send_ai_insight(ai, chat_id=chat_id,
                                                reply_markup=_main_keyboard())
            else:
                await self._send(f"\U0001f9e0 Cerebro cuántico: {bdir} ({bconf:.0f}%)",
                                 chat_id=chat_id, reply_markup=_main_keyboard())
            return

        if text == "/chart":
            await self._send_typing(chat_id)
            s = self._state or {}
            mtf_klines = await self._fetch_mtf_klines()
            if mtf_klines:
                timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
                for tf in timeframes:
                    klines = mtf_klines.get(tf)
                    if not klines:
                        continue
                    trend = s.get(f"trend_{tf}", "NEUTRAL")
                    png = await self._generate_mtf_chart(klines, tf, trend)
                    if png:
                        caption = (
                            f"<b>{settings.get_symbol()}</b> \u2014 <b>{tf}</b> | "
                            f"Tendencia: {trend}"
                        )
                        await self._send_photo(png, caption=caption,
                                               chat_id=chat_id)
                ai = await self._get_mtf_analysis(chat_id)
                if ai:
                    await self._send_ai_insight(ai, chat_id=chat_id,
                                                reply_markup=_main_keyboard())
                else:
                    await self._send("\U0001f4c8 Graficos generados. Activa Gemini AI para analisis.",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
                return
            await self._send("No se pudieron generar los graficos multi-timeframe.",
                             chat_id=chat_id, reply_markup=_main_keyboard())
            return

        # Trading commands — routed to _execute_trade
        if text.startswith(("/buy", "/sell", "/close_all", "/balance", "/positions")):
            await self._execute_trade(message)
            return

        await self._send(f"\u2753 Comando no reconocido: {text}\nUsa /help para ver los comandos disponibles.",
                         chat_id=chat_id, reply_markup=_main_keyboard())

    async def _execute_trade(self, message: dict):
        text = message.get("text", "").strip().lower()
        chat_id = message.get("chat", {}).get("id", 0)
        executor = self._order_executor

        if not executor:
            await self._send("❌ OrderExecutor no disponible", chat_id=chat_id)
            return

        if text.startswith("/buy"):
            parts = text.split()
            amount = float(parts[1]) if len(parts) > 1 else self._user_config.get("capital", 100)
            # Get current price from market state
            current_price = self._state.get("price", 0)
            if current_price <= 0:
                await self._send("❌ No hay precio de mercado disponible", chat_id=chat_id)
                return
            # Use bracket from pipeline if available (FIX B)
            bracket = (self._state or {}).get("risk_bracket", {})
            pipeline_dir = (self._state or {}).get("direction", "")
            bracket_ok = pipeline_dir in ("LONG", "ALZA")
            if bracket_ok and bracket.get("sl", 0) > 0 and bracket.get("tp1", 0) > 0:
                sl = bracket["sl"]
                tp = bracket["tp1"]
            else:
                sl = current_price * 0.985
                tp = current_price * 1.02
            try:
                loop = asyncio.get_event_loop()
                queued = await loop.run_in_executor(
                    None, lambda: executor.execute_trade_signal(
                        direction="ALZA",
                        entry_price=current_price,
                        sl_price=sl,
                        tp_price=tp,
                        capital=amount,
                    ))
                if queued:
                    leverage = int(getattr(settings, 'LEVERAGE', 100))
                    raw_qty = amount * leverage / current_price
                    await self._send(
                        f"✅ LONG encolado @ ${current_price:,.0f}\n"
                        f"Capital: ${amount:.1f} | SL: ${sl:,.0f} | TP: ${tp:,.0f}",
                        chat_id=chat_id, reply_markup=_main_keyboard())
                    ai = await self._get_trade_analysis(
                        "LONG", current_price, sl, tp, amount, leverage, raw_qty)
                    if ai:
                        await self._send_ai_insight(ai, chat_id=chat_id,
                                                    reply_markup=_main_keyboard())
                else:
                    reason = getattr(executor, '_last_reject_reason', '')
                    if "posici\u00f3n existente" in reason.lower():
                        await self._send_position_with_close(executor, chat_id)
                    else:
                        msg = "\u274c Orden rechazada por filtros de seguridad"
                        if reason:
                            msg += f"\n   Causa: {reason}"
                        await self._send(msg, chat_id=chat_id)
            except Exception as e:
                await self._send(f"❌ Error: {e}", chat_id=chat_id)
            return

        if text.startswith("/sell"):
            parts = text.split()
            amount = float(parts[1]) if len(parts) > 1 else self._user_config.get("capital", 100)
            current_price = self._state.get("price", 0)
            if current_price <= 0:
                await self._send("❌ No hay precio de mercado disponible", chat_id=chat_id)
                return
            # Use bracket from pipeline if available (FIX B)
            bracket = (self._state or {}).get("risk_bracket", {})
            pipeline_dir = (self._state or {}).get("direction", "")
            bracket_ok = pipeline_dir in ("SHORT", "BAJA")
            if bracket_ok and bracket.get("sl", 0) > 0 and bracket.get("tp1", 0) > 0:
                sl = bracket["sl"]
                tp = bracket["tp1"]
            else:
                sl = current_price * 1.015
                tp = current_price * 0.985
            try:
                loop = asyncio.get_event_loop()
                queued = await loop.run_in_executor(
                    None, lambda: executor.execute_trade_signal(
                        direction="BAJA",
                        entry_price=current_price,
                        sl_price=sl,
                        tp_price=tp,
                        capital=amount,
                    ))
                if queued:
                    leverage = int(getattr(settings, 'LEVERAGE', 100))
                    raw_qty = amount * leverage / current_price
                    await self._send(
                        f"✅ SHORT encolado @ ${current_price:,.0f}\n"
                        f"Capital: ${amount:.1f} | SL: ${sl:,.0f} | TP: ${tp:,.0f}",
                        chat_id=chat_id, reply_markup=_main_keyboard())
                    ai = await self._get_trade_analysis(
                        "SHORT", current_price, sl, tp, amount, leverage, raw_qty)
                    if ai:
                        await self._send_ai_insight(ai, chat_id=chat_id,
                                                    reply_markup=_main_keyboard())
                else:
                    reason = getattr(executor, '_last_reject_reason', '')
                    if "posici\u00f3n existente" in reason.lower():
                        await self._send_position_with_close(executor, chat_id)
                    else:
                        msg = "\u274c Orden rechazada por filtros de seguridad"
                        if reason:
                            msg += f"\n   Causa: {reason}"
                        await self._send(msg, chat_id=chat_id)
            except Exception as e:
                await self._send(f"❌ Error: {e}", chat_id=chat_id)
            return

        if text == "/close_all":
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, executor.close_all_positions)
                if result.get("success"):
                    await self._send("✅ Todas las posiciones cerradas",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
                    await self._send("📊 Riesgo mitigado — posiciones liquidadas manualmente.",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
                else:
                    await self._send(f"❌ {result.get('message', 'Error')}",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
            except Exception as e:
                await self._send(f"❌ Error: {e}", chat_id=chat_id)
            return

        if text == "/balance":
            try:
                loop = asyncio.get_event_loop()
                bal = await loop.run_in_executor(
                    None, executor.get_balance)
                if bal.get("success"):
                    body = (
                        f"Balance: <code>${bal['balance']:,.2f}</code>\n"
                        f"Disponible: <code>${bal.get('available', 0):,.2f}</code>\n"
                        f"PnL no realizado: <code>${bal.get('unrealized_pnl', 0):,.2f}</code>"
                    )
                    await self._send(
                        _format_premium_message("💰 BALANCE", body),
                        chat_id=chat_id, reply_markup=_main_keyboard())
                else:
                    await self._send("No se pudo obtener balance.",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
            except Exception as e:
                await self._send(f"❌ Error: {e}", chat_id=chat_id)
            return

        if text == "/positions":
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(
                    None, executor.get_position_with_pnl)
                if info:
                    direccion = info.get("direction", "?")
                    qty = info.get("entry_qty", 0)
                    entry = info.get("entry_price", 0)
                    mark = info.get("mark_price", 0)
                    liq = info.get("liquidation_price", 0)
                    pnl = info.get("pnl", 0)
                    pnl_pct = info.get("pnl_pct", 0)
                    lev = info.get("leverage", 0)
                    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                    body = (
                        f"Dirección: {direccion}\n"
                        f"Cantidad: {qty} BTC\n"
                        f"Entrada: ${entry:,.0f}\n"
                        f"Mark: ${mark:,.0f}\n"
                        f"Liquidación: ${liq:,.0f}\n"
                        f"Apalancamiento: {lev}x\n"
                        f"{pnl_emoji} PnL: ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
                    )
                    await self._send(
                        _format_premium_message("🔍 POSICIÓN", body),
                        chat_id=chat_id, reply_markup=_main_keyboard())
                else:
                    await self._send("No hay posiciones abiertas.",
                                     chat_id=chat_id, reply_markup=_main_keyboard())
            except Exception as e:
                await self._send(f"❌ Error: {e}", chat_id=chat_id)
            return

    # ─── Symbol command handler ───────────────────────────────────────────

    async def _handle_symbol_command(self, text: str, chat_id: int):
        parts = text.split()
        cmd = parts[1].upper() if len(parts) > 1 else ""

        if cmd == "CONFIRMAR" and len(parts) >= 3:
            new_symbol = parts[2].upper()
            is_valid, msg = await validate_symbol(new_symbol)
            if not is_valid:
                await self._send(f"❌ {msg}", chat_id=chat_id,
                                 reply_markup=_main_keyboard())
                return
            await self._do_symbol_switch(new_symbol, chat_id)
            return

        if cmd in ("", "CURRENT"):
            current = settings.get_symbol()
            top = await get_top_symbols(limit=20)
            lines = [f"<b>\U0001f501 SÍMBOLO ACTIVO:</b> <code>{current}</code>\n"]
            lines.append("<b>TOP 20 por volumen:</b>")
            for i, s in enumerate(top, 1):
                sym = s["symbol"]
                vol_b = s["volume_24h"] / 1_000_000
                marker = " ⬅️" if sym == current else ""
                lines.append(f"{i}. <code>{sym}</code> ${vol_b:.0f}M{marker}")
            await self._send("\n".join(lines), chat_id=chat_id,
                             reply_markup=_main_keyboard())
            return

        if cmd == "LIST":
            top = await get_top_symbols(limit=50)
            lines = ["<b>TOP 50 SÍMBOLOS por volumen:</b>"]
            for i, s in enumerate(top, 1):
                sym = s["symbol"]
                vol_b = s["volume_24h"] / 1_000_000
                lines.append(f"{i}. <code>{sym}</code> ${vol_b:.0f}M")
            # Telegram max 4096 chars per message
            chunks = []
            chunk = []
            for line in lines:
                chunk.append(line)
                if len("\n".join(chunk)) > 3800:
                    chunks.append("\n".join(chunk))
                    chunk = [line]
            if chunk:
                chunks.append("\n".join(chunk))
            for c in chunks:
                await self._send(c, chat_id=chat_id)
            return

        # Attempt to change symbol
        new_symbol = cmd
        is_valid, msg = await validate_symbol(new_symbol)
        if not is_valid:
            await self._send(f"❌ {msg}", chat_id=chat_id,
                             reply_markup=_main_keyboard())
            return

        # Check for open position
        executor = self._order_executor
        open_position = False
        if executor:
            try:
                loop = asyncio.get_event_loop()
                pos = await loop.run_in_executor(None, executor.get_position_with_pnl)
                if pos:
                    open_position = True
            except Exception:
                pass

        if open_position:
            await self._send(
                f"⚠️ <b>Hay una posición abierta.</b>\n\n"
                f"Cambiar a <code>{new_symbol}</code> cerrará la posición "
                f"actual y reiniciará los streams.\n\n"
                f"Enviá <code>/symbol CONFIRMAR {new_symbol}</code> para forzar el cambio.",
                chat_id=chat_id,
                reply_markup=_main_keyboard())
            return

        await self._do_symbol_switch(new_symbol, chat_id)

    async def _do_symbol_switch(self, new_symbol: str, chat_id: int):
        """Execute the symbol switch: close position, update settings, reset engine, restart WS."""
        old_symbol = settings.get_symbol()
        if new_symbol == old_symbol:
            await self._send(f"✅ Ya estás en <code>{new_symbol}</code>",
                             chat_id=chat_id, reply_markup=_main_keyboard())
            return

        # Close any open position first
        executor = self._order_executor
        if executor:
            try:
                loop = asyncio.get_event_loop()
                pos = await loop.run_in_executor(None, executor.get_position_with_pnl)
                if pos:
                    await self._send("\u23f3 Cerrando posición existente...", chat_id=chat_id)
                    result = await loop.run_in_executor(None, executor.close_all_positions)
                    if not result.get("success"):
                        await self._send(
                            f"❌ No se pudo cerrar posición: {result.get('message', 'error')}",
                            chat_id=chat_id, reply_markup=_main_keyboard())
                        return
                    await self._send("✅ Posición cerrada.", chat_id=chat_id)
            except Exception as e:
                await self._send(f"❌ Error al cerrar posición: {e}",
                                 chat_id=chat_id, reply_markup=_main_keyboard())
                return

        # Update settings
        old = settings.set_symbol(new_symbol)

        # Reset AsyncDataEngine buffers
        if self._data_engine:
            try:
                self._data_engine.reset_symbol(new_symbol)
                log.info("DataEngine reset for %s", new_symbol)
            except Exception as e:
                log.warning("DataEngine reset_symbol failed: %s", e)

        # Restart WebSocket streams
        try:
            await binance_client.start_streams(new_symbol)
            log.info("WebSocket streams restarted for %s", new_symbol)
        except Exception as e:
            log.warning("start_streams failed: %s", e)
            await self._send(
                f"⚠️ Símbolo cambiado a <code>{new_symbol}</code> "
                f"pero los WebSockets fallaron: {e}\n"
                f"Usá <code>/symbol {new_symbol}</code> de nuevo para reintentar.",
                chat_id=chat_id, reply_markup=_main_keyboard())
            return

        await self._send(
            f"✅ <b>Símbolo cambiado</b>\n"
            f"{old_symbol} → <code>{new_symbol}</code>\n\n"
            f"Streams reiniciados. El bot está operando <code>{new_symbol}</code>.",
            chat_id=chat_id, reply_markup=_main_keyboard())

    # ─── Inline callback handler ───────────────────────────────────────────

    async def _handle_callback(self, callback: dict):
        # FIX A — acknowledge callback FIRST (quita loading spinner)
        callback_id = callback.get("id", "")
        await self._answer_callback(callback)
        if callback_id in self._processed_callbacks:
            return
        self._processed_callbacks.add(callback_id)
        if len(self._processed_callbacks) > 500:
            self._processed_callbacks = set(list(self._processed_callbacks)[-200:])

        data = callback.get("data", "")
        cid = callback.get("message", {}).get("chat", {}).get("id", 0)
        mid = callback.get("message", {}).get("message_id", 0)
        user = callback.get("from", {})

        if data == "refresh_status":
            s = self._state or {}
            p = s.get("price", 0)
            chg = s.get("change_pct", 0)
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            delta = s.get("delta", 0)
            cvd = s.get("cvd", 0)

            def _te(d):
                if d in ("ALCISTA", "UP", "BULLISH"): return "\U0001f7e2"
                if d in ("BAJISTA", "DOWN", "BEARISH"): return "\U0001f7e3"
                return "\U0001f7e1"

            sig_emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"
            if sig == "NEUTRAL" or sig == "WAIT":
                sig_emoji = "\U0001f7e1"

            t5m  = _te(s.get("trend_5m", "WAIT"))
            t15m = _te(s.get("trend_15m", "WAIT"))
            t1h  = _te(s.get("trend_1h", "WAIT"))
            t4h  = _te(s.get("trend_4h", "WAIT"))
            t1d  = _te(s.get("trend_1d", "WAIT"))

            body = (
                f"<b>{settings.get_symbol()}</b> <code>${p:,.0f} ({chg:+.2f}%)</code>\n\n"
                f"<b>SEÑAL:</b> {sig_emoji} {sig} <code>({conf:.0f}%)</code>\n\n"
                f"\U0001f4c8 <b>MTF</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"  5m  {t5m}  {s.get('trend_5m', 'WAIT')}\n"
                f"  15m {t15m} {s.get('trend_15m', 'WAIT')}\n"
                f"  1h  {t1h}  {s.get('trend_1h', 'WAIT')}\n"
                f"  4h  {t4h}  {s.get('trend_4h', 'WAIT')}\n"
                f"  1d  {t1d}  {s.get('trend_1d', 'WAIT')}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f4ca Delta <code>{delta:+.0f}</code> | "
                f"CVD <code>{cvd:.1f}</code>\n\n"
                f"\u23f0 {s.get('timestamp', '')}"
            )
            await self._edit_message(cid, mid,
                                     _format_premium_message("\U0001f504 ACTUALIZADO", body))

        elif data == "gemini_analysis":
            s = self._state or {}
            snapshot_json = json.dumps({
                "price": s.get("price", 0), "delta": s.get("delta", 0),
                "rsi": s.get("rsi", 50), "signal": s.get("signal_text", "WAIT"),
                "confidence": s.get("confidence", 0),
            }, indent=2)
            prompt = (
                f"Analisis rapido del mercado de {settings.get_symbol()}:\n"
                f"```json\n{snapshot_json}\n```\n\n"
                f"Da tu opinion en 2-3 oraciones."
            )
            reply = await self._chat_gemini_raw(prompt)
            await self._send(f"\U0001f9e0 <b>Gemini AI:</b>\n\n{reply}",
                             chat_id=cid, reply_markup=_main_keyboard())

        elif data == "chart":
            await self._send_typing(cid)
            s = self._state or {}
            mtf_klines = await self._fetch_mtf_klines()
            if mtf_klines:
                timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
                for tf in timeframes:
                    klines = mtf_klines.get(tf)
                    if not klines:
                        continue
                    trend = s.get(f"trend_{tf}", "NEUTRAL")
                    png = await self._generate_mtf_chart(klines, tf, trend)
                    if png:
                        caption = (
                            f"<b>{settings.get_symbol()}</b> \u2014 <b>{tf}</b> | "
                            f"Tendencia: {trend}"
                        )
                        await self._send_photo(png, caption=caption, chat_id=cid)
                ai = await self._get_mtf_analysis(cid)
                if ai:
                    await self._send_ai_insight(ai, chat_id=cid,
                                                reply_markup=_main_keyboard())
                else:
                    await self._send("\U0001f4c8 Graficos generados. Activa Gemini AI para analisis.",
                                     chat_id=cid, reply_markup=_main_keyboard())

        elif data.startswith("set_symbol:"):
            new_symbol = data.split(":", 1)[1]
            cid = callback.get("message", {}).get("chat", {}).get("id", 0)
            mid = callback.get("message", {}).get("message_id", 0)
            if new_symbol == settings.get_symbol():
                await self._answer_callback(callback, text=f"Ya en {new_symbol}")
                return
            await self._answer_callback(callback, text=f"Cambiando a {new_symbol}...")
            await self._do_symbol_switch(new_symbol, cid)
            await self._edit_message(cid, mid, f"\U0001f501 <b>S\u00edmbolo cambiado</b>\n<code>{new_symbol}</code>")
            return

        elif data == "close_position":
            executor = self._order_executor
            if executor is None:
                await self._send("\u274c OrderExecutor no disponible", chat_id=cid)
                return
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, executor.close_all_positions)
                if result.get("success"):
                    await self._send("\u2705 Posici\u00f3n cerrada exitosamente",
                                     chat_id=cid, reply_markup=_main_keyboard())
                else:
                    msg = result.get("message", "Error desconocido")
                    await self._send(f"\u274c Error cerrando posici\u00f3n:\n{msg}",
                                     chat_id=cid, reply_markup=_main_keyboard())
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=cid, reply_markup=_main_keyboard())

        elif data.startswith("exec_order:"):
            parts = data.split(":")
            if len(parts) < 5:
                await self._send("⚠️ Datos de señal inválidos", chat_id=cid)
                return
            _, bdir, price_str, sl_str, tp_str = parts[:5]
            try:
                entry_price = float(price_str)
                sl_price = float(sl_str)
                tp_price = float(tp_str)
            except ValueError:
                await self._send("⚠️ Error parsing signal prices", chat_id=cid)
                return
            if bdir not in ("ALZA", "BAJA") or entry_price <= 0 or sl_price <= 0 or tp_price <= 0:
                await self._send("⚠️ Señal inválida — datos en cero", chat_id=cid)
                return

            # FIX 2 — Precio vencido
            msg_timestamp = callback.get("message", {}).get("date", 0)
            signal_age = time.time() - msg_timestamp
            if signal_age > 45:
                await self._send_text_plain(
                    f"⚠️ Señal expirada ({signal_age:.0f}s). Espera nueva señal.",
                    chat_id=cid)
                return

            precio_actual = self._state.get("price", 0) if self._state else 0
            if precio_actual <= 0:
                await self._send_text_plain("❌ Sin precio actual. Orden cancelada.",
                                            chat_id=cid)
                return

            desviacion = abs(precio_actual - entry_price) / entry_price
            if desviacion > 0.003:
                await self._send_text_plain(
                    f"⚠️ Precio cambió {desviacion*100:.2f}% desde la señal. "
                    f"Orden cancelada.",
                    chat_id=cid)
                return

            entry_price = precio_actual

            executor = self._order_executor
            if executor is None:
                await self._send("❌ OrderExecutor no disponible", chat_id=cid)
                return
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: executor.execute_trade_signal(
                        direction=bdir,
                        entry_price=entry_price,
                        sl_price=sl_price,
                        tp_price=tp_price,
                    )
                )
                if result:
                    await self._send(
                        f"\u23f3 Orden {bdir} encolada:\n"
                        f"Entrada: <code>${entry_price:,.0f}</code>\n"
                        f"SL: <code>${sl_price:,.0f}</code> | "
                        f"TP: <code>${tp_price:,.0f}</code>\n"
                        f"<i>Esperando confirmaci\u00f3n de Binance...</i>",
                        chat_id=cid,
                    )
                else:
                    reason = getattr(executor, '_last_reject_reason', '')
                    if "posici\u00f3n existente" in reason.lower():
                        await self._send_position_with_close(executor, cid)
                    else:
                        msg = "\u274c Orden rechazada por filtros de seguridad"
                        if reason:
                            msg += f"\n   Causa: {reason}"
                        await self._send(msg, chat_id=cid)
            except Exception as e:
                log.error(f"Callback execute_order error: {e}")
                await self._send(f"❌ Error ejecutando orden: {e}", chat_id=cid)

        elif data.startswith("set_amount:"):
            pending = self._pending_amount_change.get(cid, {})
            if not pending or time.time() > pending.get("expires", 0):
                await self._send_text_plain(
                    "⚠️ Confirmación expirada. Envía el monto de nuevo.",
                    chat_id=cid)
                self._pending_amount_change.pop(cid, None)
                return
            monto = pending["amount"]
            del self._pending_amount_change[cid]
            settings.set_global_trade_amount(monto)
            await self._send_text_plain(
                f"✅ Capital actualizado: ${monto:,.2f}",
                chat_id=cid)

        elif data == "cancel_amount":
            self._pending_amount_change.pop(cid, None)
            await self._send_text_plain(
                "❌ Cambio cancelado.",
                chat_id=cid)

    async def _answer_callback(self, callback_or_id, text: str = ""):
        """Acknowledge callback query to remove loading spinner."""
        if isinstance(callback_or_id, dict):
            qid = callback_or_id.get("id", "")
        else:
            qid = callback_or_id
        if not qid:
            return
        url = f"https://api.telegram.org/bot{self._bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": qid}
        if text:
            payload["text"] = text
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    text_resp = await resp.text()
                    log.warning("answerCallbackQuery error: %s", text_resp)
        except Exception as e:
            log.warning("answerCallbackQuery exception: %s", e)
        finally:
            if self._session is None:
                await session.close()

    # ─── Polling ───────────────────────────────────────────────────────────

    async def _poll_updates(self):
        offset = 0
        poll_failures = 0
        max_poll_delay = 120.0
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
                params = {"offset": offset, "timeout": 30, "allowed_updates":
                          ["message", "callback_query"]}
                async with self._session.get(url, params=params,
                                             timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    if resp.status != 200:
                        poll_failures += 1
                        delay = min(5.0 * (2 ** (poll_failures - 1)), max_poll_delay)
                        await asyncio.sleep(delay)
                        continue
                    poll_failures = 0
                    data = await resp.json()
                    if not data.get("ok"):
                        continue
                    BUTTON_MAP = {
                        # ── Root menu ──────────────────────────────────────────
                        "\U0001f4ca Status":    "/status",
                        "\U0001f4c8 Signal":    "/signal",
                        "\U0001f9e0 Brain":     "/brain",
                        "\U0001f4c9 Chart":     "/chart",
                        "\u2699\ufe0f Config":  "/config",
                        "\U0001f4b0 Balance":   "/balance",
                        "\U0001f4cd Positions": "/positions",
                        "\U0001f504 Símbolo":   "__symbol_menu__",
                        # ── ⚡ OPERAR BB-450 → show trading sub-menu ──────────
                        "\u26a1 OPERAR BB-450": "__trading_menu__",
                        # ── Trading sub-menu actions ───────────────────────────
                        "\U0001f7e2 ABRIR LONG":                    "/buy",
                        "\U0001f534 ABRIR SHORT":                   "/sell",
                        "\u274c CERRAR POSICI\u00d3N":              "/close_all",
                        "\u2b05\ufe0f Volver al Men\u00fa Principal": "__main_menu__",
                    }
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update:
                            msg = update["message"]
                            text = msg.get("text", "").strip()
                            chat_id = msg.get("chat", {}).get("id", 0)

                            cmd = BUTTON_MAP.get(text)

                            # ── Special UI actions (no command handler needed) ──
                            if cmd == "__trading_menu__":
                                await self._send(
                                    "\u26a1 <b>OPERAR BB-450</b>\n\n"
                                    "Selecciona una acci\u00f3n. "
                                    "<i>Las \u00f3rdenes se ejecutan al precio de mercado.</i>",
                                    chat_id=chat_id,
                                    reply_markup=_trading_keyboard(),
                                )
                                continue
                            if cmd == "__main_menu__":
                                await self._send(
                                    "\u2b05\ufe0f Men\u00fa principal restaurado.",
                                    chat_id=chat_id,
                                    reply_markup=_main_keyboard(),
                                )
                                continue

                            if cmd == "__symbol_menu__":
                                top = await get_top_symbols(limit=20)
                                rows = []
                                for i in range(0, len(top), 2):
                                    row = []
                                    row.append(_btn(top[i]["symbol"], f"set_symbol:{top[i]['symbol']}"))
                                    if i + 1 < len(top):
                                        row.append(_btn(top[i + 1]["symbol"], f"set_symbol:{top[i + 1]['symbol']}"))
                                    rows.append(_row(*row))
                                rows.append(_row(_btn("\u2b05\ufe0f Volver", "__main_menu__")))
                                await self._send(
                                    "\U0001f504 <b>Seleccion\u00e1 un s\u00edmbolo:</b>",
                                    chat_id=chat_id,
                                    reply_markup=_keyboard(*rows),
                                )
                                continue

                            if cmd:
                                msg["text"] = cmd
                                await self._handle_command(msg)

                            elif text.startswith("/"):
                                await self._handle_command(msg)

                            elif text:
                                # ── Numeric handler: confirm capital change (FIX 5 / FIX C) ──
                                if re.match(r'^\d+(\.\d+)?$', text):
                                    monto = float(text)
                                    self._pending_amount_change[chat_id] = {
                                        "amount": monto,
                                        "expires": time.time() + 120
                                    }
                                    actual = getattr(settings, 'GLOBAL_TRADE_AMOUNT', 0)
                                    await self._send_html(
                                        f"⚠️ ¿Confirmar capital por trade: <b>${monto:,.2f}</b>?\n"
                                        f"Actual: ${actual:,.2f}",
                                        chat_id=chat_id,
                                        reply_markup=_keyboard(_row(
                                            _btn("✅ Confirmar", f"set_amount:{monto}"),
                                            _btn("❌ Cancelar", "cancel_amount")
                                        ))
                                    )
                                else:
                                    # ── Free text → Gemini auto-chat ──────────────
                                    await self._handle_gemini_chat(msg)

                        elif "callback_query" in update:
                            await self._handle_callback(update["callback_query"])

            except asyncio.TimeoutError:
                poll_failures = 0
                continue
            except Exception as e:
                poll_failures += 1
                delay = min(5.0 * (2 ** (poll_failures - 1)), max_poll_delay)
                if poll_failures <= 3 or poll_failures % 6 == 0:
                    log.warning("Polling error (%d): %s — reconnecting in %.0fs\n%s",
                                poll_failures, e, delay, traceback.format_exc())
                await asyncio.sleep(delay)

    # ─── Public entry points ───────────────────────────────────────────────

    def run(self):
        """Synchronous entry point - starts the bot thread and blocks."""
        self.start()
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log.info("TelegramBot stopped")


def start_bot(chat_id: int, user_config: dict = None, queue: "queue.Queue" = None,
              exchange: object = None, gemini_client: object = None):
    """Start the Telegram bot in a daemon thread.  Non-blocking."""
    bot = TelegramBot(chat_id, user_config, queue, exchange, gemini_client)
    bot.start()
    return bot
