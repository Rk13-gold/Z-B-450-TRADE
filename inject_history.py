#!/usr/bin/env python3
"""
inject_history.py — Massive knowledge injector from Binance Futures history.

Fetches 2 000 1m candles of BTCUSDT Perpetual, computes technical indicators,
filters volatility anomalies (volume spike / wild range), and writes each
anomaly as a structured .md lesson into CONCMT/.

Usage
-----
    python inject_history.py

Output
------
    CONCMT/lesson_hist_{timestamp}.md  — one file per detected anomaly
    Console:  "✅ Inyectados {N} bloques de conocimiento histórico"
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("inject_history")

# ── Config ─────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
LIMIT = 1500          # Binance Futures max per request
VOLUME_MULTIPLIER = 3.5
RANGE_PERCENTILE = 95
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2

OUTPUT_DIR = Path(__file__).parent / "CONCMT"

# Kline field indices (Binance futures_klines format)
K_OPEN_TIME = 0
K_OPEN = 1
K_HIGH = 2
K_LOW = 3
K_CLOSE = 4
K_VOLUME = 5
K_CLOSE_TIME = 6


# ── 1. FETCH ───────────────────────────────────────────────────────────

def fetch_klines() -> list:
    """Download 2 000 1m klines from Binance Futures REST API.

    Handles rate limits with a retry delay. Returns raw kline list.
    """
    try:
        from binance.client import Client
        from config.settings import settings
    except ImportError:
        log.error(
            "No se pudo importar la configuración del proyecto. "
            "Ejecuta este script desde la raíz del proyecto BB-450."
        )
        sys.exit(1)

    client = Client(
        settings.BINANCE_REAL_API_KEY,
        settings.BINANCE_REAL_SECRET_KEY,
        testnet=False,
    )

    log.info("Descargando %d velas %s de %s Futures...", LIMIT, INTERVAL, SYMBOL)

    for attempt in range(3):
        try:
            klines = client.futures_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=LIMIT,
            )
            if not klines:
                log.warning("Binance devolvió lista vacía (intento %d/3)", attempt + 1)
                time.sleep(2)
                continue
            log.info("Descargadas %d velas correctamente.", len(klines))
            return klines
        except Exception as e:
            log.warning("Error en intento %d/3: %s", attempt + 1, e)
            time.sleep(3 * (attempt + 1))

    log.error("No se pudieron descargar velas después de 3 intentos.")
    sys.exit(1)


# ── 2. PARSE + INDICATORS ─────────────────────────────────────────────

def build_dataframe(raw: list) -> pd.DataFrame:
    """Convert raw klines → pandas DataFrame with computed indicators.

    Indicators (all vectorised, no external 'ta' dependency):
        - EMA 20, EMA 50
        - RSI 14
        - Bollinger Bands (20,2)
        - Avg Volume (20)
    """
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_vol",
        "taker_buy_quote", "ignore",
    ])

    # Numeric cast
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Timestamps
    df["ts"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df["range"] = df["high"] - df["low"]
    df["range_pct"] = df["range"] / df["open"] * 100

    # ── EMA 20 / 50 ────────────────────────────────────────────────────
    df["ema_20"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # ── RSI 14 ─────────────────────────────────────────────────────────
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(span=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ── Bollinger Bands (20, 2) ────────────────────────────────────────
    df["bb_middle"] = df["close"].rolling(BB_PERIOD).mean()
    bb_std = df["close"].rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = df["bb_middle"] + BB_STD * bb_std
    df["bb_lower"] = df["bb_middle"] - BB_STD * bb_std
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]
    df["bb_position"] = (
        (df["close"] - df["bb_lower"]) / df["bb_width"].replace(0, np.nan) * 100
    )

    # ── Avg Volume (20) ────────────────────────────────────────────────
    df["avg_volume"] = df["volume"].rolling(20).mean()
    df["vol_multiplier"] = df["volume"] / df["avg_volume"].replace(0, np.nan)

    return df


# ── 3. FILTER ──────────────────────────────────────────────────────────

def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Return rows where volatility anomaly conditions are met.

    Conditions (OR):
        A. volume > 3.5 × average volume of last 20 candles
        B. price range (high - low) > 95th percentile of all ranges
    """
    range_threshold = df["range"].quantile(RANGE_PERCENTILE / 100)

    mask_vol = df["vol_multiplier"] > VOLUME_MULTIPLIER
    mask_range = df["range"] > range_threshold

    anomalies = df[mask_vol | mask_range].copy()
    anomalies = anomalies.dropna(subset=["rsi", "ema_20", "ema_50"])

    log.info(
        "Filtro: %d anomalías detectadas "
        "(vol>%.1fx: %d | range>P%d: %d)",
        len(anomalies),
        VOLUME_MULTIPLIER,
        mask_vol.sum(),
        RANGE_PERCENTILE,
        mask_range.sum(),
    )
    return anomalies


