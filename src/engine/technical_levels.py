"""
technical_levels.py — Swing/Fibonacci/S&R/Confluence Engine for BB-450.

Computes professional algorithmic levels:
  - Swing High/Low detection (pivot points)
  - Fibonacci retracement & extension levels
  - Historical support/resistance (multi-TF)
  - Confluence zones (overlapping level clusters)
  - Market structure (HH/HL/LH/LL, BOS, CHOCH)

All methods are stateless — feed in klines, get levels out.
"""

import numpy as np
from typing import List, Dict, Tuple, Optional


# ── Constants ──────────────────────────────────────────────────────────────

FIBO_RETRACE = [0.236, 0.382, 0.5, 0.618, 0.786]
FIBO_EXTEND  = [1.272, 1.414, 1.618, 2.0, 2.618, 3.618]
SWING_LEFT   = 5
SWING_RIGHT  = 5
SR_CLUSTER_PCT = 0.15  # cluster levels within 0.15% price


# ── Swing detection ────────────────────────────────────────────────────────

def _swing_highs(highs: List[float], left: int = SWING_LEFT,
                 right: int = SWING_RIGHT) -> List[int]:
    """Return indices of swing highs (local maxima)."""
    n = len(highs)
    if n < left + right + 1:
        return []
    indices = []
    for i in range(left, n - right):
        h = highs[i]
        if all(h > highs[i - j] for j in range(1, left + 1)) and \
           all(h >= highs[i + j] for j in range(1, right + 1)):
            indices.append(i)
    return indices


def _swing_lows(lows: List[float], left: int = SWING_LEFT,
                right: int = SWING_RIGHT) -> List[int]:
    """Return indices of swing lows (local minima)."""
    n = len(lows)
    if n < left + right + 1:
        return []
    indices = []
    for i in range(left, n - right):
        l = lows[i]
        if all(l < lows[i - j] for j in range(1, left + 1)) and \
           all(l <= lows[i + j] for j in range(1, right + 1)):
            indices.append(i)
    return indices


# ── Fibonacci ──────────────────────────────────────────────────────────────

def _fib_retracement_levels(high: float, low: float) -> List[Dict]:
    """Compute Fibonacci retracement levels from a swing high->low or low->high."""
    diff = high - low
    levels = []
    for ratio in FIBO_RETRACE:
        price = high - ratio * diff
        levels.append({
            "ratio": ratio,
            "price": round(price, 2),
            "type": "retracement",
        })
    return levels


def _fib_extension_levels(high: float, low: float,
                          direction: str) -> List[Dict]:
    """Compute Fibonacci extension levels beyond the swing."""
    diff = high - low
    levels = []
    for ratio in FIBO_EXTEND:
        if direction.upper() == "UP":
            price = high + ratio * diff
        else:
            price = low - ratio * diff
        levels.append({
            "ratio": ratio,
            "price": round(price, 2),
            "type": "extension",
        })
    return levels


# ── Historical S/R ─────────────────────────────────────────────────────────

def _round_number_levels(current_price: float,
                         radius_pct: float = 10.0) -> List[float]:
    """Psychological round numbers ±radius_pct around current price."""
    r = current_price * radius_pct / 100.0
    low = current_price - r
    high = current_price + r
    step = 500 if current_price > 10000 else (100 if current_price > 1000 else 10)
    levels = []
    n = round(low / step) * step
    while n <= high:
        if n % (step * 10) == 0 or abs(n % (step * 10) - step * 5) < 1:
            levels.append(round(n, 2))
        n += step
    return levels


def _multi_tf_highs_lows(klines_1m: list, klines_5m: list,
                         klines_15m: list) -> Dict[str, List[float]]:
    """Extract key highs/lows from multiple timeframe klines."""
    result = {"highs": [], "lows": []}
    for klist in [klines_1m, klines_5m, klines_15m]:
        if not klist:
            continue
        result["highs"].append(max(float(k[2]) for k in klist))
        result["lows"].append(min(float(k[3]) for k in klist))
    return result


def _find_rejection_levels(klines: list, min_touches: int = 2,
                           tolerance_pct: float = 0.1) -> List[Dict]:
    """Detect price levels where price reversed multiple times."""
    if len(klines) < 20:
        return []
    touches: Dict[float, int] = {}
    for k in klines:
        h, l = float(k[2]), float(k[3])
        for price in touches:
            if abs(h - price) / max(price, 1) * 100 < tolerance_pct:
                touches[price] += 1
            elif abs(l - price) / max(price, 1) * 100 < tolerance_pct:
                touches[price] += 1
        touches[round(h, 2)] = touches.get(round(h, 2), 0) + 1
        touches[round(l, 2)] = touches.get(round(l, 2), 0) + 1
    return [
        {"price": p, "touches": t}
        for p, t in sorted(touches.items(), key=lambda x: -x[1])
        if t >= min_touches
    ]


