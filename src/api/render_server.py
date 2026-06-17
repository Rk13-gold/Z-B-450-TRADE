"""
BB-450 Render Server — FastAPI con lógica de trading completa.

Recibe datos crudos de Binance desde el bridge (Termux/PC),
calcula indicadores y señales, y los sirve via WebSocket + Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()

from config.settings import settings

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ── Shared state ────────────────────────────────────────────────────
market_state: dict = {"_source": "bridge", "_updated": 0}
pending_orders: list[dict] = []
ws_clients: set[WebSocket] = set()
_start_time: float = time.time()

# ── Math helpers (same as async_data_engine) ─────────────────────────

def _ema(data, period):
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    alpha = 2.0 / (period + 1)
    out = np.empty_like(data, dtype=float)
    out[0] = data[0]
    for i in range(1, len(data)):
        out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
    return out

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd_line = ef - es
    sig_line = _ema(macd_line, signal)
    hist = macd_line - sig_line
    return float(macd_line[-1]), float(sig_line[-1]), float(hist[-1])

# ── Compute all indicators from raw data ────────────────────────────

def compute_indicators(price: float, klines: list, agg_trades: list, depth: dict,
                       funding_rate: float, open_interest: float) -> dict:
    ind = {}
    of = {}

    if not klines or len(klines) < 30:
        return {"indicators": {}, "order_flow": {}, "momentum": {}, "signal": "NINGUNA", "whale_walls": {}}

    closes = np.array([float(k[4]) for k in klines])
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # VWAP
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(klines))]
    cum_pv = sum(typical[i] * volumes[i] for i in range(len(klines)))
    cum_v = sum(volumes)
    ind["vwap"] = cum_pv / cum_v if cum_v > 0 else 0
    ind["price_vwap_dist"] = ((price - ind["vwap"]) / max(ind["vwap"], 0.0001)) * 100 if ind["vwap"] else 0

    # EMAs (simple means)
    ind["ema_20"] = float(np.mean(closes[-20:])) if len(closes) >= 20 else 0
    ind["ema_50"] = float(np.mean(closes[-50:])) if len(closes) >= 50 else ind["ema_20"]

    # Bollinger
    sma20 = float(np.mean(closes[-20:]))
    std = float(np.std(closes[-20:]))
    ind["bb_upper"] = sma20 + 2 * std
    ind["bb_middle"] = sma20
    ind["bb_lower"] = sma20 - 2 * std
    if ind["bb_upper"] != ind["bb_lower"]:
        ind["bb_position"] = ((closes[-1] - ind["bb_lower"]) / (ind["bb_upper"] - ind["bb_lower"])) * 100
    else:
        ind["bb_position"] = 50.0

    # RSI + MACD
    ind["rsi"] = calc_rsi(closes)
    macd_l, macd_s, macd_h = calc_macd(closes)
    ind["macd"] = macd_l
    ind["macd_signal"] = macd_s
    ind["macd_hist"] = macd_h

    # ATR
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); l = float(klines[i][3])
        pc = float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    ind["atr"] = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0

    ind["avg_volume"] = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0
    ind["day_high"] = max(highs[-100:]) if len(highs) >= 100 else max(highs) if highs else price
    ind["day_low"] = min(lows[-100:]) if len(lows) >= 100 else min(lows) if lows else price

    # Trend
    if ind["ema_20"] > ind["ema_50"]:
        ind["trend"] = "ALCISTA"
    elif ind["ema_20"] < ind["ema_50"]:
        ind["trend"] = "BAJISTA"
    else:
        ind["trend"] = "NEUTRAL"

    # Order flow (delta from aggTrades)
    if agg_trades:
        buy_vol = sum(float(t["q"]) for t in agg_trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in agg_trades if t["m"])
    else:
        buy_vol = sell_vol = 0
    delta = buy_vol - sell_vol

    of["delta"] = round(delta, 2)
    of["cvd"] = round(delta, 2)
    of["buy_volume"] = round(buy_vol, 4)
    of["sell_volume"] = round(sell_vol, 4)
    of["window_buy_volume"] = round(buy_vol, 4)
    of["window_sell_volume"] = round(sell_vol, 4)
    of["funding_rate"] = round(funding_rate, 4)
    of["open_interest"] = round(open_interest, 2)
    of["oi_delta_5m"] = 0.0

    # Momentum
    mom = {
        "tick_speed": len(agg_trades) if agg_trades else 0,
        "cancel_rate": 0,
        "spread_velocity": 0,
        "pinam": 0,
        "volatility_explosion": False,
    }

    # Whale walls (from depth)
    ww = {}
    if depth:
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        all_qties = [q for _, q in bids] + [q for _, q in asks]
        ww_threshold = np.mean(all_qties) + 3 * np.std(all_qties) if len(all_qties) > 5 else 10.0
        buy_walls = [{"price": p, "quantity": q} for p, q in bids if q >= ww_threshold]
        sell_walls = [{"price": p, "quantity": q} for p, q in asks if q >= ww_threshold]
        total_buy = sum(w["quantity"] for w in buy_walls)
        total_sell = sum(w["quantity"] for w in sell_walls)
        imbalance = (total_buy - total_sell) / max(total_buy + total_sell, 0.001)
        ww = {
            "buy_walls": buy_walls[:5], "sell_walls": sell_walls[:5],
            "total_buy_walls": round(total_buy, 4), "total_sell_walls": round(total_sell, 4),
            "imbalance": round(imbalance, 4),
            "signal": "BUY_WALL" if imbalance > 0.3 else "SELL_WALL" if imbalance < -0.3 else "NEUTRAL",
        }

    # Signal
    lc = 0
    if ind.get("rsi", 50) < 30: lc += 1
    if ind.get("bb_position", 50) < 20: lc += 1
    if ind.get("macd_hist", 0) > 0: lc += 1
    if of.get("delta", 0) > 100: lc += 1
    if ind.get("trend") == "ALCISTA": lc += 1

    sc = 0
    if ind.get("rsi", 50) > 70: sc += 1
    if ind.get("bb_position", 50) > 80: sc += 1
    if ind.get("macd_hist", 0) < 0: sc += 1
    if of.get("delta", 0) < -100: sc += 1
    if ind.get("trend") == "BAJISTA": sc += 1

    signal = "COMPRA" if lc >= 3 else "VENTA" if sc >= 3 else "NINGUNA"

    return {
        "indicators": ind,
        "order_flow": of,
        "momentum": mom,
        "whale_walls": ww,
        "signal": signal,
        "change_pct": 0,
    }


# ── FastAPI app ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start TelegramBot in background (no depende de Binance)
    try:
        from src.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot.start()
        print("[RenderServer] 🤖 Telegram: ACTIVADO")
    except Exception as e:
        print(f"[RenderServer] ⚠ Telegram: {e}")

    print(f"[RenderServer] ✅ BB-450 listo | Puerto: {os.environ.get('PORT', '8000')}")
    yield
    print("[RenderServer] 🔴 Apagado")


app = FastAPI(title="BB-450 Trading Bot", version="4.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# ── Models ──────────────────────────────────────────────────────────

class PushData(BaseModel):
    price: float = 0
    klines: list = []
    agg_trades: list = []
    depth: dict = {}
    funding_rate: float = 0
    open_interest: float = 0


# ── Endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root():
    p = market_state.get("price", 0)
    sig = market_state.get("signal", "NINGUNA")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>BB-450</title>
<meta http-equiv="refresh" content="5">
<style>body{{background:#0a0a0f;color:#00ff88;font-family:monospace;padding:40px}}</style>
</head><body>
<h1>🟢 BB-450 RUNNING</h1>
<p>Precio: <b>${p:,.2f}</b></p>
<p>Señal: <b>{sig}</b></p>
<p>Symbol: <b>{settings.get_symbol()}</b></p>
<p>Uptime: <b>{int(time.time() - _start_time)}s</b></p>
<hr>
<p><a href="/health" style="color:#00ff88">/health</a> |
<a href="/api/state" style="color:#00ff88">/api/state</a> |
WebSocket: <code>/ws</code></p>
</body></html>""")


