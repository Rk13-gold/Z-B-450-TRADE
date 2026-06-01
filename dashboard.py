#!/usr/bin/env python3
import asyncio
import sys
import os
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance.client import Client

from config.settings import settings
from src.engine.order_flow import order_flow_engine
from src.engine.strategy import trading_strategy
from src.database.supabase_manager import supabase_manager


client = Client(settings.BINANCE_REAL_API_KEY, settings.BINANCE_REAL_SECRET_KEY, testnet=False)

# Colores ANSI
NEGRO = "\033[40m"
VERDE = "\033[32m"
ROJO = "\033[31m"
AMARILLO = "\033[33m"
CIAN = "\033[36m"
MAGENTA = "\033[35m"
BLANCO = "\033[37m"
GRIS = "\033[90m"
GRIS_OSCURO = "\033[38;5;8m"
RESET = "\033[0m"

# Estado global
data = {
    'price': 0.0,
    'price_change': 0.0,
    'price_change_pct': 0.0,
    'rsi': 50.0,
    'macd': 0.0,
    'macd_signal': 0.0,
    'macd_hist': 0.0,
    'bb_upper': 0.0,
    'bb_middle': 0.0,
    'bb_lower': 0.0,
    'bb_position': 50.0,
    'atr': 0.0,
    'ema_20': 0.0,
    'ema_50': 0.0,
    'delta': 0.0,
    'cvd': 0.0,
    'buy_volume': 0.0,
    'sell_volume': 0.0,
    'signal': 'NINGUNA',
    'trend': 'NEUTRAL',
    'daily_pnl': 0.0,
    'trade_count': 0,
    'win_rate': 0.0,
    'last_price': 0.0,
    'klines': []
}


def clear_screen():
    print("\033[3J\033[H", end="")
    sys.stdout.flush()


def get_terminal_size():
    return shutil.get_terminal_size()


def format_number(num, decimals=2):
    if abs(num) >= 1000:
        return f"{num:,.0f}"
    return f"{num:,.2f}"


def calculate_ema(prices, period):
    if len(prices) < period:
        return 0
    return sum(prices[-period:]) / period


def get_price():
    try:
        ticker = client.futures_symbol_ticker(symbol="BTCUSDT")
        return float(ticker['price'])
    except:
        return data['price']


def get_klines():
    try:
        return client.futures_klines(symbol="BTCUSDT", interval="1m", limit=200)
    except:
        return []


def get_trades():
    try:
        return client.futures_agg_trades(symbol="BTCUSDT", limit=50)
    except:
        return []


def calculate_all_indicators(klines):
    if len(klines) < 50:
        return
    
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    
    data['ema_20'] = calculate_ema(closes, 20)
    data['ema_50'] = calculate_ema(closes, 50) if len(closes) >= 50 else data['ema_20']
    
    sma20 = sum(closes[-20:]) / 20
    std = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
    data['bb_upper'] = sma20 + (2 * std)
    data['bb_middle'] = sma20
    data['bb_lower'] = sma20 - (2 * std)
    
    if data['bb_upper'] != data['bb_lower']:
        data['bb_position'] = ((closes[-1] - data['bb_lower']) / 
                              (data['bb_upper'] - data['bb_lower'])) * 100
    
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-14:]]
    losses = [-d if d < 0 else 0 for d in deltas[-14:]]
    avg_gain = sum(gains) / 14 if gains else 0
    avg_loss = sum(losses) / 14 if losses else 0
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    data['rsi'] = 100 - (100 / (1 + rs))
    
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    data['macd'] = ema12 - ema26
    
    macd_values = [ema12 - calculate_ema(closes[:i], 26) for i in range(26, len(closes))]
    data['macd_signal'] = calculate_ema(macd_values, 9) if len(macd_values) >= 9 else data['macd']
    data['macd_hist'] = data['macd'] - data['macd_signal']
    
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    data['atr'] = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0
    
    if data['ema_20'] > data['ema_50']:
        data['trend'] = 'ALCISTA'
    elif data['ema_20'] < data['ema_50']:
        data['trend'] = 'BAJISTA'
    else:
        data['trend'] = 'NEUTRAL'


