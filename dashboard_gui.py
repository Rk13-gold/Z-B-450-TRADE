#!/usr/bin/env python3
import sys
import os
import threading
import time
import subprocess
import sqlite3
import re
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SOUND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                           'assets', 'NOTIFICATORBB450.mp3')

def play_notification_sound():
    if not os.path.exists(SOUND_FILE):
        return
    
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['afplay', SOUND_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == 'linux':
            subprocess.Popen(['paplay', SOUND_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == 'win32':
            import winsound
            winsound.PlaySound(SOUND_FILE, winsound.SND_FILENAME)
    except:
        pass


from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QGridLayout,
                             QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                             QTabWidget, QShortcut, QPushButton, QOpenGLWidget,
                             QSplitter, QPlainTextEdit, QTextBrowser,
                             QProgressBar, QSlider,
                             QListWidget, QFileDialog)
from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence, QPainter, QPainterPath, QPen, QStaticText, QPixmap, QFontDatabase

from binance.client import Client
from config.settings import settings
from src.engine.order_flow import order_flow_engine
from src.telegram_bot import TelegramBot
from src.engine.order_executor import OrderExecutor
from src.engine.strategy import trading_strategy
from src.engine.async_data_engine import AsyncDataEngine
from src.database.supabase_manager import supabase_manager
from src.engine.gemini_brain import GeminiBrainManager, GeminiTradingDecision


try:
    client = Client(settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY, testnet=False)
except Exception as e:
    print(f"[DASHBOARD] ⚠ No se pudo conectar con Binance (python-binance): {e}")
    client = None

COLORS = {
    'background': '#000000',
    'panel_bg': 'rgba(0, 0, 0, 0)',
    'panel_glass': 'rgba(20, 25, 35, 0.85)',
    'panel_glass_border': 'rgba(60, 70, 90, 0.4)',
    'gradient_start': '#0a0a12',
    'gradient_end': '#0f0f18',
    'accent_turquoise': '#00ff66',
    'accent_purple': '#bb00ff',
    'accent_gold': '#ffcc00',
    'accent_cyan': '#00ff66',
    'accent_emerald': '#00ff88',
    'accent_magenta': '#bb00ff',
    'accent_crimson': '#bb00ff',
    'text_primary': '#ffffff',
    'text_secondary': '#aaaaaa',
    'text_dim': '#666666',
    'border_dim': '#222222',
    'border_glow': '#00ff66',
    'support_green': '#00cc6a',
    'resistance_red': '#bb00ff',
    'neutral_gray': '#4a4a5a',
}

PANEL_STYLE = f"""
    QFrame {{
        background-color: #000000;
        border: 1px solid #1a1a1a;
        border-radius: 6px;
    }}
"""

GLASS_PANEL_STYLE = f"""
    QFrame {{
        background: #000000;
        border: 1px solid #222222;
        border-radius: 8px;
    }}
"""

HEADER_GRADIENT_STYLE = f"""
    QLabel {{
        background: #000000;
        border-bottom: 1px solid #333333;
        border-bottom: 2px solid {COLORS['accent_cyan']};
        border-radius: 6px;
    }}
"""


class DashboardPanel(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-weight: bold; font-size: 14px; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        
        self.content_label = QLabel()
        self.content_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 16px; border: none; background: transparent;")
        self.content_label.setAlignment(Qt.AlignCenter)
        self.content_label.setWordWrap(True)
        
        layout.addWidget(self.title_label)
        layout.addWidget(self.content_label)
        
        self.setLayout(layout)
    
    def update_content(self, text_lines, color=None):
        if color is None:
            color = COLORS['text_primary']
        html = f"<span style='color: {color}; font-size: 18px; font-family: 'JetBrains Mono', monospace;'>"
        for line in text_lines:
            html += f"{line}<br>"
        html += "</span>"
        self.content_label.setText(html)


from PyQt5.QtGui import QPainter, QLinearGradient, QBrush, QPen, QFontMetrics

# ═══════════════════════════════════════════════════════════════════════════════
# FOOTPRINT CONFIGURATION — Adjustable thresholds for volume filtering
# ═══════════════════════════════════════════════════════════════════════════════
VOLUME_THRESHOLD = 2.0        # Min BTC volume to render text (filters retail noise)
WHALE_MULTIPLIER = 10.0       # Threshold × this = Institutional/Whale level (neon glow)
MEDIUM_MULTIPLIER = 3.0       # Threshold × this = Medium tier (standard color)
ZOOM_STEP = 0.15              # How much each scroll/key press changes zoom
ZOOM_MIN = 0.3                # Minimum zoom factor
ZOOM_MAX = 5.0                # Maximum zoom factor


class OrderFlowNumericFeed(QOpenGLWidget):
    """Order Flow Numeric Feed - High density data grid panel.
    
    Renders a pure numeric data grid without graphical charts or heatmaps.
    Displays price levels with BID/ASK volumes, NET DELTA, and dPOC distance.
    Optimized for low-latency HFT display with monospace typography.
    """
    def __init__(self, title="ORDER FLOW NUMERIC FEED", parent=None):
        super().__init__(parent)
        self.title = title
        self.setMinimumHeight(350)
        
        self.price_levels = {}
        self.current_price = 0.0
        self.poc_price = 0.0
        self.vah_price = 0.0
        self.val_price = 0.0
        
        self.y_scale_factor = 1.0
        self.y_scroll_offset = 0.0
        self.tick_size = 10.0
        
        self.text_cache = {}
        self.bg_buffer = None
        self.last_buffer_state = None
        
        self.order_state = None
        self.raw_trades = []
        self.bounce_zones = []
        self.predicted_candles = []
        
        self.pulse_alpha = 0
        self.pulse_direction = 1
        self.last_poc_price = 0
        
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._animate_pulse)
        self.anim_timer.start(50)
        
        self.setFocusPolicy(Qt.ClickFocus)
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)
        
        header_layout = QHBoxLayout()
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-weight: bold; font-size: 14px; border: none; background: transparent;")
        header_layout.addWidget(self.title_label)
        
        header_layout.addStretch()
        
        btn_style = "background: #222; color: #fff; font-weight: bold; font-size: 14px; padding: 4px 10px; border-radius: 4px;"
        
        btn_y_in = QPushButton("⇕+")
        btn_y_in.setToolTip("Zoom In (Y-Axis)")
        btn_y_in.setStyleSheet(btn_style)
        btn_y_in.clicked.connect(lambda: self.adjust_y_zoom(0.1))
        
        btn_y_out = QPushButton("⇕-")
        btn_y_out.setToolTip("Zoom Out (Y-Axis)")
        btn_y_out.setStyleSheet(btn_style)
        btn_y_out.clicked.connect(lambda: self.adjust_y_zoom(-0.1))
        
        btn_up = QPushButton("▲")
        btn_up.setToolTip("Scroll Up")
        btn_up.setStyleSheet(btn_style)
        btn_up.clicked.connect(lambda: self.adjust_y_pan(50))
        
        btn_dn = QPushButton("▼")
        btn_dn.setToolTip("Scroll Down")
        btn_dn.setStyleSheet(btn_style)
        btn_dn.clicked.connect(lambda: self.adjust_y_pan(-50))
        
        btn_rst = QPushButton("⟲")
        btn_rst.setToolTip("Auto-Center")
        btn_rst.setStyleSheet(btn_style)
        btn_rst.clicked.connect(self.reset_zoom)
        
        for b in [btn_y_in, btn_y_out, btn_up, btn_dn, btn_rst]:
            b.setCursor(Qt.PointingHandCursor)
            header_layout.addWidget(b)
            
        layout.addLayout(header_layout)
        layout.addStretch()
        self.setLayout(layout)
    
    def adjust_y_zoom(self, amount):
        ZOOM_MAX, ZOOM_MIN = 3.0, 0.2
        self.y_scale_factor = max(ZOOM_MIN, min(ZOOM_MAX, self.y_scale_factor + amount))
        self._update_title()
        self.update()
        
    def adjust_y_pan(self, amount):
        self.y_scroll_offset += amount
        self.update()
        
    def reset_zoom(self):
        self.y_scale_factor = 1.0
        self.y_scroll_offset = 0.0
        self._update_title()
        self.update()
    
    def _update_title(self):
        zoom_pct = int(self.y_scale_factor * 100)
        if zoom_pct == 100:
            self.title_label.setText(self.title)
        else:
            self.title_label.setText(f"{self.title}  🔍 {zoom_pct}%")
    
    def _animate_pulse(self):
        if self.poc_price != self.last_poc_price and self.poc_price > 0:
            self.pulse_alpha = 255
            self.last_poc_price = self.poc_price
        
        self.pulse_alpha += self.pulse_direction * 15
        if self.pulse_alpha >= 255:
            self.pulse_alpha = 255
            self.pulse_direction = -1
        elif self.pulse_alpha <= 60:
            self.pulse_alpha = 60
            self.pulse_direction = 1
        
        self.update()
    
    def get_pulse_alpha(self):
        return self.pulse_alpha

    def update_trades(self, trades):
        if not trades: return
        self.raw_trades.extend(trades)
        if len(self.raw_trades) > 5000:
            self.raw_trades = self.raw_trades[-5000:]
        self._build_price_levels()
        self.update()
    
    def _build_price_levels(self):
        self.price_levels = {}
        self.session_profile = {}
        
        if self.raw_trades:
            for t in self.raw_trades:
                price = round(t['price'] / self.tick_size) * self.tick_size
                qty = t['quantity']
                is_buyer_maker = t.get('is_buyer_maker', t.get('m', False))
                
                if price not in self.price_levels:
                    self.price_levels[price] = {'bid_vol': 0.0, 'ask_vol': 0.0, 'trade_count': 0}
                
                if is_buyer_maker:
                    self.price_levels[price]['bid_vol'] += qty
                else:
                    self.price_levels[price]['ask_vol'] += qty
                self.price_levels[price]['trade_count'] += 1
                
                self.session_profile[price] = self.session_profile.get(price, 0) + qty
        
        if hasattr(self, 'raw_order_book') and self.raw_order_book:
            all_bids_raw = self.raw_order_book.get('bids', [])
            all_asks_raw = self.raw_order_book.get('asks', [])
            
            for p, q in all_bids_raw:
                price = round(float(p) / self.tick_size) * self.tick_size
                if price not in self.price_levels:
                    self.price_levels[price] = {'bid_vol': 0.0, 'ask_vol': 0.0, 'trade_count': 0}
                self.price_levels[price]['bid_vol'] += float(q) * 0.1
            
            for p, q in all_asks_raw:
                price = round(float(p) / self.tick_size) * self.tick_size
                if price not in self.price_levels:
                    self.price_levels[price] = {'bid_vol': 0.0, 'ask_vol': 0.0, 'trade_count': 0}
                self.price_levels[price]['ask_vol'] += float(q) * 0.1
        
        sorted_prices = sorted(self.price_levels.keys(), reverse=True)
        if sorted_prices and self.current_price == 0:
            self.current_price = sorted_prices[0]
        
        if self.session_profile:
            self.poc_price = max(self.session_profile.items(), key=lambda x: x[1])[0]
            
            total_vol = sum(self.session_profile.values())
            target_vol = total_vol * 0.70
            
            if self.poc_price in self.session_profile:
                current_vol = self.session_profile[self.poc_price]
                sorted_prices_vol = sorted(self.session_profile.keys())
                try:
                    poc_idx = sorted_prices_vol.index(self.poc_price)
                except:
                    poc_idx = 0
                up_idx = poc_idx + 1
                dn_idx = poc_idx - 1
                
                self.vah_price = self.poc_price
                self.val_price = self.poc_price
                
                while current_vol < target_vol and (up_idx < len(sorted_prices_vol) or dn_idx >= 0):
                    up_vol = self.session_profile[sorted_prices_vol[up_idx]] if up_idx < len(sorted_prices_vol) else -1
                    dn_vol = self.session_profile[sorted_prices_vol[dn_idx]] if dn_idx >= 0 else -1
                    
                    if up_vol > dn_vol:
                        current_vol += up_vol
                        self.vah_price = sorted_prices_vol[up_idx]
                        up_idx += 1
                    else:
                        current_vol += dn_vol
                        self.val_price = sorted_prices_vol[dn_idx]
                        dn_idx -= 1
    
    def update_data(self, order_book, current_price):
        if not order_book: return
        self.current_price = current_price
        self.raw_order_book = order_book
        
        bids = sorted([(float(p), float(q)) for p, q in order_book.get('bids', []) if float(q) > 0.3], key=lambda x: x[1], reverse=True)[:30]
        asks = sorted([(float(p), float(q)) for p, q in order_book.get('asks', []) if float(q) > 0.3], key=lambda x: x[1], reverse=True)[:30]
        self.order_state = {'bids': bids, 'asks': asks, 'price': current_price}
        
        all_bids = [(float(p), float(q)) for p, q in order_book.get('bids', [])]
        all_asks = [(float(p), float(q)) for p, q in order_book.get('asks', [])]
        
        total_bid_vol = sum(q for _, q in all_bids)
        total_ask_vol = sum(q for _, q in all_asks)
        
        if total_bid_vol + total_ask_vol > 0:
            self.orderbook_imbalance = (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
        else:
            self.orderbook_imbalance = 0.0
        
        self._build_price_levels()
        self.update()
    
    def update_poc(self, poc_price, vah_price, val_price):
        self.poc_price = poc_price
        self.vah_price = vah_price
        self.val_price = val_price
    
    def _render_static_layer(self, draw_rect, h, ps, base_font_size, font, num_rows):
        pm = QPixmap(self.size())
        pm.fill(QColor("#000000"))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        
        vp_min_y = draw_rect.top()
        vp_max_y = draw_rect.bottom()
        vp_min_x = draw_rect.left()
        vp_max_x = draw_rect.right()
        
        self._min_p = self.current_price - (num_rows * self.tick_size / 2) + self.y_scroll_offset
        self._max_p = self.current_price + (num_rows * self.tick_size / 2) + self.y_scroll_offset
        ps = self._max_p - self._min_p if self._max_p != self._min_p else 1
        
        row_height = (draw_rect.height() - 30) / max(1, num_rows)
        start_y = draw_rect.top() + 25
        
        col_positions = [
            (0.02, 0.18),
            (0.21, 0.17),
            (0.39, 0.17),
            (0.57, 0.15),
            (0.73, 0.25)
        ]
        
        headers = ["PRICE", "BID VOL", "ASK VOL", "DELTA", "dPOC DIST"]
        header_colors = [
            QColor(180, 180, 180),
            QColor(0, 255, 102),
            QColor(187, 0, 255),
            QColor(255, 204, 0),
            QColor(255, 204, 0)
        ]
        
        scale_font = QFont(font)
        scale_font.setPointSize(9)
        scale_font.setBold(True)
        painter.setFont(scale_font)
        
        header_y = draw_rect.top() + 8
        for i, (header, (start_pct, _)) in enumerate(zip(headers, col_positions)):
            x = draw_rect.left() + (draw_rect.width() * start_pct)
            painter.setPen(header_colors[i])
            painter.drawText(int(x), int(header_y), header)
        
        painter.setPen(QColor(40, 40, 45))
        painter.drawLine(draw_rect.left(), int(header_y + 10), draw_rect.right(), int(header_y + 10))
        
        grid_color = QColor(30, 30, 35, 80)
        painter.setPen(grid_color)
        
        for col_idx in range(4):
            x_line = draw_rect.left() + (draw_rect.width() * col_positions[col_idx][0] + col_positions[col_idx][1])
            painter.drawLine(int(x_line), int(draw_rect.top() + 20), int(x_line), int(draw_rect.bottom()))
        
        price_range = self._max_p - self._min_p
        step = price_range / num_rows
        
        numeric_font = QFont("JetBrains Mono" if "JetBrains Mono" in QFontDatabase().families() else "monospace")
        numeric_font.setPointSize(base_font_size)
        numeric_font.setBold(False)
        painter.setFont(numeric_font)
        
        for row_index in range(num_rows):
            price_level = self._max_p - (row_index * step)
            
            y = start_y + (row_index * row_height)
            
            if y > vp_max_y + 5 or y < vp_min_y - 5:
                continue
            
            painter.setPen(grid_color)
            painter.drawLine(int(draw_rect.left()), int(y + row_height), int(draw_rect.right()), int(y + row_height))
            
            bid_vol = 0.0
            ask_vol = 0.0
            
            price_key = round(price_level / self.tick_size) * self.tick_size
            if price_key in self.price_levels:
                data = self.price_levels[price_key]
                bid_vol = data.get('bid_vol', 0.0)
                ask_vol = data.get('ask_vol', 0.0)
            
            net_delta = bid_vol - ask_vol
            
            if self.poc_price > 0 and self.poc_price > 0:
                dpoc_dist = ((price_level - self.poc_price) / self.poc_price) * 100
            else:
                dpoc_dist = 0.0
            
            is_last_price = abs(price_level - self.current_price) < self.tick_size
            is_dpoc_row = self.poc_price > 0 and abs(price_level - self.poc_price) < self.tick_size
            
            x1 = draw_rect.left() + (draw_rect.width() * col_positions[0][0])
            if is_last_price:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 255, 102, 40))
                painter.drawRect(int(draw_rect.left()), int(y), int(draw_rect.width() * 0.20), int(row_height))
                painter.setPen(QColor(0, 255, 102))
                painter.setBrush(Qt.NoBrush)
            else:
                painter.setPen(QColor(180, 180, 180))
            painter.drawText(int(x1), int(y + row_height * 0.65), f"${price_level:,.0f}")
            
            x2 = draw_rect.left() + (draw_rect.width() * col_positions[1][0])
            if bid_vol > 0:
                bid_color = QColor(187, 0, 255) if bid_vol > 2.0 else QColor(187, 0, 255)
                painter.setPen(bid_color)
                bv_text = f"{bid_vol:.2f}" if bid_vol >= 1 else f"{bid_vol:.3f}"
                painter.drawText(int(x2), int(y + row_height * 0.65), bv_text)
            else:
                painter.setPen(QColor(80, 80, 80))
                painter.drawText(int(x2), int(y + row_height * 0.65), "0.00")
            
            x3 = draw_rect.left() + (draw_rect.width() * col_positions[2][0])
            if ask_vol > 0:
                ask_color = QColor(180, 100, 200) if ask_vol > 2.0 else QColor(160, 80, 180)
                painter.setPen(ask_color)
                av_text = f"{ask_vol:.2f}" if ask_vol >= 1 else f"{ask_vol:.3f}"
                painter.drawText(int(x3), int(y + row_height * 0.65), av_text)
            else:
                painter.setPen(QColor(80, 80, 80))
                painter.drawText(int(x3), int(y + row_height * 0.65), "0.00")
            
            x4 = draw_rect.left() + (draw_rect.width() * col_positions[3][0])
            if net_delta >= 0:
                delta_color = QColor(0, 255, 136)
            else:
                delta_color = QColor(187, 0, 255)
            painter.setPen(delta_color)
            delta_sign = "+" if net_delta >= 0 else ""
            delta_text = f"{delta_sign}{net_delta:.2f}"
            painter.drawText(int(x4), int(y + row_height * 0.65), delta_text)
            
            x5 = draw_rect.left() + (draw_rect.width() * col_positions[4][0])
            if is_dpoc_row:
                pulse_alpha = getattr(self, 'pulse_alpha', 120)
                bg_color = QColor(0, 255, 102, max(40, pulse_alpha))
                painter.setPen(Qt.NoPen)
                painter.setBrush(bg_color)
                painter.drawRect(int(x5 - 5), int(y), int(draw_rect.width() * 0.26), int(row_height))
                
                glow_color = QColor(0, 255, 102, min(255, pulse_alpha + 100))
                painter.setPen(QPen(glow_color, 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(int(x5 - 5), int(y), int(draw_rect.width() * 0.26), int(row_height))
                
                painter.setPen(QColor(0, 255, 102))
                dpoc_text = f"0.00% (0)"
            else:
                dpoc_text = f"{dpoc_dist:+.2f}% ({abs(int(price_level - self.poc_price)) if self.poc_price > 0 else 0})"
                painter.setPen(QColor(255, 204, 0))
            painter.drawText(int(x5), int(y + row_height * 0.65), dpoc_text)
        
        if self.poc_price > 0:
            poc_y = start_y + ((self._max_p - self.poc_price) / step * row_height) if step > 0 else draw_rect.bottom()
            if vp_min_y <= poc_y <= vp_max_y:
                pulse_alpha = getattr(self, 'pulse_alpha', 120)
                line_color = QColor(0, 255, 102, min(255, 100 + pulse_alpha // 3))
                painter.setPen(QPen(line_color, 2))
                painter.drawLine(draw_rect.left(), int(poc_y), draw_rect.right(), int(poc_y))
                painter.setPen(QColor(0, 255, 102))
                
                font_bold = QFont(painter.font())
                font_bold.setBold(True)
                painter.setFont(font_bold)
                painter.drawText(draw_rect.right() - 45, int(poc_y) - 3, "◉ dPOC")
        
        if self.vah_price > 0:
            vah_y = start_y + ((self._max_p - self.vah_price) / step * row_height) if step > 0 else draw_rect.bottom()
            if vp_min_y <= vah_y <= vp_max_y:
                painter.setPen(QPen(QColor(0, 255, 102, 100), 1, Qt.DotLine))
                painter.drawLine(draw_rect.left(), int(vah_y), draw_rect.right(), int(vah_y))
        
        painter.end()
        return pm

    def paintEvent(self, event):
        super().paintEvent(event)
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        painter.fillRect(self.rect(), QColor("#000000"))
        
        if not self.price_levels and not self.order_state:
            painter.setPen(QColor(COLORS['text_secondary']))
            font = painter.font()
            font.setPointSize(12)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, "WAITING FOR ORDER FLOW DATA...")
            return
        
        draw_rect = self.rect().adjusted(10, 30, -10, -10)
        h = draw_rect.height()
        
        num_rows = max(15, min(40, h // 18))
        
        base_font_size = max(8, min(14, int(10 * self.y_scale_factor)))
        font = painter.font()
        font.setPointSize(base_font_size)
        font.setBold(True)
        
        ps = self.tick_size * num_rows
        
        current_state_hash = (
            self.y_scale_factor, self.y_scroll_offset, 
            draw_rect.width(), draw_rect.height(),
            self.current_price, self.poc_price,
            len(self.price_levels)
        )
        
        if self.last_buffer_state != current_state_hash or not self.bg_buffer:
            self.bg_buffer = self._render_static_layer(draw_rect, h, ps, base_font_size, font, num_rows)
            self.last_buffer_state = current_state_hash
        
        if self.bg_buffer:
            painter.drawPixmap(0, 0, self.bg_buffer)

    def get_dPOC(self):
        return self.poc_price

    def get_current_price(self):
        return self.current_price

    def get_orderbook_imbalance(self):
        return getattr(self, 'orderbook_imbalance', 0.0)

    def get_price_levels_data(self):
        return self.price_levels


class GalaxyOrderFlowChart(QOpenGLWidget):
    """Professional Footprint + Candlestick chart with Order Flow grid.
    
    Draws Japanese candlesticks with a Footprint-style BID×ASK volume grid overlay.
    Shows Volume Profile sidebar, per-candle delta bars, and bounce zone detection.
    Similar to ATAS / Sierra Chart / Bookmap professional trading platforms.
    
    Optimizations:
    - VOLUME_THRESHOLD: Filters retail noise below configurable BTC volume.
    - Y-Axis Zoom: Mouse wheel / +- keys dynamically scale vertical resolution.
    - Heatmap Meter: 3-tier color intensity (Retail/Medium/Whale) per cell.
    """
    def __init__(self, title="GALAXY ORDER FLOW", parent=None):
        super().__init__(parent)
        self.title = title
        self.klines = []
        self.max_candles = 30  # 30 candles para mejor visualización
        self.indicators = {}
        self.bounce_zones = []
        self.order_state = None
        self.trade_grid = {}
        self.raw_trades = []
        self.tick_size = 10.0
        self.setMinimumHeight(350)
        self.predicted_candles = []
        self.num_predictions = 5
        
        self.y_scale_factor = 1.0
        self.y_scroll_offset = 0.0
        self.x_scroll_offset = 0
        self.all_klines = []
        self.show_footprint_numbers = False
        
        # ── RENDERING CACHES ──
        self.text_cache = {}
        self.bg_buffer = None
        self.last_buffer_state = None
        
        # New State for Animations and Indicators
        self.entry_state = None
        self.visual_pulses = []
        
        # ── dPOC history (last 20 candles for dynamic coloring + trail) ──
        self._dpoc_history = deque(maxlen=20)      # (price, timestamp)
        self._dpoc_5m_ago = None                   # price from 5 candles back
        
        # ── EMA band pre-compute cache ──
        self._ema9_cache: list[float] = []
        self._ema21_cache: list[float] = []
        self._ema_cache_hash = None                # invalidate when klines change
        
        # ── Imbalance circles buffer (pre-computed outside paintEvent) ──
        self._imbalance_circles: list[dict] = []   # [{x, y, side, alpha}, ...]
        
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.update)
        self.anim_timer.start(33)  # ~30 FPS
        
        # Panning state
        self.is_panning = False
        self.last_mouse_pos = None
        
        self.setFocusPolicy(Qt.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, False)
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)
        
        # ── CONTROLES Y HEADER ──
        header_layout = QHBoxLayout()
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-weight: bold; font-size: 14px; border: none; background: transparent;")
        header_layout.addWidget(self.title_label)
        
        header_layout.addStretch()
        
        btn_style = "background: #222; color: #fff; font-weight: bold; font-size: 14px; padding: 4px 10px; border-radius: 4px;"
        
        btn_y_in = QPushButton("⇕+")
        btn_y_in.setToolTip("Zoom In (Eje Y)")
        btn_y_in.setStyleSheet(btn_style)
        btn_y_in.clicked.connect(lambda: self.adjust_y_zoom(0.1))
        
        btn_y_out = QPushButton("⇕-")
        btn_y_out.setToolTip("Zoom Out (Eje Y)")
        btn_y_out.setStyleSheet(btn_style)
        btn_y_out.clicked.connect(lambda: self.adjust_y_zoom(-0.1))
        
        btn_x_in = QPushButton("◂▸")
        btn_x_in.setToolTip("Zoom In X (Footprint)")
        btn_x_in.setStyleSheet(btn_style)
        btn_x_in.clicked.connect(lambda: self.adjust_x_zoom(-2))
        
        btn_x_out = QPushButton("▸◂")
        btn_x_out.setToolTip("Zoom Out X (Tendencia)")
        btn_x_out.setStyleSheet(btn_style)
        btn_x_out.clicked.connect(lambda: self.adjust_x_zoom(2))
        
        btn_up = QPushButton("▲")
        btn_up.setToolTip("Desplazar Arriba")
        btn_up.setStyleSheet(btn_style)
        btn_up.clicked.connect(lambda: self.adjust_y_pan(50))
        
        btn_dn = QPushButton("▼")
        btn_dn.setToolTip("Desplazar Abajo")
        btn_dn.setStyleSheet(btn_style)
        btn_dn.clicked.connect(lambda: self.adjust_y_pan(-50))
        
        btn_rst = QPushButton("⟲")
        btn_rst.setToolTip("Auto-Centrar")
        btn_rst.setStyleSheet(btn_style)
        btn_rst.clicked.connect(self.reset_zoom)
        
        for b in [btn_y_in, btn_y_out, btn_x_in, btn_x_out, btn_up, btn_dn, btn_rst]:
            b.setCursor(Qt.PointingHandCursor)
            header_layout.addWidget(b)
            
        layout.addLayout(header_layout)
        layout.addStretch()
        self.setLayout(layout)
    
    def adjust_y_zoom(self, amount):
        ZOOM_MAX, ZOOM_MIN = 3.0, 0.2
        self.y_scale_factor = max(ZOOM_MIN, min(ZOOM_MAX, self.y_scale_factor + amount))
        self._update_title()
        self.update()
        
    def adjust_x_zoom(self, amount):
        self.max_candles = max(10, min(200, self.max_candles + amount))
        self._slice_klines()
        self.update()
        
    def adjust_y_pan(self, amount):
        self.y_scroll_offset += amount
        self.update()
        
    def reset_zoom(self):
        self.y_scale_factor = 1.0
        self.y_scroll_offset = 0.0
        self.x_scroll_offset = 0
        self.max_candles = 50
        self._update_title()
        self._slice_klines()
        self.update()
    
    def mousePressEvent(self, event):
        """Grab focus on click so wheel/keyboard events work on this widget."""
        self.setFocus()
        if event.button() == Qt.LeftButton or event.button() == Qt.RightButton:
            self.is_panning = True
            self.last_mouse_pos = event.pos()
        super().mousePressEvent(event)
        
    def mouseMoveEvent(self, event):
        if self.is_panning and self.last_mouse_pos is not None:
            dy = event.pos().y() - self.last_mouse_pos.y()
            dx = event.pos().x() - self.last_mouse_pos.x()
            
            # Y panning
            self.y_scroll_offset += dy * (self.tick_size / 5)
            
            # X panning
            if abs(dx) > 10:
                candles_to_shift = int(dx / 10)
                self.x_scroll_offset += candles_to_shift
                self.x_scroll_offset = max(0, self.x_scroll_offset) # no podemos ver el futuro
                if hasattr(self, 'all_klines') and self.all_klines:
                    max_offset = max(0, len(self.all_klines) - self.max_candles)
                    self.x_scroll_offset = min(max_offset, self.x_scroll_offset)
                
                self._slice_klines()
                # Actualizar el punto de referencia solo si cruzamos el umbral para no perder fluidez
                self.last_mouse_pos = QPointF(event.pos().x(), self.last_mouse_pos.y())
                
            self.last_mouse_pos = QPointF(self.last_mouse_pos.x(), event.pos().y())
            self.update()
        super().mouseMoveEvent(event)
        
    def mouseReleaseEvent(self, event):
        self.is_panning = False
        self.last_mouse_pos = None
        super().mouseReleaseEvent(event)
    
    def _update_title(self):
        """Update title with current zoom level indicator."""
        zoom_pct = int(self.y_scale_factor * 100)
        if zoom_pct == 100:
            self.title_label.setText(self.title)
        else:
            self.title_label.setText(f"{self.title}  🔍 {zoom_pct}%")

    def update_indicators(self, data):
        current_trend = data.get('trend', 'NEUTRAL')
        if not hasattr(self, 'last_trend'):
            self.last_trend = current_trend
            
        # Detect signal transition to plot Entry Point
        if self.last_trend != current_trend:
            if 'UPTREND' in current_trend and 'UPTREND' not in self.last_trend:
                price = self.order_state.get('price', 0) if hasattr(self, 'order_state') and self.order_state else 0
                self.trigger_entry('BUY', price)
            elif 'DOWNTREND' in current_trend and 'DOWNTREND' not in self.last_trend:
                price = self.order_state.get('price', 0) if hasattr(self, 'order_state') and self.order_state else 0
                self.trigger_entry('SELL', price)
        self.last_trend = current_trend

        self.indicators = {
            'rsi': data.get('rsi', 50), 'cvd': data.get('cvd', 0),
            'trend': current_trend, 'macd_hist': data.get('macd_hist', 0),
            'delta': data.get('delta', 0), 'buy_volume': data.get('buy_volume', 0),
            'sell_volume': data.get('sell_volume', 0), 'atr': data.get('atr', 0),
            'vwap': data.get('vwap', 0)
        }

    def trigger_entry(self, side, price):
        if not price: return
        import time
        self.entry_state = {'type': side, 'price': price, 'time': time.time()}
        color = QColor(0, 255, 102) if side == 'BUY' else QColor(187, 0, 255)
        idx = len(self.klines) - 1 if hasattr(self, 'klines') and self.klines else 0
        self.visual_pulses.append({'idx': idx, 'price': price, 'color': color, 'start': time.time()})

    def update_klines(self, klines):
        if not klines: return
        self.all_klines = klines
        self._slice_klines()

    def _slice_klines(self):
        if not hasattr(self, 'all_klines') or not self.all_klines: return
        end_idx = len(self.all_klines) - self.x_scroll_offset
        if end_idx <= 0:
            end_idx = 1
        start_idx = max(0, end_idx - self.max_candles)
        self.klines = self.all_klines[start_idx:end_idx]
        self._build_footprint()

    def update_trades(self, trades):
        if not trades: return
        self.raw_trades.extend(trades)
        if len(self.raw_trades) > 2000:
            self.raw_trades = self.raw_trades[-2000:]

    def _build_footprint(self):
        self.trade_grid = {}
        self.session_profile = {}
        self.candle_absorptions = {}
        
        for idx, k in enumerate(self.klines):
            o, hi, lo, c, vol = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            candle_time = int(k[0])
            is_bullish = c >= o
            
            band_lo = (lo // self.tick_size) * self.tick_size
            band_hi = ((hi // self.tick_size) + 1) * self.tick_size
            bands = {}
            current = band_lo
            count = 0
            while current <= band_hi:
                bands[current] = {'bid_vol': 0, 'ask_vol': 0}
                current += self.tick_size
                count += 1
                if count > 50: break
            
            if not bands: continue
            
            candle_trades = [t for t in self.raw_trades if candle_time <= t['time'] < candle_time + 60000]
            candle_delta = 0
            
            if candle_trades:
                for t in candle_trades:
                    band = (t['price'] // self.tick_size) * self.tick_size
                    qty = t['quantity']
                    if band in bands:
                        if t['is_buyer_maker']:
                            bands[band]['bid_vol'] += qty
                            candle_delta -= qty
                        else:
                            bands[band]['ask_vol'] += qty
                            candle_delta += qty
                    self.session_profile[band] = self.session_profile.get(band, 0) + qty
            else:
                per_band = vol / max(1, len(bands))
                for bp in bands:
                    dist = abs(bp - c)
                    weight = max(0.2, 1.0 - (dist / max(1, hi - lo)))
                    wv = per_band * weight
                    if is_bullish:
                        bands[bp]['ask_vol'] = wv * 0.6
                        bands[bp]['bid_vol'] = wv * 0.4
                        candle_delta += wv * 0.2
                    else:
                        bands[bp]['bid_vol'] = wv * 0.6
                        bands[bp]['ask_vol'] = wv * 0.4
                        candle_delta -= wv * 0.2
                    self.session_profile[bp] = self.session_profile.get(bp, 0) + wv
            
            # Detect absorption: price moving opposite to massive delta
            if is_bullish and candle_delta < -2.0:
                self.candle_absorptions[idx] = ('BUY_ABSORPTION', lo)
            elif not is_bullish and candle_delta > 2.0:
                self.candle_absorptions[idx] = ('SELL_ABSORPTION', hi)
                
            self.trade_grid[idx] = bands
            
        # Calculate Daily POC, VAH, VAL
        if self.session_profile:
            self.poc_price = max(self.session_profile.items(), key=lambda x: x[1])[0]
            self._dpoc_history.append(self.poc_price)
            self._dpoc_5m_ago = self._dpoc_history[-5] if len(self._dpoc_history) >= 5 else None
            total_vol = sum(self.session_profile.values())
            target_vol = total_vol * 0.70
            
            current_vol = self.session_profile[self.poc_price]
            sorted_prices = sorted(self.session_profile.keys())
            poc_idx = sorted_prices.index(self.poc_price)
            up_idx = poc_idx + 1
            dn_idx = poc_idx - 1
            
            self.vah = self.poc_price
            self.val = self.poc_price
            
            while current_vol < target_vol and (up_idx < len(sorted_prices) or dn_idx >= 0):
                up_vol = self.session_profile[sorted_prices[up_idx]] if up_idx < len(sorted_prices) else -1
                dn_vol = self.session_profile[sorted_prices[dn_idx]] if dn_idx >= 0 else -1
                
                if up_vol > dn_vol:
                    current_vol += up_vol
                    self.vah = sorted_prices[up_idx]
                    up_idx += 1
                else:
                    current_vol += dn_vol
                    self.val = sorted_prices[dn_idx]
                    dn_idx -= 1

    def update_data(self, order_book, current_price):
        if not order_book: return
        bids = sorted([(float(p), float(q)) for p, q in order_book.get('bids', []) if float(q) > 0.3], key=lambda x: x[1], reverse=True)[:30]
        asks = sorted([(float(p), float(q)) for p, q in order_book.get('asks', []) if float(q) > 0.3], key=lambda x: x[1], reverse=True)[:30]
        self.order_state = {'bids': bids, 'asks': asks, 'price': current_price}
        self._calculate_bounce_zones(current_price, bids, asks)
        self._predict_future_candles()
        self.update()

    def _predict_future_candles(self):
        """Generate predictive ghost candles based on Order Flow + Price Action.
        
        Scoring System:
        - Order Book Imbalance: bid_vol vs ask_vol ratio (30%)
        - Delta Momentum: recent trade delta direction (25%)
        - RSI Extreme Zones: oversold/overbought reversal signals (15%)
        - Trend Alignment: EMA cross direction (15%)
        - ATR Volatility: scales the predicted candle size (15%)
        """
        self.predicted_candles = []
        if not self.klines or len(self.klines) < 5: return
        if not self.order_state: return
        
        # --- Calculate directional force ---
        # 1. Order Book Imbalance (30%)
        total_bids = sum(q for _, q in self.order_state.get('bids', []))
        total_asks = sum(q for _, q in self.order_state.get('asks', []))
        ob_total = total_bids + total_asks + 0.001
        ob_force = ((total_bids / ob_total) - 0.5) * 2  # -1 to +1
        
        # 2. Delta Momentum (25%)
        buy_vol = self.indicators.get('buy_volume', 0)
        sell_vol = self.indicators.get('sell_volume', 0)
        delta_total = buy_vol + sell_vol + 0.001
        delta_force = ((buy_vol / delta_total) - 0.5) * 2  # -1 to +1
        
        # 3. RSI Signal (15%)
        rsi = self.indicators.get('rsi', 50)
        if rsi < 30: rsi_force = 0.8   # Oversold = likely bounce up
        elif rsi < 40: rsi_force = 0.3
        elif rsi > 70: rsi_force = -0.8  # Overbought = likely drop
        elif rsi > 60: rsi_force = -0.3
        else: rsi_force = 0
        
        # 4. Trend (15%)
        trend = self.indicators.get('trend', 'NEUTRAL')
        if trend == 'ALCISTA': trend_force = 0.6
        elif trend == 'BAJISTA': trend_force = -0.6
        else: trend_force = 0
        
        # 5. MACD Histogram (momentum confirmation)
        macd_h = self.indicators.get('macd_hist', 0)
        macd_force = max(-1, min(1, macd_h * 50))  # Normalize
        
        # Composite force: weighted average
        composite = (ob_force * 0.30) + (delta_force * 0.25) + (rsi_force * 0.15) + (trend_force * 0.15) + (macd_force * 0.15)
        composite = max(-1, min(1, composite))  # Clamp
        
        # Confidence percentage
        confidence = abs(composite) * 100
        direction = 'PUMP' if composite > 0 else 'DUMP'
        
        # ATR for candle size
        atr = self.indicators.get('atr', 0)
        if atr == 0:
            # Estimate from last 5 candles
            ranges = [float(self.klines[i][2]) - float(self.klines[i][3]) for i in range(-5, 0)]
            atr = sum(ranges) / len(ranges) if ranges else 10
        
        # Generate prediction candles
        last_close = float(self.klines[-1][4])
        momentum = composite  # Decays slightly each candle
        
        for i in range(self.num_predictions):
            # Each subsequent candle has slightly less certainty
            decay = 1.0 - (i * 0.15)
            force = momentum * decay
            
            move = atr * force * 0.5  # Half ATR per candle
            wick_ext = atr * abs(force) * 0.3  # Wick extension
            
            pred_open = last_close
            pred_close = pred_open + move
            
            if force > 0:  # Bullish
                pred_high = max(pred_open, pred_close) + wick_ext
                pred_low = min(pred_open, pred_close) - (wick_ext * 0.3)
            else:  # Bearish
                pred_high = max(pred_open, pred_close) + (wick_ext * 0.3)
                pred_low = min(pred_open, pred_close) - wick_ext
            
            self.predicted_candles.append({
                'o': pred_open, 'h': pred_high, 'l': pred_low, 'c': pred_close,
                'direction': direction,
                'confidence': confidence * decay,
            })
            
            last_close = pred_close
            # Slight random-like variation (deterministic based on force)
            momentum *= 0.85  # Momentum decays

    def _calculate_bounce_zones(self, current_price, bids, asks):
        band_size = 20.0
        clusters = {}
        for p, q in bids:
            band = round(p / band_size) * band_size
            if band not in clusters: clusters[band] = {'vol': 0, 'count': 0, 'side': 'LONG'}
            clusters[band]['vol'] += q; clusters[band]['count'] += 1
        for p, q in asks:
            band = round(p / band_size) * band_size
            if band not in clusters: clusters[band] = {'vol': 0, 'count': 0, 'side': 'SHORT'}
            clusters[band]['vol'] += q; clusters[band]['count'] += 1
        if not clusters: return
        max_vol = max(c['vol'] for c in clusters.values()) or 1
        max_count = max(c['count'] for c in clusters.values()) or 1
        rsi = self.indicators.get('rsi', 50)
        cvd = self.indicators.get('cvd', 0)
        trend = self.indicators.get('trend', 'NEUTRAL')
        scored = []
        for price, data in clusters.items():
            s = (data['vol'] / max_vol) * 50 + (data['count'] / max_count) * 20
            if data['side'] == 'LONG':
                if rsi < 35: s += 10
                if cvd > 0: s += 10
                if trend == 'ALCISTA': s += 10
            else:
                if rsi > 65: s += 10
                if cvd < 0: s += 10
                if trend == 'BAJISTA': s += 10
            scored.append({'price': price, 'score': s, 'vol': data['vol'], 'side': data['side']})
        scored.sort(key=lambda x: x['score'], reverse=True)
        self.bounce_zones = scored[:6]


    def _get_cached_text(self, text, font, color):
        if len(self.text_cache) > 2000:
            self.text_cache.clear()
        key = (text, font.pointSize(), color.name())
        if key not in self.text_cache:
            fm = self.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            pm = QPixmap(max(1, tw), max(1, th))
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setFont(font)
            p.setPen(color)
            p.drawText(0, fm.ascent(), text)
            p.end()
            self.text_cache[key] = pm
        return self.text_cache[key]

    def _render_static_layer(self, draw_rect, cw, min_p, max_p, ps, h, fp_max, tier_medium, tier_whale, base_font_size, font, vp_w, candle_zone_w, fp_zone_w, bw):
        pm = QPixmap(self.size())
        pm.fill(QColor("#08080a"))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        
        vp_min_x = draw_rect.left()
        vp_max_x = draw_rect.right()
        vp_min_y = draw_rect.top()
        vp_max_y = draw_rect.bottom()
        w = draw_rect.width()
        nc = len(self.klines)
        
        def py(p): return draw_rect.bottom() - ((p - min_p) / ps * h)

        # 1. CAPA 1: FONDO Y CUADRICULA PREMIUM (Base Layer)
        scale_font = QFont(font)
        scale_font.setPointSize(7); scale_font.setBold(False); painter.setFont(scale_font)
        
        grid_pen = QPen(QColor(255, 255, 255, 15), 1, Qt.DotLine)
        
        # Horizontal Grid & Price Scale
        for t in range(9):
            tp = min_p + ps * (t / 8)
            ty = py(tp)
            painter.setPen(grid_pen)
            painter.drawLine(draw_rect.left(), int(ty), draw_rect.right() + vp_w, int(ty))
            
            painter.setPen(QColor(COLORS['text_secondary']))
            painter.drawText(self.rect().left() + 2, int(ty) + 3, f"${tp:,.0f}")
            
        # Vertical Grid (Time/Candle steps)
        painter.setPen(grid_pen)
        for idx in range(nc):
            x = draw_rect.left() + (idx * cw)
            if draw_rect.left() <= x <= draw_rect.right():
                painter.drawLine(int(x), draw_rect.top(), int(x), draw_rect.bottom())

        # 2. CAPA 2: LINEAS DE LIQUIDEZ TRANSLUCIDAS (Whale Order Book Layer)
        if hasattr(self, 'order_state') and self.order_state:
            all_bids = self.order_state.get('bids', [])
            all_asks = self.order_state.get('asks', [])
            max_vol = max((q for p, q in all_bids + all_asks), default=1)
            
            for p, q in all_bids:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    alpha = min(255, max(5, int((q / max_vol) * 255)))
                    painter.setPen(QPen(QColor(0, 255, 102, alpha), 1, Qt.SolidLine))
                    painter.drawLine(draw_rect.left(), int(y), draw_rect.right() + vp_w, int(y))
                    
            for p, q in all_asks:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    alpha = min(255, max(5, int((q / max_vol) * 255)))
                    painter.setPen(QPen(QColor(187, 0, 255, alpha), 1, Qt.SolidLine))
                    painter.drawLine(draw_rect.left(), int(y), draw_rect.right() + vp_w, int(y))

        # 2.2 SESSION POC, VAH, VAL
        if hasattr(self, 'poc_price') and self.poc_price:
            if min_p <= self.poc_price <= max_p:
                poc_y = py(self.poc_price)
                if vp_min_y <= poc_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 102, 60), 1, Qt.DotLine))
                    painter.drawLine(draw_rect.left(), int(poc_y), draw_rect.right() + vp_w, int(poc_y))
                    painter.setPen(QColor(0, 255, 102, 100))
                    painter.drawText(draw_rect.right() + vp_w - 40, int(poc_y) - 2, "dPOC")
                
            if hasattr(self, 'vah') and min_p <= self.vah <= max_p:
                vah_y = py(self.vah)
                if vp_min_y <= vah_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 102, 100), 1, Qt.DotLine))
                    painter.drawLine(draw_rect.left(), int(vah_y), draw_rect.right() + vp_w, int(vah_y))
                    painter.drawText(draw_rect.right() + vp_w - 30, int(vah_y) - 2, "VAH")
            
            if hasattr(self, 'val') and min_p <= self.val <= max_p:
                val_y = py(self.val)
                if vp_min_y <= val_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 102, 100), 1, Qt.DotLine))
                    painter.drawLine(draw_rect.left(), int(val_y), draw_rect.right() + vp_w, int(val_y))
                    painter.drawText(draw_rect.right() + vp_w - 30, int(val_y) - 2, "VAL")

        # 2.3 VWAP LINE
        vwap = self.indicators.get('vwap', 0)
        if vwap and min_p <= vwap <= max_p:
            vwap_y = py(vwap)
            if vp_min_y <= vwap_y <= vp_max_y:
                painter.setPen(QPen(QColor(255, 204, 0, 200), 2, Qt.DashLine))
                painter.drawLine(draw_rect.left(), int(vwap_y), draw_rect.right() + vp_w, int(vwap_y))
                painter.setPen(QColor(255, 204, 0))
                painter.drawText(draw_rect.left() + 2, int(vwap_y) - 2, "VWAP")

        # 3. FOOTPRINT CELLS - HISTORICAL
        painter.setFont(font)
        for idx in range(nc - 1):
            xl_cell = draw_rect.left() + (idx * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
                
            if idx not in self.trade_grid: continue
            bands = self.trade_grid[idx]
            xl_fp = xl_cell + candle_zone_w
            
            candle_max_vol = 0.001
            poc_bp = None
            for bp, vols in bands.items():
                if not (min_p <= bp <= max_p): continue
                tot = vols['bid_vol'] + vols['ask_vol']
                if tot > candle_max_vol:
                    candle_max_vol = tot
                    poc_bp = bp
            
            for bp, vols in bands.items():
                if not (min_p <= bp <= max_p): continue
                y = py(bp)
                if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                
                yb = py(bp - self.tick_size)
                ch = max(4, abs(yb - y))
                bv = vols['bid_vol']; av = vols['ask_vol']
                total_vol = bv + av
                delta = av - bv
                
                if total_vol < 0.01: continue
                
                # CAPA 4: MATRIZ NUMERICA (Footprint Text Layer)
                show_numbers = getattr(self, 'show_footprint_numbers', False)
                if not show_numbers:
                    # POC power bar replaces the old yellow box
                    if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                        bar_max_w = max(3, int(fp_zone_w * 0.35))
                        p_ratio = total_vol / max(candle_max_vol, 0.001)
                        bar_w = max(2, int(bar_max_w * p_ratio))
                        bar_x = int(xl_fp) + int(fp_zone_w) - bar_w - 1
                        if delta > 0:
                            c = QColor(187, 0, 255, 180) if total_vol < tier_medium else QColor(170, 50, 255, 200) if total_vol < tier_whale else QColor(150, 0, 255, 220)
                        else:
                            c = QColor(0, 255, 102, 180) if total_vol < tier_medium else QColor(0, 255, 80, 200) if total_vol < tier_whale else QColor(0, 255, 50, 220)
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(c)
                        painter.drawRect(bar_x, int(y) - int(ch // 2) + 2, bar_w, max(2, int(ch) - 4))
                    continue

                center_x = xl_fp + (fp_zone_w / 2)
                bid_w = (bv / candle_max_vol) * (fp_zone_w / 2)
                ask_w = (av / candle_max_vol) * (fp_zone_w / 2)
                
                bg_alpha = 60
                if total_vol >= tier_whale: bg_alpha = 180
                elif total_vol >= tier_medium: bg_alpha = 100
                elif total_vol < VOLUME_THRESHOLD: bg_alpha = 20
                
                painter.setPen(Qt.NoPen)
                if bid_w > 0:
                    painter.setBrush(QColor(187, 0, 255, bg_alpha))
                    painter.drawRect(int(center_x - bid_w), int(y) - int(ch / 2) + 1, int(bid_w), int(ch) - 2)
                if ask_w > 0:
                    painter.setBrush(QColor(0, 255, 102, bg_alpha))
                    painter.drawRect(int(center_x), int(y) - int(ch / 2) + 1, int(ask_w), int(ch) - 2)
                
                if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                    bar_max_w = max(3, int(fp_zone_w * 0.35))
                    p_ratio = total_vol / max(candle_max_vol, 0.001)
                    bar_w = max(2, int(bar_max_w * p_ratio))
                    bar_x = int(xl_fp) + int(fp_zone_w) - bar_w - 1
                    if delta > 0:
                        c = QColor(187, 0, 255, 180) if total_vol < tier_medium else QColor(170, 50, 255, 200) if total_vol < tier_whale else QColor(150, 0, 255, 220)
                    else:
                        c = QColor(0, 255, 102, 180) if total_vol < tier_medium else QColor(0, 255, 80, 200) if total_vol < tier_whale else QColor(0, 255, 50, 220)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(c)
                    painter.drawRect(bar_x, int(y) - int(ch // 2) + 2, bar_w, max(2, int(ch) - 4))
                
                if total_vol >= tier_whale:
                    glow_color = QColor(0, 255, 102) if delta > 0 else QColor(187, 0, 255)
                    painter.setPen(QPen(glow_color, 1))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(fp_zone_w), int(ch))
                
                if total_vol >= VOLUME_THRESHOLD and fp_zone_w > 15 and ch > (base_font_size - 2):
                    bt = f"{bv:.0f}" if bv >= 1 else f"{bv:.1f}"
                    at = f"{av:.0f}" if av >= 1 else f"{av:.1f}"
                    bid_color = QColor(187, 0, 255); ask_color = QColor(0, 255, 102)
                    if av > bv * 3 and av > tier_medium: ask_color = QColor(255, 255, 0)
                    if bv > av * 3 and bv > tier_medium: bid_color = QColor(255, 255, 0)
                    if total_vol >= tier_whale: bid_color = QColor(255, 255, 255); ask_color = QColor(255, 255, 255)
                    
                    pm_b = self._get_cached_text(bt, font, bid_color)
                    painter.drawPixmap(int(center_x - pm_b.width() - 3), int(y - pm_b.height()/2), pm_b)
                    pm_a = self._get_cached_text(at, font, ask_color)
                    painter.drawPixmap(int(center_x + 3), int(y - pm_a.height()/2), pm_a)

        # 4. CANDLESTICKS - HISTORICAL
        for i in range(nc - 1):
            xl_cell = draw_rect.left() + (i * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
                
            k = self.klines[i]
            o, hi, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            xc = xl_cell + (candle_zone_w / 2)
            yo = py(o); yc = py(c); yh = py(hi); yl = py(lo)
            # CAPA 3: VELAS JAPONESAS LIMPIAS (Candlestick Core Layer)
            bull = c >= o
            if bull: bc = QColor(0, 255, 102, 255); wc = QColor(0, 255, 102, 255)
            else: bc = QColor(187, 0, 255, 255); wc = QColor(187, 0, 255, 255)
            painter.setPen(QPen(wc, 1, Qt.SolidLine))
            painter.drawLine(int(xc), int(yh), int(xc), int(yl))
            bt = min(yo, yc); bh = max(1, abs(yo - yc))
            painter.setPen(QPen(wc, 1, Qt.SolidLine))
            painter.setBrush(bc)
            painter.drawRect(int(xc - bw / 2), int(bt), int(bw), int(bh))
            
            if hasattr(self, 'candle_absorptions') and i in self.candle_absorptions:
                abs_type, abs_price = self.candle_absorptions[i]
                abs_y = py(abs_price)
                if abs_y > vp_max_y + 10 or abs_y < vp_min_y - 10: continue
                if abs_type == 'BUY_ABSORPTION': 
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(187, 0, 255, 150))
                    painter.drawEllipse(QPointF(xc, abs_y + 10), 8, 8)
                else: 
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(0, 255, 102, 150))
                    painter.drawEllipse(QPointF(xc, abs_y - 10), 8, 8)

        # 6. PER-CANDLE DELTA BARS - HISTORICAL
        dh = 15; dy = draw_rect.bottom() - dh
        for idx in range(nc - 1):
            xl_cell = draw_rect.left() + (idx * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
            if idx not in self.trade_grid: continue
            bands = self.trade_grid[idx]
            tb = sum(v['bid_vol'] for v in bands.values())
            ta = sum(v['ask_vol'] for v in bands.values())
            d = ta - tb
            xc = xl_cell + (candle_zone_w / 2)
            dbh = min(dh, max(2, abs(d) / fp_max * dh))
            painter.setPen(Qt.NoPen)
            if d > 0:
                painter.setBrush(QColor(0, 255, 102, 140))
                painter.drawRect(int(xc - bw / 2), int(dy + dh - dbh), int(bw), int(dbh))
            else:
                painter.setBrush(QColor(187, 0, 255, 140))
                painter.drawRect(int(xc - bw / 2), int(dy), int(bw), int(dbh))

        painter.end()
        return pm

    def _precompute_ema_bands(self):
        """Pre-compute EMA 9 and EMA 21 from cached klines.

        Runs outside paintEvent; results stored in ``_ema9_cache`` and
        ``_ema21_cache``.  Cache is invalidated whenever klines change
        (checked via a simple kline-id hash).
        """
        if not self.klines or len(self.klines) < 3:
            self._ema9_cache = []
            self._ema21_cache = []
            return

        closes = [float(k[4]) for k in self.klines]
        klines_id = hash(tuple(closes[-3:]))  # last 3 closes
        if klines_id == self._ema_cache_hash and self._ema9_cache:
            return  # cache is fresh

        def ema(values: list[float], period: int) -> list[float]:
            if len(values) < period:
                return [values[-1]] * len(values)
            k = 2.0 / (period + 1)
            result = [values[0]]
            for v in values[1:]:
                result.append(v * k + result[-1] * (1.0 - k))
            return result

        self._ema9_cache = ema(closes, 9)
        self._ema21_cache = ema(closes, 21)
        self._ema_cache_hash = klines_id

    def _precompute_imbalance_circles(self, nc, cw, draw_rect, min_p, max_p, py):
        """Scan candles for volume/delta extremes and buffer circle draw data."""
        circles = []
        for idx in range(nc):
            if idx not in self.trade_grid:
                continue
            bands = self.trade_grid[idx]
            tb = sum(v['bid_vol'] for v in bands.values())
            ta = sum(v['ask_vol'] for v in bands.values())
            vol_mult = (tb + ta) / max(VOLUME_THRESHOLD, 0.001)
            delta = ta - tb
            if vol_mult > 3.0 or abs(delta) > 20:
                x = draw_rect.left() + (idx * cw) + (cw * 0.5)
                # Find POC price for this candle
                max_v = 0.0
                poc = 0.0
                for bp, vols in bands.items():
                    tot = vols['bid_vol'] + vols['ask_vol']
                    if tot > max_v:
                        max_v = tot
                        poc = bp
                if not (min_p <= poc <= max_p):
                    continue
                y = py(poc)
                circles.append({
                    'x': x, 'y': y,
                    'side': 'BUY' if delta > 0 else 'SELL',
                    'alpha': min(0.8, 0.3 + vol_mult * 0.1),
                    'radius': min(20, 8 + vol_mult * 2),
                })
        self._imbalance_circles = circles

    def _render_ema_cloud(self, painter, draw_rect, min_p, max_p, ps, h, nc, cw):
        """Shaded band between EMA 9 and EMA 21 using a filled polygon.

        Dark green (alpha 0.15) when EMA 9 > EMA 21 (bullish).
        Dark purple (alpha 0.15) when EMA 9 < EMA 21 (bearish).
        """
        self._precompute_ema_bands()
        if not self._ema9_cache or not self._ema21_cache:
            return
        if len(self._ema9_cache) < nc or len(self._ema21_cache) < nc:
            return

        def py(p):
            return draw_rect.bottom() - ((p - min_p) / ps * h)

        bullish = self._ema9_cache[-1] > self._ema21_cache[-1]
        fill_color = QColor(0, 80, 40, 38) if bullish else QColor(80, 0, 100, 38)

        # Build polygon: top edge = max(ema9, ema21), bottom = min(ema9, ema21)
        path = QPainterPath()
        first = True
        for i in range(nc):
            e9 = self._ema9_cache[i]
            e21 = self._ema21_cache[i]
            top = max(e9, e21)
            bot = min(e9, e21)
            if not (min_p <= top <= max_p or min_p <= bot <= max_p):
                continue
            x = draw_rect.left() + (i * cw) + (cw * 0.5)
            y_top = py(top)
            y_bot = py(bot)
            if first:
                path.moveTo(x, y_top)
                first = False
            else:
                path.lineTo(x, y_top)
        for i in range(nc - 1, -1, -1):
            e9 = self._ema9_cache[i]
            e21 = self._ema21_cache[i]
            top = max(e9, e21)
            bot = min(e9, e21)
            if not (min_p <= top <= max_p or min_p <= bot <= max_p):
                continue
            x = draw_rect.left() + (i * cw) + (cw * 0.5)
            y_bot = py(bot)
            path.lineTo(x, y_bot)
        path.closeSubpath()

        painter.setPen(Qt.NoPen)
        painter.setBrush(fill_color)
        painter.drawPath(path)

    def _render_dpoc_dynamic(self, painter, draw_rect, min_p, max_p, ps, h):
        """Dynamic dPOC line + dotted history trail.

        Color: magenta (#A020F0) if current dPOC < 5‑min‑ago dPOC,
               bright green (#00FF00) if current dPOC >= 5‑min‑ago dPOC.
        """
        if not hasattr(self, 'poc_price') or not self.poc_price:
            return
        if not (min_p <= self.poc_price <= max_p):
            return

        def py(p):
            return draw_rect.bottom() - ((p - min_p) / ps * h)

        y_poc = py(self.poc_price)

        # Dynamic color based on 5‑min comparison
        if self._dpoc_5m_ago is not None:
            dpoc_color = (QColor(0xA0, 0x20, 0xF0)       # magenta when falling
                          if self.poc_price < self._dpoc_5m_ago
                          else QColor(0x00, 0xFF, 0x00))   # green when rising
        else:
            dpoc_color = QColor(0x00, 0xFF, 0x00)

        # Main dPOC line
        painter.setPen(QPen(dpoc_color, 2, Qt.SolidLine))
        painter.drawLine(draw_rect.left(), int(y_poc),
                         draw_rect.right(), int(y_poc))

        # Label
        painter.setPen(dpoc_color)
        font = painter.font()
        bold = QFont(font); bold.setBold(True); bold.setPointSize(8)
        painter.setFont(bold)
        painter.drawText(draw_rect.right() - 55, int(y_poc) - 4,
                         f"dPOC ${self.poc_price:,.1f}")

        # Dotted history trail (last 20 dPOC values)
        if len(self._dpoc_history) >= 2:
            trail_pen = QPen(QColor(dpoc_color.red(), dpoc_color.green(),
                                     dpoc_color.blue(), 80), 1, Qt.DotLine)
            painter.setPen(trail_pen)
            n = len(self._dpoc_history)
            for i in range(1, n):
                prev = self._dpoc_history[i - 1]
                curr = self._dpoc_history[i]
                if not (min_p <= prev <= max_p and min_p <= curr <= max_p):
                    continue
                x1 = draw_rect.left() + int((i - 1) / n * draw_rect.width())
                x2 = draw_rect.left() + int(i / n * draw_rect.width())
                painter.drawLine(x1, int(py(prev)), x2, int(py(curr)))

    def _render_imbalance_circles(self, painter, min_p, max_p, ps, h, draw_rect):
        """Draw semi-transparent circles at imbalance/volume‑explosion points."""
        def py(p):
            return draw_rect.bottom() - ((p - min_p) / ps * h)

        for circ in self._imbalance_circles:
            x = circ['x']
            y = circ['y']
            r = circ['radius']
            alpha = circ['alpha']
            if circ['side'] == 'BUY':
                color = QColor(0, 255, 102, int(alpha * 255))
                glow = QColor(0, 255, 102, int(alpha * 80))
            else:
                color = QColor(0xA0, 0x20, 0xF0, int(alpha * 255))
                glow = QColor(0xA0, 0x20, 0xF0, int(alpha * 80))
            painter.setPen(QPen(color, 2))
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(x, y), r, r)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.klines or len(self.klines) < 2: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        vp_w = 40
        draw_rect = self.rect().adjusted(50, 30, -(vp_w + 5), -10)
        vp_x = draw_rect.right() + 3
        
        vp_min_x = draw_rect.left()
        vp_max_x = draw_rect.right()
        vp_min_y = draw_rect.top()
        vp_max_y = draw_rect.bottom()
        
        w = draw_rect.width()
        h = draw_rect.height()
        nc = len(self.klines)
        total_slots = nc + self.num_predictions
        cw = w / total_slots
        
        candle_zone_w = min(cw * 0.6, 25)
        fp_zone_w = cw - candle_zone_w
        bw = max(3, candle_zone_w * 0.8)
        
        live_candle_zone_w = candle_zone_w
        live_fp_zone_w = fp_zone_w
        live_bw = bw
        live_cell_w = cw
        
        all_hi = [float(k[2]) for k in self.klines]
        all_lo = [float(k[3]) for k in self.klines]
        for pc in self.predicted_candles:
            all_hi.append(pc['h'])
            all_lo.append(pc['l'])
        raw_hi = max(all_hi); raw_lo = min(all_lo)
        raw_span = raw_hi - raw_lo
        pad = raw_span * 0.08
        
        center_price = (raw_hi + raw_lo) / 2 + self.y_scroll_offset
        visible_half = (raw_span + pad * 2) / (2 * self.y_scale_factor)
        min_p = center_price - visible_half
        max_p = center_price + visible_half
        ps = max_p - min_p
        if ps == 0: return
        
        def py(p): return draw_rect.bottom() - ((p - min_p) / ps * h)

        fp_max = 0.001
        for bands in self.trade_grid.values():
            for vols in bands.values():
                fp_max = max(fp_max, vols['bid_vol'], vols['ask_vol'])
                
        tier_whale = VOLUME_THRESHOLD * WHALE_MULTIPLIER
        tier_medium = VOLUME_THRESHOLD * MEDIUM_MULTIPLIER
        base_font_size = max(6, min(12, int(8 * self.y_scale_factor)))
        font = painter.font()
        font.setPointSize(base_font_size); font.setBold(True)

        current_state_hash = (self.y_scale_factor, self.y_scroll_offset, self.x_scroll_offset, 
                              draw_rect.width(), draw_rect.height(), nc, self.width(), self.height(),
                              round(min_p, 2), round(max_p, 2), getattr(self, 'show_footprint_numbers', False),
                              id(self.order_state) if hasattr(self, 'order_state') else None)
        if self.last_buffer_state != current_state_hash or not self.bg_buffer:
            self.bg_buffer = self._render_static_layer(draw_rect, cw, min_p, max_p, ps, h, fp_max, tier_medium, tier_whale, base_font_size, font, vp_w, candle_zone_w, fp_zone_w, bw)
            self.last_buffer_state = current_state_hash

        # Draw offscreen buffer
        painter.drawPixmap(0, 0, self.bg_buffer)

        # ── PREMIUM OVERLAYS (pre-computed buffers, drawn every frame) ──
        # 1. Micro-trend cloud (EMA 9/21 band)
        self._render_ema_cloud(painter, draw_rect, min_p, max_p, ps, h, nc, cw)

        # 2. Dynamic dPOC line with color shift + history trail
        self._render_dpoc_dynamic(painter, draw_rect, min_p, max_p, ps, h)

        # 3. Imbalance circles (pre-computed in _precompute_imbalance_circles)
        self._precompute_imbalance_circles(nc, cw, draw_rect, min_p, max_p, py)
        self._render_imbalance_circles(painter, min_p, max_p, ps, h, draw_rect)

        # Draw volume bars ON TOP of candles
        self._render_volume_bars_on_candles(painter, draw_rect, cw, min_p, max_p, ps, h, base_font_size, font)

        # LIVE CANDLE RENDER (nc - 1)
        idx = nc - 1
        xl_cell = draw_rect.left() + (idx * cw)
        
        if not (xl_cell > vp_max_x or (xl_cell + live_cell_w) < vp_min_x):
            if idx in self.trade_grid:
                bands = self.trade_grid[idx]
                xl_fp = xl_cell + live_candle_zone_w
                candle_max_vol = 0.001
                poc_bp = None
                for bp, vols in bands.items():
                    if not (min_p <= bp <= max_p): continue
                    tot = vols['bid_vol'] + vols['ask_vol']
                    if tot > candle_max_vol:
                        candle_max_vol = tot
                        poc_bp = bp
                
                painter.setFont(font)
                for bp, vols in bands.items():
                    if not (min_p <= bp <= max_p): continue
                    y = py(bp)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    yb = py(bp - self.tick_size)
                    ch = max(4, abs(yb - y))
                    bv = vols['bid_vol']; av = vols['ask_vol']
                    total_vol = bv + av
                    delta = av - bv
                    if total_vol < 0.01: continue
                    
                    # CAPA 4: MATRIZ NUMERICA (Footprint Text Layer)
                    show_numbers = getattr(self, 'show_footprint_numbers', False)
                    if not show_numbers:
                        if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                            bar_max_w = max(3, int(live_fp_zone_w * 0.35))
                            p_ratio = total_vol / max(candle_max_vol, 0.001)
                            bar_w = max(2, int(bar_max_w * p_ratio))
                            bar_x = int(xl_fp) + int(live_fp_zone_w) - bar_w - 1
                            if delta > 0:
                                c = QColor(187, 0, 255, 180) if total_vol < tier_medium else QColor(170, 50, 255, 200) if total_vol < tier_whale else QColor(150, 0, 255, 220)
                            else:
                                c = QColor(0, 255, 102, 180) if total_vol < tier_medium else QColor(0, 255, 80, 200) if total_vol < tier_whale else QColor(0, 255, 50, 220)
                            painter.setPen(Qt.NoPen)
                            painter.setBrush(c)
                            painter.drawRect(bar_x, int(y) - int(ch // 2) + 2, bar_w, max(2, int(ch) - 4))
                        continue

                    center_x = xl_fp + (live_fp_zone_w / 2)
                    bid_w = (bv / candle_max_vol) * (live_fp_zone_w / 2)
                    ask_w = (av / candle_max_vol) * (live_fp_zone_w / 2)
                    
                    bg_alpha = 60
                    if total_vol >= tier_whale: bg_alpha = 180
                    elif total_vol >= tier_medium: bg_alpha = 100
                    elif total_vol < VOLUME_THRESHOLD: bg_alpha = 20
                    
                    painter.setPen(Qt.NoPen)
                    if bid_w > 0:
                        painter.setBrush(QColor(187, 0, 255, bg_alpha))
                        painter.drawRect(int(center_x - bid_w), int(y) - int(ch / 2) + 1, int(bid_w), int(ch) - 2)
                    if ask_w > 0:
                        painter.setBrush(QColor(0, 255, 102, bg_alpha))
                        painter.drawRect(int(center_x), int(y) - int(ch / 2) + 1, int(ask_w), int(ch) - 2)
                    
                    if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                        bar_max_w = max(3, int(live_fp_zone_w * 0.35))
                        p_ratio = total_vol / max(candle_max_vol, 0.001)
                        bar_w = max(2, int(bar_max_w * p_ratio))
                        bar_x = int(xl_fp) + int(live_fp_zone_w) - bar_w - 1
                        if delta > 0:
                            c = QColor(187, 0, 255, 180) if total_vol < tier_medium else QColor(170, 50, 255, 200) if total_vol < tier_whale else QColor(150, 0, 255, 220)
                        else:
                            c = QColor(0, 255, 102, 180) if total_vol < tier_medium else QColor(0, 255, 80, 200) if total_vol < tier_whale else QColor(0, 255, 50, 220)
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(c)
                        painter.drawRect(bar_x, int(y) - int(ch // 2) + 2, bar_w, max(2, int(ch) - 4))
                    
                    if total_vol >= tier_whale:
                        glow_color = QColor(0, 255, 102) if delta > 0 else QColor(187, 0, 255)
                        painter.setPen(QPen(glow_color, 1))
                        painter.setBrush(Qt.NoBrush)
                        painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(live_fp_zone_w), int(ch))
                    
                    if total_vol >= VOLUME_THRESHOLD and live_fp_zone_w > 15 and ch > (base_font_size - 2):
                        bt = f"{bv:.0f}" if bv >= 1 else f"{bv:.1f}"
                        at = f"{av:.0f}" if av >= 1 else f"{av:.1f}"
                        bid_color = QColor(187, 0, 255); ask_color = QColor(0, 255, 102)
                        if av > bv * 3 and av > tier_medium: ask_color = QColor(255, 255, 0)
                        if bv > av * 3 and bv > tier_medium: bid_color = QColor(255, 255, 0)
                        if total_vol >= tier_whale: bid_color = QColor(255, 255, 255); ask_color = QColor(255, 255, 255)
                        
                        pm_b = self._get_cached_text(bt, font, bid_color)
                        painter.drawPixmap(int(center_x - pm_b.width() - 3), int(y - pm_b.height()/2), pm_b)
                        pm_a = self._get_cached_text(at, font, ask_color)
                        painter.drawPixmap(int(center_x + 3), int(y - pm_a.height()/2), pm_a)

            # Live Candle - Vela actual más ancha
            k = self.klines[idx]
            o, hi, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            xc = xl_cell + (live_candle_zone_w / 2)
            yo = py(o); yc = py(c); yh = py(hi); yl = py(lo)
            # CAPA 3: VELAS JAPONESAS LIMPIAS (Live Candle)
            bull = c >= o
            if bull: bc = QColor(0, 255, 102, 255); wc = QColor(0, 255, 102, 255)
            else: bc = QColor(187, 0, 255, 255); wc = QColor(187, 0, 255, 255)
            painter.setPen(QPen(wc, 1, Qt.SolidLine))
            painter.drawLine(int(xc), int(yh), int(xc), int(yl))
            bt = min(yo, yc); bh = max(1, abs(yo - yc))
            painter.setPen(QPen(wc, 1, Qt.SolidLine))
            painter.setBrush(bc)
            painter.drawRect(int(xc - live_bw / 2), int(bt), int(live_bw), int(bh))

            # Live Delta Bar
            if idx in self.trade_grid:
                dh = 15; dy = draw_rect.bottom() - dh
                bands = self.trade_grid[idx]
                tb = sum(v['bid_vol'] for v in bands.values())
                ta = sum(v['ask_vol'] for v in bands.values())
                d = ta - tb
                dbh = min(dh, max(2, abs(d) / fp_max * dh))
                painter.setPen(Qt.NoPen)
                if d > 0:
                    painter.setBrush(QColor(0, 255, 102, 140))
                    painter.drawRect(int(xc - live_bw / 2), int(dy + dh - dbh), int(live_bw), int(dbh))
                else:
                    painter.setBrush(QColor(187, 0, 255, 140))
                    painter.drawRect(int(xc - live_bw / 2), int(dy + dh - dbh), int(live_bw), int(dbh))
            
            # LIVE VOLUME BAR - Barra de volumen para vela live
            if idx in self.trade_grid:
                bands = self.trade_grid[idx]
                live_bid = sum(v['bid_vol'] for v in bands.values())
                live_ask = sum(v['ask_vol'] for v in bands.values())
                live_total = live_bid + live_ask
                if live_total > 0:
                    live_mult = min(live_total / 1.0, 9.99)
                    
                    if live_bid > live_ask:
                        live_bar_color = QColor(0, 255, 136, 220)
                    else:
                        live_bar_color = QColor(187, 0, 255, 220)
                    
                    if live_mult > 3:
                        live_bar_color = QColor(255, 255, 0, 230)
                    
                    max_bar_height = 25
                    base_y = draw_rect.bottom() - 15
                    live_bar_height = min(max_bar_height, max(2, max_bar_height * (live_mult / 4.0)))
                    live_bar_w = live_cell_w * 0.8
                    live_bar_x = xl_cell + (live_cell_w - live_bar_w) / 2
                    live_vol_bar_y = base_y - live_bar_height
                    
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(live_bar_color)
                    painter.drawRect(int(live_bar_x), int(live_vol_bar_y), int(live_bar_w), int(live_bar_height))
                    
                    ltext = f"{live_mult:.1f}x"
                    ltf = QFont(font)
                    ltf.setPointSize(8)
                    ltf.setBold(True)
                    painter.setFont(ltf)
                    painter.setPen(QColor(255, 255, 255))
                    
                    fm = painter.fontMetrics()
                    text_width = fm.horizontalAdvance(ltext)
                    
                    x_center = xl_cell + live_cell_w / 2
                    y_bottom = live_vol_bar_y - 2
                    
                    painter.save()
                    painter.translate(x_center, y_bottom)
                    painter.rotate(-90)
                    y_offset = fm.ascent() / 2 - fm.descent() / 2
                    painter.drawText(0, int(y_offset), ltext)
                    painter.restore()
                    painter.drawRect(int(xc - bw / 2), int(dy), int(bw), int(dbh))

        # VOLUME PROFILE SIDEBAR (Dynamic)
        if self.order_state:
            ob_max = max((q for _, q in self.order_state['bids'] + self.order_state['asks']), default=1)
            for p, q in self.order_state['bids']:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    bww = (q / ob_max) * vp_w
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(0, 255, 102, 100))
                    painter.drawRect(int(vp_x), int(y) - 1, int(bww), 3)
            for p, q in self.order_state['asks']:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    bww = (q / ob_max) * vp_w
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(187, 0, 255, 100))
                    painter.drawRect(int(vp_x), int(y) - 1, int(bww), 3)

        # PREDICTIVE GHOST CANDLES
        if self.predicted_candles:
            sep_x = draw_rect.left() + (nc * cw)
            painter.setPen(QPen(QColor(COLORS['accent_gold']), 1, Qt.DashLine))
            painter.drawLine(int(sep_x), draw_rect.top(), int(sep_x), draw_rect.bottom())
            
            p_font = QFont(font); p_font.setPointSize(6); p_font.setBold(True); painter.setFont(p_font)
            painter.setPen(QColor(COLORS['accent_gold']))
            painter.drawText(int(sep_x + 3), draw_rect.top() + 10, "PREDICTION")
            
            for pi, pc in enumerate(self.predicted_candles):
                idx = nc + pi
                xl_cell = draw_rect.left() + (idx * cw)
                if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x: continue
                xc = xl_cell + (candle_zone_w / 2)
                
                yo = py(pc['o']); yc_p = py(pc['c']); yh = py(pc['h']); yl = py(pc['l'])
                bull = pc['c'] >= pc['o']
                conf = pc['confidence']
                
                alpha_body = max(30, int(conf * 0.8))
                alpha_wick = max(20, int(conf * 0.5))
                
                if bull:
                    body_c = QColor(0, 255, 102, alpha_body)
                    wick_c = QColor(0, 255, 102, alpha_wick)
                    border_c = QColor(0, 255, 102, alpha_wick + 30)
                else:
                    body_c = QColor(187, 0, 255, alpha_body)
                    wick_c = QColor(187, 0, 255, alpha_wick)
                    border_c = QColor(187, 0, 255, alpha_wick + 30)
                
                painter.setPen(QPen(wick_c, 1, Qt.DashLine))
                painter.drawLine(int(xc), int(yh), int(xc), int(yl))
                
                bt = min(yo, yc_p); bh = max(1, abs(yo - yc_p))
                painter.setPen(QPen(border_c, 1, Qt.DashLine))
                painter.setBrush(body_c)
                painter.drawRect(int(xc - bw / 2), int(bt), int(bw), int(bh))
                
                if pi == 0 or pi == len(self.predicted_candles) - 1:
                    painter.setPen(QColor(COLORS['text_primary']))
                    label_y = int(min(yh, yl)) - 5
                    painter.drawText(int(xc - 10), label_y, f"{conf:.0f}%")
                
        # PULSE ANIMATIONS (Radar Effect)
        import time
        current_time = time.time()
        active_pulses = []
        for p in self.visual_pulses:
            elapsed = current_time - p['start']
            if elapsed < 1.0:
                progress = elapsed / 1.0
                radius = progress * 50
                alpha = int(255 * (1.0 - progress))
                color = QColor(p['color'].red(), p['color'].green(), p['color'].blue(), alpha)
                painter.setPen(QPen(color, 2))
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), int(alpha * 0.2)))
                xc = draw_rect.left() + (p['idx'] * cw) + (candle_zone_w / 2)
                yc = py(p['price'])
                painter.drawEllipse(QPointF(xc, yc), radius, radius)
                active_pulses.append(p)
        self.visual_pulses = active_pulses
        
        # ENTRY POINT INDICATOR
        if self.entry_state:
            ep = self.entry_state['price']
            side = self.entry_state['type']
            if min_p <= ep <= max_p:
                ey = py(ep)
                if vp_min_y <= ey <= vp_max_y:
                    color = QColor(0, 255, 102) if side == 'BUY' else QColor(187, 0, 255)
                    icon = "🐂" if side == 'BUY' else "🐻"
                    
                    painter.setPen(QPen(color, 2, Qt.SolidLine))
                    painter.drawLine(draw_rect.left(), int(ey), draw_rect.right() + vp_w, int(ey))
                    
                    box_w, box_h = 130, 20
                    box_x, box_y = draw_rect.left() + 5, int(ey) - box_h - 2
                    painter.setBrush(QColor(0, 0, 0, 200))
                    painter.setPen(QPen(color, 1))
                    painter.drawRoundedRect(box_x, box_y, box_w, box_h, 4, 4)
                    
                    p_font = QFont(font); p_font.setPointSize(8); p_font.setBold(True); painter.setFont(p_font)
                    painter.setPen(color)
                    painter.drawText(box_x + 5, box_y + 14, f"{icon} ENTRY: ${ep:,.1f}")


    # ═══════════════════════════════════════════════════════════════════════
    # CAMBIO 2: Zoom & Pan Event Handlers
    # ═══════════════════════════════════════════════════════════════════════
    def wheelEvent(self, event):
        """Mouse wheel over chart: Zoom Y-axis in/out. Shift+wheel: vertical pan."""
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        
        modifiers = event.modifiers()
        
        if modifiers & Qt.ShiftModifier:
            pan_amount = (delta / 120) * self.tick_size * 2
            self.y_scroll_offset += pan_amount
        else:
            if delta > 0:
                self.y_scale_factor = min(ZOOM_MAX, self.y_scale_factor + ZOOM_STEP)
            else:
                self.y_scale_factor = max(ZOOM_MIN, self.y_scale_factor - ZOOM_STEP)
        
        self._update_title()
        self.update()
        event.accept()
    
    def keyPressEvent(self, event):
        """Keyboard shortcuts: +/- for zoom, R to reset, F to toggle X-Ray Mode."""
        key = event.key()
        if key == Qt.Key_F:
            self.show_footprint_numbers = not getattr(self, 'show_footprint_numbers', False)
            self.last_buffer_state = None
            self.update()
        elif key == Qt.Key_Plus or key == Qt.Key_Equal:
            self.y_scale_factor = min(ZOOM_MAX, self.y_scale_factor + ZOOM_STEP)
            self._update_title()
            self.update()
        elif key == Qt.Key_Minus:
            self.y_scale_factor = max(ZOOM_MIN, self.y_scale_factor - ZOOM_STEP)
            self._update_title()
            self.update()
        elif key == Qt.Key_R:
            self.y_scale_factor = 1.0
            self.y_scroll_offset = 0.0
            self._update_title()
            self.update()
        else:
            super().keyPressEvent(event)

    def _classify_candle_signal(self, idx, multiplier, bid_vol, ask_vol):
        """
        Clasifica la señal institucional de una vela.
        Retorna: ('WHALE','B','#00ff66') | ('TRAP','T','#FF2244') | ('INST','I','#FFCC00') | None
        """
        k = self.klines[idx] if idx < len(self.klines) else None
        body_ratio = 1.0
        if k:
            try:
                o = float(k[1]); hi = float(k[2]); lo = float(k[3]); c = float(k[4])
                wick = hi - lo
                body = abs(c - o)
                body_ratio = (body / wick) if wick > 0 else 1.0
            except Exception:
                pass

        if multiplier >= 5.0:
            return ('WHALE', 'B', '#00ff66')
        if multiplier >= 2.5 and body_ratio < 0.30:
            return ('TRAP', 'T', '#FF2244')
        if multiplier >= 3.0:
            return ('INST', 'I', '#FFCC00')
        return None

    def _render_volume_bars_on_candles(self, painter, draw_rect, cw, min_p, max_p, ps, h, base_font_size, font):
        """Renderiza el Signal Strip completo en 3 capas debajo del gráfico."""
        nc = len(self.klines)
        if nc < 2:
            return

        # ── Zona reservada exclusiva (75px desde el borde inferior) ──────────
        # Capa 1 (base): Delta Bars   → bottom-0   a bottom-15
        # Capa 2:        RVOL Bars    → bottom-15  a bottom-40  (25px máximo)
        # Capa 3:        RVOL texto   → bottom-40  a bottom-58  (rotado -90°)
        # Capa 4:        Signal Badge → bottom-58  a bottom-78  (I / B / T)
        STRIP_H       = 78
        DELTA_H       = 15
        RVOL_BASE_Y   = draw_rect.bottom() - DELTA_H          # base de la barra RVOL
        RVOL_MAX_H    = 25
        BADGE_ZONE_Y  = draw_rect.bottom() - STRIP_H          # tope del badge strip
        BADGE_H       = 18

        # Separador visual: línea divisoria del strip
        sep_pen = QPen(QColor(50, 55, 70, 160), 1, Qt.DotLine)
        painter.setPen(sep_pen)
        painter.drawLine(int(draw_rect.left()), int(BADGE_ZONE_Y - 2),
                         int(draw_rect.right()), int(BADGE_ZONE_Y - 2))
        painter.setPen(Qt.NoPen)

        # Calcular avg_volume sobre velas históricas
        avg_volume = 1.0
        all_vols = []
        for idx in range(nc - 1):
            if idx in self.trade_grid:
                bands = self.trade_grid[idx]
                total = sum(v['bid_vol'] + v['ask_vol'] for v in bands.values())
                if total > 0:
                    all_vols.append(total)
        if all_vols:
            all_vols.sort(reverse=True)
            half = max(1, len(all_vols) // 2)
            avg_volume = sum(all_vols[:half]) / half

        # Fuente para badges y RVOL
        badge_font = QFont(font)
        badge_font.setPointSize(9)
        badge_font.setBold(True)

        rvol_font = QFont(font)
        rvol_font.setPointSize(8)
        rvol_font.setBold(True)

        for idx in range(nc - 1):
            xl_cell = draw_rect.left() + (idx * cw)

            if xl_cell > draw_rect.right() or (xl_cell + cw) < draw_rect.left():
                continue

            # Obtener volúmenes
            if idx in self.trade_grid:
                bands     = self.trade_grid[idx]
                bid_vol   = sum(v['bid_vol'] for v in bands.values())
                ask_vol   = sum(v['ask_vol'] for v in bands.values())
                total_vol = bid_vol + ask_vol
            else:
                k = self.klines[idx] if idx < len(self.klines) else None
                if k:
                    vol     = float(k[5])
                    bid_vol = vol * 0.5
                    ask_vol = vol * 0.5
                    total_vol = vol
                else:
                    continue

            if total_vol < 0.01:
                continue

            multiplier = min(total_vol / avg_volume if avg_volume > 0 else 1.0, 9.99)

            # ── CAPA 2: RVOL Histogram ───────────────────────────────────────
            dominant = 'buy' if bid_vol > ask_vol else 'sell'
            if multiplier >= 5.0:
                bar_color = QColor(0, 255, 102, 200)    # Verde ballena
            elif multiplier >= 3.0:
                bar_color = QColor(255, 204, 0, 210)    # Dorado institucional
            elif dominant == 'buy':
                bar_color = QColor(0, 255, 136, 180)    # Verde compra
            else:
                bar_color = QColor(187, 0, 255, 180)    # Morado venta

            bar_h  = min(RVOL_MAX_H, max(2, RVOL_MAX_H * (multiplier / 4.0)))
            bar_w  = cw * 0.78
            bar_x  = xl_cell + (cw - bar_w) / 2
            bar_y  = RVOL_BASE_Y - bar_h

            painter.setPen(Qt.NoPen)
            painter.setBrush(bar_color)
            painter.drawRect(int(bar_x), int(bar_y), int(bar_w), int(bar_h))

            # ── CAPA 3: RVOL texto rotado -90° ──────────────────────────────
            mult_text = f"{multiplier:.1f}x"
            painter.setFont(rvol_font)
            painter.setPen(QColor(220, 220, 220))
            fm = painter.fontMetrics()

            x_center = xl_cell + cw / 2
            y_top    = bar_y - 2   # justo encima de la barra

            painter.save()
            painter.translate(x_center, y_top)
            painter.rotate(-90)
            y_off = (fm.ascent() - fm.descent()) / 2
            painter.drawText(0, int(y_off), mult_text)
            painter.restore()

            # ── CAPA 4: Signal Badge (I / B / T) ────────────────────────────
            signal = self._classify_candle_signal(idx, multiplier, bid_vol, ask_vol)
            if signal:
                sig_type, sig_letter, sig_hex = signal
                sig_color = QColor(sig_hex)

                # Fondo del badge (pequeño rectángulo semitransparente)
                bg = QColor(sig_color.red(), sig_color.green(), sig_color.blue(), 40)
                badge_w = min(cw * 0.78, 16)
                badge_x = xl_cell + (cw - badge_w) / 2
                badge_y = BADGE_ZONE_Y + (BADGE_H - 14) / 2

                painter.setPen(Qt.NoPen)
                painter.setBrush(bg)
                painter.drawRoundedRect(int(badge_x), int(badge_y), int(badge_w), 14, 3, 3)

                # Borde del badge
                painter.setPen(QPen(sig_color, 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(int(badge_x), int(badge_y), int(badge_w), 14, 3, 3)

                # Letra central
                painter.setFont(badge_font)
                painter.setPen(sig_color)
                fm2 = painter.fontMetrics()
                lw  = fm2.horizontalAdvance(sig_letter)
                lx  = xl_cell + (cw - lw) / 2
                ly  = badge_y + 11
                painter.drawText(int(lx), int(ly), sig_letter)

        # ── CAPA 4 LEYENDA: Separador + labels en extremo izquierdo ──────────
        legend_font = QFont(font)
        legend_font.setPointSize(7)
        painter.setFont(legend_font)

        lx = int(draw_rect.left() + 3)
        painter.setPen(QColor(0, 170, 255))
        painter.drawText(lx, int(BADGE_ZONE_Y + 12), "B")
        painter.setPen(QColor(255, 204, 0))
        painter.drawText(lx + 12, int(BADGE_ZONE_Y + 12), "I")
        painter.setPen(QColor(255, 34, 68))
        painter.drawText(lx + 24, int(BADGE_ZONE_Y + 12), "T")


    def get_dPOC(self):
        return getattr(self, 'poc_price', 0.0)
    
    def get_orderbook_imbalance(self):
        if hasattr(self, 'order_state') and self.order_state:
            bids = self.order_state.get('bids', [])
            asks = self.order_state.get('asks', [])
            total_bid = sum(q for _, q in bids)
            total_ask = sum(q for _, q in asks)
            if total_bid + total_ask > 0:
                return (total_bid - total_ask) / (total_bid + total_ask)
        return 0.0


class TrendSignalBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("border: none; background: transparent;")
        self.trend_direction = "NEUTRAL"
        self.trend_text = "WAIT ── NO CLEAR EDGE"
        self.pulse_phase = 0
        self.flash_alpha = 0
        
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate_step)
        self.anim_timer.start(16)
        
    def animate_step(self):
        self.pulse_phase = (self.pulse_phase + 2) % 360
        if self.flash_alpha > 0:
            self.flash_alpha = max(0, self.flash_alpha - 25)
        self.update()
        
    def trigger_flash(self):
        self.flash_alpha = 255
        
    def update_signal(self, direction, text, trap_text=None):
        if self.trend_direction != direction:
            self.trigger_flash()
        self.trend_direction = direction
        self.trend_text = text
        self.trap_text = trap_text

    def set_trap_mode(self, trap_text):
        """Override banner with trap alert."""
        self.trap_text = trap_text
        self.trigger_flash()
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(10, 2, -10, -2)
        
        import math
        pulse = (math.sin(math.radians(self.pulse_phase)) + 1) / 2
        
        # Base background
        painter.setPen(Qt.NoPen)
        
        trap_active = getattr(self, 'trap_text', None) is not None and 'SIN TRAMPA' not in (self.trap_text or '')
        
        if trap_active:
            # Trap mode: dark red background
            a = int(160 + pulse * 60)
            bg_color = QColor(139, 0, 0, a)
            border_color = QColor(255, 215, 0, 200)
            text_color = QColor(255, 255, 0)
            painter.setBrush(bg_color)
            painter.drawRoundedRect(rect, 8, 8)
            # Gold border
            painter.setPen(QPen(border_color, 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)
        else:
            painter.setBrush(QColor("#111"))
            painter.drawRoundedRect(rect, 8, 8)
            # Signal Background
            a = int(120 + pulse * 60)
            if self.trend_direction == "LONG":
                color = QColor(0, 255, 102, a)
            elif self.trend_direction == "SHORT":
                color = QColor(187, 0, 255, a)
            else:
                color = QColor(30, 30, 30, a)
                
            if self.flash_alpha > 0:
                color = QColor(255, 255, 255, self.flash_alpha)
                
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 8, 8)
        
        # Text
        font = painter.font(); font.setBold(True); font.setPointSize(10); painter.setFont(font)
        
        trap_active = getattr(self, 'trap_text', None) is not None and 'SIN TRAMPA' not in (self.trap_text or '')
        
        if trap_active:
            text_color = QColor("#ffff00")
            display_text = self.trap_text
        elif self.trend_direction == "NEUTRAL":
            text_color = QColor("#ffcc00")
            display_text = self.trend_text
        else:
            text_color = QColor("#fff")
            display_text = self.trend_text
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, display_text)

class OrderFlowBattleBar(QFrame):
    """Trend-synchronized battle bar.
    
    Computes a composite directional force from:
    - Order flow delta (buy vs sell volume)
    - Order book imbalance (bid vs ask walls)
    - Trend direction (EMA cross)
    - RSI momentum
    - CVD direction
    - Prediction from the chart engine
    
    Shows a clear GO LONG / GO SHORT / WAIT signal.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_buy_pct = 50.0
        self.target_buy_pct = 50.0
        self.trend_label = "ANALYZING..."
        self.trend_direction = "NEUTRAL"  # LONG, SHORT, NEUTRAL
        self.confidence = 0
        self.pulse_phase = 0  # For glow animation
        self.setFixedHeight(28)
        self.setStyleSheet("border: none; background: transparent;")
        
        # Smooth animation timer
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate_step)
        self.anim_timer.start(16)  # ~60 FPS
        
    def animate_step(self):
        diff = self.target_buy_pct - self.current_buy_pct
        if abs(diff) > 0.05:
            self.current_buy_pct += diff * 0.10
        self.pulse_phase = (self.pulse_phase + 2) % 360
        self.update()
    
    def update_battle(self, buy_volume, sell_volume, imbalance,
                      trend='NEUTRAL', rsi=50, cvd=0, prediction_dir='',
                      prediction_conf=0, confluence_score=50,
                      trend_1h='NEUTRAL', trend_4h='NEUTRAL',
                      delta=0, tick_speed=0, cancel_rate=0, pinam=0,
                      bb_squeeze='NORMAL', atr=0, spread_velocity=0,
                      avg_volume=0, volatility_explosion=False):
        """Full synchronization with all market data.

        V5 — Dynamic Order Flow Balance:
        - Primary: 30-second rolling window of aggressive buy/sell ratio
        - Secondary: weighted composite from order flow, micro, MTF, RSI, vol regime
        - Volatility Explosion bypass: when institutional presence is detected,
          all passive filters are suspended and signal is computed aggressively
        """
        # ── 30-second rolling volume window (primary bar driver) ──────
        if not hasattr(self, '_rolling_volume_30s'):
            self._rolling_volume_30s = deque(maxlen=30)
        self._rolling_volume_30s.append((buy_volume, sell_volume))
        total_buy_30 = sum(b for b, _ in self._rolling_volume_30s)
        total_sell_30 = sum(s for _, s in self._rolling_volume_30s)
        vol_total = total_buy_30 + total_sell_30 + 0.001
        rolling_buy_pct = (total_buy_30 / vol_total) * 100
        self.target_buy_pct = rolling_buy_pct

        # ── Volatility Explosion: bypass all passive filters ──────────
        if volatility_explosion:
            self._compute_signal(
                buy_volume, sell_volume, imbalance, trend, rsi, cvd,
                confluence_score, trend_1h, trend_4h,
                delta, tick_speed, cancel_rate, pinam,
                bb_squeeze, atr, spread_velocity,
                override=True,
            )
            return

        # ── CHOP ZONE / CONFLUENCE FILTER (soft: only suppress if 30s ratio is flat) ──
        force_ratio = max(total_buy_30, total_sell_30) / vol_total
        if 40 <= confluence_score <= 60 and force_ratio < 0.60:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.trend_label = "◆ CHOP ZONE — NO EDGE"
            return

        # ── HFT TOXICITY FILTER ──────────────────────────────────────
        if pinam > 0.25 and cancel_rate > 12:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.trend_label = "◆ HFT TOXIC — NO TRADE"
            return

        # ── VOLATILITY COMPRESSION ───────────────────────────────────
        if bb_squeeze == 'SQUEEZE' and atr > 0 and atr < 30:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.trend_label = "◆ BB SQUEEZE — WAIT EXPANSION"
            return

        # ── SPREAD VELOCITY ──────────────────────────────────────────
        if spread_velocity > 100:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.trend_label = "◆ WIDE SPREAD — NO EDGE"
            return

        self._compute_signal(
            buy_volume, sell_volume, imbalance, trend, rsi, cvd,
            confluence_score, trend_1h, trend_4h,
            delta, tick_speed, cancel_rate, pinam,
            bb_squeeze, atr, spread_velocity,
            override=False,
        )

    def _compute_signal(self, buy_volume, sell_volume, imbalance,
                        trend, rsi, cvd,
                        confluence_score, trend_1h, trend_4h,
                        delta, tick_speed, cancel_rate, pinam,
                        bb_squeeze, atr, spread_velocity,
                        override=False):
        """Weighted composite signal computation.

        When *override* is True (volatility explosion), spread_penalty is
        removed and ATR threshold is loosened to capture institutional flow.
        """
        spread_penalty = 0.8 if spread_velocity > 50 and not override else 1.0

        # ── COMPONENT SCORES (0–100, 50 = neutral) ────────────────────

        # 1a. Volume delta force
        total = buy_volume + sell_volume + 0.001
        vol_pct = (buy_volume / total) * 100

        # 1b. Order book imbalance
        ob_pct = (imbalance + 1) * 50

        # 1c. CVD direction
        if cvd > 50:    cvd_pct = 75
        elif cvd > 0:   cvd_pct = 60
        elif cvd < -50: cvd_pct = 25
        elif cvd < 0:   cvd_pct = 40
        else:           cvd_pct = 50

        # 1d. Delta acceleration
        if not hasattr(self, '_prev_delta'):
            self._prev_delta = delta
        delta_vel = delta - self._prev_delta
        self._prev_delta = delta
        self.delta_accel = delta_vel
        if delta_vel > 20:       delta_pct = 80
        elif delta_vel > 5:      delta_pct = 65
        elif delta_vel < -20:    delta_pct = 20
        elif delta_vel < -5:     delta_pct = 35
        else:                    delta_pct = 50

        # 2. MICROSTRUCTURE ACCELERATION
        if not hasattr(self, '_prev_tick'):
            self._prev_tick = tick_speed
        tick_accel = tick_speed - self._prev_tick
        self._prev_tick = tick_speed
        if tick_speed > 30 and tick_accel > 5:   micro_pct = 80
        elif tick_speed > 20 and tick_accel > 2: micro_pct = 65
        elif tick_speed > 30 and tick_accel < -5: micro_pct = 20
        elif tick_speed > 20 and tick_accel < -2: micro_pct = 35
        else:                                     micro_pct = 50
        if cancel_rate > 20:      micro_pct = 50 + (micro_pct - 50) * 0.3
        elif cancel_rate > 12:    micro_pct = 50 + (micro_pct - 50) * 0.6

        # 3. RSI
        if trend == 'ALCISTA':
            if rsi < 30:     rsi_pct = 80
            elif rsi < 40:   rsi_pct = 70
            elif rsi > 70:   rsi_pct = 80
            elif rsi > 60:   rsi_pct = 70
            else:            rsi_pct = 60
        elif trend == 'BAJISTA':
            if rsi < 30:     rsi_pct = 20
            elif rsi < 40:   rsi_pct = 30
            elif rsi > 70:   rsi_pct = 20
            elif rsi > 60:   rsi_pct = 30
            else:            rsi_pct = 40
        else:
            if rsi < 30:     rsi_pct = 80
            elif rsi < 40:   rsi_pct = 65
            elif rsi > 70:   rsi_pct = 20
            elif rsi > 60:   rsi_pct = 35
            else:            rsi_pct = 50

        # 4. MTF
        mtf_score = 50
        if trend_1h == 'ALCISTA':   mtf_score += 15
        elif trend_1h == 'BAJISTA': mtf_score -= 15
        if trend_4h == 'ALCISTA':   mtf_score += 5
        elif trend_4h == 'BAJISTA': mtf_score -= 5
        mtf_pct = max(0, min(100, mtf_score))

        # 5. VOLATILITY REGIME
        if atr > 100:        vol_regime = 75
        elif atr > 50:       vol_regime = 65
        elif atr < 15:       vol_regime = 35
        else:                vol_regime = 50

        # ── COMPOSITE ─────────────────────────────────────────────────
        raw_composite = (
            vol_pct * 0.15 + ob_pct * 0.10 + cvd_pct * 0.10 +
            delta_pct * 0.15 + micro_pct * 0.15 +
            rsi_pct * 0.10 + mtf_pct * 0.15 + vol_regime * 0.10
        )
        composite = 50 + (raw_composite - 50) * spread_penalty

        # ── DYNAMIC THRESHOLDS ───────────────────────────────────────
        if override:
            threshold = 55  # looser threshold during explosion
        elif atr > 70:
            threshold = 65
        elif atr < 20:
            threshold = 58
        else:
            threshold = 62

        # ── SIGNAL ───────────────────────────────────────────────────
        self.confidence = abs(composite - 50) * 2
        if composite > threshold:
            self.trend_direction = "LONG"
            self.trend_label = f"▲ GO LONG — {self.confidence:.0f}% FORCE"
        elif composite < 100 - threshold:
            self.trend_direction = "SHORT"
            self.trend_label = f"▼ GO SHORT — {self.confidence:.0f}% FORCE"
        else:
            self.trend_direction = "NEUTRAL"
            self.trend_label = f"◆ WAIT — NO CLEAR EDGE"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(10, 2, -10, -2)
        
        import math
        pulse = (math.sin(math.radians(self.pulse_phase)) + 1) / 2
        
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#111"))
        painter.drawRoundedRect(rect, 8, 8)
        
        w = rect.width()
        mid = rect.left() + (w * (self.current_buy_pct / 100))
        
        # Buy side
        buy_rect = QRectF(rect.left(), rect.top(), mid - rect.left(), rect.height())
        a = int(180 + pulse * 75) if self.trend_direction == "LONG" else 160
        painter.setBrush(QColor(0, 255, 102, a))
        painter.drawRoundedRect(buy_rect, 8, 8)
        
        # Sell side
        sell_rect = QRectF(mid, rect.top(), rect.right() - mid, rect.height())
        a = int(180 + pulse * 75) if self.trend_direction == "SHORT" else 160
        painter.setBrush(QColor(187, 0, 255, a))
        painter.drawRoundedRect(sell_rect, 8, 8)
        
        # Divider
        if self.trend_direction == "LONG": lc = QColor(0, 255, 102)
        elif self.trend_direction == "SHORT": lc = QColor(187, 0, 255)
        else: lc = QColor(COLORS['accent_gold'])
        painter.setPen(QPen(lc, 2))
        painter.drawLine(int(mid), rect.top(), int(mid), rect.bottom())
        
        font = painter.font(); font.setBold(True); font.setPointSize(8); painter.setFont(font)
        sp = 100.0 - self.current_buy_pct
        painter.setPen(QColor("#000"))
        painter.drawText(rect.left() + 5, rect.center().y() + 4, f"LONG {self.current_buy_pct:.0f}%")
        painter.drawText(rect.right() - 70, rect.center().y() + 4, f"SHORT {sp:.0f}%")


class FootprintChart(QFrame):
    def __init__(self, title="ORDER FLOW FOOTPRINT", parent=None):
        super().__init__(parent)
        self.title = title
        self.footprint_data = {} # Price -> {'buy': vol, 'sell': vol}
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        self.setMinimumHeight(150)
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_magenta']}; font-weight: bold; font-size: 13px; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)
        layout.addStretch()
        self.setLayout(layout)

    def update_trades(self, trades, current_price):
        # Cluster trades by price levels (tick size 1.0 for BTC)
        new_footprint = {}
        for t in trades:
            price_level = round(float(t['p']))
            if price_level not in new_footprint:
                new_footprint[price_level] = {'buy': 0, 'sell': 0}
            
            qty = float(t['q'])
            if t['m']: # Seller maker = Buy trade (market)
                new_footprint[price_level]['sell'] += qty
            else:
                new_footprint[price_level]['buy'] += qty
        
        self.footprint_data = new_footprint
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.footprint_data: return
        
        painter = QPainter(self)
        rect = self.rect()
        draw_rect = rect.adjusted(10, 30, -10, -10)
        
        prices = sorted(self.footprint_data.keys(), reverse=True)
        if not prices: return
        
        row_h = 18
        max_rows = draw_rect.height() // row_h
        visible_prices = prices[:max_rows]
        
        max_vol = 1.0
        for p in visible_prices:
            max_vol = max(max_vol, self.footprint_data[p]['buy'], self.footprint_data[p]['sell'])

        for i, p in enumerate(visible_prices):
            y = draw_rect.top() + (i * row_h)
            data = self.footprint_data[p]
            
            # Price level text
            painter.setPen(QColor(COLORS['text_secondary']))
            painter.drawText(draw_rect.left(), y + 14, f"{p}")
            
            # Buy bar (right)
            buy_w = (data['buy'] / max_vol) * (draw_rect.width() / 2 - 40)
            painter.fillRect(int(draw_rect.center().x() + 5), y + 2, int(buy_w), row_h - 4, QColor(0, 255, 102, 150))
            
            # Sell bar (left)
            sell_w = (data['sell'] / max_vol) * (draw_rect.width() / 2 - 40)
            painter.fillRect(int(draw_rect.center().x() - 5 - sell_w), y + 2, int(sell_w), row_h - 4, QColor(187, 0, 255, 150))
            
            # Volume texts
            painter.setPen(QColor(COLORS['text_primary']))
            painter.drawText(int(draw_rect.center().x() + 10), y + 14, f"{data['buy']:.2f}")
            painter.drawText(int(draw_rect.center().x() - 40), y + 14, f"{data['sell']:.2f}")

class LiquidityMapPanel(QFrame):
    def __init__(self, title="ORDER FLOW WALLS", parent=None):
        super().__init__(parent)
        self.title = title
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-weight: bold; font-size: 15px; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        
        self.buy_zone_label = QLabel()
        self.buy_zone_label.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-size: 12px; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        
        self.price_label = QLabel()
        self.price_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 18px; font-weight: bold; border: none; background: transparent;")
        self.price_label.setAlignment(Qt.AlignCenter)
        
        self.sell_zone_label = QLabel()
        self.sell_zone_label.setStyleSheet(f"color: {COLORS['accent_crimson']}; font-size: 12px; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        self.sell_zone_label.setAlignment(Qt.AlignRight)
        
        self.signal_label = QLabel()
        self.signal_label.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-size: 12px; font-weight: bold; border: none; background: transparent;")
        self.signal_label.setAlignment(Qt.AlignCenter)
        
        layout.addWidget(self.title_label)
        layout.addWidget(self.sell_zone_label)
        layout.addWidget(self.price_label)
        layout.addWidget(self.buy_zone_label)
        layout.addWidget(self.signal_label)
        
        self.setLayout(layout)
        
        # Animation timer for pulsing effect
        self.pulse_timer = QTimer(self)
        self.pulse_timer.timeout.connect(self.update_pulse)
        self.pulse_alpha = 255
        self.pulse_dir = -1
        self.pulse_timer.start(50)
    
    def update_pulse(self):
        self.pulse_alpha += self.pulse_dir * 15
        if self.pulse_alpha <= 100:
            self.pulse_alpha = 100
            self.pulse_dir = 1
        elif self.pulse_alpha >= 255:
            self.pulse_alpha = 255
            self.pulse_dir = -1
        self.update()

    def update_liquidity(self, data, current_price):
        buy_walls = data.get('buy_walls', [])
        sell_walls = data.get('sell_walls', [])
        imbalance = data.get('imbalance', 0)
        self.current_signal = data.get('signal', 'NEUTRAL')
        
        buy_html = f"<span style='color: {COLORS['accent_emerald']};'>"
        if buy_walls:
            for wall in buy_walls[:3]:
                qty_bar = "█" * min(int(wall['quantity'] / 20), 8)
                buy_html += f"{qty_bar} {wall['quantity']:.0f} BTC @ ${self.format_number(wall['price'])}<br>"
        else:
            buy_html += "No whale walls detected"
        buy_html += "</span>"
        self.buy_zone_label.setText(buy_html)
        
        price_html = f"<span style='color: {COLORS['accent_gold']}; font-size: 20px; font-weight: bold;'>"
        price_html += f"▶ ${self.format_number(current_price)}"
        price_html += "</span>"
        self.price_label.setText(price_html)
        
        sell_html = f"<span style='color: {COLORS['accent_crimson']}; text-align: right;'>"
        if sell_walls:
            for wall in sell_walls[:3]:
                qty_bar = "█" * min(int(wall['quantity'] / 20), 8)
                sell_html += f"{qty_bar} {wall['quantity']:.0f} BTC @ ${self.format_number(wall['price'])}<br>"
        else:
            sell_html += "No whale walls detected"
        sell_html += "</span>"
        self.sell_zone_label.setText(sell_html)
        
        if self.current_signal == 'BUY_WALL':
            color = f"rgba(0, 255, 136, {self.pulse_alpha})"
            signal_html = f"<span style='color: {color};'>🟢 BULLISH WALL ({imbalance*100:+.1f}%)</span>"
        elif self.current_signal == 'SELL_WALL':
            color = f"rgba(255, 51, 102, {self.pulse_alpha})"
            signal_html = f"<span style='color: {color};'>🔴 BEARISH WALL ({imbalance*100:+.1f}%)</span>"
        else:
            signal_html = f"<span style='color: {COLORS['accent_gold']};'>⚪ NEUTRAL FLOW</span>"
        self.signal_label.setText(signal_html)
    
    def format_number(self, num, decimals=2):
        if abs(num) >= 1000:
            return f"{num:,.0f}"
        return f"{num:,.2f}"


class AIPredictionPanel(QFrame):
    def __init__(self, title="AI PREDICTION", parent=None):
        super().__init__(parent)
        self.title = title
        self.agent_logs = []
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-weight: bold; font-size: 13px; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        
        self.prediction_label = QLabel()
        self.prediction_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 12px; font-weight: bold; border: none; background: transparent;")
        self.prediction_label.setAlignment(Qt.AlignCenter)
        
        self.confidence_label = QLabel()
        self.confidence_label.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-size: 11px; font-weight: bold; border: none; background: transparent;")
        self.confidence_label.setAlignment(Qt.AlignCenter)
        
        self.logs_label = QLabel()
        self.logs_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10px; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        self.logs_label.setAlignment(Qt.AlignLeft)
        
        layout.addWidget(self.title_label)
        layout.addWidget(self.prediction_label)
        layout.addWidget(self.confidence_label)
        layout.addWidget(self.logs_label)
        
        self.setLayout(layout)
    
    def update_prediction(self, prediction, logs):
        direction = prediction.get('direction', 'NEUTRAL')
        probability = prediction.get('probability', 50)
        confidence = prediction.get('confidence', 'LOW')
        target = prediction.get('target_price', 0)
        
        if direction == 'PUMP':
            pred_html = f"<span style='color: {COLORS['accent_emerald']}; font-size: 14px; font-weight: bold;'>"
            pred_html += f"▲ PREDICTED: {probability:.0f}% PUMP"
            pred_html += f"<br>Target: ${self.format_number(target)}"
            pred_html += "</span>"
        elif direction == 'DUMP':
            pred_html = f"<span style='color: {COLORS['accent_crimson']}; font-size: 14px; font-weight: bold;'>"
            pred_html += f"▼ PREDICTED: {probability:.0f}% DUMP"
            pred_html += f"<br>Target: ${self.format_number(target)}"
            pred_html += "</span>"
        else:
            pred_html = f"<span style='color: {COLORS['accent_gold']}; font-size: 14px; font-weight: bold;'>"
            pred_html += f"◐ NEUTRAL - {probability:.0f}% UNCERTAIN"
            pred_html += "</span>"
        
        self.prediction_label.setText(pred_html)
        
        conf_color = COLORS['accent_emerald'] if confidence == 'HIGH' else COLORS['accent_gold'] if confidence == 'MEDIUM' else COLORS['text_secondary']
        conf_html = f"<span style='color: {conf_color};'>⚡ AI CONFIDENCE: {confidence}</span>"
        self.confidence_label.setText(conf_html)
        
        logs_html = f"<span style='color: {COLORS['text_secondary']}; font-size: 10px;'>"
        for log in logs[-4:]:
            logs_html += f"{log}<br>"
        logs_html += "</span>"
        self.logs_label.setText(logs_html)
    
    def format_number(self, num, decimals=2):
        if abs(num) >= 1000:
            return f"{num:,.0f}"
        return f"{num:,.2f}"


class SentimentMeterPanel(QFrame):
    def __init__(self, title="SENTIMENT", parent=None):
        super().__init__(parent)
        self.title = title
        self.init_ui()
    
    def init_ui(self):
        self.setStyleSheet(PANEL_STYLE)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        
        self.title_label = QLabel(self.title)
        self.title_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-weight: bold; font-size: 13px; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        
        self.meter_label = QLabel()
        self.meter_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 11px; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        self.meter_label.setAlignment(Qt.AlignCenter)
        
        self.value_label = QLabel()
        self.value_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px; font-weight: bold; border: none; background: transparent;")
        self.value_label.setAlignment(Qt.AlignCenter)
        
        self.status_label = QLabel()
        self.status_label.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-size: 11px; font-weight: bold; border: none; background: transparent;")
        self.status_label.setAlignment(Qt.AlignCenter)
        
        layout.addWidget(self.title_label)
        layout.addWidget(self.meter_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.status_label)
        
        self.setLayout(layout)
    
    def update_sentiment(self, rsi_value):
        rsi = rsi_value if rsi_value else 50
        meter_width = 20
        position = int((rsi / 100) * meter_width)
        
        meter_str = ""
        for i in range(meter_width):
            if i < position - 1: meter_str += "█"
            elif i == position - 1: meter_str += "◆"
            else: meter_str += "░"
        
        if rsi < 25:
            zone_color = COLORS['accent_crimson']
            zone_text = "EXTREME FEAR"
        elif rsi < 40:
            zone_color = COLORS['accent_gold']
            zone_text = "FEAR"
        elif rsi < 60:
            zone_color = COLORS['accent_cyan']
            zone_text = "NEUTRAL"
        elif rsi < 75:
            zone_color = COLORS['accent_emerald']
            zone_text = "GREED"
        else:
            zone_color = COLORS['accent_emerald']
            zone_text = "EXTREME GREED"
        
        self.meter_label.setText(f"<span style='color: {zone_color};'>{meter_str}</span>")
        self.value_label.setText(f"<span style='color: {zone_color}; font-size: 14px; font-weight: bold;'>RSI: {rsi:.1f}</span>")
        self.status_label.setText(f"<span style='color: {zone_color};'>◈ {zone_text} ◈</span>")
def _make_separator(opacity=0.1):
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    line.setStyleSheet(f"background-color: rgba(255,255,255,{opacity}); border: none; max-height: 1px;")
    return line

def _make_quant_row(label_text, parent_layout, store_dict, key, margins=(10, 0, 10, 0)):
    row_l = QHBoxLayout()
    row_l.setContentsMargins(*margins)
    t = QLabel(label_text)
    t.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 9px;")
    v = QLabel("FETCHING...")
    v.setAlignment(Qt.AlignRight)
    v.setStyleSheet(f"color: rgba(255,255,255,0.5); font-weight: bold; font-size: 9px;")
    row_l.addWidget(t)
    row_l.addWidget(v)
    parent_layout.addLayout(row_l)
    store_dict[key] = v

class OIMomentumWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        lbl = QLabel("OI MOMENTUM PRO")
        lbl.setStyleSheet(f"color: {COLORS['accent_cyan']}; font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(10, 0, 10, 0)
        for t in ["INTERVAL", "OI DELTA %", "ACCEL RATIO"]:
            h = QLabel(t)
            h.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 8px; font-weight: bold;")
            if t != "INTERVAL": h.setAlignment(Qt.AlignRight)
            hdr.addWidget(h)
        layout.addLayout(hdr)

        self.rows = {}
        intervals = ["1s", "5s", "1m", "5m"]
        for i, iv in enumerate(intervals):
            row_l = QHBoxLayout()
            row_l.setContentsMargins(10, 0, 10, 0)
            lbl_iv = QLabel(f"Δ {iv}")
            lbl_iv.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 9px; font-weight: bold;")
            oi_v = QLabel("0.00%")
            oi_v.setAlignment(Qt.AlignRight)
            oi_v.setStyleSheet(f"color: rgba(255,255,255,0.5); font-weight: bold; font-size: 9px;")
            acc_v = QLabel("x0.0")
            acc_v.setAlignment(Qt.AlignRight)
            acc_v.setStyleSheet(f"color: rgba(255,255,255,0.5); font-weight: bold; font-size: 9px;")
            row_l.addWidget(lbl_iv)
            row_l.addWidget(oi_v)
            row_l.addWidget(acc_v)
            layout.addLayout(row_l)
            self.rows[iv] = {"oi": oi_v, "acc": acc_v}
            if i < len(intervals) - 1:
                layout.addWidget(_make_separator())

        layout.addStretch()
        self.setLayout(layout)

    def update_data(self, data_rows):
        for iv, oi, acc in data_rows:
            if iv not in self.rows: continue
            c_oi = COLORS['accent_emerald'] if oi > 0 else COLORS['accent_purple'] if oi < 0 else COLORS['text_primary']
            c_acc = COLORS['accent_cyan'] if acc > 1.5 else COLORS['text_primary']
            self.rows[iv]["oi"].setText(f"{oi:+.2f}%")
            self.rows[iv]["oi"].setStyleSheet(f"color: {c_oi}; font-weight: bold; font-size: 9px;")
            self.rows[iv]["acc"].setText(f"x{acc:.1f}")
            self.rows[iv]["acc"].setStyleSheet(f"color: {c_acc}; font-weight: bold; font-size: 9px;")

class LiquidityPoolWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        lbl = QLabel("LIQUIDITY POOL MAP")
        lbl.setStyleSheet(f"color: {COLORS['accent_turquoise']}; font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl)

        self.labels = {}
        for i, lev in enumerate(["10x", "25x", "50x", "100x"]):
            row_l = QHBoxLayout()
            row_l.setContentsMargins(10, 0, 10, 0)
            t = QLabel(f"POOL {lev}")
            t.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 9px;")
            v = QLabel("FETCHING...")
            v.setAlignment(Qt.AlignRight)
            v.setStyleSheet(f"color: rgba(255,255,255,0.5); font-weight: bold; font-size: 9px;")
            row_l.addWidget(t)
            row_l.addWidget(v)
            self.labels[lev] = v
            layout.addLayout(row_l)
            if i < 3: layout.addWidget(_make_separator(0.05))

        layout.addWidget(_make_separator(0.15))

        ww_row = QHBoxLayout()
        ww_row.setContentsMargins(10, 0, 10, 0)
        ww_t = QLabel("WHALE WALLS")
        ww_t.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 9px;")
        self.ww_v = QLabel("FETCHING...")
        self.ww_v.setAlignment(Qt.AlignRight)
        self.ww_v.setStyleSheet(f"color: rgba(255,255,255,0.5); font-weight: bold; font-size: 9px;")
        ww_row.addWidget(ww_t)
        ww_row.addWidget(self.ww_v)
        layout.addLayout(ww_row)

        layout.addStretch()

        depth_row = QHBoxLayout()
        depth_row.setContentsMargins(5, 2, 5, 0)
        self.depth_lbl = QLabel("CORE BOOK DEPTH: 50/50")
        self.depth_lbl.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 9px; font-weight: bold;")
        self.swell_lbl = QLabel("SWELL: 1.00")
        self.swell_lbl.setAlignment(Qt.AlignRight)
        self.swell_lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 9px; font-weight: bold;")
        depth_row.addWidget(self.depth_lbl)
        depth_row.addWidget(self.swell_lbl)
        layout.addLayout(depth_row)

        self.setLayout(layout)

    def update_data(self, price, pools, bid_p, ask_p, whale_dist, swell):
        for lev, p in pools:
            dist = abs(p - price)
            pct = (dist / price * 100) if price else 0
            self.labels[lev].setText(f"${p:,.0f} ── {pct:.1f}% (${dist:,.0f})")
            self.labels[lev].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        self.ww_v.setText(f"${whale_dist:,.0f}")
        self.ww_v.setStyleSheet(f"color: {COLORS['accent_gold']}; font-weight: bold; font-size: 9px;")
        self.depth_lbl.setText(f"DEPTH: {bid_p:.0f}% BID / {ask_p:.0f}% ASK")
        self.swell_lbl.setText(f"SWELL: {swell:.2f}")

class ConfluenceMatrixWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(1)

        lbl = QLabel("MTF CONFLUENCE MATRIX PRO")
        lbl.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(4, 0, 4, 0)
        for t in ["INDICATOR", "1M", "5M", "15M", "1H"]:
            h = QLabel(t)
            h.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 8px; font-weight: bold;")
            if t != "INDICATOR": h.setAlignment(Qt.AlignCenter)
            hdr.addWidget(h)
        layout.addLayout(hdr)

        self.cells = {}
        indicators = ["EMA CROSS", "SUPERTREND", "WAVE TREND", "MACD ALIGN",
                       "PARABOLIC SAR", "RSI OSCILLATOR", "CHOPPINESS IND", "ALGO BIAS"]
        for i, ind in enumerate(indicators):
            row_l = QHBoxLayout()
            row_l.setContentsMargins(4, 0, 4, 0)
            il = QLabel(ind)
            il.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 7px; font-weight: bold;")
            row_l.addWidget(il)
            self.cells[ind] = {}
            for tf in ["1M", "5M", "15M", "1H"]:
                c = QLabel("WAIT")
                c.setAlignment(Qt.AlignCenter)
                c.setStyleSheet(f"color: rgba(255,255,255,0.4); font-size: 7px; font-weight: bold;")
                row_l.addWidget(c)
                self.cells[ind][tf] = c
            layout.addLayout(row_l)
            if i < len(indicators) - 1: layout.addWidget(_make_separator(0.08))

        layout.addStretch()

        sc = QWidget()
        sc.setStyleSheet("background: rgba(0,255,0,0.08); border-radius: 4px;")
        sl = QVBoxLayout(sc)
        sl.setContentsMargins(2, 3, 2, 3)
        self.score_lbl = QLabel("CALCULATING...")
        self.score_lbl.setAlignment(Qt.AlignCenter)
        self.score_lbl.setStyleSheet(f"color: rgba(255,255,255,0.4); font-weight: 900; font-size: 14px;")
        sl.addWidget(self.score_lbl)
        layout.addWidget(sc)
        self.setLayout(layout)

    def update_data(self, score, matrix_data):
        BULL = ["ALCISTA", "SOBRECOMPRA", "CROSS UP", "LONG", "ABOVE", "AGGRESSIVE", "OVERB", "TRENDING"]
        BEAR = ["BAJISTA", "SOBREVENTA", "CROSS DOWN", "SHORT", "BELOW", "EXHAUSTION", "OVERS", "RANGING"]
        for ind, tfs in matrix_data.items():
            for tf, val in tfs.items():
                c = self.cells.get(ind, {}).get(tf)
                if c:
                    col = COLORS['accent_cyan'] if val in BULL else COLORS['accent_magenta'] if val in BEAR else COLORS['text_primary']
                    c.setText(val)
                    c.setStyleSheet(f"color: {col}; font-size: 7px; font-weight: bold;")
        if score >= 60:
            t, col, bg = f"{score:.0f}% BULLISH", COLORS['accent_cyan'], "rgba(0,245,255,0.12)"
        elif score <= 40:
            t, col, bg = f"{100-score:.0f}% BEARISH", COLORS['accent_magenta'], "rgba(255,0,255,0.12)"
        else:
            t, col, bg = f"{score:.0f}% NEUTRAL", COLORS['accent_gold'], "rgba(255,204,0,0.12)"
        self.score_lbl.setText(t)
        self.score_lbl.parent().setStyleSheet(f"background: {bg}; border-radius: 4px;")
        self.score_lbl.setStyleSheet(f"color: {col}; font-weight: 900; font-size: 14px; background: transparent;")

class HFTRiskWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        lbl = QLabel("HFT LIQUIDITY ENGINE")
        lbl.setStyleSheet(f"color: {COLORS['accent_gold']}; font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl)

        self.rows = {}

        sa = QLabel("SPEED & COEFFICIENTS")
        sa.setStyleSheet(f"color: {COLORS['text_secondary']}; font-weight: bold; font-size: 8px; padding-top: 1px;")
        layout.addWidget(sa)
        items_a = [("ts", "TICK SPEED"), ("ker", "KAUFMAN EFFICIENCY"),
                   ("cancel", "ORDER CANCEL RATE"), ("skew", "SKEWNESS COEFFICIENT")]
        for i, (k, l) in enumerate(items_a):
            _make_quant_row(l, layout, self.rows, k)
            if i < len(items_a) - 1: layout.addWidget(_make_separator(0.05))

        sb = QLabel("MICRO-SPREAD & IMBALANCE")
        sb.setStyleSheet(f"color: {COLORS['text_secondary']}; font-weight: bold; font-size: 8px; padding-top: 4px;")
        layout.addWidget(sb)
        items_b = [("spread", "BID/ASK SPREAD"), ("spread_vel", "SPREAD VELOCITY"),
                   ("depth_imb", "DEPTH IMBALANCE"), ("pinam", "HFT TOXICITY (PINAM)")]
        for i, (k, l) in enumerate(items_b):
            _make_quant_row(l, layout, self.rows, k)
            if i < len(items_b) - 1: layout.addWidget(_make_separator(0.05))

        layout.addWidget(_make_separator(0.15))
        _make_quant_row("VOLATILITY CLUSTER", layout, self.rows, "vol_cluster")

        layout.addStretch()
        self.setLayout(layout)

    def update_data(self, ts, ker, cancel, skew, spread, s_vel, d_imb, pinam, vol_cluster):
        self.rows["ts"].setText(f"{ts} ord/s")
        self.rows["ts"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        ker_c = COLORS['accent_emerald'] if ker > 0.5 else COLORS['accent_gold']
        self.rows["ker"].setText(f"{ker:.3f}")
        self.rows["ker"].setStyleSheet(f"color: {ker_c}; font-weight: bold; font-size: 9px;")
        self.rows["cancel"].setText(f"{cancel:.1f}%")
        self.rows["cancel"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        self.rows["skew"].setText(f"{skew:+.2f}")
        self.rows["skew"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        self.rows["spread"].setText(f"{spread:.2f}¢")
        self.rows["spread"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        self.rows["spread_vel"].setText(f"{s_vel:.1f} ms")
        self.rows["spread_vel"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px;")
        ic = COLORS['accent_emerald'] if d_imb > 0 else COLORS['accent_magenta']
        self.rows["depth_imb"].setText(f"{d_imb:+.1f}% {'BIDS' if d_imb > 0 else 'ASKS'}")
        self.rows["depth_imb"].setStyleSheet(f"color: {ic}; font-weight: bold; font-size: 9px;")
        pc = COLORS['accent_magenta'] if pinam > 0.7 else COLORS['accent_gold']
        self.rows["pinam"].setText(f"{pinam:.2f}")
        self.rows["pinam"].setStyleSheet(f"color: {pc}; font-weight: bold; font-size: 9px;")
        vc = COLORS['accent_magenta'] if vol_cluster == "HIGH EXPANSION" else COLORS['accent_cyan']
        self.rows["vol_cluster"].setText(vol_cluster)
        self.rows["vol_cluster"].setStyleSheet(f"color: {vc}; font-weight: bold; font-size: 9px;")

class AIBracketWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._border_color = COLORS['text_secondary']
        self.setStyleSheet(f"background: rgba(10,10,15,0.95); border: 1px solid {self._border_color}; border-radius: 6px;")
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        lbl = QLabel("AI BRACKET ORDER & RISK CONTROL")
        lbl.setStyleSheet(f"color: {COLORS['accent_magenta']}; font-weight: bold; font-size: 9px; border: none;")
        layout.addWidget(lbl)

        self.labels = {}
        self.dist_bars = {}
        fields = [("status", "STATUS"), ("trigger", "EXEC TRIGGER"), ("sl", "DYN STOP LOSS"),
                  ("tp1", "DYN TP1 (1:2)"), ("tp2", "DYN TP2 (WALL)"), ("lot", "LOT SIZE (-$10)")]
        for key, name in fields:
            row = QHBoxLayout()
            row.setContentsMargins(8, 0, 8, 0)
            t = QLabel(name)
            t.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 9px; border: none; background: transparent;")
            v = QLabel("WAITING")
            v.setAlignment(Qt.AlignRight)
            v.setStyleSheet(f"color: rgba(255,255,255,0.4); font-weight: bold; font-size: 9px; border: none; background: transparent;")
            row.addWidget(t)
            row.addWidget(v)
            self.labels[key] = v
            layout.addLayout(row)
            if key in ["trigger", "sl", "tp1", "tp2"]:
                bar_bg = QFrame()
                bar_bg.setFixedHeight(2)
                bar_bg.setStyleSheet("background: #1a1a1a; border: none; border-radius: 1px;")
                bar_f = QFrame(bar_bg)
                bar_f.setFixedHeight(2)
                bar_f.setFixedWidth(0)
                bar_f.setStyleSheet(f"background: {COLORS['accent_cyan']}; border: none; border-radius: 1px;")
                layout.addWidget(bar_bg)
                self.dist_bars[key] = (bar_bg, bar_f)

        layout.addStretch()

        self.conf_lbl = QLabel("SIGNAL CONFIDENCE: 0%")
        self.conf_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 8px; border: none; background: transparent;")
        layout.addWidget(self.conf_lbl)
        self.bar_bg = QFrame()
        self.bar_bg.setFixedHeight(4)
        self.bar_bg.setStyleSheet("background: #1a1a1a; border: none; border-radius: 2px;")
        self.bar_fill = QFrame(self.bar_bg)
        self.bar_fill.setFixedHeight(4)
        self.bar_fill.setFixedWidth(0)
        self.bar_fill.setStyleSheet(f"background: {COLORS['accent_magenta']}; border: none; border-radius: 2px;")
        layout.addWidget(self.bar_bg)
        self.setLayout(layout)

    def update_data(self, risk_panel, confidence, price, dpoc_price=None, orderbook_imb=0.0, brain_bracket=None):
        """Update panel — uses brain_bracket values when available."""
        if brain_bracket and brain_bracket.get('sl', 0) != 0:
            panel = {
                "status": brain_bracket.get('status', risk_panel["status"]),
                "trigger": brain_bracket.get('trigger', price),
                "sl": brain_bracket.get('sl', 0),
                "tp1": brain_bracket.get('tp1', 0),
                "tp2": brain_bracket.get('tp2', 0),
                "lot_size": brain_bracket.get('lot_size', 0),
            }
            st = panel["status"]
        else:
            panel = risk_panel
            st = risk_panel["status"]
        self.labels["status"].setText(st)
        if st == "WAITING":
            color = COLORS['accent_gold']; bc = COLORS['text_secondary']
        elif st == "LONG":
            color = COLORS['accent_cyan']; bc = COLORS['accent_cyan']
        else:
            color = COLORS['accent_magenta']; bc = COLORS['accent_magenta']
        self.setStyleSheet(f"background: rgba(10,10,15,0.95); border: 1px solid {bc}; border-radius: 6px;")
        self.labels["status"].setStyleSheet(f"color: {color}; font-weight: bold; font-size: 10px; border: none; background: transparent;")
        
        base_trigger = price
        
        dpoc_offset = 0.0
        if dpoc_price and dpoc_price > 0:
            dpoc_dist = price - dpoc_price
            dpoc_pct = abs(dpoc_dist / price) if price > 0 else 0
            
            if orderbook_imb > 0.3 and dpoc_dist < 0:
                dpoc_offset = dpoc_dist * 0.5
            elif orderbook_imb < -0.3 and dpoc_dist > 0:
                dpoc_offset = dpoc_dist * 0.5
        
        if st == "LONG":
            sl_val = panel.get("sl", 0)
            if sl_val == 0 and dpoc_price > 0:
                sl_val = dpoc_price - (price * 0.0025)
            tp1_val = panel.get("tp1", 0)
            if tp1_val == 0 and dpoc_price > 0:
                tp1_val = dpoc_price + (price * 0.005)
            tp2_val = panel.get("tp2", 0)
            if tp2_val == 0:
                tp2_val = price + (price * 0.015)
        elif st == "SHORT":
            sl_val = panel.get("sl", 0)
            if sl_val == 0 and dpoc_price > 0:
                sl_val = dpoc_price + (price * 0.0025)
            tp1_val = panel.get("tp1", 0)
            if tp1_val == 0 and dpoc_price > 0:
                tp1_val = dpoc_price - (price * 0.005)
            tp2_val = panel.get("tp2", 0)
            if tp2_val == 0:
                tp2_val = price - (price * 0.015)
        else:
            sl_val = panel.get("sl", 0)
            tp1_val = panel.get("tp1", 0)
            tp2_val = panel.get("tp2", 0)
        
        for k, fmt, val in [("trigger", "${:,.2f}", price), ("sl", "${:,.2f}", sl_val), ("tp1", "${:,.2f}", tp1_val), ("tp2", "${:,.2f}", tp2_val)]:
            self.labels[k].setText(fmt.format(val) if val else "—")
            if val: self.labels[k].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px; border: none; background: transparent;")
        
        lot = panel.get("lot_size", 0)
        self.labels["lot"].setText(f"{lot:.4f} BTC" if lot else "—")
        if lot: self.labels["lot"].setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold; font-size: 9px; border: none; background: transparent;")
        
        for k in ["sl", "tp1", "tp2", "trigger"]:
            if k in self.dist_bars and price > 0:
                val = panel.get(k, 0)
                if k == "trigger":
                    val = price
                if val:
                    pct = min(1.0, abs(val - price) / (price * 0.05))
                    bg, f = self.dist_bars[k]
                    w = int(pct * (bg.width() if bg.width() > 0 else 120))
                    f.setFixedWidth(max(0, w))
                    f.setStyleSheet(f"background: {color}; border: none; border-radius: 1px;")
        self.conf_lbl.setText(f"SIGNAL CONFIDENCE: {confidence:.1f}%")
        bw = int((confidence / 100.0) * (self.bar_bg.width() if self.bar_bg.width() > 0 else 120))
        self.bar_fill.setFixedWidth(max(0, min(bw, self.bar_bg.width())))
        self.bar_fill.setStyleSheet(f"background: {color}; border: none; border-radius: 2px;")



class QuantSidebarWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(280)
        self.setStyleSheet("background: #0b0c10; border-right: 1px solid #1f2833; border-top: none; border-bottom: none; border-left: none;")
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        title_style = "color: #66fcf1; font-size: 11px; font-weight: 900; background: transparent; padding: 2px; border: none;"
        
        # --- BLOCK A: AI BRACKET & RISK ---
        self.box_a = QFrame()
        self.box_a.setStyleSheet("border: 2px solid #555; border-radius: 6px; background: #111;")
        la = QVBoxLayout()
        la.setContentsMargins(8, 8, 8, 8)
        lbl_a = QLabel("🤖 AI BRACKET & RISK")
        lbl_a.setStyleSheet(title_style)
        la.addWidget(lbl_a)
        
        self.lbl_status = QLabel("STATUS: WAITING")
        self.lbl_trigger = QLabel("EXEC TRIGGER: NONE")
        self.lbl_sl = QLabel("DYN STOP LOSS: 0.00")
        self.lbl_tp = QLabel("DYN TP1: 0.00")
        self.lbl_lot = QLabel("LOT SIZE ($10 Risk): 0.000")
        
        for lbl in [self.lbl_status, self.lbl_trigger, self.lbl_sl, self.lbl_tp, self.lbl_lot]:
            lbl.setStyleSheet("color: #ccc; font-size: 11px; border: none; font-family: monospace; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            
            # Create horizontal layout for left-right alignment
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(lbl.text().split(':')[0] + ":")
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setText(lbl.text().split(':')[-1].strip())
            
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            
            # Store references to update later
            setattr(self, f"val_{lbl.objectName()}", lbl)
            la.addLayout(row)
            
        self.box_a.setLayout(la)
        layout.addWidget(self.box_a)
        
        # Fix the setattr references manually
        self.val_status = la.itemAt(1).layout().itemAt(1).widget()
        self.val_trigger = la.itemAt(2).layout().itemAt(1).widget()
        self.val_sl = la.itemAt(3).layout().itemAt(1).widget()
        self.val_tp = la.itemAt(4).layout().itemAt(1).widget()
        self.val_lot = la.itemAt(5).layout().itemAt(1).widget()
        
        # --- BLOCK B: OI MOMENTUM PRO ---
        box_b = QFrame()
        box_b.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        lb = QVBoxLayout()
        lb.setContentsMargins(8, 8, 8, 8)
        lbl_b = QLabel("⚡ OI MOMENTUM PRO")
        lbl_b.setStyleSheet(title_style)
        lb.addWidget(lbl_b)
        
        self.lbl_oi_1s = QLabel("0.0% | x1.0")
        self.lbl_oi_5s = QLabel("0.0% | x1.0")
        self.lbl_oi_1m = QLabel("0.0% | x1.0")
        
        for prefix, lbl in [("Δ 1s:", self.lbl_oi_1s), ("Δ 5s:", self.lbl_oi_5s), ("Δ 1m:", self.lbl_oi_1m)]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(prefix)
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setStyleSheet("color: #ccc; font-size: 11px; font-family: monospace; border: none; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            lb.addLayout(row)
            
        box_b.setLayout(lb)
        layout.addWidget(box_b)
        
        # --- BLOCK C: MTF CONFLUENCE MATRIX ---
        box_c = QFrame()
        box_c.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        lc = QVBoxLayout()
        lc.setContentsMargins(8, 8, 8, 8)
        lbl_c = QLabel("🎯 MTF CONFLUENCE MATRIX")
        lbl_c.setStyleSheet(title_style)
        lc.addWidget(lbl_c)
        
        grid = QGridLayout()
        grid.setSpacing(4)
        headers = ["IND", "1M", "5M", "15M", "1H"]
        for col, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; border: none; background: transparent;")
            grid.addWidget(lbl, 0, col)
            
        self.mtf_labels = {}
        inds = ["EMA", "SUP", "WAV"]
        for row, ind in enumerate(inds, start=1):
            lbl_ind = QLabel(ind)
            lbl_ind.setStyleSheet("color: #aaa; font-size: 10px; border: none; background: transparent;")
            grid.addWidget(lbl_ind, row, 0)
            for col, tf in enumerate(["1M", "5M", "15M", "1H"], start=1):
                lbl_val = QLabel("-")
                lbl_val.setStyleSheet("color: #555; font-size: 10px; border: none; background: transparent;")
                grid.addWidget(lbl_val, row, col)
                self.mtf_labels[f"{ind}_{tf}"] = lbl_val
                
        lc.addLayout(grid)
        
        self.lbl_score = QLabel("SCORE: 50% NEUTRAL")
        self.lbl_score.setStyleSheet("color: #fff; background: #333; padding: 4px; border-radius: 4px; font-size: 12px; font-weight: bold; border: none;")
        self.lbl_score.setAlignment(Qt.AlignCenter)
        lc.addWidget(self.lbl_score)
        
        box_c.setLayout(lc)
        layout.addWidget(box_c)
        
        # --- BLOCK D: HFT LIQUIDITY ENGINE ---
        box_d = QFrame()
        box_d.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        ld = QVBoxLayout()
        ld.setContentsMargins(8, 8, 8, 8)
        lbl_d = QLabel("⚙️ HFT LIQUIDITY ENGINE")
        lbl_d.setStyleSheet(title_style)
        ld.addWidget(lbl_d)
        
        self.lbl_tick = QLabel("0/s")
        self.lbl_kaufman = QLabel("0.00")
        self.lbl_spread = QLabel("0.0")
        
        for prefix, lbl in [("TICK SPEED:", self.lbl_tick), ("KAUFMAN EFF:", self.lbl_kaufman), ("SPREAD SPREAD:", self.lbl_spread)]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(prefix)
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setStyleSheet("color: #ccc; font-size: 11px; font-family: monospace; border: none; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            ld.addLayout(row)
            
        self.depth_bar_bg = QFrame()
        self.depth_bar_bg.setFixedHeight(10)
        self.depth_bar_bg.setStyleSheet("background: #222; border-radius: 5px; border: none;")
        
        self.depth_bar_fill = QFrame(self.depth_bar_bg)
        self.depth_bar_fill.setFixedHeight(10)
        self.depth_bar_fill.setStyleSheet("background: #00ff66; border-radius: 5px; border: none;")
        
        ld.addWidget(self.depth_bar_bg)
        box_d.setLayout(ld)
        layout.addWidget(box_d)
        
        layout.addStretch()
        self.setLayout(layout)
        
        self.active_signal = None
        self.frozen_risk_data = {}
        
    def update_data(self, data, m_state, dpoc_price=0.0):
        price = data.get('price', 0)
        trend = m_state.get('trend', 'NEUTRAL')
        
        if trend != 'NEUTRAL' and self.active_signal != trend:
            self.active_signal = trend
            sl_pct = 0.005
            tp_pct = 0.015
            
            if dpoc_price and dpoc_price > 0:
                if trend == 'ALCISTA':
                    sl = dpoc_price - (price * 0.0025)
                    tp = dpoc_price + (price * 0.005)
                else:
                    sl = dpoc_price + (price * 0.0025)
                    tp = dpoc_price - (price * 0.005)
            else:
                if trend == 'ALCISTA':
                    sl = price * (1 - sl_pct)
                    tp = price * (1 + tp_pct)
                else:
                    sl = price * (1 + sl_pct)
                    tp = price * (1 - tp_pct)
                
            risk_usd = 10.0
            price_risk = abs(price - sl)
            lot_size = risk_usd / price_risk if price_risk > 0 else 0
            
            self.frozen_risk_data = {
                'status': 'LONG' if trend == 'ALCISTA' else 'SHORT',
                'trigger': price,
                'sl': sl,
                'tp': tp,
                'lot': lot_size
            }
        elif trend == 'NEUTRAL':
            self.active_signal = None
            self.frozen_risk_data = {}
        
        if self.frozen_risk_data:
            st = self.frozen_risk_data['status']
            c_border = "#00ff66" if st == 'LONG' else "#bb00ff"
            self.box_a.setStyleSheet(f"border: 2px solid {c_border}; border-radius: 6px; background: #111;")
            self.val_status.setText(f"{st} ACTIVE")
            self.val_status.setStyleSheet(f"color: {c_border}; font-weight: bold; font-size: 11px; background: transparent;")
            self.val_trigger.setText(f"${self.frozen_risk_data['trigger']:,.1f}")
            self.val_sl.setText(f"${self.frozen_risk_data['sl']:,.1f}")
            self.val_tp.setText(f"${self.frozen_risk_data['tp']:,.1f}")
            self.val_lot.setText(f"{self.frozen_risk_data['lot']:.3f} BTC")
        else:
            self.box_a.setStyleSheet("border: 2px solid #555; border-radius: 6px; background: #111;")
            self.val_status.setText("WAITING")
            self.val_status.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
            self.val_trigger.setText("NONE")
            self.val_sl.setText("0.00")
            self.val_tp.setText("0.00")
            self.val_lot.setText("0.000")
            
        oi_1s = m_state.get('oi_delta_1s', 0)
        oi_5s = m_state.get('oi_delta_5s', 0)
        oi_1m = m_state.get('oi_delta_1m', 0)
        
        def fmt_oi(lbl, val):
            acc = 1.0 + (abs(val) * 10)
            color = "#00ff66" if val > 0.1 else "#bb00ff" if val < -0.1 else "#ccc"
            lbl.setText(f"{val:+.2f}% | x{acc:.1f}")
            lbl.setStyleSheet(f"color: {color}; font-size: 11px; font-family: monospace; background: transparent;")
            
        fmt_oi(self.lbl_oi_1s, oi_1s)
        fmt_oi(self.lbl_oi_5s, oi_5s)
        fmt_oi(self.lbl_oi_1m, oi_1m)
        
        score = 50
        for tf in ["1M", "5M", "15M", "1H"]:
            e_val = "BULL" if trend == 'ALCISTA' else "BEAR" if trend == 'BAJISTA' else "NEUT"
            s_val = e_val
            w_val = "UP" if m_state.get('delta', 0) > 0 else "DN"
            
            self.mtf_labels[f"EMA_{tf}"].setText(e_val)
            self.mtf_labels[f"SUP_{tf}"].setText(s_val)
            self.mtf_labels[f"WAV_{tf}"].setText(w_val)
            
            c_bull = "#00ff66"
            c_bear = "#bb00ff"
            c_neut = "#555"
            
            self.mtf_labels[f"EMA_{tf}"].setStyleSheet(f"color: {c_bull if e_val=='BULL' else c_bear if e_val=='BEAR' else c_neut}; font-size:10px; background: transparent;")
            self.mtf_labels[f"SUP_{tf}"].setStyleSheet(f"color: {c_bull if s_val=='BULL' else c_bear if s_val=='BEAR' else c_neut}; font-size:10px; background: transparent;")
            self.mtf_labels[f"WAV_{tf}"].setStyleSheet(f"color: {c_bull if w_val=='UP' else c_bear if w_val=='DN' else c_neut}; font-size:10px; background: transparent;")
            
            if e_val == 'BULL': score += 5
            elif e_val == 'BEAR': score -= 5
            
        score = max(0, min(100, score))
        c_score = "#00ff66" if score > 60 else "#bb00ff" if score < 40 else "#ffcc00"
        s_text = "BULLISH" if score > 60 else "BEARISH" if score < 40 else "NEUTRAL"
        self.lbl_score.setText(f"SCORE: {score}% {s_text}")
        self.lbl_score.setStyleSheet(f"color: #000; background: {c_score}; padding: 4px; border-radius: 4px; font-size: 12px; font-weight: bold; border: none;")
        
        ts = m_state.get('tick_speed', 0)
        ke = m_state.get('kaufman_eff', 0.5)
        ss = m_state.get('spread_velocity', 0)
        imb = m_state.get('depth_imbalance', 0)
        
        self.lbl_tick.setText(f"{ts:.1f}/s")
        self.lbl_kaufman.setText(f"{ke:.2f}")
        self.lbl_spread.setText(f"{ss:.1f}")
        
        fill_pct = max(0.0, min(1.0, (imb + 1) / 2))
        w = int(244 * fill_pct)
        self.depth_bar_fill.setFixedWidth(w)
        self.depth_bar_fill.setStyleSheet(f"background: {'#00ff66' if imb > 0 else '#bb00ff'}; border-radius: 5px; border: none;")

class MarketNarrativePanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #080a0f; border-radius: 8px; border: 1px solid #1a1f2e;")
        self.setMinimumWidth(270)
        self.setMaximumWidth(310)

        root = QVBoxLayout()
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)
        self.setLayout(root)

        # ── Header ─────────────────────────────────────────────────────────
        hdr = QLabel("🧠 NARRATIVA INSTITUCIONAL")
        hdr.setStyleSheet("color: #66fcf1; font-size: 13px; font-weight: 900; border: none; background: transparent; letter-spacing: 1px;")
        hdr.setAlignment(Qt.AlignCenter)
        root.addWidget(hdr)

        def section_title(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #445566; font-size: 9px; font-weight: bold; border: none; background: transparent; letter-spacing: 2px;")
            return lbl

        def html_label():
            lbl = QLabel()
            lbl.setTextFormat(Qt.RichText)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("border: none; background: transparent; font-size: 11px;")
            return lbl

        # ── 1. WHALE SONAR ─────────────────────────────────────────────────
        root.addWidget(section_title("▸ SONAR DE BALLENAS"))
        self.lbl_whale = html_label()
        root.addWidget(self.lbl_whale)

        # ── 2. INSTITUTIONAL POSITIONS ─────────────────────────────────────
        root.addWidget(section_title("▸ POSICIONES INSTITUCIONALES"))
        self.lbl_inst = html_label()
        root.addWidget(self.lbl_inst)

        # ── 3. LIQUIDITY TRAPS ─────────────────────────────────────────────
        root.addWidget(section_title("▸ TRAMPAS DE LIQUIDEZ"))
        self.lbl_traps = html_label()
        root.addWidget(self.lbl_traps)

        # ── 4. MARKET IMBALANCE ────────────────────────────────────────────
        root.addWidget(section_title("▸ DESEQUILIBRIO DE MERCADO"))
        self.lbl_imb_bar_bg = QFrame()
        self.lbl_imb_bar_bg.setFixedHeight(8)
        self.lbl_imb_bar_bg.setStyleSheet("background: #111620; border-radius: 4px; border: none;")
        self.lbl_imb_bar_fill = QFrame(self.lbl_imb_bar_bg)
        self.lbl_imb_bar_fill.setFixedHeight(8)
        self.lbl_imb_bar_fill.setStyleSheet("background: #00ff66; border-radius: 4px; border: none;")
        root.addWidget(self.lbl_imb_bar_bg)
        self.lbl_imb_text = html_label()
        root.addWidget(self.lbl_imb_text)

        # ── 5. MICROSTRUCTURE ──────────────────────────────────────────────
        root.addWidget(section_title("▸ MICROESTRUCTURA CUANTITATIVA"))
        self.lbl_micro = html_label()
        root.addWidget(self.lbl_micro)

        root.addStretch()

        # ── 6. DECISION ENGINE ─────────────────────────────────────────────
        self.lbl_decision = QLabel("ANALIZANDO...")
        self.lbl_decision.setStyleSheet("color: #ffcc00; font-size: 13px; font-weight: 900; background: #111620; padding: 12px; border-radius: 6px; border: 2px solid #ffcc00;")
        self.lbl_decision.setWordWrap(True)
        self.lbl_decision.setAlignment(Qt.AlignCenter)
        root.addWidget(self.lbl_decision)

        self.flash_timer = QTimer()
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(self.reset_flash)
        self.last_state = ""

    def reset_flash(self):
        self.setStyleSheet("background: #080a0f; border-radius: 8px; border: 1px solid #1a1f2e;")

    def trigger_flash(self, color):
        self.setStyleSheet(f"background: #080a0f; border-radius: 8px; border: 2px solid {color};")
        self.flash_timer.start(120)

    @staticmethod
    def _row(label, value, color):
        return (f"<tr>"
                f"<td style='color:#556677; padding:1px 4px;'>{label}</td>"
                f"<td align='right' style='color:{color}; font-weight:bold; padding:1px 4px;'>{value}</td>"
                f"</tr>")

    @staticmethod
    def _badge(text, bg, fg):
        return (f"<span style='background:{bg}; color:{fg}; padding:2px 6px; "
                f"border-radius:3px; font-weight:bold;'>{text}</span>")

    def update_narrative(self, state, order_state):
        if not state or not order_state:
            return

        # ── Raw data extraction ────────────────────────────────────────────
        buy_vol    = state.get('buy_volume', 0)
        sell_vol   = state.get('sell_volume', 0)
        total_vol  = buy_vol + sell_vol + 0.001
        delta      = buy_vol - sell_vol
        delta_pct  = (delta / total_vol) * 100
        ts         = state.get('tick_speed', 0)
        cvd        = state.get('cvd', 0)
        kaufman    = state.get('kaufman_eff', 0.5)
        spread_vel = state.get('spread_velocity', 0)
        trend      = state.get('trend', 'NEUTRAL')
        imb        = state.get('liquidity_data', {}).get('imbalance', 0)

        bids = order_state.get('bids', [])
        asks = order_state.get('asks', [])

        # Parsed order book (price, qty)
        bid_book = sorted([(float(b[0]), float(b[1])) for b in bids], key=lambda x: x[0], reverse=True)
        ask_book = sorted([(float(a[0]), float(a[1])) for a in asks], key=lambda x: x[0])

        total_bid_depth = sum(q for _, q in bid_book)
        total_ask_depth = sum(q for _, q in ask_book)
        depth_total     = total_bid_depth + total_ask_depth + 0.001
        depth_imb_pct   = ((total_bid_depth - total_ask_depth) / depth_total) * 100

        # Whale walls (>=5 BTC) and institutional walls (>=2 BTC)
        whale_bids = [(p, q) for p, q in bid_book if q >= 5.0]
        whale_asks = [(p, q) for p, q in ask_book if q >= 5.0]
        inst_bids  = [(p, q) for p, q in bid_book if 2.0 <= q < 5.0]
        inst_asks  = [(p, q) for p, q in ask_book if 2.0 <= q < 5.0]
        whale_bids.sort(key=lambda x: x[1], reverse=True)
        whale_asks.sort(key=lambda x: x[1], reverse=True)
        inst_bids.sort(key=lambda x: x[1], reverse=True)
        inst_asks.sort(key=lambda x: x[1], reverse=True)

        ba_ratio = buy_vol / max(0.001, sell_vol)

        # ── 1. WHALE SONAR ─────────────────────────────────────────────────
        if abs(delta_pct) > 35 and total_vol > 3:
            direction = "🔵 BALLENA COMPRADORA" if delta > 0 else "🔴 BALLENA VENDEDORA"
            w_color   = "#00ff66" if delta > 0 else "#bb00ff"
            whale_html = (f"<div style='background:#0a1520; padding:5px; border-left:3px solid {w_color}; border-radius:3px;'>"
                          f"<b style='color:{w_color};'>{direction}</b><br>"
                          f"<span style='color:#aaa; font-family:monospace;'>Δ {delta:+.2f} ₿ ({delta_pct:+.1f}%)</span><br>"
                          f"<span style='color:#aaa; font-family:monospace;'>Vel: {ts:.1f} ticks/s</span>"
                          f"</div>")
        elif abs(delta_pct) > 15 and total_vol > 3:
            direction = "🟢 AGRESIÓN COMPRADORA" if delta > 0 else "🟣 AGRESIÓN VENDEDORA"
            w_color   = "#00ff66" if delta > 0 else "#bb00ff"
            whale_html = (f"<div style='background:#0a1520; padding:5px; border-left:3px solid {w_color}; border-radius:3px;'>"
                          f"<b style='color:{w_color};'>{direction}</b><br>"
                          f"<span style='color:#aaa; font-family:monospace;'>Δ {delta:+.2f} ₿ ({delta_pct:+.1f}%)</span>"
                          f"</div>")
        else:
            whale_html = (f"<span style='color:#445566; font-family:monospace;'>⚪ Sin anomalías &nbsp; Δ {delta:+.2f} ₿ ({delta_pct:+.1f}%)</span>")

        self.lbl_whale.setText(whale_html)

        # ── 2. INSTITUTIONAL POSITIONS ─────────────────────────────────────
        inst_html = ""
        # Use z-score filtered walls from analyze_whale_walls() when available
        ld_pre = state.get('liquidity_data', {})
        if ld_pre and (ld_pre.get('buy_walls') or ld_pre.get('sell_walls')):
            whale_bids_display = [(w['price'], w['quantity']) for w in ld_pre.get('buy_walls', [])]
            whale_asks_display = [(w['price'], w['quantity']) for w in ld_pre.get('sell_walls', [])]
        else:
            whale_bids_display = whale_bids
            whale_asks_display = whale_asks

        for p, q in whale_bids_display[:2]:
            inst_html += (f"<div style='background:#002233; padding:3px 5px; margin:2px; border-left:3px solid #00ff66; border-radius:2px;'>"
                          f"<b style='color:#00ff66;'>🐋 BID {q:.1f}₿</b> "
                          f"<span style='color:#aaa;'>@ ${p:,.0f}</span></div>")
        for p, q in whale_asks_display[:2]:
            inst_html += (f"<div style='background:#1a0033; padding:3px 5px; margin:2px; border-left:3px solid #bb00ff; border-radius:2px;'>"
                          f"<b style='color:#bb00ff;'>🐋 ASK {q:.1f}₿</b> "
                          f"<span style='color:#aaa;'>@ ${p:,.0f}</span></div>")
        for p, q in inst_bids[:1]:
            inst_html += (f"<div style='background:#001a11; padding:3px 5px; margin:2px; border-left:3px solid #00ff66; border-radius:2px;'>"
                          f"<span style='color:#00ff66;'>🏦 INST BID {q:.1f}₿</span> "
                          f"<span style='color:#888;'>@ ${p:,.0f}</span></div>")
        for p, q in inst_asks[:1]:
            inst_html += (f"<div style='background:#1a0011; padding:3px 5px; margin:2px; border-left:3px solid #bb00ff; border-radius:2px;'>"
                          f"<span style='color:#bb00ff;'>🏦 INST ASK {q:.1f}₿</span> "
                          f"<span style='color:#888;'>@ ${p:,.0f}</span></div>")
        if not inst_html:
            inst_html = "<span style='color:#334455; font-family:monospace;'>Sin posiciones institucionales visibles</span>"
        self.lbl_inst.setText(inst_html)

        # ── 3. LIQUIDITY TRAPS ─────────────────────────────────────────────
        # Trap = large wall on one side + CVD divergence + HFT confluency
        trap_html = ""

        # Read HFT metrics from state (passed from dashboard)
        cancel_rate_narr = state.get('cancel_rate', 0.0)
        depth_imb_narr = state.get('depth_imb_pct', 0.0)
        tick_speed_narr = state.get('tick_speed', 0)
        delta_vel_narr = state.get('delta_accel', 0)

        # Use z-score filtered walls for trap detection
        bid_wall_near = whale_bids_display[0] if whale_bids_display else None
        ask_wall_near = whale_asks_display[0] if whale_asks_display else None

        # Trap OFF conditions
        if bid_wall_near or ask_wall_near:
            has_wall_narr = True
            cancel_ok = cancel_rate_narr > 55.0
            depth_ok = abs(depth_imb_narr) > 45.0
            tick_brake = abs(delta_vel_narr) * 10 > 500 and tick_speed_narr < 15

            if not cancel_ok or not depth_ok:
                # Legitimate S/R — mark as operational
                pass
            elif cancel_ok and depth_ok and tick_brake:
                # Bid trap: big bid wall but CVD falling (selling into support)
                if bid_wall_near and cvd < -2 and delta < 0:
                    trap_html += (f"<div style='background:#1a0808; padding:4px 5px; margin:2px; border-left:3px solid #FF2244; border-radius:2px;'>"
                                  f"<b style='color:#FF2244;'>🔴 TRAMPA ALCISTA</b><br>"
                                  f"<span style='color:#aaa; font-size:10px;'>Muro BID {bid_wall_near[1]:.1f}₿ @ ${bid_wall_near[0]:,.0f} con CVD bajista — stop hunt en curso</span>"
                                  f"</div>")

                # Ask trap: big ask wall but CVD rising (buying into resistance)
                if ask_wall_near and cvd > 2 and delta > 0:
                    trap_html += (f"<div style='background:#0a1a08; padding:4px 5px; margin:2px; border-left:3px solid #FF2244; border-radius:2px;'>"
                                  f"<b style='color:#FF2244;'>🔴 TRAMPA BAJISTA</b><br>"
                                  f"<span style='color:#aaa; font-size:10px;'>Muro ASK {ask_wall_near[1]:.1f}₿ @ ${ask_wall_near[0]:,.0f} con CVD alcista — fakeout en curso</span>"
                                  f"</div>")

        # Absorption: high vol + price not moving = absorption
        if ba_ratio > 0.7 and ba_ratio < 1.3 and total_vol > 5:
            trap_html += (f"<div style='background:#111a22; padding:4px 5px; margin:2px; border-left:3px solid #ffcc00; border-radius:2px;'>"
                          f"<b style='color:#ffcc00;'>⚡ ABSORCIÓN ACTIVA</b><br>"
                          f"<span style='color:#aaa; font-size:10px;'>B/A {ba_ratio:.2f}x — Institucional acumulando ambos lados</span>"
                          f"</div>")

        if not trap_html:
            trap_html = "<span style='color:#334455; font-family:monospace;'>Sin trampas detectadas</span>"
        self.lbl_traps.setText(trap_html)

        # ── 4. MARKET IMBALANCE BAR ────────────────────────────────────────
        fill_pct = max(0.0, min(1.0, (imb + 1) / 2))
        bar_w    = int((self.width() - 20) * fill_pct)
        if bar_w > 0:
            self.lbl_imb_bar_fill.setFixedWidth(bar_w)
        imb_color = "#00ff66" if imb > 0.2 else "#bb00ff" if imb < -0.2 else "#ffcc00"
        self.lbl_imb_bar_fill.setStyleSheet(f"background: {imb_color}; border-radius: 4px; border: none;")

        imb_label  = "BIDS DOMINAN" if depth_imb_pct > 10 else "ASKS DOMINAN" if depth_imb_pct < -10 else "EQUILIBRADO"
        imb_tcolor = "#00ff66" if depth_imb_pct > 10 else "#bb00ff" if depth_imb_pct < -10 else "#ffcc00"
        imb_html   = (f"<table width='100%' style='font-family:monospace; font-size:11px;'>"
                      f"{self._row('Depth Imb.', f'{depth_imb_pct:+.1f}%', imb_tcolor)}"
                      f"{self._row('Total Bids', f'{total_bid_depth:.1f}₿', '#00ff66')}"
                      f"{self._row('Total Asks', f'{total_ask_depth:.1f}₿', '#bb00ff')}"
                      f"</table>")
        self.lbl_imb_text.setText(imb_html)

        # ── 5. MICROSTRUCTURE ──────────────────────────────────────────────
        cvd_label   = "BULLISH ↑" if cvd > 2  else "BEARISH ↓" if cvd < -2 else "PLANO →"
        cvd_color   = "#00ff66"   if cvd > 2  else "#bb00ff"   if cvd < -2 else "#888"
        kauf_label  = "TENDENCIA" if kaufman > 0.6 else "RANGO"
        kauf_color  = "#00ff66"   if kaufman > 0.6 else "#ffcc00"
        ba_color    = "#00ff66"   if ba_ratio > 1.2 else "#bb00ff" if ba_ratio < 0.8 else "#888"
        sv_color    = "#bb00ff"   if spread_vel > 10 else "#888"

        micro_html = (f"<table width='100%' style='font-family:monospace; font-size:11px;'>"
                      f"{self._row('CVD Trend', cvd_label, cvd_color)}"
                      f"{self._row('Kaufman Eff.', kauf_label, kauf_color)}"
                      f"{self._row('Vol B/A', f'{ba_ratio:.2f}x', ba_color)}"
                      f"{self._row('Spread Vel.', f'{spread_vel:.1f}ms', sv_color)}"
                      f"{self._row('Tick Speed', f'{ts:.1f}/s', '#aaa')}"
                      f"</table>")
        self.lbl_micro.setText(micro_html)

        # ── 6. DECISION ENGINE ─────────────────────────────────────────────
        trap_active = "TRAMPA" in trap_html

        if trap_active:
            decision  = "⚠️ TRAMPA DETECTADA\nEvitar operar — manipulación activa"
            dec_color = "#FF2244"
        elif trend == 'ALCISTA' and depth_imb_pct > 10 and cvd > 2 and not whale_asks:
            decision  = "🟢 LONG CONFIRMADO\nAbsorción + Flujo comprador + Sin muros encima"
            dec_color = "#00ff66"
        elif trend == 'BAJISTA' and depth_imb_pct < -10 and cvd < -2 and not whale_bids:
            decision  = "🔴 SHORT CONFIRMADO\nRechazo + Flujo vendedor + Sin muros abajo"
            dec_color = "#bb00ff"
        elif abs(depth_imb_pct) > 20 and abs(delta_pct) > 20:
            decision  = "⚡ SEÑAL PARCIAL\nEsperando confluencia adicional"
            dec_color = "#ffcc00"
        else:
            decision  = "⏳ SIN VENTAJA\nChoppiness — No operar"
            dec_color = "#445566"

        self.lbl_decision.setText(decision)
        self.lbl_decision.setStyleSheet(
            f"color: {dec_color}; font-size: 12px; font-weight: 900; "
            f"background: #0d1117; padding: 10px; border-radius: 6px; "
            f"border: 2px solid {dec_color};"
        )
        new_state = decision.split()[0]
        if new_state != self.last_state:
            self.trigger_flash(dec_color)
            self.last_state = new_state

    def get_current_alert(self) -> str:
        """Return the current trap alert text, or empty string if none."""
        raw = self.lbl_traps.text()
        if "TRAMPA" in raw.upper():
            # Extract plain text — strip HTML
            import re as _re
            clean = _re.sub(r'<[^>]+>', '', raw).strip()
            return clean
        return ""


class KnowledgeParserWorker(QThread):
    """Background worker: scan, parse, clean .md files — EMIT-ONLY, no UI touch.
    All results are communicated via Qt signals.

    Signals
    -------
    progress_updated(int current, int total)
    log_message(str text)
    finished_with_data(list[str] blocks, list[str] filenames)
    """

    progress_updated = pyqtSignal(int, int)
    log_message = pyqtSignal(str)
    finished_with_data = pyqtSignal(list, list)

    def __init__(self, folder_path: str):
        super().__init__()
        self.folder_path = folder_path

    def run(self):
        md_files: list[str] = []
        md_filenames: list[str] = []
        for f in os.listdir(self.folder_path):
            if f.lower().endswith('.md'):
                md_files.append(os.path.join(self.folder_path, f))
                md_filenames.append(f)

        if not md_files:
            self.log_message.emit(
                "[Lector MD] ⚠️ No se encontraron archivos .md "
                "en la ruta seleccionada.")
            self.finished_with_data.emit([], [])
            return

        self.log_message.emit(
            f"[Lector MD] 📂 Escaneando {len(md_files)} archivos Markdown...")

        all_blocks: list[str] = []
        total = len(md_files)

        for i, filepath in enumerate(md_files):
            if self.isInterruptionRequested():
                self.log_message.emit(
                    "[Lector MD] ⛔ Procesamiento interrumpido por el usuario.")
                break
            filename = os.path.basename(filepath)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                cleaned = raw
                cleaned = re.sub(r'```[\s\S]*?```', '', cleaned)
                cleaned = re.sub(r'#{1,6}\s+', '', cleaned)
                cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', cleaned)
                cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
                cleaned = re.sub(r'`([^`]+)`', r'\1', cleaned)
                cleaned = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cleaned)
                cleaned = re.sub(r'[>\-\*\+]\s+', '', cleaned)
                cleaned = re.sub(r'!\[.*?\]\(.*?\)', '', cleaned)
                cleaned = re.sub(r'\|.*?\|', '', cleaned)
                cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
                cleaned = cleaned.strip()
                blocks = [
                    b.strip() for b in cleaned.split('\n\n')
                    if b.strip() and len(b.strip()) > 30
                ]
                all_blocks.extend(blocks)
                self.log_message.emit(
                    f"  ✓ {filename}: {len(blocks)} bloques extraídos")
            except Exception as e:
                self.log_message.emit(f"  ✗ {filename}: error — {e}")
            self.progress_updated.emit(i + 1, total)

        self.log_message.emit(
            f"[Cerebro Core] 🧠 {len(all_blocks)} bloques parseados. "
            f"Transfiriendo al pipeline de inferencia...")
        self.finished_with_data.emit(all_blocks, md_filenames)


class BrainInferenceWorker(QThread):
    """Background QThread for PyTorch brain inference.

    Receives a market snapshot, runs ``infer_sync()`` in a secondary
    thread so the UI event-loop is never blocked by tensor ops.
    All results are emitted via ``inference_finished(dict)``.
    """

    inference_finished = pyqtSignal(dict)

    def __init__(self, brain_agent, snapshot: dict,
                 knowledge_blocks: list[str] | None = None,
                 temperature: float = 0.5):
        super().__init__()
        self.brain_agent = brain_agent
        self.snapshot = snapshot
        self.knowledge_blocks = knowledge_blocks
        self.temperature = temperature

    def run(self):
        try:
            result = self.brain_agent.infer_sync(
                self.snapshot,
                knowledge_blocks=self.knowledge_blocks,
                temperature=self.temperature,
            )
            self.inference_finished.emit(result)
        except Exception as e:
            print(f"[⚠️ BRAIN WORKER] Error en inferencia: {e}")
            self.inference_finished.emit({})


class GeminiInferenceWorker(QThread):
    """Background QThread for Gemini 2.0 Flash inference.

    Runs ``execute_inference()`` in its own temporary asyncio event-loop
    so the UI thread is never blocked.  Emits a ``GeminiTradingDecision``
    or ``None`` via ``gemini_finished``.

    Supports inter-brain dialogue by receiving episodic_context and
    pytorch_metrics from the quantum brain.
    """

    gemini_finished = pyqtSignal(object)

    def __init__(self, gemini_brain: GeminiBrainManager,
                 snapshot: dict,
                 episodic_context: Optional[list] = None,
                 pytorch_metrics: Optional[dict] = None):
        super().__init__()
        self.gemini_brain = gemini_brain
        self.snapshot = snapshot
        self.episodic_context = episodic_context
        self.pytorch_metrics = pytorch_metrics

    def run(self):
        import asyncio
        try:
            result = asyncio.run(
                self.gemini_brain.execute_inference(
                    self.snapshot,
                    episodic_context=self.episodic_context,
                    pytorch_metrics=self.pytorch_metrics,
                )
            )
            self.gemini_finished.emit(result)
        except Exception as e:
            print(f"[⚠️ GEMINI WORKER] Error en inferencia: {e}")
            self.gemini_finished.emit(None)


class MainDashboard(QMainWindow):

    def __init__(self):
        super().__init__()
        self.data = {}
        self.running = True
        self._refresh_busy = False

        # Set leverage once at startup, never in the hot loop
        try:
            client.futures_change_leverage(symbol="BTCUSDT", leverage=100)
        except Exception:
            pass

        # Window flags: hide title bar, keep minimize/maximize buttons
        self.setWindowFlags(
            Qt.Window | Qt.CustomizeWindowHint
            | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint
        )

        self.init_ui()
        self.init_data()
        self.start_update_thread()

        self.telegram_bot = TelegramBot()
        self.telegram_bot.start()

        # Wire up OrderExecutor (unified trade execution, REAL / TESTNET)
        self.order_executor = OrderExecutor()
        self.order_executor.order_result.connect(self._on_order_result)

        # Wire up BrainAgent (Quantum Brain)
        try:
            from src.engine.quantum_brain import create_brain_agent
            self.brain_agent = create_brain_agent(
                telegram_queue=self.telegram_bot._queue,
                load_model=True
            )
            print("[🧠 BRAIN CORE] BrainAgent inicializado con pipeline de inferencia.")
        except Exception as e:
            print(f"[⚠️ BRAIN] Error inicializando BrainAgent: {e}")
            self.brain_agent = None

        # Wire up GeminiBrain (LLM hybrid engine)
        try:
            self.gemini_brain = GeminiBrainManager()
            if self.gemini_brain.is_enabled:
                print(f"[🧠 GEMINI BRAIN] GeminiBrainManager inicializado "
                      f"(modelo=gemini-2.0-flash)")
            else:
                print(f"[⚠️ GEMINI BRAIN] GeminiBrainManager deshabilitado — "
                      f"sin GEMINI_API_KEY")
        except Exception as e:
            print(f"[⚠️ GEMINI BRAIN] Error inicializando: {e}")
            self.gemini_brain = None

        # Auto-load last knowledge base from SQLite on startup
        self._load_persisted_knowledge_base()

        # ── Auto-Learner ────────────────────────────────────────────────
        try:
            from src.engine.auto_learner import AutoLearner
            self._auto_learner = AutoLearner(interval=30.0)
            # Wire up raw genai.Client (not GeminiBrainManager wrapper)
            if getattr(self, 'gemini_brain', None) is not None:
                raw_client = getattr(self.gemini_brain, '_client', None)
                self._auto_learner.set_gemini_client(raw_client)
            from src.engine.episodic_memory import EpisodicMemory
            self._auto_learner.set_episodic_memory(EpisodicMemory())
        except Exception as e:
            print(f"[⚠️ AUTO-LEARNER] Error: {e}")
            self._auto_learner = None

        # ── Brain inference throttle ────────────────────────────────────
        self._last_brain_time: float = 0.0
        self._last_candle_close: float = 0.0
        self._brain_cooldown: float = 1.0          # seconds
        self.brain_worker: BrainInferenceWorker | None = None
        self._pending_brain_snapshot: dict | None = None
        self._last_brain_decision: dict | None = None
        self._prev_cvd: float = 0.0

        # ── Gemini inference throttle ───────────────────────────────────
        self._last_gemini_time: float = 0.0
        self._gemini_cooldown: float = 3.0         # seconds — Gemini is slower
        self.gemini_worker: GeminiInferenceWorker | None = None
        self._last_gemini_decision: Optional['GeminiTradingDecision'] = None

        # ── Episodic memory & training ──────────────────────────────────
        self._last_retrain_time: float = 0.0
        self._last_alert_snapshot: dict | None = None
        self._last_alert_time: float = 0.0
        self._journal_pending: bool = False
    
    def init_ui(self):
        self.panels = {}
        self.setWindowTitle("BB-450 REELS MODE")
        
        # ── Adaptive screen sizing — full maximized ────────────────────
        self.setStyleSheet(f"background-color: #000000;")
        self.setWindowState(Qt.WindowMaximized)
        self.showMaximized()

        # ── Keyboard shortcuts ─────────────────────────────────────────
        self.shortcut_close = QShortcut(QKeySequence("Ctrl+C"), self)
        self.shortcut_close.activated.connect(self.close_application_cleanly)

        self.shortcut_escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.shortcut_escape.activated.connect(self.lower_to_normal_window)
        
        central = QWidget()
        self.setCentralWidget(central)
        
        # Top-level horizontal split: [Chart 65%] | [5-Col Panel 35%]
        from PyQt5.QtWidgets import QGridLayout, QSplitter
        
        root_layout = QHBoxLayout()
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(6)
        
        # ─── ROOT TABS ───
        self.tabs = QTabWidget()
        # Ocultar visualmente la barra de pestañas para controlarla solo con F1/F2
        self.tabs.tabBar().hide()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 0; background: #000; }
        """)
        
        # ─── TAB 1: ORDER FLOW TERMINAL ───
        tab1 = QWidget()
        tab1_layout = QVBoxLayout()
        tab1_layout.setContentsMargins(5, 5, 5, 5)
        tab1_layout.setSpacing(4)
        
        # Header (Small HUD inside Tab 1)
        header_widget = QWidget()
        header_widget.setStyleSheet(f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {COLORS['gradient_start']}, stop:1 {COLORS['gradient_end']}); border-bottom: 1px solid {COLORS['border_dim']};")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(15, 2, 15, 2)
        
        self.header_label = QLabel("BTCUSDT PRO MODE - ORDER FLOW")
        self.header_label.setStyleSheet(f"color: {COLORS['accent_turquoise']}; font-size: 16px; font-weight: 900; background: transparent;")
        
        header_layout.addWidget(self.header_label)
        header_layout.addStretch()
        
        self.status_indicator = QLabel("● API")
        self.status_indicator.setStyleSheet(f"color: {COLORS['accent_emerald']}; font-size: 11px; font-weight: bold; background: transparent;")
        header_layout.addWidget(self.status_indicator)
        
        self.latency_label = QLabel("0ms")
        self.latency_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 11px; font-family: monospace; background: transparent;")
        header_layout.addWidget(self.latency_label)
        
        self.tick_speed_label = QLabel("0 ticks/s")
        self.tick_speed_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 11px; font-family: monospace; background: transparent;")
        header_layout.addWidget(self.tick_speed_label)
        
        header_widget.setLayout(header_layout)
        tab1_layout.addWidget(header_widget)
        
        # HBox for Chart and Narrative
        hbox = QHBoxLayout()
        hbox.setContentsMargins(0,0,0,0)
        hbox.setSpacing(10)
        
        # Galaxy Order Flow Chart (Center) - con velas japonesas y Order Flow
        self.panels['HEATMAP'] = GalaxyOrderFlowChart("GALAXY ORDER FLOW")
        hbox.addWidget(self.panels['HEATMAP'], stretch=1)
        
        # Narrative Panel (Right)
        self.panels['NARRATIVE'] = MarketNarrativePanel()
        hbox.addWidget(self.panels['NARRATIVE'], stretch=0)
        
        tab1_layout.addLayout(hbox, stretch=1)
        
        # Symmetric Bottom Panels
        bottom_hbox = QHBoxLayout()
        bottom_hbox.setContentsMargins(0, 0, 0, 0)
        bottom_hbox.setSpacing(10)
        
        self.battle_bar = OrderFlowBattleBar()
        bottom_hbox.addWidget(self.battle_bar, stretch=1)
        
        self.trend_signal_bar = TrendSignalBar()
        bottom_hbox.addWidget(self.trend_signal_bar, stretch=1)
        
        tab1_layout.addLayout(bottom_hbox)
        
        tab1.setLayout(tab1_layout)
        self.tabs.addTab(tab1, "📈 ORDER FLOW (F1)")
        
        # ─── TAB 2: QUANTITATIVE MATRIX ───
        tab2 = QWidget()
        tab2_layout = QVBoxLayout()
        tab2_layout.setContentsMargins(5, 5, 5, 5)
        tab2_layout.setSpacing(4)
        
        # Matrix Header
        panel_header = QLabel("⚡ QUANT DATA MATRIX — MTF & ALGORITHMIC METRICS")
        panel_header.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 16px; font-weight: 900; background: transparent;")
        panel_header.setAlignment(Qt.AlignCenter)
        panel_header.setFixedHeight(24)
        tab2_layout.addWidget(panel_header)
        
        # 5 Columns Grid
        columns_widget = QWidget()
        columns_layout = QHBoxLayout()
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(6)
        
        self.grid_labels = {}
        
        # Define the 5 columns with EXTENDED metrics for Tab 2
        column_defs = [
            {
                'title': 'ORDER FLOW & OI',
                'color': COLORS['accent_cyan'],
                'metrics': [
                    'PRICE', 'CHANGE', 'BUY VOL', 'SELL VOL', 
                    'BUY/SELL RATIO', 'OB IMBALANCE', 'INS BLOCKS',
                    'OPEN INTEREST', 'FUNDING RATE', 'OI TREND'
                ],
                'bottom_widget': OIMomentumWidget
            },
            {
                'title': 'DELTA & LIQUIDITY',
                'color': COLORS['accent_turquoise'],
                'metrics': [
                    'CVD DELTA', 'DELTA VELOCITY', 'DELTA DIV',
                    'WALL BID #1', 'WALL BID #2', 
                    'WALL ASK #1', 'WALL ASK #2',
                    'LIQ ZONES', 'SUPPORT', 'RESISTANCE'
                ],
                'bottom_widget': LiquidityPoolWidget
            },
            {
                'title': 'MTF TREND (MULTI-TIMEFRAME)',
                'color': COLORS['accent_emerald'],
                'metrics': [
                    'TREND 1M', 'TREND 5M', 'TREND 15M', 'TREND 1H', 'TREND 4H',
                    'RSI 5M', 'RSI 15M', 'MACD 15M', 'MACD 1H', 'GLOBAL MACRO'
                ],
                'bottom_widget': ConfluenceMatrixWidget
            },
            {
                'title': 'MOMENTUM & VOLATILITY',
                'color': COLORS['accent_gold'],
                'metrics': [
                    'RSI (1M)', 'MACD (1M)', 'MACD HIST', 'FORCE',
                    'ATR (VOLATILITY)', 'BB UPPER', 'BB LOWER',
                    'BB SQUEEZE', 'TICK SPEED'
                ],
                'bottom_widget': HFTRiskWidget
            },
            {
                'title': 'AI ENGINE & LOG',
                'color': COLORS['accent_magenta'],
                'metrics': [
                    'AI SIGNAL', 'WIN RATE', 'LATENCY', 'EXHAUSTION',
                    'SCORE: ORDER FLOW', 'SCORE: MOMENTUM', 'SCORE: TREND',
                    'FINAL PREDICTION', 'LAST TRADE #1', 'LAST TRADE #2'
                ],
                'bottom_widget': AIBracketWidget
            },
        ]
        
        COLUMN_STYLE = (
            f"background: rgba(10,10,15,0.85); "
            f"border: 1px solid rgba(255,255,255,0.06); "
            f"border-radius: 8px;"
        )
        
        self.bottom_widgets = []
        
        for col_def in column_defs:
            col_frame = QFrame()
            col_frame.setStyleSheet(COLUMN_STYLE)
            col_layout = QVBoxLayout()
            col_layout.setContentsMargins(10, 10, 10, 10)
            col_layout.setSpacing(8)
            
            # Column header
            hdr = QLabel(col_def['title'])
            hdr.setStyleSheet(
                f"color: {col_def['color']}; font-size: 14px; font-weight: 900; "
                f"border-bottom: 2px solid {col_def['color']}; padding-bottom: 6px; "
                f"background: transparent;"
            )
            hdr.setAlignment(Qt.AlignCenter)
            col_layout.addWidget(hdr)
            
            # Top Panel (1/3 approx)
            top_panel = QWidget()
            top_panel.setStyleSheet("background: transparent;")
            top_layout = QVBoxLayout()
            top_layout.setContentsMargins(0, 0, 0, 0)
            top_layout.setSpacing(4)
            
            # Metric rows
            for metric in col_def['metrics']:
                row_w = QWidget()
                row_w.setStyleSheet("background: transparent;")
                row_l = QHBoxLayout()
                row_l.setContentsMargins(4, 0, 4, 0)
                row_l.setSpacing(4)
                
                nl = QLabel(metric)
                nl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px; font-weight: bold; background: transparent;")
                
                vl = QLabel("—")
                vl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: bold; background: transparent;")
                vl.setAlignment(Qt.AlignRight)
                
                row_l.addWidget(nl)
                row_l.addWidget(vl)
                row_w.setLayout(row_l)
                top_layout.addWidget(row_w)
                
                self.grid_labels[metric] = vl
            
            top_layout.addStretch()
            top_panel.setLayout(top_layout)
            col_layout.addWidget(top_panel, stretch=1)
            
            # Bottom Panel (2/3 approx)
            bottom_widget = col_def['bottom_widget']()
            col_layout.addWidget(bottom_widget, stretch=2)
            self.bottom_widgets.append(bottom_widget)
            
            col_frame.setLayout(col_layout)
            columns_layout.addWidget(col_frame)
        
        columns_widget.setLayout(columns_layout)
        tab2_layout.addWidget(columns_widget, stretch=1)
        
        # Footer
        footer = QLabel("POWERED BY BB-450 AI ⚡ DUAL-TAB PRO ARCHITECTURE")
        footer.setStyleSheet(f"color: {COLORS['accent_purple']}; font-size: 12px; font-weight: bold; background: transparent;")
        footer.setAlignment(Qt.AlignCenter)
        footer.setFixedHeight(20)
        tab2_layout.addWidget(footer)
        
        tab2.setLayout(tab2_layout)
        self.tabs.addTab(tab2, "📊 QUANT DATA (F2)")
        
        # Keyboard shortcuts to switch tabs
        QShortcut(QKeySequence("F1"), self).activated.connect(lambda: self.tabs.setCurrentIndex(0))
        QShortcut(QKeySequence("F2"), self).activated.connect(lambda: self.tabs.setCurrentIndex(1))
        
        # ─── TAB 3: QUANTUM BRAIN CONTROL PANEL ────────────────────────
        tab3 = QWidget()
        tab3_layout = QVBoxLayout()
        tab3_layout.setContentsMargins(10, 10, 10, 10)
        tab3_layout.setSpacing(8)

        # ═══════════════════════════════════════════════════════════════════
        # F3 — BRAIN OFFICE
        # ═══════════════════════════════════════════════════════════════════

        brain_title = QLabel("🧠 BRAIN OFFICE — CENTRAL DE CONOCIMIENTO")
        brain_title.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 16px; "
            f"font-weight: bold; background: transparent; padding: 4px;")
        brain_title.setAlignment(Qt.AlignCenter)
        tab3_layout.addWidget(brain_title)

        # ── KNOWLEDGE INGESTION ─────────────────────────────────────────
        ingest_frame = QFrame()
        ingest_frame.setStyleSheet(
            f"background: rgba(10,10,20,0.95); "
            f"border: 1px solid {COLORS['accent_cyan']}; border-radius: 6px;")
        ingest_layout = QHBoxLayout()
        ingest_layout.setContentsMargins(10, 8, 10, 8)
        ingest_layout.setSpacing(8)

        ingest_label = QLabel("📂 INGESTA DE CONOCIMIENTO")
        ingest_label.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 11px; "
            f"font-weight: bold; border: none; background: transparent;")
        ingest_layout.addWidget(ingest_label)

        self.btn_load_knowledge = QPushButton("📂 CARGAR (.md)")
        self.btn_load_knowledge.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0, 255, 102, 0.1);
                color: {COLORS['accent_cyan']};
                border: 1px solid {COLORS['accent_cyan']};
                border-radius: 4px; padding: 8px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover {{
                background: rgba(0, 255, 102, 0.25);
            }}
            QPushButton:pressed {{
                background: rgba(0, 255, 102, 0.35);
            }}
        """)
        ingest_layout.addWidget(self.btn_load_knowledge)
        ingest_layout.addStretch()

        # ── Knowledge Index Stats ───────────────────────────────────────
        kb_stats_label = QLabel("📊 KNOWLEDGE INDEX")
        kb_stats_label.setStyleSheet(
            f"color: {COLORS['accent_gold']}; font-size: 11px; "
            f"font-weight: bold; border: none; background: transparent;")
        ingest_layout.addWidget(kb_stats_label)

        self._kb_blocks_label = QLabel("0 reglas")
        self._kb_blocks_label.setStyleSheet(
            f"color: {COLORS['accent_turquoise']}; font-size: 12px; "
            f"font-weight: bold; border: none; background: transparent;")
        self._kb_blocks_label.setFixedWidth(80)
        ingest_layout.addWidget(self._kb_blocks_label)

        self._kb_size_label = QLabel("0 KB")
        self._kb_size_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10px; "
            f"border: none; background: transparent;")
        self._kb_size_label.setFixedWidth(70)
        ingest_layout.addWidget(self._kb_size_label)

        self._kb_search_label = QLabel("⏱ —")
        self._kb_search_label.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: 9px; "
            f"border: none; background: transparent;")
        self._kb_search_label.setFixedWidth(50)
        ingest_layout.addWidget(self._kb_search_label)

        ingest_frame.setLayout(ingest_layout)

        # ── QSPLITTER: TWO-COLUMN LAYOUT ──────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {COLORS['border_dim']};
                width: 2px;
            }}
        """)

        # LEFT PANEL: FILE EXPLORER
        left_panel = QFrame()
        left_panel.setStyleSheet(
            f"background: rgba(10,10,20,0.95); "
            f"border: 1px solid {COLORS['border_dim']}; border-radius: 6px;")
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(4)

        left_label = QLabel("📁 EXPLORADOR DE CONOCIMIENTO")
        left_label.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 10px; "
            f"font-weight: bold; border: none; background: transparent;")
        left_layout.addWidget(left_label)

        self.brain_files_list = QListWidget()
        self.brain_files_list.setStyleSheet(f"""
            QListWidget {{
                background: #050510;
                color: {COLORS['text_secondary']};
                border: 1px solid {COLORS['border_dim']};
                border-radius: 4px;
                font-family: 'Courier New', monospace;
                font-size: 10px; padding: 4px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-bottom: 1px solid rgba(255,255,255,0.03);
            }}
            QListWidget::item:selected {{
                background: rgba(0, 212, 255, 0.15);
                color: {COLORS['accent_cyan']};
                border-left: 3px solid {COLORS['accent_cyan']};
            }}
            QListWidget::item:hover {{
                background: rgba(0, 212, 255, 0.06);
            }}
        """)
        self.brain_files_list.setMinimumWidth(160)
        left_layout.addWidget(self.brain_files_list)
        left_panel.setLayout(left_layout)
        splitter.addWidget(left_panel)

        # RIGHT PANEL: CONTENT VIEWER + MONITOR + LOG
        right_panel = QFrame()
        right_panel.setStyleSheet(
            f"background: rgba(10,10,20,0.95); "
            f"border: 1px solid {COLORS['border_dim']}; border-radius: 6px;")
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(6, 6, 6, 6)
        right_layout.setSpacing(6)

        # Keep brain_content_viewer hidden (referenced by callbacks)
        self.brain_content_viewer = QPlainTextEdit()
        self.brain_content_viewer.setVisible(False)

        # ── Dual AI Engine Monitor (expanded — visor de contenido eliminado) ──
        ai_monitor_label = QLabel("🌌 MONITOR DE MOTORES (LSTM + GEMINI)")
        ai_monitor_label.setStyleSheet(
            f"color: {COLORS['accent_gold']}; font-size: 10px; "
            f"font-weight: bold; border: none; background: transparent;")
        right_layout.addWidget(ai_monitor_label)

        self.ai_engines_monitor = QTextBrowser()
        self.ai_engines_monitor.setOpenExternalLinks(False)
        self.ai_engines_monitor.setStyleSheet(f"""
            QTextBrowser {{
                background: #0B0B0B;
                color: {COLORS['text_secondary']};
                border: 1px solid {COLORS['border_dim']};
                border-radius: 4px;
                font-family: 'Courier New', monospace;
                font-size: 9px; padding: 6px;
            }}
        """)
        self.ai_engines_monitor.setMinimumHeight(160)
        right_layout.addWidget(self.ai_engines_monitor, stretch=3)

        # ── Auto-Learner Log ───────────────────────────────────────────
        learn_log_label = QLabel("🎓 AUTO-LEARNER LOG")
        learn_log_label.setStyleSheet(
            f"color: {COLORS['accent_turquoise']}; font-size: 10px; "
            f"font-weight: bold; border: none; background: transparent;")
        right_layout.addWidget(learn_log_label)

        self._learning_log = QTextBrowser()
        self._learning_log.setOpenExternalLinks(False)
        self._learning_log.setStyleSheet(f"""
            QTextBrowser {{
                background: #080808;
                color: {COLORS['text_dim']};
                border: 1px solid {COLORS['border_dim']};
                border-radius: 4px;
                font-family: 'Courier New', monospace;
                font-size: 9px; padding: 4px;
            }}
        """)
        self._learning_log.setMinimumHeight(60)
        self._learning_log.setMaximumHeight(120)
        right_layout.addWidget(self._learning_log)

        self.brain_console = QPlainTextEdit()
        self.brain_console.setReadOnly(True)
        self.brain_console.setMaximumBlockCount(200)
        self.brain_console.setStyleSheet(f"""
            QPlainTextEdit {{
                background: #050510;
                color: {COLORS['text_secondary']};
                border: 1px solid {COLORS['border_dim']};
                border-radius: 4px;
                font-family: 'Courier New', monospace;
                font-size: 10px; padding: 6px;
            }}
        """)
        self.brain_console.setMinimumHeight(40)
        right_layout.addWidget(self.brain_console, stretch=1)

        self.brain_progress = QProgressBar()
        self.brain_progress.setRange(0, 100)
        self.brain_progress.setValue(0)
        self.brain_progress.setTextVisible(True)
        self.brain_progress.setStyleSheet(f"""
            QProgressBar {{
                background: #111;
                border: 1px solid {COLORS['accent_cyan']};
                border-radius: 3px; height: 18px;
                text-align: center; font-size: 10px;
                color: {COLORS['accent_cyan']};
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {COLORS['accent_cyan']},
                    stop:1 {COLORS['accent_turquoise']});
                border-radius: 2px;
            }}
        """)
        right_layout.addWidget(self.brain_progress)
        right_panel.setLayout(right_layout)
        splitter.addWidget(right_panel)
        splitter.setSizes([250, 750])

        # ── BRAIN PARAMETERS + AUTO-LEARNER TOGGLE ──────────────────────
        params_frame = QFrame()
        params_frame.setStyleSheet(
            f"background: rgba(10,10,20,0.95); "
            f"border: 1px solid {COLORS['accent_cyan']}; border-radius: 6px;")
        params_layout = QHBoxLayout()
        params_layout.setContentsMargins(10, 6, 10, 6)
        params_layout.setSpacing(12)

        # Temperature slider
        temp_label = QLabel("🎛️ TEMP")
        temp_label.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 10px; "
            f"font-weight: bold; border: none; background: transparent;")
        params_layout.addWidget(temp_label)

        self.temp_slider = QSlider(Qt.Horizontal)
        self.temp_slider.setRange(0, 100)
        self.temp_slider.setValue(50)
        self.temp_slider.setFixedWidth(120)
        self.temp_slider.setTickPosition(QSlider.TicksBelow)
        self.temp_slider.setTickInterval(10)
        self.temp_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: #222; height: 4px; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {COLORS['accent_cyan']};
                width: 12px; height: 12px;
                margin: -4px 0; border-radius: 6px;
            }}
            QSlider::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0044aa,
                    stop:1 {COLORS['accent_cyan']});
                border-radius: 2px;
            }}
        """)
        params_layout.addWidget(self.temp_slider)

        self.temp_value_label = QLabel("0.50")
        self.temp_value_label.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 11px; "
            f"font-weight: bold; border: none; background: transparent;")
        self.temp_value_label.setFixedWidth(36)
        params_layout.addWidget(self.temp_value_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {COLORS['border_dim']};")
        sep.setFixedWidth(1)
        params_layout.addWidget(sep)

        # Brain stats
        self._brain_latency_label = QLabel("⏱ —")
        self._brain_latency_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 9px; "
            f"border: none; background: transparent;")
        params_layout.addWidget(self._brain_latency_label)

        self._brain_acc_label = QLabel("🎯 —")
        self._brain_acc_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 9px; "
            f"border: none; background: transparent;")
        params_layout.addWidget(self._brain_acc_label)

        self._brain_mem_label = QLabel("💾 —")
        self._brain_mem_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 9px; "
            f"border: none; background: transparent;")
        params_layout.addWidget(self._brain_mem_label)

        params_layout.addStretch()

        # Auto-Learner toggle
        self._btn_auto_learn = QPushButton("🎓 AUTO-LEARN OFF")
        self._btn_auto_learn.setCheckable(True)
        self._btn_auto_learn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.05);
                color: {COLORS['text_dim']};
                border: 1px solid {COLORS['border_dim']};
                border-radius: 4px; padding: 6px 12px;
                font-size: 10px; font-weight: bold;
            }}
            QPushButton:checked {{
                background: rgba(0, 255, 102, 0.15);
                color: {COLORS['accent_turquoise']};
                border: 1px solid {COLORS['accent_turquoise']};
            }}
            QPushButton:hover {{
                background: rgba(0, 255, 102, 0.1);
            }}
        """)
        params_layout.addWidget(self._btn_auto_learn)
        params_frame.setLayout(params_layout)

        self.brain_status = QLabel(
            "🧠 CEREBRO CUÁNTICO: INACTIVO — Sin conocimiento cargado")
        self.brain_status.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: 10px; "
            f"background: transparent; padding: 2px;")
        self.brain_status.setAlignment(Qt.AlignCenter)

        # F3 layout assembly
        tab3_layout.addWidget(ingest_frame)
        tab3_layout.addWidget(splitter, stretch=1)
        tab3_layout.addWidget(params_frame)
        tab3_layout.addWidget(self.brain_status)

        tab3.setLayout(tab3_layout)
        self.tabs.addTab(tab3, "🧠 BRAIN (F3)")

        # Keyboard shortcuts to switch tabs (F3 added)
        QShortcut(QKeySequence("F3"), self).activated.connect(
            lambda: self.tabs.setCurrentIndex(2))

        # Wire up brain UI signals
        self.btn_load_knowledge.clicked.connect(self._on_load_knowledge)
        self.temp_slider.valueChanged.connect(self._on_temp_changed)
        self.brain_files_list.itemClicked.connect(self._on_knowledge_file_selected)
        self._btn_auto_learn.clicked.connect(self._on_toggle_auto_learn)
        self._brain_worker: KnowledgeParserWorker | None = None
        self._brain_knowledge_blocks: list[str] = []
        self.brain_temperature: float = 0.50
        self._brain_folder_path: str = ""

        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(self.tabs)
        central.setLayout(central_layout)
        
        self.indicator_widgets = {}

        # Restore last window geometry (size/position) if available
        settings = QSettings("BB-450", "Dashboard")
        geo = settings.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        """Save window geometry on close."""
        settings = QSettings("BB-450", "Dashboard")
        settings.setValue("window_geometry", self.saveGeometry())
        self.running = False
        super().closeEvent(event)

    # ── Brain control callbacks ─────────────────────────────────────────

    def _on_load_knowledge(self):
        """Open folder dialog → launch KnowledgeParserWorker (non-blocking)."""
        initial_dir = os.path.join(os.getcwd(), "CONCMT")
        if not os.path.isdir(initial_dir):
            initial_dir = os.getcwd()
        folder = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar Carpeta de Conocimiento (.md)",
            initial_dir,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks)
        if not folder:
            print("[📂 SYSTEM] Selección de carpeta cancelada por el usuario.")
            return

        print(f"[📂 SYSTEM] Ruta seleccionada con éxito: {folder}")
        self._brain_folder_path = folder

        if self._brain_worker is not None:
            self._brain_worker.requestInterruption()
            self._brain_worker.deleteLater()
            self._brain_worker = None

        self.brain_console.clear()
        self.brain_progress.setValue(0)
        self.brain_status.setText(
            "🧠 CEREBRO CUÁNTICO: INGIRIENDO CONOCIMIENTO...")
        self.brain_status.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 10px; "
            f"background: transparent; padding: 2px;")
        self.btn_load_knowledge.setEnabled(False)
        self.brain_files_list.clear()
        self.brain_content_viewer.clear()

        self._brain_worker = KnowledgeParserWorker(folder)
        self._brain_worker.progress_updated.connect(
            self._on_brain_progress)
        self._brain_worker.log_message.connect(self._on_brain_log)
        self._brain_worker.finished_with_data.connect(
            self._on_brain_data_ready)
        self._brain_worker.start()

    def _on_brain_data_ready(self, blocks: list, filenames: list):
        """Atomic handoff from worker — runs in main thread via signal."""
        self._brain_knowledge_blocks = blocks

        self.brain_files_list.clear()
        for fname in filenames:
            self.brain_files_list.addItem(fname)

        if blocks and getattr(self, 'brain_agent', None) is not None:
            self.brain_agent.set_knowledge_blocks(blocks)
            print(
                f"[🧠 BRAIN CORE] Sincronización Exitosa: "
                f"{len(blocks)} bloques de conocimiento "
                f"vinculados al pipeline de inferencia.")

        # Persist last knowledge base path to SQLite
        if self._brain_folder_path:
            try:
                db_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'bb450_trades.db')
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS system_config "
                    "(key TEXT PRIMARY KEY, value TEXT)")
                conn.execute(
                    "INSERT OR REPLACE INTO system_config (key, value) "
                    "VALUES (?, ?)",
                    ('last_knowledge_base', self._brain_folder_path))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[⚠️ DB] Error persistiendo ruta de conocimiento: {e}")

        if self._brain_worker is not None:
            self._brain_worker.deleteLater()
            self._brain_worker = None

        self.btn_load_knowledge.setEnabled(True)
        if blocks:
            self.brain_status.setText(
                f"🧠 CEREBRO CUÁNTICO: ACTIVO — "
                f"{len(blocks)} reglas de bitácora cargadas")
            self.brain_status.setStyleSheet(
                f"color: {COLORS['accent_turquoise']}; font-size: 10px; "
                f"background: transparent; padding: 2px;")
            self.brain_progress.setValue(100)
        else:
            self.brain_status.setText(
                "🧠 CEREBRO CUÁNTICO: INACTIVO — Sin conocimiento cargado")
            self.brain_status.setStyleSheet(
                f"color: {COLORS['text_dim']}; font-size: 10px; "
                f"background: transparent; padding: 2px;")

    def _on_brain_progress(self, current: int, total: int):
        pct = int(current / max(total, 1) * 100)
        self.brain_progress.setValue(pct)

    def _on_brain_log(self, text: str):
        self.brain_console.appendPlainText(text)

    def _on_temp_changed(self, value: int):
        temp = value / 100.0
        self.brain_temperature = temp
        self.temp_value_label.setText(f"{temp:.2f}")
        if temp < 0.3:
            hue = COLORS['accent_cyan']
        elif temp < 0.7:
            hue = COLORS['accent_gold']
        else:
            hue = COLORS['accent_magenta']
        self.temp_value_label.setStyleSheet(
            f"color: {hue}; font-size: 11px; font-weight: bold; "
            f"border: none; background: transparent;")

    def _on_toggle_auto_learn(self):
        """Toggle Auto-Learner on/off."""
        if self._auto_learner is None:
            self._btn_auto_learn.setChecked(False)
            return
        enabled = self._auto_learner.toggle()
        if enabled:
            self._btn_auto_learn.setText("🎓 AUTO-LEARN ON")
            self._btn_auto_learn.setChecked(True)
            print("[🎓 AUTO-LEARNER] Activado")
        else:
            self._btn_auto_learn.setText("🎓 AUTO-LEARN OFF")
            self._btn_auto_learn.setChecked(False)
            print("[🎓 AUTO-LEARNER] Desactivado")

    def _run_auto_learn_analysis(self, snapshot: dict):
        """Run one auto-learn analysis cycle (non-blocking in main loop)."""
        if self._auto_learner is None or not self._auto_learner.enabled:
            return
        if not self._auto_learner.should_analyze():
            return
        try:
            brain = getattr(self, '_last_brain_decision', {}) or {}
            brain_stats = self.brain_agent.get_stats() if getattr(self, 'brain_agent', None) else {}
            memory = {}
            try:
                from src.engine.episodic_memory import EpisodicMemory
                mem = EpisodicMemory()
                memory = mem.stats()
            except Exception:
                pass
            knowledge = {}
            if getattr(self, 'brain_agent', None) is not None:
                try:
                    knowledge = self.brain_agent._knowledge_index.stats()
                except Exception:
                    pass
            training = self.brain_agent.get_training_stats() if getattr(self, 'brain_agent', None) else {}

            result = self._auto_learner.analyze(
                snapshot, brain_stats, memory, knowledge, training
            )
            if result:
                # Update the learning log widget
                log_entries = self._auto_learner.get_log(15)
                html = '<br>'.join(log_entries)
                self._learning_log.setHtml(html)
        except Exception as e:
            print(f"[⚠️ AUTO-LEARN] Error en análisis: {e}")

    def _update_brain_office(self):
        """Refresh Brain Office metric labels every update cycle."""
        # Knowledge Index stats
        if getattr(self, 'brain_agent', None) is not None:
            try:
                kstats = self.brain_agent._knowledge_index.stats()
                self._kb_blocks_label.setText(f"{kstats['blocks']} reglas")
                self._kb_size_label.setText(f"{kstats['size_kb']} KB")
            except Exception:
                pass

        # Brain agent stats
        if getattr(self, 'brain_agent', None) is not None:
            try:
                stats = self.brain_agent.get_stats()
                lat = stats.get('avg_latency_ms', 0)
                self._brain_latency_label.setText(
                    f"⏱ {lat:.0f}ms" if lat else "⏱ —")
                acc = stats.get('avg_accuracy', 0)
                self._brain_acc_label.setText(
                    f"🎯 {acc:.1f}%" if acc else "🎯 —")
                mem_s = stats.get('episodic_memory', {})
                self._brain_mem_label.setText(
                    f"💾 {mem_s.get('total_records', 0)} ev / "
                    f"{mem_s.get('failed', 0)} F")
            except Exception:
                pass

    def _on_knowledge_file_selected(self, item):
        """Display content of the selected .md file in the viewer."""
        if not self._brain_folder_path:
            return
        fname = item.text()
        fpath = os.path.join(self._brain_folder_path, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.brain_content_viewer.setPlainText(content)
        except Exception as e:
            self.brain_content_viewer.setPlainText(
                f"[ERROR] No se pudo leer {fname}: {e}")

    def _load_persisted_knowledge_base(self):
        """Auto-load last knowledge base from SQLite on startup."""
        try:
            db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'bb450_trades.db')
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS system_config "
                "(key TEXT PRIMARY KEY, value TEXT)")
            cursor = conn.execute(
                "SELECT value FROM system_config WHERE key = ?",
                ('last_knowledge_base',))
            row = cursor.fetchone()
            conn.close()
            if row is None:
                return
            folder = row[0]
            if not os.path.isdir(folder):
                print(f"[📂 SYSTEM] Ruta persistida no válida: {folder}")
                return
            print(f"[📂 SYSTEM] Auto-cargando base de conocimiento: {folder}")
            self._brain_folder_path = folder
            self.brain_files_list.clear()
            self.brain_content_viewer.clear()
            md_filenames = sorted(
                f for f in os.listdir(folder)
                if f.lower().endswith('.md'))
            if not md_filenames:
                print("[📂 SYSTEM] No hay archivos .md en la carpeta persistida.")
                return
            for fname in md_filenames:
                self.brain_files_list.addItem(fname)
            self.brain_status.setText(
                f"🧠 CEREBRO CUÁNTICO: CARGANDO {len(md_filenames)} archivos...")
            self.brain_status.setStyleSheet(
                f"color: {COLORS['accent_cyan']}; font-size: 10px; "
                f"background: transparent; padding: 2px;")
            self.brain_progress.setValue(0)
            self.btn_load_knowledge.setEnabled(False)
            self._brain_worker = KnowledgeParserWorker(folder)
            self._brain_worker.progress_updated.connect(
                self._on_brain_progress)
            self._brain_worker.log_message.connect(self._on_brain_log)
            self._brain_worker.finished_with_data.connect(
                self._on_brain_data_ready)
            self._brain_worker.start()
        except Exception as e:
            print(f"[⚠️ SYSTEM] Error en auto-carga de conocimiento: {e}")

    def init_data(self):
        self.data = {
            'price': 0.0, 'price_change': 0.0, 'price_change_pct': 0.0,
            'rsi': 50.0, 'macd': 0.0, 'macd_signal': 0.0, 'macd_hist': 0.0,
            'bb_upper': 0.0, 'bb_middle': 0.0, 'bb_lower': 0.0, 'bb_position': 50.0,
            'atr': 0.0, 'ema_20': 0.0, 'ema_50': 0.0, 'delta': 0.0, 'cvd': 0.0,
            'buy_volume': 0.0, 'sell_volume': 0.0, 'signal': 'NINGUNA',
            'trend': 'NEUTRAL', 'daily_pnl': 0.0, 'trade_count': 0,
            'win_rate': 0.0, 'last_price': 0.0, 'klines': [],
            'positions': [],
            'order_book': {'bids': [], 'asks': []},
            'liquidity_data': {'buy_walls': [], 'sell_walls': [], 'imbalance': 0, 'signal': 'NEUTRAL'}
        }
        
        self.market_state = {
            "order_flow": {
                "price": 0.0, "change": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "ratio": 0.0,
                "ob_imbalance": 0.0, "ins_blocks": "B:0 A:0", "open_interest": 0.0, "funding_rate": 0.0,
                "oi_trend": "NEUTRAL", "oi_delta_1s": 0.0, "oi_delta_5s": 0.0, "oi_delta_1m": 0.0
            },
            "liquidity": {
                "cvd_delta": 0.0, "delta_velocity": 0.0, "delta_div": "NONE",
                "wall_bid_1": 0.0, "wall_bid_size_1": 0.0, "wall_ask_1": 0.0, "wall_ask_size_1": 0.0,
                "liq_zones": 0, "support": 0.0, "resistance": 0.0,
                "liq_pool_10x": 0.0, "liq_pool_25x": 0.0, "liq_pool_50x": 0.0, "liq_pool_100x": 0.0,
                "wall_bid_2": 0.0, "wall_bid_size_2": 0.0, "wall_ask_2": 0.0, "wall_ask_size_2": 0.0,
                "depth_imbalance": 0.0
            },
            "mtf_trend": {
                "t_1m": "WAIT", "t_5m": "WAIT", "t_15m": "WAIT", "t_1h": "WAIT", "t_4h": "WAIT",
                "rsi_5m": 0.0, "rsi_15m": 0.0, "macd_15m": 0.0, "macd_1h": 0.0, "global_macro": "NEUTRAL",
                "ema_cross_5m": "NEUTRAL", "ema_cross_15m": "NEUTRAL", "ema_cross_1h": "NEUTRAL",
                "confluence_score": 0.0
            },
            "momentum": {
                "rsi_1m": 0.0, "macd_1m": 0.0, "macd_hist": 0.0, "force": "NONE",
                "atr": 0.0, "bb_upper": 0.0, "bb_lower": 0.0, "bb_squeeze": "NORMAL",
                "tick_speed": 0, "kaufman_efficiency": 0.0,
                "cancel_rate": 0.0, "skewness": 0.0, "spread_raw": 0.0,
                "spread_velocity": 0.0, "pinam": 0.0
            },
            "ai_engine": {
                "ai_signal": "NINGUNA", "win_rate": 0.0, "latency": 0, "exhaustion": "NONE",
                "score_of": 0.0, "score_mom": 0.0, "score_trend": 0.0, "final_prediction": "WAIT",
                "last_trade_1": "WAITING...", "last_trade_2": "WAITING...",
                "risk_panel": {
                    "status": "WAITING", "trigger": 0.0, "sl": 0.0, "tp1": 0.0, "tp2": 0.0, "lot_size": 0.0
                }
            }
        }
        
        self.stats = {
            'update_count': 0,
            'latency_ms': 0,
            'last_update': 'Iniciando...',
            'api_connected': False,
            'db_connected': False,
            'klines_count': 0,
            'prev_signal': 'NINGUNA',
            'uptime_seconds': 0,
            'start_time': None
        }
    
    def start_update_thread(self):
        import time
        self.stats['start_time'] = time.time()
        
        # Start the async data engine in background thread
        self._async_engine = AsyncDataEngine(self.market_state)
        self._async_engine.start()

        self.update_data()
    
    def format_number(self, num, decimals=2):
        if abs(num) >= 1000:
            return f"{num:,.0f}"
        return f"{num:,.2f}"
    
    def calculate_ema(self, prices, period):
        if len(prices) < period:
            return 0
        return sum(prices[-period:]) / period
    
    def get_price(self):
        try:
            ticker = client.futures_symbol_ticker(symbol="BTCUSDT")
            return float(ticker['price'])
        except:
            return self.data['price']
    
    def get_klines(self):
        try:
            return client.futures_klines(symbol="BTCUSDT", interval="1m", limit=200)
        except:
            return []
    
    def get_trades(self):
        try:
            return client.futures_aggregate_trades(symbol="BTCUSDT", limit=50)
        except:
            return []
    
    def get_order_book(self):
        try:
            return client.futures_order_book(symbol="BTCUSDT", limit=20)
        except:
            return {'bids': [], 'asks': []}
    
    def get_db_size(self):
        try:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bb450_trades.db')
            if os.path.exists(db_path):
                size_bytes = os.path.getsize(db_path)
                if size_bytes >= 1024 * 1024 * 1024:
                    return f"{size_bytes / (1024**3):.2f} GB"
                elif size_bytes >= 1024 * 1024:
                    return f"{size_bytes / (1024**2):.1f} MB"
                else:
                    return f"{size_bytes / 1024:.1f} KB"
            return "0 KB"
        except:
            return "N/A"
    
    def analyze_whale_walls(self, order_book):
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        # Collect all level quantities for z-score history
        all_qties = [float(q) for _, q in bids] + [float(q) for _, q in asks]
        if all_qties:
            self._ob_volume_history.extend(all_qties)

        # Compute z-score threshold from rolling distribution
        z_threshold = 3.0
        min_samples = 30
        if len(self._ob_volume_history) >= min_samples:
            arr = list(self._ob_volume_history)
            mean = sum(arr) / len(arr)
            variance = sum((x - mean) ** 2 for x in arr) / len(arr)
            std = variance ** 0.5 if variance > 0 else 1.0
            dynamic_min = mean + z_threshold * std
        else:
            # Cold start: use conservative absolute minimum (10 BTC) until history fills
            dynamic_min = 10.0

        whale_buy_walls = []
        whale_sell_walls = []
        
        for price, qty in bids:
            qty_float = float(qty)
            if qty_float >= dynamic_min:
                whale_buy_walls.append({
                    'price': float(price),
                    'quantity': qty_float,
                    'z_score': ((qty_float - mean) / std) if len(self._ob_volume_history) >= min_samples and std > 0 else 0,
                    'total_usd': qty_float * float(price)
                })
        
        for price, qty in asks:
            qty_float = float(qty)
            if qty_float >= dynamic_min:
                whale_sell_walls.append({
                    'price': float(price),
                    'quantity': qty_float,
                    'z_score': ((qty_float - mean) / std) if len(self._ob_volume_history) >= min_samples and std > 0 else 0,
                    'total_usd': qty_float * float(price)
                })
        
        whale_buy_walls.sort(key=lambda x: x['quantity'], reverse=True)
        whale_sell_walls.sort(key=lambda x: x['quantity'], reverse=True)
        
        total_buy = sum(w['quantity'] for w in whale_buy_walls)
        total_sell = sum(w['quantity'] for w in whale_sell_walls)
        
        imbalance = (total_buy - total_sell) / (total_buy + total_sell + 0.001)
        
        return {
            'buy_walls': whale_buy_walls[:5],
            'sell_walls': whale_sell_walls[:5],
            'total_buy_walls': total_buy,
            'total_sell_walls': total_sell,
            'imbalance': imbalance,
            'signal': 'BUY_WALL' if imbalance > 0.3 else 'SELL_WALL' if imbalance < -0.3 else 'NEUTRAL'
        }
    
    def calculate_ai_prediction(self):
        rsi = self.data.get('rsi', 50)
        macd_hist = self.data.get('macd_hist', 0)
        vwap = self.data.get('vwap', 0)
        price = self.data.get('price', 0)
        delta = self.data.get('delta', 0)
        trend = self.data.get('trend', 'NEUTRAL')
        
        rsi_score = 50
        if rsi < 30:
            rsi_score = 80
        elif rsi > 70:
            rsi_score = 20
        elif rsi < 40:
            rsi_score = 65
        elif rsi > 60:
            rsi_score = 35
        
        macd_score = 50
        if macd_hist > 0:
            macd_score = min(80, 50 + macd_hist * 10)
        else:
            macd_score = max(20, 50 + macd_hist * 10)
        
        vwap_score = 50
        if vwap > 0 and price > vwap:
            vwap_score = 70
        elif vwap > 0 and price < vwap:
            vwap_score = 30
        
        delta_score = 50
        if delta > 100:
            delta_score = 75
        elif delta < -100:
            delta_score = 25
        elif delta > 0:
            delta_score = 50 + min(25, delta / 10)
        else:
            delta_score = 50 + max(-25, delta / 10)
        
        trend_score = 50
        if trend == 'ALCISTA':
            trend_score = 70
        elif trend == 'BAJISTA':
            trend_score = 30
        
        weighted_score = (
            rsi_score * 0.2 +
            macd_score * 0.25 +
            vwap_score * 0.2 +
            delta_score * 0.25 +
            trend_score * 0.1
        )
        
        probability = weighted_score
        direction = "PUMP" if weighted_score > 55 else "DUMP" if weighted_score < 45 else "NEUTRAL"
        confidence = "HIGH" if abs(weighted_score - 50) > 20 else "MEDIUM" if abs(weighted_score - 50) > 10 else "LOW"
        
        return {
            'probability': probability,
            'direction': direction,
            'confidence': confidence,
            'target_price': price * (1.02 if direction == "PUMP" else 0.98) if direction != "NEUTRAL" else price,
            'rsi_score': rsi_score,
            'macd_score': macd_score,
            'vwap_score': vwap_score,
            'delta_score': delta_score,
            'trend_score': trend_score
        }
    
    def generate_agent_logs(self, prediction):
        import random
        direction = prediction['direction']
        rsi = self.data.get('rsi', 50)
        delta = self.data.get('delta', 0)
        
        logs = []
        
        if direction == "PUMP":
            logs.append(f"[Agent-1]: BUY SIGNAL - RSI oversold at {rsi:.0f}")
            logs.append(f"[Agent-2]: Delta accumulation +{delta:.0f} BTC")
            logs.append(f"[Agent-3]: Bullish divergence detected")
        elif direction == "DUMP":
            logs.append(f"[Agent-1]: SELL SIGNAL - RSI overbought at {rsi:.0f}")
            logs.append(f"[Agent-2]: Selling pressure increasing")
            logs.append(f"[Agent-3]: Bearish momentum confirmed")
        else:
            logs.append(f"[Agent-1]: No clear direction")
            logs.append(f"[Agent-2]: Waiting for confirmation")
            logs.append(f"[Agent-3]: Market in consolidation")
        
        if abs(prediction['probability'] - 50) > 25:
            logs.append(f"[Agent-MASTER]: HIGH CONFIDENCE TRADE")
        elif abs(prediction['probability'] - 50) > 15:
            logs.append(f"[Agent-MASTER]: Moderate signal detected")
        
        return logs
    
    def get_open_positions(self):
        try:
            positions = client.futures_position_information(symbol="BTCUSDT")
            open_pos = []
            for p in positions:
                if float(p.get('positionAmt', 0)) != 0:
                    entry_price = float(p.get('entryPrice', 0))
                    current_price = float(p.get('markPrice', 0))
                    amount = float(p.get('positionAmt', 0))
                    leverage = float(p.get('leverage', 1))
                    side = 'LONG' if amount > 0 else 'SHORT'
                    
                    if current_price > 0 and entry_price > 0:
                        if side == 'LONG':
                            pnl = (current_price - entry_price) * abs(amount)
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100
                        else:
                            pnl = (entry_price - current_price) * abs(amount)
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100
                    else:
                        pnl = 0
                        pnl_pct = 0
                    
                    open_pos.append({
                        'side': side,
                        'entry': entry_price,
                        'current': current_price,
                        'amount': abs(amount),
                        'leverage': leverage,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct
                    })
            return open_pos
        except:
            return []
    
    def calculate_all_indicators(self, klines):
        if len(klines) < 50:
            return
        
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        
        # VWAP - Volume Weighted Average Price
        typical_prices = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(klines))]
        cumsum_pv = sum(typical_prices[i] * volumes[i] for i in range(len(klines)))
        cumsum_v = sum(volumes)
        self.data['vwap'] = cumsum_pv / cumsum_v if cumsum_v > 0 else 0
        
        # Daily High/Low (últimos 100 klines = últimas ~2 horas)
        self.data['day_high'] = max(highs[-100:]) if len(highs) >= 100 else max(highs)
        self.data['day_low'] = min(lows[-100:]) if len(lows) >= 100 else min(lows)
        
        #距离 VWAP 的价格位置
        if self.data['vwap'] > 0:
            self.data['price_vwap_dist'] = ((self.data['price'] - self.data['vwap']) / self.data['vwap']) * 100
        
        self.data['ema_20'] = self.calculate_ema(closes, 20)
        self.data['ema_50'] = self.calculate_ema(closes, 50) if len(closes) >= 50 else self.data['ema_20']
        
        sma20 = sum(closes[-20:]) / 20
        std = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
        self.data['bb_upper'] = sma20 + (2 * std)
        self.data['bb_middle'] = sma20
        self.data['bb_lower'] = sma20 - (2 * std)
        
        if self.data['bb_upper'] != self.data['bb_lower']:
            self.data['bb_position'] = ((closes[-1] - self.data['bb_lower']) / 
                                  (self.data['bb_upper'] - self.data['bb_lower'])) * 100
        
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-14:]]
        losses = [-d if d < 0 else 0 for d in deltas[-14:]]
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        self.data['rsi'] = 100 - (100 / (1 + rs))
        
        ema12 = self.calculate_ema(closes, 12)
        ema26 = self.calculate_ema(closes, 26)
        self.data['macd'] = ema12 - ema26
        
        macd_values = [ema12 - self.calculate_ema(closes[:i], 26) for i in range(26, len(closes))]
        self.data['macd_signal'] = self.calculate_ema(macd_values, 9) if len(macd_values) >= 9 else self.data['macd']
        self.data['macd_hist'] = self.data['macd'] - self.data['macd_signal']
        
        trs = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        self.data['atr'] = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0
        
        self.data['avg_volume'] = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
        
        if self.data['ema_20'] > self.data['ema_50']:
            self.data['trend'] = 'ALCISTA'
        elif self.data['ema_20'] < self.data['ema_50']:
            self.data['trend'] = 'BAJISTA'
        else:
            self.data['trend'] = 'NEUTRAL'
    
    def determine_signal(self):
        signal = 'NINGUNA'
        
        long_conditions = 0
        if self.data['rsi'] < 30: long_conditions += 1
        if self.data['bb_position'] < 20: long_conditions += 1
        if self.data['macd'] > self.data['macd_signal'] and self.data['macd_hist'] > 0: long_conditions += 1
        if self.data['delta'] > 100: long_conditions += 1
        if self.data['trend'] == 'ALCISTA': long_conditions += 1
        
        short_conditions = 0
        if self.data['rsi'] > 70: short_conditions += 1
        if self.data['bb_position'] > 80: short_conditions += 1
        if self.data['macd'] < self.data['macd_signal'] and self.data['macd_hist'] < 0: short_conditions += 1
        if self.data['delta'] < -100: short_conditions += 1
        if self.data['trend'] == 'BAJISTA': short_conditions += 1
        
        if long_conditions >= 3: signal = 'COMPRA'
        elif short_conditions >= 3: signal = 'VENTA'
        
        return signal
    
    def update_data(self):
        # Wait for first klines from AsyncDataEngine
        klines = self.market_state.get("klines", [])
        if not klines:
            print("[⚠️ DATA] Esperando datos de AsyncDataEngine...")
            self.update_timer = QTimer()
            self.update_timer.timeout.connect(self.refresh_data)
            self.update_timer.start(1000)
            return
        for k in klines:
            kline = {'time': k[0], 'open': float(k[1]), 'high': float(k[2]),
                     'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])}
            trading_strategy.add_kline(kline)
        
        self.data['price'] = float(klines[-1][4])
        self.data['last_price'] = self.data['price']
        
        supabase_manager.connect()
        self.stats['db_connected'] = True
        
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.refresh_data)
        self.update_timer.start(1000)
    
    def refresh_data(self):
        if not self.running or self._refresh_busy:
            return
        self._refresh_busy = True
        try:

            import time
            start_time = time.time()

            # ── Read ALL pre-computed data from AsyncDataEngine (zero REST) ──
            ms = self.market_state
            ind = ms.get("indicators", {})
            of = ms.get("order_flow", {})
            lq = ms.get("liquidity", {})
            mom = ms.get("momentum", {})
            mtf = ms.get("mtf_trend", {})
            ww = ms.get("whale_walls", {})

            self.data['price'] = ms.get('price', self.data.get('price', 0))
            self.data['last_price'] = ms.get('last_price', self.data.get('last_price', 0))
            self.data['price_change'] = self.data['price'] - self.data['last_price']

            klines = ms.get('klines', [])
            self.data['klines'] = klines
            self.stats['klines_count'] = len(klines)

            # ── Copy indicators (pre-computed by AsyncDataEngine) ──
            for key in ('rsi', 'macd', 'macd_signal', 'macd_hist',
                         'bb_upper', 'bb_middle', 'bb_lower', 'bb_position',
                         'atr', 'ema_20', 'ema_50', 'vwap', 'price_vwap_dist',
                         'day_high', 'day_low', 'avg_volume', 'trend'):
                self.data[key] = ind.get(key, self.data.get(key, 0))

            # ── Copy order flow (pre-computed by AsyncDataEngine) ──
            self.data['delta'] = of.get('delta', self.data.get('delta', 0))
            self.data['cvd'] = of.get('cvd', self.data.get('cvd', 0))
            self.data['buy_volume'] = of.get('buy_volume', self.data.get('buy_volume', 0))
            self.data['sell_volume'] = of.get('sell_volume', self.data.get('sell_volume', 0))

            # ── Copy signal (pre-computed by AsyncDataEngine from indicators) ──
            self.data['signal'] = ms.get('signal', self.data.get('signal', 'NINGUNA'))

            # ── Copy order book + whale walls ──
            self.data['order_book'] = ms.get('order_book', self.data.get('order_book', {'bids': [], 'asks': []}))
            self.data['liquidity_data'] = ww if ww else self.data.get('liquidity_data', {})

            # ── Stats ──
            self.stats['latency_ms'] = int((time.time() - start_time) * 1000)
            self.stats['update_count'] += 1
            self.stats['last_update'] = time.strftime("%H:%M:%S")
            self.stats['api_connected'] = self.data['price'] > 0
            if self.stats['start_time']:
                self.stats['uptime_seconds'] = int(time.time() - self.stats['start_time'])

            # ── Feed trades to HeatMap chart (from shared cache) ──
            trades = ms.get('trades', [])
            trade_data_list = []
            for t in trades[:20]:
                trade_data_list.append({
                    'time': int(t['T']),
                    'price': float(t['p']),
                    'quantity': float(t['q']),
                    'is_buyer_maker': t['m'],
                })
            self.panels['HEATMAP'].update_trades(trade_data_list)

            # ── AI prediction (inline, lightweight, no REST) ──
            self.data['ai_prediction'] = self.calculate_ai_prediction()
            self.data['agent_logs'] = self.generate_agent_logs(self.data['ai_prediction'])

            # ── Sound notification on signal change ──
            if self.data['signal'] != 'NINGUNA' and self.stats['prev_signal'] == 'NINGUNA':
                play_notification_sound()
            self.stats['prev_signal'] = self.data['signal']

            self.data['price_change_pct'] = (self.data['price_change'] / max(self.data['last_price'], 0.0001) * 100) if self.data['last_price'] > 0 else 0

            # ── Update Galaxy Order Flow Chart ──
            self.panels['HEATMAP'].update_indicators(self.data)
            self.panels['HEATMAP'].update_klines(self.data.get('klines', []))
            self.panels['HEATMAP'].update_data(self.data['order_book'], self.data['price'])

            # ── Update all UI panels ──
            self.update_panels()

            # ════════════════════════════════════════════════════════════════
            # Telegram + Brain + Gemini dispatches (unchanged below)
            # ════════════════════════════════════════════════════════════════

            if hasattr(self, 'telegram_bot') and self.telegram_bot:
                mom = self.market_state.get('momentum', {})
                lq = self.market_state.get('liquidity', {})
                of = self.market_state.get('order_flow', {})
                mtf = self.market_state.get('mtf_trend', {})
                ai = self.market_state.get('ai_engine', {})
                bv = self.data.get('buy_volume', 0)
                sv = self.data.get('sell_volume', 0)
                total_vol = bv + sv
                ld = self.data.get('liquidity_data', {})
                cl = self.data.get('klines', [])
                closes = [float(k[4]) for k in cl[-20:]] if cl else []
                snapshot = {
                    # Precio y cambio
                    'symbol': settings.SYMBOL,
                    'price': self.data.get('price', 0),
                    'change_pct': self.data.get('price_change_pct', 0),
                    'day_high': self.data.get('day_high', 0),
                    'day_low': self.data.get('day_low', 0),
                    'vwap': self.data.get('vwap', 0),
                    'price_vwap_dist': self.data.get('price_vwap_dist', 0),
                    'price_above_vwap': self.data.get('price', 0) > self.data.get('vwap', 0),

                    # ── Buy imbalance count (last 5 candles, for divergence filter) ──
                    'buy_imbalance_count_5': (
                        sum(
                            1 for k in cl[-5:]
                            if float(k[9]) > (float(k[5]) - float(k[9])) * 1.5
                        ) if len(cl) >= 5 else 0
                    ),

                    # Tendencia y señal
                    'trend': self.data.get('trend', 'NEUTRAL'),
                    'signal_text': self.battle_bar.trend_direction if hasattr(self, 'battle_bar') else 'WAIT',
                    'confidence': self.battle_bar.confidence if hasattr(self, 'battle_bar') else 0,
                    'signal': self.data.get('signal', 'NINGUNA'),

                    # Indicadores técnicos
                    'rsi': self.data.get('rsi', 50),
                    'macd': self.data.get('macd', 0),
                    'macd_signal': self.data.get('macd_signal', 0),
                    'macd_hist': self.data.get('macd_hist', 0),
                    'bb_upper': self.data.get('bb_upper', 0),
                    'bb_middle': self.data.get('bb_middle', 0),
                    'bb_lower': self.data.get('bb_lower', 0),
                    'bb_position': self.data.get('bb_position', 50),
                    'bb_squeeze': mom.get('bb_squeeze', 'NORMAL'),
                    'atr': self.data.get('atr', 0),
                    'ema_20': self.data.get('ema_20', 0),
                    'ema_50': self.data.get('ema_50', 0),

                    # Order flow
                    'delta': self.data.get('delta', 0),
                    'delta_accel': self.battle_bar.delta_accel if hasattr(self, 'battle_bar') and hasattr(self.battle_bar, 'delta_accel') else 0,
                    'cvd': self.data.get('cvd', 0),
                    'prev_cvd': self._prev_cvd,
                    'buy_volume': bv,
                    'sell_volume': sv,
                    'volume': total_vol,
                    'avg_volume': self.data.get('avg_volume', 0),
                    'ba_ratio': bv / max(sv, 0.001),
                    'imbalance': ld.get('imbalance', 0),
                    'depth_imb_pct': lq.get('depth_imbalance', 0),
                    'cumulative_delta': lq.get('cvd_delta', of.get('oi_delta_1m', 0)),

                    # Order book / whale walls
                    'liq_zones': lq.get('liq_zones', 0),
                    'wall_bid': lq.get('wall_bid_1', 0),
                    'wall_bid_size': lq.get('wall_bid_size_1', 0),
                    'wall_ask': lq.get('wall_ask_1', 0),
                    'wall_ask_size': lq.get('wall_ask_size_1', 0),

                    # Microestructura
                    'kaufman_eff': mom.get('kaufman_efficiency', 0.5),
                    'spread_velocity': mom.get('spread_velocity', 0),
                    'tick_speed': mom.get('tick_speed', 0),
                    'tick_speed_avg_5m': mom.get('tick_speed_avg_5m', 0),
                    'volatility_explosion': mom.get('volatility_explosion', False),
                    'cancel_rate': mom.get('cancel_rate', 0),
                    'skewness': mom.get('skewness', 0),
                    'pinam': mom.get('pinam', 0),
                    'force': mom.get('force', 'NONE'),

                    # MTF
                    'trend_1m': mtf.get('t_1m', 'WAIT'),
                    'trend_5m': mtf.get('t_5m', 'WAIT'),
                    'trend_15m': mtf.get('t_15m', 'WAIT'),
                    'trend_1h': mtf.get('t_1h', 'WAIT'),
                    'trend_4h': mtf.get('t_4h', 'WAIT'),
                    'rsi_5m': mtf.get('rsi_5m', 0),
                    'rsi_15m': mtf.get('rsi_15m', 0),
                    'confluence_score': mtf.get('confluence_score', 0),
                    'global_macro': mtf.get('global_macro', 'NEUTRAL'),
                    'ema_cross_5m': mtf.get('ema_cross_5m', 'NEUTRAL'),
                    'ema_cross_15m': mtf.get('ema_cross_15m', 'NEUTRAL'),

                    # AI predictions
                    'ai_signal': ai.get('ai_signal', 'NINGUNA'),
                    'ai_final': ai.get('final_prediction', 'WAIT'),
                    'ai_score_of': ai.get('score_of', 0),
                    'ai_score_mom': ai.get('score_mom', 0),
                    'ai_score_trend': ai.get('score_trend', 0),
                    'ai_win_rate': ai.get('win_rate', 0),
                    'ai_risk_status': ai.get('risk_panel', {}).get('status', 'WAITING'),
                    'ai_trigger': ai.get('risk_panel', {}).get('trigger', 0),
                    'ai_sl': ai.get('risk_panel', {}).get('sl', 0),
                    'ai_tp1': ai.get('risk_panel', {}).get('tp1', 0),
                    'ai_tp2': ai.get('risk_panel', {}).get('tp2', 0),

                    # Stats
                    'uptime': self.stats.get('uptime_seconds', 0),
                    'update_count': self.stats.get('update_count', 0),
                    'latency_ms': self.stats.get('latency_ms', 0),
                    'timestamp': self.stats.get('last_update', ''),
                    'klines_ready': len(cl) >= 50,
                    'klines_count': len(cl),
                    'last_price': self.data.get('last_price', 0),
                    'change': self.data.get('price_change', 0),

                    # Trap detection (derived from available data)
                    'directional_probability': 50.0,
                    'market_bias': 'INCIERTO',
                    'trap_status': 'SIN TRAMPA',

                    # Anti-latency: wall-clock snapshot timestamp
                    '_snapshot_time': time.time(),
                }

                # ── Enrich trap & bias from battle_bar ────────────────────
                sig = snapshot.get('signal_text', 'WAIT')
                conf = snapshot.get('confidence', 0)
                if sig == 'LONG' and conf > 50:
                    snapshot['market_bias'] = 'ALZA'
                    snapshot['directional_probability'] = min(
                        95.0, 50.0 + conf * 0.4)
                elif sig == 'SHORT' and conf > 50:
                    snapshot['market_bias'] = 'BAJA'
                    snapshot['directional_probability'] = min(
                        95.0, 50.0 + conf * 0.4)

                rsi_val = snapshot.get('rsi', 50)
                bb_pos = snapshot.get('bb_position', 50)

                # ── HFT confluency filters ──────────────────────────────────
                mom = self.market_state.get('momentum', {})
                lq = self.market_state.get('liquidity', {})
                cancel_rate = mom.get('cancel_rate', 0.0)
                depth_imb = lq.get('depth_imbalance', 0.0)
                tick_speed = mom.get('tick_speed', 0)
                delta_vel = getattr(self.battle_bar, 'delta_accel', 0) if hasattr(self, 'battle_bar') else 0
                # delta_velocity in c/s — abs value for magnitude
                delta_vel_mag = abs(delta_vel) * 10  # scale to c/s

                # Determine if a real institutional wall exists (z-score filtered)
                ld = self.data.get('liquidity_data', {})
                has_wall = bool(ld.get('buy_walls') or ld.get('sell_walls'))

                # Trap OFF: cancel_rate < 35% → legitimate S/R regardless of wall
                if has_wall and cancel_rate < 35.0:
                    pass  # keep SIN TRAMPA

                # Trap OFF: cancel_rate between 35-55% or depth_imb < 45% → operational
                elif has_wall and (cancel_rate < 55.0 or abs(depth_imb) < 45.0):
                    pass  # keep SIN TRAMPA

                # Trap ON: cancel_rate > 55% AND depth_imb > 45%
                elif has_wall and cancel_rate > 55.0 and abs(depth_imb) > 45.0:
                    if rsi_val < 25 and bb_pos < 15 and snapshot.get('market_bias') == 'ALZA':
                        # Tick Speed Brake: require delta_vel > 500 c/s AND tick_speed < 15
                        if delta_vel_mag > 500 and tick_speed < 15:
                            snapshot['trap_status'] = '🔴 TRAMPA BAJISTA (FALSO SOPORTE)'
                    elif rsi_val > 75 and bb_pos > 85 and snapshot.get('market_bias') == 'BAJA':
                        if delta_vel_mag > 500 and tick_speed < 15:
                            snapshot['trap_status'] = '🔴 TRAMPA ALCISTA (FALSA RESISTENCIA)'

                # Store trap_status in data for UI banner
                self.data['trap_status'] = snapshot['trap_status']

                # Track CVD for exhaustion engine (prev_cvd used by check_market_exhaustion)
                current_cvd = snapshot.get('cvd', 0.0)
                self._prev_cvd = current_cvd

                # Push market snapshot to Telegram (lightning fast, no brain)
                self.telegram_bot.push_update(snapshot)

                # ── BrainAgent throttle & background dispatch ─────────────
                if getattr(self, 'brain_agent', None) is not None:
                    now = time.time()
                    candle_close = closes[-1] if closes else 0.0
                    time_elapsed = now - self._last_brain_time
                    candle_changed = (
                        candle_close != self._last_candle_close
                        and candle_close > 0.0
                    )
                    if time_elapsed >= self._brain_cooldown or candle_changed:
                        self._last_brain_time = now
                        self._last_candle_close = candle_close
                        if (self.brain_worker is None
                                or not self.brain_worker.isRunning()):
                            snap_copy = snapshot.copy()
                            self._pending_brain_snapshot = snap_copy
                            self.brain_worker = BrainInferenceWorker(
                                self.brain_agent,
                                snap_copy,
                                knowledge_blocks=(
                                    self._brain_knowledge_blocks
                                    if self._brain_knowledge_blocks
                                    else None),
                                temperature=self.brain_temperature,
                            )
                            self.brain_worker.inference_finished.connect(
                                self._on_inference_finished)
                            self.brain_worker.finished.connect(
                                self._clear_brain_worker)
                            self.brain_worker.start()

                # ── GeminiBrain dispatch: DESACTIVADO en automático ──────
                # Gemini solo se activa desde Auto-Learner (botón F3) o Telegram.
                # El bloque original está comentado para mantener referencia.
                # if getattr(self, 'gemini_brain', None) is not None and self.gemini_brain.is_enabled:
                #     ...

                # ── Background retrain check ────────────────────────────
                if getattr(self, 'brain_agent', None) is not None:
                    try:
                        now = time.time()
                        if now - self._last_retrain_time >= 14400:  # 4h
                            self._last_retrain_time = now
                            import asyncio
                            asyncio.run(self.brain_agent.background_retrain())
                    except Exception:
                        pass

                # ── Narrative journal trigger (5 min after alert) ─────
                if self._journal_pending and self._last_alert_snapshot is not None:
                    try:
                        now = time.time()
                        if now - self._last_alert_time >= 300:  # 5 min
                            self._journal_pending = False
                            journal_snap = dict(snapshot)
                            before = self._last_alert_snapshot
                            if getattr(self, 'gemini_brain', None) is not None:
                                import asyncio
                                asyncio.run(
                                    self.gemini_brain.journal_event(
                                        before, journal_snap,
                                        brain_direction=before.get('brain_direction'),
                                        brain_confidence=before.get('brain_confidence_pct', 0),
                                    )
                                )
                    except Exception:
                        pass
        finally:
            self._refresh_busy = False

    def _on_inference_finished(self, brain: dict):
        """Handle BrainInferenceWorker result — runs in main thread."""
        if not brain:
            return
        try:
            snapshot = getattr(self, '_pending_brain_snapshot', None)
            if snapshot is None:
                return
            for key in ('direction', 'confidence_pct',
                        'prob_alza', 'prob_baja', 'prob_incierto',
                        'market_rationale', 'inference_latency_ms'):
                snapshot[f'brain_{key}'] = brain.get(key, 0.0 if key in (
                    'confidence_pct', 'prob_alza', 'prob_baja',
                    'prob_incierto', 'inference_latency_ms') else '')
            # Store risk_bracket as nested dict (Telegram lo espera así)
            bracket = brain.get('risk_bracket', {})
            if bracket:
                snapshot['risk_bracket'] = bracket
                snapshot['brain_bracket_sl'] = bracket.get('sl', 0)
                snapshot['brain_bracket_tp1'] = bracket.get('tp1', 0)
                snapshot['brain_bracket_tp2'] = bracket.get('tp2', 0)
                snapshot['brain_bracket_trigger'] = bracket.get('trigger', 0)
                snapshot['brain_bracket_lot'] = bracket.get('lot_size', 0)
                snapshot['brain_bracket_status'] = bracket.get('status', '')
            self.telegram_bot.push_update(snapshot)

            # Store for UI panel updates
            self._last_brain_decision = brain
            self._last_brain_snapshot = snapshot

            # ── Save alert snapshot for narrative journaling ────────
            direction = brain.get('direction', '')
            conf = brain.get('confidence_pct', 0)
            if direction in ('ALZA', 'BAJA') and conf >= 60:
                self._last_alert_snapshot = dict(snapshot)
                self._last_alert_time = time.time()
                self._journal_pending = True

                # ── Auto-execute trade signal (solo quantum_brain) ──
                bracket = brain.get('risk_bracket', {})
                entry_price = snapshot.get('price', 0)
                sl_price = bracket.get('sl', 0)
                tp_price = bracket.get('tp1', 0)
                if (sl_price > 0 and tp_price > 0 and entry_price > 0
                        and hasattr(self, 'order_executor')
                        and self.order_executor is not None):
                    self.order_executor.execute_trade_signal(
                        direction=direction,
                        entry_price=entry_price,
                        sl_price=sl_price,
                        tp_price=tp_price,
                    )

        except Exception as e:
            print(f"[⚠️ BRAIN] Error en _on_inference_finished: {e}")

    def _on_order_result(self, success: bool, message: str, data: dict):
        env = data.get("environment", "?")
        direction = data.get("direction", "?")
        entry = data.get("entry_price", 0)
        qty = data.get("entry_qty", 0)
        sl = data.get("sl_price", 0)
        tp = data.get("tp_price", 0)
        entry_id = data.get("entry_order_id", "")
        sl_id = data.get("sl_order_id", "")
        tp_id = data.get("tp_order_id", "")

        status_icon = "✅" if success else "❌"
        log_msg = (
            f"[ORDEN {env}] {status_icon} {direction} "
            f"{qty} BTC @ ${entry:,.0f} "
            f"SL=${sl:,.0f} TP=${tp:,.0f} "
            f"entry={entry_id} sl={sl_id} tp={tp_id}"
        )
        print(log_msg)

        # Enviar notificación a Telegram
        bracket_errs = data.get("bracket_errors", "")
        bracket_info = f" | ⚠ {bracket_errs}" if bracket_errs else ""
        telegram_text = (
            f"<b>{status_icon} ORDEN EJECUTADA [{env}]</b>\n"
            f"<b>Dirección:</b> {direction}\n"
            f"<b>Cantidad:</b> <code>{qty} BTC</code>\n"
            f"<b>Entrada:</b> <code>${entry:,.0f}</code>\n"
            f"<b>SL:</b> <code>${sl:,.0f}</code>\n"
            f"<b>TP:</b> <code>${tp:,.0f}</code>\n"
            f"<b>IDs:</b> entry=<code>{entry_id}</code> "
            f"sl=<code>{sl_id}</code> tp=<code>{tp_id}</code>"
            f"{bracket_info}"
        )

        alert = {
            "type": "order_execution",
            "text": telegram_text,
        }
        if hasattr(self, "telegram_bot") and self.telegram_bot is not None:
            try:
                self.telegram_bot._queue.put_nowait(alert)
            except Exception:
                pass

    def _clear_brain_worker(self):
        """Safely delete BrainInferenceWorker and nullify the reference."""
        worker = self.sender()
        if worker is None:
            return
        if worker is self.brain_worker:
            self.brain_worker = None
        try:
            worker.deleteLater()
        except (RuntimeError, AttributeError):
            pass

    def _clear_gemini_worker(self):
        """Safely delete GeminiInferenceWorker and nullify the reference."""
        worker = self.sender()
        if worker is None:
            return
        if worker is self.gemini_worker:
            self.gemini_worker = None
        try:
            worker.deleteLater()
        except (RuntimeError, AttributeError):
            pass

    def _update_ai_monitor(self,
                            gemini_decision=None,
                            brain_decision=None):
        """Refresh the dual-engine monitor widget with both AI outputs.

        Called from both ``update_panels()`` (~1 Hz) and
        ``_on_gemini_finished()`` (~0.33 Hz).  Uses ``setHtml`` with
        monospaced formatting so the report is clean and readable.
        """
        if not hasattr(self, 'ai_engines_monitor'):
            return

        # ── Quantum Brain (PyTorch LSTM) ──────────────────────────────
        brain = brain_decision or {}
        brain_dir = brain.get('direction', 'INCIERTO') if isinstance(brain, dict) else 'INCIERTO'
        brain_conf = brain.get('confidence_pct', 0.0) if isinstance(brain, dict) else 0.0
        brain_rationale = brain.get('market_rationale', '') if isinstance(brain, dict) else ''
        brain_latency = brain.get('inference_latency_ms', 0.0) if isinstance(brain, dict) else 0.0

        if brain_dir == 'ALZA':
            brain_color = '#00FF66'
        elif brain_dir == 'BAJA':
            brain_color = '#BB00FF'
        else:
            brain_color = '#FFD700'

        # Top-3 matched knowledge blocks (from brain_agent)
        top_blocks = ''
        if isinstance(brain, dict) and brain.get('flip_blocked'):
            top_blocks = '⚠️ Flip bloqueado por histéresis'
        elif isinstance(brain, dict) and brain.get('inference_latency_ms'):
            top_blocks = 'Inferencia completada'

        # ── Gemini 2.0 Flash ──────────────────────────────────────────
        gem = gemini_decision
        if gem is not None:
            g_dir = gem.decision
            g_conf = gem.confidence
            g_reason = gem.reasoning
            g_of = gem.score_order_flow
            g_mom = gem.score_momentum
            g_trend = gem.score_trend
            g_exh = gem.exhaustion_detected
            g_entry = gem.bracket.entry
            g_sl = gem.bracket.stop_loss
            g_tp1 = gem.bracket.take_profit_1
            g_tp2 = gem.bracket.take_profit_2
            if g_dir == 'ALZA':
                g_color = '#00FF66'
            elif g_dir == 'BAJA':
                g_color = '#BB00FF'
            else:
                g_color = '#FFD700'

        # ── Build HTML report ─────────────────────────────────────────
        lines = []
        lines.append('<pre style="color:#888; font-family:monospace; font-size:9px;">')

        # Header
        lines.append('')

        # ── Block 1: Quantum Brain ────────────────────────────────────
        lines.append(
            f'<b style="color:#00D4FF;">🧠 [MOTOR 1: QUANTUM BRAIN (LSTM)]</b>')
        lines.append(
            f'<span style="color:#555;">{"─" * 50}</span>')
        lines.append(
            f'  <b>Dirección:</b>   '
            f'<span style="color:{brain_color};">{brain_dir}</span>  '
            f'<b>Confianza:</b>   '
            f'<span style="color:{brain_color};">{brain_conf:.1f}%</span>')
        if brain_latency:
            lines.append(
                f'  <b>Latencia:</b>   {brain_latency:.0f}ms')
        if brain_rationale:
            lines.append(
                f'  <b>Rationale de Agresión:</b>')
            lines.append(
                f'    <span style="color:#CCC;">{brain_rationale}</span>')
        lines.append(
            f'  <b>Sesgo de Conocimiento:</b>  '
            f'<span style="color:#888;">{top_blocks or "No aplicado"}</span>')
        lines.append('')

        # ── Block 2: Gemini ───────────────────────────────────────────
        if gem is not None:
            lines.append(
                f'<b style="color:#FFD700;">🌌 [MOTOR 2: GEMINI 2.0 FLASH]</b>')
            lines.append(
                f'<span style="color:#555;">{"─" * 50}</span>')
            lines.append(
                f'  <b>Dirección:</b>   '
                f'<span style="color:{g_color};">{g_dir}</span>  '
                f'<b>Confianza:</b>   '
                f'<span style="color:{g_color};">{g_conf:.0f}%</span>')
            lines.append(
                f'  <b>Exhaustion:</b> {g_exh}')
            lines.append(
                f'  <b>Scores:</b>  '
                f'OF={g_of:.1f}  Mom={g_mom:.1f}  Trend={g_trend:.1f}')
            lines.append(
                f'  <b>Bracket:</b>  entry={g_entry:.1f}  '
                f'SL={g_sl:.1f}  TP1={g_tp1:.1f}  TP2={g_tp2:.1f}')
            if g_reason:
                lines.append(
                    f'  <b>Análisis Institucional:</b>')
                lines.append(
                    f'    <span style="color:#CCC;">{g_reason}</span>')
            lines.append('')

        lines.append('</pre>')
        html = '\n'.join(lines)
        self.ai_engines_monitor.setHtml(html)

    def _on_gemini_finished(self, decision):
        """Handle GeminiInferenceWorker result — runs in main thread.

        Maps ``GeminiTradingDecision`` fields to the AI ENGINE panel
        and bracket widget.  Silently ignores ``None`` (network fallback).
        """
        if decision is None:
            return
        try:
            self._last_gemini_decision = decision

            # ── Inject into market_state for the panel render loop ────
            ai = self.market_state.setdefault("ai_engine", {})
            ai["gemini_decision"] = decision.decision
            ai["gemini_confidence"] = decision.confidence
            ai["gemini_exhaustion"] = decision.exhaustion_detected
            ai["gemini_score_of"] = decision.score_order_flow
            ai["gemini_score_mom"] = decision.score_momentum
            ai["gemini_score_trend"] = decision.score_trend
            ai["gemini_reasoning"] = decision.reasoning

            # ── Push Gemini bracket to the bracket widget ─────────────
            bracket = decision.bracket
            gemini_risk = {
                "status": decision.decision,
                "trigger": bracket.entry,
                "sl": bracket.stop_loss,
                "tp1": bracket.take_profit_1,
                "tp2": bracket.take_profit_2,
                "lot_size": 0.0,
            }
            if hasattr(self, 'bracket_widget'):
                price = self.data.get('price', 0)
                self.bracket_widget.update_data(
                    ai.get("risk_panel", {"status": "WAITING", "trigger": 0,
                                          "sl": 0, "tp1": 0, "tp2": 0,
                                          "lot_size": 0}),
                    decision.confidence,
                    price,
                    brain_bracket=gemini_risk,
                )

            # ── Push to dual-engine monitor (not brain_content_viewer) ──
            self._update_ai_monitor(
                gemini_decision=decision,
                brain_decision=getattr(self, '_last_brain_decision', None),
            )

            # ── Push update to Telegram for downstream dispatch ──────
            snap = getattr(self, '_pending_brain_snapshot', None)
            if snap is not None:
                snap['brain_direction'] = decision.decision
                snap['brain_confidence_pct'] = decision.confidence
                snap['brain_market_rationale'] = decision.reasoning
                self.telegram_bot.push_update(snap)

        except Exception as e:
            print(f"[⚠️ GEMINI] Error en _on_gemini_finished: {e}")

    def create_indicator_row(self, name):
        row = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(2)
        
        label_layout = QHBoxLayout()
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 14px; font-weight: bold;")
        val_lbl = QLabel("0.00")
        val_lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: bold;")
        val_lbl.setAlignment(Qt.AlignRight)
        
        label_layout.addWidget(name_lbl)
        label_layout.addWidget(val_lbl)
        
        bar_bg = QFrame()
        bar_bg.setFixedHeight(8)
        bar_bg.setStyleSheet("background: #111; border-radius: 4px;")
        bar_fill = QFrame(bar_bg)
        bar_fill.setFixedHeight(8)
        bar_fill.setStyleSheet(f"background: {COLORS['accent_turquoise']}; border-radius: 4px;")
        
        layout.addLayout(label_layout)
        layout.addWidget(bar_bg)
        row.setLayout(layout)
        
        return row, val_lbl, bar_fill, bar_bg

    def update_panels(self):
        up = COLORS['accent_turquoise']
        dn = COLORS['accent_purple']
        gold = COLORS['accent_gold']
        white = COLORS['text_primary']
        cyan = COLORS['accent_cyan']
        magenta = COLORS['accent_magenta']
        
        # Header
        price_color = up if self.data['price_change'] >= 0 else dn
        self.header_label.setText(f"◈ BTCUSDT ${self.format_number(self.data['price'])}")
        self.header_label.setStyleSheet(f"color: {price_color}; font-size: 20px; font-weight: 900; background: transparent; letter-spacing: 1px;")
        
        latency = self.stats.get('latency_ms', 0)
        lat_color = up if latency < 200 else gold if latency < 500 else dn
        self.latency_label.setText(f"⚡ {latency}ms")
        self.latency_label.setStyleSheet(f"color: {lat_color}; font-size: 11px; font-weight: bold; font-family: monospace; background: transparent;")
        
        tick_speed = self.stats.get('update_count', 0) % 60
        self.tick_speed_label.setText(f"⟳ {tick_speed}/s")
        self.tick_speed_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 11px; font-weight: bold; font-family: monospace; background: transparent;")
        
        api_connected = self.stats.get('api_connected', False)
        status_color = COLORS['accent_emerald'] if api_connected else COLORS['accent_crimson']
        status_text = "● LIVE" if api_connected else "○ OFFLINE"
        self.status_indicator.setText(status_text)
        self.status_indicator.setStyleSheet(f"color: {status_color}; font-size: 11px; font-weight: bold; background: transparent;")
        
        # Helper to set grid label value + color
        def gv(name, val, color=white):
            if name in self.grid_labels:
                self.grid_labels[name].setText(str(val))
                self.grid_labels[name].setStyleSheet(f"color: {color}; font-size: 10px; font-weight: bold; background: transparent;")
        
        price = self.data['price']
        chg = self.data['price_change_pct']
        rsi = self.data['rsi']
        imb = self.data['liquidity_data'].get('imbalance', 0)
        buy_v = self.data['buy_volume']
        sell_v = self.data['sell_volume']
        delta = self.data['delta']
        cvd = self.data['cvd']
        
        if 'NARRATIVE' in self.panels:
            mom = self.market_state.get('momentum', {})
            lq = self.market_state.get('liquidity', {})
            narrative_state = {
                **self.data,
                'kaufman_eff': mom.get('kaufman_efficiency', 0.5),
                'spread_velocity': mom.get('spread_velocity', 0),
                'tick_speed': mom.get('tick_speed', 0),
                'cancel_rate': mom.get('cancel_rate', 0.0),
                'depth_imb_pct': lq.get('depth_imbalance', 0.0),
                'delta_accel': self.battle_bar.delta_accel if hasattr(self, 'battle_bar') and hasattr(self.battle_bar, 'delta_accel') else 0,
            }
            self.panels['NARRATIVE'].update_narrative(narrative_state, self.data.get('order_book', {}))
        
        if hasattr(self, 'trend_signal_bar'):
            # Cross-verify trap status from all available sources
            active_trap = ""
            narrative = self.panels.get('NARRATIVE')
            if narrative and hasattr(narrative, 'get_current_alert'):
                alert_text = narrative.get_current_alert()
                if alert_text and "TRAMPA" in alert_text.upper():
                    active_trap = alert_text

            if active_trap or ('trap_status' in self.data and self.data['trap_status']
                               and 'SIN TRAMPA' not in self.data['trap_status']):
                final_trap_text = active_trap if active_trap else self.data['trap_status']
                self.trend_signal_bar.set_trap_mode(final_trap_text)
                self.trend_signal_bar.update()
            else:
                self.trend_signal_bar.update_signal(
                    self.battle_bar.trend_direction,
                    self.battle_bar.trend_label)
        
        # MTF data from async engine
        mt = self.market_state.get("mtf_trend", {})
        c_score = mt.get("confluence_score", 50)
        t_1h = mt.get("t_1h", "NEUTRAL")
        t_4h = mt.get("t_4h", "NEUTRAL")
        
        # Microstructure / HFT data from async engine
        mom = self.market_state.get('momentum', {})
        m_tick = mom.get('tick_speed', 0)
        m_cancel = mom.get('cancel_rate', 0)
        m_pinam = mom.get('pinam', 0)
        m_spread = mom.get('spread_velocity', 0)
        
        # Volatility data
        b_squeeze = self.data.get('bb_squeeze', 'NORMAL')
        atr_val = self.data.get('atr', 0)
        avg_vol = self.data.get('avg_volume', 0)
        
        # update battle bar — single source of truth, real data only
        self.battle_bar.update_battle(
            buy_volume=self.data['buy_volume'],
            sell_volume=self.data['sell_volume'],
            imbalance=self.data['liquidity_data'].get('imbalance', 0),
            trend=self.market_state.get('trend', 'NEUTRAL'),
            rsi=self.data['rsi'],
            cvd=self.data['cvd'],
            confluence_score=c_score,
            trend_1h=t_1h,
            trend_4h=t_4h,
            delta=self.data['delta'],
            tick_speed=m_tick,
            cancel_rate=m_cancel,
            pinam=m_pinam,
            bb_squeeze=b_squeeze,
            atr=atr_val,
            spread_velocity=m_spread,
            avg_volume=avg_vol,
            volatility_explosion=mom.get('volatility_explosion', False),
        )
            
        # ═══════════════════════════════════════════════════════════════
        # COL 1: ORDER FLOW & OI
        # ═══════════════════════════════════════════════════════════════
        gv("PRICE", f"${price:,.2f}", gold)
        gv("CHANGE", f"{chg:+.3f}%", up if chg >= 0 else dn)
        gv("BUY VOL", f"{buy_v:.2f} BTC", up)
        gv("SELL VOL", f"{sell_v:.2f} BTC", dn)
        
        ratio = buy_v / max(0.001, sell_v)
        ratio_color = up if ratio > 1.2 else dn if ratio < 0.8 else gold
        gv("BUY/SELL RATIO", f"{ratio:.2f}", ratio_color)
        
        # OB IMBALANCE from analyze_whale_walls() dynamic data
        ld = self.data.get('liquidity_data', {})
        imb_dynamic = ld.get('imbalance', imb)
        gv("OB IMBALANCE", f"{imb_dynamic:+.3f}", up if imb_dynamic > 0 else dn)
        
        bids = self.data['order_book'].get('bids', [])
        asks = self.data['order_book'].get('asks', [])
        big_bids = sum(1 for p, q in bids if float(q) > 5)
        big_asks = sum(1 for p, q in asks if float(q) > 5)
        gv("INS BLOCKS", f"B:{big_bids} A:{big_asks}", up if big_bids > big_asks else dn)
        
        # Placeholders for OI (To be connected to Binance Futures endpoints later)
        gv("OPEN INTEREST", "FETCHING...", gold)
        gv("FUNDING RATE", "0.0100%", white)
        gv("OI TREND", "NEUTRAL", gold)
        
        # ═══════════════════════════════════════════════════════════════
        # COL 2: DELTA & LIQUIDITY
        # ═══════════════════════════════════════════════════════════════
        gv("CVD DELTA", f"{cvd:+.2f}", up if cvd >= 0 else dn)
        
        d_vel = abs(delta) * 10
        gv("DELTA VELOCITY", f"{d_vel:.1f} c/s", up if delta > 0 else dn)
        
        delta_div = "NONE"
        dd_color = gold
        if chg > 0 and delta < -0.5:
            delta_div = "⚠ BEARISH DIV"
            dd_color = dn
        elif chg < 0 and delta > 0.5:
            delta_div = "⚠ BULLISH DIV"
            dd_color = up
        gv("DELTA DIV", delta_div, dd_color)
        
        # Find Whale Walls — prefer z-score filtered from analyze_whale_walls()
        ld = self.data.get('liquidity_data', {})
        whale_buy = ld.get('buy_walls', [])
        whale_sell = ld.get('sell_walls', [])
        if whale_buy:
            bid_walls = [(w['price'], w['quantity']) for w in whale_buy]
        else:
            bid_walls = sorted([(float(p), float(q)) for p,q in bids if float(q) >= 2.0], key=lambda x: x[1], reverse=True)
        if whale_sell:
            ask_walls = [(w['price'], w['quantity']) for w in whale_sell]
        else:
            ask_walls = sorted([(float(p), float(q)) for p,q in asks if float(q) >= 2.0], key=lambda x: x[1], reverse=True)
        
        if len(bid_walls) > 0: gv("WALL BID #1", f"${bid_walls[0][0]:,.0f} ({bid_walls[0][1]:.1f} B)", up)
        else: gv("WALL BID #1", "NONE", gold)
        if len(bid_walls) > 1: gv("WALL BID #2", f"${bid_walls[1][0]:,.0f} ({bid_walls[1][1]:.1f} B)", up)
        else: gv("WALL BID #2", "NONE", gold)
        
        if len(ask_walls) > 0: gv("WALL ASK #1", f"${ask_walls[0][0]:,.0f} ({ask_walls[0][1]:.1f} B)", dn)
        else: gv("WALL ASK #1", "NONE", gold)
        if len(ask_walls) > 1: gv("WALL ASK #2", f"${ask_walls[1][0]:,.0f} ({ask_walls[1][1]:.1f} B)", dn)
        else: gv("WALL ASK #2", "NONE", gold)
        
        bounces = self.panels['HEATMAP'].bounce_zones
        gv("LIQ ZONES", f"{len(bounces)} ACTIVE", magenta if len(bounces) > 3 else gold)
        long_b = [b for b in bounces if b['side'] == 'LONG']
        short_b = [b for b in bounces if b['side'] == 'SHORT']
        support = long_b[0]['price'] if long_b else self.data['bb_lower']
        resistance = short_b[0]['price'] if short_b else self.data['bb_upper']
        gv("SUPPORT", f"${support:,.0f}", up)
        gv("RESISTANCE", f"${resistance:,.0f}", dn)
        
        # ═══════════════════════════════════════════════════════════════
        # COL 3: MTF TREND (MULTI-TIMEFRAME)
        # ═══════════════════════════════════════════════════════════════
        t1m = self.data.get('trend', 'NEUTRAL')
        gv("TREND 1M", t1m, up if t1m == 'ALCISTA' else dn if t1m == 'BAJISTA' else gold)
        # Mock MTF Data until backend is hooked up
        gv("TREND 5M", "ALCISTA" if price > self.data['ema_20'] else "BAJISTA", up if price > self.data['ema_20'] else dn)
        gv("TREND 15M", "ALCISTA" if price > self.data['ema_50'] else "BAJISTA", up if price > self.data['ema_50'] else dn)
        gv("TREND 1H", "WAIT", gold)
        gv("TREND 4H", "WAIT", gold)
        gv("RSI 5M", "CALCULATING...", white)
        gv("RSI 15M", "CALCULATING...", white)
        gv("MACD 15M", "CALCULATING...", white)
        gv("MACD 1H", "CALCULATING...", white)
        gv("GLOBAL MACRO", "NEUTRAL", gold)

        # ═══════════════════════════════════════════════════════════════
        # COL 4: MOMENTUM & VOLATILITY
        # ═══════════════════════════════════════════════════════════════
        gv("RSI (1M)", f"{rsi:.1f}", dn if rsi > 70 else up if rsi < 30 else gold)
        gv("MACD (1M)", f"{self.data['macd']:.3f}", up if self.data['macd'] > self.data['macd_signal'] else dn)
        gv("MACD HIST", f"{self.data['macd_hist']:.4f}", up if self.data['macd_hist'] >= 0 else dn)
        gv("FORCE", self.battle_bar.trend_label, up if self.battle_bar.trend_direction == 'LONG' else dn if self.battle_bar.trend_direction == 'SHORT' else gold)
        gv("ATR (VOLATILITY)", f"${self.data['atr']:.2f}", gold)
        gv("BB UPPER", f"${self.data['bb_upper']:,.0f}", dn)
        gv("BB LOWER", f"${self.data['bb_lower']:,.0f}", up)
        
        bb_width = self.data['bb_upper'] - self.data['bb_lower']
        sqz = "SQUEEZE" if bb_width < self.data['atr'] * 2 else "EXPANSION"
        gv("BB SQUEEZE", sqz, magenta if sqz == "SQUEEZE" else cyan)
        gv("TICK SPEED", f"{self.stats.get('update_count', 0) % 50} t/s", white)
        
        # ═══════════════════════════════════════════════════════════════
        # COL 5: AI ENGINE & LOG  (Gemini 2.0 Flash + Quantum Brain hybrid)
        # ═══════════════════════════════════════════════════════════════

        # ── Prefer Gemini result, fall back to local PyTorch brain ──────
        gem = getattr(self, '_last_gemini_decision', None)
        brain = getattr(self, '_last_brain_decision', None)

        if gem is not None:
            # Gemini-sourced values
            ai_signal = gem.decision
            ai_confidence = gem.confidence
            ai_exhaustion = gem.exhaustion_detected
            ai_rationale = gem.reasoning
            ai_score_of = gem.score_order_flow
            ai_score_mom = gem.score_momentum
            ai_score_trend = gem.score_trend
            ai_bracket_raw = gem.bracket
            ai_latency = 0.0
        else:
            # Fallback to local PyTorch brain
            brain_dir = brain.get('direction', 'INCIERTO') if brain else 'INCIERTO'
            ai_signal = brain_dir if (brain and brain.get('direction') != 'INCIERTO' and brain.get('confidence_pct', 0) >= 50) else self.data.get('signal', 'NINGUNA')
            ai_confidence = brain.get('confidence_pct', 0.0) if brain else 0.0
            ai_rationale = brain.get('market_rationale', '') if brain else ''
            ai_latency = brain.get('inference_latency_ms', 0.0) if brain else 0.0

        # ── Push to dual-engine monitor (runs every refresh ~1 Hz) ─────
        self._update_ai_monitor(
            gemini_decision=getattr(self, '_last_gemini_decision', None),
            brain_decision=getattr(self, '_last_brain_decision', None),
        )

        # ── AI SIGNAL ───────────────────────────────────────────────────
        if gem is not None and gem.decision != 'INCIERTO':
            as_color = up if gem.decision == 'ALZA' else dn
            gv("AI SIGNAL", gem.decision, as_color)
        elif brain and brain.get('direction') != 'INCIERTO' and brain.get('confidence_pct', 0) >= 50:
            bdir = brain['direction']
            gv("AI SIGNAL", bdir, up if bdir == 'ALZA' else dn)
        else:
            gv("AI SIGNAL", self.data['signal'],
               up if self.data['signal'] == 'COMPRA' else
               dn if self.data['signal'] == 'VENTA' else gold)

        # ── WIN RATE / CONFIDENCE ──────────────────────────────────────
        if gem is not None:
            gv("WIN RATE", f"{gem.confidence:.0f}%", up if gem.confidence > 50 else dn)
        elif brain and brain.get('confidence_pct', 0) > 0:
            bc = brain['confidence_pct']
            gv("WIN RATE", f"{bc:.0f}%", up if bc > 50 else dn)
        else:
            gv("WIN RATE", f"{self.data.get('win_rate', 0):.0f}%",
               up if self.data.get('win_rate', 0) > 50 else dn)

        gv("LATENCY",
           f"{ai_latency:.0f}ms" if ai_latency else
           f"{self.stats.get('latency_ms', 0)}ms",
           up if (ai_latency or self.stats.get('latency_ms', 0)) < 500 else dn)

        # ── EXHAUSTION ──────────────────────────────────────────────────
        exhaustion = "NONE"
        ex_color = gold
        if gem is not None:
            exhaustion = gem.exhaustion_detected
            if "BULLISH" in exhaustion:
                ex_color = up
            elif "BEARISH" in exhaustion:
                ex_color = dn
        elif brain and brain.get('direction') == 'ALZA' and brain.get('confidence_pct', 0) >= 60:
            exhaustion = "▲ BRAIN BULLISH"
            ex_color = up
        elif brain and brain.get('direction') == 'BAJA' and brain.get('confidence_pct', 0) >= 60:
            exhaustion = "▼ BRAIN BEARISH"
            ex_color = dn
        elif rsi > 75 and self.data['bb_position'] > 85:
            exhaustion = "▼ SELL EXHAUST"
            ex_color = dn
        elif rsi < 25 and self.data['bb_position'] < 15:
            exhaustion = "▲ BUY EXHAUST"
            ex_color = up
        gv("EXHAUSTION", exhaustion, ex_color)

        # ── SCORES ──────────────────────────────────────────────────────
        td = self.battle_bar.trend_direction
        conf = self.battle_bar.confidence

        if gem is not None:
            gv("SCORE: ORDER FLOW", f"{gem.score_order_flow:.1f}/10", cyan)
            gv("SCORE: MOMENTUM", f"{gem.score_momentum:.1f}/10", gold)
            gv("SCORE: TREND", f"{gem.score_trend:.1f}/10", magenta)
        elif brain and brain.get('confidence_pct', 0) > 0:
            of_score = abs(brain.get('prob_alza', 0) - brain.get('prob_baja', 0)) * 10
            gv("SCORE: ORDER FLOW", f"{min(10.0, of_score):.1f}/10", cyan)
            mom_score = brain['confidence_pct'] / 10
            gv("SCORE: MOMENTUM", f"{min(10.0, mom_score):.1f}/10", gold)
            trend_score = brain.get('prob_alza', 0) if brain.get('direction') == 'ALZA' else brain.get('prob_baja', 0)
            gv("SCORE: TREND", f"{min(10.0, trend_score/10):.1f}/10", magenta)
        else:
            gv("SCORE: ORDER FLOW", f"{min(9.5, abs(delta)*2):.1f}/10", cyan)
            gv("SCORE: MOMENTUM", f"{min(9.0, abs(rsi-50)/3):.1f}/10", gold)
            gv("SCORE: TREND", f"{min(9.9, conf/10):.1f}/10", magenta)

        # ── FINAL PREDICTION ────────────────────────────────────────────
        if gem is not None and gem.decision != 'INCIERTO' and gem.confidence >= 50:
            if gem.decision == 'ALZA':
                final_pred = "LONG"
                gv("FINAL PREDICTION", f"◉ BUY {gem.confidence:.0f}%", cyan)
            else:
                final_pred = "SHORT"
                gv("FINAL PREDICTION", f"◉ SELL {gem.confidence:.0f}%", magenta)
        elif brain and brain.get('direction') == 'ALZA' and brain.get('confidence_pct', 0) >= 50:
            final_pred = "LONG"
            gv("FINAL PREDICTION", f"◉ BUY {brain['confidence_pct']:.0f}%", cyan)
        elif brain and brain.get('direction') == 'BAJA' and brain.get('confidence_pct', 0) >= 50:
            final_pred = "SHORT"
            gv("FINAL PREDICTION", f"◉ SELL {brain['confidence_pct']:.0f}%", magenta)
        elif td == 'LONG' and conf > 30:
            final_pred = "LONG"
            gv("FINAL PREDICTION", f"◉ BUY {conf:.0f}%", cyan)
        elif td == 'SHORT' and conf > 30:
            final_pred = "SHORT"
            gv("FINAL PREDICTION", f"◉ SELL {conf:.0f}%", magenta)
        else:
            final_pred = "WAIT"
            gv("FINAL PREDICTION", "◆ WAIT", gold)

        prev_pred = self.market_state["ai_engine"]["final_prediction"]
        self.market_state["ai_engine"]["final_prediction"] = final_pred

        # ── Risk bracket ───────────────────────────────────────────────
        # Gemini bracket already pushed to widget via _on_gemini_finished.
        # Use Gemini bracket for risk panel if available.
        gem_bracket = None
        if gem is not None:
            b = gem.bracket
            gem_bracket = {
                "status": gem.decision,
                "trigger": b.entry,
                "sl": b.stop_loss,
                "tp1": b.take_profit_1,
                "tp2": b.take_profit_2,
                "lot_size": 0.0,
            }

        brain_bracket = brain.get('risk_bracket', {}) if brain else {}

        if gem_bracket is not None and gem_bracket['sl'] != 0:
            risk_override = True
            final_risk = gem_bracket
        elif brain_bracket and brain_bracket.get('sl', 0) != 0:
            risk_override = True
            final_risk = brain_bracket
        else:
            risk_override = False
            if prev_pred == "WAIT" and final_pred in ["LONG", "SHORT"]:
                atr = self.data.get('atr', 10)
                wall_ask = ask_walls[0][0] if ask_walls else price + 100
                wall_bid = bid_walls[0][0] if bid_walls else price - 100

                trigger = price
                if final_pred == "LONG":
                    sl = price - (1.5 * atr)
                    tp1 = price + (2 * atr)
                    tp2 = wall_ask
                else:
                    sl = price + (1.5 * atr)
                    tp1 = price - (2 * atr)
                    tp2 = wall_bid

                loss_per_contract = abs(trigger - sl)
                lot_size = 10.0 / loss_per_contract if loss_per_contract > 0 else 0

                final_risk = {
                    "status": final_pred,
                    "trigger": trigger,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "lot_size": lot_size
                }
            elif final_pred == "WAIT" and prev_pred != "WAIT":
                final_risk = self.market_state["ai_engine"]["risk_panel"]
            else:
                final_risk = self.market_state["ai_engine"]["risk_panel"]

        self.market_state["ai_engine"]["risk_panel"] = final_risk
            
        # Col 1 Data Update: Read OI deltas from async engine (already populated)
        of = self.market_state["order_flow"]
        oi_1s = of.get("oi_delta_1s", 0.0)
        oi_5s = of.get("oi_delta_5s", 0.0)
        oi_1m = of.get("oi_delta_1m", 0.0)
        oi_5m = oi_1m * 1.8  # extrapolated until 5m endpoint available
        acc_1s = abs(oi_1s) / 0.05 if abs(oi_1s) > 0.01 else 0.5
        acc_5s = abs(oi_5s) / 0.08 if abs(oi_5s) > 0.01 else 0.5
        acc_1m = abs(oi_1m) / 0.15 if abs(oi_1m) > 0.01 else 0.5
        acc_5m = abs(oi_5m) / 0.3 if abs(oi_5m) > 0.01 else 0.5
        
        # Col 2 Data Update: Liquidity pools (calculated) + real wall data from engine
        lq = self.market_state["liquidity"]
        lq["liq_pool_10x"] = price * 1.1 if final_pred == "LONG" else price * 0.9
        lq["liq_pool_25x"] = price * 1.04 if final_pred == "LONG" else price * 0.96
        lq["liq_pool_50x"] = price * 1.02 if final_pred == "LONG" else price * 0.98
        lq["liq_pool_100x"] = price * 1.01 if final_pred == "LONG" else price * 0.99
        
        # Bid/Ask pressure from real depth data
        d_imb = lq.get("depth_imbalance", 0.0)
        bid_pressure = 50 + d_imb / 2
        ask_pressure = 100 - bid_pressure
        whale_dist = abs(bid_walls[0][0] - price) if bid_walls else abs(ask_walls[0][0] - price) if ask_walls else 500
        swell = bid_pressure / ask_pressure if ask_pressure > 0 else 1.0
        
        # Col 3 Data Update: 8-indicator confluence matrix (using real MTF data from engine)
        mt = self.market_state["mtf_trend"]
        c_score = 50 + (delta / 10) + (conf if td == "LONG" else -conf)
        mt["confluence_score"] = min(100, max(0, c_score))
        chop = "RANGING" if bb_width < self.data.get('atr', 10) * 2 else "TRENDING"
        
        # Read real MTF indicators from async engine
        rsi_5m = mt.get("rsi_5m", 50)
        rsi_15m = mt.get("rsi_15m", 50)
        rsi_1h = mt.get("rsi_1h", 50)
        macd_15m = mt.get("macd_15m", 0)
        macd_1h = mt.get("macd_1h", 0)
        ema_5m = mt.get("ema_cross_5m", "NEUTRAL")
        ema_15m = mt.get("ema_cross_15m", "NEUTRAL")
        ema_1h = mt.get("ema_cross_1h", "NEUTRAL")
        t_5m = mt.get("t_5m", "NEUTRAL")
        t_15m = mt.get("t_15m", "NEUTRAL")
        t_1h = mt.get("t_1h", "NEUTRAL")
        
        matrix_data = {
            "EMA CROSS": {"1M": "ALCISTA" if c_score > 55 else "BAJISTA", "5M": ema_5m, "15M": ema_15m, "1H": ema_1h},
            "SUPERTREND": {"1M": "ALCISTA" if td == "LONG" else "BAJISTA", "5M": t_5m, "15M": t_15m, "1H": t_1h},
            "WAVE TREND": {"1M": "SOBRECOMPRA" if rsi > 70 else "SOBREVENTA" if rsi < 30 else "NEUTRAL", "5M": "SOBRECOMPRA" if rsi_5m > 70 else "SOBREVENTA" if rsi_5m < 30 else "NEUTRAL", "15M": "SOBRECOMPRA" if rsi_15m > 70 else "SOBREVENTA" if rsi_15m < 30 else "NEUTRAL", "1H": "NEUTRAL"},
            "MACD ALIGN": {"1M": "CROSS UP" if self.data.get('macd', 0) > self.data.get('macd_signal', 0) else "CROSS DOWN", "5M": "CROSS UP" if macd_15m > 0 else "CROSS DOWN", "15M": "CROSS UP" if macd_15m > 0 else "CROSS DOWN", "1H": "CROSS UP" if macd_1h > 0 else "CROSS DOWN"},
            "PARABOLIC SAR": {"1M": "LONG" if td == "LONG" else "SHORT", "5M": "LONG" if t_5m == "ALCISTA" else "SHORT", "15M": "LONG" if t_15m == "ALCISTA" else "SHORT", "1H": "LONG" if t_1h == "ALCISTA" else "SHORT"},
            "RSI OSCILLATOR": {"1M": "OVERB" if rsi > 70 else "OVERS" if rsi < 30 else "NEUTRAL", "5M": "OVERB" if rsi_5m > 70 else "OVERS" if rsi_5m < 30 else "NEUTRAL", "15M": "OVERB" if rsi_15m > 70 else "OVERS" if rsi_15m < 30 else "NEUTRAL", "1H": "OVERB" if rsi_1h > 70 else "OVERS" if rsi_1h < 30 else "NEUTRAL"},
            "CHOPPINESS IND": {"1M": chop, "5M": "TRENDING" if t_5m != "NEUTRAL" else "RANGING", "15M": "TRENDING" if t_15m != "NEUTRAL" else "RANGING", "1H": "RANGING"},
            "ALGO BIAS": {"1M": "AGGRESSIVE" if buy_v > sell_v else "ABSORPTION", "5M": "EXHAUSTION" if rsi_5m > 70 or rsi_5m < 30 else "ABSORPTION", "15M": "ABSORPTION", "1H": "AGGRESSIVE" if t_1h == "ALCISTA" else "EXHAUSTION" if t_1h == "BAJISTA" else "ABSORPTION"}
        }
        
        # Col 4 Data Update: Read HFT metrics from async engine buffers
        mom = self.market_state["momentum"]
        ts = mom.get("tick_speed", self.stats.get('update_count', 0) % 50)
        ker = mom.get("kaufman_efficiency", 0.5)
        cancel_rate = mom.get("cancel_rate", 0.0)
        skew = mom.get("skewness", 0.0)
        spread = mom.get("spread_raw", 0.0) * 100  # convert to cents
        spread_vel = mom.get("spread_velocity", 0.0)
        depth_imb = lq.get("depth_imbalance", 0.0)
        pinam = mom.get("pinam", 0.0)
        vol_cluster = "HIGH EXPANSION" if bb_width > self.data.get('atr', 10) * 3 else "LOW COMPRESSION"
        
        # Update UI Bottom Panels
        if len(self.bottom_widgets) == 5:
            self.bottom_widgets[0].update_data([
                ("1s", oi_1s, acc_1s),
                ("5s", oi_5s, acc_5s),
                ("1m", oi_1m, acc_1m),
                ("5m", oi_5m, acc_5m),
            ])
            self.bottom_widgets[1].update_data(
                price,
                [("10x", lq["liq_pool_10x"]), ("25x", lq["liq_pool_25x"]),
                 ("50x", lq["liq_pool_50x"]), ("100x", lq["liq_pool_100x"])],
                bid_pressure, ask_pressure, whale_dist, swell
            )
            self.bottom_widgets[2].update_data(
                self.market_state["mtf_trend"]["confluence_score"], matrix_data
            )
            self.bottom_widgets[3].update_data(
                ts, ker, cancel_rate, skew, spread, spread_vel, depth_imb, pinam, vol_cluster
            )
            dpoc = 0.0
            if 'HEATMAP' in self.panels:
                dpoc = self.panels['HEATMAP'].get_dPOC()
                orderbook_imb = self.panels['HEATMAP'].get_orderbook_imbalance()
            
            self.bottom_widgets[4].update_data(
                self.market_state["ai_engine"]["risk_panel"], conf, price, dpoc, orderbook_imb if 'HEATMAP' in self.panels else 0.0,
                brain_bracket=final_risk if risk_override and final_risk.get('sl', 0) != 0 else None
            )
        
        # Trade Log Placeholders
        gv("LAST TRADE #1", "WAITING...", white)
        gv("LAST TRADE #2", "WAITING...", white)
        
        # ── Brain Office UI update (every cycle) ───────────────────────
        self._update_brain_office()

        # ── Auto-Learner analysis (throttled internally) ──────────────
        snap_for_learn = {
            'price': price,
            'delta': delta,
            'cvd': cvd,
            'rsi': rsi,
            'tick_speed': m_tick,
            'trap_status': self.data.get('trap_status', 'SIN TRAMPA'),
            'brain_direction': brain.get('direction', 'INCIERTO') if (brain := getattr(self, '_last_brain_decision', None)) else 'INCIERTO',
            'brain_confidence_pct': brain.get('confidence_pct', 0) if (brain := getattr(self, '_last_brain_decision', None)) else 0,
            'bb_position': self.data.get('bb_position', 50),
            'atr': atr_val,
            'ba_ratio': self.data.get('ba_ratio', 1.0),
            'signal_text': self.data.get('signal', 'WAIT'),
        }
        self._run_auto_learn_analysis(snap_for_learn)
    
    def order_state_available(self):
        """Check if order book data is available."""
        ob = self.data.get('order_book', {})
        return bool(ob.get('bids')) or bool(ob.get('asks'))
    
    def lower_to_normal_window(self):
        if self.isMaximized() or self.windowState() & Qt.WindowMaximized:
            self.showNormal()
        else:
            self.setWindowState(Qt.WindowMaximized)
            self.showMaximized()

    def close_application_cleanly(self):
        self.running = False

        # Stop the refresh timer
        if hasattr(self, 'update_timer') and self.update_timer is not None:
            self.update_timer.stop()

        # Stop async data engine
        if hasattr(self, '_async_engine') and self._async_engine is not None:
            self._async_engine.stop()

        # Stop telegram bot
        if hasattr(self, 'telegram_bot') and self.telegram_bot is not None:
            self.telegram_bot.stop()

        # Stop order executor thread
        if hasattr(self, 'order_executor') and self.order_executor is not None:
            self.order_executor.stop()

        # Clean up knowledge parser worker if still running
        if hasattr(self, '_brain_worker') and self._brain_worker is not None:
            self._brain_worker.requestInterruption()
            self._brain_worker.deleteLater()
            self._brain_worker = None

        # Let closeEvent propagate properly
        self.close()

        # Force exit
        QApplication.quit()

    def closeEvent(self, event):
        self.running = False
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainDashboard()
    window.show()
    sys.exit(app.exec_())