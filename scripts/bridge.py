#!/usr/bin/env python3
"""
BB-450 Bridge — corre en tu máquina local (Colombia) y envía datos de Binance a Render.

Uso:
    pip install requests python-binance python-dotenv numpy
    python scripts/bridge.py

Variables de entorno (mismas que .env):
    RENDER_URL     → https://z-b-450-trade.onrender.com
    BINANCE_API_KEY / SECRET → tus claves de Binance
    BINANCE_TESTNET=True
    SYMBOL=BTCUSDT
"""

import os
import sys
import time
import json
import math
import signal
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import numpy as np
from dotenv import load_dotenv

load_dotenv()

RENDER_URL = os.environ.get("RENDER_URL", "https://z-b-450-trade.onrender.com")
SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
BINANCE_TESTNET = os.environ.get("BINANCE_TESTNET", "True").lower() in ("true", "1", "yes")
POLL_INTERVAL = 1.0  # seconds

BASE_URL = "https://testnet.binancefuture.com" if BINANCE_TESTNET else "https://fapi.binance.com"

running = True


# ── Math helpers (same as async_data_engine) ─────────────────────────

def _ema(data, period):
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    alpha = 2.0 / (period + 1)
    out = np.empty_like(data, dtype=float)
    out[0] = data[0]
    for i in range(1, len(data)):
        out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
    return out


def calc_rsi(closes, period=14):
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
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = ema_fast - ema_slow
    sig_line = _ema(macd_line, signal)
    hist = macd_line - sig_line
    return float(macd_line[-1]), float(sig_line[-1]), float(hist[-1])


# ── Binance API calls ────────────────────────────────────────────────

