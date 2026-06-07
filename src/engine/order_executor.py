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
import math
import queue
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from threading import Lock
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config.settings import settings

log = logging.getLogger(__name__)

# ── ENDPOINTS ────────────────────────────────────────────────────────────────
REAL_BASE_URL = "https://fapi.binance.com"
REAL_WS_URL = "wss://fstream.binance.com"

# ── ORDER CONFIG ────────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_DELAY_S = 0.5

# ── Mejora 1: Split entry (3 micro-tickets) ─────────────────────────────────
SPLIT_ENTRY_TICKETS = 3
SPLIT_ENTRY_PULLBACK_WAIT_SEC = 0.8
SPLIT_ENTRY_PULLBACK_PCT = 0.0003


class OrderExecutor(QThread):
    """Background thread for executing Binance Futures orders.

    Signals
    -------
    order_result(success: bool, message: str, data: dict)
        Emitted after each execution attempt — drives both UI updates
        and Telegram notifications.
    """

    order_result = pyqtSignal(bool, str, dict)
    position_closed = pyqtSignal(dict)

    def __init__(self, parent=None, client: Optional[Client] = None):
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

        # ── Adaptive cooldown post-loss (Mejora 7 — v4-Speed) ────────
        self._consecutive_sl_count: int = 0
        self._adaptive_cooldown_base: float = 300.0  # 5 min base

        # ── Single-position gate ─────────────────────────────────────
        self._has_open_position: bool = False
        self._position_info: dict = {}

        # ── Position-close tracking (PASO 2) ─────────────────────────
        self._open_position_data: Optional[dict] = None
        self._close_poll_timer: Optional[QTimer] = None
        self._close_poll_flag: bool = False

        # ── Environment — locked to REAL ───────────────────────────

        # ── Symbol precision cache (fetched once from exchangeInfo) ──
        self._symbol_filters: dict = {}
        self._precision_cached: bool = False

        # ── Market context (updated externally by dashboard) ─────────
        self._current_price: float = 0.0
        self._ma_7: float = 0.0
        self._ma_25: float = 0.0
        self._ma_99: float = 0.0
        self._atr: float = 0.0

    # ── Precision helpers ───────────────────────────────────────────────────

    def _load_symbol_filters(self) -> dict:
        """Fetch and cache LOT_SIZE + PRICE_FILTER + precision for current symbol."""
        if self._precision_cached and self._symbol_filters:
            return self._symbol_filters
        if self._client is None:
            return {}
        try:
            info = self._client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s.get("symbol") == settings.get_symbol():
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
        step = filters.get("step_size", 0.001)
        if qty <= 0:
            return f"{0.0:.{decimals}f}"
        if step > 0:
            truncated = int(qty / step + 1e-9) * step
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
        atr: float = 0.0,
        wall_bid: float = 0.0,
        wall_ask: float = 0.0,
        delta: float = 0.0,
        cvd: float = 0.0,
        entry_filter: str = "",
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
        delta : float
            Order flow delta at entry time.
        cvd : float
            Cumulative Volume Delta at entry time.
        entry_filter : str
            Name of the filter that confirmed the entry (e.g. 'institutional', 'absorption').
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
            "atr": atr,
            "wall_bid": wall_bid,
            "wall_ask": wall_ask,
            "delta": delta,
            "cvd": cvd,
            "entry_filter": entry_filter,
        }
        self._queue.put(task)
        if not self.isRunning():
            self.start()
        log.info(
            f"[OrderExec] Enqueued {direction} @ {entry_price:.0f} | "
            f"SL={sl_price:.0f} TP={tp_price:.0f} "
            f"| atr={atr:.2f} wall_bid={wall_bid:.2f} wall_ask={wall_ask:.2f}"
        )
        return True

    def update_market_context(self, price: float = 0, ma7: float = 0,
                                ma25: float = 0, ma99: float = 0,
                                atr: float = 0) -> None:
        """Push current market context from the dashboard (called at ~1Hz)."""
        if price > 0:
            self._current_price = price
        if ma7 > 0:
            self._ma_7 = ma7
        if ma25 > 0:
            self._ma_25 = ma25
        if ma99 > 0:
            self._ma_99 = ma99
        if atr > 0:
            self._atr = atr

    @staticmethod
    def _get_last_trade_outcome(db_path: str = "bb450_trades.db") -> Optional[str]:
        """Read the last closed trade outcome from SQLite DB."""
        try:
            conn = sqlite3.connect(db_path, timeout=3)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT outcome FROM trades
                WHERE outcome IN ('TP','SL')
                ORDER BY closed_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception:
            return None

    # ── Dynamic Leverage (reactivo por historial) ────────────────────────────

    @staticmethod
    def get_reactive_leverage(db_path: str = "bb450_trades.db") -> int:
        """Calcula el apalancamiento reactivo leyendo los últimos 3 trades cerrados.

        Reglas
        ------
        - Base / reset:          20x  (si último trade fue pérdida o sin historial)
        - 1 TP consecutivo:      35x
        - 2+ TPs consecutivos:   50x  (aprovechar rachas ganadoras)
        """
        LEV_BASE  = 20
        LEV_MID   = 35
        LEV_MAX   = 50
        try:
            conn = sqlite3.connect(db_path, timeout=3)
            cur  = conn.cursor()
            # Intentar leer los últimos 3 trades cerrados ordenados por fecha desc
            cur.execute(
                """
                SELECT outcome FROM trades
                WHERE outcome IN ('TP','SL')
                ORDER BY closed_at DESC
                LIMIT 3
                """
            )
            rows = cur.fetchall()   # [(outcome,), ...] más reciente primero
            conn.close()
        except Exception as exc:
            log.warning(f"[ReactiveLE] No se pudo leer la DB ({exc}) — usando 20x")
            return LEV_BASE

        if not rows:
            return LEV_BASE

        # El trade más reciente
        last = rows[0][0]
        if last == "SL":
            log.info("[ReactiveLE] Último trade: PÉRDIDA → apalancamiento reset 20x")
            return LEV_BASE

        # Verificar racha ganadora
        wins = sum(1 for r in rows if r[0] == "TP")
        if wins >= 2:
            log.info(f"[ReactiveLE] Racha ganadora ({wins} TPs) → apalancamiento MAX 50x")
            return LEV_MAX
        else:
            log.info("[ReactiveLE] 1 TP consecutivo → apalancamiento 35x")
            return LEV_MID

    @staticmethod
    def calculate_dynamic_leverage(confidence: float) -> tuple:
        """Compute leverage and risk % from signal confidence (legacy path).

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
            positions = self._client.futures_position_information(symbol=settings.get_symbol())
            closed_any = False
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                qty = abs(amt)
                log.warning(
                    f"[EMERGENCY] Closing {side} {qty} {settings.get_symbol()} "
                    f"@ market (positionAmt={amt})"
                )
                resp = self._fapi_create_order(
                    symbol=settings.get_symbol(),
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
            now = time.time()
            if now - getattr(self, '_last_balance_err_ts', 0) > 60.0:
                log.error(f"[OrderExec] get_balance error: {exc}")
                self._last_balance_err_ts = now
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
        """Background loop: dequeue and execute trade signals + close polling."""
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

        self._close_poll_flag = bool(pos and abs(float(pos.get('positionAmt', 0))) > 0)
        if self._close_poll_flag:
            amt = float(pos.get('positionAmt', 0))
            self._open_position_data = {
                "entry_price": float(pos.get('entryPrice', 0)),
                "qty_btc": abs(amt),
                "direction": "BUY" if amt > 0 else "SELL",
                "capital": 0,
                "leverage": settings.LEVERAGE,
                "open_timestamp": datetime.now(timezone.utc).isoformat(),
                "sl_price": 0,
                "tp_price": 0,
                "delta_entrada": 0,
                "cvd_entrada": 0,
                "filtro_entrada": "recovered",
                "max_pnl": 0.0,
            }

        last_close_check = time.time()

        while self._running:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                # ── Periodic position close check ──────────────────────
                if self._close_poll_flag and time.time() - last_close_check >= 5.0:
                    last_close_check = time.time()
                    self._poll_position_close()
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
        """Query Binance for open positions on the current symbol.
        Returns the position dict if open, or None if closed.
        Side effect: updates _has_open_position flag.
        """
        if self._client is None:
            return None
        try:
            positions = self._client.futures_position_information(symbol=settings.get_symbol())
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
                    # ── MEJORA 7: Adaptive cooldown post-loss ─────────────
                    last_outcome = self._get_last_trade_outcome()
                    if last_outcome == "SL":
                        self._consecutive_sl_count += 1
                    else:
                        self._consecutive_sl_count = 0  # reset racha ganadora

                    base_cooldown = self._adaptive_cooldown_base  # 300s
                    if self._consecutive_sl_count >= 2:
                        # 2 consecutivos → 900s (15 min)
                        cooldown = 900.0
                    elif self._consecutive_sl_count == 1:
                        # 1 pérdida → 600s (10 min) si volatilidad alta
                        cooldown = base_cooldown * 2
                    else:
                        cooldown = self._close_cooldown_seconds  # default 900s

                    # ATR factor: si el mercado está muy volátil, extiende
                    if hasattr(self, '_current_price') and self._current_price > 0:
                        atr_factor = getattr(self, '_atr', 0) / max(self._current_price * 0.005, 1)
                        if atr_factor > 1.5:
                            cooldown *= min(atr_factor, 2.0)  # max 2x

                    cooldown = max(cooldown, 300.0)  # mínimo 5 min
                    self._close_cooldown_until = time.time() + cooldown
                    log.info(
                        f"[OrderExec] Cooldown post-cierre: "
                        f"{cooldown:.0f}s "
                        f"(hasta {time.strftime('%H:%M:%S', time.localtime(self._close_cooldown_until))})"
                        f" | consecutive_SL={self._consecutive_sl_count}"
                    )
                self._has_open_position = False
                self._position_info = {}
                return None
        except Exception as exc:
            now = time.time()
            if now - getattr(self, '_last_pos_err_ts', 0) > 60.0:
                log.warning(f"[OrderExec] Position check error: {exc}")
                self._last_pos_err_ts = now
            return None

    def release_position_gate(self) -> None:
        """Force-release the position gate (manual override)."""
        with self._lock:
            self._has_open_position = False
            self._position_info = {}
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

        # ── 0. HARD GATE: live position check against Binance ────────────────
        # Must be the FIRST thing after parsing the task, before any
        # modification (margin type, leverage, bracket) to prevent ping-pong
        # regardless of call origin (UI or Telegram).
        live_pos = self.check_position_status()
        if live_pos is not None:
            pos_amt = float(live_pos.get("positionAmt", 0))
            pos_side = "LONG" if pos_amt > 0 else "SHORT"
            msg = (
                f"🛡️ [BB-450 SECURITY RISK] Orden rechazada. "
                f"El Executor detectó una posición activa en Binance: "
                f"{pos_side} {abs(pos_amt):.4f} BTC. "
                f"Evitando efecto Ping-Pong."
            )
            log.warning(f"[OrderExec] {msg}")
            self.order_result.emit(
                False, msg,
                {"environment": env_tag,
                 "direction": direction,
                 "error": msg,
                 "confidence": confidence,
                 "dynamic_leverage": 0},
            )
            return

        # ── 0. Modo CROSSED ──────────────────────────────────────────────────
        try:
            self._client.futures_change_margin_type(
                symbol=settings.get_symbol(), marginType="CROSSED")
            log.info("[OrderExec] Margin type → CROSSED")
        except BinanceAPIException as _me:
            if getattr(_me, "code", 0) != -4046:
                log.warning(f"[OrderExec] Margin type warning: {_me}")
            # -4046 = already in CROSSED, ignorar silenciosamente

        # ── 0b. Apalancamiento fijo desde settings ─────────────────────────
        leverage = settings.LEVERAGE
        log.info(f"[OrderExec] Leverage fijo: {leverage}x")

        # ── 0c. Capital / size según USE_ALL_IN ─────────────────────────────
        use_all_in = getattr(settings, "USE_ALL_IN", False)
        if use_all_in:
            bal = self.get_balance()
            available_bal = bal.get("available", 0.0) if bal.get("success") else 0.0
            if available_bal <= 0:
                msg = "[REAL] USE_ALL_IN activo pero balance disponible es 0"
                log.error(f"[OrderExec] {msg}")
                self.order_result.emit(False, msg, {"error": msg})
                return
            # 90 % del balance disponible (evita usar unrealized PnL)
            capital = available_bal * 0.90
            log.info(f"[OrderExec] USE_ALL_IN: capital={capital:.2f} USDT "
                     f"(90% de {available_bal:.2f} disponibles)")
        else:
            # Usar GLOBAL_TRADE_AMOUNT si está definido y es > 0
            gta = getattr(settings, "GLOBAL_TRADE_AMOUNT", 0.0)
            if gta > 0:
                capital = gta

        # ── 1. Dynamic leverage override (confidence path — legacy) ──────────
        used_dynamic = False
        dynamic_confidence = confidence
        if confidence >= 0:
            bal_info = self.get_balance()
            live_balance = (bal_info.get("balance", 0.0)
                            if bal_info.get("success") else max(capital, 100))
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
            # El path de confianza sobreescribe capital, pero el apalancamiento
            # reactivo siempre tiene prioridad sobre el confidence-based.
            capital = live_balance * risk_pct
            used_dynamic = True
            log.info(f"[OrderExec] Dynamic (confidence) → {dyn_lev}x "
                     f"(conf={confidence:.0f}%, riesgo={risk_pct*100:.1f}%, "
                     f"capital=${capital:.2f})")

        log.info(f"[OrderExec] Leverage final: {leverage}x")


        min_lev = math.ceil(0.001 * entry_price / max(capital, 0.01))
        if leverage < min_lev:
            leverage = min(max(leverage, min_lev), 40)
            log.info(f"[OrderExec] Leverage boosted to {leverage}x "
                     f"(capital=${capital:.2f}, price=${entry_price:.0f})")

        # ── 2. Set leverage ──────────────────────────────────────────────
        for attempt in range(MAX_RETRIES):
            try:
                self._client.futures_change_leverage(
                    symbol=settings.get_symbol(), leverage=leverage
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

        # ── 3. Calcular cantidad (size) ──────────────────────────────────────
        raw_qty = (capital * leverage) / entry_price
        if use_all_in:
            raw_qty = math.floor(raw_qty * 1000) / 1000
        quantity_str = self._round_quantity(raw_qty)
        quantity_val = float(quantity_str)
        if quantity_val <= 0 or quantity_val < 0.001:
            self.order_result.emit(
                False,
                f"[{env_tag}] Cantidad inválida: raw={raw_qty:.6f} → "
                f"formateada={quantity_str} — "
                f"mínimo requerido es 0.001 BTC. "
                f"Prueba con más capital o menor precio de entrada.",
                {"environment": env_tag, "raw_qty": raw_qty,
                 "quantity": quantity_str, "min_required": 0.001},
            )
            return

        # ── 3b. Verificar margen disponible antes de enviar ─────────────────
        bal_info = self.get_balance()
        available_bal = bal_info.get("available", 0.0) if bal_info.get("success") else 0.0
        required_margin = (quantity_val * entry_price) / leverage
        if available_bal > 0 and required_margin > available_bal:
            max_qty_by_bal = (available_bal * leverage) / entry_price
            max_qty_by_bal = math.floor(max_qty_by_bal * 1000) / 1000  # truncar a 0.001
            if max_qty_by_bal < 0.001:
                self.order_result.emit(
                    False,
                    f"[{env_tag}] Margen insuficiente — "
                    f"se requieren ${required_margin:.2f} pero hay ${available_bal:.2f} disponibles. "
                    f"Deposita más USDT en tu wallet de Futuros o reduce el apalancamiento.",
                    {"environment": env_tag, "direction": direction,
                     "error": f"Margin insufficient: need ${required_margin:.2f}, have ${available_bal:.2f}",
                     "required_margin": required_margin, "available_balance": available_bal},
                )
                return
            quantity_val = max_qty_by_bal
            quantity_str = self._round_quantity(quantity_val)
            log.info(
                f"[OrderExec] Cantidad reducida por margen: "
                f"{quantity_val:.4f} BTC (max posible con ${available_bal:.2f})"
            )

        # ── 3. Split MARKET entry (Mejora 1: 3 micro-tickets) ────────────
        entry_data = self._execute_split_entry(side, quantity_val, quantity_str, leverage, entry_price)
        entry_order_id = entry_data.get("order_id", "")
        entry_fill_price_val = entry_data.get("fill_price", entry_price)
        entry_all_ids = entry_data.get("all_order_ids", [])
        quantity_val = entry_data.get("total_qty", quantity_val)  # real fill qty
        quantity_str = self._round_quantity(quantity_val)

        if not entry_order_id:
            self.order_result.emit(
                False,
                f"[{env_tag}] Split entry failed — all tickets rejected",
                {"environment": env_tag, "direction": direction,
                 "quantity": quantity_str, "error": "Split entry failed"},
            )
            return

        # ── Recalculate SL/TP based on average fill price ──
        atr = task.get("atr", 0.0)
        wall_bid = task.get("wall_bid", 0.0)
        wall_ask = task.get("wall_ask", 0.0)
        if atr > 0:
            atr_colchon = max(atr, entry_fill_price_val * 0.002) * 1.5
            if side == "BUY":
                new_sl = entry_fill_price_val - atr_colchon
                if wall_bid > 0:
                    new_sl = min(new_sl, wall_bid - 5)
                sl_price = new_sl
                tp_price = entry_fill_price_val * 1.02
            else:
                new_sl = entry_fill_price_val + atr_colchon
                if wall_ask > 0:
                    new_sl = max(new_sl, wall_ask + 5)
                sl_price = new_sl
                tp_price = entry_fill_price_val * 0.98
            log.info(
                f"[OrderExec] SL/TP recalculated from fill ${entry_fill_price_val:.2f}: "
                f"SL=${sl_price:.2f} TP=${tp_price:.2f}"
            )

            vol_buffer = entry_fill_price_val * 0.0005
            if atr > 0 and atr > entry_fill_price_val * 0.005:
                vol_buffer = entry_fill_price_val * 0.0008
            if side == "BUY":
                sl_price = min(sl_price, sl_price - vol_buffer)
            else:
                sl_price = max(sl_price, sl_price + vol_buffer)
            log.info(
                f"[OrderExec] SL buffer applied ({vol_buffer:.2f}): "
                f"SL=${sl_price:.2f}"
            )

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
                        symbol=settings.get_symbol(),
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
                        symbol=settings.get_symbol(),
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

        # ── 5. Emit result ──────────────────────────────────────────────
        open_ts = datetime.now(timezone.utc).isoformat()
        result_data = {
            "type": "order_execution",
            "event": "open",
            "environment": env_tag,
            "direction": side,
            "entry_price": entry_fill_price_val,
            "qty_btc": quantity_val,
            "margen_usdt": capital,
            "apalancamiento": settings.LEVERAGE,
            "sl_price": float(rounded_sl_str),
            "tp_price": float(rounded_tp_str),
            "entry_order_id": entry_order_id,
            "entry_all_ids": entry_all_ids,
            "total_qty": quantity_val,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "leverage": leverage,
            "capital": capital,
            "confidence": confidence,
            "dynamic_leverage": leverage if used_dynamic else 0,
            "reactive_leverage": leverage,
            "use_all_in": use_all_in,
            "open_timestamp": open_ts,
            "delta_entrada": task.get("delta", 0),
            "cvd_entrada": task.get("cvd", 0),
            "filtro_entrada": task.get("entry_filter", ""),
        }
        if bracket_errors:
            result_data["bracket_errors"] = "; ".join(bracket_errors)

        success = bool(entry_order_id) and not bracket_errors
        msg_parts = [
            f"[{env_tag}] {'✅' if success else '❌'} "
            f"{direction} {quantity_val:.4f} BTC @ {entry_fill_price_val:.2f}",
            f"| SL {result_data['sl_price']:.2f} | TP {result_data['tp_price']:.2f}",
            f"| IDs: entry={entry_order_id} "
            f"split_tickets={len(entry_all_ids)} "
            f"sl={sl_order_id or 'FAIL'} tp={tp_order_id or 'FAIL'}",
        ]
        if bracket_errors and entry_order_id:
            msg_parts.append(
                "⚠️ POSICIÓN ABIERTA SIN PROTECCIÓN: "
                + "; ".join(bracket_errors)
            )
            log.critical(
                "[OrderExec] POSICIÓN SIN BRACKET — entry=%s OK pero SL/TP "
                "fallaron. Errores: %s",
                entry_order_id, "; ".join(bracket_errors))
        msg = " ".join(msg_parts)
        self.order_result.emit(success, msg, result_data)

        # ── 6. Start position-close polling on successful open ─────────
        if success and result_data["event"] == "open":
            self._has_open_position = True
            self._open_position_data = {
                "entry_price": entry_fill_price_val,
                "qty_btc": quantity_val,
                "direction": side,
                "sl_price": float(rounded_sl_str),
                "tp_price": float(rounded_tp_str),
                "capital": capital,
                "leverage": settings.LEVERAGE,
                "open_timestamp": open_ts,
                "delta_entrada": task.get("delta", 0),
                "cvd_entrada": task.get("cvd", 0),
                "filtro_entrada": task.get("entry_filter", ""),
                "max_pnl": 0.0,
            }
            self._start_close_polling()

    # ── Position-close polling ───────────────────────────────────────────
    def _start_close_polling(self):
        self._close_poll_flag = True

    def _stop_close_polling(self):
        self._close_poll_flag = False

    def _poll_position_close(self):
        """Check if open position closed, emit close snapshot if so."""
        if not self._open_position_data:
            self._close_poll_flag = False
            return

        # Snapshot current position state before check
        last_pnl = self._position_info.get("unRealizedProfit", 0) if self._position_info else 0
        last_mark = self._position_info.get("markPrice", 0) if self._position_info else 0
        last_entry = self._position_info.get("entryPrice", 0) if self._position_info else 0
        pos_open_before = self._has_open_position

        pos = self.check_position_status()
        pos_open_after = self._has_open_position

        # Transitioned from open → closed
        if pos_open_before and not pos_open_after:
            open_data = self._open_position_data
            self._open_position_data = None
            self._close_poll_flag = False

            # Best exit price estimate: use last mark or entry
            exit_price = last_mark if last_mark > 0 else last_entry
            pnl_real = float(last_pnl)
            entry_px = open_data.get("entry_price", 0) or last_entry
            cap = max(open_data.get("capital", 1), 1)
            risk_btc = abs(entry_px - open_data.get("sl_price", 0)) if open_data.get("sl_price", 0) > 0 else 0
            risk_usdt = risk_btc * open_data.get("qty_btc", 0)
            duracion = 0
            if "open_timestamp" in open_data:
                try:
                    ot = datetime.fromisoformat(open_data["open_timestamp"])
                    duracion = (datetime.now(timezone.utc) - ot.replace(tzinfo=timezone.utc)).total_seconds()
                except Exception:
                    pass

            motivo = "MANUAL"
            if pnl_real > 0:
                motivo = "TP"
            elif pnl_real < 0:
                motivo = "SL"

            close_data = {
                "type": "order_execution",
                "event": "close",
                "exit_price": exit_price,
                "pnl_usdt": pnl_real,
                "roe_pct": (pnl_real / cap) * 100 if cap > 0 else 0,
                "motivo_cierre": motivo,
                "duracion_segundos": duracion,
                "rr_real": abs(pnl_real / risk_usdt) if risk_usdt > 0 else 0,
                "close_timestamp": datetime.now(timezone.utc).isoformat(),
                **open_data,
            }
            log.info(f"[OrderExec] Position closed — emitting close snapshot: "
                     f"PnL=${pnl_real:+.2f} exit=${exit_price:.2f} motivo={motivo}")
            self.order_result.emit(True, f"Posición cerrada — PnL ${pnl_real:+.2f}", close_data)
            self.position_closed.emit(close_data)

    # ── Mejora 1: Split entry en 3 micro-tickets ─────────────────────────
    def _execute_split_entry(self, side, total_qty, total_qty_str, leverage, entry_price):
        base_qty = total_qty / SPLIT_ENTRY_TICKETS
        base_qty = max(base_qty, 0.001)

        fills = []
        all_ids = []

        for i in range(SPLIT_ENTRY_TICKETS):
            if i > 0:
                time.sleep(SPLIT_ENTRY_PULLBACK_WAIT_SEC)

            # Tramo 3: qty_restante = total_qty - sum(fills)
            if i == SPLIT_ENTRY_TICKETS - 1:
                qty_ya_ejecutada = sum(float(f.get('executedQty', 0)) for f in fills)
                qty_restante = max(total_qty - qty_ya_ejecutada, 0.001)
                qty_restante = min(qty_restante, total_qty * 0.36)
                qty_str = self._round_quantity(qty_restante)
            else:
                qty_str = self._round_quantity(base_qty)
            qty_val = float(qty_str)

            resp = self._place_market(side, qty_str, leverage)
            if resp is None:
                # Fallback: MARKET inmediato
                log.warning(f"[OrderExec] Split ticket {i+1} timeout — fallback MARKET")
                try:
                    resp = self._fapi_create_order(
                        symbol=settings.get_symbol(), side=side, type="MARKET", quantity=qty_str,
                    )
                except Exception as exc:
                    log.warning(f"[OrderExec] Fallback MARKET ticket {i+1} failed: {exc}")
                    continue
                if not resp or not resp.get("orderId"):
                    log.warning(f"[OrderExec] Fallback MARKET ticket {i+1} no orderId")
                    continue

            order_id = str(resp.get("orderId", ""))
            all_ids.append(order_id)

            avg_price_raw = resp.get("avgPrice", str(entry_price))
            try:
                fill_price = float(avg_price_raw)
            except (ValueError, TypeError):
                fill_price = entry_price
            if fill_price <= 0:
                fill_price = entry_price

            executed_raw = resp.get("executedQty", qty_str)
            try:
                fill_qty = float(executed_raw)
            except (ValueError, TypeError):
                fill_qty = qty_val

            # Precio teórico del tramo (con pullback esperado)
            if i == 0:
                precio_teorico = entry_price
            elif i < SPLIT_ENTRY_TICKETS - 1:
                precio_teorico = entry_price * (1 - SPLIT_ENTRY_PULLBACK_PCT) if side.upper() == "BUY" else entry_price * (1 + SPLIT_ENTRY_PULLBACK_PCT)
            else:
                precio_teorico = entry_price

            slippage_pct = (fill_price - precio_teorico) / max(precio_teorico, 1) * 100
            log.info(
                f"[OrderExec] Split fill {i+1}/{SPLIT_ENTRY_TICKETS}: "
                f"qty={fill_qty:.4f} @ {fill_price:.2f} "
                f"slippage={slippage_pct:.3f}% id={order_id}"
            )

            fills.append({"qty": fill_qty, "price": fill_price, "order_id": order_id, "executedQty": executed_raw})

        with self._lock:
            self._last_trade_time = time.time()

        if not fills:
            raise RuntimeError(f"Split entry failed: 0/{SPLIT_ENTRY_TICKETS} tickets filled")

        total_qty_filled = sum(f['qty'] for f in fills)
        avg_fill = sum(f['price'] for f in fills) / len(fills)

        return {
            "order_id": all_ids[0],
            "fill_price": avg_fill,
            "all_order_ids": all_ids,
            "total_qty": total_qty_filled,
        }

    def _place_market(self, side, quantity_str, leverage):
        for attempt in range(MAX_RETRIES):
            try:
                return self._fapi_create_order(
                    symbol=settings.get_symbol(),
                    side=side,
                    type="MARKET",
                    quantity=quantity_str,
                )
            except BinanceAPIException as exc:
                code = getattr(exc, "code", "?")
                msg = getattr(exc, "message", str(exc))
                log.warning(f"[OrderExec] MARKET {side} attempt {attempt+1}: [{code}] {msg}")
                if attempt == MAX_RETRIES - 1:
                    return None
                time.sleep(RETRY_DELAY_S)
            except Exception as exc:
                log.warning(f"[OrderExec] MARKET {side} attempt {attempt+1}: {exc}")
                if attempt == MAX_RETRIES - 1:
                    return None
                time.sleep(RETRY_DELAY_S)
        return None

    # ── Direct UI operations (bypass queue, immediate API calls) ───────────

    def change_margin_type(self, margin_type: str) -> dict:
        """Set ISOLATED or CROSSED margin mode for the current symbol.
        Returns dict with success bool. Errors are silent (already-set is not fatal).
        """
        result = {"success": False, "margin_type": margin_type}
        if self._client is None:
            return result
        try:
            self._client.futures_change_margin_type(
                symbol=settings.get_symbol(), marginType=margin_type.upper())
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
        """Set leverage for the current symbol directly (not via queue)."""
        result = {"success": False, "leverage": leverage}
        if self._client is None:
            return result
        try:
            self._client.futures_change_leverage(
                symbol=settings.get_symbol(), leverage=leverage)
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
        if quantity < 0.001:
            quantity = 0.001
        qty_str = self._round_quantity(quantity)
        qty_val = float(qty_str)
        if qty_val <= 0:
            result["message"] = f"Quantity too small: {quantity}"
            return result

        # ── HARD GATE: live position check (skip if reduce_only) ──────────────
        if not reduce_only:
            live_pos = self.check_position_status()
            if live_pos is not None:
                pos_amt = float(live_pos.get("positionAmt", 0))
                pos_side = "LONG" if pos_amt > 0 else "SHORT"
                result["message"] = (
                    f"🛡️ [BB-450 SECURITY RISK] Orden rechazada. "
                    f"El Executor detectó una posición activa en Binance: "
                    f"{pos_side} {abs(pos_amt):.4f} BTC. "
                    f"Evitando efecto Ping-Pong."
                )
                log.warning(f"[OrderExec] {result['message']}")
                return result

        try:
            params = {
                "symbol": settings.get_symbol(),
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

            if code == -2019:
                bal = self.get_balance()
                log.error(
                    f"[OrderExec] ERROR -2019: Margen insuficiente. "
                    f"Balance disponible: ${bal.get('available', 0):.2f}, "
                    f"Wallet: ${bal.get('balance', 0):.2f}"
                )
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
