import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from src.api.notify import get_notifier

log = logging.getLogger(__name__)


class BB450WSServer:
    """WebSocket server for BB-450 mobile terminal (Termux).

    Broadcasts market_state every 200ms + notification events to all
    connected clients.  Accepts full trading commands from the mobile app.
    """

    def __init__(
        self,
        data_provider: Callable[[], dict],
        on_command_callback: Optional[Callable[[dict], None]] = None,
        host: str = "0.0.0.0",
        port: int = 8765,
    ):
        self._data_provider = data_provider
        self._on_command = on_command_callback
        self._host = host
        self._port = port
        self._clients: Set[WebSocketServerProtocol] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cached_state: dict = {}

    # ── Public API ─────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            log.warning("[WS] Server already running")
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info(f"[WS] Server started on {self._host}:{self._port} (daemon)")

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.stop()

    def broadcast(self, data: dict):
        """Thread-safe broadcast of a dict to all connected clients.

        Called from any thread (e.g. NotificationManager).
        """
        if self._loop and self._running:
            asyncio.run_coroutine_threadsafe(
                self._send_to_all(json.dumps(data, default=str)),
                self._loop,
            )

    # ── Asyncio internals ──────────────────────────────────────────

    async def _handler(self, websocket: WebSocketServerProtocol):
        self._clients.add(websocket)
        remote = websocket.remote_address
        log.info(f"[WS] Client connected: {remote}")
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error", "message": "Invalid JSON",
                    }))
                    continue

                if not isinstance(data, dict) or "action" not in data:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid format: {'action': '...'} required",
                    }))
                    continue

                action = data["action"]
                log.info(f"[WS] Command from {remote}: {action}")

                if self._on_command:
                    try:
                        result = self._on_command(data)
                        await websocket.send(json.dumps({
                            "type": "command_ack",
                            "action": action,
                            "status": "ok",
                            "result": result or {},
                            "timestamp": time.time(),
                        }))
                    except Exception as e:
                        log.error(f"[WS] Command error {action}: {e}")
                        await websocket.send(json.dumps({
                            "type": "command_ack",
                            "action": action,
                            "status": "error",
                            "message": str(e),
                            "timestamp": time.time(),
                        }))
                else:
                    await websocket.send(json.dumps({
                        "type": "command_ack",
                        "action": action,
                        "status": "error",
                        "message": "No command handler configured",
                        "timestamp": time.time(),
                    }))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            log.info(f"[WS] Client disconnected: {remote}")

    async def _broadcast_state(self):
        while self._running:
            try:
                data = self._data_provider()
                if data:
                    self._cached_state = data
                if self._cached_state and self._clients:
                    msg = json.dumps({
                        "type": "market_state",
                        "data": self._cached_state,
                        "timestamp": time.time(),
                    }, default=str)
                    await self._send_to_all(msg)
            except Exception as e:
                log.error(f"[WS] Broadcast error: {e}")
            await asyncio.sleep(0.2)

    async def _send_to_all(self, msg: str):
        if not self._clients:
            return
        await asyncio.gather(
            *(c.send(msg) for c in self._clients.copy()),
            return_exceptions=True,
        )

    async def _serve(self):
        self._running = True
        # Register this server as the WS broadcaster in NotificationManager
        get_notifier().set_ws_broadcaster(self.broadcast)
        async with websockets.serve(self._handler, self._host, self._port):
            log.info(f"[WS] Listening on {self._host}:{self._port}")
            await self._broadcast_state()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            log.error(f"[WS] Server terminated: {e}")
