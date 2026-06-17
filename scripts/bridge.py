#!/usr/bin/env python3
"""
BB-450 Bridge — Relay de Binance a Render.

Corre en Termux (o tu PC local). Solo envía datos crudos a Render
y ejecuta las órdenes que Render le devuelve.

Uso:
    export RENDER_URL=https://z-b-450-trade.onrender.com
    python scripts/bridge.py

    O con variables de entorno en .env:
    RENDER_URL=https://z-b-450-trade.onrender.com
    BINANCE_API_KEY=...
    BINANCE_SECRET_KEY=...
    BINANCE_TESTNET=True
    SYMBOL=BTCUSDT
"""

import os
import sys
import time
import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
from dotenv import load_dotenv

load_dotenv()

RENDER_URL = os.environ.get("RENDER_URL", "https://z-b-450-trade.onrender.com").rstrip("/")
SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
TESTNET = os.environ.get("BINANCE_TESTNET", "True").lower() in ("true", "1", "yes")
API_BASE = "https://testnet.binancefuture.com" if TESTNET else "https://fapi.binance.com"

running = True


def api_get(path, params=None):
    r = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_ticker():
    return float(api_get("/fapi/v1/ticker/price", {"symbol": SYMBOL})["price"])


def fetch_klines(limit=200):
    return api_get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1m", "limit": limit})


def fetch_trades(limit=50):
    return api_get("/fapi/v1/aggTrades", {"symbol": SYMBOL, "limit": limit})


def fetch_depth(limit=20):
    return api_get("/fapi/v1/depth", {"symbol": SYMBOL, "limit": limit})


def fetch_funding():
    return float(api_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL}).get("lastFundingRate", 0)) * 100


def fetch_open_interest():
    return float(api_get("/fapi/v1/openInterest", {"symbol": SYMBOL})["openInterest"])


def execute_order(order: dict):
    """Execute a trade/close command on Binance."""
    from binance.client import Client
    from binance.exceptions import BinanceAPIException

    api_key = os.environ.get("BINANCE_API_KEY") or os.environ.get("BINANCE_REAL_API_KEY", "")
    secret = os.environ.get("BINANCE_SECRET_KEY") or os.environ.get("BINANCE_REAL_SECRET_KEY", "")
    if not api_key or not secret:
        print("[Bridge] ⚠ Sin API keys — no se puede ejecutar orden")
        return

    client = Client(api_key, secret, testnet=TESTNET)
    action = order.get("action", "").upper()

    try:
        if action == "TRADE":
            direction = order.get("direction", "")
            side = "BUY" if direction == "LONG" else "SELL"
            capital = float(order.get("capital", os.environ.get("GLOBAL_TRADE_AMOUNT", "1.0")))
            leverage = int(order.get("leverage", os.environ.get("DEFAULT_LEVERAGE", "40")))
            price = float(order.get("entry", fetch_ticker()))
            qty = max(0.001, (capital * leverage) / price)

            client.futures_change_leverage(symbol=SYMBOL, leverage=leverage)
            client.futures_create_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)

            sl = float(order.get("sl", 0))
            tp = float(order.get("tp", 0))
            if sl:
                stop_side = "SELL" if side == "BUY" else "BUY"
                client.futures_create_order(symbol=SYMBOL, side=stop_side,
                                            type="STOP_MARKET", quantity=qty,
                                            stopPrice=sl, reduceOnly=True)
            if tp:
                tp_side = "SELL" if side == "BUY" else "BUY"
                client.futures_create_order(symbol=SYMBOL, side=tp_side,
                                            type="TAKE_PROFIT_MARKET", quantity=qty,
                                            stopPrice=tp, reduceOnly=True)

            print(f"[Bridge] ✅ {direction} ejecutada: {qty:.3f} @ {SYMBOL}")

        elif action == "CLOSE":
            positions = client.futures_position_information(symbol=SYMBOL)
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                client.futures_create_order(symbol=SYMBOL, side=side,
                                            type="MARKET", quantity=abs(amt), reduceOnly=True)
                print(f"[Bridge] ✅ Posición cerrada: {side} {abs(amt)}")

    except BinanceAPIException as e:
        print(f"[Bridge] ❌ Error API Binance: {e.message}")
    except Exception as e:
        print(f"[Bridge] ❌ Error ejecutando orden: {e}")


def main():
    global running

    def handle_signal(sig, frame):
        global running
        print("\n[Bridge] Apagando...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"╔══════════════════════════════════════╗")
    print(f"║   BB-450 BRIDGE (Relay)              ║")
    print(f"║   Enviando a:                        ║")
    print(f"║   {RENDER_URL}  ║")
    print(f"║   Símbolo: {SYMBOL}                      ║")
    print(f"║   Testnet: {str(TESTNET)}                        ║")
    print(f"╚══════════════════════════════════════╝")

    last_status = 0
    last_price = 0

    while running:
        try:
            price = fetch_ticker()
            klines = fetch_klines(200)
            trades = fetch_trades(50)
            depth = fetch_depth(20)
            funding = fetch_funding()
            oi = fetch_open_interest()

            payload = {
                "price": price,
                "klines": klines,
                "agg_trades": trades,
                "depth": depth,
                "funding_rate": funding,
                "open_interest": oi,
            }

            r = requests.post(f"{RENDER_URL}/api/push", json=payload, timeout=10)
            if r.status_code == 200:
                pending = r.json().get("pending_orders", [])
                for order in pending:
                    execute_order(order)
            else:
                pass  # Render might be deploying

            now = int(time.time())
            if now - last_status >= 30:
                change = ((price - last_price) / max(last_price, 0.0001)) * 100 if last_price else 0
                arrow = "▲" if change >= 0 else "▼"
                print(f"[Bridge] 🟢 ${price:,.2f} {arrow}{abs(change):.2f}%")
                last_status = now
                last_price = price

        except requests.exceptions.ConnectionError:
            print(f"[Bridge] ⚠ Render no disponible, reintentando...")
            time.sleep(5)
        except Exception as e:
            print(f"[Bridge] ⚠ {e}")
            time.sleep(3)

        time.sleep(1)

    print("[Bridge] Detenido.")


if __name__ == "__main__":
    main()
