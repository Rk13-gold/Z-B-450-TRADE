"""
BB-450 Render Server — FastAPI app with REST, WebSocket, and health check.

Runs the trading engine headlessly (no PyQt5) and exposes:
  - GET  /health      → health check (Render keeps alive)
  - WS   /ws          → real-time market data + trading commands
  - GET  /api/state   → full market snapshot as JSON
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
from fastapi.responses import JSONResponse

load_dotenv()

from config.settings import settings
from src.engine.async_data_engine import AsyncDataEngine
from src.engine.binance_client import binance_client
from src.engine.order_flow import order_flow_engine
from src.telegram_bot import TelegramBot
from src.api.render_executor import HeadlessOrderExecutor

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── Shared state ────────────────────────────────────────────────────
market_state: dict = {}
_start_time: float = time.time()

# ── Components ──────────────────────────────────────────────────────
data_engine: Optional[AsyncDataEngine] = None
telegram_bot: Optional[TelegramBot] = None
order_executor: Optional[HeadlessOrderExecutor] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global data_engine, telegram_bot, order_executor

    # 1. Headless Order Executor
    order_executor = HeadlessOrderExecutor()
    order_executor.start()

    # 2. Async Data Engine (fills market_state dict)
    data_engine = AsyncDataEngine(market_state)
    data_engine.start()

    # 3. Telegram Bot
    telegram_bot = TelegramBot(order_executor=order_executor)
    telegram_bot.set_data_engine(data_engine)
    telegram_bot.start()

    print(f"[RenderServer] ✅ BB-450 iniciado en modo headless")
    print(f"[RenderServer] 🌐 Puerto: {os.environ.get('PORT', '8000')}")
    print(f"[RenderServer] 📊 Símbolo: {settings.get_symbol()}")
    print(f"[RenderServer] 🤖 Telegram: {'ACTIVADO' if settings.TELEGRAM_ENABLED else 'DESACTIVADO'}")

    yield  # ⇐ app runs here

    # Shutdown
    print("[RenderServer] 🔴 Apagando...")
    if telegram_bot:
        telegram_bot.stop()
    if data_engine:
        data_engine.stop()
    if order_executor:
        order_executor.stop()
    print("[RenderServer] ✅ Apagado completo")


app = FastAPI(
    title="BB-450 Trading Bot",
    version="4.0",
    lifespan=lifespan,
)

# ── CORS (allow GitHub Pages origin) ────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST endpoints ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint — Render keeps the service alive."""
    p = market_state.get("price", 0)
    sig = market_state.get("signal", "NINGUNA")
    return {
        "status": "ok",
        "uptime": int(time.time() - _start_time),
        "price": p,
        "signal": sig,
        "symbol": settings.get_symbol(),
    }


@app.get("/api/state")
async def api_state():
    """Full market snapshot as JSON."""
    return {
        "market": market_state,
        "uptime": int(time.time() - _start_time),
        "symbol": settings.get_symbol(),
        "timestamp": time.time(),
    }


# ── WebSocket ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client_addr = ws.client
    print(f"[WS] Cliente conectado: {client_addr}")

    # Broadcast loop for this client
    async def broadcast():
        while True:
            try:
                state_snapshot = {
                    "type": "market_state",
                    "data": dict(market_state),
                    "timestamp": time.time(),
                }
                await ws.send_json(state_snapshot)
            except Exception:
                break
            await asyncio.sleep(0.3)

    # Command reader
    async def reader():
        while True:
            try:
                raw = await ws.receive_text()
                data = json.loads(raw)
                action = data.get("action", "").upper()
                print(f"[WS] Comando: {action} de {client_addr}")

                if action == "TRADE":
                    direction = data.get("direction", "")
                    sl = float(data.get("sl", 0))
                    tp = float(data.get("tp", 0))
                    leverage = int(data.get("leverage", settings.LEVERAGE))
                    capital = float(data.get("capital", settings.GLOBAL_TRADE_AMOUNT))

                    if direction not in ("LONG", "SHORT"):
                        await ws.send_json({"type": "error", "message": "Dirección inválida"})
                        continue

                    price = market_state.get("price", 0)
                    result = order_executor.execute_trade_signal(
                        direction=direction,
                        entry=price,
                        sl=sl,
                        tp=tp,
                        leverage=leverage,
                        capital=capital,
                    )
                    await ws.send_json({
                        "type": "command_ack",
                        "action": "TRADE",
                        "status": "ok" if result["success"] else "error",
                        "message": result["message"],
                        "data": result.get("data", {}),
                    })

                elif action == "CLOSE":
                    result = order_executor.close_all_positions()
                    await ws.send_json({
                        "type": "command_ack",
                        "action": "CLOSE",
                        "status": "ok" if result["success"] else "error",
                        "message": result["message"],
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

    # Run both tasks concurrently
    await asyncio.gather(broadcast(), reader(), return_exceptions=True)
    print(f"[WS] Cliente desconectado: {client_addr}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "src.api.render_server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