def _cluster_levels(levels: List[float],
                    pct_threshold: float = SR_CLUSTER_PCT) -> List[float]:
    """Cluster nearby price levels into representative zones."""
    if not levels:
        return []
    sorted_l = sorted(levels)
    clusters = []
    current = [sorted_l[0]]
    for price in sorted_l[1:]:
        avg = sum(current) / len(current)
        if abs(price - avg) / max(avg, 1) * 100 < pct_threshold:
            current.append(price)
        else:
            clusters.append(round(sum(current) / len(current), 2))
            current = [price]
    if current:
        clusters.append(round(sum(current) / len(current), 2))
    return clusters


# ── Market structure ───────────────────────────────────────────────────────

def _market_structure(highs: List[float], lows: List[float],
                      price: float) -> Dict:
    """Determine HH/HL/LH/LL market structure."""
    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "NEUTRAL", "last_hh": None, "last_hl": None,
                "last_lh": None, "last_ll": None, "bos": False}
    last_h = highs[-1] if highs else price
    prev_h = highs[-2] if len(highs) >= 2 else last_h
    last_l = lows[-1] if lows else price
    prev_l = lows[-2] if len(lows) >= 2 else last_l

    higher_high = last_h > prev_h
    higher_low = last_l > prev_l
    lower_high = last_h < prev_h
    lower_low = last_l < prev_l

    bos = False
    if higher_high and higher_low:
        trend = "UPTREND"
    elif lower_high and lower_low:
        trend = "DOWNTREND"
    else:
        trend = "RANGING"

    return {
        "trend": trend,
        "last_hh": last_h if higher_high else (prev_h if lower_high else None),
        "last_hl": last_l if higher_low else (prev_l if lower_low else None),
        "last_lh": last_h if lower_high else None,
        "last_ll": last_l if lower_low else None,
        "bos": bos,
    }


# ── Confluence ─────────────────────────────────────────────────────────────

def _find_confluence_zones(
    fib_retrace: List[Dict],
    sr_levels: List[float],
    round_nums: List[float],
    vwap: float,
    ema20: float,
    ema50: float,
    dpoc: float = 0,
    price: float = 0,
    tolerance_pct: float = 0.3,
) -> List[Dict]:
    """Find zones where multiple level types converge."""
    all_levels: List[Tuple[float, str, float]] = []
    for fb in fib_retrace:
        all_levels.append((fb["price"], f"fib_{fb['ratio']}", 0.8))
    for sr in sr_levels:
        all_levels.append((sr, "sr_hist", 0.7))
    for rn in round_nums:
        if price and abs(rn - price) / max(price, 1) * 100 > 5:
            continue
        all_levels.append((rn, "round", 0.5))
    all_levels.append((vwap, "vwap", 0.6))
    all_levels.append((ema20, "ema20", 0.4))
    all_levels.append((ema50, "ema50", 0.4))
    if dpoc > 0:
        all_levels.append((dpoc, "dpoc", 0.7))

    zones = []
    used = set()
    for i, (p1, t1, w1) in enumerate(all_levels):
        if i in used:
            continue
        cluster = [(p1, t1, w1)]
        used.add(i)
        for j, (p2, t2, w2) in enumerate(all_levels):
            if j in used:
                continue
            if abs(p2 - p1) / max(p1, 1) * 100 < tolerance_pct:
                cluster.append((p2, t2, w2))
                used.add(j)
        if len(cluster) >= 2:
            avg_price = sum(c[0] for c in cluster) / len(cluster)
            unique_types = list(set(c[1] for c in cluster))
            score = sum(c[2] for c in cluster) / max(len(cluster), 1)
            zones.append({
                "price": round(avg_price, 2),
                "types": unique_types,
                "count": len(cluster),
                "score": round(score, 2),
            })

    zones.sort(key=lambda z: -z["score"])
    return zones


# ── Main engine ────────────────────────────────────────────────────────────

