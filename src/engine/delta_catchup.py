"""
Delta-CatchUp — Retroactive kline sync after downtime.

When AUTOMATIC_ON is activated after hours/days offline, this module:
1. Reads the last persisted kline close_time from system_config
2. Calculates the gap in minutes
3. Downloads missing klines in paginated batches of up to 1000
4. Feeds them into TradingStrategy + indicator cache
5. Updates the persisted marker for next restart
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config.settings import settings

log = logging.getLogger("DeltaCatchUp")

MAX_BATCH = 1000
MAX_BACKFILL_HOURS = 48
DB_PATH = "bb450_trades.db"

# Raw kline indices
KLINE_OT = 0
KLINE_O = 1
KLINE_H = 2
KLINE_L = 3
KLINE_C = 4
KLINE_V = 5
KLINE_CT = 6


def _get_last_kline_time(db_path: str = DB_PATH) -> int:
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT value FROM system_config WHERE key = 'last_kline_close_time'"
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return int(row[0])
    except Exception as e:
        log.warning("Could not read last_kline_close_time: %s", e)
    return 0


def _set_last_kline_time(ts_ms: int, db_path: str = DB_PATH) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
            ("last_kline_close_time", str(ts_ms)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Could not write last_kline_close_time: %s", e)


def _kline_to_dict(raw: list) -> dict:
    return {
        "time": raw[KLINE_OT],
        "open": float(raw[KLINE_O]),
        "high": float(raw[KLINE_H]),
        "low": float(raw[KLINE_L]),
        "close": float(raw[KLINE_C]),
        "volume": float(raw[KLINE_V]),
    }


def compute_gap(last_kline_time_ms: int) -> Tuple[int, int, float]:
    """Returns (gap_minutes, from_ms, gap_hours) or (0, 0, 0.0)."""
    if last_kline_time_ms <= 0:
        return 0, 0, 0.0

    now_ms = int(time.time() * 1000)
    gap_ms = now_ms - last_kline_time_ms
    gap_minutes = int(gap_ms / 60_000)
    gap_hours = gap_ms / 3_600_000

    if gap_minutes < 2:
        return 0, 0, 0.0

    return gap_minutes, last_kline_time_ms, gap_hours


def download_klines(
    client: Client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = MAX_BATCH,
) -> List[list]:
    try:
        return client.futures_klines(
            symbol=symbol,
            interval=interval,
            startTime=start_ms,
            endTime=end_ms,
            limit=limit,
        )
    except (BinanceAPIException, BinanceRequestException) as e:
        log.error("Binance API error during kline download: %s", e)
        return []
    except Exception as e:
        log.error("Unexpected error during kline download: %s", e)
        return []


def run_catch_up(
    client: Client,
    strategy,
    data_engine=None,
    symbol: str = None,
    interval: str = "1m",
    max_hours: int = MAX_BACKFILL_HOURS,
) -> int:
    """
    Detect gap → download in paginated batches → feed → persist.

    Parameters
    ----------
    client : Client
        Binance Futures client (shared singleton).
    strategy : TradingStrategy
        In-memory strategy whose add_kline() builds indicator history.
    data_engine : AsyncDataEngine or None
        If provided, new klines are also injected into _klines_cache.

    Returns
    -------
    int
        Number of klines loaded. 0 if no gap or error.
    """
    symbol = symbol or settings.SYMBOL
    last_kline_time = _get_last_kline_time()

    gap_minutes, from_ms, gap_hours = compute_gap(last_kline_time)
    if gap_minutes <= 0:
        log.info("[Δ CatchUp] No gap — skipping")
        return 0

    # Cap maximum backfill
    max_backfill_ms = max_hours * 3_600_000
    now_ms = int(time.time() * 1000)
    capped_from = max(from_ms, now_ms - max_backfill_ms)

    if capped_from != from_ms:
        log.info(
            "[Δ CatchUp] Gap of %.1f h exceeds max (%d h), capping",
            gap_hours, max_hours,
        )

    if capped_from >= now_ms:
        return 0

    log.info(
        "[Δ CatchUp] Gap: %.0f min (%.1f h). "
        "Downloading %s → %s ...",
        gap_minutes, gap_hours,
        datetime.fromtimestamp(capped_from / 1000, tz=timezone.utc).strftime("%H:%M"),
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%H:%M"),
    )

    total_loaded = 0
    current_start = capped_from
    last_close_time = 0

    while current_start < now_ms:
        batch = download_klines(
            client=client,
            symbol=symbol,
            interval=interval,
            start_ms=current_start,
            end_ms=now_ms,
        )
        if not batch:
            log.warning("[Δ CatchUp] Empty batch at %d — stopping", current_start)
            break

        for raw_kline in batch:
            kline_dict = _kline_to_dict(raw_kline)
            strategy.add_kline(kline_dict)
            last_close_time = raw_kline[KLINE_CT]

        total_loaded += len(batch)

        # Advance the search cursor past this batch
        next_start = batch[-1][KLINE_CT] + 1
        if next_start <= current_start:
            log.warning("[Δ CatchUp] Non-advancing cursor — breaking")
            break
        current_start = next_start

        log.info(
            "[Δ CatchUp] +%d klines (total: %d, up to %s)",
            len(batch), total_loaded,
            datetime.fromtimestamp(
                batch[-1][KLINE_CT] / 1000, tz=timezone.utc
            ).strftime("%H:%M"),
        )

    if last_close_time > 0:
        _set_last_kline_time(last_close_time)

    log.info(
        "[Δ CatchUp] Done. %d klines ingested. Strategy has %d klines.",
        total_loaded, len(strategy.klines),
    )

    # Also persist the very first time if this is a fresh start
    # so next reboot doesn't re-download everything
    if last_kline_time == 0 and last_close_time > 0:
        _set_last_kline_time(last_close_time)

    return total_loaded
