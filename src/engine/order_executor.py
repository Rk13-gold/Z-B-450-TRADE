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
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from threading import Lock
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config.settings import settings

log = logging.getLogger(__name__)

# ── ENDPOINTS ────────────────────────────────────────────────────────────────
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

    def __init__(self, parent: Optional = None, client: Optional[Client] = None):
        super().__init__(parent)
        self._lock = Lock()
        self._queue: queue.Queue[Optional[dict]] = queue.Queue()
        self._running = True
        self._client: Optional[Client] = client

        # ── Safety: cooldown anti-spam ───────────────────────────────
        self._last_trade_time: float = 0.0
        self._cooldown_period: float = 60.0   # segundos entre trades
        self._last_reject_reason: str = ""

        # ── Post-close cooldown (15 min) ─────────────────────────────
        self._close_cooldown_until: float = 0.0
        self._close_cooldown_seconds: float = 900.0  # 15 min

        # ── Single-position gate ─────────────────────────────────────
        self._has_open_position: bool = False
        self._position_info: dict = {}

        # ── Algo bracket tracking ────────────────────────────────────
        self._active_algo_ids: list[str] = []
        self._brackets_verified: bool = False

        # ── Environment — locked to REAL ───────────────────────────

        # ── Symbol precision cache (fetched once from exchangeInfo) ──
        self._symbol_filters: dict = {}
        self._precision_cached: bool = False

        # ── Market context (updated externally by dashboard) ─────────
        self._current_price: float = 0.0
        self._ma_7: float = 0.0
        self._ma_25: float = 0.0
        self._ma_99: float = 0.0

        # ── Trailing stop state ─────────────────────────────────────
        self._trailing_enabled: bool = True
        self._trail_breakeven_pnl: float = 6.0     # $ → move SL to entry
        self._trail_distance_pct: float = 0.2       # % trailing distance
        self._trail_use_ma7: bool = True             # prefer MA7 over fixed %
        self._trail_activated: bool = False          # breakeven reached?
        self._trailing_sl_price: float = 0.0
        self._trailing_algo_id: str = ""

    # ── Precision helpers ───────────────────────────────────────────────────

    def _load_symbol_filters(self) -> dict:
        """Fetch and cache LOT_SIZE + PRICE_FILTER + precision for SYMBOL."""
        if self._precision_cached and self._symbol_filters:
            return self._symbol_filters
        if self._client is None:
            return {}
        try:
            info = self._client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s.get("symbol") == SYMBOL:
                    filters = {}
                    filters["price_precision"] = int(s.get("pricePrecision", 2))
                    filters["qty_precision"] = int(s.get("quantityPrecision", 4))
                    for f in s.get("filters", []):
                        ft = f.get("filterType", "")
                        if ft == "LOT_SIZE":
                            filters["step_size"] = float(f.get("stepSize", 0.0001))
                            filters["min_qty"] = float(f.get("minQty", 0.0001))
                        elif ft == "PRICE_FILTER":
                            filters["tick_size"] = float(f.get("tickSize", 0.01))
                    self._symbol_filters = filters
                    self._precision_cached = True
                    log.info(
                        f"[OrderExec] Precision: tick={filters.get('tick_size')}, "
                        f"step={filters.get('step_size')}, "
                        f"price_dec={filters.get('price_precision')}, "
                        f"qty_dec={filters.get('qty_precision')}")
                    return filters
        except Exception as exc:
            log.warning(f"[OrderExec] Failed to load exchange info: {exc}")
        return {}

    def _round_price(self, price: float) -> str:
        """Round price to symbol's tickSize and format to pricePrecision."""
        filters = self._load_symbol_filters()
        decimals = filters.get("price_precision", 2)
        tick = filters.get("tick_size", 0.01)
        if tick > 0:
            rounded = round(price / tick) * tick
        else:
            rounded = price
        return f"{rounded:.{decimals}f}"

    def _round_quantity(self, qty: float) -> str:
        """Round quantity down to symbol's stepSize and format to qtyPrecision."""
        filters = self._load_symbol_filters()
        decimals = filters.get("qty_precision", 4)
        step = filters.get("step_size", 0.0001)
        if step > 0:
            truncated = int(qty / step) * step
        else:
            truncated = qty
        return f"{truncated:.{decimals}f}"

    # ── Public API ──────────────────────────────────────────────────────────

    def execute_trade_signal(
        self,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        leverage: int = 10,
        capital: float = 100.0,
        confidence: float = -1.0,
    ) -> bool:
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
            Leverage multiplier (ignored if confidence >= 0).
        capital : float
            Available capital in USDT (ignored if confidence >= 0).
        confidence : float
            Signal confidence 0-100. When >= 0, ``calculate_dynamic_leverage``
            overrides *leverage* and *capital* automatically.
        """

        # ── SAFETY LAYER 0: Sincronización con Binance ──────────────
        now = time.time()

        # ── SAFETY LAYER 1: Validación anti-ceros ────────────────────
        reject_reason = None
        if direction not in ('ALZA', 'BAJA'):
            reject_reason = f"Dirección inválida: '{direction}'"
        elif entry_price <= 0:
            reject_reason = f"Precio de entrada inválido: ${entry_price}"
        elif sl_price <= 0:
            reject_reason = f"Stop Loss inválido: ${sl_price}"
        elif tp_price <= 0:
            reject_reason = f"Take Profit inválido: ${tp_price}"
        if reject_reason:
            self._last_reject_reason = reject_reason
            log.warning(f"[OrderExec] REJECTED — {reject_reason}")
            print(f"[⚠️ RECHAZADO] {reject_reason}")
            return False

        # ── SAFETY LAYER 3: Cooldown anti-spam ──────────────────────
        with self._lock:
            elapsed = now - self._last_trade_time
            if self._last_trade_time > 0 and elapsed < self._cooldown_period:
                remaining = self._cooldown_period - elapsed
                self._last_reject_reason = f"Cooldown activo — faltan {remaining:.0f}s"
                log.info(f"[OrderExec] Cooldown active — {elapsed:.0f}s < {self._cooldown_period}s")
                print(f"[⏳ RECHAZADO] {self._last_reject_reason}")
                return False

        # ── SAFETY LAYER 4: Single-position gate ───────────────────
        with self._lock:
            if self._has_open_position:
                self._last_reject_reason = "Conflicto de posición existente — ya hay una posición abierta"
                log.info(f"[OrderExec] {self._last_reject_reason}")
                print(f"[🚫 RECHAZADO] {self._last_reject_reason}")
                return False

        # ── SAFETY LAYER 5: Close-cooldown (15 min post-close) ────
        with self._lock:
            if self._close_cooldown_until > now:
                remaining = self._close_cooldown_until - now
                self._last_reject_reason = (
                    f"Enfriamiento post-cierre — faltan {remaining:.0f}s "
                    f"para poder re-ingresar")
                log.info(f"[OrderExec] Close-cooldown active — {remaining:.0f}s")
                print(f"[⏳ RECHAZADO] {self._last_reject_reason}")
                return False

        # ── SAFETY LAYER 6: MA Trend Filter ────────────────────────
        if not self._check_trend_filter(direction):
            print(f"[🚫 RECHAZADO] {self._last_reject_reason}")
            return False

        task = {
            "direction": direction,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "leverage": leverage,
            "capital": capital,
            "confidence": confidence,
            "ts": now,
        }
        self._queue.put(task)
        if not self.isRunning():
            self.start()
        log.info(
            f"[OrderExec] Enqueued {direction} @ {entry_price:.0f} | "
            f"SL={sl_price:.0f} TP={tp_price:.0f}"
        )
        return True

    def update_market_context(self, price: float = 0, ma7: float = 0,
                               ma25: float = 0, ma99: float = 0) -> None:
        """Push current market context from the dashboard (called at ~1Hz)."""
        if price > 0:
            self._current_price = price
        if ma7 > 0:
            self._ma_7 = ma7
        if ma25 > 0:
            self._ma_25 = ma25
        if ma99 > 0:
            self._ma_99 = ma99

    # ── Dynamic Leverage ─────────────────────────────────────────────────────

    @staticmethod
    def calculate_dynamic_leverage(confidence: float) -> tuple:
        """Compute leverage and risk % from signal confidence.

        Rules
        -----
        confidence < 40  → (0, 0.0)          blocked
        40 ≤ conf ≤ 60  → (10, 0.025)        10x, 2.5 % risk
        confidence > 60  → (25, 0.07)         25x, 7 % risk
        """
        if confidence < 40:
            return (0, 0.0)
        if confidence <= 60:
            return (10, 0.025)
        return (25, 0.07)

    def configure_trailing(self, enabled: bool = True,
                            breakeven_pnl: float = 6.0,
                            distance_pct: float = 0.2,
                            use_ma7: bool = True) -> None:
        """Configure trailing stop parameters."""
        self._trailing_enabled = enabled
        self._trail_breakeven_pnl = breakeven_pnl
        self._trail_distance_pct = distance_pct
        self._trail_use_ma7 = use_ma7

    @property
    def can_open_new_trade(self) -> bool:
        """True if neither position-gate nor close-cooldown blocks entry."""
        if self._has_open_position:
            return False
        if self._close_cooldown_until > time.time():
            return False
        return True

    def _check_trend_filter(self, direction: str) -> bool:
        """MA trend filter: reject LONG if price below MA25/MA99 (macro bearish),
        reject SHORT if price above MA25/MA99 (macro bullish).
        Returns True if signal passes filter (ok to proceed).
        """
        price = self._current_price
        if price <= 0 or (self._ma_25 <= 0 and self._ma_99 <= 0):
            return True  # no data → allow
        direction = direction.upper()
        if direction == "ALZA":
            # Trying to go LONG while price is below MAs = counter-trend
            ma_ref = self._ma_25 if self._ma_25 > 0 else self._ma_99
            if price < ma_ref:
                self._last_reject_reason = (
                    f"Filtro MA: precio ${price:.0f} bajo MA {ma_ref:.0f} — "
                    f"tendencia macro bajista, LONG bloqueado")
                log.info(f"[OrderExec] {self._last_reject_reason}")
                return False
        elif direction == "BAJA":
            # Trying to go SHORT while price is above MAs = counter-trend
            ma_ref = self._ma_25 if self._ma_25 > 0 else self._ma_99
            if price > ma_ref:
                self._last_reject_reason = (
                    f"Filtro MA: precio ${price:.0f} sobre MA {ma_ref:.0f} — "
                    f"tendencia macro alcista, SHORT bloqueado")
                log.info(f"[OrderExec] {self._last_reject_reason}")
                return False
        return True

    def stop(self) -> None:
        """Signal the thread to exit gracefully."""
        self._running = False
        self._queue.put(None)
        self.wait(3000)

    # ── Emergency / utility commands ────────────────────────────────────────────

    def close_all_positions(self) -> dict:
        """Emergency close of all open positions.

        This method bypasses cooldown, position gate, and all safety layers.
        It directly cancels active algo brackets and sends a MARKET order
        in the opposite direction for any open position.
        """
        result = {"success": False, "message": "", "data": {}}
        if self._client is None:
            result["message"] = "Cliente Binance no disponible"
            log.error("[OrderExec] close_all_positions — no client")
            return result
        try:
            positions = self._client.futures_position_information(symbol=SYMBOL)
            closed_any = False
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                qty = abs(amt)
                log.warning(
                    f"[EMERGENCY] Closing {side} {qty} {SYMBOL} "
                    f"@ market (positionAmt={amt})"
                )
                resp = self._fapi_create_order(
                    symbol=SYMBOL,
                    side=side,
                    type="MARKET",
                    quantity=qty,
                    reduceOnly=True,
                )
                order_id = resp.get("orderId", "")
                log.info(f"[EMERGENCY] Close order placed: {order_id}")
                closed_any = True
                result["data"]["order_id"] = str(order_id)

            # Release position gate after closing
            if closed_any:
                with self._lock:
                    self.release_position_gate()
                result["success"] = True
                result["message"] = "Todas las posiciones cerradas"
            else:
                result["message"] = "No hay posiciones abiertas para cerrar"
        except Exception as exc:
            log.error(f"[EMERGENCY] close_all_positions error: {exc}")
            result["message"] = f"Error cerrando posiciones: {exc}"
        return result

    def get_balance(self) -> dict:
        """Get USDT futures account balance."""
        result = {"success": False, "balance": 0.0}
        if self._client is None:
            return result
        try:
            account = self._client.futures_account()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    result["success"] = True
                    result["balance"] = float(asset.get("walletBalance", 0))
                    result["available"] = float(asset.get("availableBalance", 0))
                    result["unrealized_pnl"] = float(asset.get("unrealizedProfit", 0))
                    break
        except Exception as exc:
            log.error(f"[OrderExec] get_balance error: {exc}")
        return result

    @property
    def is_testnet(self) -> bool:
        return False  # BB-450 locked to REAL production

    def switch_environment(self, testnet: bool) -> dict:
        """BLOCKED: BB-450 operates on REAL production only."""
        import logging
        log = logging.getLogger(__name__)
        if testnet:
            log.error("[OrderExec] switch_environment(TESTNET) REJECTED — "
                      "production-locked mode")
            return {"success": False,
                    "message": "TESTNET bloqueado — BB-450 solo opera en REAL",
                    "balance": 0.0, "available": 0.0, "unrealized_pnl": 0.0}
        return {"success": True,
                "message": "REAL ya activo — ningún cambio necesario",
                "balance": 0.0, "available": 0.0, "unrealized_pnil": 0.0}

    # ── Thread main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        """Background loop: dequeue and execute trade signals."""
        ENV_TAG = "REAL"
        log.info(f"[OrderExec] Thread started | ENV={ENV_TAG}")
        self._build_client()
        if self._client is None:
            self.order_result.emit(False, f"[{ENV_TAG}] Client init failed — check API keys", {})
            return

        # Sincronizar estado local con Binance (recovery post-reinicio)
        pos = self.check_position_status()
        if pos:
            log.info(f"[OrderExec] Position recovered on startup: "
                     f"{pos.get('positionAmt', 0)} BTC @ "
                     f"${pos.get('entryPrice', 0):,.0f}")

        self._last_trail_check: float = 0.0

        while self._running:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                now = time.time()
                if now - self._last_trail_check >= 3.0:
                    self._last_trail_check = now
                    self._check_trailing_stop()
                continue
            if task is None:
                break
            try:
                self._execute_single(task)
            except Exception as exc:
                log.exception("[OrderExec] Unhandled error")
                self.order_result.emit(False, str(exc), {"error": str(exc)})

        log.info("[OrderExec] Thread stopped")

    # ── Position tracking ───────────────────────────────────────────────────

    def has_open_position(self) -> bool:
        return self._has_open_position

    def get_position_info(self) -> dict:
        return dict(self._position_info)

    def check_position_status(self) -> Optional[dict]:
        """Query Binance for open positions on SYMBOL.
        Returns the position dict if open, or None if closed.
        Side effect: updates _has_open_position flag.
        """
        if self._client is None:
            return None
        try:
            positions = self._client.futures_position_information(symbol=SYMBOL)
            with self._lock:
                for pos in positions:
                    pos_amt = float(pos.get("positionAmt", 0))
                    if pos_amt != 0:
                        self._has_open_position = True
                        self._position_info.update({
                            "positionAmt": pos_amt,
                            "entryPrice": float(pos.get("entryPrice", 0)),
                            "unRealizedProfit": float(pos.get("unRealizedProfit", 0)),
                            "markPrice": float(pos.get("markPrice", 0)),
                            "liquidationPrice": float(pos.get("liquidationPrice", 0)),
                            "leverage": int(float(pos.get("leverage", 0))),
                            "timestamp": time.time(),
                        })
                        return dict(self._position_info)
                if self._has_open_position:
                    log.info("[OrderExec] Position closed — releasing position gate")
                    self._close_cooldown_until = time.time() + self._close_cooldown_seconds
                    log.info(
                        f"[OrderExec] Cooldown post-cierre: "
                        f"{self._close_cooldown_seconds:.0f}s "
                        f"(hasta {time.strftime('%H:%M:%S', time.localtime(self._close_cooldown_until))})")
                self._has_open_position = False
                self._position_info = {}
                self._trail_activated = False
                self._trailing_sl_price = 0.0
                self._trailing_algo_id = ""
                return None
        except Exception as exc:
            log.warning(f"[OrderExec] Position check error: {exc}")
            return None

    def release_position_gate(self) -> None:
        """Force-release the position gate (manual override)."""
        with self._lock:
            self._has_open_position = False
            self._position_info = {}
            self._active_algo_ids = []
            self._brackets_verified = False
            self._trail_activated = False
            self._trailing_sl_price = 0.0
            self._trailing_algo_id = ""
        log.info("[OrderExec] Position gate manually released")

    def get_position_with_pnl(self) -> Optional[dict]:
        """Fetch current position from Binance with live PnL data.
        Returns None if no position is open.
        """
        pos = self.check_position_status()
        if pos is None:
            return None
        amt = float(pos.get("positionAmt", 0))
        if amt == 0:
            return None
        entry = float(pos.get("entryPrice", 0))
        mark = float(pos.get("markPrice", 0))
        pnl = float(pos.get("unRealizedProfit", 0))
        liq = float(pos.get("liquidationPrice", 0))
        lev = int(float(pos.get("leverage", 0)))
        pos_value = abs(amt) * entry
        pnl_pct = (pnl / pos_value * 100) if pos_value > 0 else 0.0
        return {
            "direction": "LONG" if amt > 0 else "SHORT",
            "entry_qty": abs(amt),
            "entry_price": entry,
            "mark_price": mark,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "liquidation_price": liq,
            "leverage": lev,
        }

    # ── Trailing Stop ────────────────────────────────────────────────────────

    def _check_trailing_stop(self) -> None:
        """Evaluate and update trailing stop every ~3s when a position is open.

        Flow:
          1. Skip if trailing not enabled, no open position, or no algo SL.
          2. Fetch live position PnL and mark price.
          3. If PnL >= breakeven threshold AND trail not yet activated:
               → cancel old SL algo order
               → place new SL at entry price (breakeven)
               → set _trail_activated = True
          4. If trail activated AND mark price moved favorably:
               → compute new trail price (MA7 or fixed % behind)
               → if new price is better (tighter to market) than current SL:
                   → cancel old SL, place new SL at trailed price
        """
        if not self._trailing_enabled or not self._has_open_position:
            return
        if self._client is None:
            return

        pos = self.get_position_with_pnl()
        if pos is None:
            return

        entry = pos["entry_price"]
        mark = pos["mark_price"]
        pnl = pos["pnl"]
        direction = pos["direction"]
        algo_id = self._trailing_algo_id

        if not algo_id:
            return

        is_long = direction == "LONG"

        # ── Step 1: Breakeven trigger ──────────────────────────────────
        if not self._trail_activated:
            if pnl >= self._trail_breakeven_pnl:
                be_price = entry
                be_str = self._round_price(be_price)
                try:
                    # Cancel old SL algo
                    if algo_id:
                        self._client.futures_cancel_algo_order(
                            symbol=SYMBOL, algoId=algo_id)
                        log.info(f"[Trail] SL cancelado (breakeven) — id={algo_id}")
                    # Place new SL at entry price
                    opposite = "SELL" if is_long else "BUY"
                    resp = self._create_algo_order(
                        symbol=SYMBOL,
                        side=opposite,
                        type="STOP_MARKET",
                        triggerPrice=be_str,
                        closePosition="true",
                        workingType="MARK_PRICE",
                    )
                    new_id = str(resp.get("clientAlgoId", "") or resp.get("algoId", ""))
                    with self._lock:
                        self._trail_activated = True
                        self._trailing_sl_price = be_price
                        self._trailing_algo_id = new_id
                    log.info(
                        f"[Trail] Breakeven activado → SL en ${be_price:,.0f} "
                        f"| PnL=${pnl:+.2f} | mark=${mark:,.0f}")
                except Exception as exc:
                    log.warning(f"[Trail] Breakeven error: {exc}")
            return

        # ── Step 2: Trail the stop ─────────────────────────────────────
        # Compute target trail price
        if self._trail_use_ma7 and self._ma_7 > 0:
            trail_target = self._ma_7
        else:
            # Fixed % trailing distance
            if is_long:
                trail_target = mark * (1 - self._trail_distance_pct / 100.0)
            else:
                trail_target = mark * (1 + self._trail_distance_pct / 100.0)

        current_sl = self._trailing_sl_price
        if current_sl <= 0:
            return

        # Determine if new price improves the stop
        improvement = False
        if is_long:
            # LONG: SL should rise (trail upward)
            if trail_target > current_sl and trail_target < mark:
                improvement = True
        else:
            # SHORT: SL should fall (trail downward)
            if trail_target < current_sl and trail_target > mark:
                improvement = True

        if not improvement:
            return

        # Minimum distance check: SL must be at least 0.1% away from mark
        min_dist = mark * 0.001
        if is_long and (mark - trail_target) < min_dist:
            return
        if not is_long and (trail_target - mark) < min_dist:
            return

        # Apply the new trailed SL
        try:
            new_sl_str = self._round_price(trail_target)
            # Cancel old SL
            self._client.futures_cancel_algo_order(
                symbol=SYMBOL, algoId=algo_id)
            # Place new SL
            opposite = "SELL" if is_long else "BUY"
            resp = self._create_algo_order(
                symbol=SYMBOL,
                side=opposite,
                type="STOP_MARKET",
                triggerPrice=new_sl_str,
                closePosition="true",
                workingType="MARK_PRICE",
            )
            new_id = str(resp.get("clientAlgoId", "") or resp.get("algoId", ""))
            with self._lock:
                self._trailing_sl_price = trail_target
                self._trailing_algo_id = new_id
            log.info(
                f"[Trail] SL actualizado → ${trail_target:,.0f} "
                f"(mark=${mark:,.0f}) | mejora ${current_sl:,.0f}→${trail_target:,.0f}")
        except Exception as exc:
            log.warning(f"[Trail] Update error: {exc}")

    # ── Internals ───────────────────────────────────────────────────────────

    def _build_client(self) -> None:
        """Create Binance Futures client — REAL production only."""
        api_key = settings.BINANCE_REAL_API_KEY
        secret = settings.BINANCE_REAL_SECRET_KEY
        if not api_key or not secret:
            log.error("[OrderExec] REAL API keys missing — "
                      "check BINANCE_REAL_API_KEY / BINANCE_REAL_SECRET_KEY")
            return
        try:
            self._client = Client(api_key, secret, testnet=False, ping=False)
        except Exception as exc:
            log.error(f"[OrderExec] Failed to build REAL client: {exc}")
            self._client = None

    def _execute_single(self, task: dict) -> None:
        """Execute one trade signal: set leverage → MARKET entry → SL + TP."""
        env_tag = "REAL"
        direction = task["direction"]
        entry_price = task["entry_price"]
        sl_price = task["sl_price"]
        tp_price = task["tp_price"]
        leverage = task["leverage"]
        capital = task["capital"]
        confidence = task.get("confidence", -1.0)

        side = "BUY" if direction.upper() == "ALZA" else "SELL"
        opposite_side = "SELL" if side == "BUY" else "BUY"

        # ── 0. Dynamic leverage override ─────────────────────────────────
        used_dynamic = False
        dynamic_confidence = confidence
        if confidence >= 0:
            bal = self.get_balance()
            live_balance = bal.get("balance", 0.0) if bal.get("success") else max(capital, 100)
            dyn_lev, risk_pct = self.calculate_dynamic_leverage(confidence)
            if dyn_lev == 0:
                msg = (f"[{env_tag}] Señal rechazada — confianza {confidence:.0f}% "
                       f"por debajo del umbral mínimo (40%)")
                log.info(f"[OrderExec] {msg}")
                self.order_result.emit(
                    False, msg,
                    {"environment": env_tag, "direction": direction,
                     "error": f"Confianza {confidence:.0f}% < 40% — señal descartada",
                     "confidence": confidence, "dynamic_leverage": 0},
                )
                return
            leverage = dyn_lev
            capital = live_balance * risk_pct
            used_dynamic = True
            log.info(f"[OrderExec] Dynamic leverage → {leverage}x "
                     f"(confianza={confidence:.0f}%, riesgo={risk_pct*100:.1f}%, "
                     f"capital=${capital:.2f})")

        # ── 1. Set leverage ──────────────────────────────────────────────
        for attempt in range(MAX_RETRIES):
            try:
                self._client.futures_change_leverage(
                    symbol=SYMBOL, leverage=leverage
                )
                log.info(f"[OrderExec] Leverage set to {leverage}x")
                break
            except BinanceAPIException as exc:
                code = getattr(exc, "code", "?")
                msg = getattr(exc, "message", str(exc))
                log.warning(f"[OrderExec] Leverage attempt {attempt+1} — "
                            f"BinanceAPIException [{code}]: {msg}")
                if attempt == MAX_RETRIES - 1:
                    err_label = (f"🚨 ERROR DE APALANCAMIENTO: "
                                 f"No se pudo ajustar el multiplicador [{code}]: {msg}")
                    self.order_result.emit(
                        False, err_label,
                        {"environment": env_tag, "direction": direction,
                         "error": f"BinanceAPIException [{code}]: {msg}",
                         "confidence": confidence, "dynamic_leverage": leverage},
                    )
                    return
                time.sleep(RETRY_DELAY_S)
            except Exception as exc:
                log.warning(f"[OrderExec] Leverage attempt {attempt + 1} failed: {exc}")
                if attempt == MAX_RETRIES - 1:
                    self.order_result.emit(
                        False,
                        f"[{env_tag}] Leverage error: {exc}",
                        {"environment": env_tag, "direction": direction,
                         "error": str(exc),
                         "confidence": confidence, "dynamic_leverage": leverage},
                    )
                    return
                time.sleep(RETRY_DELAY_S)

        # ── 2. Calculate quantity ────────────────────────────────────────
        raw_qty = (capital * leverage) / entry_price
        quantity_str = self._round_quantity(raw_qty)
        quantity_val = float(quantity_str)
        if quantity_val <= 0:
            self.order_result.emit(
                False,
                f"[{env_tag}] Quantity too small: {raw_qty:.6f} → {quantity_str}",
                {"environment": env_tag, "raw_qty": raw_qty, "quantity": quantity_str},
            )
            return

        # ── 3. MARKET entry ──────────────────────────────────────────────
        entry_order_id = None
        entry_fill_price_val = entry_price
        for attempt in range(MAX_RETRIES):
            try:
                entry_resp = self._fapi_create_order(
                    symbol=SYMBOL,
                    side=side,
                    type="MARKET",
                    quantity=quantity_str,
                )
                entry_order_id = str(entry_resp.get("orderId", ""))
                avg_price_raw = entry_resp.get("avgPrice", str(entry_price))
                try:
                    entry_fill_price_val = float(avg_price_raw)
                except (ValueError, TypeError):
                    entry_fill_price_val = entry_price
                if entry_fill_price_val <= 0:
                    entry_fill_price_val = entry_price
                entry_fill_price_str = self._round_price(entry_fill_price_val)
                log.info(
                    f"[OrderExec] MARKET {side} {quantity_val:.4f} BTC @ "
                    f"{entry_fill_price_val:.2f} -> id {entry_order_id}"
                )
                with self._lock:
                    self._last_trade_time = time.time()
                break
            except BinanceAPIException as exc:
                code = getattr(exc, "code", "?")
                msg = getattr(exc, "message", str(exc))
                err_str = f"BinanceAPIException [{code}]: {msg}"
                log.warning(f"[OrderExec] Entry attempt {attempt+1} — {err_str}")
                if attempt == MAX_RETRIES - 1:
                    self.order_result.emit(
                        False,
                        f"[{env_tag}] Entry error [{code}]: {msg}",
                        {
                            "environment": env_tag,
                            "direction": direction,
                            "quantity": quantity_str,
                            "error": err_str,
                        },
                    )
                    return
                time.sleep(RETRY_DELAY_S)
            except Exception as exc:
                log.warning(f"[OrderExec] Entry attempt {attempt + 1} failed: {exc}")
                if attempt == MAX_RETRIES - 1:
                    self.order_result.emit(
                        False,
                        f"[{env_tag}] Entry error: {exc}",
                        {
                            "environment": env_tag,
                            "direction": direction,
                            "quantity": quantity_str,
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
        rounded_sl_str = "0"
        rounded_tp_str = "0"

        if sl_price > 0:
            rounded_sl_str = self._round_price(sl_price)
            for attempt in range(MAX_RETRIES):
                try:
                    sl_resp = self._create_algo_order(
                        symbol=SYMBOL,
                        side=opposite_side,
                        type="STOP_MARKET",
                        triggerPrice=rounded_sl_str,
                        closePosition="true",
                        workingType="MARK_PRICE",
                    )
                    sl_order_id = str(sl_resp.get("clientAlgoId", "") or sl_resp.get("algoId", ""))
                    log.info(
                        f"[OrderExec] SL STOP_MARKET (algo) @ {rounded_sl_str} → id {sl_order_id}"
                    )
                    break
                except BinanceAPIException as exc:
                    code = getattr(exc, "code", "?")
                    msg = getattr(exc, "message", str(exc))
                    log.warning(f"[OrderExec] SL algo attempt {attempt+1} — "
                                f"BinanceAPIException [{code}]: {msg}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"SL [{code}]: {msg}")
                    time.sleep(RETRY_DELAY_S)
                except Exception as exc:
                    log.warning(f"[OrderExec] SL algo attempt {attempt + 1} failed: {exc}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"SL: {exc}")
                    time.sleep(RETRY_DELAY_S)
        else:
            bracket_errors.append("SL price <= 0 — skipped")

        if tp_price > 0:
            rounded_tp_str = self._round_price(tp_price)
            for attempt in range(MAX_RETRIES):
                try:
                    tp_resp = self._create_algo_order(
                        symbol=SYMBOL,
                        side=opposite_side,
                        type="TAKE_PROFIT_MARKET",
                        triggerPrice=rounded_tp_str,
                        closePosition="true",
                        workingType="MARK_PRICE",
                    )
                    tp_order_id = str(tp_resp.get("clientAlgoId", "") or tp_resp.get("algoId", ""))
                    log.info(
                        f"[OrderExec] TP TAKE_PROFIT_MARKET (algo) @ {rounded_tp_str} → id {tp_order_id}"
                    )
                    break
                except BinanceAPIException as exc:
                    code = getattr(exc, "code", "?")
                    msg = getattr(exc, "message", str(exc))
                    log.warning(f"[OrderExec] TP algo attempt {attempt+1} — "
                                f"BinanceAPIException [{code}]: {msg}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"TP [{code}]: {msg}")
                    time.sleep(RETRY_DELAY_S)
                except Exception as exc:
                    log.warning(f"[OrderExec] TP algo attempt {attempt + 1} failed: {exc}")
                    if attempt == MAX_RETRIES - 1:
                        bracket_errors.append(f"TP: {exc}")
                    time.sleep(RETRY_DELAY_S)
        else:
            bracket_errors.append("TP price <= 0 — skipped")

        # ── 5. Trailing stop activation (replace static SL with dynamic) ──
        use_trailing = self._trailing_enabled and sl_order_id
        if use_trailing:
            with self._lock:
                self._trail_activated = False
                self._trailing_sl_price = float(rounded_sl_str) if sl_order_id else 0.0
                self._trailing_algo_id = sl_order_id
            # Do NOT set a TP — trailing will manage the exit dynamically.
            # Cancel TP if it was placed
            if tp_order_id:
                try:
                    self._client.futures_cancel_algo_order(
                        symbol=SYMBOL, algoId=tp_order_id)
                    log.info(f"[OrderExec] TP cancelado para trailing — id={tp_order_id}")
                except Exception:
                    pass
                tp_order_id = ""
                rounded_tp_str = "0"
            msg_trail = " | TRAILING ACTIVADO"
        else:
            msg_trail = ""

        # ── 6. Emit result ──────────────────────────────────────────────
        result_data = {
            "environment": env_tag,
            "direction": direction,
            "entry_qty": quantity_val,
            "entry_price": entry_fill_price_val,
            "sl_price": float(rounded_sl_str),
            "tp_price": float(rounded_tp_str),
            "entry_order_id": entry_order_id,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "leverage": leverage,
            "capital": capital,
            "trailing_active": use_trailing,
            "confidence": confidence,
            "dynamic_leverage": leverage if used_dynamic else 0,
        }
        if bracket_errors:
            result_data["bracket_errors"] = "; ".join(bracket_errors)

        success = bool(entry_order_id)
        msg = (
            f"[{env_tag}] {'✅' if success else '❌'} "
            f"{direction} {quantity_val:.4f} BTC @ {entry_fill_price_val:.2f} "
            f"| SL {result_data['sl_price']:.2f} | TP {result_data['tp_price']:.2f} "
            f"| IDs: entry={entry_order_id} "
            f"sl={sl_order_id or 'FAIL'} tp={tp_order_id or 'FAIL'}"
            f"{msg_trail}"
        )
        self.order_result.emit(success, msg, result_data)

    # ── Direct UI operations (bypass queue, immediate API calls) ───────────

    def change_margin_type(self, margin_type: str) -> dict:
        """Set ISOLATED or CROSSED margin mode for SYMBOL.
        Returns dict with success bool. Errors are silent (already-set is not fatal).
        """
        result = {"success": False, "margin_type": margin_type}
        if self._client is None:
            return result
        try:
            self._client.futures_change_margin_type(
                symbol=SYMBOL, marginType=margin_type.upper())
            result["success"] = True
            log.info(f"[OrderExec] Margin type → {margin_type.upper()}")
        except BinanceAPIException as exc:
            code = getattr(exc, "code", 0)
            if code == -4046:  # Already in this margin mode
                result["success"] = True
            else:
                log.warning(f"[OrderExec] Margin type error [{code}]: {exc}")
        except Exception as exc:
            log.warning(f"[OrderExec] Margin type error: {exc}")
        return result

    def change_leverage_direct(self, leverage: int) -> dict:
        """Set leverage for SYMBOL directly (not via queue)."""
        result = {"success": False, "leverage": leverage}
        if self._client is None:
            return result
        try:
            self._client.futures_change_leverage(
                symbol=SYMBOL, leverage=leverage)
            result["success"] = True
            log.info(f"[OrderExec] Leverage → {leverage}x")
        except Exception as exc:
            log.warning(f"[OrderExec] Leverage error: {exc}")
            result["error"] = str(exc)
        return result

    def market_order_direct(self, side: str, quantity: float, price: float = 0,
                            reduce_only: bool = False) -> dict:
        """Place a MARKET order directly (bypass queue). Returns result dict.

        Parameters
        ----------
        side : str
            'BUY' (LONG) or 'SELL' (SHORT).
        quantity : float
            Amount in BTC (will be rounded to step size).
        price : float
            Reference price for display only.
        reduce_only : bool
            If True, only reduces position (no new entry).

        Returns dict with success, order_id, fill_price, message.
        """
        result = {"success": False, "order_id": "", "fill_price": 0, "message": ""}
        if self._client is None:
            result["message"] = "Client not initialised"
            return result
        qty_str = self._round_quantity(quantity)
        qty_val = float(qty_str)
        if qty_val <= 0:
            result["message"] = f"Quantity too small: {quantity}"
            return result
        try:
            params = {
                "symbol": SYMBOL,
                "side": side.upper(),
                "type": "MARKET",
                "quantity": qty_str,
            }
            if reduce_only:
                params["reduceOnly"] = "true"
            resp = self._client.futures_create_order(**params)
            order_id = str(resp.get("orderId", ""))
            avg_px = float(resp.get("avgPrice", price or 0))
            if avg_px <= 0:
                avg_px = price
            result["success"] = bool(order_id)
            result["order_id"] = order_id
            result["fill_price"] = avg_px
            result["quantity"] = qty_val
            result["message"] = f"{side} {qty_str} BTC @ ${avg_px:.2f}"
            log.info(f"[OrderExec] Direct MARKET {side} {qty_str} → id={order_id}")
        except Exception as exc:
            log.warning(f"[OrderExec] Direct MARKET error: {exc}")
            result["message"] = str(exc)
        return result

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
