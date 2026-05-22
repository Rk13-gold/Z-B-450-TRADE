"""
BB-450 Telegram Bot
===================
Runs in a daemon thread, pushes real-time alerts and responds
to commands via the Telegram Bot API (raw HTTP, no extra deps).

Architecture:
  - Own asyncio event loop in a daemon thread
  - Receives dashboard snapshots via a thread-safe queue
  - Long-polls Telegram for commands (getUpdates)
  - Sends messages / inline keyboards proactively
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from queue import Queue, Empty
from typing import Optional

import aiohttp

from config.settings import settings

log = logging.getLogger("TelegramBot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
GEMINI_MODEL = "gemini-2.0-flash"


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight helper: inline keyboard builder
# ──────────────────────────────────────────────────────────────────────────────

def _btn(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def _row(*btns):
    return list(btns)


def _keyboard(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def _reply_kb(*rows, resize=True, persistent=True, one_time=False):
    """Build a ReplyKeyboardMarkup that sits at the bottom of the chat."""
    return {
        "keyboard": [[{"text": c} for c in r] for r in rows],
        "resize_keyboard": resize,
        "is_persistent": persistent,
        "one_time_keyboard": one_time,
        "input_field_placeholder": "Toca un botón..."
    }


# ──────────────────────────────────────────────────────────────────────────────
# Alert detector — fires once per threshold crossing
# ──────────────────────────────────────────────────────────────────────────────

class AlertState:
    def __init__(self):
        self.sent_crash = False
        self.sent_buy = False
        self.sent_sell = False
        self.sent_volume = False
        self.prev_rsi = 50

    def reset(self):
        self.sent_crash = False
        self.sent_buy = False
        self.sent_sell = False
        self.sent_volume = False


# ──────────────────────────────────────────────────────────────────────────────
# Main Bot
# ──────────────────────────────────────────────────────────────────────────────

class TelegramBot:
    """
    Telegram bot that runs in a background thread.

    Usage from dashboard::

        bot = TelegramBot()
        bot.start()
        while True:
            snapshot = {...}   # see push_update() docs
            bot.push_update(snapshot)
            time.sleep(1)
    """

    def __init__(self):
        self.token: str = settings.TELEGRAM_BOT_TOKEN
        self.chat_id: str = settings.TELEGRAM_CHAT_ID
        self.enabled: bool = settings.TELEGRAM_ENABLED

        self._queue: Queue = Queue(maxsize=5)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

        # ── State cache (latest dashboard snapshot) ──
        self._state: dict = {}
        self._price_history: deque = deque(maxlen=300)  # 5 min @ 1s
        self._last_signal: str = "WAIT"

        # ── Alert memory ──
        self._alerts: deque = deque(maxlen=50)
        self._alert_state = AlertState()
        self._user_config = {"crash": True, "buy_sell": True, "volume": True, "rsi": True}

        # ── Pending Telegram commands (handled in the async loop) ──
        self._last_update_id = 0

        # ── Gemini AI ──
        self._gemini_key: str = settings.GEMINI_API_KEY
        self._gemini_enabled: bool = bool(self._gemini_key)
        self._gemini_history: list = []

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def start(self):
        if not self.enabled:
            print("[TelegramBot] ⏹ DESACTIVADO — TELEGRAM_ENABLED=false en .env")
            return
        if not self.token:
            print("[TelegramBot] ⏹ DESACTIVADO — TELEGRAM_BOT_TOKEN vacío en .env")
            return
        if not self.chat_id:
            print("[TelegramBot] ⏹ DESACTIVADO — TELEGRAM_CHAT_ID vacío en .env")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TelegramBot")
        self._thread.start()
        print("[TelegramBot] 🟢 CONECTANDO...")
        # Start a watchdog that prints status every 30s
        def _watchdog():
            while self._running:
                time.sleep(30)
                if self._thread and self._thread.is_alive():
                    pass  # heartbeat is printed by polling
                else:
                    print("[TelegramBot] 🔴 THREAD MUERTO — reintentando...")
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
        """Thread-safe: feed latest dashboard data to the bot."""
        try:
            self._queue.put_nowait(snapshot)
        except Exception:
            pass  # drop if queue full

    # ──────────────────────────────────────────────────────────────────────────
    # Thread entry point
    # ──────────────────────────────────────────────────────────────────────────

    def _run_loop(self):
        while self._running:
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._main())
            except Exception as e:
                print(f"[TelegramBot] 🔴 Error fatal: {e} — reiniciando en 5s")
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
                print(f"[TelegramBot] 🔄 Reconectando... (chat_id={self.chat_id})")

    async def _main(self):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=65, sock_connect=15)
        ) as session:
            self._session = session
            await self._verify_connection()
            tasks = [
                asyncio.create_task(self._process_queue()),
                asyncio.create_task(self._poll_updates()),
                asyncio.create_task(self._alert_loop()),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _verify_connection(self):
        """Test the bot token against Telegram API and send a startup message."""
        try:
            url = API_BASE.format(token=self.token, method="getMe")
            async with self._session.get(url) as resp:
                data = await resp.json()
                if data.get("ok"):
                    bot_user = data["result"].get("username", "?")
                    print(f"[TelegramBot] ✅ Bot @{bot_user} autenticado correctamente")
                    print(f"[TelegramBot] 🟢 CONECTADO — enviando a chat_id={self.chat_id}")
                    if self._gemini_enabled:
                        print(f"[TelegramBot] 🤖 Gemini AI activado — escribe cualquier mensaje para chatear")
                    else:
                        print(f"[TelegramBot] ⚠️ Gemini AI desactivado — agrega GEMINI_API_KEY en .env")
                else:
                    print(f"[TelegramBot] ❌ Token inválido — {data}")
                    return
        except Exception as e:
            print(f"[TelegramBot] ❌ Error de conexión: {e}")
            return

        try:
            msg = (
                "\U0001f916 *BB-450 Bot Conectado* \U0001f916\n\n"
                "El dashboard está corriendo y monitoreando `{}`.\n\n"
                "Comandos:\n"
                "/start — Menú principal\n"
                "/info — Todos los indicadores\n"
                "/signal — Señal actual\n"
                "/alerts — Configurar alertas\n"
                "/status — Estado del bot\n"
                "/micro — Microestructura cuantitativa"
            ).format(settings.SYMBOL)
            await self._send(msg)
            print(f"[TelegramBot] 📨 Mensaje de bienvenida enviado al chat {self.chat_id}")
        except Exception as e:
            print(f"[TelegramBot] ⚠️ No se pudo enviar el mensaje de bienvenida: {e}")
            print(f"[TelegramBot] 💡 Verifica que TELEGRAM_CHAT_ID sea correcto")
            print(f"[TelegramBot] 💡 El usuario debe iniciar el bot con /start primero")

    # ──────────────────────────────────────────────────────────────────────────
    # Queue consumer — picks up dashboard snapshots
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_queue(self):
        while self._running:
            try:
                snapshot = self._queue.get(timeout=0.05)
                self._state = snapshot
                p = snapshot.get("price", 0)
                if p > 0:
                    self._price_history.append((time.time(), p))

                sig = snapshot.get("signal_text", "WAIT")
                if sig != self._last_signal:
                    self._last_signal = sig
                    if sig in ("LONG", "SHORT") and self._user_config.get("buy_sell", True):
                        await self._send_signal_alert(snapshot)
            except Empty:
                await asyncio.sleep(0.05)
                continue
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────────
    # Alert loop — runs every 5s, checks thresholds
    # ──────────────────────────────────────────────────────────────────────────

    async def _alert_loop(self):
        while self._running:
            await asyncio.sleep(5)
            if not self._state:
                continue
            try:
                await self._check_crash_alert()
                await self._check_volume_alert()
                await self._check_rsi_alert()
                await self._check_trend_alert()
                await self._check_signal_strength()
            except Exception:
                pass

    async def _check_crash_alert(self):
        if not self._user_config.get("crash", True):
            return
        if len(self._price_history) < 30:
            return
        now = time.time()
        recent = [p for t, p in self._price_history if t >= now - 60]
        if len(recent) < 10:
            return
        current = recent[-1]
        peak_60s = max(recent)
        drop_pct = (peak_60s - current) / max(peak_60s, 1) * 100
        if drop_pct >= settings.ALERT_CRASH_PCT and not self._alert_state.sent_crash:
            self._alert_state.sent_crash = True
            msg = (
                f"\U0001f4a5 *ALERTA: FLASH CRASH* \U0001f4a5\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Precio: `${current:,.0f}`\n"
                f"  Ca\u00edda: `{drop_pct:.1f}%` en 60s\n"
                f"  Pico: `${peak_60s:,.0f}`\n"
                f"\n\U0001f6a8 Revisar posiciones STOP-LOSS"
            )
            await self._send(msg)
        elif drop_pct < settings.ALERT_CRASH_PCT / 2:
            self._alert_state.sent_crash = False

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
            msg = (
                f"\U0001f4ca *ALERTA: VOLUMEN ANORMAL* \U0001f4ca\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Multiplicador: `{mult:.1f}x`\n"
                f"  Vol: `{vol:.2f}` | Avg: `{avg_vol:.2f}`\n"
                f"  Precio: `${self._state.get('price', 0):,.0f}`"
            )
            await self._send(msg)
        elif mult < settings.ALERT_VOLUME_SPIKE / 2:
            self._alert_state.sent_volume = False

    async def _check_rsi_alert(self):
        if not self._user_config.get("rsi", True):
            return
        rsi = self._state.get("rsi", 50)
        price = self._state.get("price", 0)
        if rsi >= settings.ALERT_RSI_OVERBOUGHT and self._alert_state.prev_rsi < settings.ALERT_RSI_OVERBOUGHT:
            msg = (
                f"\U0001f534 *RSI SOBRECOMPRA* \U0001f534\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  RSI: `{rsi:.1f}` > {settings.ALERT_RSI_OVERBOUGHT:.0f}\n"
                f"  Precio: `${price:,.0f}`\n"
                f"  \u2193 Posible reversi\u00f3n bajista"
            )
            await self._send(msg)
        elif rsi <= settings.ALERT_RSI_OVERSOLD and self._alert_state.prev_rsi > settings.ALERT_RSI_OVERSOLD:
            msg = (
                f"\U0001f7e2 *RSI SOBREVENTA* \U0001f7e2\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  RSI: `{rsi:.1f}` < {settings.ALERT_RSI_OVERSOLD:.0f}\n"
                f"  Precio: `${price:,.0f}`\n"
                f"  \u2191 Posible rebote alcista"
            )
            await self._send(msg)
        self._alert_state.prev_rsi = rsi

    async def _check_trend_alert(self):
        """Alert on trend changes detected by the dashboard."""
        trend = self._state.get("trend", "NEUTRAL")
        sig = self._state.get("signal_text", "WAIT")
        if not hasattr(self, '_prev_trend'):
            self._prev_trend = trend
            self._prev_sig_check = sig
            return
        if trend != self._prev_trend:
            self._prev_trend = trend
            emoji = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f534"
            msg = (
                f"{emoji} *CAMBIO DE TENDENCIA* {emoji}\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Nueva tendencia: `{trend}`\n"
                f"  Precio: `${self._state.get('price', 0):,.0f}`\n"
                f"  RSI: `{self._state.get('rsi', 50):.1f}`"
            )
            await self._send(msg)

    async def _check_signal_strength(self):
        """Alert when signal confidence crosses thresholds."""
        conf = self._state.get("confidence", 0)
        sig = self._state.get("signal_text", "WAIT")
        if not hasattr(self, '_prev_conf'):
            self._prev_conf = conf
            return
        diff = abs(conf - self._prev_conf)
        if diff >= 20 and sig in ("LONG", "SHORT") and self._user_config.get("buy_sell", True):
            emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f534"
            msg = (
                f"{emoji} *SE\u00d1AL FORTALECIDA* {emoji}\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Se\u00f1al: `{sig}` | Confianza: `{conf:.0f}%`\n"
                f"  Delta: `{self._state.get('delta', 0):.0f}`\n"
                f"  Precio: `${self._state.get('price', 0):,.0f}`"
            )
            await self._send(msg)
        self._prev_conf = conf

    async def _send_signal_alert(self, snapshot):
        direction = snapshot.get("signal_text", "WAIT")
        price = snapshot.get("price", 0)
        conf = snapshot.get("confidence", 0)
        rsi = snapshot.get("rsi", 50)
        delta = snapshot.get("delta", 0)
        trend = snapshot.get("trend", "NEUTRAL")

        if direction == "LONG":
            msg = (
                f"\U0001f7e2 *SE\u00d1AL DE COMPRA* \U0001f7e2\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Direcci\u00f3n: `LONG` | Conf: `{conf:.0f}%`\n"
                f"  Precio: `${price:,.0f}` | Trend: `{trend}`\n"
                f"  RSI: `{rsi:.1f}` | Delta: `{delta:.0f}`\n"
                f"\n\U0001f4a1 Evaluar entrada con SL t\u00e9cnico"
            )
        elif direction == "SHORT":
            msg = (
                f"\U0001f534 *SE\u00d1AL DE VENTA* \U0001f534\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Direcci\u00f3n: `SHORT` | Conf: `{conf:.0f}%`\n"
                f"  Precio: `${price:,.0f}` | Trend: `{trend}`\n"
                f"  RSI: `{rsi:.1f}` | Delta: `{delta:.0f}`\n"
                f"\n\U0001f4a1 Evaluar entrada con SL t\u00e9cnico"
            )
        else:
            return
        await self._send(msg)

    # ──────────────────────────────────────────────────────────────────────────
    # Telegram API helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _api_call(self, method, **kwargs):
        url = API_BASE.format(token=self.token, method=method)
        for attempt in range(3):
            try:
                async with self._session.post(url, json=kwargs) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        log.warning(f"Telegram API {method}: {resp.status} {text}")
                    return await resp.json()
            except asyncio.TimeoutError:
                print(f"[TelegramBot] ⏱ Timeout en {method} (intento {attempt+1}/3)")
                if attempt < 2:
                    await asyncio.sleep(1.5 ** attempt)
                continue
            except aiohttp.ClientError as e:
                print(f"[TelegramBot] 🔌 Error red en {method}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.5 ** attempt)
                continue
            except Exception as e:
                print(f"[TelegramBot] ❌ Error inesperado en {method}: {type(e).__name__}: {e}")
                return {"ok": False, "description": str(e)}
        print(f"[TelegramBot] ❌ {method} falló tras 3 intentos")
        return {"ok": False, "description": "max retries"}

    async def _typing(self):
        """Show 'bot is typing...' indicator below the bot name in Telegram."""
        if not self._session:
            return
        url = API_BASE.format(token=self.token, method="sendChatAction")
        try:
            await self._session.post(url, json={"chat_id": self.chat_id, "action": "typing"})
        except Exception:
            pass

    async def _typing_for(self, coro):
        """Keep typing indicator active while a coroutine runs."""
        async def _keep_typing():
            while True:
                await self._typing()
                await asyncio.sleep(4)
        task = asyncio.create_task(_keep_typing())
        try:
            return await coro
        finally:
            task.cancel()

    async def _send(self, text: str, kb=None, parse_mode="Markdown"):
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if kb:
            payload["reply_markup"] = kb
        result = await self._api_call("sendMessage", **payload)
        if not result.get("ok"):
            desc = result.get("description", "")
            # If Markdown parsing failed, retry as plain text
            if parse_mode == "Markdown" and "can't parse entities" in desc.lower():
                payload.pop("parse_mode", None)
                payload["text"] = text
                result = await self._api_call("sendMessage", **payload)
                if not result.get("ok"):
                    desc = result.get("description", "?")
                    print(f"[TelegramBot] ⚠️ sendMessage falló incluso como texto plano: {desc}")
            else:
                print(f"[TelegramBot] ⚠️ sendMessage falló: {desc}")
                if "chat not found" in desc.lower():
                    print(f"[TelegramBot] 💡 TELEGRAM_CHAT_ID={self.chat_id} — el usuario debe enviar /start al bot primero")
        return result.get("ok", False)

    async def _edit(self, chat_id, message_id, text: str, kb=None, parse_mode="Markdown"):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if kb:
            payload["reply_markup"] = kb
        await self._api_call("editMessageText", **payload)

    async def _answer_cb(self, cb_id, text=""):
        await self._api_call("answerCallbackQuery", callback_query_id=cb_id, text=text)

    # ──────────────────────────────────────────────────────────────────────────
    # Gemini AI — trading advisor
    # ──────────────────────────────────────────────────────────────────────────

    def _build_gemini_prompt(self, user_text: str) -> list:
        s = self._state
        trend_5m = s.get("trend_5m", "WAIT")
        trend_15m = s.get("trend_15m", "WAIT")
        trend_1h = s.get("trend_1h", "WAIT")
        market_context = (
            f"── MERCADO ──\n"
            f"Symbol: {s.get('symbol', 'BTCUSDT')} | Price: ${s.get('price', 0):,.0f}\n"
            f"Change: {s.get('change_pct', 0):+.2f}% | VWAP Dist: {s.get('price_vwap_dist', 0):+.2f}%\n"
            f"Day High: ${s.get('day_high', 0):,.0f} | Day Low: ${s.get('day_low', 0):,.0f}\n"
            f"\n"
            f"── TENDENCIA ──\n"
            f"Trend: {s.get('trend', 'NEUTRAL')} | Señal: {s.get('signal_text', 'WAIT')} ({s.get('confidence', 0):.0f}%)\n"
            f"MTF: 5m={trend_5m} | 15m={trend_15m} | 1h={trend_1h}\n"
            f"Confluence: {s.get('confluence_score', 0):.0f}% | Macro: {s.get('global_macro', 'NEUTRAL')}\n"
            f"EMA Cross 5m: {s.get('ema_cross_5m', 'NEUTRAL')} | 15m: {s.get('ema_cross_15m', 'NEUTRAL')}\n"
            f"\n"
            f"── TÉCNICOS ──\n"
            f"RSI: {s.get('rsi', 50):.1f} (5m: {s.get('rsi_5m', 0):.1f} | 15m: {s.get('rsi_15m', 0):.1f})\n"
            f"MACD: {s.get('macd', 0):.4f} | Signal: {s.get('macd_signal', 0):.4f} | Hist: {s.get('macd_hist', 0):.4f}\n"
            f"BB Pos: {s.get('bb_position', 50):.1f}% | BB Squeeze: {s.get('bb_squeeze', 'NORMAL')}\n"
            f"ATR: ${s.get('atr', 0):.2f} | EMA20: ${s.get('ema_20', 0):,.0f} | EMA50: ${s.get('ema_50', 0):,.0f}\n"
            f"\n"
            f"── ORDER FLOW ──\n"
            f"Delta: {s.get('delta', 0):+.0f} | CVD: {s.get('cvd', 0):+.0f}\n"
            f"Buy Vol: {s.get('buy_volume', 0):.1f} | Sell Vol: {s.get('sell_volume', 0):.1f}\n"
            f"B/A Ratio: {s.get('ba_ratio', 1):.3f} | Imbalance: {s.get('imbalance', 0):+.3f}\n"
            f"Force: {s.get('force', 'NONE')}\n"
            f"\n"
            f"── MICROESTRUCTURA ──\n"
            f"Kaufman Eff: {s.get('kaufman_eff', 0.5):.2f} | Tick Speed: {s.get('tick_speed', 0):.1f}/s\n"
            f"Spread Vel: {s.get('spread_velocity', 0):.1f}ms | Cancel Rate: {s.get('cancel_rate', 0):.1f}%\n"
            f"Skewness: {s.get('skewness', 0):.3f} | PINAM: {s.get('pinam', 0):.3f}\n"
            f"\n"
            f"── LIQUIDEZ ──\n"
            f"Depth Imb: {s.get('depth_imb_pct', 0):+.1f}%\n"
            f"Wall Bid: ${s.get('wall_bid', 0):,.0f} ({s.get('wall_bid_size', 0):.2f} BTC)\n"
            f"Wall Ask: ${s.get('wall_ask', 0):,.0f} ({s.get('wall_ask_size', 0):.2f} BTC)\n"
            f"Liq Zones: {s.get('liq_zones', 0)}\n"
            f"\n"
            f"── AI ──\n"
            f"AI Signal: {s.get('ai_signal', 'NINGUNA')} | Final: {s.get('ai_final', 'WAIT')}\n"
            f"Score OF: {s.get('ai_score_of', 0):.1f} | Mom: {s.get('ai_score_mom', 0):.1f} | Trend: {s.get('ai_score_trend', 0):.1f}\n"
            f"Win Rate: {s.get('ai_win_rate', 0):.1f}%\n"
            f"Risk: {s.get('ai_risk_status', 'WAITING')} | Trigger: ${s.get('ai_trigger', 0):,.0f}\n"
            f"SL: ${s.get('ai_sl', 0):,.0f} | TP1: ${s.get('ai_tp1', 0):,.0f} | TP2: ${s.get('ai_tp2', 0):,.0f}\n"
            f"\n"
            f"Timestamp: {s.get('timestamp', '—')}\n"
        )
        system = (
            "Eres un trader profesional de criptomonedas con 15 años de experiencia. "
            "Analiza data de mercado en tiempo real y responde en español. "
            "Sé directo, técnico y accionable. "
            "1) Indica señal clara: LONG / SHORT / WAIT con nivel de convicción (bajo/medio/alto). "
            "2) Fundamenta con los datos: RSI, MACD, Order Flow, Microestructura, MTF. "
            "3) Si recomiendas operar, incluye: entry, stop-loss, take-profit 1 y 2. "
            "4) Menciona el marco temporal predominante (5m/15m/1h). "
            "5) Advertencia de riesgo estándar al final."
        )
        contents = [{"role": "user", "parts": [{"text": system + "\n\nDATOS DEL MERCADO:\n" + market_context}]}]
        for entry in self._gemini_history[-10:]:
            contents.append({"role": entry["role"], "parts": [{"text": entry["text"]}]})
        contents.append({"role": "user", "parts": [{"text": user_text}]})
        return contents

    async def _chat_gemini(self, user_text: str) -> str:
        if not self._gemini_enabled:
            return (
                "\u26a0\ufe0f Gemini no configurado.\n"
                "Agrega `GEMINI_API_KEY=tu_api_key` en el archivo `.env`\n"
                "y obtén la key en https://aistudio.google.com/app/apikey"
            )
        url = GEMINI_BASE.format(model=GEMINI_MODEL, key=self._gemini_key)
        payload = {
            "contents": self._build_gemini_prompt(user_text),
            "generationConfig": {
                "maxOutputTokens": 1024,
                "temperature": 0.3,
            }
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"\u26a0\ufe0f Error Gemini ({resp.status}): {text[:200]}"
                data = await resp.json()
                candidate = data.get("candidates", [{}])[0]
                reply = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
                if not reply:
                    return "\u26a0\ufe0f Gemini no gener\u00f3 respuesta"
                self._gemini_history.append({"role": "user", "text": user_text})
                self._gemini_history.append({"role": "model", "text": reply})
                return reply
        except Exception as e:
            return f"\u26a0\ufe0f Error de conexi\u00f3n con Gemini: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # Command / button handlers
    # ──────────────────────────────────────────────────────────────────────────

    async def _cmd_start(self):
        kb = _keyboard(
            _row(_btn("\U0001f4ca Info", "info")),
            _row(_btn("\U0001f4e1 Signal", "signal"), _btn("\U0001f514 Alerts", "alerts")),
            _row(_btn("\U0001f504 Refresh", "refresh"), _btn("\U0001f916 AI", "ai")),
        )
        msg = (
            "\U0001f916 *BB-450 Trading Bot*\n\n"
            "Monitoreo en tiempo real de `{}`\n\n"
            "Usa los botones o comandos:\n"
            "/info — Todos los indicadores\n"
            "/signal — Señal actual\n"
            "/alerts — Configurar alertas\n"
            "/status — Estado del bot\n"
            "/config — Ajustes\n"
            "/ai — Consultar a Gemini AI"
        ).format(settings.SYMBOL)
        await self._send(msg, kb=kb)

        # Persistent reply keyboard at the bottom of the chat
        rkb = _reply_kb(
            ("\U0001f4ca Info", "\U0001f4e1 Signal", "\U0001f514 Alertas"),
            ("\U0001f9f9 Estado", "\U0001f4c8 Micro", "\U0001f916 AI"),
            ("\U0001f504 Refresh", "\U0001f3b5 \u00daltimo"),
        )
        await self._send(
            "\U0001f447 *Botones r\u00e1pidos* \u2014 toca para consultar datos en tiempo real",
            kb=rkb,
            parse_mode="Markdown",
        )

    async def _cmd_info(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f *Sin datos* — esperando actualización...")
            return
        if not s.get("klines_ready"):
            cnt = s.get("klines_count", 0)
            await self._send(f"\u23f3 *Cargando indicadores...* ({cnt}/50 klines)\nEsperando datos de mercado...")
            return

        p = s.get('price', 0); chg = s.get('change_pct', 0); trend = s.get('trend', 'NEUTRAL')
        sig = s.get('signal_text', 'WAIT'); conf = s.get('confidence', 0)
        rsi = s.get('rsi', 50); macd = s.get('macd', 0); mh = s.get('macd_hist', 0)
        bb = s.get('bb_position', 50); atr = s.get('atr', 0)
        delta = s.get('delta', 0); cvd = s.get('cvd', 0); bv = s.get('buy_volume', 0); sv = s.get('sell_volume', 0)
        imb = s.get('imbalance', 0); ke = s.get('kaufman_eff', 0.5); ts = s.get('tick_speed', 0)
        trend_5m = s.get('trend_5m', 'WAIT'); trend_1h = s.get('trend_1h', 'WAIT')
        conf_score = s.get('confluence_score', 0); macro = s.get('global_macro', 'NEUTRAL')
        dh = s.get('day_high', 0); dl = s.get('day_low', 0); vwap = s.get('vwap', 0)

        te = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f534" if trend == "BAJISTA" else "\U0001f7e1"
        se = "\U0001f7e2" if sig == "LONG" else "\U0001f534" if sig == "SHORT" else "\u26ab"
        de = "\U0001f7e2" if delta > 0 else "\U0001f534" if delta < 0 else "\u26ab"
        rsi_e = "\U0001f7e2" if rsi < 40 else "\U0001f534" if rsi > 60 else "\U0001f7e1"
        bb_e = "\U0001f7e2" if bb < 30 else "\U0001f534" if bb > 70 else "\U0001f7e1"

        lines = [
            f"\U0001f3b5 *{settings.SYMBOL}* \u2014 *AN\u00c1LISIS COMPLETO* \U0001f3b5",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"*PRECIO* \U0001f4b0",
            f"  \u25b6 `${p:,.0f}` | \U0001f4c8 `{chg:+.2f}%` | High `${dh:,.0f}` Low `${dl:,.0f}`",
            f"  VWAP `${vwap:,.0f}`",
            f"",
            f"*TENDENCIA* {te}",
            f"  Trend: `{trend}` | Macro: `{macro}`",
            f"  MTF: 5m `{trend_5m}` | 1h `{trend_1h}` | Conf `{conf_score:.0f}%`",
            f"  \U0001f514 Se\u00f1al: {se} `{sig}` ({conf:.0f}%)",
            f"",
            f"*INDICADORES T\u00c9CNICOS* \U0001f4ca",
            f"  RSI {rsi_e} `{rsi:.1f}` | MACD `{macd:.4f}` Hist `{mh:.4f}`",
            f"  BB {bb_e} `{bb:.1f}%` | ATR `${atr:.2f}`",
            f"",
            f"*ORDER FLOW* \U0001f4c8",
            f"  Delta {de} `{delta:.0f}` | CVD `{cvd:.0f}`",
            f"  Buy `{bv:.1f}` / Sell `{sv:.1f}` | Imb `{imb:+.3f}`",
            f"",
            f"*MICROESTRUCTURA* \U0001f52e",
            f"  Kaufman `{ke:.2f}` | Tick `{ts:.1f}/s`",
            f"",
            f"\u23f0 `{s.get('timestamp', '')}`",
        ]
        await self._send("\n".join(lines))

    async def _cmd_signal(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f Sin datos")
            return
        if not s.get("klines_ready"):
            cnt = s.get("klines_count", 0)
            await self._send(f"\u23f3 *Cargando...* ({cnt}/50 klines)")
            return

        direction = s.get("signal_text", "WAIT")
        conf = s.get("confidence", 0); price = s.get("price", 0)
        delta = s.get("delta", 0); cvd = s.get("cvd", 0); rsi = s.get("rsi", 50)
        trend = s.get("trend", "NEUTRAL"); trend_5m = s.get("trend_5m", "WAIT")
        trend_1h = s.get("trend_1h", "WAIT"); ke = s.get("kaufman_eff", 0.5)

        if direction == "LONG":
            emoji, col, dir_arrow = "\U0001f7e2", "\U0001f7e2", "\U0001f847"
        elif direction == "SHORT":
            emoji, col, dir_arrow = "\U0001f534", "\U0001f534", "\U0001f846"
        else:
            emoji, col, dir_arrow = "\u26ab", "\u26ab", "\u23f1\ufe0f"

        lines = [
            f"{emoji} *SE\u00d1AL: {direction}* {emoji}",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"  Precio: `${price:,.0f}`",
            f"  Confianza: `{conf:.0f}%` | Trend: `{trend}`",
            f"  MTF: 5m `{trend_5m}` / 1h `{trend_1h}`",
            f"",
            f"  RSI: `{rsi:.1f}` | Delta: `{delta:.0f}` | CVD: `{cvd:.0f}`",
            f"  Kaufman: `{ke:.2f}`",
            f"",
            f"\u23f0 `{s.get('timestamp', '')}`",
        ]
        await self._send("\n".join(lines))

    async def _cmd_alerts(self):
        crash_on = self._user_config.get("crash", True)
        bs_on = self._user_config.get("buy_sell", True)
        vol_on = self._user_config.get("volume", True)
        rsi_on = self._user_config.get("rsi", True)

        kb = _keyboard(
            _row(_btn(f"{'\u2705' if crash_on else '\u274c'} Crash {settings.ALERT_CRASH_PCT:.0f}%", "tog_crash")),
            _row(_btn(f"{'\u2705' if bs_on else '\u274c'} Buy/Sell", "tog_buysell")),
            _row(_btn(f"{'\u2705' if vol_on else '\u274c'} Volume Spike", "tog_volume")),
            _row(_btn(f"{'\u2705' if rsi_on else '\u274c'} RSI Extremes", "tog_rsi")),
            _row(_btn("\U0001f504 Refresh", "refresh"), _btn("\U0001f519 Back", "start")),
        )
        msg = (
            "*Configuraci\u00f3n de Alertas*\n\n"
            "Toca cada opci\u00f3n para activar/desactivar:\n\n"
            f"{'\u2705' if crash_on else '\u274c'} Crash: ca\u00edda > {settings.ALERT_CRASH_PCT:.0f}% en 60s\n"
            f"{'\u2705' if bs_on else '\u274c'} Buy/Sell: nueva se\u00f1al LONG o SHORT\n"
            f"{'\u2705' if vol_on else '\u274c'} Volumen: pico > {settings.ALERT_VOLUME_SPIKE:.0f}x promedio\n"
            f"{'\u2705' if rsi_on else '\u274c'} RSI: sobrecompra > {settings.ALERT_RSI_OVERBOUGHT:.0f} / sobreventa < {settings.ALERT_RSI_OVERSOLD:.0f}"
        )
        await self._send(msg, kb=kb)

    async def _cmd_micro(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f *Sin datos*")
            return
        ke = s.get("kaufman_eff", 0.5); sv = s.get("spread_velocity", 0)
        ts = s.get("tick_speed", 0); cr = s.get("cancel_rate", 0)
        sk = s.get("skewness", 0); pn = s.get("pinam", 0)
        force = s.get("force", "NONE"); bb_sq = s.get("bb_squeeze", "NORMAL")
        delta = s.get("delta", 0); imb = s.get("imbalance", 0)
        depth = s.get("depth_imb_pct", 0); lz = s.get("liq_zones", 0)
        f_e = "\U0001f7e2" if force == "BUY" else "\U0001f534" if force == "SELL" else "\u26ab"
        sq_e = "\U0001f7e2" if bb_sq == "SQUEEZE" else "\u26ab"

        lines = [
            f"\U0001f52e *MICROESTRUCTURA CUANTITATIVA* \U0001f52e",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"  Kaufman Eff: `{ke:.2f}`",
            f"  Tick Speed: `{ts:.1f} t/s`",
            f"  Spread Vel: `{sv:.1f} ms`",
            f"  Cancel Rate: `{cr:.1f}%`",
            f"  Skewness: `{sk:.3f}`",
            f"  PINAM: `{pn:.3f}`",
            f"",
            f"  Fuerza {f_e}: `{force}` | BB Squeeze {sq_e}: `{bb_sq}`",
            f"",
            f"*LIQUIDEZ*",
            f"  Delta: `{delta:.0f}` | Imbalance: `{imb:+.3f}`",
            f"  Depth Imb: `{depth:+.1f}%` | Liq Zones: `{lz}`",
            f"",
            f"\u23f0 `{s.get('timestamp', '')}`",
        ]
        await self._send("\n".join(lines))

    async def _cmd_status(self):
        s = self._state
        uptime = s.get("uptime", 0); updates = s.get("update_count", 0)
        latency = s.get("latency_ms", 0); price = s.get("price", 0)
        klines = s.get("klines_count", 0); ready = s.get("klines_ready", False)
        ai = s.get("ai_win_rate", 0); gem = "\u2705" if self._gemini_enabled else "\u274c"
        connected = "\u2705" if price > 0 else "\u274c"

        h, r = divmod(int(uptime), 3600); m, s_sec = divmod(r, 60)
        uptime_str = f"{h}h {m}m {s_sec}s" if h > 0 else f"{m}m {s_sec}s"

        lines = [
            f"\U0001f9f9 *ESTADO DEL SISTEMA* \U0001f9f9",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"  Conexi\u00f3n: {connected} Binance API",
            f"  Gemini: {gem} {'Activo' if self._gemini_enabled else 'Desactivado'}",
            f"  Symbol: `{settings.SYMBOL}`",
            f"  Precio: `${price:,.0f}`",
            f"",
            f"  Klines: `{klines}` {'\u2705' if ready else '\u23f3'}",
            f"  Updates: `{updates}` | Latencia: `{latency}ms`",
            f"  Uptime: `{uptime_str}`",
            f"  AI Win Rate: `{ai:.1f}%`",
            f"",
            f"  Alertas: {sum(1 for v in self._user_config.values() if v)}/4 activas",
        ]
        kb = _keyboard(_row(_btn("\U0001f504 Refresh", "refresh"), _btn("\U0001f519 Back", "start")))
        await self._send("\n".join(lines), kb=kb)

    # ──────────────────────────────────────────────────────────────────────────
    # Long-poll for updates (commands + callback queries)
    # ──────────────────────────────────────────────────────────────────────────

    async def _poll_updates(self):
        heartbeat = 0
        while self._running:
            try:
                url = API_BASE.format(token=self.token, method="getUpdates")
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                }
                print(f"[TelegramBot] ⏳ Esperando comandos... (offset={self._last_update_id})", end="\r")
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("result", [])
                        if results:
                            print(f"\n[TelegramBot] ↻ {len(results)} actualizaciones recibidas")
                            for upd in data.get("result", []):
                                self._last_update_id = upd["update_id"]
                                await self._handle_update(upd)
                        print(f"[TelegramBot] ✓ Poll ok — esperando siguientes... (offset={self._last_update_id})")
                    elif resp.status == 409:
                        print(f"\n[TelegramBot] ⚠️ 409 Conflict — otra instancia de polling activa?")
                        await asyncio.sleep(2)
                    elif resp.status >= 400:
                        text = await resp.text()
                        print(f"\n[TelegramBot] ⚠️ HTTP {resp.status}: {text[:120]}")
                        await asyncio.sleep(10)
                heartbeat += 1
            except asyncio.TimeoutError:
                heartbeat += 1
                print(f"[TelegramBot] ⏱ Timeout 25s — re-pol...", end="\r")
                continue
            except Exception as e:
                print(f"\n[TelegramBot] ❌ Error en polling: {e}")
                log.warning(f"Poll error: {e}")
                await asyncio.sleep(5)

    async def _handle_update(self, upd: dict):
        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return
        msg = upd.get("message")
        if not msg:
            return
        chat_id = str(msg["chat"]["id"])
        if chat_id != self.chat_id:
            # Silently ignore unauthorized users (don't reveal existence)
            return
        text = msg.get("text", "")
        print(f"[TelegramBot] 📩 Comando recibido: \"{text[:60]}\"")
        if text.startswith("/"):
            cmd = text
        elif "\U0001f4ca" in text and "Info" in text:
            cmd = "/info"
        elif "\U0001f4e1" in text and "Signal" in text:
            cmd = "/signal"
        elif "\U0001f514" in text and "Alertas" in text:
            cmd = "/alerts"
        elif "\U0001f9f9" in text and "Estado" in text:
            cmd = "/status"
        elif "\U0001f4c8" in text and "Micro" in text:
            cmd = "/micro"
        elif "\U00002699" in text and "Config" in text:
            cmd = "/config"
        elif "\U0001f504" in text and "Refresh" in text:
            cmd = "/refresh"
        elif "\U0001f3b5" in text or "\u00daltimo" in text:
            cmd = "/ultimo"
        elif "\U0001f916" in text and "AI" in text:
            cmd = "/ai"
        else:
            cmd = text

        try:
            await self._typing()
            if cmd == "/start":
                await self._cmd_start()
            elif cmd == "/info":
                await self._cmd_info()
            elif cmd == "/signal":
                await self._cmd_signal()
            elif cmd == "/alerts":
                await self._cmd_alerts()
            elif cmd == "/status":
                await self._cmd_status()
            elif cmd == "/config":
                await self._cmd_alerts()
            elif cmd == "/micro":
                await self._cmd_micro()
            elif cmd == "/ai" or cmd.startswith("/ai "):
                user_q = text[4:].strip() if cmd.startswith("/ai ") else ""
                if not user_q:
                    await self._send(
                        "\U0001f916 *Gemini AI Trader*\n\n"
                        "Uso: `/ai tu pregunta`\n"
                        "o simplemente escribe cualquier mensaje y Gemini responder\u00e1.\n\n"
                        "Ej: `/ai deber\u00eda abrir LONG ahora?`"
                    )
                else:
                    reply = await self._typing_for(self._chat_gemini(user_q))
                    await self._send(reply, parse_mode=None)
            elif cmd == "/refresh":
                s = self._state
                sig = s.get("signal_text", "WAIT")
                conf = s.get("confidence", 0)
                price = s.get("price", 0)
                rsi = s.get("rsi", 50)
                trend = s.get("trend", "NEUTRAL")
                msg = (
                    f"\U0001f504 *Actualizado*\n"
                    f"Precio: `${price:,.0f}` | Trend: `{trend}`\n"
                    f"Señal: `{sig}` ({conf:.0f}%)\n"
                    f"RSI: `{rsi:.1f}`"
                )
                await self._send(msg)
            elif cmd == "/ultimo":
                s = self._state
                price = s.get("price", 0)
                chg = s.get("change_pct", 0)
                trend = s.get("trend", "NEUTRAL")
                sig = s.get("signal_text", "WAIT")
                arrow = "\U0001f7e2\U0001f846" if chg >= 0 else "\U0001f534\U0001f847"
                msg = (
                    f"\U0001f3b5 *{settings.SYMBOL}* {arrow} `${price:,.0f}`\n"
                    f"Cambio: `{chg:+.2f}%` | Trend: `{trend}` | Señal: `{sig}`"
                )
                await self._send(msg)
            else:
                reply = await self._typing_for(self._chat_gemini(text))
                if reply:
                    await self._send(reply, parse_mode=None)
        except Exception as e:
            print(f"[TelegramBot] ❌ Error al procesar comando \"{text}\": {e}")
            log.exception("Command handler error")

    async def _handle_callback(self, cb: dict):
        cid = cb["message"]["chat"]["id"]
        mid = cb["message"]["message_id"]
        data = cb["data"]
        cb_id = cb["id"]

        if data == "start":
            await self._answer_cb(cb_id)
            await self._cmd_start()
        elif data == "info":
            await self._answer_cb(cb_id)
            await self._cmd_info()
        elif data == "signal":
            await self._answer_cb(cb_id)
            await self._cmd_signal()
        elif data == "alerts":
            await self._answer_cb(cb_id)
            await self._cmd_alerts()
        elif data == "refresh":
            await self._answer_cb(cb_id, "\U0001f504 Actualizando...")
            s = self._state
            sig = s.get("signal_text", "WAIT")
            conf = s.get("confidence", 0)
            price = s.get("price", 0)
            rsi = s.get("rsi", 50)
            trend = s.get("trend", "NEUTRAL")
            new_text = (
                f"\U0001f504 *Actualizado*\n"
                f"Precio: `${price:,.0f}` | Trend: `{trend}`\n"
                f"Señal: `{sig}` ({conf:.0f}%)\n"
                f"RSI: `{rsi:.1f}`"
            )
            await self._edit(cid, mid, new_text)
        elif data == "config":
            await self._answer_cb(cb_id)
            await self._cmd_alerts()
        elif data == "ai":
            await self._answer_cb(cb_id, "Escribe tu pregunta para Gemini")
        elif data.startswith("tog_"):
            key = data.replace("tog_", "")
            if key == "crash":
                self._user_config["crash"] = not self._user_config["crash"]
            elif key == "buysell":
                self._user_config["buy_sell"] = not self._user_config["buy_sell"]
            elif key == "volume":
                self._user_config["volume"] = not self._user_config["volume"]
            elif key == "rsi":
                self._user_config["rsi"] = not self._user_config["rsi"]
            await self._answer_cb(cb_id, "Configuración actualizada")
            await self._cmd_alerts()
