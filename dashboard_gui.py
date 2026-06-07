#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
# CARGA GLOBAL del .env — ANTES de cualquier import que use settings
# ═══════════════════════════════════════════════════════════════════════════════
from dotenv import load_dotenv
import os
_env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '.env'))
load_dotenv(_env_path)
print(f"[ENTRY] .env cargado desde: {_env_path}")

import sys
import asyncio
import threading
import time
import subprocess
import json
import sqlite3
from datetime import datetime, timedelta, timezone
import re
import math
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use('Qt5Agg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

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


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES DE PRECISIÓN — Mejoras v4-Speed
# ═══════════════════════════════════════════════════════════════════════════════
# Mejora 4 — Velocidad de precio
PRICE_VELOCITY_ABORT_THRESHOLD = 0.08      # % cambio en 1s → abortar
PRICE_VELOCITY_REDUCE_THRESHOLD = 0.04     # % cambio en 1s → reducir tamaño
PRICE_VELOCITY_WINDOW_SEC = 1.0            # ventana en segundos

# Mejora 3 — Ventana post-imbalance
IMBALANCE_WINDOW_SEC = 1.5                 # segundos para esperar tras imbalance
IMBALANCE_OB_THRESHOLD = 15                # |ob_pct - 50| > umbral

# Mejora 2 — Absorción de liquidez
ABSORPTION_MAX_WAIT_SEC = 0.8              # tiempo máximo de espera
ABSORPTION_ASK_THRESHOLD = 2.0             # reducción mínima BTC en ask walls (LONG)
ABSORPTION_BID_THRESHOLD = 2.0             # reducción mínima BTC en bid walls (SHORT)

# Filtros de entrada — CVD relativo y rango 4h
CVD_NEUTRALIZE_PRICE_CHANGE = 0.003   # 0.3% de cambio de precio en 1h
CVD_NEUTRALIZE_THRESHOLD    = 300     # unidades de CVD relativo
RANGO_4H_PENALTY_CONFIDENCE = 25
RANGO_4H_SHORT_MAX_POSITION = 0.45    # SHORT penalizado si precio en 45% inferior del rango
RANGO_4H_LONG_MIN_POSITION  = 0.55    # LONG penalizado si precio en 55% superior del rango
MOMENTUM_CONTRADICT_THRESHOLD = 0.002

# Mejora 5 — Confirmación institucional
INSTITUTIONAL_FLOW_BTC = 5.0               # flujo mínimo acumulado institucional
INSTITUTIONAL_FLOW_WINDOW_MS = 2000        # ventana de tiempo para acumular flujo
INSTITUTIONAL_MAX_WAIT_MS = 1200           # tiempo máximo de espera en ms

# Mejora 8 — Re-entry tras aborto
REENTRY_ZONE_PCT = 0.3                     # ±% para zona de re-intento
REENTRY_COOLDOWN_SEC = 30                  # segundos de espera máximo

# Mejora 7 — Ajuste de TP por velocidad
TP_VELOCITY_LOW_TRIGGER = 0.02             # % cambio lento → recortar TP
TP_VELOCITY_LOW_REDUCTION = 0.5            # factor reducción TP
TP_VELOCITY_HIGH_TRIGGER = 0.15            # % cambio violento → recortar TP
TP_VELOCITY_HIGH_REDUCTION = 0.8           # factor reducción TP

# Mejora 6 — Pesos dinámicos MTF
MTF_WEIGHT_LOW_VOL = {"1h": 5, "4h": 5, "1d": 5, "5m": 10, "15m": 10}
MTF_WEIGHT_MID_VOL = {"1h": 15, "4h": 10, "1d": 10, "5m": 5, "15m": 5}
MTF_WEIGHT_HIGH_VOL = {"1h": 10, "4h": 5, "1d": 5, "5m": 15, "15m": 15}

# Mejora 1 — Split entry
SPLIT_ENTRY_TICKETS = 3                    # número de micro-tickets
SPLIT_ENTRY_PULLBACK_WAIT_SEC = 0.8        # espera entre tickets


from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QGridLayout, QTableWidgetItem,
                             QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                             QTabWidget, QShortcut, QPushButton, QOpenGLWidget,
                             QSplitter, QPlainTextEdit, QTextBrowser,
                             QProgressBar, QSlider, QScrollArea,
                             QListWidget, QFileDialog, QLineEdit,
                             QTableWidget, QHeaderView, QDoubleSpinBox, QSpinBox,
                             QGroupBox, QRadioButton, QMessageBox)
from PyQt5.QtGui import QDoubleValidator
from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence, QPainter, QPainterPath, QPen, QPolygonF, QStaticText, QPixmap, QFontDatabase

from binance.client import Client
from config.settings import settings
from src.engine.order_flow import order_flow_engine
from src.telegram_bot import TelegramBot
from src.engine.order_executor import OrderExecutor
from src.engine.strategy import trading_strategy
from src.engine.async_data_engine import AsyncDataEngine
from src.database.supabase_manager import supabase_manager
from src.engine.gemini_brain import GeminiBrainManager, GeminiTradingDecision


# ── Validación temprana de variables de entorno ────────────────────────────
settings.validate()

try:
    client = Client(settings.BINANCE_REAL_API_KEY, settings.BINANCE_REAL_SECRET_KEY, testnet=False)
