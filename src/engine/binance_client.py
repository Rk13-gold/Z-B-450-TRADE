import asyncio
import json
import aiohttp
from datetime import datetime
from typing import Callable, Dict, Optional
from binance.client import Client
from config.settings import settings


class BinanceClient:
    def __init__(self):
        self.client = Client(
            settings.BINANCE_API_KEY,
            settings.BINANCE_SECRET_KEY,
            testnet=settings.BINANCE_TESTNET
        )
        self.symbol = settings.SYMBOL

        self.kline_callback: Optional[Callable] = None
        self.depth_callback: Optional[Callable] = None
        self.trade_callback: Optional[Callable] = None

        self.last_kline = {}
        self.last_depth = {}

        self._ws_tasks = []
        self._running = False

    async def set_leverage(self):
        try:
            self.client.futures_change_leverage(
                symbol=self.symbol,
                leverage=settings.LEVERAGE
            )
            print(f"✅ Apalancamiento configurado: {settings.LEVERAGE}x")
        except Exception as e:
            print(f"❌ Error al configurar apalancamiento: {e}")

    async def get_historical_klines(self, interval: str = "1m", limit: int = 200):
        klines = self.client.futures_klines(
            symbol=self.symbol,
            interval=interval,
            limit=limit
        )
        return klines

    async def start_kline_stream(self, callback: Callable):
        self.kline_callback = callback
        self._running = True

        if settings.BINANCE_TESTNET:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@kline_1m"
        else:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@kline_1m"

        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_kline_message))
        self._ws_tasks.append(task)

    async def start_depth_stream(self, callback: Callable):
        self.depth_callback = callback

        if settings.BINANCE_TESTNET:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@depth20@100ms"
        else:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@depth20@100ms"

        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_depth_message))
        self._ws_tasks.append(task)

    async def start_trade_stream(self, callback: Callable):
        self.trade_callback = callback

        if settings.BINANCE_TESTNET:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@aggTrade"
        else:
            stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@aggTrade"

        task = asyncio.create_task(self._ws_listener(stream_url, self._handle_trade_message))
        self._ws_tasks.append(task)

    async def _ws_listener(self, url: str, handler: Callable):
        print(f"🔌 WS iniciado: {url}")
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=30) as ws:
                        print(f"✅ WS conectado")
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
                                print("⚠️ Error en WS")
                                break
            except Exception as e:
                print(f"⚠️ WS error: {e}")
                await asyncio.sleep(5)

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

    async def place_order(self, side: str, quantity: float, order_type: str = "MARKET"):
        try:
            order = self.client.futures_create_order(
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

            order = self.client.futures_create_order(
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

    def get_current_price(self):
        try:
            ticker = self.client.futures_symbol_ticker(symbol=self.symbol)
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
    print(f"[✅ BINANCE] Conectado a {'testnet' if settings.BINANCE_TESTNET else 'mainnet'}")
except Exception as e:
    print(f"[❌ BINANCE] No se pudo conectar a Binance API: {e}")
    print("[⚠ BINANCE] Continuando sin conexión — las funciones de mercado no estarán disponibles")
    binance_client = None