import asyncio
import signal
import sys
import argparse
from datetime import datetime
from rich.console import Console

from config.settings import settings
from src.engine.binance_client import binance_client
from src.engine.order_flow import order_flow_engine
from src.engine.strategy import trading_strategy
from src.engine.ai_analyst import ai_analyst
from src.database.supabase_manager import supabase_manager


console = Console()


class TradingBot:
    def __init__(self, mock_mode: bool = False):
        self.running = False
        self.mock_mode = mock_mode
        self.position = None
        self.entry_time = None
        self.entry_price = 0.0
        self.daily_pnl = 0.0
        self.price = 100000.0
        self.last_kline = None

    async def initialize(self):
        console.print("🔄 [cyan]Inicializando BB-450 Bot de Scalping...[/cyan]")

        if self.mock_mode:
            console.print("📋 [yellow]Modo MOCK - Sin conexión real a Binance[/yellow]")
            for i in range(200):
                kline = {
                    'time': datetime.now().timestamp() * 1000,
                    'open': self.price,
                    'high': self.price + 100,
                    'low': self.price - 100,
                    'close': self.price + (i % 10 - 5) * 10,
                    'volume': 1000
                }
                trading_strategy.add_kline(kline)
        else:
            try:
                await binance_client.set_leverage()
                historical_klines = await binance_client.get_historical_klines("1m", 200)
                for k in historical_klines:
                    kline = {
                        'time': k[0],
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    }
                    trading_strategy.add_kline(kline)
            except Exception as e:
                console.print(f"⚠️ [yellow]Error conectando a Binance: {e}[/yellow]")
                console.print("📋 [yellow]Cambiando a modo MOCK[/yellow]")
                self.mock_mode = True
                await self._init_mock_data()

        supabase_manager.connect()
        console.print("✅ [green]Sistema inicializado[/green]")

    async def _init_mock_data(self):
        for i in range(200):
            kline = {
                'time': datetime.now().timestamp() * 1000,
                'open': self.price,
                'high': self.price + 100,
                'low': self.price - 100,
                'close': self.price + (i % 10 - 5) * 10,
                'volume': 1000
            }
            trading_strategy.add_kline(kline)

    async def start_websockets(self):
        if self.mock_mode:
            console.print("📡 [cyan]Modo MOCK - Generando datos simulados[/cyan]")
            asyncio.create_task(self._mock_data_generator())
        else:
            await binance_client.start_kline_stream(self.handle_kline)
            await binance_client.start_depth_stream(self.handle_depth)
            await binance_client.start_trade_stream(self.handle_trade)

    async def _mock_data_generator(self):
        counter = 0
        while self.running:
            await asyncio.sleep(1)
            counter += 1
            self.price += (counter % 3 - 1) * 50

            kline = {
                'time': datetime.now().timestamp() * 1000,
                'open': self.price - 20,
                'high': self.price + 30,
                'low': self.price - 40,
                'close': self.price,
                'volume': 100 + counter % 50
            }
            await self.handle_kline(kline)

            if counter % 3 == 0:
                trade = {
                    'time': datetime.now().timestamp(),
                    'price': self.price,
                    'quantity': 0.001,
                    'is_buyer_maker': counter % 2 == 0
                }
                await self.handle_trade(trade)

    async def handle_kline(self, kline: dict):
        self.price = kline['close']
        self.last_kline = kline
        trading_strategy.add_kline(kline)

        indicators = trading_strategy.calculate_indicators()
        if indicators and self.running:
            await self._check_signals()

    async def handle_depth(self, depth: dict):
        ob_info = order_flow_engine.analyze_order_book(depth)

    async def handle_trade(self, trade: dict):
        order_flow_engine.add_trade(trade)

        delta_info = order_flow_engine.calculate_delta()

        if self.running:
            await self._check_signals()

    async def _check_signals(self):
        if not self.position and self.price > 0:
            depth_data = {'bids': [], 'asks': []}
            ob_info = order_flow_engine.analyze_order_book(depth_data)
            delta_info = order_flow_engine.calculate_delta()

            signal_info = trading_strategy.analyze(delta_info, ob_info, self.price)

            if signal_info.get('signal') in ['long', 'short']:
                console.print(f"🎯 [bold]Señal detectada: {signal_info['signal'].upper()}[/bold]")
                if not self.mock_mode:
                    await self.execute_trade(signal_info)
                else:
                    console.print(f"📋 [yellow]Modo MOCK - Trade simulado[/yellow]")

    async def execute_trade(self, signal: dict):
        side = signal['signal']
        entry_price = signal['price']
        stop_loss = signal['stop_loss']
        quantity = 0.01

        binance_side = "BUY" if side == "long" else "SELL"

        console.print(f"\n🚀 [bold]Ejecutando {side.upper()}[/bold] @ ${entry_price}")

        order = await binance_client.place_order(binance_side, quantity)

        if order:
            self.position = {
                'side': side,
                'entry_price': entry_price,
                'quantity': quantity,
                'stop_loss': stop_loss,
                'entry_time': datetime.now()
            }

            self.entry_price = entry_price
            self.entry_time = datetime.now()

            stop_order = await binance_client.place_stop_loss(binance_side, quantity, stop_loss)

    async def check_exit_conditions(self):
        if not self.position:
            return

        pnl_percent = (self.price - self.entry_price) / self.entry_price * 100

        if self.position['side'] == 'short':
            pnl_percent = -pnl_percent

        exit_triggered = pnl_percent <= -2.0 or pnl_percent >= 1.5

        if exit_triggered:
            await self.close_position(pnl_percent)

    async def close_position(self, pnl_percent: float):
        side = self.position['side']
        quantity = self.position['quantity']

        exit_side = "SELL" if side == "long" else "BUY"

        console.print(f"🚪 [yellow]Cerrando posición {side}[/yellow] @ ${self.price}")

        if not self.mock_mode:
            order = await binance_client.place_order(exit_side, quantity)

        pnl = (self.price - self.entry_price) * quantity if side == 'long' else (self.entry_price - self.price) * quantity

        trade_data = {
            'side': side,
            'entry_price': self.entry_price,
            'exit_price': self.price,
            'quantity': quantity,
            'pnl': pnl,
            'entry_time': self.entry_time.isoformat(),
            'exit_time': datetime.now().isoformat(),
            'duration': (datetime.now() - self.entry_time).seconds
        }

        await supabase_manager.save_trade(trade_data)

        self.daily_pnl += pnl
        console.print(f"💰 [bold]PnL: ${pnl:.2f}[/bold] | Daily: ${self.daily_pnl:.2f}")

        self.position = None
        self.entry_price = 0.0

    def _get_bb_position(self, indicators: dict) -> float:
        bb_upper = indicators.get('bb_upper')
        bb_lower = indicators.get('bb_lower')
        current_price = indicators.get('close')

        if not all([bb_upper, bb_lower, current_price]):
            return 0.5

        return (current_price - bb_lower) / (bb_upper - bb_lower)

    async def run(self):
        await self.initialize()
        await self.start_websockets()

        console.print("\n" + "="*50)
        console.print("🎯 [bold green]BB-450 LISTO PARA OPERAR[/bold green]")
        if self.mock_mode:
            console.print("📋 [yellow]MODO MOCK ACTIVO[/yellow]")
        console.print("="*50 + "\n")

        self.running = True
        counter = 0
        while self.running:
            await asyncio.sleep(1)
            counter += 1

            if counter % 10 == 0:
                delta = order_flow_engine.calculate_delta()
                console.print(f"Price: ${self.price:,.0f} | Delta: {delta.get('delta', 0):.0f} | CVD: {order_flow_engine.cumulative_delta:.0f}")

            if self.position:
                await self.check_exit_conditions()

    async def stop(self):
        self.running = False
        if not self.mock_mode:
            await binance_client.close_connection()
        console.print("🔴 [red]Bot detenido[/red]")


async def main():
    parser = argparse.ArgumentParser(description='BB-450 Bot de Scalping')
    parser.add_argument('--mock', action='store_true', help='Ejecutar en modo simulación sin API')
    parser.add_argument('--timeout', type=int, default=0, help='Tiempo máximo de ejecución en segundos (0 = infinito)')
    args = parser.parse_args()

    bot = TradingBot(mock_mode=args.mock)

    def signal_handler(sig, frame):
        console.print("\n⚠️ [yellow]Interrupción recibida[/yellow]")
        asyncio.create_task(bot.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    try:
        if args.timeout > 0:
            await asyncio.wait_for(bot.run(), timeout=args.timeout)
        else:
            await bot.run()
    except asyncio.TimeoutError:
        console.print("⏰ [yellow]Timeout alcanzado[/yellow]")
        await bot.stop()
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())