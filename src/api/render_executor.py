"""Headless Order Executor for Render — no PyQt5 dependency."""

import logging
import math
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config.settings import settings

log = logging.getLogger(__name__)


class HeadlessOrderExecutor:
    """Lightweight order executor that works without Qt.

    Compatible with the TelegramBot interface (get_position_with_pnl,
    close_all_positions, _client).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._client: Optional[Client] = None
        self._running = True
        self._has_open_position = False
        self._position_info: dict = {}
        self._last_trade_time: float = 0.0
        self._cooldown_period: float = 60.0

    # ── Public API ─────────────────────────────────────────────────

    def start(self):
        """Initialize Binance client."""
        try:
            self._client = self._build_client()
            log.info("HeadlessOrderExecutor initialized")
        except Exception as e:
            log.error("Failed to create Binance client: %s", e)

    def stop(self):
        self._running = False

    def get_position_with_pnl(self) -> Optional[dict]:
        """Fetch current position from Binance with live PnL."""
        if self._client is None:
            return None
        try:
            pos = self._client.futures_position_information(
                symbol=settings.get_symbol()
            )
            if not pos:
                return None
            for p in pos:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                entry = float(p.get("entryPrice", 0))
                mark = float(p.get("markPrice", 0))
                pnl_val = float(p.get("unRealizedProfit", 0))
                liq = float(p.get("liquidationPrice", 0))
                lev = int(float(p.get("leverage", 1)))
                pos_value = abs(amt) * entry
                pnl_pct = (pnl_val / pos_value * 100) if pos_value > 0 else 0.0
                return {
                    "direction": "LONG" if amt > 0 else "SHORT",
                    "entry_qty": abs(amt),
                    "entry_price": entry,
                    "mark_price": mark,
                    "pnl": pnl_val,
                    "pnl_pct": pnl_pct,
                    "liquidation_price": liq,
                    "leverage": lev,
                }
        except Exception as e:
            log.warning("get_position_with_pnl error: %s", e)
        return None

    def close_all_positions(self) -> dict:
        """Close all open positions."""
        result = {"success": False, "message": "", "data": {}}
        if self._client is None:
            result["message"] = "Cliente Binance no disponible"
            return result
        try:
            positions = self._client.futures_position_information(
                symbol=settings.get_symbol()
            )
            closed_any = False
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                qty = abs(amt)
                self._client.futures_create_order(
                    symbol=settings.get_symbol(),
                    side=side,
                    type="MARKET",
                    quantity=self._truncate_qty(qty),
                    reduceOnly=True,
                )
                closed_any = True
                log.warning("Closed position %s %s %s", side, qty, settings.get_symbol())

            if closed_any:
                result["success"] = True
                result["message"] = "Posiciones cerradas"
                self._has_open_position = False
                self._position_info = {}
            else:
                result["message"] = "No hay posiciones abiertas"
                result["success"] = True
        except Exception as e:
            result["message"] = f"Error cerrando posiciones: {e}"
            log.error("close_all_positions error: %s", e)
        return result

    def execute_trade_signal(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        leverage: int = None,
        capital: float = None,
    ) -> dict:
        """Execute a trade signal."""
        result = {"success": False, "message": "", "data": {}}
        if self._client is None:
            result["message"] = "Cliente Binance no disponible"
            return result

        with self._lock:
            now = time.time()
            if now - self._last_trade_time < self._cooldown_period:
                result["message"] = "Cooldown activo, espera %.0fs" % (
                    self._cooldown_period - (now - self._last_trade_time)
                )
                return result

            try:
                lev = leverage or settings.LEVERAGE
                cap = capital or settings.GLOBAL_TRADE_AMOUNT

                self._client.futures_change_leverage(
                    symbol=settings.get_symbol(), leverage=lev
                )

                qty = self._calculate_quantity(cap, lev, entry)
                if qty <= 0:
                    result["message"] = "Cantidad calculada inválida"
                    return result

                side = "BUY" if direction.upper() in ("LONG", "BUY") else "SELL"

                order = self._client.futures_create_order(
                    symbol=settings.get_symbol(),
                    side=side,
                    type="MARKET",
                    quantity=qty,
                )

                if order and order.get("status") in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                    self._has_open_position = True
                    self._position_info = {
                        "direction": direction.upper(),
                        "entry_price": entry,
                        "quantity": qty,
                        "sl": sl,
                        "tp": tp,
                        "leverage": lev,
                    }
                    self._last_trade_time = now

                    if sl > 0:
                        self._place_stop_loss(side, qty, sl)
                    if tp > 0:
                        self._place_take_profit(side, qty, tp)

                    result["success"] = True
                    result["message"] = f"{direction.upper()} ejecutada: {qty} @ market"
                    result["data"] = self._position_info
                else:
                    result["message"] = f"Orden no llenada: {order}"

            except BinanceAPIException as e:
                result["message"] = f"Error API: {e.message}"
                log.error("Trade execution error: %s", e)
            except Exception as e:
                result["message"] = f"Error: {e}"
                log.error("Trade execution error: %s", e)

        return result

    # ── Private ────────────────────────────────────────────────────

    def _build_client(self) -> Optional[Client]:
        if settings.BINANCE_TESTNET:
            return Client(settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY, testnet=True)
        return Client(settings.BINANCE_REAL_API_KEY, settings.BINANCE_REAL_SECRET_KEY, testnet=False)

    def _calculate_quantity(self, capital: float, leverage: int, price: float) -> float:
        if price <= 0:
            return 0
        raw = (capital * leverage) / price
        return self._truncate_qty(raw)

    def _truncate_qty(self, qty: float) -> float:
        """Truncate to 3 decimal places (BTCUSDT precision)."""
        return float(Decimal(str(qty)).quantize(Decimal("0.001"), rounding=ROUND_DOWN))

    def _place_stop_loss(self, side: str, qty: float, sl_price: float):
        stop_side = "SELL" if side == "BUY" else "BUY"
        try:
            self._client.futures_create_order(
                symbol=settings.get_symbol(),
                side=stop_side,
                type="STOP_MARKET",
                quantity=qty,
                stopPrice=sl_price,
                reduceOnly=True,
            )
        except Exception as e:
            log.error("Failed to place SL: %s", e)

    def _place_take_profit(self, side: str, qty: float, tp_price: float):
        tp_side = "SELL" if side == "BUY" else "BUY"
        try:
            self._client.futures_create_order(
                symbol=settings.get_symbol(),
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                quantity=qty,
                stopPrice=tp_price,
                reduceOnly=True,
            )
        except Exception as e:
            log.error("Failed to place TP: %s", e)
