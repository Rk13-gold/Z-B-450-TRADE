"""
symbol_utils.py — Binance perpetual symbol validation & caching.

Used by Telegram /symbol command to validate user input and list
available USDT perpetuals sorted by 24h volume.
"""

import asyncio
import time
import aiohttp
import logging

from config.settings import settings

log = logging.getLogger(__name__)

# ── 1-hour cache ──────────────────────────────────────────────────────────────
_perpetual_cache: list[dict] | None = None
_cache_ts: float = 0.0
_CACHE_TTL: float = 3600.0  # 1 hour

FAPI_BASE = "https://fapi.binance.com"


async def fetch_perpetual_symbols(force_refresh: bool = False) -> list[dict]:
    """Return list of all TRADING USDT perpetual contracts from Binance.

    Each entry::
        {"symbol": "BTCUSDT", "volume_24h": 12345.67, "price": 50000.0,
         "status": "TRADING", "contractType": "PERPETUAL"}

    Results are cached for 1 hour.
    """
    global _perpetual_cache, _cache_ts
    now = time.time()
    if not force_refresh and _perpetual_cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _perpetual_cache

    url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
    ticker_url = f"{FAPI_BASE}/fapi/v1/ticker/24hr"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("exchangeInfo HTTP %s", resp.status)
                    return _perpetual_cache or []
                data = await resp.json()

            # Fetch 24h tickers for volume sorting
            async with session.get(ticker_url, timeout=aiohttp.ClientTimeout(total=15)) as t_resp:
                tickers = {}
                if t_resp.status == 200:
                    for t in await t_resp.json():
                        tickers[t["symbol"]] = float(t.get("quoteVolume", 0))

        symbols = data.get("symbols", [])
        perpetuals = []
        for s in symbols:
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING":
                sym = s["symbol"]
                perpetuals.append({
                    "symbol": sym,
                    "volume_24h": tickers.get(sym, 0),
                    "price": 0.0,  # filled below
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "baseAsset": s.get("baseAsset", ""),
                    "quoteAsset": s.get("quoteAsset", ""),
                })

        perpetuals.sort(key=lambda x: x["volume_24h"], reverse=True)
        _perpetual_cache = perpetuals
        _cache_ts = now
        log.info("Fetched %d perpetual symbols from Binance", len(perpetuals))
        return perpetuals

    except Exception as e:
        log.warning("fetch_perpetual_symbols error: %s", e)
        return _perpetual_cache or []


async def validate_symbol(symbol: str) -> tuple[bool, str]:
    """Validate a symbol exists and is a TRADING perpetual contract.

    Returns (is_valid, message).
    - On success: (True, "SYMBOL")
    - On failure: (False, "reason")
    """
    symbol = symbol.upper().strip()
    if not symbol.endswith("USDT"):
        return False, f"Símbolo debe terminar en USDT (ej: BTCUSDT)"

    perpetuals = await fetch_perpetual_symbols()
    for p in perpetuals:
        if p["symbol"] == symbol:
            return True, symbol

    # Try direct API check as fallback
    try:
        url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for s in data.get("symbols", []):
                        if s["symbol"] == symbol:
                            if s.get("contractType") != "PERPETUAL":
                                return False, f"{symbol} existe pero no es PERPETUAL"
                            if s.get("status") != "TRADING":
                                return False, f"{symbol} existe pero status={s.get('status')}"
                            return True, symbol
    except Exception as e:
        log.warning("validate_symbol direct check error: %s", e)

    # Suggest similar symbols
    similar = [p["symbol"] for p in perpetuals if symbol[:3] in p["symbol"]]
    hint = ""
    if similar:
        hint = f"\n\nQuizás quisiste decir: {', '.join(similar[:5])}"

    return False, f"Símbolo '{symbol}' no encontrado como perpetuo en Binance.{hint}"


async def get_top_symbols(limit: int = 20, min_volume: float = 0) -> list[dict]:
    """Return top N perpetual symbols by 24h volume."""
    perpetuals = await fetch_perpetual_symbols()
    filtered = [p for p in perpetuals if p["volume_24h"] >= min_volume]
    return filtered[:limit]