def determine_signal():
    signal = 'NINGUNA'
    
    long_conditions = 0
    if data['rsi'] < 30:
        long_conditions += 1
    if data['bb_position'] < 20:
        long_conditions += 1
    if data['macd'] > data['macd_signal'] and data['macd_hist'] > 0:
        long_conditions += 1
    if data['delta'] > 100:
        long_conditions += 1
    if data['trend'] == 'ALCISTA':
        long_conditions += 1
    
    short_conditions = 0
    if data['rsi'] > 70:
        short_conditions += 1
    if data['bb_position'] > 80:
        short_conditions += 1
    if data['macd'] < data['macd_signal'] and data['macd_hist'] < 0:
        short_conditions += 1
    if data['delta'] < -100:
        short_conditions += 1
    if data['trend'] == 'BAJISTA':
        short_conditions += 1
    
    if long_conditions >= 3:
        signal = 'COMPRA'
    elif short_conditions >= 3:
        signal = 'VENTA'
    
    return signal


def pad_center(text, width):
    lines = text.split('\n')
    padded = []
    for line in lines:
        if len(line) >= width:
            padded.append(line[:width])
        else:
            pad_left = (width - len(line)) // 2
            padded.append(' ' * pad_left + line + ' ' * (width - pad_left - len(line)))
    return '\n'.join(padded)


