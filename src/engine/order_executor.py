"""
order_executor.py — Uniﬁed order execution engine for BB-450.

Architecture
────────────
  OrderExecutor : QThread that receives trade signals and executes them
                  via Binance Futures REST API. Supports REAL and TESTNET
                  environments via a single boolean switch.

Flow
────
  1. Call execute_trade_signal(direction, entry, sl, tp, leverage, capital)
  2. Signal queued in a thread-safe Queue
  3. Background thread picks it up and:
       a. Builds Binance Futures client (testnet or real)
       b. Sets leverage
       c. Calculates quantity (capital * leverage / entry, truncated)
       d. Sends MARKET entry
       e. On success: sends STOP_MARKET (SL) + TAKE_PROFIT_MARKET (TP)
  4. order_result signal emits (success, message, data)
"""

from __future__ import annotations

import logging
import queue
import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config.settings import settings

log = logging.getLogger(__name__)

# ── ENVIRONMENT SWITCH ──────────────────────────────────────────────────────
USE_TESTNET = True   # False → REAL account; True → Binance Testnet

# ── ENDPOINTS ────────────────────────────────────────────────────────────────
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
TESTNET_WS_URL = "wss://stream.binancefuture.com"
REAL_BASE_URL = "https://fapi.binance.com"
REAL_WS_URL = "wss://fstream.binance.com"

# ── ORDER CONFIG ────────────────────────────────────────────────────────────
SYMBOL = settings.SYMBOL or "BTCUSDT"
MAX_RETRIES = 3
RETRY_DELAY_S = 0.5