except Exception as e:
    print(f"[DASHBOARD] ⚠ No se pudo conectar con Binance REAL: {e}")
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

        # VOLATILITY CONE (replaces ghost candles)
        if self.predicted_candles and len(self.klines) > 1:
            last_close = float(self.klines[-1][4])
            atr = self.indicators.get('atr', 10)
            n_cells = len(self.predicted_candles)
            sep_x = draw_rect.left() + (nc * cw)

            # Cone vertices (polygon: upper 2σ → upper 1σ → lower 1σ → lower 2σ)
            cone_poly = QPolygonF()
            # Upper edge (right to left for closed polygon)
            for i in range(n_cells - 1, -1, -1):
                dx = (i + 1) * cw
                upper_1 = py(last_close + atr * 1.0 * (i + 1) / n_cells)
                cone_poly << QPointF(sep_x + dx, upper_1)
            # Lower edge (left to right)
            for i in range(n_cells):
                dx = (i + 1) * cw
                lower_1 = py(last_close - atr * 1.0 * (i + 1) / n_cells)
                cone_poly << QPointF(sep_x + dx, lower_1)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(100, 200, 255, 20))
            painter.drawPolygon(cone_poly)

            # 2σ outer band (wider cone)
            cone_poly_2 = QPolygonF()
            for i in range(n_cells - 1, -1, -1):
                dx = (i + 1) * cw
                upper_2 = py(last_close + atr * 2.0 * (i + 1) / n_cells)
                cone_poly_2 << QPointF(sep_x + dx, upper_2)
            for i in range(n_cells):
                dx = (i + 1) * cw
                lower_2 = py(last_close - atr * 2.0 * (i + 1) / n_cells)
                cone_poly_2 << QPointF(sep_x + dx, lower_2)

            painter.setBrush(QColor(100, 200, 255, 10))
            painter.drawPolygon(cone_poly_2)

            # Center dashed line (mean projection)
            painter.setPen(QPen(QColor(100, 200, 255, 80), 1, Qt.DashLine))
            for i in range(n_cells):
                dx = (i + 1) * cw
                mean_y = py(last_close)
                painter.drawPoint(QPointF(sep_x + dx, mean_y))

            # Label
            p_font = QFont(font); p_font.setPointSize(6); p_font.setBold(True); painter.setFont(p_font)
            painter.setPen(QColor(100, 200, 255))
            painter.drawText(int(sep_x + 3), draw_rect.top() + 10, "VOLATILITY CONE")

        # LIQUIDITY ARROW (from current price → nearest high-volume wall)
        if self.order_state:
            current_price = float(self.klines[-1][4]) if self.klines else 0
            bids = sorted([(float(b[0]), float(b[1])) for b in self.order_state.get('bids', [])],
                          key=lambda x: x[0], reverse=True)
            asks = sorted([(float(a[0]), float(a[1])) for a in self.order_state.get('asks', [])],
                          key=lambda x: x[0])
            all_walls = [(p, q, 'BID') for p, q in bids if q >= 5.0] + \
                        [(p, q, 'ASK') for p, q in asks if q >= 5.0]
            if all_walls and current_price > 0:
                target = max(all_walls, key=lambda x: x[1])  # highest volume wall
                tgt_p, tgt_q, tgt_side = target
                if min_p <= tgt_p <= max_p:
                    src_y = py(current_price)
                    tgt_y = py(tgt_p)
                    src_x = sep_x if self.predicted_candles else draw_rect.right()
                    tgt_x = draw_rect.right() - 10 if tgt_side == 'ASK' else draw_rect.left() + 10
                    arrow_color = QColor(0, 255, 102) if tgt_side == 'BID' else QColor(187, 0, 255)
                    painter.setPen(QPen(arrow_color, 2, Qt.SolidLine))
                    painter.drawLine(int(src_x), int(src_y), int(tgt_x), int(tgt_y))
                    # Arrowhead
                    angle = math.atan2(tgt_y - src_y, tgt_x - src_x)
                    arrow_sz = 8
                    ax = int(tgt_x - arrow_sz * math.cos(angle - 0.4))
                    ay = int(tgt_y - arrow_sz * math.sin(angle - 0.4))
                    bx = int(tgt_x - arrow_sz * math.cos(angle + 0.4))
                    by = int(tgt_y - arrow_sz * math.sin(angle + 0.4))
                    painter.setBrush(arrow_color)
                    painter.setPen(Qt.NoPen)
                    painter.drawPolygon(QPolygonF([
                        QPointF(tgt_x, tgt_y), QPointF(ax, ay), QPointF(bx, by)
                    ]))
                    # Volume label
                    painter.setFont(p_font)
                    painter.setPen(arrow_color)
                    label_x = int((src_x + tgt_x) / 2)
                    label_y = int((src_y + tgt_y) / 2) - 8
                    painter.drawText(label_x, label_y,
                                     f"{tgt_side} {tgt_q:.1f}₿")
                
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

        # ── Macro Bounce State (1D support level rebounds) ────────────
        # (reserved)
        self.multiplicador_posicion = 1.0

        # ── FASE 0: Liquidity Target (v4-Pro) ─────────────────────────
        self.liquidity_magnet = "NONE"           # "SHORTS_ABOVE" | "LONGS_BELOW" | "NONE"
        self.magnet_price = 0.0
        self.provisional_tp = 0.0
        self.regimen_mercado = ""
        self.analisis_cuant = ""
        self.decision = "ESPERAR"                # ALZA | BAJA | ESPERAR
        self.sweep_status = "NONE"               # NONE | SWEEP_DETECTED | ABSORPTION_CONFIRMED_REVERSAL

        # ── Mejoras v4-Speed ─────────────────────────────────────────
        self._tick_history_3s = deque(maxlen=30)
        self._tick_integrity_score = 1.0
        self._book_depth_bids_volume = 0.0
        self._book_depth_asks_volume = 0.0
        self._funding_rate = 0.0
        self._oi_delta_5min = 0.0
        self._magnet_timestamp = 0.0
        self._magnet_price_at_set = 0.0

        # ── Mejora 3: ventana post-imbalance ─────────────────────────
        self.imbalance_detected_at = 0.0
        self.imbalance_direction = 0

        # ── Mejora 4: buffer de precio 1s ───────────────────────────
        self.price_buffer_1s = deque(maxlen=10)

        # ── FILTRO 1: historial CVD para CVD relativo ──────────────
        self._cvd_history = deque(maxlen=7200)  # 2h a 1 tick/segundo

        # ── FILTRO 2: historial precio para rango 4h ───────────────
        self._price_history_4h = deque(maxlen=14400)  # 4h a 1 tick/segundo
        self.posicion_rango_4h = 50.0

    def animate_step(self):
        diff = self.target_buy_pct - self.current_buy_pct
        if abs(diff) > 0.05:
            self.current_buy_pct += diff * 0.10
        self.pulse_phase = (self.pulse_phase + 2) % 360
        self.update()
    
    def update_battle(self, buy_volume, sell_volume, imbalance,
                      trend='NEUTRAL', rsi=50, cvd=0, prediction_dir='',
                      prediction_conf=0, confluence_score=50,
                      trend_1h='NEUTRAL', trend_4h='NEUTRAL', trend_1d='NEUTRAL',
                      trend_5m='NEUTRAL', trend_15m='NEUTRAL',
                      delta=0, tick_speed=0, cancel_rate=0, pinam=0,
                      bb_squeeze='NORMAL', atr=0, spread_velocity=0,
                      avg_volume=0, volatility_explosion=False,
                      price=0, bb_upper=0, bb_middle=0, bb_lower=0,
                      macd_line=0, macd_signal_line=0, macd_hist=0,
                      ema_20=0, ema_9=0, kaufman_eff=0.5,
                       upper_wick_pct=0.0, lower_wick_pct=0.0,
                       open_price=0.0, high_price=0.0, low_price=0.0,
                       critical_support=0.0, consecutive_red_bars=0,
                        spoofing_risk=0.0, hft_speed=0.0, active_trap="",
                        ba_ratio=1.0, depth_imb_pct=0.0, relative_volume=0.0,
                        liquidity_pools=None, whale_bid_walls=None, whale_ask_walls=None,
                        book_depth_bids_volume=0.0, book_depth_asks_volume=0.0,
                        funding_rate=0.0, oi_delta_5min=0.0):
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

        # ═══════════════════════════════════════════════════════════════
        # MEJORA 2: TICK INTEGRITY — últimos 3s (v4-Speed)
        # ═══════════════════════════════════════════════════════════════
        self._tick_history_3s.append(tick_speed)
        recent_ticks = list(self._tick_history_3s)
        if len(recent_ticks) >= 10:
            avg_tick_3s = sum(recent_ticks) / len(recent_ticks)
        else:
            avg_tick_3s = tick_speed
        self._tick_integrity_score = avg_tick_3s  # used later by _compute_signal

        # ═══════════════════════════════════════════════════════════════
        # Nuevos campos para las mejoras v4-Speed
        # ═══════════════════════════════════════════════════════════════
        self._book_depth_bids_volume = book_depth_bids_volume
        self._book_depth_asks_volume = book_depth_asks_volume
        self._funding_rate = funding_rate
        self._oi_delta_5min = oi_delta_5min

        # ═══════════════════════════════════════════════════════════════
        # FASE 0: MAPA DE LIQUIDEZ MÁXIMA — Imán del Precio (v4-Pro)
        # ═══════════════════════════════════════════════════════════════
        if liquidity_pools is None:
            liquidity_pools = {}
        if whale_bid_walls is None:
            whale_bid_walls = []
        if whale_ask_walls is None:
            whale_ask_walls = []

        best_magnet_price = 0.0
        best_magnet_label = "NONE"
        best_magnet_dist = float('inf')

        # 1) Check nearest whale bid wall (liquidity below price)
        for w in whale_bid_walls:
            w_price = float(w.get('price', 0)) if isinstance(w, dict) else float(w[0])
            w_size = float(w.get('quantity', w.get('size', 0))) if isinstance(w, dict) else float(w[1])
            if w_price > 0 and price > 0:
                dist = abs(price - w_price)
                # Weight by size: larger walls attract more
                weighted_dist = dist / max(w_size, 0.1) * 10
                if weighted_dist < best_magnet_dist and w_size >= 2.0:
                    best_magnet_dist = weighted_dist
                    best_magnet_price = w_price
                    best_magnet_label = "LONGS_BELOW"

        # 2) Check nearest whale ask wall (liquidity above price)
        for w in whale_ask_walls:
            w_price = float(w.get('price', 0)) if isinstance(w, dict) else float(w[0])
            w_size = float(w.get('quantity', w.get('size', 0))) if isinstance(w, dict) else float(w[1])
            if w_price > 0 and price > 0:
                dist = abs(price - w_price)
                weighted_dist = dist / max(w_size, 0.1) * 10
                if weighted_dist < best_magnet_dist and w_size >= 2.0:
                    best_magnet_dist = weighted_dist
                    best_magnet_price = w_price
                    best_magnet_label = "SHORTS_ABOVE"

        # 3) Check liquidation pool levels
        pool_shorts = liquidity_pools.get('pool_shorts_arriba', [])
        pool_longs = liquidity_pools.get('pool_longs_abajo', [])
        for level in pool_shorts:
            if level > 0 and price > 0:
                d = abs(price - level)
                if d < best_magnet_dist:
                    best_magnet_dist = d
                    best_magnet_price = level
                    best_magnet_label = "SHORTS_ABOVE"
        for level in pool_longs:
            if level > 0 and price > 0:
                d = abs(price - level)
                if d < best_magnet_dist:
                    best_magnet_dist = d
                    best_magnet_price = level
                    best_magnet_label = "LONGS_BELOW"

        self.liquidity_magnet = best_magnet_label
        self.magnet_price = best_magnet_price

        # Track when magnet was set (for age validation)
        if best_magnet_price != self._magnet_price_at_set:
            self._magnet_timestamp = time.time()
            self._magnet_price_at_set = best_magnet_price

        # Set provisional TP 0.02% BEFORE the magnet for optimal fill
        if best_magnet_price > 0 and price > 0:
            if best_magnet_label == "SHORTS_ABOVE":
                self.provisional_tp = best_magnet_price * 0.9998
            elif best_magnet_label == "LONGS_BELOW":
                self.provisional_tp = best_magnet_price * 1.0002
            else:
                self.provisional_tp = 0.0
        else:
            self.provisional_tp = 0.0

        # ═══════════════════════════════════════════════════════════════
        # MEJORA 4: buffer de precio 1s (para velocity check)
        # ═══════════════════════════════════════════════════════════════
        if price > 0:
            self.price_buffer_1s.append((time.time(), price))
            self._price_history_4h.append(price)

        # ═══════════════════════════════════════════════════════════════
        # FILTRO 1: historial CVD
        # ═══════════════════════════════════════════════════════════════
        self._cvd_history.append({"ts": time.time(), "cvd": cvd})

        # ═══════════════════════════════════════════════════════════════
        # MEJORA 7: Ajustar TP dinámico por velocidad de precio
        # ═══════════════════════════════════════════════════════════════
        if self.provisional_tp > 0 and price > 0:
            self.provisional_tp = self._adjust_tp_by_velocity(
                self.provisional_tp, price, best_magnet_label
            )

        self._compute_signal(
            buy_volume, sell_volume, imbalance, trend, rsi, cvd,
            confluence_score, trend_1h, trend_4h, trend_1d,
            trend_5m=trend_5m, trend_15m=trend_15m,
            delta=delta, tick_speed=tick_speed,
            cancel_rate=cancel_rate, pinam=pinam,
            bb_squeeze=bb_squeeze, atr=atr,
            spread_velocity=spread_velocity,
            price=price, bb_upper=bb_upper, bb_middle=bb_middle, bb_lower=bb_lower,
            upper_wick_pct=upper_wick_pct,
            lower_wick_pct=lower_wick_pct,
            open_price=open_price, high_price=high_price, low_price=low_price,
            ema_20=ema_20,
            critical_support=critical_support,
            consecutive_red_bars=consecutive_red_bars,
            spoofing_risk=spoofing_risk,
            hft_speed=hft_speed,
            active_trap=active_trap,
            ba_ratio=ba_ratio,
            depth_imb_pct=depth_imb_pct,
            relative_volume=relative_volume,
            override=False,
        )

        # ═══════════════════════════════════════════════════════════════
        # FASE 3: AJUSTE DINÁMICO DE EJECUCIÓN — HFT → Multiplier (v4-Speed)
        # ═══════════════════════════════════════════════════════════════
        if self.trend_direction != "NEUTRAL" and hft_speed > 5 and depth_imb_pct > 0:
            self.confidence = min(100, self.confidence + 15)
            self.multiplicador_posicion = 1.5
        elif hft_speed < 0.5:
            self.multiplicador_posicion = 0.5
            if self.trend_direction != "NEUTRAL":
                self.confidence = max(0, self.confidence - 25)
        else:
            # Base: confidence-based multiplier
            if self.confidence >= 80:
                self.multiplicador_posicion = 1.5
            elif self.confidence >= 60:
                self.multiplicador_posicion = 1.0
            else:
                self.multiplicador_posicion = 0.5

    # ── FILTRO 1: CVD relativo (neutralizar CVD residual) ───────────────────
    def _get_cvd_relative(self, minutes=120) -> float:
        if len(self._cvd_history) < 10:
            return self._cvd_history[-1]["cvd"] if self._cvd_history else 0
        cutoff = time.time() - minutes * 60
        pasados = [x for x in self._cvd_history if x["ts"] >= cutoff]
        if not pasados:
            return 0
        return self._cvd_history[-1]["cvd"] - pasados[0]["cvd"]

    def _compute_signal(self, buy_volume, sell_volume, imbalance,
                        trend, rsi, cvd,
                        confluence_score, trend_1h, trend_4h, trend_1d='NEUTRAL',
                        trend_5m='NEUTRAL', trend_15m='NEUTRAL',
                        delta=0, tick_speed=0, cancel_rate=0, pinam=0,
                        bb_squeeze='NORMAL', atr=0, spread_velocity=0,
                        price=0, bb_upper=0, bb_middle=0, bb_lower=0,
                        upper_wick_pct=0.0, lower_wick_pct=0.0,
                        open_price=0.0, high_price=0.0, low_price=0.0,
                        ema_20=0.0,
                        critical_support=0.0, consecutive_red_bars=0,
                        spoofing_risk=0.0, hft_speed=0.0, active_trap="",
                        ba_ratio=1.0, depth_imb_pct=0.0, relative_volume=0.0,
                        override=False):
        """Weighted composite signal computation.

        When *override* is True (volatility explosion), spread_penalty is
        removed and ATR threshold is loosened to capture institutional flow.
        """
        spread_penalty = 0.8 if spread_velocity > 50 and not override else 1.0
        tick_penalty = 1.0

        # ══════════════════════════════════════════════════════════════
        # FASE 0a: MINIMUM BOOK DEPTH FILTER (Mejora 1 — v4-Speed)
        # ══════════════════════════════════════════════════════════════
        total_book_depth = self._book_depth_bids_volume + self._book_depth_asks_volume
        if total_book_depth > 0:
            bid_pct = (self._book_depth_bids_volume / total_book_depth) * 100
            ask_pct = (self._book_depth_asks_volume / total_book_depth) * 100
            min_side_pct = min(bid_pct, ask_pct)
            if min_side_pct < 5 and abs(depth_imb_pct) < 5 and 0.95 < ba_ratio < 1.05:
                self.trend_direction = "NEUTRAL"
                self.confidence = 0
                self.decision = "ESPERAR"
                self.regimen_mercado = "NO_PROFUNDIDAD"
                self.analisis_cuant = f"Book sin profundidad: bids {bid_pct:.0f}% asks {ask_pct:.0f}%"
                self.trend_label = "◆ BOOK SIN PROFUNDIDAD — FILTRO ACTIVADO"
                return

        # ══════════════════════════════════════════════════════════════
        # FASE 0b: TICK INTEGRITY PENALTY (Mejora 2 — v4-Speed)
        # ══════════════════════════════════════════════════════════════
        if self._tick_integrity_score < 3:
            tick_penalty = 0.7
            if len(self._tick_history_3s) >= 10:
                declining = all(
                    self._tick_history_3s[i] >= self._tick_history_3s[i+1]
                    for i in range(len(self._tick_history_3s) - 5)
                )
                if declining:
                    tick_penalty = 0.4

        # ── COMPONENT SCORES (0–100, 50 = neutral) ────────────────────

        # 1a. Volume delta force
        total = buy_volume + sell_volume + 0.001
        vol_pct = (buy_volume / total) * 100

        # 1b. Order book imbalance
        ob_pct = (imbalance + 1) * 50

        # ══════════════════════════════════════════════════════════════
        # MEJORA 3: Detectar imbalance fuerte — marcar timestamp
        # ══════════════════════════════════════════════════════════════
        if abs(ob_pct - 50) > IMBALANCE_OB_THRESHOLD:
            self.imbalance_detected_at = time.time()
            self.imbalance_direction = 1 if ob_pct > 50 else -1

        # 1c. CVD direction (FILTRO 1 — CVD relativo neutraliza residual)
        cvd_relativo = self._get_cvd_relative(120)
        if len(self._price_history_4h) >= 3600:
            precio_1h_ago = self._price_history_4h[-3600]
        elif self._price_history_4h:
            precio_1h_ago = self._price_history_4h[0]
        else:
            precio_1h_ago = 0
        precio_cambio_1h = (price - precio_1h_ago) / precio_1h_ago if precio_1h_ago else 0
        cvd_contradice = (
            (precio_cambio_1h > CVD_NEUTRALIZE_PRICE_CHANGE and cvd_relativo < -CVD_NEUTRALIZE_THRESHOLD) or
            (precio_cambio_1h < -CVD_NEUTRALIZE_PRICE_CHANGE and cvd_relativo > CVD_NEUTRALIZE_THRESHOLD)
        )
        if cvd_contradice:
            cvd_para_score = 0
            print(f"[CVD] Neutralizado: residual {cvd_relativo:.0f} vs precio_cambio {precio_cambio_1h*100:.3f}%")
        else:
            cvd_para_score = cvd_relativo
        if cvd_para_score > 50:    cvd_pct = 75
        elif cvd_para_score > 0:   cvd_pct = 60
        elif cvd_para_score < -50: cvd_pct = 25
        elif cvd_para_score < 0:   cvd_pct = 40
        else:                      cvd_pct = 50

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

        # 4. MTF — pesos dinámicos por ATR (Mejora 6)
        w = self._get_mtf_weights(atr)
        mtf_score = 50
        if trend_1h == 'ALCISTA':   mtf_score += w["1h"]
        elif trend_1h == 'BAJISTA': mtf_score -= w["1h"]
        if trend_4h == 'ALCISTA':   mtf_score += w["4h"]
        elif trend_4h == 'BAJISTA': mtf_score -= w["4h"]
        if trend_1d == 'ALCISTA':   mtf_score += w["1d"]
        elif trend_1d == 'BAJISTA': mtf_score -= w["1d"]
        if trend_5m == 'ALCISTA':   mtf_score += w["5m"]
        elif trend_5m == 'BAJISTA': mtf_score -= w["5m"]
        if trend_15m == 'ALCISTA':  mtf_score += w["15m"]
        elif trend_15m == 'BAJISTA': mtf_score -= w["15m"]
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
        composite = 50 + (raw_composite - 50) * spread_penalty * tick_penalty

        # ── DEBUG FIELDS (injected into snapshot) ─────────────────────
        self.debug_vol_pct = round(vol_pct, 1)
        self.debug_ob_pct = round(ob_pct, 1)
        self.debug_cvd_pct = round(cvd_pct, 1)
        self.debug_delta_pct = round(delta_pct, 1)
        self.debug_micro_pct = round(micro_pct, 1)
        self.debug_composite = round(composite, 2)
        self.debug_cvd_raw = cvd
        self.debug_delta_raw = delta
        self.debug_cvd_relativo = round(cvd_relativo, 1)

        # ══════════════════════════════════════════════════════════════
        # FASE 1a: SPOOFING > 70% — abortar inmediatamente
        # ══════════════════════════════════════════════════════════════
        if spoofing_risk > 70:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.decision = "ESPERAR"
            self.regimen_mercado = "BLOQUEO_POR_SPOOFING"
            self.analisis_cuant = f"Spoofing {spoofing_risk:.0f}% > 70% — manipulación activa"
            self.trend_label = "◆ SPOOFING > 70% — BLOQUEO POR MANIPULACIÓN"
            return

        # ── DYNAMIC THRESHOLDS ───────────────────────────────────────
        if override:
            threshold = 55  # looser threshold during explosion
        elif atr > 70:
            threshold = 65
        elif atr < 20:
            threshold = 58
        else:
            threshold = 62
        self.debug_threshold = threshold

        # ── PROVISIONAL SIGNAL ───────────────────────────────────────
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
            return  # no further checks needed

        # ══════════════════════════════════════════════════════════════
        # FASE 1: COMPUERTA DE MITIGACIÓN DE RIESGO (v4-Speed)
        # ══════════════════════════════════════════════════════════════
        # a) Spoofing > 70% — abortar inmediatamente
        if spoofing_risk > 70:
            self.trend_direction = "NEUTRAL"
            self.confidence = 0
            self.decision = "ESPERAR"
            self.regimen_mercado = "BLOQUEO_POR_SPOOFING"
            self.analisis_cuant = f"Spoofing {spoofing_risk:.0f}% > 70% — manipulación activa"
            self.trend_label = "◆ SPOOFING > 70% — BLOQUEO POR MANIPULACIÓN"
            return

        # b) Active trap cancela si coincide con la dirección provisional
        if active_trap:
            trap_upper = active_trap.upper()
            if ("TRAMPA_ALCISTA" in trap_upper or "TRAMPA ALCISTA" in trap_upper):
                if self.trend_direction == "LONG":
                    self.trend_direction = "NEUTRAL"
                    self.confidence = 0
                    self.decision = "ESPERAR"
                    self.regimen_mercado = "EVITANDO_TRAMPA_DEL_BOOK"
                    self.analisis_cuant = "Trampa alcista bloquea LONG"
                    self.trend_label = "◆ TRAMPA ALCISTA — BLOQUEO LONG"
                    return
            elif ("TRAMPA_BAJISTA" in trap_upper or "TRAMPA BAJISTA" in trap_upper):
                if self.trend_direction == "SHORT":
                    self.trend_direction = "NEUTRAL"
                    self.confidence = 0
                    self.decision = "ESPERAR"
                    self.regimen_mercado = "EVITANDO_TRAMPA_DEL_BOOK"
                    self.analisis_cuant = "Trampa bajista bloquea SHORT"
                    self.trend_label = "◆ TRAMPA BAJISTA — BLOQUEO SHORT"
                    return

        # ══════════════════════════════════════════════════════════════
        # FILTRO 2: Posición en rango 4h (movimiento consumido)
        # ══════════════════════════════════════════════════════════════
        self.posicion_rango_4h = 50.0
        if len(self._price_history_4h) >= 100:
            precio_min_4h = min(self._price_history_4h)
            precio_max_4h = max(self._price_history_4h)
            rango_4h = precio_max_4h - precio_min_4h
            if rango_4h > 0:
                posicion_rango = (price - precio_min_4h) / rango_4h
                self.posicion_rango_4h = round(posicion_rango * 100, 1)
                if self.trend_direction == "SHORT" and posicion_rango < RANGO_4H_SHORT_MAX_POSITION:
                    old_conf = self.confidence
                    self.confidence = max(0, self.confidence - RANGO_4H_PENALTY_CONFIDENCE)
                    print(f"[RANGO] SHORT penalizado: precio en {posicion_rango*100:.1f}% del rango 4h "
                          f"confianza {old_conf:.0f}% → {self.confidence:.0f}%")
                elif self.trend_direction == "LONG" and posicion_rango > RANGO_4H_LONG_MIN_POSITION:
                    old_conf = self.confidence
                    self.confidence = max(0, self.confidence - RANGO_4H_PENALTY_CONFIDENCE)
                    print(f"[RANGO] LONG penalizado: precio en {posicion_rango*100:.1f}% del rango 4h "
                          f"confianza {old_conf:.0f}% → {self.confidence:.0f}%")

        # ══════════════════════════════════════════════════════════════
        # FILTRO 3: Momentum de precio reciente (60s)
        # ══════════════════════════════════════════════════════════════
        self.momentum_1min_pct = 0.0
        if len(self._price_history_4h) >= 60:
            precio_hace_60s = list(self._price_history_4h)[-60]
            if precio_hace_60s > 0:
                momentum_1min = (price - precio_hace_60s) / precio_hace_60s
                self.momentum_1min_pct = round(momentum_1min * 100, 3)
                if self.trend_direction == "SHORT" and momentum_1min > MOMENTUM_CONTRADICT_THRESHOLD:
                    self.confidence = max(0, self.confidence - 30)
                    print(f"[MOMENTUM] SHORT penalizado por momentum alcista: "
                          f"{momentum_1min*100:.3f}% en 60s "
                          f"confianza → {self.confidence:.0f}%")
                elif self.trend_direction == "LONG" and momentum_1min < -MOMENTUM_CONTRADICT_THRESHOLD:
                    self.confidence = max(0, self.confidence - 30)
                    print(f"[MOMENTUM] LONG penalizado por momentum bajista: "
                          f"{momentum_1min*100:.3f}% en 60s "
                          f"confianza → {self.confidence:.0f}%")

        # ══════════════════════════════════════════════════════════════
        # OUTPUT: decision + regimen_mercado + analisis_cuant (v4-Speed)
        # ══════════════════════════════════════════════════════════════
        if self.trend_direction == "LONG":
            self.decision = "ALZA"
            self.regimen_mercado = self.regimen_mercado or "ABSORCION_INSTITUCIONAL_CONFIRMADA"
            self.analisis_cuant = (
                f"ALZA conf={self.confidence:.0f}% magnet={self.liquidity_magnet} "
                f"tp={self.provisional_tp:.0f} hft={hft_speed:.1f}"
            )
        elif self.trend_direction == "SHORT":
            self.decision = "BAJA"
            self.regimen_mercado = self.regimen_mercado or "ABSORCION_INSTITUCIONAL_CONFIRMADA"
            self.analisis_cuant = (
                f"BAJA conf={self.confidence:.0f}% magnet={self.liquidity_magnet} "
                f"tp={self.provisional_tp:.0f} hft={hft_speed:.1f}"
            )
        else:
            self.decision = "ESPERAR"
            self.regimen_mercado = "RANGO_INDECISO"
            self.analisis_cuant = "ESPERAR — sin confluencia suficiente"

    # ── Mejora 7: Ajustar TP dinámico por velocidad de precio ─────────────
    def _adjust_tp_by_velocity(self, provisional_tp, price, magnet_label):
        if len(self.price_buffer_1s) < 5:
            return provisional_tp
        t0, p0 = self.price_buffer_1s[0]
        tn, pn = self.price_buffer_1s[-1]
        elapsed = tn - t0
        if elapsed <= 0:
            return provisional_tp
        velocity_pct = abs((pn - p0) / p0) * 100
        if velocity_pct < TP_VELOCITY_LOW_TRIGGER:
            return provisional_tp * TP_VELOCITY_LOW_REDUCTION
        elif velocity_pct > TP_VELOCITY_HIGH_TRIGGER:
            return provisional_tp * TP_VELOCITY_HIGH_REDUCTION
        return provisional_tp

    # ── Mejora 6: Pesos MTF dinámicos según ATR ──────────────────────────
    def _get_mtf_weights(self, atr):
        if atr > 70:
            return MTF_WEIGHT_HIGH_VOL
        elif atr < 30:
            return MTF_WEIGHT_LOW_VOL
        return MTF_WEIGHT_MID_VOL


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
    PANEL_NORMAL_BORDER = "1px solid #1a1f2e"
    PANEL_DIM_BORDER = "1px solid #1a1f2e"
    PANEL_ALERT_BORDER = "2px solid #ffcc00"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dimmed = False
        self._decision_state = ""
        self._liquidation_feed: deque = deque(maxlen=12)
        self._last_liq_update: str = ""
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

    def _build_stylesheet(self, border_style: str = PANEL_NORMAL_BORDER) -> str:
        return (f"background: #080a0f; border-radius: 8px; border: {border_style};"
                + (" opacity: 0.7;" if self._dimmed else ""))

    def set_dimmed(self, dimmed: bool):
        self._dimmed = dimmed
        border = self.PANEL_DIM_BORDER if dimmed else self.PANEL_NORMAL_BORDER
        self.setStyleSheet(self._build_stylesheet(border))

    def reset_flash(self):
        self.setStyleSheet(self._build_stylesheet(self.PANEL_NORMAL_BORDER))

    def trigger_flash(self, color):
        self.setStyleSheet(self._build_stylesheet(f"2px solid {color}"))
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
        hft_speed = state.get('hft_speed', 0.0)
        hft_color = "#FF6644" if hft_speed > 5 else "#ffcc00" if hft_speed > 2 else "#445566"
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

        whale_html += (f"<div style='margin-top:4px; font-family:monospace; font-size:10px;'>"
                       f"<span style='color:{hft_color};'>HFT Speed: {hft_speed:.1f} inst/s</span>"
                       f"</div>")
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

        # ── Liquidation Tracker mini-feed ────────────────────────────────
        liq_list = state.get('liquidation_events', [])
        if liq_list:
            liq_html = "<div style='margin-top:4px; border-top:1px solid #1a1f2e; padding-top:4px;'>"
            for liq in liq_list[-6:]:  # last 6
                is_long_liq = liq.get('side') == 'SELL'
                lcolor = "#00ff66" if is_long_liq else "#FF2244"
                badge = "LONG⬆" if is_long_liq else "SHORT⬇"
                lsize = liq.get('total_value', 0)
                liq_html += (f"<div style='display:inline-block; background:#0d1117; margin:1px; padding:1px 4px; "
                             f"border-left:2px solid {lcolor}; border-radius:2px; font-size:9px; font-family:monospace;'>"
                             f"<span style='color:{lcolor};'>{badge}</span> "
                             f"<span style='color:#aaa;'>${lsize:,.0f}</span>"
                             f"</div>")
            liq_html += "</div>"
            trap_html += liq_html

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

        cancel_rate_narr = state.get('cancel_rate', 0.0)
        spoofing_risk = state.get('spoofing_risk', 0.0)
        spoof_color = "#FF2244" if spoofing_risk > 70 else "#ffcc00" if spoofing_risk > 40 else "#445566"

        micro_html = (f"<table width='100%' style='font-family:monospace; font-size:11px;'>"
                      f"{self._row('CVD Trend', cvd_label, cvd_color)}"
                      f"{self._row('Kaufman Eff.', kauf_label, kauf_color)}"
                      f"{self._row('Vol B/A', f'{ba_ratio:.2f}x', ba_color)}"
                      f"{self._row('Spread Vel.', f'{spread_vel:.1f}ms', sv_color)}"
                      f"{self._row('Tick Speed', f'{ts:.1f}/s', '#aaa')}"
                      f"{self._row('Spoofing Risk', f'{spoofing_risk:.0f}%', spoof_color)}"
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

        # ── Emergency dimming: SEÑAL PARCIAL / SIN VENTAJA ──────────────
        should_dim = ("SEÑAL PARCIAL" in decision or "SIN VENTAJA" in decision)
        self.set_dimmed(should_dim)

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


class F4Worker(QThread):
    """Background worker for F4 API calls — prevents UI freezing."""

    balance_updated = pyqtSignal(float, float, float)
    leverage_result = pyqtSignal(bool, int)
    margin_result = pyqtSignal(bool, str)
    order_result_signal = pyqtSignal(bool, str, float, float, str, str)
    env_toggled = pyqtSignal(bool, bool, float, float, float)

    def __init__(self, executor):
        super().__init__()
        self._executor = executor
        self._pending = None

    def fetch_balance(self):
        self._pending = "balance"
        if not self.isRunning():
            self.start()

    def set_leverage(self, lev: int):
        self._pending = ("leverage", lev)
        if not self.isRunning():
            self.start()

    def set_margin(self, mode: str):
        self._pending = ("margin", mode)
        if not self.isRunning():
            self.start()

    def place_order(self, side: str, qty: float, price: float):
        self._pending = ("order", side, qty, price)
        if not self.isRunning():
            self.start()

    def switch_env(self, testnet: bool):
        self._pending = ("switch_env", testnet)
        if not self.isRunning():
            self.start()

    def run(self):
        if self._executor is None:
            return
        pending = self._pending
        self._pending = None

        if pending == "balance":
            result = self._executor.get_balance()
            if result.get("success"):
                self.balance_updated.emit(
                    result["balance"],
                    result.get("available", 0),
                    result.get("unrealized_pnl", 0),
                )

        elif isinstance(pending, tuple):
            cmd = pending[0]
            if cmd == "leverage":
                lev = pending[1]
                res = self._executor.change_leverage_direct(lev)
                self.leverage_result.emit(res.get("success", False), lev)
            elif cmd == "margin":
                mode = pending[1]
                res = self._executor.change_margin_type(mode)
                self.margin_result.emit(res.get("success", False), mode)
            elif cmd == "order":
                side, qty, price = pending[1], pending[2], pending[3]
                res = self._executor.market_order_direct(side, qty, price)
                success = res.get("success", False)
                oid = res.get("order_id", "")
                fill = res.get("fill_price", 0)
                qty_filled = res.get("quantity", qty)
                msg = res.get("message", "")
                self.order_result_signal.emit(success, side, qty_filled, fill, oid, msg)
            elif cmd == "switch_env":
                testnet = pending[1]
                result = self._executor.switch_environment(testnet)
                self.env_toggled.emit(
                    result.get("success", False),
                    self._executor.is_testnet,
                    result.get("balance", 0),
                    result.get("available", 0),
                    result.get("unrealized_pnl", 0),
                )


# ══════════════════════════════════════════════════════════════════════════════
# SignalDataWorker — dedicated worker for REAL account data (balance, position, leverage)
# ══════════════════════════════════════════════════════════════════════════════

class SignalDataWorker(QThread):
    """Worker for REAL account data. Auto-reconnect with exponential backoff."""

    balance_updated = pyqtSignal(float, float, float)
    position_updated = pyqtSignal(object)
    leverage_updated = pyqtSignal(int)
    connection_status = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._client = None
        self._backoff = 1.0
        self._last_error_ts: float = 0.0

    def _ensure_client(self):
        if self._client is not None:
            return True
        try:
            api_key = settings.BINANCE_REAL_API_KEY
            secret = settings.BINANCE_REAL_SECRET_KEY
            if not api_key or not secret:
                raise ValueError("Missing REAL API keys — check BINANCE_REAL_API_KEY/SECRET")
            self._client = Client(api_key, secret, testnet=False)
            self._backoff = 1.0
            self.connection_status.emit(True, "Conectado a Binance REAL")
            return True
        except Exception as exc:
            self._client = None
            self.connection_status.emit(False, f"Error REAL: {exc}")
            return False

    def fetch_all(self):
        if not self._ensure_client():
            self.connection_status.emit(False, f"Reconectando en {self._backoff:.0f}s...")
            return
        try:
            account = self._client.futures_account()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    self.balance_updated.emit(
                        float(asset.get("walletBalance", 0)),
                        float(asset.get("availableBalance", 0)),
                        float(asset.get("unrealizedProfit", 0)),
                    )
                    break

            positions = self._client.futures_position_information(symbol=settings.get_symbol())
            pos = None
            lev = 0
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                raw_lev = int(float(p.get("leverage", 0)))
                if raw_lev > 0:
                    lev = raw_lev
                if amt != 0:
                    pos = {
                        "amt": amt,
                        "entry": float(p.get("entryPrice", 0)),
                        "mark": float(p.get("markPrice", 0)),
                        "upnl": float(p.get("unRealizedProfit", 0)),
                        "liq": float(p.get("liquidationPrice", 0)),
                        "leverage": raw_lev,
                    }
                    break
            self.position_updated.emit(pos)
            if lev > 0:
                self.leverage_updated.emit(lev)

            self._backoff = 1.0
            self.connection_status.emit(True, "Conectado a Binance REAL")

        except Exception as exc:
            self._backoff = min(self._backoff * 2, 60.0)
            self.connection_status.emit(False, f"Reconexión en {self._backoff:.0f}s...")
            now = time.time()
            if now - self._last_error_ts > 30.0:
                print(f"[SignalDataWorker] Error: {exc}")
                self._last_error_ts = now
            self._client = None

    def stop(self):
        self._running = False


# ══════════════════════════════════════════════════════════════════════════════
# SignalMonitorTab — F2: Signal monitoring & professional trading station
# ══════════════════════════════════════════════════════════════════════════════

class SignalMonitorTab(QFrame):
    """F2: Signal monitoring dashboard with AI analysis, P&L calculator, and execution."""

    _on_ai_result_signal = pyqtSignal(str, str, float, str, float, float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._executor = None
        self._f4worker = None
        self._gemini_brain = None
        self._brain_agent = None
        self._data_worker = None

        # Signal state
        self._signal_direction = "NEUTRAL"
        self._signal_confidence = 0.0
        self._signal_timestamp = 0.0
        self._prev_signal = "NEUTRAL"
        self._trend_label = "◆ WAIT"
        self._price = 0.0
        self._change_pct = 0.0
        self._current_price = 0.0

        # Account state (from SignalDataWorker)
        self._balance = 0.0
        self._available = 0.0
        self._unrealized_pnl = 0.0
        self._leverage = 0
        self._position = None
        self._data_connected = False

        # AI state
        self._auto_ai = True
        self._last_gemini = None
        self._last_brain = None
        self._ai_busy = False

        # Execution state
        self._order_busy = False
        self._risk_pct = 2.5
        self._capital_preset = 100

        # Market/indicator data cache
        self._rsi = 50.0
        self._macd = 0.0
        self._macd_signal = 0.0
        self._macd_hist = 0.0
        self._bb_upper = 0.0
        self._bb_middle = 0.0
        self._bb_lower = 0.0
        self._atr = 0.0
        self._ema_20 = 0.0
        self._delta = 0.0
        self._cvd = 0.0
        self._buy_vol = 0.0
        self._sell_vol = 0.0
        self._imbalance = 0.0
        self._tech_levels = {}
        self._mtf_trend = {}
        self._signal_text = "NINGUNA"
        self._bounce_sl = 0.0
        self._spoofing_risk = 0.0
        self._hft_speed = 0.0
        self._active_trap = ""
        self._depth_imb_pct = 0.0
        self._cancel_rate = 0.0
        self._multiplicador_posicion = 1.0
        self._battle_decision = "ESPERAR"
        self._regimen_mercado = ""
        self._analisis_cuant = ""
        self._liquidity_magnet = "NONE"
        self._provisional_tp = 0.0
        self._magnet_price = 0.0
        # Liquidity walls (for dynamic SL)
        self._wall_bid = 0.0
        self._wall_ask = 0.0

        # ── Mejoras v4-Speed ─────────────────────────────────────────
        # 7) Consecutive losses for adaptive cooldown
        self._consecutive_losses = 0
        self._tick_integrity_score = 1.0
        self._funding_rate = 0.0
        self._oi_delta_5min = 0.0
        self._magnet_timestamp = 0.0
        self._magnet_price_at_set = 0.0

        # ── Mejora 3: ventana post-imbalance ─────────────────────────
        self.imbalance_detected_at = 0.0
        self.imbalance_direction = 0

        # ── Mejora 4: buffer de precio 1s ───────────────────────────
        self.price_buffer_1s = deque(maxlen=10)

        # ── Mejora 5: cinta de operaciones (institucionales) ─────────
        self.trade_tape = deque(maxlen=100)

        # ── Mejora 2: seguimiento de absorción de ballenas ───────────
        self._whale_bid_walls = []
        self._whale_ask_walls = []

        # ── Mejora 8: re-entry tras aborto ───────────────────────────
        self.pending_reentry = None          # {"direction": ..., "price": ..., "timestamp": ...}

        # ── Entry stats (para ajuste de thresholds) ───────────────────
        self.entry_stats = {
            "reentry_rechazado_senal_vieja": 0,
        }

        self._on_ai_result_signal.connect(self._on_ai_result)
        self._init_ui()
        self._init_timers()

    # ── Public: wire external dependencies ───────────────────────────────

    def set_executor(self, executor, f4worker=None):
        self._executor = executor
        self._f4worker = f4worker
        if f4worker:
            f4worker.order_result_signal.connect(self._on_order_result_f4)
        self._data_worker = SignalDataWorker()
        self._data_worker.balance_updated.connect(self._on_balance)
        self._data_worker.position_updated.connect(self._on_position)
        self._data_worker.leverage_updated.connect(self._on_leverage)
        self._data_worker.connection_status.connect(self._on_connection_status)

    def set_ai_references(self, gemini_brain=None, brain_agent=None):
        self._gemini_brain = gemini_brain
        self._brain_agent = brain_agent

    # ── Public: update signal data from MainDashboard (1Hz) ─────────────

    def _get_active_position_side(self) -> Optional[str]:
        """Return 'LONG' or 'SHORT' if a position is open, None otherwise."""
        if self._position and isinstance(self._position, dict):
            amt = self._position.get("amt", 0)
            if abs(amt) > 0:
                return "LONG" if amt > 0 else "SHORT"
        return None

    def update_signal_data(self, data: dict):
        self._price = data.get("price", self._price)
        self._change_pct = data.get("change_pct", self._change_pct)
        self._current_price = self._price

        # ═══════════════════════════════════════════════════════════════
        # MEJORA 4: alimentar buffer de precio 1s
        # ═══════════════════════════════════════════════════════════════
        if self._price > 0:
            self.price_buffer_1s.append((time.time(), self._price))

        # ═══════════════════════════════════════════════════════════════
        # MEJORA 8: verificar si el precio volvió a la zona de re-entry
        # ═══════════════════════════════════════════════════════════════
        if self.pending_reentry is not None:
            re = self.pending_reentry
            if time.time() - re["timestamp"] > REENTRY_COOLDOWN_SEC:
                self.pending_reentry = None
            else:
                dist_pct = abs(self._price - re["price"]) / re["price"] * 100
                if dist_pct <= REENTRY_ZONE_PCT:
                    direction = re["direction"]
                    if direction in ("LONG", "SHORT"):
                        # ── FIX 2: señal demasiado vieja ────────────────
                        signal_age = (datetime.utcnow() - data.get("signal_ts", datetime.min)).total_seconds()
                        if signal_age > 8.0:
                            print(f"[SIGNAL MONITOR] Re-entry rechazado: señal tiene {signal_age:.1f}s de antigüedad")
                            self.entry_stats["reentry_rechazado_senal_vieja"] = self.entry_stats.get("reentry_rechazado_senal_vieja", 0) + 1
                            self.pending_reentry = None
                            return

                        # ── FIX 4: posición ya abierta ──────────────────
                        if self._position and abs(self._position.get("amt", 0)) > 0:
                            print("[SIGNAL MONITOR] Re-entry cancelado: posición ya abierta")
                            self.pending_reentry = None
                            return

                        # ── FIX 4: Re-verificar contexto antes de re-entry ──
                        # 1) Spoofing
                        # 1) Spoofing
                        spoofing_ok = self._spoofing_risk < 70
                        # 2) Trampa activa
                        trap_ok = True
                        if self._active_trap:
                            trap_upper = self._active_trap.upper()
                            if ("TRAMPA_ALCISTA" in trap_upper or "TRAMPA ALCISTA" in trap_upper):
                                if direction == "LONG":
                                    trap_ok = False
                            elif ("TRAMPA_BAJISTA" in trap_upper or "TRAMPA BAJISTA" in trap_upper):
                                if direction == "SHORT":
                                    trap_ok = False
                        # 3) Composite fresco del battle_bar
                        fresh_dir = data.get("direction", self._signal_direction)
                        fresh_conf = data.get("confidence", self._signal_confidence)
                        composite_ok = (fresh_dir == direction and fresh_conf >= 55)

                        if spoofing_ok and trap_ok and composite_ok:
                            print(f"[SIGNAL MONITOR] Precio retornó a zona — re-lanzando señal {direction}")
                            self._execute_signal_direct(direction)
                        else:
                            reasons = []
                            if not spoofing_ok:
                                reasons.append(f"spoofing={self._spoofing_risk:.0f}%")
                                self.entry_stats["reentry_rechazado_spoofing"] = self.entry_stats.get("reentry_rechazado_spoofing", 0) + 1
                            if not trap_ok:
                                reasons.append(f"trap={self._active_trap}")
                            if not composite_ok:
                                reasons.append(f"composite={fresh_dir}/{fresh_conf:.0f}% != {direction}")
                                self.entry_stats["reentry_rechazado_composite"] = self.entry_stats.get("reentry_rechazado_composite", 0) + 1
                            reason_str = " | ".join(reasons)
                            msg = f"🚫 RE-ENTRY RECHAZADO — {reason_str}"
                            print(f"[SIGNAL MONITOR] {msg}")
                            if self._executor and hasattr(self._executor, 'order_result'):
                                self._executor.order_result.emit(False, msg, {"error": msg, "reason": "reentry_rechazado"})
                    self.pending_reentry = None
                elif dist_pct > REENTRY_ZONE_PCT * 3:
                    self.pending_reentry = None

        self._signal_text = data.get("signal", self._signal_text)

        # ── Position Mutex: reject ALL signals while a position is open ──
        pos_side = self._get_active_position_side()
        incoming_dir = data.get("direction", "NEUTRAL")
        if pos_side is not None and incoming_dir != "NEUTRAL":
            print(f"🛡️ Seguridad: Señal {incoming_dir} ignorada porque ya existe una posición de {pos_side} activa en mercado.")
            self._signal_direction = "NEUTRAL"
            self._signal_confidence = 0
            self._trend_label = "◆ POSICIÓN ACTIVA — SIN SEÑAL"
        else:
            self._signal_direction = incoming_dir
            self._signal_confidence = data.get("confidence", self._signal_confidence)
            self._trend_label = data.get("trend_label", self._trend_label)

        # Indicator cache
        self._rsi = data.get("rsi", self._rsi)
        self._macd = data.get("macd", self._macd)
        self._macd_signal = data.get("macd_signal", self._macd_signal)
        self._macd_hist = data.get("macd_hist", self._macd_hist)
        self._bb_upper = data.get("bb_upper", self._bb_upper)
        self._bb_middle = data.get("bb_middle", self._bb_middle)
        self._bb_lower = data.get("bb_lower", self._bb_lower)
        self._atr = data.get("atr", self._atr)
        self._ema_20 = data.get("ema_20", self._ema_20)
        self._delta = data.get("delta", self._delta)
        self._cvd = data.get("cvd", self._cvd)
        self._buy_vol = data.get("buy_volume", self._buy_vol)
        self._sell_vol = data.get("sell_volume", self._sell_vol)
        self._imbalance = data.get("imbalance", self._imbalance)
        self._tech_levels = data.get("technical_levels", self._tech_levels) or {}
        self._mtf_trend = data.get("mtf_trend", self._mtf_trend) or {}
        self._wall_bid = data.get("wall_bid", self._wall_bid)
        self._wall_ask = data.get("wall_ask", self._wall_ask)
        self._bounce_sl = data.get("bounce_sl", self._bounce_sl)
        self._spoofing_risk = data.get("spoofing_risk", self._spoofing_risk)
        self._hft_speed = data.get("hft_speed", self._hft_speed)
        self._active_trap = data.get("active_trap", self._active_trap)
        self._depth_imb_pct = data.get("depth_imb_pct", self._depth_imb_pct)
        self._cancel_rate = data.get("cancel_rate", self._cancel_rate)
        self._multiplicador_posicion = data.get("multiplicador_posicion", self._multiplicador_posicion)
        self._battle_decision = data.get("decision", "ESPERAR")
        self._regimen_mercado = data.get("regimen_mercado", "")
        self._analisis_cuant = data.get("analisis_cuant", "")
        self._liquidity_magnet = data.get("liquidity_magnet", "NONE")
        self._provisional_tp = data.get("provisional_tp", 0.0)
        self._magnet_price = data.get("magnet_price", 0.0)

        # ── Mejoras v4-Speed: nuevos campos ──────────────────────────
        self._funding_rate = data.get("funding_rate", self._funding_rate)
        self._oi_delta_5min = data.get("oi_delta_5min", self._oi_delta_5min)
        self._magnet_timestamp = data.get("magnet_timestamp", self._magnet_timestamp)
        self._magnet_price_at_set = data.get("magnet_price_at_set", self._magnet_price_at_set)
        self._tick_integrity_score = data.get("tick_integrity_score", 1.0)

        # ── Mejora 3: propagar imbalance ───────────────────────────
        self.imbalance_detected_at = data.get("imbalance_detected_at", self.imbalance_detected_at)
        self.imbalance_direction = data.get("imbalance_direction", self.imbalance_direction)

        # ── Mejora 2: whale walls para absorción ───────────────────
        self._whale_bid_walls = data.get("whale_bid_walls", self._whale_bid_walls)
        self._whale_ask_walls = data.get("whale_ask_walls", self._whale_ask_walls)

        self._refresh_ui()

        # Detect signal change (minimum confidence threshold = 55%)
        if self._signal_direction != self._prev_signal:
            if self._signal_direction in ("LONG", "SHORT") and self._prev_signal == "NEUTRAL":
                if self._signal_confidence >= 55:
                    self._signal_timestamp = time.time()
                    self._on_signal_detected()
            self._prev_signal = self._signal_direction

    # ── UI Construction ─────────────────────────────────────────────────

    def _init_ui(self):
        self.setStyleSheet("background: #000; border: none;")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #000; }"
            "QScrollBar:vertical { width: 6px; background: #111; }"
            "QScrollBar::handle:vertical { background: #333; border-radius: 3px; }"
        )
        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: #000;")
        layout = QVBoxLayout(self._scroll_content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._build_header(layout)
        self._build_account_panel(layout)
        self._build_signal_panel(layout)
        self._build_technical_panel(layout)
        self._build_pnl_panel(layout)
        self._build_ai_panel(layout)
        self._build_sizing_panel(layout)
        self._build_execution_panel(layout)

        layout.addStretch()
        scroll.setWidget(self._scroll_content)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

    def _make_card(self, title, title_color):
        card = QFrame()
        card.setStyleSheet(
            "background: rgba(10,10,15,0.85); "
            "border: 1px solid rgba(255,255,255,0.06); "
            "border-radius: 6px; padding: 6px;")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(4)
        if title:
            hdr = QLabel(title)
            hdr.setStyleSheet(f"color: {title_color}; font-size: 12px; font-weight: 900; "
                              "background: transparent; border: none; padding-bottom: 4px;")
            card_layout.addWidget(hdr)
        return card, card_layout

    def _sl(self, text, color, size=10, bold=True):
        lb = QLabel(text)
        w = "bold" if bold else "normal"
        lb.setStyleSheet(f"color: {color}; font-size: {size}px; font-weight: {w}; background: transparent; border: none;")
        return lb

    # ── 1. HEADER ───────────────────────────────────────────────────────

    def _build_header(self, layout):
        card, cl = self._make_card("SIGNAL MONITOR — REAL", "#00ff66")
        hrow = QHBoxLayout()
        hrow.setSpacing(12)

        self._h_price = QLabel("$0.00")
        self._h_price.setStyleSheet("font-size: 22px; font-weight: 900; color: #ffcc00; background: transparent;")
        hrow.addWidget(self._h_price)

        self._h_change = QLabel("0.00%")
        self._h_change.setStyleSheet("font-size: 14px; font-weight: bold; background: transparent;")
        hrow.addWidget(self._h_change)

        self._h_signal_tag = QLabel("⚪ ESPERANDO")
        self._h_signal_tag.setStyleSheet(
            "font-size: 13px; font-weight: 900; background: transparent; "
            "border: 1px solid #666; border-radius: 4px; padding: 2px 8px;")
        hrow.addWidget(self._h_signal_tag)

        hrow.addStretch()
        self._h_status = QLabel("")
        self._h_status.setStyleSheet("font-size: 10px; color: #666; background: transparent;")
        hrow.addWidget(self._h_status)

        cl.addLayout(hrow)
        layout.addWidget(card)

    # ── 2. CUENTA REAL ──────────────────────────────────────────────────

    def _build_account_panel(self, layout):
        card, cl = self._make_card("CUENTA REAL — FUTURES", "#00ff66")
        row = QHBoxLayout()
        row.setSpacing(20)

        self._a_bal = self._sl("Balance: $0.00", "#ccc", 11)
        row.addWidget(self._a_bal)
        self._a_avail = self._sl("Disponible: $0.00", "#ccc", 11)
        row.addWidget(self._a_avail)
        self._a_upnl = self._sl("PnL no realizado: $0.00", "#666", 11)
        row.addWidget(self._a_upnl)
        self._a_lev = self._sl("Apalancamiento: —", "#aaa", 11)
        row.addWidget(self._a_lev)
        row.addStretch()
        self._a_conn = self._sl("⏳ Conectando...", "#ffcc00", 10)
        row.addWidget(self._a_conn)

        cl.addLayout(row)

        self._a_pos_frame = QFrame()
        self._a_pos_frame.setStyleSheet("border: 1px solid #333; border-radius: 4px; background: rgba(255,255,255,0.02);")
        pf = QHBoxLayout(self._a_pos_frame)
        pf.setContentsMargins(6, 3, 6, 3)
        self._a_pos_text = self._sl("Sin posición abierta", "#666", 10)
        pf.addWidget(self._a_pos_text)
        pf.addStretch()
        cl.addWidget(self._a_pos_frame)

        layout.addWidget(card)

    # ── 3. SEÑAL ACTIVA ─────────────────────────────────────────────────

    def _build_signal_panel(self, layout):
        card, cl = self._make_card("SEÑAL ACTIVA — ESTRATEGIA", "#bb00ff")

        # Giant direction indicator
        sig_row = QHBoxLayout()
        sig_row.setSpacing(12)
        self._sig_indicator = QLabel("⚪ ESPERANDO")
        self._sig_indicator.setStyleSheet(
            "font-size: 26px; font-weight: 900; background: transparent; "
            "border: 2px solid #555; border-radius: 8px; padding: 6px 16px;")
        sig_row.addWidget(self._sig_indicator)

        self._sig_conf_label = self._sl("Confianza: 0%", "#ffcc00", 14)
        sig_row.addWidget(self._sig_conf_label)
        sig_row.addStretch()
        cl.addLayout(sig_row)

        # Confidence bar
        self._conf_bar = QProgressBar()
        self._conf_bar.setRange(0, 100)
        self._conf_bar.setValue(0)
        self._conf_bar.setFixedHeight(10)
        self._conf_bar.setTextVisible(False)
        self._conf_bar.setStyleSheet(
            "QProgressBar { background: #111; border: 1px solid #333; border-radius: 4px; }"
            "QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #333, stop:0.5 #bb00ff, stop:1 #00ff66); border-radius: 3px; }")
        cl.addWidget(self._conf_bar)

        # 6 conditions checklist
        cond_grid = QHBoxLayout()
        cond_grid.setSpacing(8)
        self._cond_labels = {}
        cond_names = [
            ("d", "Delta > 0"),
            ("cvd", "CVD > 0"),
            ("rsi", "RSI en zona"),
            ("bb", "BB Position"),
            ("macd", "MACD Cross"),
            ("ema", "EMA Trend"),
            ("trend", "Trend Align"),
        ]
        for key, name in cond_names:
            vb = QVBoxLayout()
            vb.setSpacing(1)
            lb = self._sl(name, "#888", 8, False)
            vb.addWidget(lb)
            val = self._sl("—", "#555", 9)
            vb.addWidget(val)
            self._cond_labels[key] = val
            cond_grid.addLayout(vb)
        cl.addLayout(cond_grid)

        # Reason label
        self._sig_reason = self._sl("◆ Analizando mercado...", "#aaa", 10)
        cl.addWidget(self._sig_reason)

        layout.addWidget(card)

    # ── 4. ANÁLISIS TÉCNICO (2 columns) ─────────────────────────────────

    def _build_technical_panel(self, layout):
        card, cl = self._make_card("ANÁLISIS TÉCNICO", "#00ff88")
        cols = QHBoxLayout()
        cols.setSpacing(6)

        # LEFT: Technical Levels + Order Flow
        left_frame = QFrame()
        left_frame.setStyleSheet("border: 1px solid rgba(255,255,255,0.04); border-radius: 4px; background: rgba(0,0,0,0.3);")
        left_l = QVBoxLayout(left_frame)
        left_l.setContentsMargins(6, 4, 6, 4)
        left_l.setSpacing(2)
        left_l.addWidget(self._sl("NIVELES TÉCNICOS", "#00ff66", 10))
        self._t_fib = self._sl("Fibonacci: —", "#bb00ff", 9)
        left_l.addWidget(self._t_fib)
        self._t_sr = self._sl("S/R: —", "#ffcc00", 9)
        left_l.addWidget(self._t_sr)
        self._t_conf = self._sl("Confluencia: —", "#00ff88", 9)
        left_l.addWidget(self._t_conf)
        self._t_struct = self._sl("Estructura: —", "#aaa", 9)
        left_l.addWidget(self._t_struct)
        left_l.addWidget(self._sl("ORDER FLOW", "#00ff66", 10))
        self._t_delta = self._sl("Delta: —", "#ccc", 9)
        left_l.addWidget(self._t_delta)
        self._t_cvd = self._sl("CVD: —", "#ccc", 9)
        left_l.addWidget(self._t_cvd)
        self._t_bvol = self._sl("Buy Vol: —", "#00cc6a", 9)
        left_l.addWidget(self._t_bvol)
        self._t_svol = self._sl("Sell Vol: —", "#bb00ff", 9)
        left_l.addWidget(self._t_svol)
        self._t_imb = self._sl("Imbalance: —", "#ffcc00", 9)
        left_l.addWidget(self._t_imb)

        # RIGHT: Momentum + MTF
        right_frame = QFrame()
        right_frame.setStyleSheet("border: 1px solid rgba(255,255,255,0.04); border-radius: 4px; background: rgba(0,0,0,0.3);")
        right_l = QVBoxLayout(right_frame)
        right_l.setContentsMargins(6, 4, 6, 4)
        right_l.setSpacing(2)
        right_l.addWidget(self._sl("MOMENTUM & MTF", "#bb00ff", 10))
        self._t_rsi = self._sl("RSI: —", "#ffcc00", 9)
        right_l.addWidget(self._t_rsi)
        self._t_macd = self._sl("MACD: —", "#ccc", 9)
        right_l.addWidget(self._t_macd)
        self._t_bb = self._sl("BB: —", "#aaa", 9)
        right_l.addWidget(self._t_bb)
        self._t_atr = self._sl("ATR: —", "#ffcc00", 9)
        right_l.addWidget(self._t_atr)
        right_l.addWidget(self._sl("TENDENCIAS MTF", "#bb00ff", 10))
        self._t_trends = self._sl("1M→ 5M→ 15M→ 1H→ 4H→", "#aaa", 9)
        right_l.addWidget(self._t_trends)
        self._t_conf_score = self._sl("Confluencia: —", "#00ff88", 9)
        right_l.addWidget(self._t_conf_score)

        cols.addWidget(left_frame)
        cols.addWidget(right_frame)
        cl.addLayout(cols)
        layout.addWidget(card)

    # ── 5. CALCULADORA P&L ──────────────────────────────────────────────

    def _build_pnl_panel(self, layout):
        card, cl = self._make_card("CALCULADORA P&L REAL", "#ffcc00")
        self._pnl_frame = card
        self._pnl_frame.setVisible(False)

        row1 = QHBoxLayout()
        row1.setSpacing(16)
        self._pnl_risk = self._sl("Riesgo: 2.5%", "#ffcc00", 11)
        row1.addWidget(self._pnl_risk)

        self._risk_slider = QSlider(Qt.Horizontal)
        self._risk_slider.setRange(5, 100)
        self._risk_slider.setValue(25)
        self._risk_slider.setFixedWidth(180)
        self._risk_slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: #333; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #ffcc00; width: 12px; border-radius: 6px; margin: -4px 0; }"
            "QSlider::sub-page:horizontal { background: #ffcc00; border-radius: 2px; }")
        self._risk_slider.valueChanged.connect(self._on_risk_change)
        row1.addWidget(self._risk_slider)

        self._pnl_size = self._sl("Tamaño: — BTC", "#ccc", 10)
        row1.addWidget(self._pnl_size)
        row1.addStretch()
        cl.addLayout(row1)

        grid = QHBoxLayout()
        grid.setSpacing(12)
        self._pnl_loss = self._sl("Si SL → Pérdida: —", "#bb00ff", 11)
        grid.addWidget(self._pnl_loss)
        self._pnl_gain1 = self._sl("Si TP1 → Ganancia: —", "#00ff66", 11)
        grid.addWidget(self._pnl_gain1)
        self._pnl_gain2 = self._sl("Si TP2 → Ganancia: —", "#00ff88", 11)
        grid.addWidget(self._pnl_gain2)
        self._pnl_rr = self._sl("R/R: —", "#ffcc00", 11)
        grid.addWidget(self._pnl_rr)
        grid.addStretch()
        cl.addLayout(grid)

        layout.addWidget(card)

    # ── 6. AI COMENTARIO ────────────────────────────────────────────────

    def _build_ai_panel(self, layout):
        card, cl = self._make_card("AI — COMENTARIO DE MERCADO", "#bb00ff")

        # Controls row
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)

        self._ai_toggle_btn = QPushButton("🤖 Auto ON")
        self._ai_toggle_btn.setCheckable(True)
        self._ai_toggle_btn.setChecked(True)
        self._ai_toggle_btn.setStyleSheet(
            "QPushButton { background: #0a2e0a; color: #00ff66; border: 1px solid #00ff66; "
            "border-radius: 3px; padding: 3px 10px; font-size: 10px; font-weight: bold; }"
            "QPushButton:checked { background: #3a0a0a; color: #ff4444; border: 1px solid #ff4444; }")
        self._ai_toggle_btn.clicked.connect(self._toggle_auto_ai)
        ctrl.addWidget(self._ai_toggle_btn)

        self._ai_gemini_btn = QPushButton("🧠 Gemini")
        self._ai_gemini_btn.setStyleSheet(
            "QPushButton { background: #1a0a2e; color: #bb00ff; border: 1px solid #bb00ff; "
            "border-radius: 3px; padding: 3px 10px; font-size: 10px; font-weight: bold; }"
            "QPushButton:hover { background: #2a0f3f; }"
            "QPushButton:disabled { color: #555; border-color: #333; }")
        self._ai_gemini_btn.clicked.connect(self._run_gemini)
        ctrl.addWidget(self._ai_gemini_btn)

        self._ai_brain_btn = QPushButton("⚡ Quantum Brain")
        self._ai_brain_btn.setStyleSheet(
            "QPushButton { background: #0a1a2e; color: #00ff66; border: 1px solid #00ff66; "
            "border-radius: 3px; padding: 3px 10px; font-size: 10px; font-weight: bold; }"
            "QPushButton:hover { background: #0f2a3f; }"
            "QPushButton:disabled { color: #555; border-color: #333; }")
        self._ai_brain_btn.clicked.connect(self._run_brain)
        ctrl.addWidget(self._ai_brain_btn)

        self._ai_status = self._sl("✅ Listo", "#666", 9)
        ctrl.addWidget(self._ai_status)
        ctrl.addStretch()
        cl.addLayout(ctrl)

        # Result card
        self._ai_result_frame = QFrame()
        self._ai_result_frame.setStyleSheet("border: 1px solid #333; border-radius: 4px; background: rgba(0,0,0,0.3);")
        ai_rl = QVBoxLayout(self._ai_result_frame)
        ai_rl.setContentsMargins(6, 4, 6, 4)
        ai_rl.setSpacing(2)
        self._ai_decision = self._sl("Decisión: —", "#aaa", 12)
        ai_rl.addWidget(self._ai_decision)
        self._ai_reasoning = self._sl("", "#ccc", 9)
        self._ai_reasoning.setWordWrap(True)
        ai_rl.addWidget(self._ai_reasoning)
        scores = QHBoxLayout()
        self._ai_score_of = self._sl("OF: —", "#00ff66", 9)
        scores.addWidget(self._ai_score_of)
        self._ai_score_mom = self._sl("Mom: —", "#ffcc00", 9)
        scores.addWidget(self._ai_score_mom)
        self._ai_score_trend = self._sl("Trend: —", "#bb00ff", 9)
        scores.addWidget(self._ai_score_trend)
        scores.addStretch()
        ai_rl.addLayout(scores)

        bracket_info = QHBoxLayout()
        self._ai_bracket_info = self._sl("Bracket: —", "#aaa", 9)
        bracket_info.addWidget(self._ai_bracket_info)
        bracket_info.addStretch()
        ai_rl.addLayout(bracket_info)
        cl.addWidget(self._ai_result_frame)

        layout.addWidget(card)

    # ── 7. SIZING GLOBAL ─────────────────────────────────────────────────

    def _build_sizing_panel(self, layout):
        card = QGroupBox("⚙️ CONFIGURACIÓN DE TAMAÑO DE ORDEN GLOBAL")
        card.setStyleSheet(
            "QGroupBox { background: rgba(10,10,15,0.85); border: 1px solid #333; "
            "border-radius: 6px; margin-top: 12px; padding: 12px 8px 8px 8px; "
            "font-size: 11px; font-weight: bold; color: #DEFF9A; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; "
            "padding: 0 4px; }")
        cl = QVBoxLayout(card)
        cl.setSpacing(6)

        self._rad_all_in = QRadioButton("Operar con todo el capital disponible")
        self._rad_custom_amount = QRadioButton("Operar con monto fijo")
        for rb in (self._rad_all_in, self._rad_custom_amount):
            rb.setStyleSheet(
                "QRadioButton { color: #ccc; font-size: 10px; spacing: 6px; }"
                "QRadioButton::indicator { width: 14px; height: 14px; "
                "border: 2px solid #555; border-radius: 7px; background: #111; }"
                "QRadioButton::indicator:checked { background: #DEFF9A; "
                "border-color: #DEFF9A; }")

        self._rad_all_in.setChecked(settings.USE_ALL_IN)
        self._rad_custom_amount.setChecked(not settings.USE_ALL_IN)
        self._rad_all_in.toggled.connect(self._on_sizing_mode_toggle)
        cl.addWidget(self._rad_all_in)
        cl.addWidget(self._rad_custom_amount)

        amount_row = QHBoxLayout()
        amount_row.setSpacing(6)
        amt_label = QLabel("Cantidad (USD):")
        amt_label.setStyleSheet("color: #aaa; font-size: 10px; background: transparent;")
        amount_row.addWidget(amt_label)

        self._global_amount_input = QLineEdit()
        self._global_amount_input.setPlaceholderText("1.00")
        self._global_amount_input.setText(f"{settings.GLOBAL_TRADE_AMOUNT:.2f}")
        self._global_amount_input.setReadOnly(settings.USE_ALL_IN)
        self._global_amount_input.setValidator(QDoubleValidator(0.01, 999999.0, 2))
        self._global_amount_input.setStyleSheet(
            "QLineEdit { background: #0a0a0a; color: #fff; "
            "border: 1px solid #333; border-radius: 3px; padding: 4px 6px; "
            "font-size: 12px; }"
            "QLineEdit:focus { border-color: #DEFF9A; }"
            "QLineEdit:read-only { color: #555; }")
        amount_row.addWidget(self._global_amount_input)
        cl.addLayout(amount_row)

        self._btn_save_sizing = QPushButton("💾 Guardar y Aplicar a todas las Órdenes")
        self._btn_save_sizing.setStyleSheet(
            "QPushButton { background: #0a2e0a; color: #DEFF9A; "
            "border: 2px solid #DEFF9A; border-radius: 6px; padding: 6px 16px; "
            "font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: #0f4f0f; }"
            "QPushButton:pressed { background: #1a6a1a; }")
        self._btn_save_sizing.clicked.connect(self._save_sizing_config)
        cl.addWidget(self._btn_save_sizing)

        self._sizing_feedback = QLabel()
        self._sizing_feedback.setStyleSheet("font-size: 10px; font-weight: bold; background: transparent;")
        cl.addWidget(self._sizing_feedback)

        layout.addWidget(card)

    def _on_sizing_mode_toggle(self):
        is_all_in = self._rad_all_in.isChecked()
        self._global_amount_input.setReadOnly(is_all_in)
        self._global_amount_input.setStyleSheet(
            "QLineEdit { background: #0a0a0a; color: #fff; "
            "border: 1px solid #333; border-radius: 3px; padding: 4px 6px; "
            "font-size: 12px; }"
            "QLineEdit:focus { border-color: #DEFF9A; }"
            "QLineEdit:read-only { color: #555; }")

    def _save_sizing_config(self):
        use_all_in = self._rad_all_in.isChecked()
        settings.USE_ALL_IN = use_all_in

        if not use_all_in:
            try:
                amount = float(self._global_amount_input.text() or 0)
            except ValueError:
                amount = 0.0
            if amount < 1.00:
                QMessageBox.warning(self, "Monto Inválido",
                    "❌ El monto mínimo de prueba permitido es de $1.00 USD")
                self._rad_all_in.setChecked(True)
                settings.USE_ALL_IN = True
                return
            settings.set_global_trade_amount(amount)
            label_text = f"🟢 LOTE ACTIVO: ${amount:.2f} USD fijado para siguientes operaciones"
            label_color = "#00ff66"
        else:
            label_text = "🟢 MODO ALL-IN: Usando todo el capital disponible"
            label_color = "#ffcc00"

        self._sizing_feedback.setText(label_text)
        self._sizing_feedback.setStyleSheet(
            f"color: {label_color}; font-size: 10px; font-weight: bold; background: transparent;")
        print(f"[Sizing] Config guardada — USE_ALL_IN={use_all_in}, "
              f"GLOBAL_TRADE_AMOUNT={settings.GLOBAL_TRADE_AMOUNT:.2f}")

    # ── 8. EJECUCIÓN ────────────────────────────────────────────────────

    def _build_execution_panel(self, layout):
        card, cl = self._make_card("EJECUCIÓN PROFESIONAL", "#ff4444")

        row = QHBoxLayout()
        row.setSpacing(8)

        self._ex_long = QPushButton("🟢 ABRIR LONG")
        self._ex_long.setStyleSheet(
            "QPushButton { background: #0a2e0a; color: #00ff66; border: 2px solid #00ff66; "
            "border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: 900; }"
            "QPushButton:hover { background: #0f3f0f; }"
            "QPushButton:pressed { background: #1a5a1a; }"
            "QPushButton:disabled { color: #333; border-color: #222; background: #0a0a0a; }")
        self._ex_long.clicked.connect(lambda: self._execute_market("BUY"))
        row.addWidget(self._ex_long)

        self._ex_short = QPushButton("🔴 ABRIR SHORT")
        self._ex_short.setStyleSheet(
            "QPushButton { background: #3a0a0a; color: #ff4444; border: 2px solid #ff4444; "
            "border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: 900; }"
            "QPushButton:hover { background: #4f0f0f; }"
            "QPushButton:pressed { background: #5a1a1a; }"
            "QPushButton:disabled { color: #333; border-color: #222; background: #0a0a0a; }")
        self._ex_short.clicked.connect(lambda: self._execute_market("SELL"))
        row.addWidget(self._ex_short)

        self._ex_autorizar = QPushButton("🔒 Autorizar Señal")
        self._ex_autorizar.setStyleSheet(
            "QPushButton { background: #1a1a0a; color: #ffcc00; border: 2px solid #ffcc00; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; font-weight: 900; }"
            "QPushButton:hover { background: #2a2a0f; }"
            "QPushButton:pressed { background: #3a3a1a; }"
            "QPushButton:disabled { color: #333; border-color: #222; background: #0a0a0a; }")
        self._ex_autorizar.clicked.connect(self._execute_signal)
        row.addWidget(self._ex_autorizar)

        row.addStretch()
        self._ex_status = self._sl("", "#666", 10)
        row.addWidget(self._ex_status)
        cl.addLayout(row)

        # Capital preset
        cap_row = QHBoxLayout()
        cap_row.setSpacing(4)
        cap_row.addWidget(self._sl("Capital:", "#aaa", 9))
        for pct in [25, 50, 75, 100]:
            btn = QPushButton(f"{pct}%")
            btn.setStyleSheet(
                "QPushButton { background: #111; color: #aaa; border: 1px solid #333; "
                "border-radius: 3px; padding: 2px 8px; font-size: 9px; }"
                "QPushButton:hover { background: #222; color: #fff; }"
                f"QPushButton[pct='{pct}'] {{ }}")
            btn.clicked.connect(lambda checked, p=pct: self._set_capital(pct))
            if pct == 100:
                btn.setStyleSheet(
                    "QPushButton { background: #222; color: #00ff66; border: 1px solid #00ff66; "
                    "border-radius: 3px; padding: 2px 8px; font-size: 9px; }")
            cap_row.addWidget(btn)
        cap_row.addStretch()
        cl.addLayout(cap_row)

        layout.addWidget(card)

    # ── Timers ──────────────────────────────────────────────────────────

    def _init_timers(self):
        self._data_timer = QTimer(self)
        self._data_timer.timeout.connect(self._poll_data)
        self._data_timer.start(5000)

    def _poll_data(self):
        if self._data_worker and not self._data_worker.isRunning():
            self._data_worker.fetch_all()
        if self._executor is not None:
            try:
                self._executor.check_position_status()
            except Exception:
                pass

    # ── Data Handlers ───────────────────────────────────────────────────

    def _on_balance(self, bal, avail, upnl):
        self._balance = bal
        self._available = avail
        self._unrealized_pnl = upnl
        self._update_account_display()

    def _on_position(self, pos):
        self._position = pos
        self._update_account_display()

    def _on_leverage(self, lev):
        self._leverage = lev
        self._update_account_display()

    def _on_connection_status(self, ok, msg):
        self._data_connected = ok
        color = "#00ff66" if ok else "#ff4444"
        self._a_conn.setText(msg)
        self._a_conn.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent; border: none;")

    def _on_risk_change(self, val):
        self._risk_pct = val / 10.0
        self._update_pnl_calc()

    def _on_order_result_f4(self, success, side, qty, fill, oid, msg):
        self._order_busy = False
        self._ex_long.setEnabled(True)
        self._ex_short.setEnabled(True)
        self._ex_autorizar.setEnabled(True)
        icon = "✅" if success else "❌"
        self._ex_status.setText(f"{icon} {side} {qty:.4f} @ ${fill:.2f} id={oid}" if success else f"{icon} {msg}")
        self._ex_status.setStyleSheet(
            f"color: {'#00ff66' if success else '#ff4444'}; font-size: 10px; background: transparent; border: none;")

    # ── Signal Detection ────────────────────────────────────────────────

    def _on_signal_detected(self):
        pos_side = self._get_active_position_side()
        if pos_side is not None:
            print(f"🛡️ Seguridad: _on_signal_detected bloqueada — posición {pos_side} activa")
            return
        print(f"[SIGNAL MONITOR] Señal detectada: {self._signal_direction} ({self._signal_confidence:.0f}%) regimen={self._regimen_mercado}")
        self._pnl_frame.setVisible(True)
        self._update_pnl_calc()
        if self._auto_ai:
            self._run_gemini()

    def _toggle_auto_ai(self):
        self._auto_ai = self._ai_toggle_btn.isChecked()
        self._ai_toggle_btn.setText("🤖 Auto ON" if self._auto_ai else "🤖 Auto OFF")

    # ── AI Analysis ─────────────────────────────────────────────────────

    def _run_gemini(self):
        if self._ai_busy or self._gemini_brain is None:
            return
        self._ai_busy = True
        self._ai_status.setText("⟳ Analizando Gemini...")
        self._ai_status.setStyleSheet("color: #ffcc00; font-size: 9px; background: transparent; border: none;")
        self._ai_gemini_btn.setEnabled(False)
        self._ai_brain_btn.setEnabled(False)

        snapshot = self._build_snapshot()
        threading.Thread(target=self._gemini_worker, args=(snapshot,), daemon=True).start()

    def _gemini_worker(self, snapshot):
        try:
            result = asyncio.run(self._gemini_brain.execute_inference(snapshot))
            if result:
                self._last_gemini = result
                self._on_ai_result_signal.emit(
                    "gemini", result.decision, result.confianza,
                    result.analisis_cuant,
                    result.stop_loss, result.take_profit,
                    result.regimen_mercado,
                )
            else:
                self._on_ai_result_signal.emit("gemini", "ERROR", 0, "Error en inferencia Gemini", 0, 0, "")
        except Exception as exc:
            self._on_ai_result_signal.emit("gemini", "ERROR", 0, str(exc), 0, 0, "")

    def _run_brain(self):
        if self._ai_busy or self._brain_agent is None:
            return
        self._ai_busy = True
        self._ai_status.setText("⟳ Analizando Quantum Brain...")
        self._ai_status.setStyleSheet("color: #ffcc00; font-size: 9px; background: transparent; border: none;")
        self._ai_gemini_btn.setEnabled(False)
        self._ai_brain_btn.setEnabled(False)

        snapshot = self._build_snapshot()
        threading.Thread(target=self._brain_worker, args=(snapshot,), daemon=True).start()

    def _brain_worker(self, snapshot):
        try:
            result = self._brain_agent.infer_sync(snapshot)
            if result and result.get("direction"):
                self._last_brain = result
                bdir = result["direction"]
                bracket = result.get("risk_bracket") or result.get("risk", {})
                sl = bracket.get("sl", 0) if isinstance(bracket, dict) else 0
                tp = bracket.get("tp1", 0) if isinstance(bracket, dict) else 0
                self._on_ai_result_signal.emit(
                    "brain", bdir, result.get("confidence_pct", 0),
                    result.get("market_rationale", ""),
                    sl, tp, "",
                )
            else:
                self._on_ai_result_signal.emit("brain", "ERROR", 0, "Sin resultado", 0, 0, "")
        except Exception as exc:
            self._on_ai_result_signal.emit("brain", "ERROR", 0, str(exc), 0, 0, "")

    def _on_ai_result(self, source, decision, confianza, analisis_cuant,
                       stop_loss, take_profit, regimen_mercado):
        self._ai_busy = False
        self._ai_gemini_btn.setEnabled(True)
        self._ai_brain_btn.setEnabled(True)
        if decision == "ERROR":
            self._ai_status.setText(f"❌ {analisis_cuant}")
            self._ai_status.setStyleSheet("color: #ff4444; font-size: 9px; background: transparent; border: none;")
            return

        self._ai_status.setText(f"✅ {source.upper()} OK — {time.strftime('%H:%M:%S')}")
        self._ai_status.setStyleSheet("color: #00ff66; font-size: 9px; background: transparent; border: none;")

        dir_icon = "🟢" if decision == "ALZA" else "🔴" if decision == "BAJA" else "⚪"
        self._ai_decision.setText(f"{dir_icon} Decisión: {decision} — Confianza: {confianza:.1f}%")
        self._ai_decision.setStyleSheet(
            f"color: {'#00ff66' if decision == 'ALZA' else '#ff4444' if decision == 'BAJA' else '#ffcc00'}; "
            "font-size: 12px; background: transparent; border: none;")

        self._ai_reasoning.setText(analisis_cuant)
        self._ai_reasoning.setStyleSheet("color: #ccc; font-size: 9px; background: transparent; border: none;")

        # Show regimen_mercado and SL/TP instead of old scores
        regime_icon = {
            "DIRECCIONAL_CON_VOLUMEN_HFT": "📈",
            "ABSORCION_INSTITUCIONAL_CONFIRMADA": "🔄",
            "LIQUIDITY_SWEEP_REVERSAL": "⚡",
            "BLOQUEO_POR_SPOOFING": "🚫",
            "EVITANDO_TRAMPA_DEL_BOOK": "⚠️",
            "RANGO_INDECISO": "⏸️",
            "DIRECCIONAL_A_FAVOR_DE_TENDENCIA": "📈",
            "ABSORCION_CONTRATENDENCIA_BLOQUEADA": "🚫",
        }.get(regimen_mercado, "⏸️")
        self._ai_score_of.setText(f"{regime_icon} {regimen_mercado[:25]}")
        self._ai_score_mom.setText(f"SL: ${stop_loss:,.0f}" if stop_loss else "SL: —")
        self._ai_score_trend.setText(f"TP: ${take_profit:,.0f}" if take_profit else "TP: —")

        if stop_loss and take_profit:
            self._ai_bracket_info.setText(
                f"SL ${stop_loss:,.0f} → TP ${take_profit:,.0f}" +
                (f" | {regimen_mercado}" if regimen_mercado else ""))
            self._ai_bracket_info.setStyleSheet("color: #ffcc00; font-size: 9px; background: transparent; border: none;")
        else:
            self._ai_bracket_info.setText("Bracket: —")

        self._update_pnl_calc()

    def _build_snapshot(self):
        return {
            "price": self._price,
            "change_pct": self._change_pct,
            "rsi": self._rsi,
            "macd": self._macd,
            "macd_signal": self._macd_signal,
            "macd_hist": self._macd_hist,
            "bb_upper": self._bb_upper,
            "bb_middle": self._bb_middle,
            "bb_lower": self._bb_lower,
            "atr": self._atr,
            "delta": self._delta,
            "cvd": self._cvd,
            "buy_volume": self._buy_vol,
            "sell_volume": self._sell_vol,
            "imbalance": self._imbalance,
            "volume": self._buy_vol + self._sell_vol,
            "avg_volume": (self._buy_vol + self._sell_vol) / max(self._atr, 1),
            "technical_levels": self._tech_levels,
            "signal": self._signal_text,
            "trend": self._mtf_trend.get("t_1m", "NEUTRAL"),
            "trend_5m": self._mtf_trend.get("t_5m", "NEUTRAL"),
            "trend_15m": self._mtf_trend.get("t_15m", "NEUTRAL"),
            "trend_1h": self._mtf_trend.get("t_1h", "NEUTRAL"),
            "trend_4h": self._mtf_trend.get("t_4h", "NEUTRAL"),
            "trend_1d": self._mtf_trend.get("t_1d", "NEUTRAL"),
            "confluence_score": self._mtf_trend.get("confluence_score", 50),
            "direction": self._signal_direction,
            "confidence_pct": self._signal_confidence,
            "spoofing_risk": self._spoofing_risk,
            "hft_speed": self._hft_speed,
            "trap_status": self._active_trap or "SIN TRAMPA",
            "cancel_rate": self._cancel_rate,
            "depth_imb_pct": self._depth_imb_pct,
            "decision": self._battle_decision,
            "regimen_mercado": self._regimen_mercado,
            "liquidity_magnet": self._liquidity_magnet,
            "provisional_tp": self._provisional_tp,
            "magnet_price": self._magnet_price,
            "funding_rate": self._funding_rate,
            "oi_delta_5min": self._oi_delta_5min,
            "tick_integrity_score": self._tick_integrity_score,
            "book_depth_bids_volume": self._book_depth_bids_volume,
            "book_depth_asks_volume": self._book_depth_asks_volume,
            "magnet_timestamp": self._magnet_timestamp,
            "magnet_price_at_set": self._magnet_price_at_set,
            "_snapshot_time": time.time(),
        }

    # ── Execution ──────────────────────────────────────────────────────

    def _execute_market(self, side):
        if self._order_busy or not self._f4worker:
            return
        pos_side = self._get_active_position_side()
        if pos_side is not None:
            print(f"🛡️ Seguridad: Market {side} bloqueada — posición {pos_side} activa")
            self._ex_status.setText(f"🚫 Posición {pos_side} activa — esperar cierre")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            return
        self._order_busy = True
        self._ex_long.setEnabled(False)
        self._ex_short.setEnabled(False)
        self._ex_autorizar.setEnabled(False)
        lev = max(self._leverage, 1)
        if settings.USE_ALL_IN:
            capital = max(self._available, self._balance, 100) * self._capital_preset / 100.0
        else:
            capital = settings.GLOBAL_TRADE_AMOUNT
            min_lev = math.ceil(0.001 * max(self._price, 1) / max(capital, 0.01))
            lev = max(lev, min_lev, 75)
        if self._executor and lev != self._leverage:
            self._executor.change_leverage_direct(lev)
            self._leverage = lev
        qty = (capital * lev) / max(self._price, 1)
        if qty < 0.001:
            qty = 0.001
        self._f4worker.place_order(side, qty, self._price)
        self._ex_status.setText(f"⟳ Enviando {side} {qty:.4f} BTC ({lev}x)…")
        self._ex_status.setStyleSheet("color: #ffcc00; font-size: 10px; background: transparent; border: none;")

    def _execute_signal(self):
        if self._order_busy or self._executor is None or self._signal_direction == "NEUTRAL":
            return

        # ── TTL: expire signals older than 45 seconds ────────────────
        elapsed = time.time() - self._signal_timestamp
        if self._signal_timestamp <= 0 or elapsed > 45:
            reason = (
                "⏰ SEÑAL EXPIRADA"
                if self._signal_timestamp > 0
                else "⚠️ SEÑAL NO INICIALIZADA"
            )
            msg = f"{reason} — Tiempo máximo de espera superado ({elapsed:.0f}s > 45s)"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            if self._executor and hasattr(self._executor, 'order_result'):
                self._executor.order_result.emit(
                    False, msg,
                    {"environment": "REAL" if (hasattr(self._executor, 'is_testnet')
                                               and not self._executor.is_testnet) else "TEST",
                     "direction": self._signal_direction,
                     "error": msg,
                     "confidence": self._signal_confidence,
                     "dynamic_leverage": 0},
                )
            self._order_busy = False
            self._ex_long.setEnabled(True)
            self._ex_short.setEnabled(True)
            self._ex_autorizar.setEnabled(True)
            return

        pos_side = self._get_active_position_side()
        if pos_side is not None:
            print(f"🛡️ Seguridad: Señal {self._signal_direction} bloqueada — posición {pos_side} activa")
            self._ex_status.setText(f"🚫 Posición {pos_side} activa — esperar cierre")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            return

        # ════════════════════════════════════════════════════════════════
        # MEJORA 4: PRICE VELOCITY — abortar si movimiento violento
        # ════════════════════════════════════════════════════════════════
        velo_result = self._check_price_velocity()
        if velo_result == "ABORTAR":
            reason = "PRICE_VELOCITY_ABORT"
            self.entry_stats[reason] = self.entry_stats.get(reason, 0) + 1
            msg = f"⏰ VELOCIDAD DE PRECIO EXCESIVA — señal abortada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            if self._executor and hasattr(self._executor, 'order_result'):
                self._executor.order_result.emit(False, msg, {"error": msg, "reason": reason})
            self._order_busy = False
            return
        elif velo_result == "REDUCIR":
            self._multiplicador_posicion *= 0.5
            print(f"[SIGNAL MONITOR] Velocidad elevada — multiplicador reducido 50%")

        # ════════════════════════════════════════════════════════════════
        # MEJORA 3: VENTANA POST-IMBALANCE — abortar si pasó ventana
        # ════════════════════════════════════════════════════════════════
        if self.imbalance_detected_at > 0:
            imbalance_elapsed = time.time() - self.imbalance_detected_at
            if imbalance_elapsed > IMBALANCE_WINDOW_SEC:
                reason = "IMBALANCE_WINDOW_EXPIRED"
                self.entry_stats[reason] = self.entry_stats.get(reason, 0) + 1
                msg = f"⏰ VENTANA POST-IMBALANCE EXPIRADA ({imbalance_elapsed:.1f}s > {IMBALANCE_WINDOW_SEC}s) — señal abortada"
                print(f"[SIGNAL MONITOR] {msg}")
                self._ex_status.setText(f"🚫 {msg}")
                self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
                self._order_busy = False
                return

        # ════════════════════════════════════════════════════════════════
        # MEJORA 2: ABSORCIÓN DE LIQUIDEZ — esperar hasta 800ms
        # ════════════════════════════════════════════════════════════════
        absorption_ok = self._wait_for_absorption()
        if not absorption_ok:
            reason = "ABSORPTION_FAILED"
            self.entry_stats[reason] = self.entry_stats.get(reason, 0) + 1
            msg = f"⏰ ABSORCIÓN DE LIQUIDEZ NO CONFIRMADA — señal abortada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return

        # ════════════════════════════════════════════════════════════════
        # MEJORA 5: CONFIRMACIÓN INSTITUCIONAL — esperar hasta 1200ms
        # ════════════════════════════════════════════════════════════════
        institutional_ok = self._wait_for_institutional_confirm()
        if not institutional_ok:
            reason = "INSTITUTIONAL_CONFIRM_FAILED"
            self.entry_stats[reason] = self.entry_stats.get(reason, 0) + 1
            msg = f"⏰ SIN CONFIRMACIÓN INSTITUCIONAL — señal abortada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return

        # ══════════════════════════════════════════════════════════════
        # MEJORA 3: AGE CHECK + VALIDACIÓN DEL LIQUIDITY MAGNET
        # ══════════════════════════════════════════════════════════════
        if self._magnet_timestamp > 0 and self._magnet_price_at_set > 0:
            magnet_age = time.time() - self._magnet_timestamp
            if magnet_age > 120:  # magnet older than 2 minutes
                msg = "⏰ IMÁN DE LIQUIDEZ EXPIRADO (>120s) — señal abortada"
                print(f"[SIGNAL MONITOR] {msg}")
                self._ex_status.setText(f"🚫 {msg}")
                self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
                self._order_busy = False
                return
            price_drift = abs(self._price - self._magnet_price_at_set) / max(self._magnet_price_at_set, 1) * 100
            if price_drift > 0.5:
                msg = f"⏰ PRECIO SE DESVIÓ {price_drift:.2f}% DEL IMÁN — señal abortada"
                print(f"[SIGNAL MONITOR] {msg}")
                self._ex_status.setText(f"🚫 {msg}")
                self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
                # ══════════════════════════════════════════════════════
                # MEJORA 8: setear re-entry pendiente si aborta por drift
                # ══════════════════════════════════════════════════════
                self.pending_reentry = {
                    "direction": self._signal_direction,
                    "price": self._magnet_price_at_set,
                    "timestamp": time.time(),
                }
                self._order_busy = False
                return

        # ══════════════════════════════════════════════════════════════
        # MEJORA 4: FILTRO DE SESIÓN UTC
        # ══════════════════════════════════════════════════════════════
        now_utc = time.gmtime()
        utc_hour = now_utc.tm_hour
        utc_min = now_utc.tm_min
        # Session transitions (block entirely)
        if (utc_hour == 23 and utc_min >= 55) or (utc_hour == 0 and utc_min <= 5):
            msg = "⏰ TRANSICIÓN DE SESIÓN (23:55-00:05 UTC) — señal bloqueada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return
        if (utc_hour == 7 and utc_min >= 55) or (utc_hour == 8 and utc_min <= 5):
            msg = "⏰ TRANSICIÓN DE SESIÓN (07:55-08:05 UTC) — señal bloqueada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return
        if (utc_hour == 15 and utc_min >= 55) or (utc_hour == 16 and utc_min <= 5):
            msg = "⏰ TRANSICIÓN DE SESIÓN (15:55-16:05 UTC) — señal bloqueada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return
        # Low-liquidity windows: reduce position size
        low_liq = (utc_hour == 0 and utc_min >= 5) or utc_hour == 1 or (utc_hour == 2 and utc_min <= 30)
        low_liq = low_liq or (utc_hour == 12 or (utc_hour == 13 and utc_min <= 30))
        if low_liq:
            self._multiplicador_posicion *= 0.5
            print(f"[SIGNAL MONITOR] Ventana de baja liquidez — multiplicador reducido 50%")

        # ══════════════════════════════════════════════════════════════
        # MEJORA 5: FUNDING RATE Y OPEN INTEREST DELTA
        # ══════════════════════════════════════════════════════════════
        if abs(self._funding_rate) > 0.05:
            msg = f"⏰ FUNDING RATE ({self._funding_rate:+.4f}%) > 0.05% — señal bloqueada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return
        if abs(self._oi_delta_5min) > 15:
            msg = f"⏰ OI DELTA 5m ({self._oi_delta_5min:+.1f}%) > 15% — señal bloqueada"
            print(f"[SIGNAL MONITOR] {msg}")
            self._ex_status.setText(f"🚫 {msg}")
            self._ex_status.setStyleSheet("color: #ff4444; font-size: 10px; background: transparent; border: none;")
            self._order_busy = False
            return

        self._order_busy = True
        self._ex_long.setEnabled(False)
        self._ex_short.setEnabled(False)
        self._ex_autorizar.setEnabled(False)

        bdir = "ALZA" if self._signal_direction == "LONG" else "BAJA"
        entry = self._price
        sl = 0
        tp1 = 0

        # Try Gemini v2 flat fields first, then BrainAgent legacy bracket
        if self._last_gemini:
            if hasattr(self._last_gemini, "stop_loss") and self._last_gemini.stop_loss:
                sl = self._last_gemini.stop_loss
                tp1 = self._last_gemini.take_profit
            elif hasattr(self._last_gemini, "bracket"):
                sl = self._last_gemini.bracket.stop_loss
                tp1 = self._last_gemini.bracket.take_profit_1
        elif self._last_brain:
            bracket = self._last_brain.get("risk_bracket") or self._last_brain.get("risk", {})
            if isinstance(bracket, dict):
                sl = bracket.get("sl", 0)
                tp1 = bracket.get("tp1", 0)

        if not sl or not tp1:
            # ══════════════════════════════════════════════════════════
            # MEJORA 6: SL DINÁMICO BASADO EN ESTRUCTURA DEL BOOK
            # ══════════════════════════════════════════════════════════
            colchon_estructural = self._atr if self._atr > 0 else entry * 0.002
            if self._signal_direction == "LONG":
                wall_sl = self._wall_bid - 5 if self._wall_bid > 0 else 0
                if wall_sl > 0:
                    book_dist = abs(entry - wall_sl)
                    if book_dist > entry * 0.0015:
                        sl = wall_sl + 2
                    else:
                        sl = wall_sl
                else:
                    sl = entry - colchon_estructural
                tp1 = entry * 1.02
            else:
                wall_sl = self._wall_ask + 5 if self._wall_ask > 0 else 0
                if wall_sl > 0:
                    book_dist = abs(wall_sl - entry)
                    if book_dist > entry * 0.0015:
                        sl = wall_sl - 2
                    else:
                        sl = wall_sl
                else:
                    sl = entry + colchon_estructural
                tp1 = entry * 0.98

        # Macro bounce SL override: use precise wick-low SL if available
        if self._bounce_sl > 0:
            sl = self._bounce_sl

        # ── FASE 0: Provisional TP from liquidity magnet (v4-Pro) ─────
        if self._provisional_tp > 0:
            if self._signal_direction == "LONG":
                tp1 = max(sl, min(tp1, self._provisional_tp))
            elif self._signal_direction == "SHORT":
                tp1 = min(sl, max(tp1, self._provisional_tp))

        # ══════════════════════════════════════════════════════════════
        # FASE 4: INYECCIÓN DE LIQUIDEZ DINÁMICA — Ajuste SL/TP
        # ══════════════════════════════════════════════════════════════
        if self._cancel_rate > 60:
            extra_sl_buffer = entry * 0.0005
            if self._signal_direction == "LONG":
                sl -= extra_sl_buffer
            else:
                sl += extra_sl_buffer

        if self._depth_imb_pct > 40:
            if self._signal_direction == "LONG":
                tp1 = max(sl, tp1 - entry * 0.001)
            elif self._signal_direction == "SHORT":
                tp1 = min(sl, tp1 + entry * 0.001)

        if settings.USE_ALL_IN:
            cap = max(self._available, self._balance, 100) * self._capital_preset / 100.0
        else:
            cap = settings.GLOBAL_TRADE_AMOUNT
        cap *= self._multiplicador_posicion
        lev = max(self._leverage, 10) if self._leverage > 0 else 10
        success = self._executor.execute_trade_signal(
            bdir, entry, sl, tp1, lev, cap,
            confidence=self._signal_confidence,
            atr=self._atr, wall_bid=self._wall_bid, wall_ask=self._wall_ask)
        if success:
            self._ex_status.setText("✅ Señal enviada a ejecución con bracket SL/TP")
        else:
            self._ex_status.setText(f"❌ Rechazado: {getattr(self._executor, '_last_reject_reason', 'desconocido')}")
        self._ex_status.setStyleSheet(f"color: {'#00ff66' if success else '#ff4444'}; font-size: 10px; background: transparent; border: none;")
        self._order_busy = False
        self._ex_long.setEnabled(True)
        self._ex_short.setEnabled(True)
        self._ex_autorizar.setEnabled(True)

    # ── Mejora 8: re-entry directo (llamado desde update_signal_data) ─────
    def _execute_signal_direct(self, direction):
        if self._order_busy or self._executor is None:
            return
        self._signal_direction = direction
        self._execute_signal()

    # ── Mejora 4: verificar velocidad de precio en ventana 1s ─────────────
    def _check_price_velocity(self):
        if len(self.price_buffer_1s) < 5:
            return "OK"
        t0, p0 = self.price_buffer_1s[0]
        tn, pn = self.price_buffer_1s[-1]
        elapsed = tn - t0
        if elapsed <= 0:
            return "OK"
        velocity_pct = abs((pn - p0) / p0) * 100
        if velocity_pct > PRICE_VELOCITY_ABORT_THRESHOLD:
            return "ABORTAR"
        if velocity_pct > PRICE_VELOCITY_REDUCE_THRESHOLD:
            return "REDUCIR"
        return "OK"

    # ── Mejora 5: confirmación institucional por flujo acumulado en ventana ─
    def _wait_for_institutional_confirm(self):
        start = time.time()
        while (time.time() - start) * 1000 < INSTITUTIONAL_MAX_WAIT_MS:
            now = time.time()
            window_trades = [
                t for t in self.trade_tape
                if (now - t.get("ts", now)) * 1000 < INSTITUTIONAL_FLOW_WINDOW_MS
            ]
            if len(window_trades) < 3:
                time.sleep(0.05)
                continue

            buy_flow = sum(abs(t.get("qty", 0)) for t in window_trades if t.get("side", "") == "BUY")
            sell_flow = sum(abs(t.get("qty", 0)) for t in window_trades if t.get("side", "") == "SELL")

            if self._signal_direction == "LONG":
                if buy_flow >= INSTITUTIONAL_FLOW_BTC and buy_flow > sell_flow * 1.5:
                    print(f"[SIGNAL MONITOR] Flujo institucional: {buy_flow:.2f} BTC buy / {sell_flow:.2f} BTC sell en 2s")
                    return True
            else:
                if sell_flow >= INSTITUTIONAL_FLOW_BTC and sell_flow > buy_flow * 1.5:
                    print(f"[SIGNAL MONITOR] Flujo institucional: {sell_flow:.2f} BTC sell / {buy_flow:.2f} BTC buy en 2s")
                    return True

            time.sleep(0.05)

        self.entry_stats["abortado_sin_flujo_institucional"] = self.entry_stats.get("abortado_sin_flujo_institucional", 0) + 1
        return False

    # ── Mejora 2: absorción por velocidad de desaparición de pared ───────
    def _wait_for_absorption(self):
        walls = self._whale_ask_walls if self._signal_direction == "LONG" else self._whale_bid_walls
        if not walls:
            return True

        def top5_vol(wlist):
            top5 = sorted(
                wlist,
                key=lambda w: abs(float(w.get("quantity", w.get("size", 0))) if isinstance(w, dict) else float(w[1])),
                reverse=True,
            )[:5]
            return sum(
                abs(float(w.get("quantity", w.get("size", 0))) if isinstance(w, dict) else float(w[1]))
                for w in top5
            )

        mediciones = []
        start = time.time()
        while (time.time() - start) < ABSORPTION_MAX_WAIT_SEC:
            mediciones.append((time.time(), top5_vol(walls)))
            if len(mediciones) >= 3:
                reduccion = mediciones[0][1] - mediciones[-1][1]
                threshold = ABSORPTION_ASK_THRESHOLD if self._signal_direction == "LONG" else ABSORPTION_BID_THRESHOLD
                if reduccion >= threshold:
                    return True
            time.sleep(0.05)
        return False

    def _set_capital(self, pct):
        self._capital_preset = pct
        self._update_pnl_calc()

    # ── UI Refresh ──────────────────────────────────────────────────────

    def _refresh_ui(self):
        self._update_header()
        self._update_signal_display()
        self._update_technical_display()
        self._update_pnl_calc()

    def _update_header(self):
        price_color = "#00ff66" if self._change_pct >= 0 else "#bb00ff"
        self._h_price.setText(f"${self._price:,.2f}")
        self._h_price.setStyleSheet(f"font-size: 22px; font-weight: 900; color: {price_color}; background: transparent;")
        self._h_change.setText(f"{self._change_pct:+.3f}%")
        self._h_change.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {price_color}; background: transparent;")

        if self._signal_direction == "LONG":
            tag_text = "🟢 LONG"
            tag_color = "#00ff66"
            tag_border = "#00ff66"
        elif self._signal_direction == "SHORT":
            tag_text = "🔴 SHORT"
            tag_color = "#ff4444"
            tag_border = "#ff4444"
        else:
            tag_text = "⚪ ESPERANDO"
            tag_color = "#ffcc00"
            tag_border = "#666"

        self._h_signal_tag.setText(tag_text)
        self._h_signal_tag.setStyleSheet(
            f"font-size: 13px; font-weight: 900; color: {tag_color}; background: transparent; "
            f"border: 1px solid {tag_border}; border-radius: 4px; padding: 2px 8px;")

        self._h_status.setText(time.strftime("%H:%M:%S UTC"))

    def _update_account_display(self):
        self._a_bal.setText(f"Balance: ${self._balance:,.2f}")
        self._a_avail.setText(f"Disponible: ${self._available:,.2f}")
        pnl_color = "#00ff66" if self._unrealized_pnl >= 0 else "#bb00ff"
        self._a_upnl.setText(f"PnL no realizado: ${self._unrealized_pnl:+,.2f}")
        self._a_upnl.setStyleSheet(f"color: {pnl_color}; font-size: 11px; background: transparent; border: none;")
        if self._leverage > 0:
            self._a_lev.setText(f"Apalancamiento: {self._leverage}x")
            self._a_lev.setStyleSheet("color: #ffcc00; font-size: 11px; background: transparent; border: none;")

        if self._position:
            amt = self._position.get("amt", 0)
            direction = "LONG" if amt > 0 else "SHORT"
            entry = self._position.get("entry", 0)
            mark = self._position.get("mark", 0)
            upnl = self._position.get("upnl", 0)
            liq = self._position.get("liq", 0)
            pnl_pct = (mark / max(entry, 1) - 1) * 100 * (1 if amt > 0 else -1)
            pnl_c = "#00ff66" if upnl >= 0 else "#bb00ff"
            self._a_pos_text.setText(
                f"⚠ {direction} {abs(amt):.4f} BTC @ ${entry:,.0f} | "
                f"Mark ${mark:,.0f} | PnL ${upnl:+,.2f} ({pnl_pct:+.2f}%) | "
                f"Liquidación ${liq:,.0f}")
            self._a_pos_text.setStyleSheet(f"color: {pnl_c}; font-size: 10px; background: transparent; border: none;")
            self._a_pos_frame.setVisible(True)
        else:
            self._a_pos_text.setText("Sin posición abierta")
            self._a_pos_text.setStyleSheet("color: #666; font-size: 10px; background: transparent; border: none;")
            self._a_pos_frame.setVisible(True)

    def _update_signal_display(self):
        pos_side = self._get_active_position_side()
        if pos_side is not None:
            sig_text = f"🔒 BLOQUEADO ({pos_side})"
            sig_color = "#ff6600"
            sig_border = "#ff6600"
            sig_bg = "rgba(255,102,0,0.05)"
        elif self._signal_direction == "LONG":
            sig_text = "🟢 LONG"
            sig_color = "#00ff66"
            sig_border = "#00ff66"
            sig_bg = "rgba(0,255,102,0.05)"
        elif self._signal_direction == "SHORT":
            sig_text = "🔴 SHORT"
            sig_color = "#ff4444"
            sig_border = "#ff4444"
            sig_bg = "rgba(255,68,68,0.05)"
        else:
            sig_text = "⚪ ESPERANDO"
            sig_color = "#ffcc00"
            sig_border = "#555"
            sig_bg = "transparent"

        self._sig_indicator.setText(sig_text)
        self._sig_indicator.setStyleSheet(
            f"font-size: 26px; font-weight: 900; color: {sig_color}; background: {sig_bg}; "
            f"border: 2px solid {sig_border}; border-radius: 8px; padding: 6px 16px;")
        self._sig_conf_label.setText(f"Confianza: {self._signal_confidence:.0f}%")
        self._sig_conf_label.setStyleSheet(f"color: {sig_color}; font-size: 14px; background: transparent; border: none;")
        self._conf_bar.setValue(int(min(100, self._signal_confidence)))
        self._sig_reason.setText(self._trend_label)

        # Update conditions — direction-aware checks
        is_long_sig = self._signal_direction == "LONG"
        is_short_sig = self._signal_direction == "SHORT"
        delta_ok = (self._delta > 0) if is_long_sig else (self._delta < 0) if is_short_sig else False
        cvd_ok = (self._cvd > 0) if is_long_sig else (self._cvd < 0) if is_short_sig else False
        trend = self._mtf_trend.get("t_1m", "NEUTRAL")
        rsi_val = self._rsi
        if trend == "ALCISTA":
            rsi_ok = rsi_val < 40 or rsi_val > 60
        elif trend == "BAJISTA":
            rsi_ok = rsi_val < 40 or rsi_val > 60
        else:
            rsi_ok = rsi_val < 30 or rsi_val > 70

        bb_pos = ((self._price - self._bb_lower) / max(self._bb_upper - self._bb_lower, 1)) * 100 if self._bb_upper > 0 else 50
        bb_ok = (bb_pos < 20 or bb_pos > 80) if (is_long_sig or is_short_sig) else False
        macd_line = self._macd
        macd_sig = self._macd_signal
        macd_h = self._macd_hist
        macd_ok = (macd_line > macd_sig and macd_h > 0) if is_long_sig else (macd_line < macd_sig and macd_h < 0) if is_short_sig else False
        ema_ok = (self._price > self._ema_20) if is_long_sig else (self._price < self._ema_20) if is_short_sig else False
        t1h = self._mtf_trend.get("t_1h", "NEUTRAL")
        t4h = self._mtf_trend.get("t_4h", "NEUTRAL")
        trend_ok = (t1h == "ALCISTA" and t4h == "ALCISTA") or (t1h == "BAJISTA" and t4h == "BAJISTA")

        cond_values = {
            "d": (delta_ok, f"{self._delta:+.1f}"),
            "cvd": (cvd_ok, f"{self._cvd:+.1f}"),
            "rsi": (rsi_ok, f"RSI={rsi_val:.0f}"),
            "bb": (bb_ok, f"pos={bb_pos:.0f}%"),
            "macd": (macd_ok, "bull" if macd_ok else "bear"),
            "trend": (trend_ok, f"1H={t1h} 4H={t4h}"),
            "ema": (ema_ok, f"{self._price:.0f}/{self._ema_20:.0f}" if self._ema_20 > 0 else "N/A"),
        }
        for key, (ok, val) in cond_values.items():
            lbl = self._cond_labels.get(key)
            if lbl:
                icon = "✅" if ok else "❌"
                c = "#00ff66" if ok else "#ff4444"
                lbl.setText(f"{icon} {val}")
                lbl.setStyleSheet(f"color: {c}; font-size: 9px; background: transparent; border: none;")

        # ── Strict confluence filter: Autorizar requires direction + EMA + MACD alignment ──
        is_real = (self._executor is not None and hasattr(self._executor, 'is_testnet')
                   and not self._executor.is_testnet)
        if is_long_sig or is_short_sig:
            confluence_ok = delta_ok and ema_ok and macd_ok
        else:
            confluence_ok = False
        if self._signal_direction == "NEUTRAL":
            self._ex_autorizar.setText("⏳ ESPERANDO SEÑAL")
            self._ex_autorizar.setEnabled(False)
        elif is_real and (self._signal_confidence < 40 or not confluence_ok):
            reason = "FILTRO MACRO" if not trend_ok else "SIN CONFLUENCIA"
            self._ex_autorizar.setText(f"🚨 {reason}")
            self._ex_autorizar.setEnabled(False)
        else:
            self._ex_autorizar.setText("🔒 Autorizar Señal")
            self._ex_autorizar.setEnabled(True)

    def _update_technical_display(self):
        tech = self._tech_levels
        price = self._price

        # Fibonacci
        fib_r = tech.get("fib_retracement", [])
        if fib_r:
            parts = [f"{fb['ratio']}→${fb['price']:,.0f}" for fb in fib_r[:4]]
            self._t_fib.setText(f"Fibonacci: {' | '.join(parts)}")
            self._t_fib.setStyleSheet("color: #bb00ff; font-size: 9px; background: transparent; border: none;")
        else:
            self._t_fib.setText("Fibonacci: —")

        # S/R
        ns = tech.get("nearest_support")
        nr = tech.get("nearest_resistance")
        parts = []
        if ns:
            npct = (ns["price"] / max(price, 1) - 1) * 100
            parts.append(f"Soporte ${ns['price']:,.0f} ({npct:+.2f}%)")
        if nr:
            npct = (nr["price"] / max(price, 1) - 1) * 100
            parts.append(f"Resistencia ${nr['price']:,.0f} ({npct:+.2f}%)")
        if parts:
            self._t_sr.setText(" | ".join(parts))
            self._t_sr.setStyleSheet("color: #ffcc00; font-size: 9px; background: transparent; border: none;")
        else:
            self._t_sr.setText("S/R: —")

        # Confluence
        cz = tech.get("confluence_zones", [])
        if cz:
            top_z = cz[0]
            self._t_conf.setText(f"Confluencia: ${top_z['price']:,.0f} (score:{top_z['score']})")
            self._t_conf.setStyleSheet("color: #00ff88; font-size: 9px; background: transparent; border: none;")
        else:
            self._t_conf.setText("Confluencia: —")

        # Market structure
        ms = tech.get("market_structure", "")
        if ms:
            self._t_struct.setText(f"Estructura: {ms}")
            self._t_struct.setStyleSheet("color: #ffcc00; font-size: 9px; background: transparent; border: none;")
        else:
            self._t_struct.setText("Estructura: —")

        # Order flow
        dc = "#00ff66" if self._delta >= 0 else "#bb00ff"
        self._t_delta.setText(f"Delta: {self._delta:+.2f}")
        self._t_delta.setStyleSheet(f"color: {dc}; font-size: 9px; background: transparent; border: none;")
        cc = "#00ff66" if self._cvd >= 0 else "#bb00ff"
        self._t_cvd.setText(f"CVD: {self._cvd:+.2f}")
        self._t_cvd.setStyleSheet(f"color: {cc}; font-size: 9px; background: transparent; border: none;")
        self._t_bvol.setText(f"Buy Vol: {self._buy_vol:.2f} BTC")
        self._t_svol.setText(f"Sell Vol: {self._sell_vol:.2f} BTC")
        ratio = self._buy_vol / max(self._sell_vol, 0.001)
        ic = "#00ff66" if ratio > 1.1 else "#bb00ff" if ratio < 0.9 else "#ffcc00"
        self._t_imb.setText(f"Imbalance: {self._imbalance:+.3f}")
        self._t_imb.setStyleSheet(f"color: {ic}; font-size: 9px; background: transparent; border: none;")

        # RSI
        rsi_color = "#bb00ff" if self._rsi > 70 else "#00ff66" if self._rsi < 30 else "#ffcc00"
        self._t_rsi.setText(f"RSI: {self._rsi:.1f}")
        self._t_rsi.setStyleSheet(f"color: {rsi_color}; font-size: 9px; background: transparent; border: none;")

        # MACD
        mc = "#00ff66" if self._macd >= self._macd_signal else "#bb00ff"
        self._t_macd.setText(f"MACD: {self._macd:.3f} (signal: {self._macd_signal:.3f})")
        self._t_macd.setStyleSheet(f"color: {mc}; font-size: 9px; background: transparent; border: none;")

        # BB
        if self._bb_upper > 0:
            squeeze = "SQUEEZE" if (self._bb_upper - self._bb_lower) < self._atr * 2 else "EXPANSION"
            sc = "#bb00ff" if squeeze == "SQUEEZE" else "#00ff88"
            self._t_bb.setText(f"BB: ↑${self._bb_upper:,.0f} ↓${self._bb_lower:,.0f} ({squeeze})")
            self._t_bb.setStyleSheet(f"color: {sc}; font-size: 9px; background: transparent; border: none;")
        else:
            self._t_bb.setText("BB: —")

        # ATR
        self._t_atr.setText(f"ATR: ${self._atr:.2f}")

        # MTF Trends
        mtf = self._mtf_trend
        trend_map = {"ALCISTA": "↗", "BAJISTA": "↘", "NEUTRAL": "→", "WAIT": "→"}
        t1 = trend_map.get(mtf.get("t_1m", "NEUTRAL"), "→")
        t5 = trend_map.get(mtf.get("t_5m", "NEUTRAL"), "→")
        t15 = trend_map.get(mtf.get("t_15m", "NEUTRAL"), "→")
        t1h = trend_map.get(mtf.get("t_1h", "NEUTRAL"), "→")
        t4h = trend_map.get(mtf.get("t_4h", "NEUTRAL"), "→")
        self._t_trends.setText(f"1M {t1}  5M {t5}  15M {t15}  1H {t1h}  4H {t4h}")
        self._t_trends.setStyleSheet("color: #aaa; font-size: 9px; background: transparent; border: none;")

        cs = mtf.get("confluence_score", 50)
        cs_color = "#00ff66" if cs >= 70 else "#ffcc00" if cs >= 40 else "#bb00ff"
        self._t_conf_score.setText(f"Confluencia: {cs:.0f}%")
        self._t_conf_score.setStyleSheet(f"color: {cs_color}; font-size: 9px; background: transparent; border: none;")

    def _update_pnl_calc(self):
        if self._signal_direction == "NEUTRAL" or self._price <= 0:
            return
        self._pnl_frame.setVisible(True)
        self._pnl_risk.setText(f"Riesgo: {self._risk_pct:.1f}%")

        cap_base = max(self._available, self._balance, 100)
        cap_risk = cap_base * self._risk_pct / 100.0 * self._capital_preset / 100.0
        lev = max(self._leverage, 1) if self._leverage > 0 else 10
        pos_size = (cap_risk * lev) / max(self._price, 1)
        self._pnl_size.setText(f"Tamaño: {pos_size:.4f} BTC")

        sl_price = 0.0
        tp1_price = 0.0
        tp2_price = 0.0
        bracket = None
        if self._last_gemini:
            if hasattr(self._last_gemini, "stop_loss") and self._last_gemini.stop_loss:
                sl_price = self._last_gemini.stop_loss
                tp1_price = self._last_gemini.take_profit
                tp2_price = 0
            elif hasattr(self._last_gemini, "bracket"):
                bracket = self._last_gemini.bracket
        elif self._last_brain:
            bracket = self._last_brain.get("risk_bracket") or self._last_brain.get("risk", {})

        if bracket:
            sl_price = bracket.stop_loss if hasattr(bracket, "stop_loss") else bracket.get("sl", 0)
            tp1_price = bracket.take_profit_1 if hasattr(bracket, "take_profit_1") else bracket.get("tp1", 0)
            tp2_price = bracket.take_profit_2 if hasattr(bracket, "take_profit_2") else bracket.get("tp2", 0)

        is_long = self._signal_direction == "LONG"
        if sl_price <= 0:
            sl_price = self._price * 0.98 if is_long else self._price * 1.02
        if tp1_price <= 0:
            tp1_price = self._price * 1.02 if is_long else self._price * 0.98

        loss = (self._price - sl_price) * pos_size if is_long else (sl_price - self._price) * pos_size
        gain1 = (tp1_price - self._price) * pos_size if is_long else (self._price - tp1_price) * pos_size
        gain2 = (tp2_price - self._price) * pos_size if is_long and tp2_price > 0 else \
                (self._price - tp2_price) * pos_size if not is_long and tp2_price > 0 else 0

        loss_pct = loss / max(self._available, 1) * 100
        gain1_pct = gain1 / max(self._available, 1) * 100
        gain2_pct = gain2 / max(self._available, 1) * 100 if gain2 != 0 else 0

        rr1 = abs(gain1 / max(abs(loss), 0.01))
        rr2 = abs(gain2 / max(abs(loss), 0.01)) if gain2 != 0 else 0

        self._pnl_loss.setText(f"SL ${sl_price:,.0f} → ${loss:+,.2f} ({loss_pct:+.2f}%)")
        self._pnl_loss.setStyleSheet(f"color: #bb00ff; font-size: 11px; background: transparent; border: none;")
        self._pnl_gain1.setText(f"TP1 ${tp1_price:,.0f} → ${gain1:+,.2f} ({gain1_pct:+.2f}%)")
        self._pnl_gain1.setStyleSheet(f"color: #00ff66; font-size: 11px; background: transparent; border: none;")
        if gain2 != 0:
            self._pnl_gain2.setText(f"TP2 ${tp2_price:,.0f} → ${gain2:+,.2f} ({gain2_pct:+.2f}%)")
            self._pnl_gain2.setStyleSheet(f"color: #00ff88; font-size: 11px; background: transparent; border: none;")
            self._pnl_gain2.setVisible(True)
        else:
            self._pnl_gain2.setVisible(False)

        rr_text = f"1:{rr1:.2f}" if rr2 == 0 else f"1:{rr1:.2f} / 1:{rr2:.2f}"
        self._pnl_rr.setText(f"R/R: {rr_text}")
        self._pnl_rr.setStyleSheet(f"color: #ffcc00; font-size: 11px; background: transparent; border: none;")