class TechnicalLevelsEngine:
    """Computes professional technical levels from kline data.

    Usage:
        engine = TechnicalLevelsEngine()
        result = engine.compute(
            klines_1m=..., klines_5m=..., klines_15m=...,
            price=vwap=, ema20=, ema50=, dpoc=,
        )
        # result dict is ready for market_state["technical_levels"]
    """

    def compute(
        self,
        klines_1m: list,
        price: float = 0,
        vwap: float = 0,
        ema20: float = 0,
        ema50: float = 0,
        dpoc: float = 0,
        klines_5m: Optional[list] = None,
        klines_15m: Optional[list] = None,
    ) -> Dict:
        """Full computation pipeline. Returns a dict for market_state."""
        klines_1m = klines_1m or []
        klines_5m = klines_5m or klines_1m
        klines_15m = klines_15m or klines_5m
        price = price or 0

        closes = [float(k[4]) for k in klines_1m]
        highs  = [float(k[2]) for k in klines_1m]
        lows   = [float(k[3]) for k in klines_1m]

        # 1. Swing points
        sh_idx = _swing_highs(highs)
        sl_idx = _swing_lows(lows)
        swing_highs = [{"price": round(highs[i], 2), "index": i} for i in sh_idx]
        swing_lows  = [{"price": round(lows[i], 2), "index": i} for i in sl_idx]

        # 2. Best swing for Fibonacci (highest high / lowest low in recent window)
        recent_high = max(highs[-30:]) if highs else price
        recent_low  = min(lows[-30:]) if lows else price

        # Determine if current bias is up or down
        if price >= (recent_high + recent_low) / 2:
            fib_high = recent_high
            fib_low = recent_low
            fib_direction = "UP"
        else:
            fib_high = recent_high
            fib_low = recent_low
            fib_direction = "DOWN"

        # 3. Fibonacci levels
        fib_retrace = _fib_retracement_levels(fib_high, fib_low)
        fib_extend = _fib_extension_levels(fib_high, fib_low, fib_direction)

        # 4. Historical S/R
        mtf = _multi_tf_highs_lows(klines_1m, klines_5m, klines_15m)
        rejection = _find_rejection_levels(klines_1m)
        rejection_prices = [r["price"] for r in rejection[:10]]
        round_nums = _round_number_levels(price) if price > 0 else []

        all_sr_raw = mtf["highs"] + mtf["lows"] + rejection_prices + round_nums
        sr_levels = _cluster_levels(all_sr_raw)

        # Classify S/R
        supports = [p for p in sr_levels if p < price]
        resistances = [p for p in sr_levels if p > price]
        supports.sort(reverse=True)
        resistances.sort()

        # 5. Market structure
        mkt_struct = _market_structure(
            [s["price"] for s in swing_highs],
            [s["price"] for s in swing_lows],
            price,
        )

        # 6. Confluence zones
        sr_for_confluence = supports[:5] + resistances[:5]
        confluence = _find_confluence_zones(
            fib_retrace, sr_for_confluence, round_nums,
            vwap, ema20, ema50, dpoc, price,
        )

        # 7. Nearest key levels
        nearest_support_price = None
        nearest_resistance_price = None
        for z in confluence:
            if z["price"] < price:
                if nearest_support_price is None or z["price"] > nearest_support_price:
                    nearest_support_price = z["price"]
            elif z["price"] > price:
                if nearest_resistance_price is None or z["price"] < nearest_resistance_price:
                    nearest_resistance_price = z["price"]

        # Also check simple fib levels for nearest
        for fb in fib_retrace:
            p = fb["price"]
            if p < price:
                if nearest_support_price is None or p > nearest_support_price:
                    nearest_support_price = p
            elif p > price:
                if nearest_resistance_price is None or p < nearest_resistance_price:
                    nearest_resistance_price = p

        for s in supports:
            if nearest_support_price is None or s > nearest_support_price:
                nearest_support_price = s
        for r in resistances:
            if nearest_resistance_price is None or r < nearest_resistance_price:
                nearest_resistance_price = r

        nearest_support = None
        nearest_resistance = None
        if nearest_support_price is not None:
            nearest_support = {"price": nearest_support_price, "types": ["sr_hist"], "score": 0.5, "count": 1}
        if nearest_resistance_price is not None:
            nearest_resistance = {"price": nearest_resistance_price, "types": ["sr_hist"], "score": 0.5, "count": 1}

        return {
            "swing_highs": swing_highs[-10:],
            "swing_lows": swing_lows[-10:],
            "fib_retracement": fib_retrace,
            "fib_extension": fib_extend,
            "fib_high": round(fib_high, 2),
            "fib_low": round(fib_low, 2),
            "fib_direction": fib_direction,
            "supports": supports[:6],
            "resistances": resistances[:6],
            "round_numbers": round_nums,
            "market_structure": mkt_struct,
            "confluence_zones": confluence[:6],
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
        }


# ── Format helpers ─────────────────────────────────────────────────────────