@app.get("/health")
async def health():
    p = market_state.get("price", 0)
    sig = market_state.get("signal", "NINGUNA")
    return {"status": "ok", "uptime": int(time.time() - _start_time),
            "price": p, "signal": sig, "symbol": settings.get_symbol(),
            "websocket_clients": len(ws_clients)}


@app.get("/api/state")
async def api_state():
    return {"market": market_state, "uptime": int(time.time() - _start_time),
            "symbol": settings.get_symbol(), "timestamp": time.time()}


@app.post("/api/push")
async def push_data(data: PushData):
    """Receive raw Binance data from local bridge, compute everything."""
    global market_state

    # Compute all indicators on Render
    result = compute_indicators(
        price=data.price,
        klines=data.klines,
        agg_trades=data.agg_trades,
        depth=data.depth,
        funding_rate=data.funding_rate,
        open_interest=data.open_interest,
    )

    market_state = {
        "price": data.price,
        "signal": result["signal"],
        "change_pct": result.get("change_pct", 0),
        "indicators": result["indicators"],
        "order_flow": result["order_flow"],
        "momentum": result["momentum"],
        "whale_walls": result["whale_walls"],
        "klines": data.klines[-120:],
        "trades": data.agg_trades,
        "funding_rate": data.funding_rate,
        "open_interest": data.open_interest,
        "_source": "bridge",
        "_updated": time.time(),
    }

    # Return pending orders for the bridge to execute
    cmds = list(pending_orders)
    pending_orders.clear()
    return {"status": "ok", "pending_orders": cmds}


# ── WebSocket ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    print(f"[WS] Cliente conectado ({len(ws_clients)} total)")

    async def broadcast():
        while True:
            try:
                await ws.send_json({
                    "type": "market_state",
                    "data": dict(market_state),
                    "timestamp": time.time(),
                })
            except Exception:
                break
            await asyncio.sleep(0.3)

    async def reader():
        while True:
            try:
                raw = await ws.receive_text()
                data = json.loads(raw)
                action = data.get("action", "").upper()
                if action in ("TRADE", "CLOSE"):
                    pending_orders.append(data)
                    await ws.send_json({"type": "command_ack", "action": action,
                                        "status": "ok", "message": "Orden encolada para bridge"})
                elif action == "PING":
                    await ws.send_json({"type": "pong"})
                else:
                    await ws.send_json({"type": "error", "message": f"Acción desconocida: {action}"})
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "JSON inválido"})
            except Exception as e:
                log.error(f"[WS] Error: {e}")
                break

    await asyncio.gather(broadcast(), reader(), return_exceptions=True)
    ws_clients.discard(ws)
    print(f"[WS] Cliente desconectado ({len(ws_clients)} restantes)")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("src.api.render_server:app", host="0.0.0.0", port=port, log_level="info")
