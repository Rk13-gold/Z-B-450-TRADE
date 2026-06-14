#!/usr/bin/env python3
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from textual.app import App, ComposeResult
from textual.widgets import Static, Label
from textual.containers import Container, Grid
from textual.reactive import reactive

from config.settings import settings
from src.engine.binance_client import binance_client
from src.engine.order_flow import order_flow_engine
from src.engine.strategy import trading_strategy
from src.database.supabase_manager import local_trade_db


class TradingDashboard(App):
    CSS = """
    Screen {
        background: $surface;
    }
    
    .main-container {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr;
        height: 100%;
        padding: 1;
    }
    
    .panel {
        border: solid $primary;
        height: 100%;
        padding: 1;
    }
    
    .price {
        content-align: center middle;
        text-style: bold;
    }
    
    .price-value {
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }
    
    .grid-row {
        layout: horizontal;
    }
    
    .rsi-buy { color: $success; }
    .rsi-sell { color: $error; }
    .rsi-neutral { color: $text-muted; }
    
    .status {
        color: $text-muted;
    }
    
    .signal-buy { color: $success; text-style: bold; }
    .signal-sell { color: $error; text-style: bold; }
    .signal-neutral { color: $text-muted; }
    """

    BINDINGS = [("q", "quit", "Salir")]

    def __init__(self):
        super().__init__()
        self.price = 0.0
        self.rsi = 50.0
        self.macd = 0.0
        self.bb = 50.0
        self.delta = 0.0
        self.cvd = 0.0
        self.signal = "NINGUNA"
        
    def compose(self) -> ComposeResult:
        with Container(classes="main-container"):
            with Container(classes="panel"):
                yield Static("PRECIO BTCUSDT", classes="price")
                yield Label("---", classes="price-value", id="price-label")
            
            with Container(classes="panel"):
                yield Static("INDICADORES", classes="price")
                yield Label("RSI: --", id="rsi-label")
                yield Label("MACD: --", id="macd-label")
                yield Label("BB%: --", id="bb-label")
            
            with Container(classes="panel"):
                yield Static("DELTA / CVD", classes="price")
                yield Label("Delta: --", id="delta-label")
                yield Label("CVD: --", id="cvd-label")
            
            with Container(classes="panel"):
                yield Static("SEÑAL", classes="price")
                yield Label("NINGUNA", id="signal-label")

    async def on_mount(self):
        self.title = "BB-450 - Scalping Bot"
        await self.initialize_system()

    async def initialize_system(self):
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
            
            if historical_klines:
                self.price = float(historical_klines[-1][4])

            local_trade_db.connect()
            
            await binance_client.start_kline_stream(self.handle_kline)
            await binance_client.start_trade_stream(self.handle_trade)
            
            asyncio.create_task(self.update_loop())
            
        except Exception as e:
            print(f"Error: {e}")

    async def handle_kline(self, kline: dict):
        self.price = kline['close']
        trading_strategy.add_kline(kline)
        
        indicators = trading_strategy.calculate_indicators()
        self.rsi = indicators.get('rsi', 50) or 50
        self.macd = indicators.get('macd', 0) or 0
        
        bb_upper = indicators.get('bb_upper')
        bb_lower = indicators.get('bb_lower')
        if bb_upper and bb_lower and bb_upper != bb_lower:
            self.bb = (self.price - bb_lower) / (bb_upper - bb_lower) * 100

    async def handle_trade(self, trade: dict):
        order_flow_engine.add_trade(trade)
        delta_info = order_flow_engine.calculate_delta()
        self.delta = delta_info.get('delta', 0)
        self.cvd = order_flow_engine.cumulative_delta

    async def update_loop(self):
        while True:
            await asyncio.sleep(2)
            
            # Actualizar labels
            price_label = self.query_one("#price-label")
            price_label.update(f"${self.price:,.2f}")
            
            rsi_label = self.query_one("#rsi-label")
            rsi_color = "$success" if self.rsi < 30 else "$error" if self.rsi > 70 else "$text"
            rsi_label.update(f"RSI: {self.rsi:.1f}")
            
            macd_label = self.query_one("#macd-label")
            macd_label.update(f"MACD: {self.macd:.2f}")
            
            bb_label = self.query_one("#bb-label")
            bb_label.update(f"BB%: {self.bb:.1f}")
            
            delta_label = self.query_one("#delta-label")
            delta_label.update(f"Delta: {self.delta:.0f}")
            
            cvd_label = self.query_one("#cvd-label")
            cvd_label.update(f"CVD: {self.cvd:.0f}")
            
            # Señales
            delta_info = order_flow_engine.calculate_delta()
            signal_info = trading_strategy.analyze(delta_info, {}, self.price)
            
            signal_label = self.query_one("#signal-label")
            if signal_info.get('signal') == 'long':
                self.signal = "COMPRA"
                signal_label.update("[green]COMPRA[/green]")
            elif signal_info.get('signal') == 'short':
                self.signal = "VENTA"
                signal_label.update("[red]VENTA[/red]")
            else:
                self.signal = "NINGUNA"
                signal_label.update("NINGUNA")


def run_tui():
    app = TradingDashboard()
    app.run()


if __name__ == "__main__":
    run_tui()