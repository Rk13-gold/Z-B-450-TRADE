import asyncio
import json
import logging
import random
import aiohttp
from datetime import datetime
from typing import Callable, Dict, Optional
from binance.client import Client
from config.settings import settings

log = logging.getLogger("BinanceClient")


class BinanceClient:
    def __init__(self):
        self.client = Client(
            settings.BINANCE_REAL_API_KEY,
            settings.BINANCE_REAL_SECRET_KEY,
            testnet=False
        )
        self.symbol = settings.SYMBOL

        self.kline_callback: Optional[Callable] = None
        self.depth_callback: Optional[Callable] = None
        self.trade_callback: Optional[Callable] = None
        self.liquidation_callback: Optional[Callable] = None

        self.last_kline = {}
        self.last_depth = {}

        self._ws_tasks = []
        self._running = False
        self._ws_reconnect_attempts: Dict[str, int] = {}

    async def set_leverage(self):
        try:
            await asyncio.to_thread(
                self.client.futures_change_leverage,
                symbol=self.symbol,
                leverage=settings.LEVERAGE
            )
            print(f"✅ Apalancamiento configurado: {settings.LEVERAGE}x")
        except Exception as e:
            print(f"❌ Error al configurar apalancamiento: {e}")

    async def get_historical_klines(self, interval: str = "1m", limit: int = 200):
        return await asyncio.to_thread(
            self.client.futures_klines,
            symbol=self.symbol,
            interval=interval,
            limit=limit
        )

    async def start_kline_stream(self, callback: Callable):
        self.kline_callback = callback
        self._running = True
        stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@kline_1m"
        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_kline_message))
        self._ws_tasks.append(task)

    async def start_depth_stream(self, callback: Callable):
        self.depth_callback = callback
        stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@depth20@100ms"
        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_depth_message))
        self._ws_tasks.append(task)

    async def start_trade_stream(self, callback: Callable):
        self.trade_callback = callback
        stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@aggTrade"
        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_trade_message))
        self._ws_tasks.append(task)

    async def start_liquidation_stream(self, callback: Callable):
        self.liquidation_callback = callback
        stream_url = "wss://fstream.binance.com/ws/!forceOrder@arr"
        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_liquidation_message))
        self._ws_tasks.append(task)

    async def _ws_listener(self, url: str, handler: Callable):
        initial_reconnect_delay = 1.0
        max_reconnect_delay = 60.0

        if url not in self._ws_reconnect_attempts:
            self._ws_reconnect_attempts[url] = 0

        print(f"🔌 WS iniciado: {url}")
        while self._running:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30, sock_connect=15)
                ) as session:
                    async with session.ws_connect(
                        url,
                        heartbeat=20,
                        compress=15,
                        max_msg_size=0,
                        receive_timeout=10,
                    ) as ws:
                        print(f"✅ WS conectado: {url}")
                        self._ws_reconnect_attempts[url] = 0
                        msg_count = 0
                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                msg_count += 1
                                if msg_count <= 3:
                                    print(f"📥 Datos: {data.get('e', 'unknown')}")
                                await handler(data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"⚠️ Error en WS: {url} — {ws.exception()}")
                                break
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                                print(f"🔌 WS cerrado: {url}")
                                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                attempt = self._ws_reconnect_attempts.get(url, 0)
                delay = min(initial_reconnect_delay * (2 ** attempt) + random.uniform(0, 1), max_reconnect_delay)
                print(f"⚠️ WS error ({url}): {e} — reconectando en {delay:.1f}s (intento {attempt + 1})")
                log.warning("WS error %s: %s — reconnect in %.1fs (attempt %d)", url, e, delay, attempt + 1)
                self._ws_reconnect_attempts[url] = attempt + 1
                await asyncio.sleep(delay)

    async def _handle_kline_message(self, msg: Dict):
        if msg.get('e') == 'kline':
            k = msg['k']
            kline_data = {
                'time': k['t'],
                'open': float(k['o']),
                'high': float(k['h']),
                'low': float(k['l']),
                'close': float(k['c']),
                'volume': float(k['v']),
                'closed': k['x']
            }
            self.last_kline = kline_data
            if self.kline_callback:
                await self.kline_callback(kline_data)

    async def _handle_depth_message(self, msg: Dict):
        bids = [[float(p[0]), float(p[1])] for p in msg.get('b', [])]
        asks = [[float(p[0]), float(p[1])] for p in msg.get('a', [])]

        depth_data = {
            'time': msg.get('E'),
            'bids': bids,
            'asks': asks
        }
        self.last_depth = depth_data
        if self.depth_callback:
            await self.depth_callback(depth_data)

    async def _handle_trade_message(self, msg: Dict):
        if msg.get('e') == 'aggTrade':
            trade_data = {
                'time': msg.get('T'),
                'price': float(msg.get('p')),
                'quantity': float(msg.get('q')),
                'is_buyer_maker': msg.get('m')
            }
            if self.trade_callback:
                await self.trade_callback(trade_data)

    async def _handle_liquidation_message(self, msg: Dict):
        if msg.get('e') == 'forceOrder':
            o = msg.get('o', {})
            liq_data = {
                'time': msg.get('E', 0),
                'symbol': o.get('s', ''),
                'side': o.get('S', ''),         # SELL = long liquidated, BUY = short liquidated
                'price': float(o.get('p', 0)),
                'quantity': float(o.get('q', 0)),
                'total_value': float(o.get('q', 0)) * float(o.get('p', 0)),
            }
            if self.liquidation_callback:
                await self.liquidation_callback(liq_data)

    async def place_order(self, side: str, quantity: float, order_type: str = "MARKET"):
        try:
            order = await asyncio.to_thread(
                self.client.futures_create_order,
                symbol=self.symbol,
                side=side,
                type=order_type,
                quantity=quantity
            )
            print(f"✅ Orden ejecutada: {side} {quantity} {self.symbol}")
            return order
        except Exception as e:
            print(f"❌ Error al colocar orden: {e}")
            return None

    async def place_stop_loss(self, side: str, quantity: float, stop_price: float):
        try:
            if side == "BUY":
                stop_side = "SELL"
                activation_price = str(stop_price - (stop_price * 0.002))
            else:
                stop_side = "BUY"
                activation_price = str(stop_price + (stop_price * 0.002))

            order = await asyncio.to_thread(
                self.client.futures_create_order,
                symbol=self.symbol,
                side=stop_side,
                type="STOP_MARKET",
                quantity=quantity,
                stopPrice=activation_price
            )
            print(f"🛡️ Stop Loss colocado en {activation_price}")
            return order
        except Exception as e:
            print(f"❌ Error al colocar Stop Loss: {e}")
            return None

    async def get_current_price(self):
        try:
            ticker = await asyncio.to_thread(
                self.client.futures_symbol_ticker,
                symbol=self.symbol
            )
            return float(ticker['price'])
        except Exception as e:
            print(f"❌ Error al obtener precio: {e}")
            return 0.0

    async def close_connection(self):
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        print("🔌 Conexiones WebSocket cerradas")


try:
    binance_client = BinanceClient()
    print("[✅ BINANCE] Conectado a mainnet REAL — producción activa")
except Exception as e:
    print(f"[❌ BINANCE] No se pudo conectar a Binance API: {e}")
    print("[⚠ BINANCE] Continuando sin conexión — las funciones de mercado no estarán disponibles")
    binance_client = None