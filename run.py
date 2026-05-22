#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import signal
from datetime import datetime
from rich.console import Console
from config.settings import settings
from src.engine.binance_client import binance_client
from src.engine.order_flow import order_flow_engine
from src.engine.strategy import trading_strategy
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
        self.price = 0.0

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
                try:
                    await binance_client.set_leverage()
                except Exception as e:
                    console.print(f"⚠️ [yellow]Apalancamiento: {e}[/yellow]")

                try:
                    console.print("📊 Cargando datos históricos...")
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
                    # Actualizar precio actual
                    if historical_klines:
                        self.price = float(historical_klines[-1][4])
                    console.print(f"✅ {len(historical_klines)} klines cargados | Precio: ${self.price:,.2f}")
                except Exception as e:
                    console.print(f"⚠️ [yellow]Klines: {e}[/yellow]")
                    await self._init_mock_data()
            except Exception as e:
                console.print(f"⚠️ [yellow]Error: {e}[/yellow]")
                self.mock_mode = True
                await self._init_mock_data()
                return

        if not self.mock_mode:
            if not settings.BINANCE_API_KEY or "invalid" in str(settings.BINANCE_API_KEY).lower():
                console.print("⚠️ [yellow]Sin API Key válida - Modo MOCK[/yellow]")
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
            console.print("📡 [cyan]Iniciando WebSockets...[/cyan]")
            
            # Solo iniciar kline stream por ahora
            try:
                await binance_client.start_kline_stream(self.handle_kline)
            except Exception as e:
                console.print(f"⚠️ [yellow]Kline: {e}[/yellow]")
            
            try:
                await binance_client.start_depth_stream(self.handle_depth)
            except Exception as e:
                console.print(f"⚠️ [yellow]Depth: {e}[/yellow]")
            
            try:
                await binance_client.start_trade_stream(self.handle_trade)
            except Exception as e:
                console.print(f"⚠️ [yellow]Trade: {e}[/yellow]")
                
            console.print("✅ [green]WebSockets iniciados[/green]")

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
        trading_strategy.add_kline(kline)
        
        if self.running:
            await self._check_signals()

    async def handle_depth(self, depth: dict):
        pass  # Order book silencioso para evitar spam

    async def handle_trade(self, trade: dict):
        order_flow_engine.add_trade(trade)
        
        if self.running:
            await self._check_signals()

    async def _check_signals(self):
        if not self.position and self.price > 0:
            ob_info = order_flow_engine.analyze_order_book({'bids': [], 'asks': []})
            delta_info = order_flow_engine.calculate_delta()
            signal_info = trading_strategy.analyze(delta_info, ob_info, self.price)

            if signal_info.get('signal') in ['long', 'short']:
                console.print(f"🎯 [bold]Señal: {signal_info['signal'].upper()} @ ${self.price:,.0f}[/bold]")
                if self.mock_mode:
                    console.print(f"📋 [yellow]Modo MOCK - Trade simulado[/yellow]")

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
            
            if counter % 3 == 0:
                indicators = trading_strategy.calculate_indicators()
                delta_info = order_flow_engine.calculate_delta()
                
                rsi = indicators.get('rsi', 0) or 50
                macd = indicators.get('macd', 0) or 0
                delta = delta_info.get('delta', 0)
                cvd = order_flow_engine.cumulative_delta
                bb_upper = indicators.get('bb_upper')
                bb_lower = indicators.get('bb_lower')
                
                bb_pos = 50
                if bb_upper and bb_lower and bb_upper != bb_lower:
                    try:
                        bb_pos = (self.price - bb_lower) / (bb_upper - bb_lower) * 100
                    except:
                        bb_pos = 50
                
                # Señal actual
                signal = trading_strategy.analyze(delta_info, {}, self.price)
                sig_text = signal.get('signal', '---').upper()
                
                console.print(f"┌─────────────────────────────────────────────┐")
                console.print(f"│ 💰 {self.price:>11,.2f} │ RSI: {rsi:>5.1f} │ MACD: {macd:>7.2f} │")
                console.print(f"│ 📊 Delta: {delta:>6.0f} │ CVD: {cvd:>6.0f} │ BB%: {bb_pos:>5.1f} │")
                console.print(f"│ 🎯 Señal: {sig_text:>31} │")
                console.print(f"└─────────────────────────────────────────────┘")
            
            if self.running and self.position:
                await self.check_exit_conditions()

    async def stop(self):
        self.running = False
        if not self.mock_mode:
            await binance_client.close_connection()
        console.print("🔴 [red]Bot detenido[/red]")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='BB-450 Bot de Scalping')
    parser.add_argument('--mock', action='store_true', help='Modo simulación')
    parser.add_argument('--timeout', type=int, default=0, help='Timeout (segundos)')
    args = parser.parse_args()

    bot = TradingBot(mock_mode=args.mock)

    def signal_handler(sig, frame):
        console.print("\n⚠️ [yellow]Interrupción recibida[/yellow]")
        asyncio.create_task(bot.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    try:
        if args.timeout > 0:
            asyncio.run(asyncio.wait_for(bot.run(), timeout=args.timeout))
        else:
            asyncio.run(bot.run())
    except asyncio.TimeoutError:
        console.print("⏰ [yellow]Timeout alcanzado[/yellow]")
        asyncio.run(bot.stop())


if __name__ == "__main__":
    main()