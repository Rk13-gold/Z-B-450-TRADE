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
import re
import unicodedata
from datetime import datetime, timezone
import io
import json
import logging
import os
import threading
import time
from collections import deque
from queue import Queue, Empty
from typing import Optional, Callable

import aiohttp

# matplotlib headless backend — must be set before any pyplot import
os.environ["MPLBACKEND"] = "Agg"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D

from config.settings import settings
from src.engine.binance_client import binance_client

log = logging.getLogger("TelegramBot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
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
        self.sent_pump = False
        self.sent_buy = False
        self.sent_sell = False
        self.sent_volume = False
        self.sent_whale = False
        self.prev_rsi = 50
        self.prev_cum_delta = 0.0

    def reset(self):
        self.sent_crash = False
        self.sent_pump = False
        self.sent_buy = False
        self.sent_sell = False
        self.sent_volume = False
        self.sent_whale = False


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
        self._user_config = {
            "crash": True, "buy_sell": True, "volume": True, "rsi": True,
            "capital": 100.0, "risk_pct": 1.0, "sl_pct": 0.5, "tp_pct": 1.5,
        }

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
                "\U0001f916 <b>BB-450 Bot Conectado</b> \U0001f916\n\n"
                "El dashboard está corriendo y monitoreando <code>{}</code>.\n\n"
                "<b>Comandos:</b>\n"
                "/start \u2014 Menú principal\n"
                "/info \u2014 Todos los indicadores\n"
                "/signal \u2014 Señal actual\n"
                "/alerts \u2014 Configurar notificaciones\n"
                "/status \u2014 Estado del bot\n"
                "/micro \u2014 Microestructura cuantitativa"
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
                        conf = snapshot.get("confidence", 0)
                        vol = snapshot.get("volume", 0)
                        avg_vol = snapshot.get("avg_volume", 0)
                        if conf > 75 and avg_vol > 0 and (vol / avg_vol) >= 1.5:
                            await self._send_signal_alert(snapshot)
                        else:
                            print(f"[TelegramBot] ╔ Señal débil filtrada: {sig} conf={conf:.0f}% vol_ratio={vol/max(avg_vol,0.001):.1f}x")
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
                await self._check_pump_alert()
                await self._check_volume_alert()
                await self._check_rsi_alert()
                await self._check_trend_alert()
                await self._check_signal_strength()
                await self._check_whale_alert()
                await self._check_trade_opportunity()
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
                f"\U0001f4a5 <b>ALERTA: FLASH CRASH</b> \U0001f4a5\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Precio: `${current:,.0f}`\n"
                f"  Ca\u00edda: `{drop_pct:.1f}%` en 60s\n"
                f"  Pico: `${peak_60s:,.0f}`\n"
                f"  Delta: `{self._state.get('delta', 0):+.0f}` | CVD: `{self._state.get('cvd', 0):.0f}`\n"
                f"  Vol: `{self._state.get('volume', 0):.1f}` | B/A: `{self._state.get('ba_ratio', 1):.2f}x`\n"
                f"\n\U0001f6a8 Revisar posiciones STOP-LOSS"
            )
            await self._send(msg)
        elif drop_pct < settings.ALERT_CRASH_PCT / 2:
            self._alert_state.sent_crash = False

    async def _check_pump_alert(self):
        """Alert on sharp upward movements (flash pump) - balances the crash alert."""
        if not self._user_config.get("crash", True):
            return
        if len(self._price_history) < 30:
            return
        now = time.time()
        recent = [p for t, p in self._price_history if t >= now - 60]
        if len(recent) < 10:
            return
        current = recent[-1]
        low_60s = min(recent)
        pump_pct = (current - low_60s) / max(low_60s, 1) * 100
        if pump_pct >= settings.ALERT_CRASH_PCT and not self._alert_state.sent_pump:
            self._alert_state.sent_pump = True
            msg = (
                f"\U0001f4a5 <b>ALERTA: FLASH PUMP</b> \U0001f4a5\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Precio: `${current:,.0f}`\n"
                f"  Subida: `{pump_pct:.1f}%` en 60s\n"
                f"  M\u00ednimo: `${low_60s:,.0f}`\n"
                f"\n\U0001f4a1 Evaluar toma de ganancias parciales"
            )
            await self._send(msg)
        elif pump_pct < settings.ALERT_CRASH_PCT / 2:
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
            msg = (
                f"\U0001f4ca <b>ALERTA: VOLUMEN ANORMAL</b> \U0001f4ca\n"
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
                f"\U0001f7e3 <b>RSI SOBRECOMPRA</b> \U0001f7e3\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  RSI: `{rsi:.1f}` > {settings.ALERT_RSI_OVERBOUGHT:.0f}\n"
                f"  Precio: `${price:,.0f}`\n"
                f"  \u2193 Posible reversi\u00f3n bajista"
            )
            await self._send(msg)
        elif rsi <= settings.ALERT_RSI_OVERSOLD and self._alert_state.prev_rsi > settings.ALERT_RSI_OVERSOLD:
            msg = (
                f"\U0001f7e2 <b>RSI SOBREVENTA</b> \U0001f7e2\n"
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
            emoji = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f7e3"
            msg = (
                f"{emoji} <b>CAMBIO DE TENDENCIA</b> {emoji}\n"
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
            emoji = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3"
            msg = (
                f"{emoji} <b>SE\u00d1AL FORTALECIDA</b> {emoji}\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"  Se\u00f1al: `{sig}` | Confianza: `{conf:.0f}%`\n"
                f"  Delta: `{self._state.get('delta', 0):.0f}`\n"
                f"  Precio: `${self._state.get('price', 0):,.0f}`"
            )
            await self._send(msg)
        self._prev_conf = conf

    async def _check_whale_alert(self):
        """Detect whale accumulation/distribution via cumulative delta acceleration."""
        s = self._state
        if not s:
            return
        if self._alert_state.sent_whale:
            if not hasattr(self, '_whale_cooldown'):
                self._whale_cooldown = 0
            self._whale_cooldown += 1
            if self._whale_cooldown >= 12:
                self._alert_state.sent_whale = False
                self._whale_cooldown = 0
            return

        cum_delta = s.get('cumulative_delta', 0)
        vol = s.get('volume', 0)
        delta = s.get('delta', 0)
        bv = s.get('buy_volume', 0)
        sv = s.get('sell_volume', 0)
        ts = s.get('tick_speed', 0)

        prev = self._alert_state.prev_cum_delta
        delta_accel = cum_delta - prev
        self._alert_state.prev_cum_delta = cum_delta

        is_whale = abs(delta_accel) > 100 and vol > 5 and ts > 30
        if not is_whale:
            return

        self._alert_state.sent_whale = True
        side = "\U0001f7e2 COMPRADORA" if delta_accel > 0 else "\U0001f7e3 VENDEDORA"
        emoji = "\U0001f7e2" if delta_accel > 0 else "\U0001f7e3"

        msg = (
            f"\U0001f40b <b>BALLENA {side}</b> \U0001f40b\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Delta acum: <code>{cum_delta:+.1f}</code> (\u0394 <code>{delta_accel:+.1f}</code>)\n"
            f"Vol: <code>{vol:.1f}</code> | B/A: <code>{bv:.0f}/{sv:.0f}</code>\n"
            f"Tick Speed: <code>{ts:.1f} t/s</code>\n"
            f"Precio: <code>${s.get('price', 0):,.0f}</code>\n"
            f"\u23f0 {s.get('timestamp', '')}"
        )
        await self._send(msg)

        klines = await self._fetch_klines()
        if klines:
            png = await self._generate_chart(klines)
            if png:
                await self._send_photo(
                    png,
                    caption=f"{emoji} Whale \u2014 {delta_accel:+.0f} \u0394 en 5s | ${s.get('price',0):,.0f}"
                )

    async def _check_trade_opportunity(self):
        """AI-powered trade confirmation. Fires when signal confidence > 75."""
        s = self._state
        if not s:
            return
        conf = s.get("confidence", 0)
        direction = s.get("signal_text", "WAIT")
        if conf < 75 or direction not in ("LONG", "SHORT"):
            return
        if not hasattr(self, '_last_trade_check'):
            self._last_trade_check = 0
        now = time.time()
        if now - self._last_trade_check < 120:
            return
        self._last_trade_check = now

        if not self._gemini_enabled:
            return

        price = s.get("price", 0)
        delta = s.get("delta", 0)
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
        sv = s.get("spread_velocity", 0)
        ts = s.get("tick_speed", 0)

        prompt = (
            f"Eres un trader profesional. Analiza si es seguro abrir una operacion.\n\n"
            f"SEÑAL: {direction} | CONFIANZA: {conf:.0f}%\n"
            f"PRECIO: ${price:,.0f}\n"
            f"DELTA: {delta:+.1f} | CVD: {cvd:+.1f} | CUM DELTA: {cum_delta:+.1f}\n"
            f"RSI: {rsi:.1f} | B/A RATIO: {ba:.3f}x | VOL: {vol:.1f}\n"
            f"PINAM: {pinam:.4f} | CANCEL RATE: {cancel:.1f}%\n"
            f"TREND 5M: {trend_5m} | TREND 1H: {trend_1h}\n"
            f"TICK: {ts:.1f}/s | SPREAD VEL: {sv:.1f}ms\n"
            f"WALL BID: {wall_bid_sz:.1f} BTC | WALL ASK: {wall_ask_sz:.1f} BTC\n\n"
            f"INSTRUCCIONES:\n"
            f"1) Decide si ENTRAR o NO ENTRAR.\n"
            f"2) Si es ENTRAR: da entry exacto, SL (por debajo de soporte), TP (1:2 riesgo/recompensa minimo).\n"
            f"3) Si es NO ENTRAR: explica por que (trampa, spoofing, poco volumen, divergencia).\n"
            f"4) Responde SOLO JSON con formato:\n"
            f"{{\"decision\":\"ENTRAR\"|\"NO_ENTRAR\",\"entry\":precio,\"sl\":precio,\"tp\":precio,\"razon\":\"texto\"}}"
        )

        try:
            reply = await self._chat_gemini_raw(prompt)
            import json as _json
            data = _json.loads(reply)
        except Exception:
            return

        decision = data.get("decision", "NO_ENTRAR")
        razon = data.get("razon", "Sin análisis")
        entry = data.get("entry", price)
        sl = data.get("sl", price * 0.995)
        tp = data.get("tp", price * 1.01)
        capital = self._user_config.get("capital", 100)
        sl_pct = abs((entry - sl) / entry) * 100
        tp_pct = abs((tp - entry) / entry) * 100

        emoji = "\U0001f7e2" if direction == "LONG" else "\U0001f7e3"
        if decision == "ENTRAR":
            lines = [
                f"{emoji} <b>OPORTUNIDAD CONFIRMADA POR AI</b> {emoji}",
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                f"",
                f"<b>DIRECCI\u00d3N:</b> {direction} | Confianza: <code>{conf:.0f}%</code>",
                f"<b>PRECIO:</b> <code>${entry:,.1f}</code>",
                f"",
                f"<b>\U0001f4b0 CAPITAL:</b> <code>${capital:.1f}</code> \u00d7 <code>{settings.LEVERAGE}x</code>",
                f"<b>\U0001f6a9 STOP LOSS:</b> <code>${sl:,.1f}</code> ({sl_pct:.1f}%)",
                f"<b>\U0001f4c8 TAKE PROFIT:</b> <code>${tp:,.1f}</code> ({tp_pct:.1f}%)",
                f"<b>R:R:</b> <code>1:{tp_pct/sl_pct:.1f}</code>",
                f"",
                f"<b>\U0001f4ac AI:</b> {razon}",
            ]
        else:
            lines = [
                f"\u26a0\ufe0f <b>AI RECOMIENDA NO ENTRAR</b> \u26a0\ufe0f",
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                f"",
                f"Se\u00f1al: <code>{direction}</code> ({conf:.0f}%) | Precio: <code>${price:,.0f}</code>",
                f"",
                f"<b>\U0001f4ac An\u00e1lisis AI:</b>",
                f"{razon}",
                f"",
                f"<i>Esperando mejor oportunidad...</i>",
            ]

        await self._send('\n'.join(lines))

        if decision == "ENTRAR":
            klines = await self._fetch_klines()
            if klines:
                png = await self._generate_chart(klines)
                if png:
                    await self._send_photo(png, caption=f"{emoji} {direction} confirmado por AI | SL ${sl:,.0f} TP ${tp:,.0f}")

    async def _chat_gemini_raw(self, prompt: str) -> str:
        """Send a raw prompt to Gemini without conversation history, return raw text."""
        if not self._gemini_enabled:
            return '{"decision":"NO_ENTRAR","razon":"Gemini no configurado"}'
        url = GEMINI_BASE.format(model=GEMINI_MODEL, key=self._gemini_key)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 512, "temperature": 0.1},
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return '{"decision":"NO_ENTRAR","razon":"Error AI"}'
                data = await resp.json()
                candidate = data.get("candidates", [{}])[0]
                return candidate.get("content", {}).get("parts", [{}])[0].get("text", '{"decision":"NO_ENTRAR","razon":"Sin respuesta"}')
        except Exception:
            return '{"decision":"NO_ENTRAR","razon":"Error de conexion"}'

    async def _send_signal_alert(self, snapshot):
        direction = snapshot.get("signal_text", "WAIT")
        conf = snapshot.get("confidence", 0)
        price = snapshot.get("price", 0)
        rsi = snapshot.get("rsi", 50)
        macd = snapshot.get("macd", 0); macd_sig = snapshot.get("macd_signal", 0); macd_h = snapshot.get("macd_hist", 0)
        delta = snapshot.get("delta", 0); cvd = snapshot.get("cvd", 0)
        bv = snapshot.get("buy_volume", 0); sv = snapshot.get("sell_volume", 0)
        vol = snapshot.get("volume", 0); ba = snapshot.get("ba_ratio", 1.0)
        trend = snapshot.get("trend", "NEUTRAL"); trend_5m = snapshot.get("trend_5m", "WAIT")
        trend_15m = snapshot.get("trend_15m", "WAIT")
        bb_pos = snapshot.get("bb_position", 50); bb_sq = snapshot.get("bb_squeeze", "NORMAL")
        ke = snapshot.get("kaufman_eff", 0.5)
        force = snapshot.get("force", "NONE")
        imb = snapshot.get("imbalance", 0); depth = snapshot.get("depth_imb_pct", 0)
        wall_bid = snapshot.get("wall_bid", 0); wall_bid_sz = snapshot.get("wall_bid_size", 0)
        wall_ask = snapshot.get("wall_ask", 0); wall_ask_sz = snapshot.get("wall_ask_size", 0)
        vwap = snapshot.get("vwap", 0); atr = snapshot.get("atr", 0)
        ema_20 = snapshot.get("ema_20", 0); ema_50 = snapshot.get("ema_50", 0)
        ai_final = snapshot.get("ai_final", "WAIT"); ai_conf = snapshot.get("ai_score_of", 0)
        ai_sl = snapshot.get("ai_sl", 0); ai_tp1 = snapshot.get("ai_tp1", 0); ai_tp2 = snapshot.get("ai_tp2", 0)

        if direction == "LONG":
            emoji, side_e = "\U0001f7e2", "\U0001f7e2"
        elif direction == "SHORT":
            emoji, side_e = "\U0001f7e3", "\U0001f7e3"
        else:
            return

        f_e = "\U0001f7e2" if force == "BUY" else "\U0001f7e3" if force == "SELL" else "\u26ab"
        sq_e = "\U0001f4a5" if bb_sq == "SQUEEZE" else "\u26ab"
        ai_e = "\U0001f7e2" if ai_final == "LONG" else "\U0001f7e3" if ai_final == "SHORT" else "\u26ab"

        lines = [
            f"{emoji} <b>SE\u00d1AL AUTOM\u00c1TICA: {direction}</b> {emoji}",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"<b>PRECIO</b> \U0001f4b0",
            f"  `${price:,.0f}` | VWAP `${vwap:,.0f}` | ATR `${atr:.1f}`",
            f"  EMA 20/50: `${ema_20:,.0f}` / `${ema_50:,.0f}`",
            f"  Confianza: `{conf:.0f}%` | Trend: `{trend}`",
            f"",
            f"<b>ORDER FLOW</b> \U0001f4c8",
            f"  Delta: `{delta:+.0f}` | CVD: `{cvd:.0f}` | B/A: `{ba:.2f}x`",
            f"  Buy: `{bv:.1f}` | Sell: `{sv:.1f}` | Vol: `{vol:.1f}`",
            f"  Imbalance: `{imb:+.3f}` | Depth: `{depth:+.1f}%`",
            f"",
            f"<b>INDICADORES</b> \U0001f4ca",
            f"  RSI: `{rsi:.1f}` | MACD: `{macd:.4f}` | Signal: `{macd_sig:.4f}`",
            f"  Hist: `{macd_h:.4f}` | BB Pos: `{bb_pos:.1f}%` {sq_e}",
            f"  Kaufman: `{ke:.2f}` | {f_e} Force: `{force}`",
            f"",
            f"<b>MTF</b> \U0001f3c6",
            f"  Trend: `{trend}` | 5m: `{trend_5m}` | 15m: `{trend_15m}`",
            f"",
            f"<b>WALLS</b> \U0001f3f0",
        ]

        if wall_bid_sz >= 2:
            lines.append(f"  \U0001f40b BID {wall_bid_sz:.1f}\u20bf @ `${wall_bid:,.0f}`")
        if wall_ask_sz >= 2:
            lines.append(f"  \U0001f40b ASK {wall_ask_sz:.1f}\u20bf @ `${wall_ask:,.0f}`")
        if wall_bid_sz < 2 and wall_ask_sz < 2:
            lines.append(f"  Sin muros significativos")

        lines += [
            f"",
            f"<b>AI ENGINE</b> {ai_e}",
            f"  Final: `{ai_final}` | Score: `{ai_conf:.1f}`",
        ]

        if ai_sl > 0:
            lines += [
                f"  SL: `${ai_sl:,.0f}` | TP1: `${ai_tp1:,.0f}` | TP2: `${ai_tp2:,.0f}`",
            ]

        lines += [
            f"",
            f"\u23f0 `{snapshot.get('timestamp', '')}`",
        ]

        await self._send("\n".join(lines))

        # Send chart with the signal
        klines = await self._fetch_klines()
        if klines:
            png = await self._generate_chart(klines)
            if png:
                caption = (
                    f"{emoji} *{direction}* ({conf:.0f}%) | "
                    f"RSI `{rsi:.1f}` | Delta `{delta:+.0f}` | Trend `{trend}`"
                )
                await self._send_photo(png, caption=caption)

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

    async def _send_photo(self, photo_bytes: bytes, caption: str = "", parse_mode="HTML"):
        """Send a photo using multipart/form-data upload."""
        url = API_BASE.format(token=self.token, method="sendPhoto")
        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field("photo", photo_bytes, filename="chart.png", content_type="image/png")
        if caption:
            data.add_field("caption", caption)
            if parse_mode:
                data.add_field("parse_mode", parse_mode)
        for attempt in range(3):
            try:
                async with self._session.post(url, data=data) as resp:
                    j = await resp.json()
                    if not j.get("ok"):
                        desc = j.get("description", "")
                        if "can't parse entities" in desc.lower() and parse_mode:
                            # retry without parse_mode
                            data2 = aiohttp.FormData()
                            data2.add_field("chat_id", self.chat_id)
                            data2.add_field("photo", photo_bytes, filename="chart.png", content_type="image/png")
                            data2.add_field("caption", caption)
                            async with self._session.post(url, data=data2) as resp2:
                                return (await resp2.json()).get("ok", False)
                        log.warning(f"sendPhoto falló: {desc}")
                    return j.get("ok", False)
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt < 2:
                    await asyncio.sleep(1.5 ** attempt)
                else:
                    log.warning(f"sendPhoto error tras 3 intentos: {e}")
                    return False

    async def _fetch_klines(self, symbol="BTCUSDT", interval="1m", limit=60):
        url = f"{BINANCE_FAPI}/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.warning(f"fetch_klines: {e}")
        return []

    async def _generate_chart(self, klines, show_indicators=True):
        """Professional candlestick chart with VWAP, EMA20, Bollinger Bands, volume.
        Returns PNG bytes."""
        if not klines or len(klines) < 5:
            return None
        times = [mdates.date2num(datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)) for k in klines]
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(10, 6),
            gridspec_kw={"height_ratios": [3, 1]},
            sharex=True,
        )
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#0d1117")
        axv.set_facecolor("#0d1117")
        ax.set_title(f"{settings.SYMBOL} \u2014 1m", color="#c9d1d9", fontsize=12, fontweight="bold")

        # ── Candlesticks ──
        width = (times[-1] - times[0]) / len(times) * 0.6 if len(times) > 1 else 60
        for i in range(len(times)):
            color = "#00e676" if closes[i] >= opens[i] else "#aa00ff"
            ax.add_patch(Rectangle(
                (times[i] - width / 2, opens[i]), width,
                closes[i] - opens[i],
                facecolor=color, edgecolor=color, linewidth=0.5, zorder=3,
            ))
            ax.plot([times[i], times[i]], [lows[i], highs[i]], color=color, linewidth=0.8, zorder=2)

        last = closes[-1]
        hh = max(highs)
        ll = min(lows)

        if show_indicators:
            # ── VWAP ──
            tp_v = [(h + l + c) / 3 * v for h, l, c, v in zip(highs, lows, closes, volumes)]
            cum_v = [sum(volumes[:i+1]) for i in range(len(volumes))]
            cum_tpv = [sum(tp_v[:i+1]) for i in range(len(tp_v))]
            vwap = [cum_tpv[i] / cum_v[i] if cum_v[i] > 0 else closes[i] for i in range(len(closes))]
            ax.plot(times, vwap, color="#f5a623", linewidth=1.2, alpha=0.8, label="VWAP")

            # ── EMA 20 ──
            ema20 = [closes[0]]
            k = 2 / (20 + 1)
            for c in closes[1:]:
                ema20.append(c * k + ema20[-1] * (1 - k))
            ax.plot(times, ema20, color="#00bcd4", linewidth=1.2, alpha=0.7, label="EMA 20")

            # ── Bollinger Bands (20,2) ──
            bb_period = 20
            if len(closes) >= bb_period:
                bb_sma = []
                bb_upper = []
                bb_lower = []
                for i in range(len(closes)):
                    if i >= bb_period - 1:
                        window = closes[i-bb_period+1:i+1]
                        sma = sum(window) / bb_period
                        variance = sum((x - sma) ** 2 for x in window) / bb_period
                        std = variance ** 0.5
                        bb_sma.append(sma)
                        bb_upper.append(sma + 2 * std)
                        bb_lower.append(sma - 2 * std)
                    else:
                        bb_sma.append(None)
                        bb_upper.append(None)
                        bb_lower.append(None)
                valid = slice(bb_period - 1, len(times))
                ax.plot(times[valid], bb_upper[valid], color="#7c4dff", linewidth=0.8, alpha=0.5, label="BB Upper")
                ax.plot(times[valid], bb_lower[valid], color="#7c4dff", linewidth=0.8, alpha=0.5, label="BB Lower")
                ax.fill_between(times[valid], bb_upper[valid], bb_lower[valid], alpha=0.05, color="#7c4dff")

        # ── Key price levels ──
        ax.axhline(last, color="#ffffff", linewidth=0.8, linestyle="--", alpha=0.4)
        ax.text(times[-1], last, f"  {last:.1f}", color="#ffffff", fontsize=9, fontweight="bold", va="bottom")
        ax.axhline(hh, color="#aa00ff", linewidth=0.5, linestyle=":", alpha=0.5)
        ax.text(times[-1], hh, f"  H {hh:.1f}", color="#aa00ff", fontsize=7, va="bottom")
        ax.axhline(ll, color="#00e676", linewidth=0.5, linestyle=":", alpha=0.5)
        ax.text(times[-1], ll, f"  L {ll:.1f}", color="#00e676", fontsize=7, va="top")

        # ── Volume bars ──
        max_v = max(volumes) if volumes else 1
        for i in range(len(times)):
            color = "#00e676" if closes[i] >= opens[i] else "#aa00ff"
            axv.bar(times[i], volumes[i] / max_v, width=width, color=color, alpha=0.5)

        axv.set_ylim(0, 1.2)
        axv.set_yticks([])
        axv.set_ylabel("Vol", color="#8b949e", fontsize=9)

        # ── Styling ──
        ax.set_ylabel("Precio", color="#8b949e", fontsize=9)
        ax.tick_params(colors="#8b949e", labelsize=8)
        axv.tick_params(colors="#8b949e", labelsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")
        ax.grid(True, alpha=0.08, color="#8b949e")
        axv.grid(True, alpha=0.08, color="#8b949e")

        legend = ax.legend(loc="upper left", fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#c9d1d9")
        for text in legend.get_texts():
            text.set_color("#c9d1d9")

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

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

    @staticmethod
    def _html_escape(text: str) -> str:
        """Escape HTML special characters for Telegram HTML parse_mode."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert *bold* -> <b>bold</b>, `code` -> <code>code</code>, _italic_ -> <i>italic</i>.
        Preserves any existing HTML tags already in the text."""
        # Protect existing HTML tags with placeholders
        tag_pattern = re.compile(r'</?(b|i|u|s|code|pre|a|tg-spoiler|span)\b[^>]*>')
        protected = {}
        def _protect(m):
            pid = f"\x00TAG{len(protected)}\x00"
            protected[pid] = m.group(0)
            return pid
        text = tag_pattern.sub(_protect, text)

        # Escape remaining special chars
        text = TelegramBot._html_escape(text)

        # Convert markdown patterns to HTML tags
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.+?)\*', r'<b>\1</b>', text)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        text = re.sub(r'_(.+?)_', r'<i>\1</i>', text)

        # Restore protected HTML tags
        for pid, tag in protected.items():
            text = text.replace(pid, tag)
        return text

    @staticmethod
    def _strip_emoji(text: str) -> str:
        """Remove emoji/symbol characters, normalize accents, keep letters/digits/spaces."""
        text = unicodedata.normalize('NFKD', text)
        return ''.join(
            c for c in text
            if unicodedata.category(c) not in ('So', 'Mn')
        ).strip()

    @staticmethod
    def _safe_get(d: dict, key: str, fmt=None, default='--') -> str:
        """Null-safe field accessor. Returns formatted value or '--' on any failure."""
        try:
            val = d.get(key)
            if val is None:
                return default
            if fmt:
                return fmt(val)
            return str(val)
        except Exception:
            return default

    # ── Reply keyboard lookup matrix ─────────────────────────────────
    # Maps cleaned button text → handler method name.
    # Keys are lowercased keyword fragments after emoji removal.
    REPLY_BUTTON_MAP: dict[str, str] = {
        'info':      '_cmd_info',
        'signal':    '_cmd_signal',
        'scalp':     '_handle_scalp',
        'trampas':   '_handle_trampas',
        'insti':     '_handle_trampas',
        'micro':     '_handle_micro',
        'chart':     '_cmd_chart',
        'alertas':   '_cmd_alerts',
        'estado':    '_cmd_status',
        'config':    '_cmd_config',
        'operar':    '_handle_operar',
        'long':      '_handle_long',
        'short':     '_handle_short',
        'volver':    '_handle_back_main',
    }

    async def _cmd_refresh(self):
        """Reply handler: /refresh — latest snapshot summary."""
        s = self._state
        sig = self._safe_get(s, 'signal_text')
        conf = self._safe_get(s, 'confidence', fmt=lambda v: f'{v:.0f}%')
        price = self._safe_get(s, 'price', fmt=lambda v: f'${v:,.0f}')
        rsi = self._safe_get(s, 'rsi', fmt=lambda v: f'{v:.1f}')
        trend = self._safe_get(s, 'trend')
        await self._send(
            f"\U0001f504 <b>Actualizado</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Precio: <code>{price}</code>\n"
            f"Trend: <code>{trend}</code> \u2014 Señal: <code>{sig}</code> ({conf})\n"
            f"RSI: <code>{rsi}</code>"
        )

    async def _cmd_ultimo(self):
        """Reply handler: /ultimo — last tick + direction."""
        s = self._state
        price = self._safe_get(s, 'price', fmt=lambda v: f'${v:,.0f}')
        chg = self._safe_get(s, 'change_pct', fmt=lambda v: f'{v:+.2f}%')
        trend = self._safe_get(s, 'trend')
        sig = self._safe_get(s, 'signal_text')
        arrow = "\U0001f7e2\U0001f846" if s.get('change_pct', 0) >= 0 else "\U0001f7e3\U0001f847"
        sig_e = "\U0001f7e2" if sig == 'LONG' else "\U0001f7e3" if sig == 'SHORT' else "\u26ab"
        await self._send(
            f"\U0001f3b5 <b>{settings.SYMBOL}</b> {arrow} <code>{price}</code>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Cambio: <code>{chg}</code>\n"
            f"Trend: <code>{trend}</code>\n"
            f"Señal: {sig_e} <code>{sig}</code>"
        )

    async def _cmd_ai_help(self):
        """Reply handler: /ai — show usage."""
        await self._send(
            "\U0001f916 <b>Gemini AI Trader</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "Uso: <code>/ai tu pregunta</code>\n"
            "O simplemente escribe cualquier mensaje y Gemini responder\u00e1.\n\n"
            "Ejemplo: <code>/ai deber\u00eda abrir LONG ahora?</code>"
        )

    # ── Reply button high-density handlers ───────────────────────────

    async def _handle_scalp(self):
        """Quantitative scalp bias based on microstructure aggressiveness."""
        s = self._state
        ts_val = s.get('tick_speed', 0)
        ts = self._safe_get(s, 'tick_speed', fmt=lambda v: f'{v:.1f}')
        delta = s.get('delta', 0)
        cvd = s.get('cvd', 0)
        pinam = s.get('pinam', 0)
        skew = s.get('skewness', 0)
        bv = s.get('buy_volume', 0)
        sv = s.get('sell_volume', 0)

        # Bias based on real computed metrics
        high_freq = ts_val > 25
        net_aggressive = (bv - sv) / max(bv + sv, 0.001)
        tox = pinam > 0.25
        asym = abs(skew) > 0.3

        if high_freq and net_aggressive > 0.15 and delta > 0:
            bias = '\U0001f525 AGRESIVIDAD COMPRADORA'
        elif high_freq and net_aggressive < -0.15 and delta < 0:
            bias = '\U0001f525 AGRESIVIDAD VENDEDORA'
        elif tox and asym:
            bias = '\U0001f4a5 TOXICIDAD ALTA \u2014 POSIBLE TRAMPA'
        elif ts_val > 20 and abs(delta / max(bv + sv, 0.001)) > 0.3:
            bias = '\U0001f40b FLUJO DIRECCIONAL FUERTE'
        else:
            bias = '\u2696\ufe0f MICRO-RANGO SIN DIRECCI\u00d3N'

        await self._send(
            f"\U0001f3c3 <b>SCALP — MICROESTRUCTURA</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            f"<b>BIAS</b>: {bias}\n\n"
            f"<b>INDICADORES</b>\n"
            f"  \U0001f4e1 Tick Speed:  <code>{ts} t/s</code>\n"
            f"  \u0394 Delta:         <code>{self._safe_get(s, 'delta', fmt=lambda v: f'{v:+.2f}')}</code>\n"
            f"  CVD:               <code>{self._safe_get(s, 'cvd', fmt=lambda v: f'{v:+.2f}')}</code>\n"
            f"  PINAM:             <code>{self._safe_get(s, 'pinam', fmt=lambda v: f'{v:.4f}')}</code>\n"
            f"  Skewness:          <code>{self._safe_get(s, 'skewness', fmt=lambda v: f'{v:+.4f}')}</code>\n"
            f"  B/A Ratio:         <code>{self._safe_get(s, 'ba_ratio', fmt=lambda v: f'{v:.4f}')}</code>\n\n"
            f"<b>CONTEXTO</b>\n"
            f"  Precio:    <code>{self._safe_get(s, 'price', fmt=lambda v: f'${v:,.0f}')}</code>\n"
            f"  Trend 1m:  <code>{self._safe_get(s, 'trend')}</code>\n"
            f"  Trend 5m:  <code>{self._safe_get(s, 'trend_5m')}</code>\n"
            f"  RSI 1m:    <code>{self._safe_get(s, 'rsi', fmt=lambda v: f'{v:.1f}')}</code>\n\n"
            f"\u23f0 {self._safe_get(s, 'timestamp')}"
        )

    async def _handle_trampas(self):
        """Quantitative institutional narrative with multi-factor algorithms."""
        s = self._state
        price = s.get('price', 0)
        bid_px = self._safe_get(s, 'wall_bid', fmt=lambda v: f'${v:,.0f}')
        bid_sz = s.get('wall_bid_size', 0)
        ask_px = self._safe_get(s, 'wall_ask', fmt=lambda v: f'${v:,.0f}')
        ask_sz = s.get('wall_ask_size', 0)
        depth = s.get('depth_imb_pct', 0)
        cancel = s.get('cancel_rate', 0)
        delta = s.get('delta', 0)
        cvd = s.get('cvd', 0)
        cum_delta = s.get('cumulative_delta', 0)
        vol = s.get('volume', 0)
        bv = s.get('buy_volume', 0)
        sv = s.get('sell_volume', 0)
        ba = s.get('ba_ratio', 1.0)
        skew = s.get('skewness', 0)
        pinam = s.get('pinam', 0)
        ts = s.get('tick_speed', 0)
        imb = s.get('imbalance', 0)

        # ── Algorithm 1: Delta/Price Divergence ──────────────────────────
        # When price moves opposite to delta, institutions are trapping
        delta_direction = 1 if delta > 0 else -1
        price_direction = 1 if s.get('change_pct', 0) > 0 else -1
        divergence = delta_direction != price_direction and abs(delta) > 5

        # ── Algorithm 2: Iceberg / Stacked Wall Probability ─────────────
        # Large walls at round numbers with cancel_rate > 10% = iceberg
        iceberg_score = 0
        if bid_sz >= 3 or ask_sz >= 3:
            bid_round = s.get('wall_bid', 0) % 100 == 0 if s.get('wall_bid', 0) else False
            ask_round = s.get('wall_ask', 0) % 100 == 0 if s.get('wall_ask', 0) else False
            if (bid_round and bid_sz >= 3) or (ask_round and ask_sz >= 3):
                iceberg_score += 30
            if cancel > 10:
                iceberg_score += 20
            if bid_sz >= 5 or ask_sz >= 5:
                iceberg_score += 25
            iceberg_score = min(iceberg_score, 100)

        # ── Algorithm 3: Cumulative Delta Divergence ────────────────────
        # CVD rising while price flat = accumulation, CVD falling while flat = distribution
        cvd_div = "ACUMULACION" if cum_delta > 50 and abs(s.get('change_pct', 0)) < 0.3 else \
                  "DISTRIBUCION" if cum_delta < -50 and abs(s.get('change_pct', 0)) < 0.3 else \
                  "NEUTRAL"

        # ── Algorithm 4: Multi-factor Spoofing Confidence ───────────────
        spoof_score = 0
        if cancel > 20:
            spoof_score += 35
        elif cancel > 10:
            spoof_score += 15
        if (imb > 0.3 and delta < -3) or (imb < -0.3 and delta > 3):
            spoof_score += 30
        if pinam > 0.3:
            spoof_score += 20
        if iceberg_score > 50:
            spoof_score += 15
        spoof_score = min(spoof_score, 100)

        # ── Algorithm 5: Absorption Intensity ───────────────────────────
        # High vol + balanced B/A + low cancel = real absorption
        if 0.85 < ba < 1.15 and vol > 3:
            absorb_intensity = min(100, (vol / 10) * 40 + (1 - abs(ba - 1)) * 30)
            absorb_label = "ALTA" if absorb_intensity > 65 else "MEDIA" if absorb_intensity > 35 else "BAJA"
        else:
            absorb_intensity = 0
            absorb_label = "NULA"

        # ── Line construction ────────────────────────────────────────────
        lines = [
            f"\U0001f50d <b>NARRATIVA INSTITUCIONAL CUANTITATIVA</b>",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"<b>\U0001f40b WHALE MAP</b>",
            f"  \U0001f7e2 Bid Wall: <code>{bid_px}</code> \u2014 <code>{bid_sz:.1f} BTC</code>",
            f"  \U0001f7e3 Ask Wall: <code>{ask_px}</code> \u2014 <code>{ask_sz:.1f} BTC</code>",
        ]

        # Iceberg warning
        if iceberg_score >= 50:
            lines += [
                f"  \U0001f4a7 <b>Iceberg Detection</b> \u2014 Score <code>{iceberg_score:.0f}%</code>",
                f"  Posibles \u00f3rdenes ocultas en libro",
            ]

        # Spoofing section
        lines += [
            f"",
            f"<b>\U0001f6a8 SPOOFING ANALYSIS</b>",
            f"  Score: <code>{spoof_score:.0f}%</code>",
        ]
        if spoof_score >= 50:
            lines += [f"  \u26a0\ufe0f Alta probabilidad de manipulaci\u00f3n"]
        elif spoof_score >= 25:
            lines += [f"  \U0001f7e1 Se\u00f1ales de cancelaciones sospechosas"]
        else:
            lines += [f"  \u2705 Sin evidencia de spoofing"]

        # Divergence detection
        lines += [f""]
        if divergence:
            lines += [
                f"<b>\U0001f504 DIVERGENCIA DETECTADA</b>",
                f"  Precio vs Delta en direcciones opuestas",
                f"  \u2191 Delta: <code>{delta:+.1f}</code> | Precio: <code>{s.get('change_pct', 0):+.2f}%</code>",
            ]
            if delta > 0 and price_direction < 0:
                lines += [f"  \U0001f7e2 Presi\u00f3n compradora ignorada \u2014 posible acumulaci\u00f3n"]
            elif delta < 0 and price_direction > 0:
                lines += [f"  \U0001f7e3 Presi\u00f3n vendedora ignorada \u2014 posible distribuci\u00f3n"]

        # CVD divergence
        lines += [
            f"",
            f"<b>\U0001f4ca FLUJO ACUMULADO</b>",
            f"  CVD Regime: <code>{cvd_div}</code>",
            f"  CVD: <code>{cvd:+.1f}</code> | \u0394 Acum: <code>{cum_delta:+.1f}</code>",
            f"  Buy/Sell: <code>{bv:.1f}</code> / <code>{sv:.1f}</code> | B/A: <code>{ba:.3f}x</code>",
            f"  Depth Imb: <code>{depth:+.1f}%</code> | Cancel: <code>{cancel:.1f}%</code>",
            f"",
            f"<b>\U0001f4a1 ABSORPTION</b>",
            f"  Intensidad: <code>{absorb_label}</code> ({absorb_intensity:.0f}%)",
            f"  PINAM: <code>{pinam:.4f}</code> | Skew: <code>{skew:+.4f}</code>",
            f"  Tick Speed: <code>{ts:.1f} t/s</code>",
            f"\u23f0 {self._safe_get(s, 'timestamp')}",
        ]
        await self._send('\n'.join(lines))

    async def _handle_micro(self):
        """Statistical flow report: asymmetry, absorption, rotation risk."""
        s = self._state
        cd = s.get('cumulative_delta', 0)
        skew = s.get('skewness', 0)
        pinam = s.get('pinam', 0)
        ba = s.get('ba_ratio', 1.0)
        tick = self._safe_get(s, 'tick_speed', fmt=lambda v: f'{v:.1f}')
        spread_vel = self._safe_get(s, 'spread_velocity', fmt=lambda v: f'{v:.1f}ms')

        # Asymmetry analysis
        if abs(skew) > 0.3:
            asymmetry = '\U0001f7e3 ASIMETR\u00cdA NEGATIVA' if skew < 0 else '\U0001f7e2 ASIMETR\u00cdA POSITIVA'
        else:
            asymmetry = '\u26aa FLUJO SIM\u00c9TRICO'

        # Absorption vs rotation
        if 0.8 < ba < 1.2 and pinam < 0.2:
            regime = '\U0001f4a1 ABSORCI\u00d3N PASIVA \u2014 Rango equilibrado, sin direcci\u00f3n clara'
        elif abs(skew) > 0.5 and pinam > 0.3:
            regime = '\U0001f4a5 ROTACI\u00d3N INMINENTE \u2014 Alta toxicidad, flujo direccional'
        elif cd > 50 and ba >= 1.2:
            regime = '\U0001f7e2 ACUMULACI\u00d3N ACTIVA \u2014 Presi\u00f3n compradora sostenida'
        elif cd < -50 and ba <= 0.8:
            regime = '\U0001f7e3 DISTRIBUCI\u00d3N ACTIVA \u2014 Presi\u00f3n vendedora sostenida'
        else:
            regime = '\u2696\ufe0f ZONA DE INCERTIDUMBRE \u2014 Sin ventaja estad\u00edstica'

        await self._send(
            f"\U0001f52e <b>MICROESTRUCTURA CUANTITATIVA</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            f"<b>R\u00c9GIMEN DE FLUJO</b>\n"
            f"  {regime}\n\n"
            f"<b>ESTAD\u00cdSTICOS</b>\n"
            f"  \u0394 Acumulado:   <code>{self._safe_get(s, 'cumulative_delta', fmt=lambda v: f'{v:+.2f}')}</code>\n"
            f"  Skewness:       <code>{self._safe_get(s, 'skewness', fmt=lambda v: f'{v:+.4f}')}</code>\n"
            f"  PINAM (toxic):  <code>{self._safe_get(s, 'pinam', fmt=lambda v: f'{v:.4f}')}</code>\n"
            f"  B/A Ratio:      <code>{self._safe_get(s, 'ba_ratio', fmt=lambda v: f'{v:.4f}')}</code>\n\n"
            f"<b>HFT M\u00c9TRICS</b>\n"
            f"  Tick Speed:     <code>{tick} t/s</code>\n"
            f"  Spread Vel:     <code>{spread_vel}</code>\n"
            f"  Cancel Rate:    <code>{self._safe_get(s, 'cancel_rate', fmt=lambda v: f'{v:.1f}%')}</code>\n\n"
            f"<b>ASIMETR\u00cdA</b>\n"
            f"  {asymmetry}\n\n"
            f"\u23f0 {self._safe_get(s, 'timestamp')}"
        )

    async def _send(self, text: str, kb=None, parse_mode="HTML"):
        payload = {"chat_id": self.chat_id, "text": self._markdown_to_html(text)}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if kb:
            payload["reply_markup"] = kb
        result = await self._api_call("sendMessage", **payload)
        if not result.get("ok"):
            desc = result.get("description", "")
            # If HTML parsing failed, retry as plain text
            if parse_mode == "HTML" and "can't parse entities" in desc.lower():
                payload.pop("parse_mode", None)
                payload["text"] = self._html_escape(text)
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
            "Eres un trader scalper profesional especializado en temporalidad de 1 minuto. "
            "Responde SIEMPRE en español con formato visual elegante y profesional.\n\n"
            "ESTRUCTURA OBLIGATORIA:\n"
            "1) Señal clara al inicio: 🟢 LONG / 🟣 SHORT / ⏳ WAIT con nivel de convicción (bajo/medio/alto)\n"
            "2) Línea separadora ───\n"
            "3) 📊 Análisis: delta, CVD, imbalance, bid/ask walls, whale walls\n"
            "4) 📈 Tendencia: MTF 5m/15m, confluence score, dirección institucional\n"
            "5) 🎯 Estrategia: Entry claro + SL técnico + TP1/TP2\n"
            "6) ⚠️ Riesgo: advertencia de apalancamiento\n\n"
            "REGLAS DE FORMATO:\n"
            "- Usa SIEMPRE 🟢 para LONG/bullish y 🟣 para SHORT/bearish\n"
            "- Palabras clave en *negritas* (ej: *soporte*, *resistencia*, *acumulación*)\n"
            "- Valores numéricos en `código` (ej: `$67,500`, `RSI 42.5`)\n"
            "- Emojis como bullet points: 📊💰📈📉🎯🛡️🚀🐋⚡🔥💎\n"
            "- Cada sección separada por línea en blanco\n"
            "- Sin _underscores_ ni caracteres raros\n"
            "- Timestamp al final en formato `HH:MM UTC`"
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
                return self._format_ai_response(reply)
        except Exception as e:
            return f"\u26a0\ufe0f Error de conexi\u00f3n con Gemini: {e}"

    def _format_ai_response(self, text: str) -> str:
        """Post-process Gemini: emoji, HTML bold, professional formatting."""
        # 1. Ensure signal words have emoji + bold
        text = re.sub(r'(?<!\w)LONG(?!\w)', '🟢 <b>LONG</b>', text)
        text = re.sub(r'(?<!\w)SHORT(?!\w)', '🟣 <b>SHORT</b>', text)
        text = re.sub(r'(?<!\w)WAIT(?!\w)', '⏳ <b>WAIT</b>', text)
        text = re.sub(r'(?<!\w)NEUTRAL(?!\w)', '⚪ <b>NEUTRAL</b>', text)

        # 2. Convert any remaining *bold* to <b>bold</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<b>\1</b>', text)

        # 3. Format sections elegantly
        header_map = {
            'analisis': '📊', 'analisis': '📊',
            'estrategia': '🎯',
            'execucion': '🚀', 'ejecucion': '🚀',
            'entry': '🚀', 'entrada': '🚀',
            'riesgo': '⚠️',
            'tendencia': '📈',
            'resumen': '📋',
            'conclusion': '🎯', 'conclusion': '🎯',
            'señal': '📡', 'senal': '📡',
        }
        header_pat = r'^([:\s]*)(' + '|'.join(header_map.keys()) + r')(?=[:\s])'
        def _add_header_emoji(m):
            word = m.group(2)
            emoji = header_map.get(word.lower(), '')
            return f'{m.group(1)}{emoji} <b>{word.capitalize()}</b>' if emoji else m.group(0)
        text = re.sub(header_pat, _add_header_emoji, text, flags=re.MULTILINE | re.IGNORECASE)

        return text

    # ──────────────────────────────────────────────────────────────────────────
    # Command / button handlers
    # ──────────────────────────────────────────────────────────────────────────

    async def _cmd_start(self):
        kb = _keyboard(
            _row(_btn("\U0001f4ca Info", "info"), _btn("\U0001f4e1 Signal", "signal"), _btn("\U0001f3c3 Scalp", "scalp")),
            _row(_btn("\U0001f50d Trampas", "trampas"), _btn("\U0001f52e Micro", "micro"), _btn("\U0001f4f7 Chart", "chart")),
            _row(_btn("\u2699\ufe0f Config", "config"), _btn("\U0001f4a1 Operar", "operar"), _btn("\U0001f9f9 Estado", "status")),
            _row(_btn("\U0001f514 Notificaciones", "alerts"), _btn("\U0001f7e2 LONG", "long"), _btn("\U0001f7e3 SHORT", "short")),
        )
        msg = (
            "\U0001f916 <b>BB-450 Trading Bot</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "Monitoreo en tiempo real de <code>{}</code>\n\n"
            "<b>COMANDOS:</b>\n"
            "\U0001f4ca <b>Info</b> \u2014 Todos los indicadores\n"
            "\U0001f4e1 <b>Signal</b> \u2014 Se\u00f1al actual + gr\u00e1fico velas\n"
            "\U0001f3c3 <b>Scalp</b> \u2014 Scalping 1m\n"
            "\U0001f50d <b>Trampas</b> \u2014 Narrativa institucional\n"
            "\U0001f52e <b>Micro</b> \u2014 Microestructura cuantitativa\n"
            "\U0001f4f7 <b>Chart</b> \u2014 Gr\u00e1fico velas\n"
            "\U0001f9f9 <b>Estado</b> \u2014 Estado del bot\n"
            "\U0001f514 <b>Notificaciones</b> \u2014 Configurar alertas\n"
            "\u2699\ufe0f <b>Config</b> \u2014 Capital, riesgo, SL/TP\n"
            "\U0001f7e2 <b>LONG</b> \u2014 Abrir LONG real con SL/TP\n"
            "\U0001f7e3 <b>SHORT</b> \u2014 Abrir SHORT real con SL/TP\n"
        ).format(settings.SYMBOL)
        await self._send(msg, kb=kb)

        await self._send_main_kb()

    def _send_main_kb(self):
        """Send the persistent main ReplyKeyboard."""
        rkb = _reply_kb(
            ("\U0001f4ca Info", "\U0001f4e1 Signal", "\U0001f3c3 Scalp"),
            ("\U0001f50d Insti", "\U0001f52e Micro", "\U0001f4f7 Chart"),
            ("\u2699\ufe0f Config", "\U0001f4a1 Operar", "\U0001f9f9 Estado"),
            ("\U0001f514 Notificaciones",),
        )
        return self._send(
            "\U0001f447 <b>Botones rapidos</b> \u2014 toca para consultar datos o ejecutar",
            kb=rkb,
        )

    async def _handle_operar(self):
        """Replace keyboard with LONG / SHORT / BACK submenu."""
        rkb = _reply_kb(
            ("\U0001f7e2 LONG", "\U0001f7e3 SHORT"),
            ("\U0001f519 Volver",),
        )
        await self._send(
            "\U0001f4a1 <b>OPERAR</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            f"Capital: <code>${self._user_config.get('capital', 100):.1f}</code> | "
            f"SL <code>{self._user_config.get('sl_pct', 0.5):.1f}%</code> | "
            f"TP <code>{self._user_config.get('tp_pct', 1.5):.1f}%</code>\n\n"
            "Selecciona direcci\u00f3n para abrir orden real con margen aislado:",
            kb=rkb,
        )

    async def _handle_back_main(self):
        """Restore main ReplyKeyboard."""
        await self._send_main_kb()

    async def _cmd_info(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f <b>Sin datos</b> — esperando actualización...")
            return
        if not s.get("klines_ready"):
            cnt = s.get("klines_count", 0)
            await self._send(f"\u23f3 <b>Cargando indicadores...</b> ({cnt}/50 klines)\nEsperando datos de mercado...")
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

        te = "\U0001f7e2" if trend == "ALCISTA" else "\U0001f7e3" if trend == "BAJISTA" else "\U0001f7e1"
        se = "\U0001f7e2" if sig == "LONG" else "\U0001f7e3" if sig == "SHORT" else "\u26ab"
        de = "\U0001f7e2" if delta > 0 else "\U0001f7e3" if delta < 0 else "\u26ab"
        rsi_e = "\U0001f7e2" if rsi < 40 else "\U0001f7e3" if rsi > 60 else "\U0001f7e1"
        bb_e = "\U0001f7e2" if bb < 30 else "\U0001f7e3" if bb > 70 else "\U0001f7e1"

        lines = [
            f"\U0001f3b5 <b>{settings.SYMBOL} \u2014 AN\u00c1LISIS COMPLETO</b> \U0001f3b5",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"<b>PRECIO</b> \U0001f4b0",
            f"  \u25b6 `${p:,.0f}` | \U0001f4c8 `{chg:+.2f}%` | High `${dh:,.0f}` Low `${dl:,.0f}`",
            f"  VWAP `${vwap:,.0f}`",
            f"",
            f"<b>TENDENCIA</b> {te}",
            f"  Trend: `{trend}` | Macro: `{macro}`",
            f"  MTF: 5m `{trend_5m}` | 1h `{trend_1h}` | Conf `{conf_score:.0f}%`",
            f"  \U0001f514 Se\u00f1al: {se} `{sig}` ({conf:.0f}%)",
            f"",
            f"<b>INDICADORES T\u00c9CNICOS</b> \U0001f4ca",
            f"  RSI {rsi_e} `{rsi:.1f}` | MACD `{macd:.4f}` Hist `{mh:.4f}`",
            f"  BB {bb_e} `{bb:.1f}%` | ATR `${atr:.2f}`",
            f"",
            f"<b>ORDER FLOW</b> \U0001f4c8",
            f"  Delta {de} `{delta:.0f}` | CVD `{cvd:.0f}`",
            f"  Buy `{bv:.1f}` / Sell `{sv:.1f}` | Imb `{imb:+.3f}`",
            f"",
            f"<b>MICROESTRUCTURA</b> \U0001f52e",
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
            await self._send(f"\u23f3 <b>Cargando...</b> ({cnt}/50 klines)")
            return

        direction = s.get("signal_text", "WAIT")
        conf = s.get("confidence", 0); price = s.get("price", 0)
        delta = s.get("delta", 0); cvd = s.get("cvd", 0); rsi = s.get("rsi", 50)
        trend = s.get("trend", "NEUTRAL"); trend_5m = s.get("trend_5m", "WAIT")
        trend_15m = s.get("trend_15m", "WAIT"); trend_1h = s.get("trend_1h", "WAIT")
        ke = s.get("kaufman_eff", 0.5); ts = s.get("tick_speed", 0)
        bv = s.get("buy_volume", 0); sv = s.get("sell_volume", 0)
        ba = s.get("ba_ratio", 1.0); imb = s.get("imbalance", 0)
        cum_delta = s.get("cumulative_delta", 0)
        wall_bid = s.get("wall_bid", 0); wall_ask = s.get("wall_ask", 0)
        wall_bid_sz = s.get("wall_bid_size", 0); wall_ask_sz = s.get("wall_ask_size", 0)
        cancel = s.get("cancel_rate", 0); pinam = s.get("pinam", 0)
        depth = s.get("depth_imb_pct", 0)
        conf_score = s.get("confluence_score", 0)

        if direction == "LONG":
            emoji, col, dir_arrow = "\U0001f7e2", "\U0001f7e2", "\U0001f847"
        elif direction == "SHORT":
            emoji, col, dir_arrow = "\U0001f7e3", "\U0001f7e3", "\U0001f846"
        else:
            emoji, col, dir_arrow = "\u26ab", "\u26ab", "\u23f1\ufe0f"

        de = "\U0001f7e2" if delta > 0 else "\U0001f7e3"
        dom = "\U0001f7e2 BIDS DOMINAN" if depth > 10 else "\U0001f7e3 ASKS DOMINAN" if depth < -10 else "\u26ab EQUILIBRADO"

        lines = [
            f"{emoji} <b>SE\u00d1AL: {direction}</b> {emoji}",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"<b>\U0001f4b0 PRECIO</b>",
            f"  <code>${price:,.0f}</code> | Confianza: <code>{conf:.0f}%</code> | Trend: <code>{trend}</code>",
            f"",
            f"<b>\U0001f3c6 MULTITIEMPO</b>",
            f"  5m: <code>{trend_5m}</code> | 15m: <code>{trend_15m}</code> | 1h: <code>{trend_1h}</code>",
            f"  Confluencia: <code>{conf_score:.0f}%</code>",
            f"",
            f"<b>\U0001f4ca ORDER FLOW</b>",
            f"  Delta {de}: <code>{delta:+.0f}</code> | CVD: <code>{cvd:+.0f}</code> | Acum: <code>{cum_delta:+.0f}</code>",
            f"  B/A: <code>{ba:.3f}x</code> | Imb: <code>{imb:+.3f}</code> | Depth: <code>{depth:+.1f}%</code>",
            f"  Buy <code>{bv:.0f}</code> / Sell <code>{sv:.0f}</code> | {dom}",
            f"",
            f"<b>\U0001f52e MICRO</b>",
            f"  RSI: <code>{rsi:.1f}</code> | Tick: <code>{ts:.1f} t/s</code> | Cancel: <code>{cancel:.1f}%</code>",
            f"  PINAM: <code>{pinam:.4f}</code>",
            f"",
            f"<b>\U0001f3f0 WALLS</b>",
            f"  \U0001f7e2 BID <code>${wall_bid:,.0f}</code> (<code>{wall_bid_sz:.1f} BTC</code>)",
            f"  \U0001f7e3 ASK <code>${wall_ask:,.0f}</code> (<code>{wall_ask_sz:.1f} BTC</code>)",
            f"",
            f"\u23f0 {s.get('timestamp', '')}",
        ]
        await self._send("\n".join(lines))

        # Send chart with AI analysis
        klines = await self._fetch_klines()
        if klines:
            png = await self._generate_chart(klines)
            if not png:
                return

            # Generate AI analysis of the chart data
            ai_text = ""
            if self._gemini_enabled:
                closes = [float(k[4]) for k in klines[-20:]]
                vols = [float(k[5]) for k in klines[-20:]]
                price_chg = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes else 0
                avg_vol = sum(vols) / len(vols) if vols else 0
                last_vol = vols[-1] if vols else 0

                prompt = (
                    f"Analiza este grafico de 1 minuto de {settings.SYMBOL}:\n"
                    f"Precio actual: ${price:,.0f}\n"
                    f"Senal: {direction} ({conf:.0f}%)\n"
                    f"Cambio en 20 velas: {price_chg:+.2f}%\n"
                    f"RSI: {rsi:.1f}\n"
                    f"Delta: {delta:+.0f} | CVD: {cvd:+.0f}\n"
                    f"B/A: {ba:.3f}x\n"
                    f"Volumen ultima vela: {last_vol:.0f} (promedio: {avg_vol:.0f})\n"
                    f"Trend: {trend} | 5m: {trend_5m} | 1h: {trend_1h}\n"
                    f"Cancel Rate: {cancel:.1f}% | PINAM: {pinam:.4f}\n\n"
                    f"Responde en 1-2 parrafos: ¿que indica el grafico? "
                    f"¿Hay soporte/resistencia clave? ¿El volumen confirma? "
                    f"¿Que harías? SOLO texto, sin JSON."
                )
                try:
                    ai_text = await self._chat_gemini_raw(prompt)
                    ai_text = ai_text.strip().strip('"').strip("'")
                    if len(ai_text) > 500:
                        ai_text = ai_text[:500] + "..."
                except Exception:
                    ai_text = ""

            caption = (
                f"\U0001f4e1 <b>{direction}</b> ({conf:.0f}%) | RSI <code>{rsi:.1f}</code> | "
                f"Delta <code>{delta:+.0f}</code> | Trend <code>{trend}</code>"
            )
            await self._send_photo(png, caption=caption)

            if ai_text:
                await self._send(
                    f"\U0001f916 <b>AI Analiza el Gr\u00e1fico</b>\n"
                    f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
                    f"{ai_text}"
                )

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
            "\U0001f514 <b>Configuraci\u00f3n de Notificaciones</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "Toca cada opci\u00f3n para activar/desactivar:\n\n"
            f"{'\u2705' if crash_on else '\u274c'} <b>Crash</b>: ca\u00edda > {settings.ALERT_CRASH_PCT:.0f}% en 60s\n"
            f"{'\u2705' if bs_on else '\u274c'} <b>Buy/Sell</b>: nueva se\u00f1al LONG o SHORT\n"
            f"{'\u2705' if vol_on else '\u274c'} <b>Volumen</b>: pico > {settings.ALERT_VOLUME_SPIKE:.0f}x promedio\n"
            f"{'\u2705' if rsi_on else '\u274c'} <b>RSI</b>: sobrecompra > {settings.ALERT_RSI_OVERBOUGHT:.0f} / sobreventa < {settings.ALERT_RSI_OVERSOLD:.0f}"
        )
        await self._send(msg, kb=kb)

    async def _cmd_trampas(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f <b>Sin datos</b>")
            return

        p = s.get('price', 0); dh = s.get('day_high', 0); dl = s.get('day_low', 0)
        delta = s.get('delta', 0); cvd = s.get('cvd', 0)
        bv = s.get('buy_volume', 0); sv = s.get('sell_volume', 0)
        vol = s.get('volume', 0); ba = s.get('ba_ratio', 1.0)
        imb = s.get('imbalance', 0); depth = s.get('depth_imb_pct', 0)
        wall_bid = s.get('wall_bid', 0); wall_bid_sz = s.get('wall_bid_size', 0)
        wall_ask = s.get('wall_ask', 0); wall_ask_sz = s.get('wall_ask_size', 0)
        bb_sq = s.get('bb_squeeze', 'NORMAL'); bb_pos = s.get('bb_position', 50)
        force = s.get('force', 'NONE'); ke = s.get('kaufman_eff', 0.5)
        trend = s.get('trend', 'NEUTRAL'); rsi = s.get('rsi', 50)
        ts = s.get('tick_speed', 0)
        trend_5m = s.get('trend_5m', 'WAIT'); trend_15m = s.get('trend_15m', 'WAIT')
        macro = s.get('global_macro', 'NEUTRAL'); vwap = s.get('vwap', 0)
        ai_final = s.get('ai_final', 'WAIT'); ai_of = s.get('ai_score_of', 0)
        ai_mom = s.get('ai_score_mom', 0); ai_trend = s.get('ai_score_trend', 0)
        ai_win = s.get('ai_win_rate', 0)

        # ── WHALE SONAR ──────────────────────────────────────────────────────
        delta_pct = (delta / max(vol, 0.001)) * 100 if vol > 0 else 0
        whale_line = ""
        if abs(delta_pct) > 35 and vol > 3:
            direction = "\U0001f535 BALLENA COMPRADORA" if delta > 0 else "\U0001f7e3 BALLENA VENDEDORA"
            whale_line = f"  {direction}\n  \u0394 {delta:+.2f} \u20bf ({delta_pct:+.1f}%) | Vel: {ts:.1f} t/s"
        elif abs(delta_pct) > 15 and vol > 3:
            direction = "\U0001f7e2 AGRESI\u00d3N COMPRADORA" if delta > 0 else "\U0001f7e3 AGRESI\u00d3N VENDEDORA"
            whale_line = f"  {direction}\n  \u0394 {delta:+.2f} \u20bf ({delta_pct:+.1f}%)"
        else:
            whale_line = f"  \u26aa Sin anomal\u00edas | \u0394 {delta:+.2f} \u20bf ({delta_pct:+.1f}%)"

        # ── WALLS ────────────────────────────────────────────────────────────
        walls = []
        if wall_bid_sz >= 5:
            walls.append(f"\U0001f40b BID {wall_bid_sz:.1f}\u20bf @ `${wall_bid:,.0f}`")
        elif wall_bid_sz >= 2:
            walls.append(f"\U0001f3e6 INST BID {wall_bid_sz:.1f}\u20bf @ `${wall_bid:,.0f}`")
        if wall_ask_sz >= 5:
            walls.append(f"\U0001f40b ASK {wall_ask_sz:.1f}\u20bf @ `${wall_ask:,.0f}`")
        elif wall_ask_sz >= 2:
            walls.append(f"\U0001f3e6 INST ASK {wall_ask_sz:.1f}\u20bf @ `${wall_ask:,.0f}`")
        if not walls:
            walls.append("Sin posiciones institucionales visibles")

        # ── TRAPS (EXACT DASHBOARD LOGIC) ────────────────────────────────────
        traps = []

        # Bid trap: big bid wall + CVD falling + delta negative → selling into support
        if wall_bid_sz >= 5 and cvd < -2 and delta < 0:
            traps.append(
                "\U0001f7e3 <b>TRAMPA ALCISTA</b>\n"
                f"  Muro BID {wall_bid_sz:.1f}\u20bf @ `${wall_bid:,.0f}` con CVD bajista\n"
                f"  — posible stop hunt hacia abajo"
            )

        # Ask trap: big ask wall + CVD rising + delta positive → buying into resistance
        if wall_ask_sz >= 5 and cvd > 2 and delta > 0:
            traps.append(
                "\U0001f7e3 <b>TRAMPA BAJISTA</b>\n"
                f"  Muro ASK {wall_ask_sz:.1f}\u20bf @ `${wall_ask:,.0f}` con CVD alcista\n"
                f"  — posible fakeout hacia arriba"
            )

        # Absorption: B/A ratio balanced + high volume → institutional accumulation
        if 0.7 < ba < 1.3 and vol > 5:
            traps.append(
                "\U000026a1 <b>ABSORCI\u00d3N ACTIVA</b>\n"
                f"  B/A {ba:.2f}x — institucional acumulando en ambos lados"
            )

        if not traps:
            traps.append("  \u2705 Sin trampas detectadas")

        # ── BUILD MESSAGE ────────────────────────────────────────────────────
        f_e = "\U0001f7e2" if force == "BUY" else "\U0001f7e3" if force == "SELL" else "\u26ab"
        sq_e = "\U0001f4a5" if bb_sq == "SQUEEZE" else "\u26ab"
        ai_e = "\U0001f7e2" if ai_final == "LONG" else "\U0001f7e3" if ai_final == "SHORT" else "\u26ab"
        s_dir = "\U0001f7e2 BIDS DOMINAN" if depth > 10 else "\U0001f7e3 ASKS DOMINAN" if depth < -10 else "\U0001f7e1 EQUILIBRADO"

        lines = [
            f"\U0001f50d <b>NARRATIVA INSTITUCIONAL</b> \U0001f50d",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"<b>WHALE SONAR</b> \U0001f40b",
            whale_line,
            f"",
            f"<b>ORDER BOOK</b> \U0001f4ca",
            f"  {s_dir}",
            f"  Depth Imb: `{depth:+.1f}%` | Imbalance: `{imb:+.3f}`",
            f"  B/A: `{ba:.2f}x` | Vol: `{vol:.1f}`",
            f"",
            f"<b>WALLS</b> \U0001f3f0",
        ] + [f"  {w}" for w in walls] + [
            f"",
            f"<b>FUERZA</b> {f_e}",
            f"  Force: `{force}` | BB {sq_e}: `{bb_sq}` ({bb_pos:.0f}%)",
            f"  Kaufman: `{ke:.2f}` | RSI: `{rsi:.1f}`",
            f"  Trend: `{trend}` | 5m: `{trend_5m}` | 15m: `{trend_15m}`",
            f"",
            f"<b>TRAMPAS</b> \U0001f6a9",
        ] + traps + [
            f"",
            f"<b>AI ENGINE</b> {ai_e}",
            f"  Final: `{ai_final}` | Score OF: `{ai_of:.1f}` | Mom: `{ai_mom:.1f}`",
            f"  Trend: `{ai_trend:.1f}` | Win Rate: `{ai_win:.1f}%`",
            f"",
            f"\u23f0 `{s.get('timestamp', '')}`",
        ]
        await self._send("\n".join(lines))

    async def _cmd_chart(self):
        """Fetch klines from Binance, generate chart, send as photo."""
        await self._typing()
        klines = await self._fetch_klines()
        if not klines:
            await self._send("\u26a0\ufe0f <b>Error</b> — no se pudieron obtener klines")
            return
        png = await self._generate_chart(klines)
        if not png:
            await self._send("\u26a0\ufe0f <b>Error</b> — no se pudo generar el gráfico")
            return

        s = self._state
        price = s.get("price", 0); trend = s.get("trend", "NEUTRAL")
        sig = s.get("signal_text", "WAIT"); rsi = s.get("rsi", 50)
        caption = (
            f"\U0001f4f7 <b>BTCUSDT — 1m</b>\n"
            f"Precio: `${price:,.0f}` | {sig} ({s.get('confidence', 0):.0f}%)\n"
            f"RSI: `{rsi:.1f}` | Trend: `{trend}` | VWAP/EMA líneas"
        )
        await self._send_photo(png, caption=caption)

    async def _cmd_micro(self):
        s = self._state
        if not s:
            await self._send("\u26a0\ufe0f <b>Sin datos</b>")
            return
        ke = s.get("kaufman_eff", 0.5); sv = s.get("spread_velocity", 0)
        ts = s.get("tick_speed", 0); cr = s.get("cancel_rate", 0)
        sk = s.get("skewness", 0); pn = s.get("pinam", 0)
        force = s.get("force", "NONE"); bb_sq = s.get("bb_squeeze", "NORMAL")
        delta = s.get("delta", 0); imb = s.get("imbalance", 0)
        depth = s.get("depth_imb_pct", 0); lz = s.get("liq_zones", 0)
        f_e = "\U0001f7e2" if force == "BUY" else "\U0001f7e3" if force == "SELL" else "\u26ab"
        sq_e = "\U0001f7e2" if bb_sq == "SQUEEZE" else "\u26ab"

        lines = [
            f"\U0001f52e <b>MICROESTRUCTURA CUANTITATIVA</b> \U0001f52e",
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
            f"<b>LIQUIDEZ</b>",
            f"  Delta: `{delta:.0f}` | Imbalance: `{imb:+.3f}`",
            f"  Depth Imb: `{depth:+.1f}%` | Liq Zones: `{lz}`",
            f"",
            f"\u23f0 `{s.get('timestamp', '')}`",
        ]
        await self._send("\n".join(lines))

    async def _cmd_scalp(self):
        s = self._state
        if not s or not s.get("klines_ready"):
            await self._send("\u23f3 <b>Cargando datos para scalping...</b>")
            return

        p = s.get('price', 0); delta = s.get('delta', 0); cvd = s.get('cvd', 0)
        imb = s.get('imbalance', 0); depth = s.get('depth_imb_pct', 0)
        bv = s.get('buy_volume', 0); sv = s.get('sell_volume', 0); ratio = s.get('ba_ratio', 1)
        rsi = s.get('rsi', 50); trend = s.get('trend', 'NEUTRAL')
        sig = s.get('signal_text', 'WAIT'); conf = s.get('confidence', 0)
        ke = s.get('kaufman_eff', 0.5); ts = s.get('tick_speed', 0)
        force = s.get('force', 'NONE'); bb_sq = s.get('bb_squeeze', 'NORMAL')
        trend_5m = s.get('trend_5m', 'WAIT'); trend_15m = s.get('trend_15m', 'WAIT')
        wall_bid = s.get('wall_bid', 0); wall_ask = s.get('wall_ask', 0)
        bb_pos = s.get('bb_position', 50); macd_h = s.get('macd_hist', 0)
        atr = s.get('atr', 0); vwap = s.get('vwap', 0); pvd = s.get('price_vwap_dist', 0)

        # Signal direction emojis
        de = "\U0001f7e2" if delta > 0 else "\U0001f7e3"
        im_e = "\U0001f7e2" if imb > 0 else "\U0001f7e3"
        f_e = "\U0001f7e2" if force == "BUY" else "\U0001f7e3" if force == "SELL" else "\u26ab"
        sq_e = "\U0001f4a5" if bb_sq == "SQUEEZE" else "\u26ab"

        # Scalping bias calculation
        imbalance_score = (imb * 50) + 50  # -1..+1 → 0..100
        delta_score = min(100, max(0, (delta / max(abs(delta) + 1, 1) * 50) + 50))
        vol_score = min(100, (bv / max(bv + sv, 0.001)) * 100)
        bias = (imbalance_score * 0.35 + delta_score * 0.30 + vol_score * 0.20 + conf * 0.15)
        scalp_dir = "LONG" if bias > 55 else "SHORT" if bias < 45 else "WAIT"
        strength = abs(bias - 50) * 2

        te = "\U0001f7e2" if scalp_dir == "LONG" else "\U0001f7e3" if scalp_dir == "SHORT" else "\u26ab"

        lines = [
            f"\U0001f3c3 <b>SCALP 1m</b> {te}",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"*DIRECCI\u00d3N:* `{scalp_dir}` | Fuerza: `{strength:.0f}%`",
            f"  Precio: `${p:,.0f}` | ATR `${atr:.2f}`",
            f"  VWAP: `${vwap:,.0f}` | Dist: `{pvd:+.2f}%`",
            f"",
            f"<b>ORDER FLOW</b> \U0001f4c8",
            f"  Delta {de}: `{delta:.0f}` | CVD: `{cvd:.0f}`",
            f"  B/A Ratio: `{ratio:.3f}` | Buy/Sell: `{bv:.0f}/{sv:.0f}`",
            f"  Imbalance {im_e}: `{imb:+.3f}` | Depth: `{depth:+.1f}%`",
            f"",
            f"<b>MICRO</b> \U0001f52e",
            f"  Kaufman: `{ke:.2f}` | Tick: `{ts:.1f}/s`",
            f"  Fuerza {f_e}: `{force}` | BB Squeeze {sq_e}",
            f"",
            f"<b>WHALE WALLS</b> \U0001f40b",
            f"  Bid: `${wall_bid:,.0f}` | Ask: `${wall_ask:,.0f}`",
            f"  BB Pos: `{bb_pos:.1f}%` | MACD Hist: `{macd_h:.4f}`",
            f"",
            f"<b>MTF</b>",
            f"  1m Trend: `{trend}` | 5m: `{trend_5m}` | 15m: `{trend_15m}`",
            f"  RSI 1m: `{rsi:.1f}`",
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
            f"\U0001f9f9 <b>ESTADO DEL SISTEMA</b> \U0001f9f9",
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
            f"  Notificaciones: {sum(1 for v in self._user_config.values() if v)}/4 activas",
        ]
        kb = _keyboard(_row(_btn("\U0001f504 Refresh", "refresh"), _btn("\U0001f519 Back", "start")))
        await self._send("\n".join(lines), kb=kb)

    # ── Config: capital / risk / SL/TP ───────────────────────────────────
    async def _cmd_config(self):
        cfg = self._user_config
        lines = [
            "\u2699\ufe0f <b>CONFIGURACI\u00d3N DE TRADING</b>",
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            "",
            f"<b>Capital por operaci\u00f3n:</b>  <code>${cfg.get('capital', 100):.2f}</code>",
            f"<b>Riesgo por trade:</b>      <code>{cfg.get('risk_pct', 1):.1f}%</code>",
            f"<b>Stop Loss:</b>            <code>SL {cfg.get('sl_pct', 0.5):.1f}%</code>",
            f"<b>Take Profit:</b>          <code>TP {cfg.get('tp_pct', 1.5):.1f}%</code>",
            f"<b>Apalancamiento:</b>       <code>{settings.LEVERAGE}x</code>",
            f"<b>Margen:</b>               <code>AISLADO</code>",
            "",
            "<i>Editables solo desde .env por ahora:</i>",
            f"  LEVERAGE={settings.LEVERAGE}x, SYMBOL={settings.SYMBOL}",
        ]
        kb = _keyboard(
            _row(_btn("\U0001f4b0 Capital $50", "cfg_cap_50"), _btn("\U0001f4b0 Capital $100", "cfg_cap_100"), _btn("\U0001f4b0 Capital $200", "cfg_cap_200")),
            _row(_btn("\U0001f6a8 SL 0.5%", "cfg_sl_0.5"), _btn("\U0001f6a8 SL 1.0%", "cfg_sl_1.0"), _btn("\U0001f6a8 SL 2.0%", "cfg_sl_2.0")),
            _row(_btn("\U0001f4c8 TP 1.0%", "cfg_tp_1.0"), _btn("\U0001f4c8 TP 1.5%", "cfg_tp_1.5"), _btn("\U0001f4c8 TP 2.5%", "cfg_tp_2.5")),
            _row(_btn("\U0001f519 Volver", "start")),
        )
        await self._send('\n'.join(lines), kb=kb)

    # ── LONG / SHORT execution ─────────────────────────────────────────
    async def _handle_long(self):
        await self._execute_trade("BUY")

    async def _handle_short(self):
        await self._execute_trade("SELL")

    async def _execute_trade(self, side: str):
        cfg = self._user_config
        capital = cfg.get("capital", 100)
        risk_pct = cfg.get("risk_pct", 1.0)
        sl_pct = cfg.get("sl_pct", 0.5)
        tp_pct = cfg.get("tp_pct", 1.5)
        symbol = settings.SYMBOL
        leverage = settings.LEVERAGE

        await self._typing()

        # 1) Get live price
        try:
            price = binance_client.get_current_price()
            if not price or price <= 0:
                price = self._state.get("price", 0)
            if not price or price <= 0:
                await self._send("\u26a0\ufe0f <b>Error</b> \u2014 no se pudo obtener precio")
                return
        except Exception as e:
            await self._send(f"\u26a0\ufe0f <b>Error de precio:</b> <code>{e}</code>")
            return

        # 2) Calculate quantity (truncated to precision)
        raw_qty = (capital * leverage) / price
        step_size = 0.001
        qty = int(raw_qty / step_size) * step_size
        if qty <= 0:
            qty = step_size

        sl_price = price * (1 - sl_pct / 100) if side == "BUY" else price * (1 + sl_pct / 100)
        tp_price = price * (1 + tp_pct / 100) if side == "BUY" else price * (1 - tp_pct / 100)
        side_display = "\U0001f7e2 LONG" if side == "BUY" else "\U0001f7e3 SHORT"
        side_opp = "SELL" if side == "BUY" else "BUY"

        # 3) Set isolated margin & leverage
        try:
            binance_client.client.futures_change_margin_type(
                symbol=symbol, marginType="ISOLATED"
            )
        except Exception:
            pass  # may already be isolated
        try:
            binance_client.client.futures_change_leverage(
                symbol=symbol, leverage=leverage
            )
        except Exception as e:
            await self._send(f"\u26a0\ufe0f <b>Leverage error:</b> <code>{e}</code>")
            return

        # 4) Place MARKET order
        order = None
        try:
            order = await binance_client.place_order(side, qty)
        except Exception as e:
            await self._send(f"\u274c <b>Error orden:</b> <code>{e}</code>")
            return

        if not order:
            await self._send("\u274c <b>Orden rechazada</b> por Binance")
            return

        order_id = order.get("orderId", "?")

        # 5) Place STOP LOSS
        sl_ok = False
        try:
            sl_order = await binance_client.place_stop_loss(side, qty, sl_price)
            sl_ok = bool(sl_order)
        except Exception as e:
            print(f"[TelegramBot] SL error: {e}")

        # 6) Place TAKE PROFIT via limit order
        tp_ok = False
        try:
            tp_order = binance_client.client.futures_create_order(
                symbol=symbol,
                side=side_opp,
                type="LIMIT",
                quantity=qty,
                price=str(round(tp_price, 1)),
                timeInForce="GTC",
                reduceOnly=True,
            )
            tp_ok = bool(tp_order)
        except Exception as e:
            print(f"[TelegramBot] TP error: {e}")

        # 7) Confirmation
        lines = [
            f"\U0001f4e1 <b>ORDEN EJECUTADA</b> \U0001f4e1",
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            f"",
            f"{side_display}  <code>{qty:.3f} {symbol}</code>",
            f"Entry:  <code>${price:,.1f}</code>",
            f"Capital: <code>${capital:.1f}</code> \u00d7 <code>{leverage}x</code> = <code>${capital*leverage:.0f}</code>",
            f"",
            f"\U0001f6a9 <b>SL</b> <code>${sl_price:,.1f}</code> ({sl_pct:.1f}%)  {'\u2705' if sl_ok else '\u274c'}",
            f"\U0001f4c8 <b>TP</b> <code>${tp_price:,.1f}</code> ({tp_pct:.1f}%)  {'\u2705' if tp_ok else '\u274c'}",
            f"",
            f"Orden ID: <code>{order_id}</code>",
            f"\u23f0 {self._safe_get(self._state, 'timestamp')}",
        ]
        await self._send('\n'.join(lines))

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
            return
        text = msg.get("text", "")
        print(f"[TelegramBot] \U0001f4e9 Comando: \"{text[:60]}\"")

        # ── 1) Direct slash commands ──────────────────────────────────────
        if text.startswith("/"):
            cmd = text
            if cmd == "/start":
                await self._cmd_start()
                return
            elif cmd == "/info":
                await self._cmd_info()
                return
            elif cmd == "/signal":
                await self._cmd_signal()
                return
            elif cmd == "/alerts" or cmd == "/config":
                await self._cmd_alerts()
                return
            elif cmd == "/trampas":
                await self._cmd_trampas()
                return
            elif cmd == "/long":
                await self._handle_long()
                return
            elif cmd == "/short":
                await self._handle_short()
                return
            elif cmd == "/config":
                await self._cmd_config()
                return
            elif cmd == "/status":
                await self._cmd_status()
                return
            elif cmd == "/micro":
                await self._cmd_micro()
                return
            elif cmd == "/chart":
                await self._cmd_chart()
                return
            elif cmd == "/scalp":
                await self._cmd_scalp()
                return
            elif cmd == "/ai" or cmd.startswith("/ai "):
                user_q = text[4:].strip() if cmd.startswith("/ai ") else ""
                if not user_q:
                    await self._cmd_ai_help()
                else:
                    reply = await self._typing_for(self._chat_gemini(user_q))
                    await self._send(reply)
                return
            elif cmd == "/refresh":
                await self._cmd_refresh()
                return
            elif cmd == "/ultimo":
                await self._cmd_ultimo()
                return

        # ── 2) Reply keyboard buttons ────────────────────────────────────
        cleaned = self._strip_emoji(text).lower().strip()
        matched_handler = None
        for keyword, handler_name in self.REPLY_BUTTON_MAP.items():
            if keyword in cleaned:
                matched_handler = getattr(self, handler_name, None)
                break

        if matched_handler:
            await self._typing()
            try:
                await matched_handler()
            except Exception as e:
                print(f"[TelegramBot] \u274c Error en {handler_name}: {e}")
                log.exception(f"Handler error: {handler_name}")
            return

        # ── 3) Fallback: Gemini AI chat ──────────────────────────────────
        # Auto-detect chart requests for visual analysis
        wants_chart = any(kw in text.lower() for kw in ['chart', 'grafico', 'gráfico', 'vela', 'candle', 'precio', 'precios'])
        reply = await self._typing_for(self._chat_gemini(text))
        if reply:
            await self._send(reply)
        if wants_chart:
            klines = await self._fetch_klines()
            if klines:
                png = await self._generate_chart(klines)
                if png:
                    await self._send_photo(
                        png,
                        caption="\U0001f4f7 <b>Gr\u00e1fico generado por AI</b> \u2014 velas + VWAP + EMA 20"
                    )

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
        elif data == "status":
            await self._answer_cb(cb_id)
            await self._cmd_status()
        elif data == "config":
            await self._answer_cb(cb_id)
            await self._cmd_config()
        elif data == "operar":
            await self._answer_cb(cb_id)
            await self._handle_operar()
        elif data == "long":
            await self._answer_cb(cb_id, "\U0001f7e2 Ejecutando LONG...")
            await self._handle_long()
        elif data == "short":
            await self._answer_cb(cb_id, "\U0001f7e3 Ejecutando SHORT...")
            await self._handle_short()
        elif data == "trampas":
            await self._answer_cb(cb_id)
            await self._handle_trampas()
        elif data == "scalp":
            await self._answer_cb(cb_id)
            await self._handle_scalp()
        elif data == "micro":
            await self._answer_cb(cb_id)
            await self._handle_micro()
        elif data == "chart":
            await self._answer_cb(cb_id, "\U0001f4f7 Generando gráfico...")
            await self._cmd_chart()
        elif data.startswith("cfg_"):
            await self._answer_cb(cb_id, "\u2705 Config actualizada")
            parts = data.split("_")
            if len(parts) == 3:
                key_map = {'cap': 'capital', 'sl': 'sl_pct', 'tp': 'tp_pct'}
                key = key_map.get(parts[1], parts[1])
                val = float(parts[2])
                self._user_config[key] = val
            await self._cmd_config()
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
