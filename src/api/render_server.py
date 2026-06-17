"""
BB-450 Render Server — FastAPI app with REST, WebSocket, and health check.

Architecture:
  - Can receive live market data via POST /api/push from a local bridge
  - Broadcasts market_state to all WebSocket clients
  - Runs Telegram bot for alerts and commands
  - Falls back gracefully if Binance is unreachable
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()

from config.settings import settings

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── Shared state ────────────────────────────────────────────────────
market_state: dict = {}
pending_orders: list[dict] = []
_start_time: float = time.time()

# ── Components (optional — gracefully skipped if Binance blocked) ──
data_engine = None
telegram_bot = None
order_executor = None

# ── WebSocket clients ───────────────────────────────────────────────
ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_bot, order_executor

    # Try to init components, but don't crash if Binance is blocked
    try:
        from src.api.render_executor import HeadlessOrderExecutor
        order_executor = HeadlessOrderExecutor()
        order_executor.start()
    except Exception as e:
        print(f"[RenderServer] ⚠ Executor no disponible: {e}")

    try:
        from src.engine.async_data_engine import AsyncDataEngine
        de = AsyncDataEngine(market_state)
        de.start()
    except Exception as e:
        print(f"[RenderServer] ⚠ DataEngine no disponible: {e}")

    try:
        from src.telegram_bot import TelegramBot
        telegram_bot = TelegramBot(order_executor=order_executor)
        if data_engine:
            telegram_bot.set_data_engine(data_engine)
        telegram_bot.start()
        print(f"[RenderServer] 🤖 Telegram: ACTIVADO")
    except Exception as e:
        print(f"[RenderServer] ⚠ Telegram no disponible: {e}")

    print(f"[RenderServer] ✅ BB-450 iniciado | Puerto: {os.environ.get('PORT', '8000')}")

    yield

    print("[RenderServer] 🔴 Apagando...")
    if telegram_bot:
        telegram_bot.stop()
    if order_executor:
        order_executor.stop()
    print("[RenderServer] ✅ Apagado completo")


app = FastAPI(title="BB-450 Trading Bot", version="4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST endpoints ──────────────────────────────────────────────────

@app.get("/")
async def root():
    p = market_state.get("price", 0)
    sig = market_state.get("signal", "NINGUNA")
    mode = "BRIDGE" if market_state.get("_source") == "bridge" else "DIRECT"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>BB-450</title>
<meta http-equiv="refresh" content="5">
<style>body{{background:#0a0a0f;color:#00ff88;font-family:monospace;padding:40px}}</style>
</head><body>
<h1>🟢 BB-450 RUNNING ({mode})</h1>
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
    ws_count = len(ws_clients)
    return {
        "status": "ok",
        "uptime": int(time.time() - _start_time),
        "price": p,
        "signal": sig,
        "symbol": settings.get_symbol(),
        "websocket_clients": ws_count,
        "mode": "bridge" if market_state.get("_source") == "bridge" else "direct",
    }


@app.get("/api/state")
async def api_state():
    return {
        "market": market_state,
        "uptime": int(time.time() - _start_time),
        "symbol": settings.get_symbol(),
        "timestamp": time.time(),
    }


class PushData(BaseModel):
    price: float = 0
    signal: str = "NINGUNA"
    change_pct: float = 0
    indicators: dict = {}
    order_flow: dict = {}
    liquidity: dict = {}
    momentum: dict = {}
    klines: list = []
    whale_walls: dict = {}
    technical_levels: dict = {}
    trades: list = []


@app.post("/api/push")
async def push_data(data: PushData):
    """Receive market data from local bridge script."""
    global market_state
    market_state = {
        "price": data.price,
        "signal": data.signal,
        "change_pct": data.change_pct,
        "indicators": data.indicators,
        "order_flow": data.order_flow,
        "liquidity": data.liquidity,
        "momentum": data.momentum,
        "klines": data.klines,
        "whale_walls": data.whale_walls,
        "technical_levels": data.technical_levels,
        "trades": data.trades,
        "_source": "bridge",
        "_updated": time.time(),
    }
    # Return any pending orders for the bridge to execute
    cmds = list(pending_orders)
    pending_orders.clear()
    return {"status": "ok", "pending_orders": cmds}


# ── WebSocket ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    client_addr = ws.client
    print(f"[WS] Cliente conectado: {client_addr} ({len(ws_clients)} total)")

    async def broadcast():
        while True:
            try:
                snapshot = {
                    "type": "market_state",
                    "data": dict(market_state),
                    "timestamp": time.time(),
                }
                await ws.send_json(snapshot)
            except Exception:
                break
            await asyncio.sleep(0.3)

    async def reader():
        while True:
            try:
                raw = await ws.receive_text()
                data = json.loads(raw)
                action = data.get("action", "").upper()
                print(f"[WS] Comando: {action} de {client_addr}")

                if action in ("TRADE", "CLOSE"):
                    pending_orders.append(data)
                    await ws.send_json({
                        "type": "command_ack",
                        "action": action,
                        "status": "ok",
                        "message": "Orden enviada al bridge local",
                    })
                elif action == "PING":
                    await ws.send_json({"type": "pong"})
                else:
                    await ws.send_json({
                        "type": "error",
                        "message": f"Acción desconocida: {action}",
                    })

            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "JSON inválido"})
            except Exception as e:
                log.error(f"[WS] Error: {e}")
                break

    await asyncio.gather(broadcast(), reader(), return_exceptions=True)
    ws_clients.discard(ws)
    print(f"[WS] Cliente desconectado: {client_addr} ({len(ws_clients)} restantes)")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("src.api.render_server:app", host="0.0.0.0", port=port, log_level="info")
