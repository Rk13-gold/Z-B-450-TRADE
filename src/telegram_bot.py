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

import asyncio, io, json, logging, os, re, threading, time, unicodedata
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import aiohttp, edge_tts

os.environ["MPLBACKEND"] = "Agg"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

from config.settings import settings

log = logging.getLogger("TelegramBot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
GEMINI_MODEL = "gemini-2.0-flash"

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

def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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

def _markdown_to_html(text: str) -> str:
    tag_pat = re.compile(r'</?(b|i|u|s|code|pre|a|tg-spoiler|span)\b[^>]*>')
    protected = {}
    def _protect(m):
        pid = f"\x00TAG{len(protected)}\x00"
        protected[pid] = m.group(0)
        return pid
    text = tag_pat.sub(_protect, text)
    text = _html_escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<b>\1</b>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'_(.+?)_', r'<i>\1</i>', text)
    for pid, tag in protected.items():
        text = text.replace(pid, tag)
    return text

def _strip_emoji(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if unicodedata.category(c) not in ("So", "Mn")).strip()

def _safe_get(d: dict, key: str, fmt=None, default="--") -> str:
    try:
        val = d.get(key)
        if val is None: return default
        return fmt(val) if fmt else str(val)
    except Exception:
        return default

def _format_premium_message(title: str, body: str, timestamp: str = "") -> str:
    """Build Telegram-safe HTML message with premium styling."""
    lines = [f"<b>{title}</b>", SEP, "", body]
    if timestamp:
        lines += ["", f"\u23f0 <code>{timestamp}</code>"]
    return _sanitize_html("\n".join(lines))

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

    def __init__(self):
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
        self._prev_trap_type: Optional[str] = None

        self._last_update_id = 0

        self._gemini_key: str = settings.GEMINI_API_KEY
        self._gemini_enabled: bool = bool(self._gemini_key)
        self._gemini_history: list = []

        self._brain_block_log_counter: int = 0

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
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=65, sock_connect=15)
        ) as session:
            self._session = session
            await self._verify_connection()
            await asyncio.gather(
                asyncio.create_task(self._process_queue()),
                asyncio.create_task(self._poll_updates()),
                asyncio.create_task(self._alert_loop()),
                return_exceptions=True,
            )

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
                f"El dashboard est\u00e1 corriendo y monitoreando <code>{settings.SYMBOL}</code>.\n\n"
                "<b>Comandos:</b>\n"
                "/start - Men\u00fa principal\n"
                "/info - Todos los indicadores\n"
                "/signal - Se\u00f1al actual\n"
                "/alerts - Configurar notificaciones\n"
                "/status - Estado del bot\n"
                "/micro - Microestructura cuantitativa",
            )
            await self._send(msg)
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
                        vol_ratio = vol / max(avg_vol, 0.001)
                        if conf > 55 and avg_vol > 0 and vol_ratio >= 1.0:
                            await self._send_signal_alert(snapshot)
                        else:
                            print(f"[TELEGRAM BOT] Alerta bloqueada: Confianza ({conf:.0f}%) por debajo del umbral o vol_ratio ({vol_ratio:.1f}x) insuficiente.")

                # CANAL 2: SENIAL DEL CEREBRO CUANTICO
                brain_dir = snapshot.get('brain_direction')
                if brain_dir in ('ALZA', 'BAJA'):
                    brain_conf = snapshot.get('brain_confidence_pct', 0.0)

                    # Price Action Invalidation
                    price_above_vwap = snapshot.get('price_above_vwap', False)
                    buy_imb_5 = snapshot.get('buy_imbalance_count_5', 0)
                    if brain_dir == 'BAJA' and price_above_vwap and buy_imb_5 >= 3:
                        self._log_brain_block(
                            "divergencia BAJA - precio sobre VWAP + "
                            f"{buy_imb_5} desequilibrios de compra")
                        self._last_brain_direction = None
                        continue

                    if brain_dir == self._last_brain_direction:
                        self._log_brain_block("direcci\u00f3n repetida")
                    elif brain_conf >= 60.0:
                        self._last_brain_direction = brain_dir
                        print(f"[TELEGRAM BOT] Alerta del Cerebro Cu\u00e1ntico detectada: {brain_dir} @ {brain_conf:.0f}%")
                        await self._send_brain_alert(snapshot)

            except asyncio.TimeoutError:
                continue
            except Exception:
                pass

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
            return
        if trend != self._prev_trend:
            self._prev_trend = trend
            emoji = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f7e3"
            body = (
                f"  Nueva tendencia: <code>{trend}</code>\n"
                f"  Precio: <code>${self._state.get('price', 0):,.0f}</code>\n"
                f"  RSI: <code>{self._state.get('rsi', 50):.1f}</code>"
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

        price_above_vwap = self._state.get('price_above_vwap', False)
        buy_imb_5 = self._state.get('buy_imbalance_count_5', 0)

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
            reasons = " | ".join(triggers[:3])
            body = (
                f"  Precio: <code>${p:,.0f}</code>\n"
                f"  {reasons}\n"
                f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.0f}</code>\n\n"
                f"\U0001f6a8 <i>Movimiento fuerte - monitorear entrada</i>"
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
            klines = await self._fetch_klines()
            if klines:
                png = await self._generate_chart(klines)
                if png:
                    await self._send_photo(png, caption=f"{emoji} {direction} confirmado por AI | SL ${sl:,.0f} TP ${tp:,.0f}")


    # ─── Send helpers ──────────────────────────────────────────────────────

    async def _send(self, text: str, chat_id: int = None) -> bool:
        cid = chat_id or self._chat_id
        if not cid:
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": cid, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return True
                    text_resp = await resp.text()
                    log.warning("Telegram send error %s: %s", resp.status, text_resp)
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram send exception: %s", e)
            return False

    async def _send_photo(self, photo_bytes: bytes, caption: str = "",
                           chat_id: int = None) -> bool:
        cid = chat_id or self._chat_id
        if not cid:
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field("chat_id", str(cid))
                data.add_field("photo", photo_bytes,
                               filename="chart.png", content_type="image/png")
                if caption:
                    data.add_field("caption", caption, content_type="text/plain")
                    data.add_field("parse_mode", "HTML")
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return True
                    text_resp = await resp.text()
                    log.warning("Telegram sendPhoto error %s: %s", resp.status, text_resp)
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram sendPhoto exception: %s", e)
            return False

    async def _edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._bot_token}/editMessageText"
        payload = {"chat_id": chat_id, "message_id": message_id,
                    "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return resp.status == 200
        except Exception:
            return False

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

        body = (
            f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n\n"
            f"<b>MERCADO:</b>\n"
            f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"  B/A: <code>{ba:.3f}x</code> | Vol: <code>{vol_r:.1f}x</code>\n"
            f"  RSI: <code>{rsi:.1f}</code> | Trend 5m: <code>{trend}</code>\n"
            f"  Trampa: <code>{trap}</code>\n\n"
            f"<b>ESTRATEGIA:</b>\n"
            f"  1\u20e3 Confirmar con CVD/Delta\n"
            f"  2\u20e3 Esperar retroceso a soporte\n"
            f"  3\u20e3 Entrada con SL ajustado a la vela anterior\n\n"
            f"\u23f0 {snapshot.get('timestamp', '')}"
        )
        await self._send(_format_premium_message(f"{emoji} {sig} DETECTADO ({conf:.0f}%)", body))

    async def _send_brain_alert(self, snapshot: dict):
        bdir = snapshot.get('direction') or snapshot.get('brain_direction', 'INCIERTO')
        bconf = snapshot.get('confidence_pct') or snapshot.get('brain_confidence_pct', 0.0)
        p = snapshot.get('price', 0)
        delta = snapshot.get('delta', 0)
        cvd = snapshot.get('cvd', 0)
        vol = snapshot.get('volume', 0)
        avg_vol = snapshot.get('avg_volume', 0)
        vol_r = vol / avg_vol if avg_vol > 0 else 0
        bracket = snapshot.get('risk_bracket', {})
        if not isinstance(bracket, dict):
            bracket = {}
        sl = bracket.get('sl', 0) or snapshot.get('brain_bracket_sl', 0)
        tp1 = bracket.get('tp1', 0) or snapshot.get('brain_bracket_tp1', 0)
        tp2 = bracket.get('tp2', 0) or snapshot.get('brain_bracket_tp2', 0)

        # Safety: nunca enviar con SL/TP inválidos
        if sl <= 0 or tp1 <= 0:
            log.error(f"[SAFETY] _send_brain_alert ABORTADO — "
                      f"SL=${sl:.0f} TP=${tp1:.0f}")
            return

        capital = self._user_config.get('capital', 100)
        sl_pct = abs((p - sl) / p * 100) if p > 0 else 0
        emoji = "\U0001f7e2" if bdir == 'ALZA' else "\U0001f7e3"

        r_perdida = sl_pct
        r_ganancia = abs((tp1 - sl) / (p - sl)) * r_perdida if abs(p - sl) > 0 else 0

        body = (
            f"<b>DIRECCION:</b> {emoji} {bdir}\n"
            f"<b>CONFIANZA:</b> <code>{bconf:.1f}%</code>\n"
            f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n\n"
            f"<b>MERCADO:</b>\n"
            f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
            f"  Vol: <code>{vol_r:.1f}x</code>\n\n"
            f"<b>\U0001f4b0 CAPITAL:</b> <code>${capital:.1f}</code> x <code>{settings.LEVERAGE}x</code>\n"
            f"<b>\U0001f6a9 SL:</b> <code>${sl:,.0f}</code> ({sl_pct:.1f}% | R {r_perdida:.1f}%)\n"
            f"<b>\U0001f4c8 TP1:</b> <code>${tp1:,.0f}</code> (R {r_ganancia:.1f}%)\n"
            + (f"<b>\U0001f4c8 TP2:</b> <code>${tp2:,.0f}</code>\n" if tp2 else "")
            + f"\n\u23f0 {snapshot.get('timestamp', '')}"
        )
        await self._send(_format_premium_message("\U0001f9e0 SENIAL DEL CEREBRO CUANTICO", body))

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
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.0-flash", contents=prompt
                ))
            return resp.text.strip()
        except Exception as e:
            log.warning("Gemini raw inference error: %s", e)
            return json.dumps({"decision": "NO_ENTRAR", "razon": f"Error Gemini: {e}"})

    # ─── Chart generation ──────────────────────────────────────────────────

    async def _fetch_klines(self) -> list:
        try:
            from src.engine.binance_client import binance_client as bc
            if bc is None:
                return []
            loop = asyncio.get_event_loop()
            klines = await loop.run_in_executor(
                None, lambda: bc.get_klines(symbol=settings.SYMBOL, interval="1m", limit=120)
            )
            return klines if klines else []
        except Exception as e:
            log.warning("Fetch klines error: %s", e)
            return []

    async def _generate_chart(self, klines: list) -> Optional[bytes]:
        if not klines or len(klines) < 30:
            return None
        try:
            dates = [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc) for k in klines]
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})
            fig.patch.set_facecolor("#0B0B0B")
            ax1.set_facecolor("#0B0B0B")
            ax2.set_facecolor("#0B0B0B")

            # Candlesticks
            for i in range(len(klines)):
                color = "#00FF88" if closes[i] >= klines[i][1] else "#FF3366"
                ax1.plot([dates[i], dates[i]], [lows[i], highs[i]], color=color, linewidth=0.8)
                ax1.plot([dates[i], dates[i]], [min(closes[i], float(klines[i][1])),
                         max(closes[i], float(klines[i][1]))], color=color, linewidth=3)

            # EMAs
            ema9 = self._compute_ema(closes, 9)
            ema21 = self._compute_ema(closes, 21)
            if ema9 and ema21:
                ax1.plot(dates[-len(ema9):], ema9, color="#FFD700", linewidth=1, alpha=0.9, label="EMA 9")
                ax1.plot(dates[-len(ema21):], ema21, color="#FF69B4", linewidth=1, alpha=0.9, label="EMA 21")

            # Volume bars
            vol_colors = ["#00FF88" if closes[i] >= float(klines[i][1]) else "#FF3366" for i in range(len(klines))]
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

            ax1.set_title(f"{settings.SYMBOL} - Analisis Tecnico",
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
            log.warning("Chart generation error: %s", e)
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
            f"Datos del mercado en vivo de {settings.SYMBOL}:\n"
            f"```json\n{snapshot_json}\n```\n\n"
            f"Pregunta del usuario ({user_name}): {user_text}\n\n"
            f"Responde de forma clara y concisa (max 350 tokens). "
            f"Usa datos del snapshot cuando sea relevante. "
            f"NO des consejos financieros, solo analisis."
        )
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._gemini_client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt,
                    config={"generation_config": {"max_output_tokens": 350}}
                ))
            reply = resp.text.strip()
        except Exception as e:
            reply = f"Error al consultar Gemini: {e}"
            log.warning("Gemini chat error: %s", e)

        await self._send(f"<b>Gemini AI ({user_name}):</b>\n\n{reply}", chat_id=chat_id)

    # ─── Command handlers ──────────────────────────────────────────────────

    async def _handle_command(self, message: dict):
        text = message.get("text", "").strip().lower()
        chat_id = message.get("chat", {}).get("id", 0)
        user = message.get("from", {})

        if text == "/start":
            await self._send(
                f"\U0001f916 <b>BB-450 Trading Bot</b>\n\n"
                f"Bienvenido! Usa /help para ver comandos disponibles.",
                chat_id=chat_id)
            return

        if text == "/help":
            await self._send(
                "\U0001f4d6 <b>COMANDOS DISPONIBLES</b>\n\n"
                "/start - Iniciar bot\n"
                "/help - Mostrar comandos\n"
                "/status - Estado del mercado\n"
                "/config - Configuración actual\n"
                "/signal - Última señal generada\n"
                "/brain - Última señal del cerebro cuántico\n"
                "/buy [monto] - Abrir LONG con monto opcional\n"
                "/sell [monto] - Abrir SHORT con monto opcional\n"
                "/close_all - Cerrar todas las posiciones\n"
                "/balance - Ver balance de la cuenta\n"
                "/positions - Ver posiciones abiertas\n"
                "/gemini [pregunta] - Consultar Gemini AI\n"
                "/chart - Generar gráfico técnico\n"
                "/settings - Ver ajustes activos\n\n"
                "Ejemplo: <code>/gemini que opinas del delta actual?</code>",
                chat_id=chat_id)
            return

        if text.startswith("/gemini"):
            user_text = text[len("/gemini"):].strip()
            if not user_text:
                await self._send(
                    "Debes incluir una pregunta. Ej: <code>/gemini que opinas del mercado?</code>",
                    chat_id=chat_id)
                return
            message["text"] = user_text
            await self._handle_gemini_chat(message)
            return

        if text == "/status":
            s = self._state or {}
            p = s.get("price", 0)
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            delta = s.get("delta", 0)
            cvd = s.get("cvd", 0)
            vol = s.get("volume", 0)
            avg_vol = s.get("avg_volume", 0)
            vol_r = vol / avg_vol if avg_vol > 0 else 0
            brain_dir = s.get("brain_direction", "N/A")
            brain_conf = s.get("brain_confidence_pct", 0)
            trap = s.get("trap_status", "N/A")
            emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"
            if sig == "WAIT":
                emoji = "\u23f3"

            body = (
                f"<b>{settings.SYMBOL}</b> | <code>${p:,.0f}</code>\n\n"
                f"<b>SEÑAL:</b> {emoji} {sig} <code>({conf:.0f}%)</code>\n"
                f"<b>CEREBRO:</b> {brain_dir} <code>({brain_conf:.0f}%)</code>\n"
                f"<b>TRAMPA:</b> <code>{trap}</code>\n\n"
                f"<b>MERCADO:</b>\n"
                f"  Delta: <code>{delta:+.0f}</code> | CVD: <code>{cvd:.1f}</code>\n"
                f"  Vol: <code>{vol_r:.1f}x</code>\n\n"
                f"\u23f0 {s.get('timestamp', 'No data')}"
            )
            await self._send(_format_premium_message("\U0001f4ca ESTADO DEL MERCADO", body),
                             chat_id=chat_id)
            return

        if text == "/config" or text == "/settings":
            body = (
                f"<b>CONFIGURACION ACTIVA</b>\n\n"
                f"Símbolo: <code>{settings.SYMBOL}</code>\n"
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
                             chat_id=chat_id)
            return

        if text == "/signal":
            s = self._state or {}
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            if sig in ("LONG", "SHORT"):
                await self._send_signal_alert(s)
            else:
                await self._send(f"\u23f3 No hay señal activa. Esperando...", chat_id=chat_id)
            return

        if text == "/brain":
            s = self._state or {}
            bdir = s.get("brain_direction", "N/A")
            bconf = s.get("brain_confidence_pct", 0)
            if bdir in ("ALZA", "BAJA"):
                await self._send_brain_alert(s)
            else:
                await self._send(f"\U0001f9e0 Cerebro cuántico: {bdir} ({bconf:.0f}%)",
                                 chat_id=chat_id)
            return

        if text == "/chart":
            klines = await self._fetch_klines()
            if klines:
                png = await self._generate_chart(klines)
                if png:
                    s = self._state or {}
                    sig = s.get('signal_text', 'WAIT')
                    conf = s.get('confidence', 0)
                    caption = f"{settings.SYMBOL} | Señal: {sig} ({conf:.0f}%)"
                    await self._send_photo(png, caption=caption, chat_id=chat_id)
                    return
            await self._send("No se pudo generar el gráfico.", chat_id=chat_id)
            return

        # Trading commands
    async def _execute_trade(self, message: dict):
        text = message.get("text", "").strip().lower()
        chat_id = message.get("chat", {}).get("id", 0)

        if text.startswith("/buy"):
            if not self._exchange:
                await self._send("\u274c Exchange no configurado", chat_id=chat_id)
                return
            parts = text.split()
            amount = float(parts[1]) if len(parts) > 1 else self._user_config.get("capital", 100)
            try:
                from src.engine.order_executor import OrderExecutor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: OrderExecutor(self._exchange).execute_buy(amount))
                if result:
                    await self._send(f"\u2705 LONG ejecutado\nMonto: ${amount:.1f}\n"
                                     f"Precio: ${result.get('price', 0):,.0f}",
                                     chat_id=chat_id)
                else:
                    await self._send("\u274c Error ejecutando LONG", chat_id=chat_id)
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=chat_id)
            return

        if text.startswith("/sell"):
            if not self._exchange:
                await self._send("\u274c Exchange no configurado", chat_id=chat_id)
                return
            parts = text.split()
            amount = float(parts[1]) if len(parts) > 1 else self._user_config.get("capital", 100)
            try:
                from src.engine.order_executor import OrderExecutor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: OrderExecutor(self._exchange).execute_sell(amount))
                if result:
                    await self._send(f"\u2705 SHORT ejecutado\nMonto: ${amount:.1f}\n"
                                     f"Precio: ${result.get('price', 0):,.0f}",
                                     chat_id=chat_id)
                else:
                    await self._send("\u274c Error ejecutando SHORT", chat_id=chat_id)
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=chat_id)
            return

        if text == "/close_all":
            if not self._exchange:
                await self._send("\u274c Exchange no configurado", chat_id=chat_id)
                return
            try:
                from src.engine.order_executor import OrderExecutor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: OrderExecutor(self._exchange).close_all_positions())
                if result:
                    await self._send("\u2705 Todas las posiciones cerradas", chat_id=chat_id)
                else:
                    await self._send("\u274c Error cerrando posiciones", chat_id=chat_id)
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=chat_id)
            return

        if text == "/balance":
            if not self._exchange:
                await self._send("\u274c Exchange no configurado", chat_id=chat_id)
                return
            try:
                from src.engine.order_executor import OrderExecutor
                loop = asyncio.get_event_loop()
                balance = await loop.run_in_executor(
                    None, lambda: OrderExecutor(self._exchange).get_balance())
                if balance:
                    body = "\n".join(
                        f"  {asset}: <code>{free:.4f}</code>"
                        for asset, free in balance.items()
                    )
                    await self._send(_format_premium_message("\U0001f4b0 BALANCE", body),
                                     chat_id=chat_id)
                else:
                    await self._send("No se pudo obtener balance.", chat_id=chat_id)
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=chat_id)
            return

        if text == "/positions":
            if not self._exchange:
                await self._send("\u274c Exchange no configurado", chat_id=chat_id)
                return
            try:
                from src.engine.order_executor import OrderExecutor
                loop = asyncio.get_event_loop()
                positions = await loop.run_in_executor(
                    None, lambda: OrderExecutor(self._exchange).get_positions())
                if positions:
                    lines = []
                    for pos in positions:
                        side_emoji = "\U0001f7e2" if pos.get("side") == "LONG" else "\U0001f7e3"
                        lines.append(
                            f"{side_emoji} {pos.get('symbol', settings.SYMBOL)}\n"
                            f"  Size: <code>{pos.get('size', 0):.4f}</code>\n"
                            f"  Entry: <code>${pos.get('entry', 0):,.0f}</code>\n"
                            f"  PnL: <code>{pos.get('pnl', 0):+.2f} USDT</code>\n"
                        )
                    body = "\n".join(lines)
                    await self._send(_format_premium_message("\U0001f4cb POSICIONES", body),
                                     chat_id=chat_id)
                else:
                    await self._send("No hay posiciones abiertas.", chat_id=chat_id)
            except Exception as e:
                await self._send(f"\u274c Error: {e}", chat_id=chat_id)
            return

    # ─── Inline callback handler ───────────────────────────────────────────

    async def _handle_callback(self, callback: dict):
        data = callback.get("data", "")
        cid = callback.get("message", {}).get("chat", {}).get("id", 0)
        mid = callback.get("message", {}).get("message_id", 0)
        user = callback.get("from", {})

        if data == "refresh_status":
            s = self._state or {}
            p = s.get("price", 0)
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            body = (
                f"<b>PRECIO:</b> <code>${p:,.0f}</code>\n"
                f"<b>SEÑAL:</b> <code>{sig}</code> ({conf:.0f}%)\n"
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
                f"Analisis rapido del mercado de {settings.SYMBOL}:\n"
                f"```json\n{snapshot_json}\n```\n\n"
                f"Da tu opinion en 2-3 oraciones."
            )
            reply = await self._chat_gemini_raw(prompt)
            await self._send(f"\U0001f9e0 <b>Gemini AI:</b>\n\n{reply}", chat_id=cid)

        elif data == "chart":
            klines = await self._fetch_klines()
            if klines:
                png = await self._generate_chart(klines)
                if png:
                    await self._send_photo(png, caption=f"{settings.SYMBOL} | Chart", chat_id=cid)

    # ─── Polling ───────────────────────────────────────────────────────────

    async def _poll_updates(self):
        offset = 0
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
                params = {"offset": offset, "timeout": 30, "allowed_updates":
                          ["message", "callback_query"]}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params,
                                            timeout=aiohttp.ClientTimeout(total=35)) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(5)
                            continue
                        data = await resp.json()
                        if not data.get("ok"):
                            continue
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            if "message" in update:
                                msg = update["message"]
                                text = msg.get("text", "")
                                if text.startswith("/"):
                                    await self._handle_command(msg)
                                elif text:
                                    msg["text"] = text
                                    await self._handle_gemini_chat(msg)
                            elif "callback_query" in update:
                                await self._handle_callback(update["callback_query"])
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.warning("Polling error: %s", e)
                await asyncio.sleep(5)

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


