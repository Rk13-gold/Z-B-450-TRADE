"""
trading_control.py — Panel de control profesional para trading diario.

Arquitectura
────────────
  TradingControlPanel es un QDockWidget flotante que permite:
    - Visualizar la señal activa con SL/TP/R:R
    - Autorizar señales generadas por la estrategia
    - Ejecutar órdenes manuales LONG/SHORT
    - Monitorear posición abierta y PnL
    - Ajustar capital y apalancamiento

  Se sincroniza con MainDashboard vía update_signal() y emite
  señales para ejecutar órdenes a través de OrderExecutor.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QProgressBar,
    QSlider, QTextEdit, QSizePolicy,
)

from src.ui.theme_engine import theme as get_theme

log = logging.getLogger(__name__)

LONG_COLOR = "#00ff88"
SHORT_COLOR = "#ff4444"
NEUTRAL_COLOR = "#888888"
GOLD_COLOR = "#ffaa00"


class TradingControlPanel(QDockWidget):
    """Panel de control de trading profesional flotante."""

    # Señales para comunicarse con OrderExecutor
    signal_authorized = pyqtSignal(dict)   # {direction, entry, sl, tp1, tp2, capital, leverage}
    manual_order = pyqtSignal(str, float)  # (side="BUY"/"SELL", capital_pct)
    close_position = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("🎮 Trading Control", parent)
        self.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetMovable)
        self.setMinimumWidth(260)
        self.setMaximumWidth(320)

        # ── Estado interno ──
        self._price = 0.0
        self._bid = 0.0
        self._ask = 0.0
        self._signal = "WAIT"
        self._confidence = 0.0
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit_1 = 0.0
        self._take_profit_2 = 0.0
        self._risk_reward = 0.0
        self._capital_pct = 50.0
        self._leverage = 3
        self._position_side = ""
        self._position_entry = 0.0
        self._position_qty = 0.0
        self._position_pnl = 0.0
        self._has_position = False

        self._build_ui()
        self._apply_dark_style()
        get_theme().register(self, self._apply_theme)

    def _build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Price Header ──
        self._price_label = QLabel("$0.00")
        self._price_label.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {NEUTRAL_COLOR};")
        self._price_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._price_label)

        spread_layout = QHBoxLayout()
        self._bid_label = QLabel("Bid: --")
        self._ask_label = QLabel("Ask: --")
        self._spread_label = QLabel("Spread: --")
        for lbl in (self._bid_label, self._ask_label, self._spread_label):
            lbl.setStyleSheet("font-size: 9px; color: #888;")
            lbl.setAlignment(Qt.AlignCenter)
        spread_layout.addWidget(self._bid_label)
        spread_layout.addWidget(self._ask_label)
        spread_layout.addWidget(self._spread_label)
        layout.addLayout(spread_layout)

        layout.addWidget(self._make_separator())

        # ── Signal Display ──
        signal_header = QLabel("📡 SEÑAL ACTIVA")
        signal_header.setStyleSheet("font-size: 10px; font-weight: bold; color: #aaa;")
        layout.addWidget(signal_header)

        self._signal_badge = QLabel("ESPERANDO")
        self._signal_badge.setAlignment(Qt.AlignCenter)
        self._signal_badge.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {NEUTRAL_COLOR}; "
            f"background: #1a1a2e; border-radius: 4px; padding: 4px;"
        )
        layout.addWidget(self._signal_badge)

        # Confidence bar
        self._conf_bar = QProgressBar()
        self._conf_bar.setRange(0, 100)
        self._conf_bar.setValue(0)
        self._conf_bar.setTextVisible(True)
        self._conf_bar.setFixedHeight(14)
        self._conf_bar.setStyleSheet(
            "QProgressBar { background: #1a1a2e; border: none; border-radius: 3px; text-align: center; font-size: 8px; color: #aaa; }"
            "QProgressBar::chunk { background: #555; border-radius: 3px; }"
        )
        layout.addWidget(self._conf_bar)

        # Signal details grid
        self._entry_lbl = QLabel("Entry: --")
        self._sl_lbl = QLabel("SL: --")
        self._tp1_lbl = QLabel("TP1: --")
        self._tp2_lbl = QLabel("TP2: --")
        self._rr_lbl = QLabel("R:R: --")
        for lbl in (self._entry_lbl, self._sl_lbl, self._tp1_lbl, self._tp2_lbl, self._rr_lbl):
            lbl.setStyleSheet("font-size: 10px; color: #bbb; font-family: Consolas;")
        detail_grid = QVBoxLayout()
        detail_grid.setSpacing(1)
        detail_grid.addWidget(self._entry_lbl)
        detail_grid.addWidget(self._sl_lbl)
        detail_grid.addWidget(self._tp1_lbl)
        detail_grid.addWidget(self._tp2_lbl)
        detail_grid.addWidget(self._rr_lbl)
        layout.addLayout(detail_grid)

        # Authorize button
        self._auth_btn = QPushButton("🔒 AUTORIZAR SEÑAL")
        self._auth_btn.setStyleSheet(
            f"QPushButton {{ background: {GOLD_COLOR}; color: #000; font-weight: bold; "
            f"font-size: 12px; padding: 8px; border-radius: 4px; border: none; }}"
            f"QPushButton:hover {{ background: #ffbb22; }}"
            f"QPushButton:disabled {{ background: #333; color: #666; }}"
        )
        self._auth_btn.clicked.connect(self._on_authorize)
        self._auth_btn.setEnabled(False)
        layout.addWidget(self._auth_btn)

        layout.addWidget(self._make_separator())

        # ── Manual Controls ──
        manual_header = QLabel("🎮 CONTROL MANUAL")
        manual_header.setStyleSheet("font-size: 10px; font-weight: bold; color: #aaa;")
        layout.addWidget(manual_header)

        btn_row = QHBoxLayout()
        self._long_btn = QPushButton("🟢 LONG")
        self._long_btn.setStyleSheet(
            f"QPushButton {{ background: #004422; color: {LONG_COLOR}; font-weight: bold; "
            f"font-size: 14px; padding: 10px; border-radius: 4px; border: 1px solid {LONG_COLOR}; }}"
            f"QPushButton:hover {{ background: #006633; }}"
        )
        self._long_btn.clicked.connect(lambda: self._on_manual("BUY"))
        self._short_btn = QPushButton("🔴 SHORT")
        self._short_btn.setStyleSheet(
            f"QPushButton {{ background: #440000; color: {SHORT_COLOR}; font-weight: bold; "
            f"font-size: 14px; padding: 10px; border-radius: 4px; border: 1px solid {SHORT_COLOR}; }}"
            f"QPushButton:hover {{ background: #660000; }}"
        )
        self._short_btn.clicked.connect(lambda: self._on_manual("SELL"))
        btn_row.addWidget(self._long_btn)
        btn_row.addWidget(self._short_btn)
        layout.addLayout(btn_row)

        # Capital presets
        cap_row = QHBoxLayout()
        cap_label = QLabel("Capital:")
        cap_label.setStyleSheet("font-size: 10px; color: #888;")
        cap_row.addWidget(cap_label)
        for pct in [25, 50, 75, 100]:
            btn = QPushButton(f"{pct}%")
            btn.setFixedHeight(22)
            btn.setStyleSheet(
                f"QPushButton {{ background: #1a1a2e; color: #aaa; font-size: 9px; "
                f"border: 1px solid #333; border-radius: 3px; padding: 2px 6px; }}"
                f"QPushButton:hover {{ background: #2a2a3e; border-color: {GOLD_COLOR}; }}"
                f"QPushButton:checked {{ background: #2a2a3e; border-color: {GOLD_COLOR}; color: {GOLD_COLOR}; }}"
            )
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, p=pct: self._set_capital(p))
            cap_row.addWidget(btn)
        cap_row.addStretch()
        layout.addLayout(cap_row)
        self._set_capital(int(self._capital_pct))  # chequea el botón por defecto (50%)

        # Leverage slider
        lev_row = QHBoxLayout()
        lev_label = QLabel(f"Apalanc: {self._leverage}x")
        lev_label.setStyleSheet("font-size: 10px; color: #888;")
        self._lev_label = lev_label
        lev_row.addWidget(lev_label)
        self._lev_slider = QSlider(Qt.Horizontal)
        self._lev_slider.setRange(1, 40)
        self._lev_slider.setValue(self._leverage)
        self._lev_slider.valueChanged.connect(lambda v: self._on_leverage(v))
        self._lev_slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: #333; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #888; width: 12px; border-radius: 6px; margin: -4px 0; }"
        )
        lev_row.addWidget(self._lev_slider)
        layout.addLayout(lev_row)

        layout.addWidget(self._make_separator())

        # ── Position Status ──
        self._pos_frame = QFrame()
        pos_layout = QVBoxLayout(self._pos_frame)
        pos_layout.setContentsMargins(0, 0, 0, 0)
        pos_layout.setSpacing(2)
        pos_header = QLabel("📊 POSICIÓN")
        pos_header.setStyleSheet("font-size: 10px; font-weight: bold; color: #aaa;")
        pos_layout.addWidget(pos_header)

        self._pos_side_lbl = QLabel("--")
        self._pos_side_lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #888;")
        pos_layout.addWidget(self._pos_side_lbl)

        self._pos_entry_lbl = QLabel("Entry: --")
        self._pos_qty_lbl = QLabel("Size: --")
        self._pos_pnl_lbl = QLabel("PnL: --")
        for lbl in (self._pos_entry_lbl, self._pos_qty_lbl, self._pos_pnl_lbl):
            lbl.setStyleSheet("font-size: 10px; color: #bbb; font-family: Consolas;")
        pos_layout.addWidget(self._pos_entry_lbl)
        pos_layout.addWidget(self._pos_qty_lbl)
        pos_layout.addWidget(self._pos_pnl_lbl)

        self._close_btn = QPushButton("⏹ CERRAR POSICIÓN")
        self._close_btn.setStyleSheet(
            f"QPushButton {{ background: #440000; color: {SHORT_COLOR}; font-weight: bold; "
            f"font-size: 11px; padding: 6px; border-radius: 4px; border: 1px solid {SHORT_COLOR}; }}"
            f"QPushButton:hover {{ background: #660000; }}"
            f"QPushButton:disabled {{ background: #1a1a2e; color: #444; border-color: #333; }}"
        )
        self._close_btn.clicked.connect(self._on_close_position)
        self._close_btn.setEnabled(False)
        pos_layout.addWidget(self._close_btn)
        layout.addWidget(self._pos_frame)

        layout.addWidget(self._make_separator())

        # ── Log ──
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(80)
        self._log.setStyleSheet(
            "QTextEdit { background: #0a0a14; color: #666; font-size: 8px; "
            "font-family: Consolas; border: 1px solid #1a1a2e; border-radius: 3px; }"
        )
        self._log.append("🟢 Sistema listo")
        layout.addWidget(self._log)

        self.setWidget(container)

    def _make_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #1a1a2e;")
        return sep

    def _apply_dark_style(self):
        self.setStyleSheet(
            "QDockWidget { background: #0d0d1a; color: #ccc; font-family: Segoe UI; }"
            "QDockWidget::title { background: #1a1a2e; padding: 6px; font-weight: bold; font-size: 11px; }"
        )

    def _apply_theme(self):
        tm = get_theme()
        bg = tm.get_ui_color("panel_bg", "#0d0d1a")
        txt = tm.get_ui_color("text_primary", "#ccc")
        title_bg = tm.get_ui_color("accent_secondary", "#1a1a2e")
        self.setStyleSheet(
            f"QDockWidget {{ background: {bg}; color: {txt}; font-family: Segoe UI; }}"
            f"QDockWidget::title {{ background: {title_bg}; padding: 6px; font-weight: bold; font-size: 11px; }}"
        )

    # ── Public API ──

    def _to_float(self, v, default=0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    def update_signal(self, data: dict):
        """Actualiza todos los indicadores desde MainDashboard."""
        self._price = self._to_float(data.get('price', self._price))
        self._bid = self._to_float(data.get('bid', self._bid))
        self._ask = self._to_float(data.get('ask', self._ask))
        self._signal = data.get('signal', self._signal) or "WAIT"
        self._confidence = self._to_float(data.get('confidence', self._confidence))
        self._entry_price = self._to_float(data.get('entry_price', self._entry_price))
        self._stop_loss = self._to_float(data.get('stop_loss', self._stop_loss))
        self._take_profit_1 = self._to_float(data.get('take_profit_1', self._take_profit_1))
        self._take_profit_2 = self._to_float(data.get('take_profit_2', self._take_profit_2))
        self._risk_reward = self._to_float(data.get('risk_reward', self._risk_reward))
        self._has_position = bool(data.get('has_position', self._has_position))
        self._position_side = data.get('position_side', self._position_side) or ""
        self._position_entry = self._to_float(data.get('position_entry', self._position_entry))
        self._position_qty = self._to_float(data.get('position_qty', self._position_qty))
        self._position_pnl = self._to_float(data.get('position_pnl', self._position_pnl))

        self._refresh_ui()

    def update_position(self, pos_data: dict):
        """Actualiza solo el estado de la posición."""
        self._has_position = pos_data.get('has_position', self._has_position)
        self._position_side = pos_data.get('side', self._position_side)
        self._position_entry = pos_data.get('entry_price', self._position_entry)
        self._position_qty = pos_data.get('quantity', self._position_qty)
        self._position_pnl = pos_data.get('pnl', self._position_pnl)
        self._refresh_ui()

    def add_log(self, msg: str):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── UI Refresh ──

    def _refresh_ui(self):
        price_str = f"${self._price:,.2f}" if self._price else "$0.00"
        self._price_label.setText(price_str)

        bid_val = float(self._bid) if self._bid else 0
        ask_val = float(self._ask) if self._ask else 0
        if bid_val and ask_val:
            self._bid_label.setText(f"Bid: ${bid_val:,.2f}")
            self._ask_label.setText(f"Ask: ${ask_val:,.2f}")
            spread = ask_val - bid_val
            self._spread_label.setText(f"Spread: ${spread:.2f}")
        else:
            self._bid_label.setText("Bid: --")
            self._ask_label.setText("Ask: --")
            self._spread_label.setText("Spread: --")

        # Signal badge
        if self._signal in ("COMPRA", "LONG", "BUY"):
            sig_text = "⬆ LONG"
            sig_color = LONG_COLOR
            sig_bg = "#002211"
        elif self._signal in ("VENTA", "SHORT", "SELL"):
            sig_text = "⬇ SHORT"
            sig_color = SHORT_COLOR
            sig_bg = "#220000"
        else:
            sig_text = "◼ ESPERANDO"
            sig_color = NEUTRAL_COLOR
            sig_bg = "#111122"

        self._signal_badge.setText(sig_text)
        self._signal_badge.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {sig_color}; "
            f"background: {sig_bg}; border-radius: 4px; padding: 4px;"
        )

        # Confidence bar
        conf = int(self._confidence)
        self._conf_bar.setValue(conf)
        if conf > 70:
            bar_color = LONG_COLOR
        elif conf > 40:
            bar_color = GOLD_COLOR
        else:
            bar_color = NEUTRAL_COLOR
        self._conf_bar.setFormat(f"Confianza: {conf}%")
        self._conf_bar.setStyleSheet(
            "QProgressBar { background: #1a1a2e; border: none; border-radius: 3px; text-align: center; font-size: 8px; color: #aaa; }"
            f"QProgressBar::chunk {{ background: {bar_color}; border-radius: 3px; }}"
        )

        # Signal details
        def fmt_price(p):
            return f"${p:,.2f}" if p else "--"

        self._entry_lbl.setText(f"Entry: {fmt_price(self._entry_price)}")
        self._sl_lbl.setText(f"SL:    {fmt_price(self._stop_loss)}")
        self._tp1_lbl.setText(f"TP1:  {fmt_price(self._take_profit_1)}")
        self._tp2_lbl.setText(f"TP2:  {fmt_price(self._take_profit_2)}")
        self._rr_lbl.setText(f"R:R:  1:{self._risk_reward:.2f}" if self._risk_reward else "R:R:  --")

        # Auth button
        has_valid_signal = self._signal not in ("WAIT", "NINGUNA", "") and self._entry_price > 0
        self._auth_btn.setEnabled(has_valid_signal and not self._has_position)

        # Position status
        if self._has_position:
            self._pos_frame.setVisible(True)
            side_color = LONG_COLOR if self._position_side in ("LONG", "BUY") else SHORT_COLOR
            self._pos_side_lbl.setText(f"{'🟢' if self._position_side in ('LONG','BUY') else '🔴'} {self._position_side} {self._position_qty:.4f} BTC")
            self._pos_side_lbl.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {side_color};")
            self._pos_entry_lbl.setText(f"Entry: ${self._position_entry:,.2f}")
            self._pos_qty_lbl.setText(f"Size:  {self._position_qty:.4f} BTC")
            pnl_color = LONG_COLOR if self._position_pnl >= 0 else SHORT_COLOR
            self._pos_pnl_lbl.setText(f"PnL:   {self._position_pnl:+.2f}%")
            self._pos_pnl_lbl.setStyleSheet(f"font-size: 10px; color: {pnl_color}; font-family: Consolas;")
            self._close_btn.setEnabled(True)
        else:
            self._pos_frame.setVisible(False)
            self._close_btn.setEnabled(False)

    # ── Event Handlers ──

    def _on_authorize(self):
        data = {
            'direction': self._signal,
            'entry': self._entry_price,
            'sl': self._stop_loss,
            'tp1': self._take_profit_1,
            'tp2': self._take_profit_2,
            'capital_pct': self._capital_pct,
            'leverage': self._leverage,
            'confidence': int(self._confidence),
        }
        self.signal_authorized.emit(data)
        self.add_log(f"🔒 Señal autorizada: {self._signal} @ ${self._entry_price:,.2f}")

    def _on_manual(self, side: str):
        self.manual_order.emit(side, self._capital_pct / 100.0)
        cap_str = f"{self._capital_pct:.0f}%"
        self.add_log(f"🎮 {side} manual ({cap_str}) @ ${self._price:,.2f}")

    def _on_close_position(self):
        self.close_position.emit()
        self.add_log("⏹ Posición cerrada manualmente")

    def _set_capital(self, pct: int):
        self._capital_pct = float(pct)
        for btn in self.findChildren(QPushButton):
            if btn.text().endswith('%') and btn.isCheckable():
                btn.setChecked(btn.text() == f"{pct}%")

    def _on_leverage(self, value: int):
        self._leverage = value
        if hasattr(self, '_lev_label'):
            self._lev_label.setText(f"Apalanc: {value}x")
