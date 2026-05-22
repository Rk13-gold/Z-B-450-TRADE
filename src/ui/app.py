from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Static, DataTable, Label, Sparkline
from textual import work
from textual.css.match import match
from rich.text import Text
from rich.console import Console
from rich.table import Table
from datetime import datetime
import asyncio


class CryptoDashboard(App):
    CSS = """
    Screen {
        background: $background;
    }

    #main-container {
        layout: grid;
        grid-size: 3 2;
        grid-columns: 1fr 1fr 1fr;
        grid-rows: 1fr 1fr;
        height: 100%;
        padding: 1;
    }

    .panel {
        border: solid $primary;
        padding: 1;
        margin: 1;
    }

    .panel-title {
        text-align: center;
        text-style: bold;
        color: $text;
        background: $surface;
        padding: 0 1;
    }

    #price-panel {
        border-color: $accent;
    }

    #delta-panel {
        border-color: $success;
    }

    #orderbook-panel {
        border-color: $warning;
    }

    #chart-panel {
        border-color: $primary;
    }

    #indicators-panel {
        border-color: $accent;
    }

    #signals-panel {
        border-color: $error;
    }

    .long {
        color: #00ff00;
        text-style: bold;
    }

    .short {
        color: #ff0066;
        text-style: bold;
    }

    .neutral {
        color: #888888;
    }

    .positive {
        color: #00ff00;
    }

    .negative {
        color: #ff0066;
    }

    #price-display {
        content-align: center middle;
        text-style: bold;
        font-size: 40;
    }

    #delta-display {
        content-align: center middle;
        font-size: 24;
    }

    #orderbook-display {
        color: $text;
    }

    #signal-display {
        content-align: center middle;
        font-size: 20;
    }
    """

    def __init__(self):
        super().__init__()
        self.price = 0.0
        self.delta = 0.0
        self.cvd = 0.0
        self.rsi = 50.0
        self.macd = 0.0
        self.bb_position = 0.5
        self.orderbook_bids = []
        self.orderbook_asks = []
        self.last_signal = "NINGUNO"
        self.trades_count = 0
        self.pnl_total = 0.0

    def compose(self) -> ComposeResult:
        with Container(id="main-container"):
            with Vertical(classes="panel", id="price-panel"):
                yield Static("PRECIO BTCUSDT", classes="panel-title")
                yield Static("0.00", id="price-display")

            with Vertical(classes="panel", id="delta-panel"):
                yield Static("DELTA / CVD", classes="panel-title")
                yield Static("Delta: 0\nCVD: 0", id="delta-display")

            with Vertical(classes="panel", id="orderbook-panel"):
                yield Static("ORDER BOOK", classes="panel-title")
                yield Static("", id="orderbook-display")

            with Vertical(classes="panel", id="chart-panel"):
                yield Static("GRÁFICO (1m)", classes="panel-title")
                yield Static("░░░▒▒▒▓▓▓███", id="chart-display")

            with Vertical(classes="panel", id="indicators-panel"):
                yield Static("INDICADORES", classes="panel-title")
                yield Static("RSI: 50\nMACD: 0.00\nBB: 50%", id="indicators-display")

            with Vertical(classes="panel", id="signals-panel"):
                yield Static("SEÑALES", classes="panel-title")
                yield Static("NINGUNO", id="signal-display")

    async def on_mount(self):
        self.title = "BB-450 | Bot de Scalping"
        self.sub_title = "100x Leverage | Binance Futures"

    async def update_price(self, price: float):
        self.price = price
        display = self.query_one("#price-display")
        display.update(f"${price:,.2f}")

    async def update_delta(self, delta: float, cvd: float, delta_strength: float):
        self.delta = delta
        self.cvd = cvd

        delta_display = self.query_one("#delta-display")

        color = "green" if delta > 0 else "red" if delta < 0 else "white"
        delta_str = f"[{color}]Delta: {delta:,.0f}[/{color}]\nCVD: {cvd:,.0f}"

        delta_display.update(delta_str)

    async def update_orderbook(self, bids, asks):
        self.orderbook_bids = bids
        self.orderbook_asks = asks

        bid_vol = sum(vol for _, vol in bids[:5])
        ask_vol = sum(vol for _, vol in asks[:5])

        display = self.query_one("#orderbook-display")

        bars = "█" * 10
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) * 100 if (bid_vol + ask_vol) > 0 else 0

        text = f"Bids: {bid_vol:.2f} | Asks: {ask_vol:.2f}\n"
        text += f"{bars} Imbalance: {imbalance:.1f}%"

        display.update(text)

    async def update_indicators(self, rsi: float, macd: float, bb_pos: float, atr: float):
        self.rsi = rsi
        self.macd = macd
        self.bb_position = bb_pos

        rsi_color = "green" if rsi < 30 else "red" if rsi > 70 else "white"

        display = self.query_one("#indicators-display")

        text = f"RSI(14): [{rsi_color}]{rsi:.1f}[/{rsi_color}]\n"
        text += f"MACD: {macd:.4f}\n"
        text += f"BB Pos: {bb_pos*100:.1f}%\n"
        text += f"ATR: {atr:.2f}"

        display.update(text)

    async def update_signal(self, signal: str):
        self.last_signal = signal

        display = self.query_one("#signal-display")

        if signal == "LONG":
            display.update("[green]🟢 SEÑAL COMPRA[/green]")
        elif signal == "SHORT":
            display.update("[red]🔴 SEÑAL VENTA[/red]")
        else:
            display.update("[yellow]🟡 SIN SEÑAL[/yellow]")

    async def update_chart(self, candles: list):
        if len(candles) < 20:
            return

        display = self.query_one("#chart-display")

        chart = ""
        for c in candles[-20:]:
            open_p, close_p = c['open'], c['close']
            high_p, low_p = c['high'], c['low']

            if close_p >= open_p:
                chart += "│"
            else:
                chart += "─"

        display.update(chart)


class TUIDisplay:
    def __init__(self):
        self.app = CryptoDashboard()
        self.console = Console()

    async def start(self):
        self.app.run()

    def print_trade(self, trade: dict):
        table = Table(title="Trade Ejecutado")
        table.add_column("Campo", style="cyan")
        table.add_column("Valor", style="white")

        table.add_row("Lado", trade.get('side', ''))
        table.add_row("Entrada", str(trade.get('entry_price', 0)))
        table.add_row("Cantidad", str(trade.get('quantity', 0)))
        table.add_row("PnL", str(trade.get('pnl', 0)))

        self.console.print(table)


tui_display = TUIDisplay()