def create_panel_content(panel_type):
    if panel_type == 'PRECIO':
        price_color = VERDE if data['price_change'] >= 0 else ROJO
        change_symbol = "▲" if data['price_change'] >= 0 else "▼"
        return f"""{CIAN}PRECIO{RESET}
{price_color}${format_number(data['price'])}{RESET}
{price_color}{change_symbol} ${format_number(abs(data['price_change']))} ({data['price_change_pct']:+.2f}%){RESET}"""
    
    elif panel_type == 'TENDENCIA':
        trend_color = VERDE if data['trend'] == 'ALCISTA' else ROJO if data['trend'] == 'BAJISTA' else AMARILLO
        ema_dir = "▲" if data['ema_20'] > data['ema_50'] else "▼"
        return f"""{CIAN}TENDENCIA{RESET}
{trend_color}{data['trend']}{RESET}
{GRIS}{ema_dir} EMA 20/50{GRIS}"""
    
    elif panel_type == 'MACD':
        macd_color = VERDE if data['macd_hist'] >= 0 else ROJO
        macd_symbol = "▲" if data['macd_hist'] >= 0 else "▼"
        return f"""{CIAN}MACD{RESET}
{macd_color}{macd_symbol} {format_number(data['macd'])}{RESET}
{GRIS}Sig: {format_number(data['macd_signal'])}{GRIS}
{GRIS}Hst: {format_number(data['macd_hist'])}{GRIS}"""
    
    elif panel_type == 'BOLLINGER':
        bb_color = VERDE if data['bb_position'] < 30 else ROJO if data['bb_position'] > 70 else AMARILLO
        return f"""{CIAN}BOLLINGER{RESET}
{bb_color}BB%: {data['bb_position']:.1f}%{RESET}
{GRIS}Up: {format_number(data['bb_upper'])}{GRIS}
{GRIS}Dn: {format_number(data['bb_lower'])}{GRIS}"""
    
    elif panel_type == 'ORDER_FLOW':
        delta_color = VERDE if data['delta'] >= 0 else ROJO
        cvd_color = VERDE if data['cvd'] >= 0 else ROJO
        total_vol = data['buy_volume'] + data['sell_volume']
        buy_pct = (data['buy_volume'] / total_vol * 100) if total_vol > 0 else 50
        bar_len = 12
        buy_bar = int(buy_pct / 100 * bar_len)
        vol_bar = "█" * buy_bar + "░" * (bar_len - buy_bar)
        return f"""{CIAN}ORDER FLOW{RESET}
{delta_color}Δ: {data['delta']:+}{RESET}
{cvd_color}CV: {data['cvd']:+}{RESET}
{GRIS}{vol_bar}{GRIS}"""
    
    elif panel_type == 'ATR':
        return f"""{CIAN}ATR (14){RESET}
{AMARILLO}${format_number(data['atr'])}{RESET}
{GRIS}H: {format_number(data['atr']*2)}{GRIS}
{GRIS}L: {format_number(data['atr']*0.5)}{GRIS}"""
    
    elif panel_type == 'SEÑAL':
        if data['signal'] == 'COMPRA':
            sig_color = VERDE
            sig_text = "🟢 COMPRA"
        elif data['signal'] == 'VENTA':
            sig_color = ROJO
            sig_text = "🔴 VENTA"
        else:
            sig_color = AMARILLO
            sig_text = "🟡 NINGUNA"
        trend_rsi = "Sobrev" if data['rsi'] < 30 else "Sobrecom" if data['rsi'] > 70 else "Neutral"
        return f"""{CIAN}SEÑAL{RESET}
{sig_color}{sig_text}{RESET}
{GRIS}RSI: {trend_rsi}{GRIS}"""
    
    elif panel_type == 'MERCADO':
        return f"""{CIAN}MERCADO{RESET}
{BLANCO}BTCUSDT{RESET}
{AMARILLO}100x LEV{RESET}
{GRIS}1m TF{GRIS}"""
    
    elif panel_type == 'PNL':
        pnl_color = VERDE if data['daily_pnl'] >= 0 else ROJO
        return f"""{CIAN}PnL DIARIO{RESET}
{pnl_color}${data['daily_pnl']:+.2f}{RESET}
{GRIS}Trades: {data['trade_count']}{GRIS}
{GRIS}Win: {data['win_rate']:.1f}%{GRIS}"""
    
    elif panel_type == 'RSI':
        rsi_color = ROJO if data['rsi'] > 70 else VERDE if data['rsi'] < 30 else AMARILLO
        rsi_bar = "█" * int(data['rsi'] / 10) + "░" * (10 - int(data['rsi'] / 10))
        return f"""{CIAN}RSI (14){RESET}
{rsi_color}{data['rsi']:.1f}{RESET}
{rsi_color}{rsi_bar}{RESET}"""
    
    elif panel_type == 'VOLUMEN':
        vol = sum([float(k[5]) for k in data.get('klines', [])[-20:]]) if data.get('klines') else 0
        return f"""{CIAN}VOLUMEN (20){RESET}
{BLANCO}{format_number(vol, 0)}{RESET}
{GRIS}Avg: {format_number(vol/20, 0)}{GRIS}"""
    
    return ""