# ── 4. WRITE ───────────────────────────────────────────────────────────

def write_lesson(row: pd.Series, idx: int) -> str:
    """Write a single anomaly as a structured .md lesson file.

    Returns the file path created, or empty string on failure.
    """
    ts: pd.Timestamp = row["ts"]
    filename = f"lesson_hist_{ts.strftime('%Y%m%d_%H%M%S')}.md"
    path = OUTPUT_DIR / filename

    price = row["close"]
    rsi = row["rsi"]
    ema_20 = row["ema_20"]
    ema_50 = row["ema_50"]
    bb_pos = row["bb_position"]
    vol_mult = row["vol_multiplier"]
    candle_range = row["range"]
    candle_range_pct = row["range_pct"]

    # Distance to bands
    if price > row["bb_middle"]:
        band_dist = (price - row["bb_middle"]) / (row["bb_upper"] - row["bb_middle"] + 1e-10)
        band_side = "superior"
    else:
        band_dist = (row["bb_middle"] - price) / (row["bb_middle"] - row["bb_lower"] + 1e-10)
        band_side = "inferior"

    content = (
        f"# Lección: {ts.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"## Contexto del Mercado\n"
        f"- Precio: ${price:,.2f}\n"
        f"- RSI (14): {rsi:.1f}\n"
        f"- EMA 20: ${ema_20:,.2f} | EMA 50: ${ema_50:,.2f}\n"
        f"- Distancia a Banda {band_side}: {band_dist*100:.1f}%\n"
        f"- Rango de Vela: ${candle_range:.2f} ({candle_range_pct:.2f}%)\n"
        f"- Multiplicador de Volumen: {vol_mult:.1f}x\n\n"
        f"## Observación de Aprendizaje\n"
        f"Inyección de volatilidad histórica detectada. "
        f"Expansión de {candle_range_pct:.2f}% con volumen "
        f"{vol_mult:.1f}x el promedio. "
        f"El cerebro debe analizar cómo reaccionó el precio "
        f"en esta zona de desequilibrio institucional y "
        f"compararlo con patrones FAILED/SUCCESS de memoria episódica.\n\n"
        f"## Tags\n"
        f"`#auto_learn` `#historico_binance` `#inyeccion_volatilidad`\n"
    )

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.debug("  ✔ %s", filename)
        return str(path)
    except OSError as e:
        log.warning("Error escribiendo %s: %s", filename, e)
        return ""


def main():
    print("=" * 60)
    print("  BB-450 — INYECTOR MASIVO DE CONOCIMIENTO HISTÓRICO")
    print("=" * 60)

    # Ensure output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Directorio de salida: %s", OUTPUT_DIR.resolve())

    # 1. Fetch
    raw = fetch_klines()

    # 2. Parse + compute indicators
    log.info("Calculando indicadores técnicos...")
    df = build_dataframe(raw)
    log.info(
        "DataFrame: %d filas | rango precio: $%.2f – $%.2f",
        len(df), df["low"].min(), df["high"].max(),
    )

    # 3. Filter anomalies
    anomalies = detect_anomalies(df)

    if anomalies.empty:
        log.info("No se detectaron anomalías en este bloque histórico.")
        print("\n⚠ No se inyectaron bloques de conocimiento.")
        return

    # 4. Write lessons
    log.info("Escribiendo %d lecciones...", len(anomalies))
    count = 0
    for idx, (_, row) in enumerate(anomalies.iterrows()):
        path = write_lesson(row, idx)
        if path:
            count += 1

    print(f"\n✅ Inyectados {count} bloques de conocimiento histórico "
          f"en {OUTPUT_DIR}/")
    log.info("Proceso completado.")


if __name__ == "__main__":
    main()