def fetch_klines(limit=200):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": "1m", "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_ticker():
    url = f"{BASE_URL}/fapi/v1/ticker/price"
    r = requests.get(url, params={"symbol": SYMBOL}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_trades(limit=50):
    url = f"{BASE_URL}/fapi/v1/aggTrades"
    r = requests.get(url, params={"symbol": SYMBOL, "limit": limit}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_depth(limit=20):
    url = f"{BASE_URL}/fapi/v1/depth"
    r = requests.get(url, params={"symbol": SYMBOL, "limit": limit}, timeout=5)
    r.raise_for_status()
    return r.json()


def fetch_funding():
    url = f"{BASE_URL}/fapi/v1/premiumIndex"
    r = requests.get(url, params={"symbol": SYMBOL}, timeout=5)
    r.raise_for_status()
    return float(r.json().get("lastFundingRate", 0)) * 100


def fetch_open_interest():
    url = f"{BASE_URL}/fapi/v1/openInterest"
    r = requests.get(url, params={"symbol": SYMBOL}, timeout=5)
    r.raise_for_status()
    return float(r.json()["openInterest"])


# ── Signal logic (simplified, same as dashboard.py) ─────────────────

def determine_signal(ind, of):
    lc = 0
    if ind.get("rsi", 50) < 30: lc += 1
    if ind.get("bb_position", 50) < 20: lc += 1
    if ind.get("macd_hist", 0) > 0: lc += 1
    if of.get("delta", 0) > 100: lc += 1
    if ind.get("trend") == "ALCISTA": lc += 1

    sc = 0
    if ind.get("rsi", 50) > 70: sc += 1
    if ind.get("bb_position", 50) > 80: sc += 1
    if ind.get("macd_hist", 0) < 0: sc += 1
    if of.get("delta", 0) < -100: sc += 1
    if ind.get("trend") == "BAJISTA": sc += 1

    if lc >= 3: return "COMPRA"
    if sc >= 3: return "VENTA"
    return "NINGUNA"


# ── Main loop ────────────────────────────────────────────────────────

def main():
    global running

    def handle_signal(sig, frame):
        global running
        print("\n[Bridge] Apagando...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"╔══════════════════════════════════════╗")
    print(f"║    BB-450 BRIDGE                     ║")
    print(f"║    Enviando datos a:                 ║")
    print(f"║    {RENDER_URL:<35s}║")
    print(f"║    Símbolo: {SYMBOL:<28s}║")
    print(f"║    Testnet: {str(BINANCE_TESTNET):<28s}║")
    print(f"╚══════════════════════════════════════╝")

    last_price = 0
    klines_cache = []

    while running:
        try:
            # ── Fetch data from Binance ──
            klines = fetch_klines()
            price = fetch_ticker()
            trades = fetch_trades()
            depth = fetch_depth()
            funding = fetch_funding()
            oi = fetch_open_interest()

            klines_cache = klines

            # ── Compute indicators ──
            closes = np.array([float(k[4]) for k in klines])
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            ind = {}

            # VWAP
            typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(klines))]
            cum_pv = sum(typical[i] * volumes[i] for i in range(len(klines)))
            cum_v = sum(volumes)
            ind["vwap"] = cum_pv / cum_v if cum_v > 0 else 0

            # EMA-like trend
            ind["ema_20"] = float(np.mean(closes[-20:])) if len(closes) >= 20 else 0
            ind["ema_50"] = float(np.mean(closes[-50:])) if len(closes) >= 50 else ind["ema_20"]

            # Bollinger
            sma20 = float(np.mean(closes[-20:]))
            std = float(np.std(closes[-20:]))
            ind["bb_upper"] = sma20 + 2 * std
            ind["bb_middle"] = sma20
            ind["bb_lower"] = sma20 - 2 * std
            if ind["bb_upper"] != ind["bb_lower"]:
                ind["bb_position"] = ((closes[-1] - ind["bb_lower"]) / (ind["bb_upper"] - ind["bb_lower"])) * 100
            else:
                ind["bb_position"] = 50.0

            ind["rsi"] = calc_rsi(closes)
            macd_l, macd_s, macd_h = calc_macd(closes)
            ind["macd"] = macd_l
            ind["macd_signal"] = macd_s
            ind["macd_hist"] = macd_h

            # ATR
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

            # ── Order Flow (simple delta from aggTrades) ──
            bids_vol = sum(float(t["q"]) for t in trades if not t["m"])
            asks_vol = sum(float(t["q"]) for t in trades if t["m"])
            delta = bids_vol - asks_vol

            of = {
                "delta": delta,
                "cvd": delta,  # simplified
                "buy_volume": bids_vol,
                "sell_volume": asks_vol,
                "funding_rate": funding,
                "open_interest": oi,
                "oi_delta_5m": 0,
            }

            change_pct = ((price - last_price) / max(last_price, 0.0001)) * 100 if last_price > 0 else 0
            last_price = price

            # ── Signal ──
            signal = determine_signal(ind, of)

            # ── Build payload ──
            payload = {
                "price": price,
                "signal": signal,
                "change_pct": round(change_pct, 2),
                "indicators": {
                    "rsi": round(ind.get("rsi", 50), 1),
                    "macd": round(ind.get("macd", 0), 2),
                    "macd_signal": round(ind.get("macd_signal", 0), 2),
                    "macd_hist": round(ind.get("macd_hist", 0), 2),
                    "bb_upper": round(ind.get("bb_upper", 0), 2),
                    "bb_middle": round(ind.get("bb_middle", 0), 2),
                    "bb_lower": round(ind.get("bb_lower", 0), 2),
                    "bb_position": round(ind.get("bb_position", 50), 1),
                    "ema_20": round(ind.get("ema_20", 0), 2),
                    "ema_50": round(ind.get("ema_50", 0), 2),
                    "atr": round(ind.get("atr", 0), 2),
                    "vwap": round(ind.get("vwap", 0), 2),
                    "avg_volume": round(ind.get("avg_volume", 0), 2),
                    "trend": ind.get("trend", "NEUTRAL"),
                },
                "order_flow": {
                    "delta": round(delta, 2),
                    "cvd": round(delta, 2),
                    "buy_volume": round(bids_vol, 4),
                    "sell_volume": round(asks_vol, 4),
                    "funding_rate": round(funding, 4),
                    "open_interest": round(oi, 2),
                },
                "klines": klines[-120:],
                "whale_walls": {},
                "technical_levels": {},
                "trades": trades,
                "liquidity": {},
                "momentum": {},
            }

            # ── Send to Render ──
            r = requests.post(
                f"{RENDER_URL}/api/push",
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                resp_data = r.json()
                pending = resp_data.get("pending_orders", [])
                if pending:
                    for order in pending:
                        print(f"[Bridge] 📩 Orden pendiente: {order}")
                        # TODO: execute order via Binance API
            else:
                print(f"[Bridge] ⚠ Error push: {r.status_code} {r.text[:100]}")

            # Status every 30s
            if int(time.time()) % 30 == 0:
                print(f"[Bridge] 🟢 Enviando | ${price:,.2f} | RSI: {ind.get('rsi', 0):.1f} | Señal: {signal}")

        except Exception as e:
            print(f"[Bridge] ⚠ Error: {e}")

        time.sleep(POLL_INTERVAL)

    print("[Bridge] Detenido.")


if __name__ == "__main__":
    main()
