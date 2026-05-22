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
import time
import math
import threading
from collections import deque
from typing import Dict, Optional

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
        self.symbol = symbol or settings.SYMBOL
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Circular buffers for HFT microstructure ──
        self._depth_events = deque(maxlen=500)
        self._spread_history = deque(maxlen=200)
        self._order_diffs = deque(maxlen=500)
        self._oi_history = deque(maxlen=300)

        # ── Last-known-good cache ──
        self._last_oi = 0.0
        self._last_best_bid = 0.0
        self._last_best_ask = 0.0
        self._last_spread_ts = 0.0

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
                asyncio.create_task(self._poll_open_interest(session)),
                asyncio.create_task(self._poll_mtf_indicators(session)),
                asyncio.create_task(self._poll_deep_book(session)),
                asyncio.create_task(self._poll_funding_rate(session)),
                asyncio.create_task(self._compute_hft_metrics()),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

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
    # 5. HFT MICROSTRUCTURE METRICS (computed every 1s from buffers)
    # ─────────────────────────────────────────────────────────────
    async def _compute_hft_metrics(self):
        while self._running:
            now = time.time()
            mom = self.market_state.get("momentum", {})

            # Tick Speed: depth events in last 1s
            recent = [t for t in self._depth_events if t >= now - 1.0]
            mom["tick_speed"] = len(recent) * 10  # extrapolate

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

            await asyncio.sleep(1.0)