class RiskManagementTab(QFrame):
    """F4: BB-450 REELS MODE — Riesgo, Registro y Curva de Capital."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._executor = None
        self._f4worker = None
        self._current_price = 0.0
        self._balance = 0.0
        self._available = 0.0
        self._unrealized_pnl = 0.0

        # ── JSON persistence ──────────────────────────────────────────
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self._trades_path = os.path.join(base_dir, 'trades_history.json')
        self._stats_path = os.path.join(base_dir, 'entry_stats_history.json')
        self._trades: list[dict] = []
        self._entry_stats: dict = {}

        # ── Leverage (updated from worker) ────────────────────────────
        self._leverage = 40

        # ── Pending open trade (compatibility with MainDashboard) ─────
        self._pending_trade: Optional[dict] = None

        self._init_ui()
        self._load_json()

    # ── Public: wire executor after construction ─────────────────────────────

    def set_executor(self, executor):
        self._executor = executor
        self._f4worker = F4Worker(executor)
        self._f4worker.balance_updated.connect(self._on_balance_fetched)
        self._f4worker.leverage_result.connect(self._on_leverage_result)
        self._f4worker.margin_result.connect(self._on_margin_result)
        executor.order_result.connect(self._on_executor_snapshot)
        executor.position_closed.connect(self._on_close_snapshot)
        self.refresh_env_data()

    def _on_balance_fetched(self, balance, available, upnl):
        self._balance = balance
        self._available = available
        self._unrealized_pnl = upnl
        self._update_env_display()

    def _on_leverage_result(self, success, leverage):
        if success:
            self._leverage = leverage

    def _on_margin_result(self, success, margin_type):
        pass

    # ── Snapshot handlers ──────────────────────────────────────────────────

    def _on_executor_snapshot(self, success, msg, data):
        """Receive open/close snapshots from OrderExecutor.order_result."""
        event = data.get("event", "")
        if event == "open":
            self._on_trade_open(data)
        elif event == "close":
            self._on_trade_close(data)

    def _on_close_snapshot(self, data):
        """Receive close snapshots from OrderExecutor.position_closed."""
        self._on_trade_close(data)

    def _on_trade_open(self, data: dict):
        """Record a new open trade from executor snapshot."""
        self._pending_trade = {
            "fecha": data.get("open_timestamp", datetime.now(timezone.utc).isoformat()),
            "par": settings.get_symbol(),
            "direccion": data.get("side", data.get("direction", "BUY")),
            "precio_entrada": data.get("price", data.get("entry_price", 0)),
            "sl": data.get("sl_price", 0),
            "tp": data.get("tp_price", 0),
            "btc": data.get("qty_btc", data.get("total_qty", data.get("qty", 0))),
            "margen": data.get("margen_usdt", data.get("capital", 0)),
            "apalancamiento": data.get("apalancamiento", data.get("leverage", 40)),
            "delta_entrada": data.get("delta_entrada", 0),
            "cvd_entrada": data.get("cvd_entrada", 0),
            "entry_stats": data.get("filtro_entrada", ""),
        }

    def _on_trade_close(self, data: dict):
        """Close pending trade and persist to JSON."""
        open_data = self._pending_trade
        if open_data is None:
            return
        trade = dict(open_data)
        trade.update({
            "precio_salida": data.get("exit_price", 0),
            "pnl_usdt": data.get("pnl_usdt", 0),
            "roe_pct": data.get("roe_pct", 0),
            "rr_real": data.get("rr_real", 0),
            "duracion_segundos": data.get("duracion_segundos", 0),
            "cierre": data.get("motivo_cierre", "MANUAL"),
        })
        self._trades.append(trade)
        self._pending_trade = None
        self._save_json()
        self._refresh_table()
        self._recalc_dashboard()
        self._redraw_curve()

    def refresh_env_data(self):
        if self._f4worker:
            self._f4worker.fetch_balance()
        elif self._executor:
            result = self._executor.get_balance()
            if result.get("success"):
                self._balance = result["balance"]
                self._available = result.get("available", 0)
                self._unrealized_pnl = result.get("unrealized_pnl", 0)
            self._update_env_display()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _init_ui(self):
        self.setStyleSheet("background: #000; border: none;")

        # ── Header (balance/env strip) ─────────────────────────────────────
        header = QFrame()
        header.setStyleSheet(
            f"background: rgba(10,10,15,0.85); "
            f"border: 1px solid {COLORS['border_glow']}; "
            f"border-radius: 6px; padding: 4px;")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 4, 10, 4)
        self._env_label = QLabel("🔴 REAL — PRODUCCIÓN")
        self._env_label.setStyleSheet(
            "font-size: 11px; font-weight: bold; padding: 2px 10px; "
            "border-radius: 4px; color: #ff4444; "
            "background: #3a0a0a; border: 1px solid #ff4444;")
        hl.addWidget(self._env_label)
        hl.addSpacing(10)
        self._bal_label = QLabel()
        self._bal_label.setStyleSheet("font-size: 11px; color: #ccc; background: transparent;")
        hl.addWidget(self._bal_label)
        hl.addSpacing(8)
        self._pnl_label = QLabel()
        self._pnl_label.setStyleSheet("font-size: 12px; font-weight: bold; background: transparent;")
        hl.addWidget(self._pnl_label)
        hl.addStretch()

        # ── QTabWidget with 3 sub-tabs ─────────────────────────────────────
        self._tab_widget = QTabWidget()
        _bg = COLORS['border_glow']
        self._tab_widget.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #222; background: #000; }"
            "QTabBar::tab { background: #0a0a0a; color: #888; padding: 6px 14px; "
            "font-size: 10px; font-weight: bold; border: 1px solid #1a1a1a; "
            "border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; }"
            "QTabBar::tab:selected { background: #000; color: #DEFF9A; "
            f"border-color: {_bg}; }}"
            "QTabBar::tab:hover { color: #ccc; }")

        self._build_register_tab()
        self._build_dashboard_tab()
        self._build_curve_tab()

        # ── Main layout ───────────────────────────────────────────────────
        ml = QVBoxLayout(self)
        ml.setContentsMargins(6, 4, 6, 4)
        ml.setSpacing(4)
        ml.addWidget(header)
        ml.addWidget(self._tab_widget)

        self._update_env_display()

    # ═══════════════════════════════════════════════════════════════════════
    # Sub-tab 1: 📋 Registro de Operaciones
    # ═══════════════════════════════════════════════════════════════════════

    def _build_register_tab(self):
        tab = QWidget()
        tab.setStyleSheet("background: #000;")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(4, 4, 4, 4)

        self._register_table = QTableWidget()
        headers = [
            "Fecha", "Par", "Dir", "Precio Ent.", "Precio Sal.",
            "SL", "TP", "BTC", "Margen", "Apalancamiento",
            "PnL USDT", "ROE%", "R:R", "Duración",
            "Delta Ent.", "CVD Ent.", "Cierre", "entry_stats",
        ]
        self._register_table.setColumnCount(len(headers))
        self._register_table.setHorizontalHeaderLabels(headers)
        hh = self._register_table.horizontalHeader()
        hh.setStyleSheet("QHeaderView::section { background: #111; color: #aaa; "
                         "border: 1px solid #222; padding: 2px; font-size: 9px; }")
        self._register_table.setAlternatingRowColors(True)
        self._register_table.setStyleSheet(
            "QTableWidget { background: #050505; color: #ccc; "
            "gridline-color: #1a1a1a; border: none; font-size: 10px; "
            "alternate-background-color: #0a0a0a; }"
            "QTableWidget::item { padding: 1px 3px; }"
            "QTableWidget::item:selected { background: #1a1a2e; color: #fff; }")
        self._register_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._register_table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self._register_table)

        self._tab_widget.addTab(tab, "📋 Registro de Operaciones")

    def _refresh_table(self):
        trades = list(reversed(self._trades))  # newest first
        self._register_table.setRowCount(len(trades))
        for row, t in enumerate(trades):
            pnl = t.get("pnl_usdt", 0)
            vals = [
                t.get("fecha", "—")[:19],
                t.get("par", "—"),
                "🟢 LONG" if t.get("direccion", "").upper() == "BUY" else "🔴 SHORT",
                f"${t.get('precio_entrada', 0):,.2f}" if t.get("precio_entrada") else "—",
                f"${t.get('precio_salida', 0):,.2f}" if t.get("precio_salida") else "—",
                f"${t.get('sl', 0):,.2f}" if t.get("sl") else "—",
                f"${t.get('tp', 0):,.2f}" if t.get("tp") else "—",
                f"{t.get('btc', 0):.4f}" if t.get("btc") else "—",
                f"${t.get('margen', 0):,.2f}" if t.get("margen") else "—",
                f"{t.get('apalancamiento', 0)}x" if t.get("apalancamiento") else "—",
                f"${pnl:+,.2f}" if pnl != 0 else "$0.00",
                f"{t.get('roe_pct', 0):+.1f}%" if t.get("roe_pct") else "—",
                f"1:{t.get('rr_real', 0):.2f}" if t.get("rr_real") else "—",
                self._fmt_duration(t.get("duracion_segundos", 0)),
                f"{t.get('delta_entrada', 0):+.1f}" if t.get("delta_entrada") else "—",
                f"{t.get('cvd_entrada', 0):+.1f}" if t.get("cvd_entrada") else "—",
                t.get("cierre", "—"),
                t.get("entry_stats", "—") or "—",
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col == 10:  # PnL column
                    item.setForeground(QColor("#00ff66") if pnl >= 0 else QColor("#F87171"))
                self._register_table.setItem(row, col, item)
            # Row color
            if pnl > 0:
                for col in range(len(vals)):
                    bg = self._register_table.item(row, col)
                    if bg:
                        bg.setBackground(QColor(0, 40, 0, 60))
            elif pnl < 0:
                for col in range(len(vals)):
                    bg = self._register_table.item(row, col)
                    if bg:
                        bg.setBackground(QColor(40, 0, 0, 60))
        self._register_table.resizeColumnsToContents()

    @staticmethod
    def _fmt_duration(secs):
        if not secs:
            return "—"
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h{m:02d}m"
        if m > 0:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    # ═══════════════════════════════════════════════════════════════════════
    # Sub-tab 2: 📊 Dashboard Ejecutivo
    # ═══════════════════════════════════════════════════════════════════════

    def _build_dashboard_tab(self):
        tab = QWidget()
        tab.setStyleSheet("background: #000;")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        def _kpi_card(title, label_ref, default="—", color="#DEFF9A"):
            card = QFrame()
            card.setStyleSheet("background: #0a0a0f; border: 1px solid #222; border-radius: 6px; padding: 6px;")
            cl = QVBoxLayout(card)
            cl.setSpacing(1)
            t = QLabel(title)
            t.setStyleSheet("color: #888; font-size: 9px; background: transparent;")
            cl.addWidget(t)
            v = QLabel(default)
            v.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold; background: transparent;")
            cl.addWidget(v)
            setattr(self, label_ref, v)
            return card

        # ── Fila 1: Cuenta ──
        f1 = QHBoxLayout()
        f1.setSpacing(6)
        f1.addWidget(_kpi_card("Balance Inicial", "_dash_ini_bal"))
        f1.addWidget(_kpi_card("Balance Actual", "_dash_cur_bal"))
        f1.addWidget(_kpi_card("PnL Total USDT", "_dash_pnl_total"))
        f1.addWidget(_kpi_card("PnL Hoy USDT", "_dash_pnl_today"))
        f1.addWidget(_kpi_card("PnL Semana USDT", "_dash_pnl_week"))
        lay.addLayout(f1)

        # ── Fila 2: Performance ──
        f2 = QHBoxLayout()
        f2.setSpacing(6)
        f2.addWidget(_kpi_card("Win Rate", "_dash_wr"))
        f2.addWidget(_kpi_card("Profit Factor", "_dash_pf"))
        f2.addWidget(_kpi_card("R:R Promedio", "_dash_avg_rr"))
        f2.addWidget(_kpi_card("Max Drawdown", "_dash_max_dd"))
        f2.addWidget(_kpi_card("Racha Actual", "_dash_streak"))
        lay.addLayout(f2)

        # ── Fila 3: Sistema ──
        f3 = QHBoxLayout()
        f3.setSpacing(6)
        f3.addWidget(_kpi_card("Señales Evaluadas Hoy", "_dash_eval"))
        f3.addWidget(_kpi_card("Señales Ejecutadas Hoy", "_dash_exec"))
        f3.addWidget(_kpi_card("Señales Abortadas Hoy", "_dash_abort"))
        f3.addWidget(_kpi_card("Filtro + Aborta", "_dash_top_filter"))
        f3.addWidget(_kpi_card("Cooldown Activo", "_dash_cooldown"))
        lay.addLayout(f3)

        # ── Refresh button ──
        refresh_btn = QPushButton("🔄 Actualizar desde Binance")
        refresh_btn.setStyleSheet(
            "QPushButton { background: #0a0a0f; color: #DEFF9A; "
            "border: 1px solid #DEFF9A; border-radius: 4px; padding: 6px; "
            "font-size: 10px; }"
            "QPushButton:hover { background: #1a2a1a; }")
        refresh_btn.clicked.connect(self.refresh_env_data)
        lay.addWidget(refresh_btn)
        lay.addStretch()

        self._tab_widget.addTab(tab, "📊 Dashboard Ejecutivo")

    def _recalc_dashboard(self):
        trades = self._trades
        total = len(trades)
        pnls = [t.get("pnl_usdt", 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        net = sum(pnls)

        # ── Account KPIs ──
        ini_bal = trades[0].get("margen", 0) if trades else 0
        cur_bal = max(self._balance, 0)

        # PnL today
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_start = (datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())).strftime("%Y-%m-%d")
        pnl_today = sum(t.get("pnl_usdt", 0) for t in trades if (t.get("fecha", "")[:10] == today_str))
        pnl_week = sum(t.get("pnl_usdt", 0) for t in trades if (t.get("fecha", "")[:10] >= week_start))

        self._set_kpi(self._dash_ini_bal, f"${ini_bal:.2f}" if ini_bal else "—", "#DEFF9A")
        self._set_kpi(self._dash_cur_bal, f"${cur_bal:.2f}" if cur_bal else "—", "#DEFF9A")
        pnl_color = "#00ff66" if net >= 0 else "#F87171"
        self._set_kpi(self._dash_pnl_total, f"${net:+,.2f}" if total else "$0.00", pnl_color)
        self._set_kpi(self._dash_pnl_today, f"${pnl_today:+,.2f}" if pnl_today else "$0.00",
                      "#00ff66" if pnl_today >= 0 else "#F87171")
        self._set_kpi(self._dash_pnl_week, f"${pnl_week:+,.2f}" if pnl_week else "$0.00",
                      "#00ff66" if pnl_week >= 0 else "#F87171")

        # ── Performance KPIs ──
        wr = (len(wins) / total * 100) if total > 0 else 0
        pf = abs(sum(wins) / sum(losses)) if sum(losses) else (float("inf") if wins else 0)
        rr_avg = sum(abs(t.get("rr_real", 0)) for t in trades) / total if total > 0 else 0

        # Max drawdown (peak-to-trough)
        running_max = -float("inf")
        max_dd = 0.0
        cumulative = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > running_max:
                running_max = cumulative
            dd = (running_max - cumulative) / max(running_max, 1) * 100
            if dd > max_dd:
                max_dd = dd

        # Streak
        streak = 0
        for p in reversed(pnls):
            if (streak >= 0 and p > 0) or (streak <= 0 and p < 0):
                streak += (1 if p > 0 else -1)
            else:
                break
        streak_text = f"{'+' if streak > 0 else ''}{streak} seguidos" if streak != 0 else "—"

        self._set_kpi(self._dash_wr, f"{wr:.1f}% ({len(wins)}/{total})" if total else "—",
                      "#00ff66" if wr >= 50 else "#F87171")
        self._set_kpi(self._dash_pf, f"{pf:.2f}" if pf and pf != float("inf") else "∞" if pf == float("inf") else "—",
                      "#00ff66" if pf >= 1.5 else "#ffcc00" if pf >= 1 else "#F87171")
        self._set_kpi(self._dash_avg_rr, f"1:{rr_avg:.2f}" if rr_avg else "—", "#DEFF9A")
        self._set_kpi(self._dash_max_dd, f"{max_dd:.1f}%", "#F87171" if max_dd > 10 else "#ffcc00")
        self._set_kpi(self._dash_streak, streak_text,
                      "#00ff66" if streak > 0 else "#F87171" if streak < 0 else "#888")

        # ── System KPIs (from entry_stats) ──
        today_key = today_str.replace("-", "")
        stats_today = self._entry_stats.get(today_key, {})
        evaluated = stats_today.get("evaluadas", 0)
        executed = stats_today.get("ejecutadas", 0)
        aborted = stats_today.get("abortadas", 0)
        filters = stats_today.get("filtros", {})
        top_filter = max(filters, key=filters.get) if filters else "—"
        top_count = filters.get(top_filter, 0) if top_filter != "—" else 0

        self._set_kpi(self._dash_eval, str(evaluated), "#DEFF9A")
        self._set_kpi(self._dash_exec, str(executed), "#00ff66")
        self._set_kpi(self._dash_abort, str(aborted), "#F87171")
        self._set_kpi(self._dash_top_filter,
                      f"{top_filter} ({top_count})" if top_filter != "—" else "—", "#ffcc00")
        cooldown = "Sí" if self._executor and getattr(self._executor, '_has_open_position', False) else "No"
        self._set_kpi(self._dash_cooldown, cooldown, "#F87171" if cooldown == "Sí" else "#00ff66")

    def _set_kpi(self, label, text, color):
        if label is not None:
            label.setText(str(text))
            label.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold; background: transparent;")

    # ═══════════════════════════════════════════════════════════════════════
    # Sub-tab 3: 📈 Curva de Capital
    # ═══════════════════════════════════════════════════════════════════════

    def _build_curve_tab(self):
        tab = QWidget()
        tab.setStyleSheet("background: #000;")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(4, 4, 4, 4)

        self._curve_fig = Figure(figsize=(8, 4), dpi=100, facecolor="#050505")
        self._curve_ax = self._curve_fig.add_subplot(111)
        self._curve_ax.set_facecolor("#050505")
        self._curve_canvas = FigureCanvasQTAgg(self._curve_fig)
        self._curve_canvas.setStyleSheet("border: 1px solid #222; border-radius: 4px;")
        lay.addWidget(self._curve_canvas)

        self._tab_widget.addTab(tab, "📈 Curva de Capital")

    def _redraw_curve(self):
        self._curve_ax.clear()
        self._curve_ax.set_facecolor("#050505")
        trades = self._trades
        n = len(trades)
        if n == 0:
            self._curve_ax.text(0.5, 0.5, "Sin operaciones cerradas",
                                transform=self._curve_ax.transAxes, ha="center", va="center",
                                color="#555", fontsize=12)
            self._curve_ax.set_title("Curva de Capital — 0 operaciones",
                                     color="#888", fontsize=10)
            self._curve_canvas.draw()
            return

        pnls = [t.get("pnl_usdt", 0) for t in trades]
        cumulative = [sum(pnls[:i+1]) for i in range(n)]
        ini_bal = trades[0].get("margen", 0) or 0
        balance_curve = [ini_bal + c for c in cumulative]

        xs = list(range(1, n + 1))

        # Main curve
        self._curve_ax.plot(xs, balance_curve, color="#4488ff", linewidth=1.5, label="Balance")

        # Reference line (initial balance)
        self._curve_ax.axhline(y=ini_bal, color="#555", linestyle="--", linewidth=0.8, label=f"Balance Inicial (${ini_bal:.0f})")

        # Green/red dots
        for i, p in enumerate(pnls):
            color = "#00ff66" if p >= 0 else "#ff4444"
            self._curve_ax.scatter(xs[i], balance_curve[i], c=color, s=20, zorder=5)

        # Max drawdown line
        running_max = -float("inf")
        max_dd_pct = 0.0
        max_dd_end = 0
        cumulative_val = 0.0
        for i, p in enumerate(pnls):
            cumulative_val += p
            bal = ini_bal + cumulative_val
            if bal > running_max:
                running_max = bal
            dd = (running_max - bal) / max(running_max, 1) * 100
            if dd > max_dd_pct:
                max_dd_pct = dd
                max_dd_end = bal
        if max_dd_pct > 0:
            self._curve_ax.axhline(y=max_dd_end, color="#ff8800", linestyle=":", linewidth=0.8,
                                   label=f"Max DD ({max_dd_pct:.1f}%)")

        self._curve_ax.set_title(
            f"Curva de Capital — {n} operaciones | "
            f"Balance: ${balance_curve[-1]:.2f} | "
            f"Max DD: {max_dd_pct:.1f}%",
            color="#888", fontsize=10)
        self._curve_ax.set_xlabel("Operación #", color="#555", fontsize=9)
        self._curve_ax.set_ylabel("Balance (USDT)", color="#555", fontsize=9)
        self._curve_ax.tick_params(colors="#555", labelsize=8)
        self._curve_ax.grid(True, alpha=0.1, color="#333")
        self._curve_ax.legend(loc="upper left", fontsize=8, facecolor="#0a0a0a", edgecolor="#333",
                             labelcolor="#aaa")
        self._curve_fig.tight_layout()
        self._curve_canvas.draw()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _update_env_display(self):
        env_tag = "REAL"
        env_color = "#ff4444"
        bal_text = f"Balance: <b>${self._balance:,.2f}</b>" if self._balance > 0 else "Balance: <b>—</b>"
        avail_text = f"Disponible: <b>${self._available:,.2f}</b>" if self._available > 0 else "Disponible: <b>—</b>"
        self._bal_label.setText(
            f"<span style='color:{env_color}'>{env_tag}</span>  │  "
            f"{bal_text}  │  {avail_text}")
        pnl_color = "#00ff66" if self._unrealized_pnl >= 0 else "#bb00ff"
        self._pnl_label.setText(
            f"PnL: <span style='color:{pnl_color}'>"
            f"<b>${self._unrealized_pnl:+,.2f}</b></span>")

    # ── JSON persistence ──────────────────────────────────────────────────

    def _save_json(self):
        try:
            with open(self._trades_path, "w") as f:
                json.dump(self._trades, f, indent=2)
            with open(self._stats_path, "w") as f:
                json.dump(self._entry_stats, f, indent=2)
        except Exception as e:
            print(f"[F4] Error guardando JSON: {e}")

    def _load_json(self):
        try:
            if os.path.exists(self._trades_path):
                with open(self._trades_path) as f:
                    self._trades = json.load(f)
            if os.path.exists(self._stats_path):
                with open(self._stats_path) as f:
                    self._entry_stats = json.load(f)
        except Exception as e:
            print(f"[F4] Error cargando JSON: {e}")
            self._trades = []
            self._entry_stats = {}
        self._refresh_table()
        self._recalc_dashboard()
        self._redraw_curve()

    # ── Compatibility API (called from MainDashboard) ────────────────────

    def add_trade(self, symbol: str, direction: str, entry_price: float,
                  capital: float, leverage: int, pnl: float = 0,
                  delta: float = 0, cvd: float = 0, is_open: bool = True):
        self._pending_trade = {
            "fecha": datetime.now(timezone.utc).isoformat(),
            "par": symbol,
            "direccion": "BUY" if direction.upper() in ("LONG", "BUY", "ALZA") else "SELL",
            "precio_entrada": entry_price,
            "sl": 0,
            "tp": 0,
            "btc": 0,
            "margen": capital,
            "apalancamiento": leverage,
            "delta_entrada": delta,
            "cvd_entrada": cvd,
            "entry_stats": "",
        }

    def close_last_trade(self, final_pnl: float,
                         last_entry: float = 0, last_exit: float = 0):
        if self._pending_trade is None:
            return
        trade = dict(self._pending_trade)
        trade.update({
            "precio_salida": last_exit,
            "pnl_usdt": final_pnl,
            "roe_pct": (final_pnl / max(trade.get("margen", 1), 1)) * 100,
            "rr_real": 0,
            "duracion_segundos": 0,
            "cierre": "TP" if final_pnl > 0 else "SL" if final_pnl < 0 else "MANUAL",
        })
        self._trades.append(trade)
        self._pending_trade = None
        self._save_json()
        self._refresh_table()
        self._recalc_dashboard()
        self._redraw_curve()

    def update_live_data(self, balance: float, available: float,
                         pnl: float, env_type: str = "REAL",
                         current_price: float = 0,
                         technical_levels: dict = None):
        self._current_price = current_price
        self._balance = balance
        self._available = available
        self._unrealized_pnl = pnl
        self._update_env_display()

    def refresh_entry_stats(self, stats: dict):
        """Update entry_stats from SignalMonitorTab."""
        today_key = datetime.now(timezone.utc).strftime("%Y%m%d")
        if today_key not in self._entry_stats:
            self._entry_stats[today_key] = {"evaluadas": 0, "ejecutadas": 0, "abortadas": 0, "filtros": {}}
        daily = self._entry_stats[today_key]

        # Count total filters and classify
        for reason, count in stats.items():
            if reason.startswith("abortado_") or reason.endswith("_FAILED") or reason.endswith("_ABORT"):
                daily["abortadas"] += count
                daily["filtros"][reason] = daily["filtros"].get(reason, 0) + count
            else:
                daily["evaluadas"] += count

        self._save_json()
        self._recalc_dashboard()
class MainDashboard(QMainWindow):

    def __init__(self):
        super().__init__()
        self.data = {}
        self.running = True
        self._refresh_busy = False
        self.grid_labels = {}
        self.bottom_widgets = []

        # Set leverage once at startup, never in the hot loop
        try:
            client.futures_change_leverage(symbol=settings.get_symbol(), leverage=40)
        except Exception:
            pass

        # Window flags: hide title bar, keep minimize/maximize buttons
        self.setWindowFlags(
            Qt.Window | Qt.CustomizeWindowHint
            | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint
        )

        self.init_ui()
        self.init_data()

        # Crear un único cliente Binance compartido — REAL production only
        from binance.client import Client
        _shared_api_key = settings.BINANCE_REAL_API_KEY
        _shared_secret = settings.BINANCE_REAL_SECRET_KEY
        self._shared_client = Client(
            _shared_api_key,
            _shared_secret,
            testnet=False,
            ping=False,
        )

        # Wire up OrderExecutor FIRST (valida credenciales antes de arrancar nada)
        self.order_executor = OrderExecutor(client=self._shared_client)
        self.order_executor.order_result.connect(self._on_order_result)
        self.risk_tab.set_executor(self.order_executor)
        self.signal_tab.set_executor(self.order_executor, self.risk_tab._f4worker)

        self.start_update_thread()

        # TelegramBot recibe el executor en el constructor (singleton forzado)
        self.telegram_bot = TelegramBot(order_executor=self.order_executor)
        self.telegram_bot.start()

        # Wire up data engine to TelegramBot for /symbol hot-swap
        if hasattr(self, '_async_engine') and self._async_engine:
            self.telegram_bot.set_data_engine(self._async_engine)

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
                      f"(modelo=gemini-2.5-flash)")
            else:
                print(f"[⚠️ GEMINI BRAIN] GeminiBrainManager deshabilitado — "
                      f"sin GEMINI_API_KEY")
        except Exception as e:
            print(f"[⚠️ GEMINI BRAIN] Error inicializando: {e}")
            self.gemini_brain = None

        # Wire AI references into SignalMonitorTab
        self.signal_tab.set_ai_references(
            gemini_brain=getattr(self, 'gemini_brain', None),
            brain_agent=getattr(self, 'brain_agent', None),
        )

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

        # ── HFT Speed tracking (large trades/sec) ──────────────────────
        self._hft_trades: deque = deque(maxlen=300)
        self._hft_speed: float = 0.0
        self._hft_threshold_btc: float = 0.5

        # ── Liquidation events ring buffer ─────────────────────────────
        self._liquidation_events: deque = deque(maxlen=100)

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

        # ── Single-position tracking ───────────────────────────────────
        self._last_pos_track_time: float = 0.0
        self._was_position_open: bool = False
        self._last_pos_pnl: float = 0.0
        self._last_pos_entry_price: float = 0.0
        self._last_pos_exit_price: float = 0.0
    
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
        
        self.header_label = QLabel(f"{settings.get_symbol()} PRO MODE - ORDER FLOW")
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
        
        # ─── TAB 2: SIGNAL MONITOR ───
        self.signal_tab = SignalMonitorTab()
        self.tabs.addTab(self.signal_tab, "📡 SIGNAL (F2)")
        
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

        # ── TAB 4: RISK MANAGEMENT ───────────────────────────────────────
        tab4 = QWidget()
        tab4_layout = QVBoxLayout(tab4)
        tab4_layout.setContentsMargins(0, 0, 0, 0)
        self.risk_tab = RiskManagementTab()
        tab4_layout.addWidget(self.risk_tab)
        tab4.setLayout(tab4_layout)
        self.tabs.addTab(tab4, "\U0001f6a8 RIESGO (F4)")
        QShortcut(QKeySequence("F4"), self).activated.connect(
            lambda: self.tabs.setCurrentIndex(3))

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
        qsettings = QSettings("BB-450", "Dashboard")
        geo = qsettings.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        """Save window geometry and perform clean shutdown."""
        self.running = False
        qsettings = QSettings("BB-450", "Dashboard")
        qsettings.setValue("window_geometry", self.saveGeometry())
        self.close_application_cleanly()
        event.accept()

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

        # Position tracking timer (must be created here, independent of klines)
        self._pos_timer = QTimer()
        self._pos_timer.timeout.connect(self._check_position_tracking)
        self._pos_timer.start(5000)
        
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
                "t_1m": "WAIT", "t_5m": "WAIT", "t_15m": "WAIT", "t_1h": "WAIT", "t_4h": "WAIT", "t_1d": "WAIT",
                "rsi_5m": 0.0, "rsi_15m": 0.0, "rsi_1h": 0.0, "rsi_4h": 0.0, "rsi_1d": 0.0,
                "macd_15m": 0.0, "macd_1h": 0.0, "macd_4h": 0.0, "macd_1d": 0.0,
                "global_macro": "NEUTRAL",
                "ema_cross_5m": "NEUTRAL", "ema_cross_15m": "NEUTRAL", "ema_cross_1h": "NEUTRAL",
                "ema_cross_4h": "NEUTRAL", "ema_cross_1d": "NEUTRAL",
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
                "ai_signal": "NINGUNA", "win_rate": 0.0, "latency": 0,
                "gemini_regimen": "", "gemini_sl": 0.0, "gemini_tp": 0.0, "final_prediction": "WAIT",
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

        # ── Delta-CatchUp: retroactive kline sync ─────────────────────────
        try:
            from src.engine.delta_catchup import run_catch_up
            client = getattr(self, '_shared_client', None)
            if client is not None:
                n = run_catch_up(client, trading_strategy)
                if n > 0:
                    print(f"[Δ CATCH-UP] {n} klines cargados retroactivamente")
            else:
                print("[Δ CATCH-UP] Cliente Binance no disponible — saltando")
        except Exception as e:
            print(f"[Δ CATCH-UP] Error: {e}")

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
            ticker = client.futures_symbol_ticker(symbol=settings.get_symbol())
            return float(ticker['price'])
        except:
            return self.data['price']
    
    def get_klines(self):
        try:
            return client.futures_klines(symbol=settings.get_symbol(), interval="1m", limit=200)
        except:
            return []
    
    def get_trades(self):
        try:
            return client.futures_aggregate_trades(symbol=settings.get_symbol(), limit=50)
        except:
            return []
    
    def get_order_book(self):
        try:
            return client.futures_order_book(symbol=settings.get_symbol(), limit=20)
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
            positions = client.futures_position_information(symbol=settings.get_symbol())
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
        
        self.data['ema_9'] = self.calculate_ema(closes, 9) if len(closes) >= 9 else closes[-1] if closes else 0
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

            # ── Copy technical levels (Fibonacci, S/R, confluence) ──
            tl = ms.get('technical_levels')
            if tl:
                self.data['technical_levels'] = tl

            # ── Copy order flow (pre-computed by AsyncDataEngine) ──
            self.data['delta'] = of.get('delta', self.data.get('delta', 0))
            self.data['cvd'] = of.get('cvd', self.data.get('cvd', 0))
            self.data['buy_volume'] = of.get('buy_volume', self.data.get('buy_volume', 0))
            self.data['sell_volume'] = of.get('sell_volume', self.data.get('sell_volume', 0))
            self.data['window_buy_volume'] = of.get('window_buy_volume', of.get('buy_volume', self.data.get('window_buy_volume', 0)))
            self.data['window_sell_volume'] = of.get('window_sell_volume', of.get('sell_volume', self.data.get('window_sell_volume', 0)))

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

            # ── Heartbeat log cada 5 minutos ──
            if self.stats['update_count'] > 0 and self.stats['update_count'] % 300 == 0:
                uptime_str = f"{self.stats['uptime_seconds'] // 3600}h{(self.stats['uptime_seconds'] % 3600) // 60}m"
                print(f"💓 [HEARTBEAT] BB-450 GUI activo | "
                      f"updates={self.stats['update_count']} | "
                      f"uptime={uptime_str} | "
                      f"price=${self.data['price']:,.0f} | "
                      f"latency={self.stats['latency_ms']}ms")

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
                bv = self.data.get('window_buy_volume', self.data.get('buy_volume', 0))
                sv = self.data.get('window_sell_volume', self.data.get('sell_volume', 0))
                total_vol = bv + sv
                ld = self.data.get('liquidity_data', {})
                cl = self.data.get('klines', [])
                closes = [float(k[4]) for k in cl[-20:]] if cl else []
                snapshot = {
                    # Precio y cambio
                    'symbol': settings.get_symbol(),
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
                    'trend_label': self.battle_bar.trend_label if hasattr(self, 'battle_bar') else '',
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
                    'trend_1d': mtf.get('t_1d', 'WAIT'),
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

                    # ── Debug: _compute_signal component scores ──────────
                    'debug_vol_pct': getattr(self.battle_bar, 'debug_vol_pct', None),
                    'debug_ob_pct': getattr(self.battle_bar, 'debug_ob_pct', None),
                    'debug_cvd_pct': getattr(self.battle_bar, 'debug_cvd_pct', None),
                    'debug_delta_pct': getattr(self.battle_bar, 'debug_delta_pct', None),
                    'debug_micro_pct': getattr(self.battle_bar, 'debug_micro_pct', None),
                    'debug_composite': getattr(self.battle_bar, 'debug_composite', None),
                    'debug_threshold': getattr(self.battle_bar, 'debug_threshold', None),
                    'debug_cvd_raw': getattr(self.battle_bar, 'debug_cvd_raw', None),
                    'debug_delta_raw': getattr(self.battle_bar, 'debug_delta_raw', None),
                    'debug_cvd_relativo': getattr(self.battle_bar, 'debug_cvd_relativo', None),
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
                            async def _retrain():
                                try:
                                    await self.brain_agent.background_retrain()
                                except Exception:
                                    pass
                            threading.Thread(
                                target=lambda: asyncio.run(_retrain()),
                                daemon=True,
                                name="BrainRetrain",
                            ).start()
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
                                async def _journal():
                                    try:
                                        await self.gemini_brain.journal_event(
                                            before, journal_snap,
                                            brain_direction=before.get('brain_direction'),
                                            brain_confidence=before.get('brain_confidence_pct', 0),
                                        )
                                    except Exception:
                                        pass
                                threading.Thread(
                                    target=lambda: asyncio.run(_journal()),
                                    daemon=True,
                                    name="BrainJournal",
                                ).start()
                    except Exception:
                        pass
        finally:
            self._refresh_busy = False

    def _check_position_tracking(self):
        """Check open position status and update risk tab live data."""
        executor = getattr(self, 'order_executor', None)
        if executor is None:
            return
        now = time.time()

        try:
            pos = executor.check_position_status()
        except Exception:
            return

        if pos is None:
            was_open = self._was_position_open
            if was_open:
                self._last_pos_track_time = 0
                self._was_position_open = False
                # Record close PnL in journal
                final_pnl = getattr(self, '_last_pos_pnl', 0)
                entry_p = getattr(self, '_last_pos_entry_price', 0)
                exit_p = getattr(self, '_last_pos_exit_price', 0)
                rt = getattr(self, 'risk_tab', None)
                if rt is not None:
                    rt.close_last_trade(final_pnl, entry_p, exit_p)
                try:
                    if hasattr(self, 'telegram_bot') and self.telegram_bot is not None:
                        self.telegram_bot._queue.put_nowait({
                            "type": "position_tracking",
                            "text": "<b>⚠️ [BB-450 LIVE OPERATIONS]</b>\n"
                                    "<b>✅ POSICIÓN CERRADA</b>\n\n"
                                    "La operación ha finalizado. "
                                    "El bot está listo para la próxima señal.",
                        })
                except Exception:
                    pass

        else:
            self._was_position_open = True
            last_track = getattr(self, '_last_pos_track_time', 0)
            if now - last_track >= 120.0:
                self._last_pos_track_time = now
                try:
                    direction = "LONG" if float(pos.get("positionAmt", 0)) > 0 else "SHORT"
                    entry = float(pos.get("entryPrice", 0))
                    mark = float(pos.get("markPrice", 0))
                    upnl = float(pos.get("unRealizedProfit", 0))
                    liq = float(pos.get("liquidationPrice", 0))
                    leverage = int(float(pos.get("leverage", 0)))
                    pnl_pct = (mark - entry) / entry * 100 * leverage if entry > 0 else 0

                    # Store PnL + prices for journal close (when position closes next cycle)
                    self._last_pos_pnl = upnl
                    self._last_pos_entry_price = entry
                    self._last_pos_exit_price = mark

                    text = (
                        f"<b>⚠️ [BB-450 LIVE OPERATIONS]</b>\n"
                        f"<b>📊 SEGUIMIENTO DE POSICIÓN</b>\n"
                        f"<b>Dirección:</b> {direction}\n"
                        f"<b>Entrada:</b> <code>${entry:,.2f}</code>\n"
                        f"<b>Mark:</b> <code>${mark:,.2f}</code>\n"
                        f"<b>PnL:</b> <code>${upnl:+.2f}</code> ({pnl_pct:+.2f}%)\n"
                        f"<b>Apalancamiento:</b> {leverage}x\n"
                        f"<b>Liquidación:</b> <code>${liq:,.2f}</code>"
                    )

                    if hasattr(self, 'telegram_bot') and self.telegram_bot is not None:
                        self.telegram_bot._queue.put_nowait({
                            "type": "position_tracking",
                            "text": text,
                        })
                except Exception:
                    pass

        # ── Always update risk tab live data (every 5s, regardless of position) ──
        try:
            bal = executor.get_balance()
            if bal.get("success"):
                b = bal["balance"]
                a = bal.get("available", 0)
                u = bal.get("unrealized_pnl", 0)
                env = "REAL"
                price = float(self.data.get("price", 0))
                rt = getattr(self, 'risk_tab', None)
                if rt is not None:
                    tech = self.data.get("technical_levels", {})
                    rt.update_live_data(b, a, u, env, price, tech)
        except Exception:
            pass

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

                # ── Trade signal routed through Telegram inline confirmation ─
                #   El ejecutor no corre aquí. La señal se envía al canal de
                #   Telegram con botón inline "Autorizar Orden a Mercado" y
                #   solo se ejecuta cuando el usuario pulsa ese botón.
                #   Esto evita que se abran posiciones sin supervisión.

        except Exception as e:
            print(f"[⚠️ BRAIN] Error en _on_inference_finished: {e}")

    def _on_order_result(self, success: bool, message: str, data: dict):
        direction = data.get("direction", "?")
        entry = data.get("entry_price", 0)
        qty = data.get("entry_qty", 0)
        sl = data.get("sl_price", 0)
        tp = data.get("tp_price", 0)
        entry_id = data.get("entry_order_id", "")
        sl_id = data.get("sl_order_id", "")
        tp_id = data.get("tp_order_id", "")

        # ── Anti-falso-positivo: solo cuando el executor ya reportó fallo ─
        #   Si el order executor dice success=True, confiamos en que
        #   Binance confirmó la orden; cualquier error posterior (ej. -4067
        #   en la creación de brackets SL/TP) se reporta aparte, sin
        #   falsear el resultado de la entrada.
        entry_id_valid = bool(entry_id) and entry_id != "None"
        entry_price_valid = entry > 0
        bracket_errs = data.get("bracket_errors", "")
        error_detail = ""
        if not success and (not entry_id_valid or not entry_price_valid):
            api_error = data.get("error", "")
            if api_error:
                error_detail = api_error
            else:
                error_detail = "Credenciales inválidas al ejecutar orden"
        elif bracket_errs:
            error_detail = f"⚠ Bracket: {bracket_errs}"

        status_icon = "✅" if success else "❌"
        log_msg = (
            f"[REAL] {status_icon} {direction} "
            f"{qty} BTC @ ${entry:,.0f} "
            f"SL=${sl:,.0f} TP=${tp:,.0f} "
            f"entry={entry_id} sl={sl_id} tp={tp_id}"
        )
        print(log_msg)

        # ── Journal trade on successful execution ───────────────────────
        if success:
            lev = data.get("leverage", 100)
            cap = data.get("capital", 100)
            ms = getattr(self, 'market_state', {})
            of = ms.get("order_flow", {})
            delta_entry = of.get("delta", 0)
            cvd_entry = of.get("cvd", 0)
            rt = getattr(self, 'risk_tab', None)
            if rt is not None:
                sym = settings.get_symbol()
                dir_j = "LONG" if direction.upper() == "BUY" or direction.upper() == "ALZA" else "SHORT"
                rt.add_trade(sym, dir_j, entry, cap, lev, 0, delta_entry, cvd_entry, is_open=True)

        # ── Telegram alert: institutional format ─────────────────────────
        bracket_info = f" ⚠ {bracket_errs}" if bracket_errs else ""
        trailing_info = " 🔄 TRAILING ACTIVO" if data.get("trailing_active") else ""
        dyn_lev = data.get("dynamic_leverage", 0)
        dyn_conf = data.get("confidence", -1.0)
        leverage_info = ""
        if dyn_lev > 0 and dyn_conf >= 0:
            leverage_info = (
                f"\n⚡ Apalancamiento Ajustado Dinámicamente: "
                f"{dyn_lev}x (Basado en {dyn_conf:.0f}% de certeza)")
        telegram_text = (
            f"<b>⚠️ [BB-450 LIVE OPERATIONS]</b>\n"
            f"<b>{status_icon} ORDEN EJECUTADA</b>\n"
            f"<b>Dirección:</b> {direction}\n"
            f"<b>Cantidad:</b> <code>{qty:.4f} BTC</code>\n"
            f"<b>Entrada:</b> <code>${entry:,.2f}</code>\n"
            f"<b>Stop Loss:</b> <code>${sl:,.2f}</code>\n"
            f"<b>Take Profit:</b> <code>${tp:,.2f}</code>\n"
            f"<b>ID Entrada:</b> <code>{entry_id or '—'}</code>\n"
            f"<b>ID SL:</b> <code>{sl_id or '—'}</code>\n"
            f"<b>ID TP:</b> <code>{tp_id or '—'}</code>"
            f"{leverage_info}{bracket_info}{trailing_info}"
        )
        if not success and error_detail:
            telegram_text = (
                f"<b>🚨 OPERACIÓN FALLIDA</b>\n"
                f"{error_detail}")

        # ── Update SignalMonitorTab leverage label in real-time ─────────
        result_lev = data.get("leverage", 0)
        if result_lev > 0:
            st = getattr(self, 'signal_tab', None)
            if st is not None:
                st._leverage = result_lev
                st._update_account_display()

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
            g_conf = gem.confianza
            g_reason = gem.analisis_cuant
            g_regimen = gem.regimen_mercado
            g_sl = gem.stop_loss
            g_tp = gem.take_profit
            g_trigger = gem.trigger_price
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
                f'<span style="color:{g_color};">{g_conf:.1f}%</span>')
            lines.append(
                f'  <b>Régimen:</b> {g_regimen}')
            lines.append(
                f'  <b>SL:</b> ${g_sl:,.0f}  <b>TP:</b> ${g_tp:,.0f}  '
                f'<b>Trigger:</b> ${g_trigger:,.0f}')
            if g_reason:
                lines.append(
                    f'  <b>Análisis Cuant:</b>')
                lines.append(
                    f'    <span style="color:#CCC;">{g_reason}</span>')
            lines.append('')

        lines.append('</pre>')
        html = '\n'.join(lines)
        self.ai_engines_monitor.setHtml(html)

    def _on_gemini_finished(self, decision):
        """Handle GeminiInferenceWorker result — runs in main thread."""
        if decision is None:
            return
        try:
            self._last_gemini_decision = decision

            # ── Inject into market_state for the panel render loop ────
            ai = self.market_state.setdefault("ai_engine", {})
            ai["gemini_decision"] = decision.decision
            ai["gemini_confidence"] = decision.confianza
            ai["gemini_regimen"] = decision.regimen_mercado
            ai["gemini_analisis"] = decision.analisis_cuant
            ai["gemini_sl"] = decision.stop_loss
            ai["gemini_tp"] = decision.take_profit
            ai["gemini_trigger"] = decision.trigger_price

            # ── Push Gemini bracket to the bracket widget ─────────────
            gemini_risk = {
                "status": decision.decision,
                "trigger": decision.trigger_price,
                "sl": decision.stop_loss,
                "tp1": decision.take_profit,
                "tp2": 0,
                "lot_size": 0.0,
            }
            if hasattr(self, 'bracket_widget'):
                price = self.data.get('price', 0)
                self.bracket_widget.update_data(
                    ai.get("risk_panel", {"status": "WAITING", "trigger": 0,
                                          "sl": 0, "tp1": 0, "tp2": 0,
                                          "lot_size": 0}),
                    decision.confianza,
                    price,
                    brain_bracket=gemini_risk,
                )

            # ── Push to dual-engine monitor ──
            self._update_ai_monitor(
                gemini_decision=decision,
                brain_decision=getattr(self, '_last_brain_decision', None),
            )

            # ── Push update to Telegram for downstream dispatch ──────
            snap = getattr(self, '_pending_brain_snapshot', None)
            if snap is not None:
                snap['brain_direction'] = decision.decision
                snap['brain_confidence_pct'] = decision.confianza
                snap['brain_market_rationale'] = decision.analisis_cuant
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
        self.header_label.setText(f"\u25c8 {settings.get_symbol()} ${self.format_number(self.data['price'])}")
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
        
        # ── Push market context to OrderExecutor (trailing/MA filter) ──
        executor = getattr(self, 'order_executor', None)
        if executor is not None:
            executor.update_market_context(
                price=self.data.get("price", 0),
                atr=self.data.get("atr", 0))

        signal_tab = getattr(self, 'signal_tab', None)

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

        # ── Klines candle data (current tick) ─────────────────────────
        klines = self.data.get('klines', [])
        if klines:
            o = float(klines[-1][1])
            h = float(klines[-1][2])
            l = float(klines[-1][3])
            close_ = float(klines[-1][4])
            real_range = h - l
            if real_range > 0:
                upper_wick_pct = (h - max(o, close_)) / real_range
                lower_wick_pct = (min(o, close_) - l) / real_range
            else:
                upper_wick_pct = 0.0
                lower_wick_pct = 0.0
            # Consecutive red bars (closes < opens, last 5 candles)
            consecutive_red_bars = 0
            for i in range(-1, -6, -1):
                if abs(i) <= len(klines):
                    c = float(klines[i][4])
                    op = float(klines[i][1])
                    if c < op:
                        consecutive_red_bars += 1
                    else:
                        break
        else:
            o = h = l = 0.0
            upper_wick_pct = lower_wick_pct = 0.0
            consecutive_red_bars = 0

        # ── Critical support (nearest 1D support) ─────────────────────
        tech = self.data.get("technical_levels", {})
        if tech:
            critical_support = tech.get("nearest_support", 0.0) or 0.0
        else:
            critical_support = self.data.get("day_low", 0.0) or 0.0

        # MTF data from async engine
        mt = self.market_state.get("mtf_trend", {})
        c_score = mt.get("confluence_score", 50)
        t_1h = mt.get("t_1h", "NEUTRAL")
        t_4h = mt.get("t_4h", "NEUTRAL")
        t_1d = mt.get("t_1d", "NEUTRAL")
        t_5m = mt.get("t_5m", "NEUTRAL")
        t_15m = mt.get("t_15m", "NEUTRAL")

        # Microstructure / HFT data from async engine
        mom = self.market_state.get('momentum', {})
        lq = self.market_state.get('liquidity', {})
        m_tick = mom.get('tick_speed', 0)
        m_cancel = mom.get('cancel_rate', 0)
        m_pinam = mom.get('pinam', 0)
        m_spread = mom.get('spread_velocity', 0)

        # ── HFT Speed (large trades/sec) ──────────────────────────────
        hft_threshold = getattr(self, '_hft_threshold_btc', 0.5)
        ts_now = time.time()
        trades = self.market_state.get('trades', [])
        for t in trades:
            qty = float(t.get('q', 0))
            if qty >= hft_threshold:
                self._hft_trades.append((ts_now, qty))
        self._hft_trades = deque(
            [x for x in self._hft_trades if ts_now - x[0] < 60],
            maxlen=300
        )
        hft_speed = sum(q for _, q in self._hft_trades) / max(ts_now - (self._hft_trades[0][0] if self._hft_trades else ts_now), 1)

        # ── Spoofing Risk ─────────────────────────────────────────────
        cancel_rate_val = mom.get('cancel_rate', 0.0)
        pinam_val = mom.get('pinam', 0.0)
        ob = self.data.get('order_book', {})
        bids_ob = sorted(ob.get('bids', []), key=lambda x: float(x[0]), reverse=True) if ob else []
        asks_ob = sorted(ob.get('asks', []), key=lambda x: float(x[0])) if ob else []
        top_bid_vol = float(bids_ob[0][1]) if bids_ob else 0
        top_ask_vol = float(asks_ob[0][1]) if asks_ob else 0
        total_bid_vol = sum(float(b[1]) for b in bids_ob[:10]) if bids_ob else 0.001
        total_ask_vol = sum(float(a[1]) for a in asks_ob[:10]) if asks_ob else 0.001
        top_bid_ratio = top_bid_vol / total_bid_vol
        top_ask_ratio = top_ask_vol / total_ask_vol
        wall_top_heavy = max(top_bid_ratio, top_ask_ratio)
        spoofing_risk = min(100, (
            (cancel_rate_val / 100) * 40 +
            (pinam_val / 100) * 30 +
            max(0, wall_top_heavy - 0.4) * 150
        ))

        # ── B/A ratio (absorption detection) ──────────────────────────
        ba_ratio = total_bid_vol / max(total_ask_vol, 0.001)

        # Volatility data
        b_squeeze = self.data.get('bb_squeeze', 'NORMAL')
        atr_val = self.data.get('atr', 0)
        avg_vol = self.data.get('avg_volume', 0)

        # ── Relative Volume (current vs avg) ──────────────────────────
        relative_volume = (self.data.get('buy_volume', 0) + self.data.get('sell_volume', 0)) / max(avg_vol, 0.001)

        # ── Depth imbalance ───────────────────────────────────────────
        depth_imb_pct = lq.get('depth_imbalance', 0.0)

        # ── Active trap from narrative panel ──────────────────────────
        active_trap = ""
        narrative_panel = self.panels.get('NARRATIVE')
        if narrative_panel and hasattr(narrative_panel, 'get_current_alert'):
            alert_text = narrative_panel.get_current_alert()
            if alert_text and "TRAMPA" in alert_text.upper():
                active_trap = alert_text

        # ── Push signal data to SignalMonitorTab (F2) ──────────────────
        if signal_tab is not None:
            signal_tab.update_signal_data({
                "direction": self.battle_bar.trend_direction,
                "confidence": self.battle_bar.confidence,
                "trend_label": self.battle_bar.trend_label,
                "price": self.data.get("price", 0),
                "change_pct": self.data.get("price_change_pct", 0),
                "rsi": self.data.get("rsi", 50),
                "macd": self.data.get("macd", 0),
                "macd_signal": self.data.get("macd_signal", 0),
                "macd_hist": self.data.get("macd_hist", 0),
                "bb_upper": self.data.get("bb_upper", 0),
                "bb_middle": self.data.get("bb_middle", 0),
                "bb_lower": self.data.get("bb_lower", 0),
                "atr": self.data.get("atr", 0),
                "delta": self.data.get("delta", 0),
                "cvd": self.data.get("cvd", 0),
                "buy_volume": self.data.get("buy_volume", 0),
                "sell_volume": self.data.get("sell_volume", 0),
                "imbalance": self.data.get("liquidity_data", {}).get("imbalance", 0),
                "technical_levels": self.data.get("technical_levels", {}),
                "mtf_trend": self.market_state.get("mtf_trend", {}),
                "signal": self.data.get("signal", "NINGUNA"),
                "ema_9": self.data.get("ema_9", 0),
                "ema_20": self.data.get("ema_20", 0),
                "wall_bid": self.data.get("liquidity_data", {}).get("wall_bid_1", 0),
                "wall_ask": self.data.get("liquidity_data", {}).get("wall_ask_1", 0),
                "bounce_sl": 0.0,
                "spoofing_risk": spoofing_risk,
                "hft_speed": hft_speed,
                "active_trap": active_trap,
                "depth_imb_pct": depth_imb_pct,
                "cancel_rate": m_cancel,
                "multiplicador_posicion": getattr(self.battle_bar, 'multiplicador_posicion', 1.0),
                "decision": getattr(self.battle_bar, 'decision', "ESPERAR"),
                "regimen_mercado": getattr(self.battle_bar, 'regimen_mercado', ""),
                "analisis_cuant": getattr(self.battle_bar, 'analisis_cuant', ""),
                "liquidity_magnet": getattr(self.battle_bar, 'liquidity_magnet', "NONE"),
                "provisional_tp": getattr(self.battle_bar, 'provisional_tp', 0.0),
                "magnet_price": getattr(self.battle_bar, 'magnet_price', 0.0),
                "book_depth_bids_volume": getattr(self.battle_bar, '_book_depth_bids_volume', 0.0),
                "book_depth_asks_volume": getattr(self.battle_bar, '_book_depth_asks_volume', 0.0),
                "funding_rate": self.data.get("funding_rate", 0.0),
                "oi_delta_5min": self.data.get("oi_delta_5min", 0.0),
                "magnet_timestamp": getattr(self.battle_bar, '_magnet_timestamp', 0.0),
                "magnet_price_at_set": getattr(self.battle_bar, '_magnet_price_at_set', 0.0),
                "tick_integrity_score": getattr(self.battle_bar, '_tick_integrity_score', 1.0),
                # ── Mejora 3: imbalance window ──────────────────────
                "imbalance_detected_at": getattr(self.battle_bar, 'imbalance_detected_at', 0.0),
                "imbalance_direction": getattr(self.battle_bar, 'imbalance_direction', 0),
                # ── Mejora 2: whale walls para absorción ────────────
                "whale_bid_walls": self.data.get('liquidity_data', {}).get('buy_walls', []),
                "whale_ask_walls": self.data.get('liquidity_data', {}).get('sell_walls', []),
                "signal_ts": datetime.utcnow(),
            })

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
            trend_1d=t_1d,
            trend_5m=t_5m,
            trend_15m=t_15m,
            delta=self.data['delta'],
            tick_speed=m_tick,
            cancel_rate=m_cancel,
            pinam=m_pinam,
            bb_squeeze=b_squeeze,
            atr=atr_val,
            spread_velocity=m_spread,
            avg_volume=avg_vol,
            volatility_explosion=mom.get('volatility_explosion', False),
            price=self.data.get('price', 0),
            bb_upper=self.data.get('bb_upper', 0),
            bb_middle=self.data.get('bb_middle', 0),
            bb_lower=self.data.get('bb_lower', 0),
            macd_line=self.data.get('macd', 0),
            macd_signal_line=self.data.get('macd_signal', 0),
            macd_hist=self.data.get('macd_hist', 0),
            ema_20=self.data.get('ema_20', 0),
            ema_9=self.data.get('ema_9', 0),
            kaufman_eff=mom.get('kaufman_efficiency', 0.5),
            upper_wick_pct=upper_wick_pct,
            lower_wick_pct=lower_wick_pct,
            open_price=o,
            high_price=h,
            low_price=l,
            critical_support=critical_support,
            consecutive_red_bars=consecutive_red_bars,
            spoofing_risk=spoofing_risk,
            hft_speed=hft_speed,
            active_trap=active_trap,
            ba_ratio=ba_ratio,
            depth_imb_pct=depth_imb_pct,
            relative_volume=relative_volume,
            liquidity_pools={
                "pool_shorts_arriba": [ price * 1.10, price * 1.04, price * 1.02, price * 1.01 ],
                "pool_longs_abajo":  [ price * 0.90, price * 0.96, price * 0.98, price * 0.99 ],
            },
            whale_bid_walls=self.data.get('liquidity_data', {}).get('buy_walls', []),
            whale_ask_walls=self.data.get('liquidity_data', {}).get('sell_walls', []),
            book_depth_bids_volume=self.data.get('book_depth_bids_volume', 0.0),
            book_depth_asks_volume=self.data.get('book_depth_asks_volume', 0.0),
            funding_rate=self.data.get('funding_rate', 0.0),
            oi_delta_5min=self.data.get('oi_delta_5min', 0.0),
        )
            
        # ═══════════════════════════════════════════════════════════════
        # NARRATIVA INSTITUCIONAL
        # ═══════════════════════════════════════════════════════════════
        if 'NARRATIVE' in self.panels:
            self.panels['NARRATIVE'].update_narrative(self.data, self.data.get('order_book', {}))
            
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

        # Technical levels from market_state (Fibonacci + S/R)
        tech = self.data.get("technical_levels", {})
        if tech and tech.get("fib_retracement"):
            fb = tech["fib_retracement"]
            fib_618 = next((f for f in fb if f["ratio"] == 0.618), None)
            fib_382 = next((f for f in fb if f["ratio"] == 0.382), None)
            if fib_618:
                gv("FIB 0.618", f"${fib_618['price']:,.0f}", magenta)
            if fib_382:
                gv("FIB 0.382", f"${fib_382['price']:,.0f}", cyan)
            closest = None
            for f in fb:
                d = abs(f["price"] - price)
                if closest is None or d < closest[0]:
                    closest = (d, f)
            if closest:
                fl = closest[1]
                pct = (fl["price"] - price) / max(price, 1) * 100
                clr = up if pct > 0 else dn
                gv("FIB NEAREST", f"${fl['price']:,.0f} ({pct:+.2f}%)", clr)
        ns = tech.get("nearest_support") if tech else None
        nr = tech.get("nearest_resistance") if tech else None
        if ns:
            ns_pct = (ns["price"] - price) / max(price, 1) * 100
            gv("SR NEAR S", f"${ns['price']:,.0f} ({ns_pct:+.2f}%)", up)
        if nr:
            nr_pct = (nr["price"] - price) / max(price, 1) * 100
            gv("SR NEAR R", f"${nr['price']:,.0f} ({nr_pct:+.2f}%)", dn)

        # Confluence zone
        cz = tech.get("confluence_zones") if tech else []
        if cz:
            top_z = cz[0]
            gv("CONFLUENCE", f"${top_z['price']:,.0f} (score:{top_z['score']})", gold)
        
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
            # Gemini v2 fields
            ai_signal = gem.decision
            ai_confidence = gem.confianza
            ai_rationale = gem.analisis_cuant
            ai_regimen = gem.regimen_mercado
            ai_sl = gem.stop_loss
            ai_tp = gem.take_profit
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
        if gem is not None and gem.decision != 'ESPERAR':
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
            gv("WIN RATE", f"{gem.confianza:.0f}%", up if gem.confianza > 50 else dn)
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

        # ── REGIMEN / EXHAUSTION ───────────────────────────────────────
        exhaustion = "NONE"
        ex_color = gold
        if gem is not None:
            exhaustion = gem.regimen_mercado
            if "TENDENCIA" in exhaustion:
                ex_color = up
            elif "BLOQUEADA" in exhaustion:
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

        # ── BRACKET / SL TP ────────────────────────────────────────────
        td = self.battle_bar.trend_direction
        conf = self.battle_bar.confidence

        if gem is not None:
            gv("SL", f"${gem.stop_loss:,.0f}" if gem.stop_loss else "—", dn)
            gv("TP", f"${gem.take_profit:,.0f}" if gem.take_profit else "—", up)
            gv("REGIMEN", gem.regimen_mercado[:24], gold)
        elif brain and brain.get('confidence_pct', 0) > 0:
            br = brain.get('risk_bracket') or {}
            sl_v = br.get('sl', 0)
            tp_v = br.get('tp1', 0)
            gv("SL", f"${sl_v:,.0f}" if sl_v else "—", dn)
            gv("TP", f"${tp_v:,.0f}" if tp_v else "—", up)
        else:
            gv("SL", "—", dn)
            gv("TP", "—", up)

        # ── FINAL PREDICTION ────────────────────────────────────────────
        if gem is not None and gem.decision != 'ESPERAR' and gem.confianza >= 50:
            if gem.decision == 'ALZA':
                final_pred = "LONG"
                gv("FINAL PREDICTION", f"◉ BUY {gem.confianza:.0f}%", cyan)
            else:
                final_pred = "SHORT"
                gv("FINAL PREDICTION", f"◉ SELL {gem.confianza:.0f}%", magenta)
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
            gem_bracket = {
                "status": gem.decision,
                "trigger": gem.trigger_price,
                "sl": gem.stop_loss,
                "tp1": gem.take_profit,
                "tp2": 0,
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

        # Stop all timers
        for name in ('update_timer', '_pos_timer', '_data_timer',
                      'pulse_timer', 'flash_timer'):
            obj = getattr(self, name, None)
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass

        # Stop all anim_timer instances (multiple sub-widgets have one)
        for widget in self.findChildren((QTimer,)):
            try:
                widget.stop()
            except Exception:
                pass

        # Stop async data engine
        if hasattr(self, '_async_engine') and self._async_engine is not None:
            self._async_engine.stop()

        # Stop telegram bot
        if hasattr(self, 'telegram_bot') and self.telegram_bot is not None:
            self.telegram_bot.stop()

        # Stop order executor thread
        if hasattr(self, 'order_executor') and self.order_executor is not None:
            self.order_executor.stop()

        # Stop signal data worker
        if hasattr(self, '_data_worker') and self._data_worker is not None:
            try:
                self._data_worker.quit()
                self._data_worker.wait(2000)
            except Exception:
                pass

        # Stop F4 worker
        if hasattr(self, 'risk_tab'):
            rt = self.risk_tab
            if hasattr(rt, '_f4worker') and rt._f4worker is not None:
                try:
                    rt._f4worker.quit()
                    rt._f4worker.wait(2000)
                except Exception:
                    pass

        # Clean up knowledge parser worker if still running
        if hasattr(self, '_brain_worker') and self._brain_worker is not None:
            self._brain_worker.requestInterruption()
            self._brain_worker.deleteLater()
            self._brain_worker = None

        # Force exit
        QApplication.quit()
        import os
        os._exit(0)

    def closeEvent(self, event):
        print("\n👋 BB-450 cerrando sesión... ¡Hasta la próxima!")
        self.running = False
        event.accept()


if __name__ == "__main__":
    import signal
    def _sigint_handler(signum, frame):
        app = QApplication.instance()
        for w in app.topLevelWidgets():
            if isinstance(w, MainDashboard):
                w.close_application_cleanly()
                return
        print("\n👋 Recibido Ctrl+C — BB-450 cerrando sesión... ¡Hasta la próxima!")
        QApplication.quit()
    signal.signal(signal.SIGINT, _sigint_handler)

    app = QApplication(sys.argv)
    window = MainDashboard()
    window.show()
    sys.exit(app.exec_())