def format_levels_for_prompt(tech: dict, price: float) -> str:
    """Format technical levels into a compact string for AI prompts."""
    if not tech:
        return ""

    lines = ["📐 NIVELES TÉCNICOS ACTIVOS:"]

    fib_r = tech.get("fib_retracement", [])
    if fib_r:
        parts = [f"{fb['ratio']}→${fb['price']}" for fb in fib_r[:5]]
        lines.append(f"  Fibonacci retracement: {', '.join(parts)}")

    fib_e = tech.get("fib_extension", [])
    if fib_e:
        parts = [f"{fb['ratio']}→${fb['price']}" for fb in fib_e[:3]]
        lines.append(f"  Fibonacci extension: {', '.join(parts)}")

    supports = tech.get("supports", [])
    resistances = tech.get("resistances", [])
    if supports:
        lines.append(f"  Soportes: {' | '.join(f'${p}' for p in supports[:4])}")
    if resistances:
        lines.append(f"  Resistencias: {' | '.join(f'${p}' for p in resistances[:4])}")

    cz = tech.get("confluence_zones", [])
    if cz:
        zones_str = []
        for z in cz[:3]:
            pct = abs(z["price"] - price) / max(price, 1) * 100 if price > 0 else 0
            zones_str.append(
                f"${z['price']} ({'/'.join(z['types'][:3])}, "
                f"score={z['score']}, {pct:.2f}%)"
            )
        lines.append(f"  Zonas de confluencia: {' | '.join(zones_str)}")

    ms = tech.get("market_structure", {})
    if ms:
        lines.append(f"  Estructura: {ms.get('trend', 'NEUTRAL')}")

    ns = tech.get("nearest_support")
    nr = tech.get("nearest_resistance")
    if ns and price > 0:
        pct = abs(ns["price"] - price) / max(price, 1) * 100
        lines.append(
            f"  Soporte más cercano: ${ns['price']} "
            f"({'/'.join(ns.get('types', [''])[:2])}, −{pct:.2f}%)")
    if nr and price > 0:
        pct = abs(nr["price"] - price) / max(price, 1) * 100
        lines.append(
            f"  Resistencia más cercana: ${nr['price']} "
            f"({'/'.join(nr.get('types', [''])[:2])}, +{pct:.2f}%)")

    return "\n".join(lines)


def format_levels_for_telegram(tech: dict, price: float,
                                symbol: str = None) -> str:
    if symbol is None:
        from config.settings import settings
        symbol = settings.get_symbol()
    """Format levels into a rich Telegram HTML message."""
    if not tech or not tech.get("fib_retracement"):
        return "⚠️ No hay datos de niveles técnicos disponibles."

    lines = [f"📊 <b>NIVELES TÉCNICOS — {symbol}</b>\n"]

    # Market structure
    ms = tech.get("market_structure", {})
    trend = ms.get("trend", "NEUTRAL")
    emoji = "📈" if trend == "UPTREND" else "📉" if trend == "DOWNTREND" else "📊"
    lines.append(f"{emoji} <b>Estructura:</b> {trend}")

    # Fibonacci retracement
    fib_r = tech.get("fib_retracement", [])
    if fib_r:
        lines.append(f"\n🔻 <b>Fibonacci Retracement</b>")
        for fb in fib_r:
            arrow = "⬆" if fb["ratio"] in (0.236, 0.382) else "⬇" if fb["ratio"] in (0.786,) else "➖"
            lines.append(f"  {arrow} {fb['ratio']} → <code>${fb['price']}</code>")
        lines.append(
            f"  📐 Swing: ${tech.get('fib_low', 0)} → ${tech.get('fib_high', 0)} "
            f"({tech.get('fib_direction', '')})")

    # Fibonacci extension
    fib_e = tech.get("fib_extension", [])
    if fib_e:
        lines.append(f"\n🚀 <b>Fibonacci Extension</b>")
        for fb in fib_e[:3]:
            lines.append(f"  ⚡ {fb['ratio']} → <code>${fb['price']}</code>")

    # S/R
    supports = tech.get("supports", [])
    resistances = tech.get("resistances", [])
    if supports or resistances:
        lines.append(f"\n🛡️ <b>Soportes / Resistencias</b>")
        if resistances:
            lines.append(f"  🔺 Resistencias: {' | '.join(f'<code>${p}</code>' for p in resistances[:4])}")
        if supports:
            lines.append(f"  🔻 Soportes: {' | '.join(f'<code>${p}</code>' for p in supports[:4])}")

    # Confluence
    cz = tech.get("confluence_zones", [])
    if cz:
        lines.append(f"\n🎯 <b>Zonas de Confluencia</b>")
        for z in cz[:3]:
            lines.append(
                f"  • <code>${z['price']}</code> — "
                f"{'/'.join(z['types'][:3])} "
                f"(score: {z['score']})")

    # Nearest levels
    ns = tech.get("nearest_support")
    nr = tech.get("nearest_resistance")
    if ns or nr:
        lines.append(f"\n📍 <b>Distancia desde ${price:,.0f}</b>")
        if ns:
            pct = abs(ns["price"] - price) / max(price, 1) * 100
            lines.append(f"  🔻 Soporte: <code>${ns['price']}</code> (−{pct:.2f}%)")
        if nr:
            pct = abs(nr["price"] - price) / max(price, 1) * 100
            lines.append(f"  🔺 Resistencia: <code>${nr['price']}</code> (+{pct:.2f}%)")

    return "\n".join(lines)
