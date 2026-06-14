"""
ws_client.py — Asynchronous WebSocket client for BB-450 with auto-reconnect.

Message types received:
  - market_state     → on_market_state(data)
  - notification     → on_notification(data)
  - command_ack      → on_command_ack(action, status, result)

Command types sent:
  - TRADE            {"action": "TRADE", "direction": "ALZA", "sl": 49750, "tp": 50500, "leverage": 40, "risk_pct": 1.0, "split": true}
  - CLOSE            {"action": "CLOSE"}
  - CLOSE_PARTIAL    {"action": "CLOSE_PARTIAL", "pct": 50}
  - MOVE_SL          {"action": "MOVE_SL", "price": 50100}
  - MOVE_TP          {"action": "MOVE_TP", "price": 50500}
  - SET_LEVERAGE     {"action": "SET_LEVERAGE", "leverage": 40}
  - AI_QUERY         {"action": "AI_QUERY", "question": "..."}
  - GET_RISK         {"action": "GET_RISK"}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets
from websockets.asyncio.client import connect as ws_connect

from config import WS_URI, WS_RECONNECT_DELAY, WS_PING_INTERVAL, WS_PING_TIMEOUT

log = logging.getLogger(__name__)


class BB450WSClient:
    """Asynchronous WebSocket client with auto-reconnect."""

    def __init__(self, uri: str = WS_URI):
        self.uri: str = uri
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running: bool = False
        self._reconnect_delay: float = WS_RECONNECT_DELAY

        # ── Callbacks (override from the UI) ──────────────────────
        self.on_market_state: Callable[[dict], Any] = lambda d: None
        self.on_notification: Callable[[dict], Any] = lambda d: None
        self.on_command_ack: Callable[[str, str, dict], Any] = lambda a, s, r: None
        self.on_status: Callable[[bool], Any] = lambda c: None

    # ── Public API ─────────────────────────────────────────────────

    async def connect(self):
        """Infinite connection + retry loop."""
        self._running = True
        while self._running:
            try:
                async with ws_connect(
                    self.uri,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self.on_status(True)
                    log.info("[WS] Connected to %s", self.uri)
                    await self._reader(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("[WS] Connection lost: %s — retry in %.0fs",
                            exc, self._reconnect_delay)
            finally:
                self._ws = None
                self.on_status(False)
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)

    async def disconnect(self):
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def send_command(self, cmd: dict) -> bool:
        """Send a JSON command to the server."""
        if self._ws is None:
            log.warning("[WS] Send attempt without connection: %s", cmd)
            return False
        try:
            payload = json.dumps(cmd, ensure_ascii=False)
            await self._ws.send(payload)
            log.info("[WS] Command sent: %s", cmd)
            return True
        except Exception as exc:
            log.error("[WS] Send error %s: %s", cmd, exc)
            return False

    # ── Convenience command helpers ────────────────────────────────

    async def trade(self, direction: str, sl: float = 0, tp: float = 0,
                    leverage: int = 40, risk_pct: float = 1.0,
                    split: bool = False) -> bool:
        return await self.send_command({
            "action": "TRADE", "direction": direction,
            "sl": sl, "tp": tp, "leverage": leverage,
            "risk_pct": risk_pct, "split": split,
        })

    async def close_all(self) -> bool:
        return await self.send_command({"action": "CLOSE"})

    async def close_partial(self, pct: float) -> bool:
        return await self.send_command({"action": "CLOSE_PARTIAL", "pct": pct})

    async def move_sl(self, price: float) -> bool:
        return await self.send_command({"action": "MOVE_SL", "price": price})

    async def move_tp(self, price: float) -> bool:
        return await self.send_command({"action": "MOVE_TP", "price": price})

    async def set_leverage(self, leverage: int) -> bool:
        return await self.send_command({"action": "SET_LEVERAGE", "leverage": leverage})

    async def ai_query(self, question: str) -> bool:
        return await self.send_command({"action": "AI_QUERY", "question": question})

    async def get_risk(self) -> bool:
        return await self.send_command({"action": "GET_RISK"})

    # ── Private ────────────────────────────────────────────────────

    async def _reader(self, ws: websockets.WebSocketClientProtocol):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("[WS] Invalid JSON: %s", exc)
                continue

            msg_type = msg.get("type", "")

            if msg_type == "market_state":
                self.on_market_state(msg.get("data", {}))

            elif msg_type == "notification":
                self.on_notification(msg)

            elif msg_type == "command_ack":
                self.on_command_ack(
                    msg.get("action", ""),
                    msg.get("status", ""),
                    msg.get("result", {}),
                )

            else:
                log.debug("[WS] Unknown message type: %s", msg_type)