def _format_premium_message(title: str, body: str) -> str:
    """Format a premium-style HTML message with automatic tag closing."""
    safe_title = _escape_html(str(title))
    safe_body = str(body)
    safe_body = _close_html_tags(safe_body)
    return (
        f"\U0001f30c <b>{safe_title}</b>\n\n"
        f"{safe_body}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>BB-450 Trading System</i> | \u23f0 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


def _close_html_tags(text: str) -> str:
    """Auto-close unclosed HTML tags to prevent Telegram parse_mode errors."""
    tags = []
    i = 0
    while i < len(text):
        if text[i] == "<":
            close = text.find(">", i)
            if close == -1:
                break
            tag = text[i + 1:close]
            if tag.startswith("/"):
                if tags and tags[-1] == tag[1:]:
                    tags.pop()
            elif tag in ("b", "i", "code", "pre", "u", "s", "em", "strong"):
                tags.append(tag)
            i = close + 1
        else:
            i += 1
    for tag in reversed(tags):
        text += f"</{tag}>"
    return text


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def start_bot(chat_id: int, user_config: dict = None, queue: "queue.Queue" = None,
              exchange: object = None, gemini_client: object = None):
    """Start the Telegram bot in a daemon thread.  Non-blocking."""
    bot = TelegramBot(chat_id, user_config, queue, exchange, gemini_client)
    bot.start()
    return bot
