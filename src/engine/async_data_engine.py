"""
BB-450 Async Data Engine
========================
Non-blocking data pipeline that feeds real-time Binance Futures metrics
into the market_state dictionary via threading + asyncio.

Architecture:
  - Runs its own asyncio event loop in a daemon thread
  - Writes to a thread-safe shared dict (market_state) that the Qt UI reads
  - All API failures gracefully degrade to last-known-good values
"""

import asyncio
import logging
import time
import math

log = logging.getLogger(__name__)
import threading
from collections import deque
from typing import Dict, Optional, Any

import aiohttp
import numpy as np

from config.settings import settings

# ═══════════════════════════════════════════════════════════════════════════════
# MATH: Native RSI / MACD / EMA (no external TA lib dependency)
# ═══════════════════════════════════════════════════════════════════════════════

def _ema(data, period):
    """Exponential Moving Average over a numpy array."""
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    alpha = 2.0 / (period + 1)
    out = np.empty_like(data, dtype=float)
    out[0] = data[0]
    for i in range(1, len(data)):
        out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
    return out


def calc_rsi(closes, period=14):
    """Relative Strength Index from close prices."""
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
    """MACD line, signal line, histogram."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = ema_fast - ema_slow
    sig_line = _ema(macd_line, signal)
    hist = macd_line - sig_line
    return float(macd_line[-1]), float(sig_line[-1]), float(hist[-1])


# ═══════════════════════════════════════════════════════════════════════════════
# CORE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

BASE_URL = "https://fapi.binance.com"


class AsyncDataEngine:
    """
    Runs in a background daemon thread with its own asyncio loop.
    Populates a shared `market_state` dict that the Qt main thread reads.
    """

    def __init__(self, market_state: Dict, symbol: str = None):
        self.market_state = market_state
        self.symbol = symbol or settings.get_symbol()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Circular buffers for HFT microstructure ──
        self._depth_events = deque(maxlen=500)
        self._spread_history = deque(maxlen=200)
        self._order_diffs = deque(maxlen=500)
        self._oi_history = deque(maxlen=300)
        self._tick_speed_history = deque(maxlen=300)  # 5 min @ 1s

        # ── Circular buffers for indicator computation ──
        self._klines_cache: list = []
        self._ob_volume_history = deque(maxlen=1800)

        # ── Technical Levels Engine ──
        self._tech_levels_engine = None  # lazy import
        self._tech_levels_cache = {}  # last computed result

        # ── Last-known-good cache ──
        self._last_oi = 0.0
        self._last_best_bid = 0.0
        self._last_best_ask = 0.0
        self._last_spread_ts = 0.0
        self._last_price = 0.0

    # ─────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="AsyncDataEngine")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def reset_symbol(self, new_symbol: str):
        """Hot-swap symbol without restarting the engine.

        Clears internal buffers so stale data for the old symbol
        is not mixed with the new one.
        """
        self.symbol = new_symbol
        # Clear all circular buffers
        self._depth_events.clear()
        self._spread_history.clear()
        self._order_diffs.clear()
        self._oi_history.clear()
        self._tick_speed_history.clear()
        self._klines_cache.clear()
        self._ob_volume_history.clear()
        self._tech_levels_cache.clear()
        self._last_oi = 0.0
        self._last_best_bid = 0.0
        self._last_best_ask = 0.0
        self._last_spread_ts = 0.0
        self._last_price = 0.0
        log.info("AsyncDataEngine reset for symbol %s", new_symbol)

    def set_timeframe(self, interval: str):
        """Reinicia la collección de klines con un nuevo intervalo.

        Se limpia el caché de klines y se actualiza la clave interna
        para que el próximo ciclo _poll_klines_and_indicators use el
        nuevo timeframe.
        """
        old = self.market_state.get("timeframe", "1m")
        self.market_state["timeframe"] = interval
        self._klines_cache.clear()
        self._tech_levels_cache.clear()
        log.info("AsyncDataEngine timeframe changed: %s → %s", old, interval)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            print(f"[AsyncDataEngine] Fatal: {e}")
        finally:
            self._loop.close()

    async def _main(self):
        """Spawn all concurrent tasks."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            tasks = [
                asyncio.create_task(self._poll_price(session)),
                asyncio.create_task(self._poll_klines_and_indicators(session)),
                asyncio.create_task(self._poll_trades(session)),
                asyncio.create_task(self._poll_order_book_light(session)),
                asyncio.create_task(self._poll_open_interest(session)),
                asyncio.create_task(self._poll_mtf_indicators(session)),
                asyncio.create_task(self._poll_deep_book(session)),
                asyncio.create_task(self._poll_funding_rate(session)),
                asyncio.create_task(self._poll_technical_levels(session)),
                asyncio.create_task(self._compute_hft_metrics()),
                asyncio.create_task(self._heartbeat()),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self):
        """Log cada 5 minutos confirmando que el radar sigue vivo."""
        while self._running:
            await asyncio.sleep(300)
            if self._running:
                price = self.market_state.get('price', 0)
                price_str = f"${price:,.0f}" if isinstance(price, (int, float)) and price > 0 else "--"
                print(f"💓 [HEARTBEAT] BB-450 AsyncDataEngine activo | "
                      f"precio={price_str} | "
                      f"klines={len(self._klines_cache)} | "
                      f"oi_history={len(self._oi_history)}")

    # ─────────────────────────────────────────────────────────────
    # 0. PRICE TICKER (every 1s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_price(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/ticker/price"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        p = float(data.get("price", 0))
                        old = self._last_price
                        self.market_state["price"] = p
                        self.market_state["last_price"] = old or p
                        self.market_state["change_pct"] = ((p - old) / max(old, 0.0001)) * 100 if old > 0 else 0
                        self._last_price = p
                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # ─────────────────────────────────────────────────────────────
    # 0b. KLINES + INDICATORS (every 1s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_klines_and_indicators(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/klines"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol, "interval": "1m", "limit": 200}) as resp:
                    if resp.status == 200:
                        klines = await resp.json()
                        self._klines_cache = klines
                        self.market_state["klines"] = klines

                        closes = [float(k[4]) for k in klines]
                        highs = [float(k[2]) for k in klines]
                        lows = [float(k[3]) for k in klines]
                        volumes = [float(k[5]) for k in klines]

                        ind = {}

                        # VWAP
                        typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(klines))]
                        cum_pv = sum(typical[i] * volumes[i] for i in range(len(klines)))
                        cum_v = sum(volumes)
                        ind["vwap"] = cum_pv / cum_v if cum_v > 0 else 0

                        ind["day_high"] = max(highs[-100:]) if len(highs) >= 100 else max(highs)
                        ind["day_low"] = min(lows[-100:]) if len(lows) >= 100 else min(lows)

                        p = self.market_state.get("price", closes[-1])
                        if ind["vwap"] > 0:
                            ind["price_vwap_dist"] = ((p - ind["vwap"]) / ind["vwap"]) * 100

                        ind["ema_20"] = float(np.mean(closes[-20:])) if len(closes) >= 20 else 0
                        ind["ema_50"] = float(np.mean(closes[-50:])) if len(closes) >= 50 else ind["ema_20"]

                        sma20 = float(np.mean(closes[-20:]))
                        std = float(np.std(closes[-20:]))
                        ind["bb_upper"] = sma20 + 2 * std
                        ind["bb_middle"] = sma20
                        ind["bb_lower"] = sma20 - 2 * std
                        if ind["bb_upper"] != ind["bb_lower"]:
                            ind["bb_position"] = ((closes[-1] - ind["bb_lower"]) / (ind["bb_upper"] - ind["bb_lower"])) * 100
                        else:
                            ind["bb_position"] = 50.0

                        ind["rsi"] = calc_rsi(np.array(closes))
                        macd_l, macd_s, macd_h = calc_macd(np.array(closes))
                        ind["macd"] = macd_l
                        ind["macd_signal"] = macd_s
                        ind["macd_hist"] = macd_h

                        trs = []
                        for i in range(1, len(klines)):
                            h = float(klines[i][2]); l = float(klines[i][3])
                            pc = float(klines[i-1][4])
                            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                        ind["atr"] = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0

                        ind["avg_volume"] = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0

                        # Trend
                        if ind["ema_20"] > ind["ema_50"]:
                            ind["trend"] = "ALCISTA"
                        elif ind["ema_20"] < ind["ema_50"]:
                            ind["trend"] = "BAJISTA"
                        else:
                            ind["trend"] = "NEUTRAL"

                        self.market_state["indicators"] = ind

                        # ── Signal ──
                        signal = "NINGUNA"
                        lc = 0
                        if ind["rsi"] < 30: lc += 1
                        if ind["bb_position"] < 20: lc += 1
                        if ind["macd"] > ind["macd_signal"] and ind["macd_hist"] > 0: lc += 1
                        if self.market_state.get("order_flow", {}).get("delta", 0) > 100: lc += 1
                        if ind["trend"] == "ALCISTA": lc += 1
                        sc = 0
                        if ind["rsi"] > 70: sc += 1
                        if ind["bb_position"] > 80: sc += 1
                        if ind["macd"] < ind["macd_signal"] and ind["macd_hist"] < 0: sc += 1
                        if self.market_state.get("order_flow", {}).get("delta", 0) < -100: sc += 1
                        if ind["trend"] == "BAJISTA": sc += 1
                        if lc >= 3: signal = "COMPRA"
                        elif sc >= 3: signal = "VENTA"
                        self.market_state["signal"] = signal

                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # ─────────────────────────────────────────────────────────────
    # 0c. TRADES + ORDER FLOW (every 1s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_trades(self, session: aiohttp.ClientSession):
        from src.engine.order_flow import order_flow_engine
        url = f"{BASE_URL}/fapi/v1/aggTrades"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol, "limit": 50}) as resp:
                    if resp.status == 200:
                        trades = await resp.json()
                        self.market_state["trades"] = trades

                        for t in trades[:20]:
                            order_flow_engine.add_trade({
                                "time": int(t["T"]),
                                "price": float(t["p"]),
                                "quantity": float(t["q"]),
                                "is_buyer_maker": t["m"],
                            })

                        delta_info = order_flow_engine.calculate_delta()
                        of = self.market_state.get("order_flow", {})
                        of["delta"] = delta_info.get("delta", 0)
                        of["cvd"] = order_flow_engine.cumulative_delta
                        of["buy_volume"] = delta_info.get("buy_volume", 0)
                        of["sell_volume"] = delta_info.get("sell_volume", 0)
                        of["window_buy_volume"] = delta_info.get("window_buy_volume", 0)
                        of["window_sell_volume"] = delta_info.get("window_sell_volume", 0)

                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # ─────────────────────────────────────────────────────────────
    # 0d. ORDER BOOK + WHALE WALLS (every 1s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_order_book_light(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/depth"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol, "limit": 20}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.market_state["order_book"] = data

                        # ── Whale wall detection via z-score ──
                        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
                        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]

                        all_qties = [q for _, q in bids] + [q for _, q in asks]
                        if all_qties:
                            self._ob_volume_history.extend(all_qties)

                        z_threshold = 3.0
                        min_samples = 30
                        ob_max_z = 0.0
                        if len(self._ob_volume_history) >= min_samples:
                            arr = list(self._ob_volume_history)
                            mean = sum(arr) / len(arr)
                            variance = sum((x - mean) ** 2 for x in arr) / len(arr)
                            std = variance ** 0.5 if variance > 0 else 1.0
                            dynamic_min = mean + z_threshold * std
                            # Track max z-score across all levels (for explosion trigger)
                            for qty in all_qties:
                                z = (qty - mean) / std if std > 0 else 0
                                if z > ob_max_z:
                                    ob_max_z = z
                        else:
                            dynamic_min = 10.0
                        self.market_state["_ob_max_z_score"] = ob_max_z

                        buy_walls = []
                        sell_walls = []
                        for price, qty in bids:
                            if qty >= dynamic_min:
                                buy_walls.append({"price": price, "quantity": qty})
                        for price, qty in asks:
                            if qty >= dynamic_min:
                                sell_walls.append({"price": price, "quantity": qty})
                        buy_walls.sort(key=lambda x: x["quantity"], reverse=True)
                        sell_walls.sort(key=lambda x: x["quantity"], reverse=True)

                        total_buy = sum(w["quantity"] for w in buy_walls)
                        total_sell = sum(w["quantity"] for w in sell_walls)
                        imbalance = (total_buy - total_sell) / max(total_buy + total_sell + 0.001, 0.001)

                        ww = {
                            "buy_walls": buy_walls[:5],
                            "sell_walls": sell_walls[:5],
                            "total_buy_walls": total_buy,
                            "total_sell_walls": total_sell,
                            "imbalance": imbalance,
                            "signal": "BUY_WALL" if imbalance > 0.3 else "SELL_WALL" if imbalance < -0.3 else "NEUTRAL",
                        }
                        self.market_state["whale_walls"] = ww

                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # ─────────────────────────────────────────────────────────────
    # 1. OPEN INTEREST (every 1s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_open_interest(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/openInterest"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        oi = float(data.get("openInterest", 0))
                        ts = time.time()

                        self._oi_history.append({"ts": ts, "oi": oi})

                        # Deltas
                        delta_1s = oi - self._last_oi if self._last_oi else 0
                        delta_5s = self._calc_oi_delta(ts, 5)
                        delta_1m = self._calc_oi_delta(ts, 60)
                        delta_5m = self._calc_oi_delta(ts, 300)

                        of = self.market_state.get("order_flow", {})
                        of["open_interest"] = oi
                        of["oi_delta_1s"] = (delta_1s / max(oi, 1)) * 100
                        of["oi_delta_5s"] = (delta_5s / max(oi, 1)) * 100
                        of["oi_delta_1m"] = (delta_1m / max(oi, 1)) * 100

                        self._last_oi = oi
                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    def _calc_oi_delta(self, now, seconds):
        cutoff = now - seconds
        filtered = [h for h in self._oi_history if h["ts"] >= cutoff]
        if len(filtered) < 2:
            return 0
        return filtered[-1]["oi"] - filtered[0]["oi"]

    # ─────────────────────────────────────────────────────────────
    # 2. MULTI-TIMEFRAME INDICATORS (every 15s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_mtf_indicators(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/klines"
        timeframes = [
            ("5m", 100, "5M"),
            ("15m", 100, "15M"),
            ("1h", 100, "1H"),
            ("4h", 100, "4H"),
            ("1d", 100, "1D"),
        ]
        while self._running:
            for interval, limit, label in timeframes:
                try:
                    params = {"symbol": self.symbol, "interval": interval, "limit": limit}
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            klines = await resp.json()
                            closes = np.array([float(k[4]) for k in klines])
                            if len(closes) > 26:
                                rsi_val = calc_rsi(closes)
                                macd_l, macd_s, macd_h = calc_macd(closes)

                                mt = self.market_state.get("mtf_trend", {})
                                mt[f"rsi_{label.lower()}"] = rsi_val
                                mt[f"macd_{label.lower()}"] = macd_l

                                # Also populate EMA crosses for confluence
                                ema_f = _ema(closes, 9)
                                ema_s = _ema(closes, 21)
                                if len(ema_f) > 0 and len(ema_s) > 0:
                                    cross = "ALCISTA" if ema_f[-1] > ema_s[-1] else "BAJISTA"
                                    mt[f"ema_cross_{label.lower()}"] = cross

                                # Trend direction
                                if rsi_val > 60 and macd_h > 0:
                                    mt[f"t_{label.lower()}"] = "ALCISTA"
                                elif rsi_val < 40 and macd_h < 0:
                                    mt[f"t_{label.lower()}"] = "BAJISTA"
                                else:
                                    mt[f"t_{label.lower()}"] = "NEUTRAL"

                        elif resp.status == 429:
                            await asyncio.sleep(10)
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            await asyncio.sleep(15.0)

    # ─────────────────────────────────────────────────────────────
    # 3. DEEP ORDER BOOK (every 2s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_deep_book(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/depth"
        prev_bids = {}
        prev_asks = {}
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol, "limit": 100}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
                        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
                        ts = time.time()

                        # ── Whale wall detection (top-2 by volume) ──
                        bids_sorted = sorted(bids, key=lambda x: x[1], reverse=True)
                        asks_sorted = sorted(asks, key=lambda x: x[1], reverse=True)

                        lq = self.market_state.get("liquidity", {})
                        if len(bids_sorted) >= 2:
                            lq["wall_bid_1"] = bids_sorted[0][0]
                            lq["wall_bid_size_1"] = bids_sorted[0][1]
                            lq["wall_bid_2"] = bids_sorted[1][0]
                            lq["wall_bid_size_2"] = bids_sorted[1][1]
                        if len(asks_sorted) >= 2:
                            lq["wall_ask_1"] = asks_sorted[0][0]
                            lq["wall_ask_size_1"] = asks_sorted[0][1]
                            lq["wall_ask_2"] = asks_sorted[1][0]
                            lq["wall_ask_size_2"] = asks_sorted[1][1]

                        # ── Best bid/ask for spread tracking ──
                        if bids and asks:
                            best_bid = bids[0][0]
                            best_ask = asks[0][0]
                            spread = best_ask - best_bid

                            now_ms = time.time() * 1000
                            if self._last_best_bid != best_bid or self._last_best_ask != best_ask:
                                if self._last_spread_ts > 0:
                                    delta_ms = now_ms - self._last_spread_ts
                                    self._spread_history.append(delta_ms)
                                self._last_spread_ts = now_ms
                            self._last_best_bid = best_bid
                            self._last_best_ask = best_ask

                            mom = self.market_state.get("momentum", {})
                            mom["spread_raw"] = spread

                        # ── Depth imbalance (100 levels) ──
                        total_bid_vol = sum(q for _, q in bids)
                        total_ask_vol = sum(q for _, q in asks)
                        total = total_bid_vol + total_ask_vol
                        if total > 0:
                            lq["depth_imbalance"] = ((total_bid_vol - total_ask_vol) / total) * 100

                        # ── Order diff detection (cancel rate proxy) ──
                        curr_bids = {p: q for p, q in bids}
                        curr_asks = {p: q for p, q in asks}
                        cancelled = 0
                        for p, q in prev_bids.items():
                            if p not in curr_bids or curr_bids[p] < q * 0.5:
                                cancelled += 1
                        for p, q in prev_asks.items():
                            if p not in curr_asks or curr_asks[p] < q * 0.5:
                                cancelled += 1
                        total_prev = len(prev_bids) + len(prev_asks)
                        if total_prev > 0:
                            self._order_diffs.append({
                                "ts": ts,
                                "cancel_rate": cancelled / total_prev * 100
                            })
                        prev_bids = curr_bids
                        prev_asks = curr_asks

                        self._depth_events.append(ts)

                    elif resp.status == 429:
                        await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(2.0)

    # ─────────────────────────────────────────────────────────────
    # 4. FUNDING RATE (every 30s)
    # ─────────────────────────────────────────────────────────────
    async def _poll_funding_rate(self, session: aiohttp.ClientSession):
        url = f"{BASE_URL}/fapi/v1/premiumIndex"
        while self._running:
            try:
                async with session.get(url, params={"symbol": self.symbol}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fr = float(data.get("lastFundingRate", 0))
                        of = self.market_state.get("order_flow", {})
                        of["funding_rate"] = fr * 100
            except Exception:
                pass
            await asyncio.sleep(30.0)

    # ─────────────────────────────────────────────────────────────
    # 4b. TECHNICAL LEVELS (Swing / Fibonacci / S&R / Confluence) — every 30s
    # ─────────────────────────────────────────────────────────────
    async def _poll_technical_levels(self, session: aiohttp.ClientSession):
        """Compute swing highs/lows, Fibonacci, S/R, and confluence zones."""
        if self._tech_levels_engine is None:
            from src.engine.technical_levels import TechnicalLevelsEngine
            self._tech_levels_engine = TechnicalLevelsEngine()

        while self._running:
            try:
                klines_1m = self._klines_cache or []
                klines_5m_data = []
                klines_15m_data = []

                # Fetch 5m and 15m klines for multi-TF S/R
                async with session.get(
                    f"{BASE_URL}/fapi/v1/klines",
                    params={"symbol": self.symbol, "interval": "5m", "limit": 100},
                ) as resp:
                    if resp.status == 200:
                        klines_5m_data = await resp.json()
                async with session.get(
                    f"{BASE_URL}/fapi/v1/klines",
                    params={"symbol": self.symbol, "interval": "15m", "limit": 100},
                ) as resp:
                    if resp.status == 200:
                        klines_15m_data = await resp.json()

                ind = self.market_state.get("indicators", {})
                price = self.market_state.get("price", 0)
                result = self._tech_levels_engine.compute(
                    klines_1m=klines_1m,
                    klines_5m=klines_5m_data,
                    klines_15m=klines_15m_data,
                    price=price,
                    vwap=ind.get("vwap", 0),
                    ema20=ind.get("ema_20", 0),
                    ema50=ind.get("ema_50", 0),
                )
                self._tech_levels_cache = result
                self.market_state["technical_levels"] = result

            except Exception:
                pass
            await asyncio.sleep(30.0)

    # ─────────────────────────────────────────────────────────────
    # 5. HFT MICROSTRUCTURE METRICS (computed every 1s from buffers)
    # ─────────────────────────────────────────────────────────────
    async def _compute_hft_metrics(self):
        while self._running:
            now = time.time()
            mom = self.market_state.get("momentum", {})

            # Tick Speed: depth events in last 1s
            recent = [t for t in self._depth_events if t >= now - 1.0]
            tick_spd = len(recent) * 10
            mom["tick_speed"] = tick_spd

            # Rolling 5-min tick-speed history for explosion detection
            self._tick_speed_history.append(tick_spd)
            mom["tick_speed_avg_5m"] = (
                sum(self._tick_speed_history) / len(self._tick_speed_history)
            ) if self._tick_speed_history else 30.0

            # Order Cancel Rate: average from recent diffs
            recent_diffs = [d for d in self._order_diffs if d["ts"] >= now - 10]
            if recent_diffs:
                mom["cancel_rate"] = sum(d["cancel_rate"] for d in recent_diffs) / len(recent_diffs)
            else:
                mom["cancel_rate"] = 0.0

            # Spread Velocity: median ms between spread changes
            if len(self._spread_history) > 2:
                arr = list(self._spread_history)[-20:]
                mom["spread_velocity"] = float(np.median(arr))
            else:
                mom["spread_velocity"] = 0.0

            # Skewness from depth imbalance history
            if len(self._order_diffs) > 10:
                rates = [d["cancel_rate"] for d in list(self._order_diffs)[-50:]]
                mean_r = np.mean(rates)
                std_r = np.std(rates)
                if std_r > 0:
                    mom["skewness"] = float(np.mean(((np.array(rates) - mean_r) / std_r) ** 3))
                else:
                    mom["skewness"] = 0.0

            # HFT Toxicity (PINAM): ratio of aggressive vs passive fills proxy
            lq = self.market_state.get("liquidity", {})
            d_imb = lq.get("depth_imbalance", 0)
            cancel = mom.get("cancel_rate", 0)
            mom["pinam"] = min(1.0, (abs(d_imb) / 50 + cancel / 100) / 2)

            # ── Volatility Explosion Sensor ───────────────────────────
            avg_5m = mom.get("tick_speed_avg_5m", 30)
            tick_trigger = tick_spd > avg_5m * 3.0 and avg_5m > 5
            # Individual order volume z-score > 3.5σ (checked in _poll_order_book_light)
            ob_z_trigger = self.market_state.get("_ob_max_z_score", 0) > 3.5
            mom["volatility_explosion"] = tick_trigger or ob_z_trigger

            await asyncio.sleep(1.0)