def create_dashboard():
    rows, cols = get_terminal_size()
    col_w = cols // 4
    
    lines = []
    
    # Clear y header
    lines.append(f"{NEGRO}\033[H\033[J{GRIS}{'─' * cols}{RESET}")
    title = "BB-450 PRO DASHBOARD"
    lines.append(f"{NEGRO}{GRIS}{title.center(cols)}{RESET}")
    lines.append(f"{NEGRO}{GRIS}{'─' * cols}{RESET}")
    
    # Layout 4 columnas x 3 filas
    row1 = ['PRECIO', 'TENDENCIA', 'MACD', 'BOLLINGER']
    row2 = ['ORDER_FLOW', 'ATR', 'SEÑAL', 'MERCADO']
    row3 = ['PNL', 'RSI', 'VOLUMEN', None]
    
    all_rows = [row1, row2, row3]
    
    for row_idx, row in enumerate(all_rows):
        row_contents = []
        max_h = 0
        for panel in row:
            if panel:
                content = create_panel_content(panel)
                lines_panel = content.split('\n')
                row_contents.append(lines_panel)
                max_h = max(max_h, len(lines_panel))
            else:
                row_contents.append([''])
                max_h = max(max_h, 1)
        
        for h in range(max_h):
            line_parts = []
            for i, panel_lines in enumerate(row_contents):
                part = panel_lines[h] if h < len(panel_lines) else ''
                line_parts.append(part.ljust(col_w) if i < len(row_contents)-1 else part.rjust(col_w))
            lines.append(f"{NEGRO}{''.join(line_parts)}{RESET}")
        
        if row_idx < len(all_rows) - 1:
            lines.append(f"{NEGRO}{GRIS}{'─' * cols}{RESET}")
    
    # Footer
    lines.append(f"{NEGRO}{GRIS}{'─' * cols}{RESET}")
    lines.append(f"{GRIS}Ctrl+C para salir{RESET}")
    
    output = '\n'.join(lines)
    print(output)
    sys.stdout.flush()


async def loading():
    clear_screen()
    rows, cols = get_terminal_size()
    title = "BB-450 PRO DASHBOARD v1.0"
    print(f"{NEGRO}{GRIS}{title.center(cols)}{RESET}")
    print(f"{NEGRO}{GRIS}{'─' * cols}{RESET}")
    print()
    
    tasks = [
        "Inicializando...",
        "Cargando datos...",
        "Calculando indicadores...",
        "Iniciando..."
    ]
    
    for label in tasks:
        print(f"{CIAN}▸{RESET} {label}")
        await asyncio.sleep(0.15)
    
    print()
    print(f"{VERDE}✓ Sistema listo!{RESET}")
    await asyncio.sleep(0.5)


async def main():
    global data
    
    await loading()
    
    try:
        client.futures_change_leverage(symbol="BTCUSDT", leverage=100)
    except:
        pass
    
    klines = get_klines()
    for k in klines:
        kline = {'time': k[0], 'open': float(k[1]), 'high': float(k[2]),
                 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])}
        trading_strategy.add_kline(kline)
    
    data['price'] = float(klines[-1][4])
    data['last_price'] = data['price']
    data['klines'] = klines
    calculate_all_indicators(klines)
    
    supabase_manager.connect()
    
    print(f"{GRIS}Presiona Ctrl+C para salir{GRIS}")
    
    last_kline_time = klines[-1][0]
    
    while True:
        await asyncio.sleep(1)
        
        # Actualizar precio
        data['price'] = get_price()
        data['price_change'] = data['price'] - data['last_price']
        
        # Actualizar klines
        klines = get_klines()
        if klines and int(klines[-1][0]) > last_kline_time:
            data['klines'] = klines
            for k in klines:
                kline = {'time': k[0], 'open': float(k[1]), 'high': float(k[2]),
                         'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])}
                trading_strategy.add_kline(kline)
            last_kline_time = int(klines[-1][0])
            calculate_all_indicators(klines)
        
        # Actualizar trades
        try:
            trades = get_trades()
            for t in trades[:20]:
                trade_data = {
                    'time': int(t['T']),
                    'price': float(t['p']),
                    'quantity': float(t['q']),
                    'is_buyer_maker': t['m']
                }
                order_flow_engine.add_trade(trade_data)
            
            delta_info = order_flow_engine.calculate_delta()
            data['delta'] = delta_info.get('delta', 0)
            data['cvd'] = order_flow_engine.cumulative_delta
            data['buy_volume'] = delta_info.get('buy_volume', 0)
            data['sell_volume'] = delta_info.get('sell_volume', 0)
        except:
            pass
        
        # Señal
        data['signal'] = determine_signal()
        
        if data['price'] > 0:
            data['price_change_pct'] = (data['price_change'] / data['last_price']) * 100
        
        create_dashboard()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{NEGRO}{ROJO}▸ Dashboard detenido{RESET}")