class OrderExecutor(QThread):
    """Background thread for executing Binance Futures orders.

    Signals
    -------
    order_result(success: bool, message: str, data: dict)
        Emitted after each execution attempt — drives both UI updates
        and Telegram notifications.
    """

    order_result = pyqtSignal(bool, str, dict)

    def __init__(self, parent: Optional = None):
        super().__init__(parent)
        self._queue: queue.Queue[Optional[dict]] = queue.Queue()
        self._running = True
        self._client: Optional[Client] = None

        # ── Safety: cooldown anti-spam ───────────────────────────────
        self._last_trade_time: float = 0.0
        self._cooldown_period: float = 60.0   # segundos entre trades

    # ── Public API ──────────────────────────────────────────────────────────

    def execute_trade_signal(
        self,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        leverage: int = 100,
        capital: float = 100.0,
    ) -> None:
        """Queue a trade signal for background execution.

        Parameters
        ----------
        direction : str
            'ALZA' (LONG) or 'BAJA' (SHORT).
        entry_price : float
            Current market price for entry.
        sl_price : float
            Stop-loss price.
        tp_price : float
            Take-profit price.
        leverage : int
            Leverage multiplier (default 100x).
        capital : float
            Available capital in USDT (default 100).
        """

        # ── SAFETY LAYER 1: Validación anti-ceros ────────────────────
        if direction not in ('ALZA', 'BAJA') or entry_price <= 0 or sl_price <= 0 or tp_price <= 0:
            print("[⚠️ RECHAZADO] Intento de ejecución bloqueado: Datos inválidos o en cero.")
            log.warning(
                f"[OrderExec] REJECTED — direction={direction} "
                f"entry={entry_price:.0f} sl={sl_price:.0f} tp={tp_price:.0f}"
            )
            return

        # ── SAFETY LAYER 3: Cooldown anti-spam ──────────────────────
        now = time.time()
        elapsed = now - self._last_trade_time
        if elapsed < self._cooldown_period:
            print(
                f"[⏳ COOLDOWN] Trade bloqueado — faltan "
                f"{self._cooldown_period - elapsed:.0f}s para el próximo."
            )
            log.info(f"[OrderExec] Cooldown active — {elapsed:.0f}s < {self._cooldown_period}s")
            return

        task = {
            "direction": direction,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "leverage": leverage,
            "capital": capital,
            "ts": now,
        }
        self._queue.put(task)
        if not self.isRunning():
            self.start()
        log.info(
            f"[OrderExec] Enqueued {direction} @ {entry_price:.0f} | "
            f"SL={sl_price:.0f} TP={tp_price:.0f}"
        )

    def stop(self) -> None:
        """Signal the thread to exit gracefully."""
        self._running = False
        self._queue.put(None)
        self.wait(3000)

    # ── Thread main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        """Background loop: dequeue and execute trade signals."""
        env_tag = "TESTNET" if USE_TESTNET else "REAL"
        log.info(f"[OrderExec] Thread started | ENV={env_tag}")
        self._build_client()
        if self._client is None:
            self.order_result.emit(False, f"[{env_tag}] Client init failed — check API keys", {})
            return

        while self._running:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task is None:
                break
            try:
                self._execute_single(task)
            except Exception as exc:
                log.exception("[OrderExec] Unhandled error")
                self.order_result.emit(False, str(exc), {"error": str(exc)})

        log.info("[OrderExec] Thread stopped")

    # ── Internals ───────────────────────────────────────────────────────────

    def _build_client(self) -> None:
        """Create Binance Futures client for selected environment."""
        if USE_TESTNET:
            api_key = settings.BINANCE_API_KEY
            secret = settings.BINANCE_SECRET_KEY
            base_url = TESTNET_BASE_URL
        else:
            api_key = settings.BINANCE_REAL_API_KEY
            secret = settings.BINANCE_REAL_SECRET_KEY
            base_url = REAL_BASE_URL

        if not api_key or not secret:
            tag = "TESTNET" if USE_TESTNET else "REAL"
            log.error(f"[OrderExec] Missing API keys for {tag}")
            return

        self._client = Client(api_key, secret, testnet=USE_TESTNET, ping=False)

    def _execute_single(self, task: dict) -> None:
        """Execute one trade signal: set leverage → MARKET entry → SL + TP."""
        env_tag = "TESTNET" if USE_TESTNET else "REAL"
        direction = task["direction"]
        entry_price = task["entry_price"]
        sl_price = task["sl_price"]
        tp_price = task["tp_price"]
        leverage = task["leverage"]
        capital = task["capital"]

        side = "BUY" if direction.upper() == "ALZA" else "SELL"
        opposite_side = "SELL" if side == "BUY" else "BUY"

        # ── 1. Set leverage ──────────────────────────────────────────────
        for attempt in range(MAX_RETRIES):
            try:
                self._client.futures_change_leverage(
                    symbol=SYMBOL, leverage=leverage
                )
                log.info(f"[OrderExec] Leverage set to {leverage}x")
                break
            except Exception as exc:
                log.warning(f"[OrderExec] Leverage attempt {attempt + 1} failed: {exc}")
                if attempt == MAX_RETRIES - 1:
                    self.order_result.emit(
                        False,
                        f"[{env_tag}] Leverage error: {exc}",
                        {"environment": env_tag, "error": str(exc)},
                    )
                    return
                time.sleep(RETRY_DELAY_S)

        # ── 2. Calculate quantity ────────────────────────────────────────
        raw_qty = (capital * leverage) / entry_price
        quantity = float(
            Decimal(str(raw_qty)).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        )
        if quantity <= 0:
            self.order_result.emit(
                False,
                f"[{env_tag}] Quantity too small: {raw_qty:.6f} → {quantity}",
                {"environment": env_tag, "raw_qty": raw_qty, "quantity": quantity},
            )
            return

        # ── 3. MARKET entry ──────────────────────────────────────────────
        entry_order_id = None
        entry_fill_price = entry_price
        for attempt in range(MAX_RETRIES):
            try:
                entry_resp = self._fapi_create_order(
                    symbol=SYMBOL,
                    side=side,
                    type="MARKET",
                    quantity=quantity,
                )
                entry_order_id = str(entry_resp.get("orderId", ""))
                entry_fill_price = float(
                    entry_resp.get("avgPrice", entry_price)
                )
                log.info(
                    f"[OrderExec] MARKET {side} {quantity} BTC @ "
                    f"{entry_fill_price:.0f} → id {entry_order_id}"
                )
                # Safety: actualizar cooldown solo tras entrada exitosa
                self._last_trade_time = time.time()
                break
            except Exception as exc:
                log.warning(f"[OrderExec] Entry attempt {attempt + 1} failed: {exc}")
                if attempt == MAX_RETRIES - 1:
                    self.order_result.emit(
                        False,
                        f"[{env_tag}] Entry error: {exc}",
                        {
                            "environment": env_tag,
                            "direction": direction,
                            "quantity": quantity,
                            "error": str(exc),
                        },
                    )
                    return
                time.sleep(RETRY_DELAY_S)

        # ── 4. SL + TP bracket (Algo Service) ───────────────────────────
        # Binance migró las órdenes condicionales al endpoint
        # POST /fapi/v1/algoOrder.  STOP_MARKET y TAKE_PROFIT_MARKET
        # DEBEN enviarse a través de futures_create_algo_order().
        # Parámetros clave:
        #   algoType="CONDITIONAL"  (se añade automáticamente)
        #   triggerPrice  (≠ stopPrice del endpoint clásico)
        #   closePosition="true"   (string, no booleano)
        #   workingType="MARK_PRICE"
        sl_order_id = ""
        tp_order_id = ""
        bracket_errors = []

        if sl_price > 0:
            for attempt in range(MAX_RETRIES):
                try:
                    sl_resp = self._create_algo_order(
                        symbol=SYMBOL,
                        side=opposite_side,
                        type="STOP_MARKET",
                        triggerPrice=sl_price,
                        closePosition="true",
                        workingType="MARK_PRICE",
                    )
                    sl_order_id = str(sl_resp.get("clientAlgoId", "") or sl_resp.get("algoId", ""))
                    log.info(
                        f"[OrderExec] SL STOP_MARKET (algo) @ {sl_price:.0f} → id {sl_order_id}"
                    )
                    break
                except Exception as exc:
                    log.warning(f"[OrderExec] SL algo attempt {attempt + 1} failed: {exc}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"SL: {exc}")
                    time.sleep(RETRY_DELAY_S)
        else:
            bracket_errors.append("SL price <= 0 — skipped")

        if tp_price > 0:
            for attempt in range(MAX_RETRIES):
                try:
                    tp_resp = self._create_algo_order(
                        symbol=SYMBOL,
                        side=opposite_side,
                        type="TAKE_PROFIT_MARKET",
                        triggerPrice=tp_price,
                        closePosition="true",
                        workingType="MARK_PRICE",
                    )
                    tp_order_id = str(tp_resp.get("clientAlgoId", "") or tp_resp.get("algoId", ""))
                    log.info(
                        f"[OrderExec] TP TAKE_PROFIT_MARKET (algo) @ {tp_price:.0f} → id {tp_order_id}"
                    )
                    break
                except Exception as exc:
                    log.warning(f"[OrderExec] TP algo attempt {attempt + 1} failed: {exc}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"TP: {exc}")
                    time.sleep(RETRY_DELAY_S)
        else:
            bracket_errors.append("TP price <= 0 — skipped")

        # ── 5. Emit result ──────────────────────────────────────────────
        result_data = {
            "environment": env_tag,
            "direction": direction,
            "entry_qty": quantity,
            "entry_price": entry_fill_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "entry_order_id": entry_order_id,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
        }
        if bracket_errors:
            result_data["bracket_errors"] = "; ".join(bracket_errors)

        success = bool(entry_order_id)
        msg = (
            f"[{env_tag}] {'✅' if success else '❌'} "
            f"{direction} {quantity} BTC @ {entry_fill_price:.0f} "
            f"| SL {sl_price:.0f} | TP {tp_price:.0f} "
            f"| IDs: entry={entry_order_id} "
            f"sl={sl_order_id or 'FAIL'} tp={tp_order_id or 'FAIL'}"
        )
        self.order_result.emit(success, msg, result_data)

    def _fapi_create_order(self, **kwargs) -> dict:
        """Call futures_create_order with unified error handling.

        Usado exclusivamente para la orden de entrada MARKET.
        Las órdenes condicionales (SL/TP) van por _create_algo_order.
        """
        if self._client is None:
            raise RuntimeError("Client not initialised")

        params = {}
        for k, v in kwargs.items():
            if v is not None:
                if k == "closePosition":
                    params[k] = bool(v)
                else:
                    params[k] = v

        try:
            return self._client.futures_create_order(**params)
        except BinanceAPIException as exc:
            code = getattr(exc, "code", "?")
            msg = getattr(exc, "message", str(exc))
            log.error(f"[OrderExec] BinanceAPIException [{code}]: {msg}")
            raise
        except BinanceRequestException as exc:
            log.error(f"[OrderExec] BinanceRequestException: {exc}")
            raise

    def _create_algo_order(self, **kwargs) -> dict:
        """Call futures_create_algo_order (POST /fapi/v1/algoOrder).

        Parámetros clave (según docs Binance):
          algoType     → "CONDITIONAL" (se añade automáticamente)
          triggerPrice → precio que activa la orden (≠ stopPrice clásico)
          closePosition→ "true" (string, no booleano)
          workingType  → "MARK_PRICE" o "CONTRACT_PRICE"

        Maneja errores conocidos:
          -4120 → STOP_ORDER_SWITCH_ALGO  (migración no activada)
          -2015 → permisos de API insuficientes
        """
        if self._client is None:
            raise RuntimeError("Client not initialised")

        params = {}
        for k, v in kwargs.items():
            if v is not None:
                if k == "closePosition":
                    params[k] = str(v) if isinstance(v, bool) else v
                else:
                    params[k] = v

        try:
            return self._client.futures_create_algo_order(**params)
        except BinanceAPIException as exc:
            code = getattr(exc, "code", "?")
            msg = getattr(exc, "message", str(exc))
            log.error(f"[OrderExec] BinanceAPIException [{code}]: {msg}")

            if code == -4120:
                log.critical(
                    "[OrderExec] ERROR -4120: STOP_ORDER_SWITCH_ALGO — "
                    "debes activar la migración de órdenes condicionales "
                    "en la configuración de API de Binance Futures."
                )
            elif code == -2015:
                log.critical(
                    "[OrderExec] ERROR -2015: Permisos de API insuficientes. "
                    "Asegúrate de que la API key tenga habilitados "
                    "'Enable Trading' y 'Enable Futures' en Binance."
                )
            raise
        except BinanceRequestException as exc:
            log.error(f"[OrderExec] BinanceRequestException: {exc}")
            raise
