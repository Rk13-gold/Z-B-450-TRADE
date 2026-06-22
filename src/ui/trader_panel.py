"""
trader_panel.py — Panel "El Trader" para el dashboard PyQt5.

Arquitectura
────────────
  TraderPanel es un widget QDockWidget/Panel que muestra:
    - Último evento narrado
    - Texto del guión en efecto "máquina de escribir"
    - Botón "Repetir última narración"
    - Indicador de estado (escuchando, narrando, inactivo)
    - Historial de últimas 10 narraciones
    - Estadísticas del narrador

  Se integra en dashboard_gui.py como un panel acoplable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette, QTextCursor
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QListWidget, QListWidgetItem,
    QFrame, QSizePolicy, QSplitter, QSpinBox, QCheckBox,
)

from src.engine.event_detector import MarketEvent, event_detector
from src.engine.script_generator import NarrationScript, script_generator
from src.engine.voice_narrator import voice_narrator

from src.ui.theme_engine import theme as get_theme

log = logging.getLogger(__name__)

# ── Estilos ─────────────────────────────────────────────────────────

STYLE_ACTIVE = """
    QLabel#statusLabel {
        color: #00ff88;
        font-weight: bold;
        font-size: 12px;
    }
"""

STYLE_NARRATING = """
    QLabel#statusLabel {
        color: #ffaa00;
        font-weight: bold;
        font-size: 12px;
    }
"""

STYLE_IDLE = """
    QLabel#statusLabel {
        color: #888888;
        font-weight: bold;
        font-size: 12px;
    }
"""

PANEL_STYLE = """
    QWidget#traderPanel {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-radius: 8px;
    }
    QTextEdit {
        background-color: #0d1117;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 4px;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 12px;
        padding: 8px;
    }
    QPushButton {
        background-color: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 4px;
        padding: 6px 16px;
        min-width: 80px;
    }
    QPushButton:hover {
        background-color: #30363d;
        border-color: #8b949e;
    }
    QPushButton:pressed {
        background-color: #1a1e24;
    }
    QPushButton#playBtn {
        background-color: #238636;
        color: white;
        font-weight: bold;
    }
    QPushButton#playBtn:hover {
        background-color: #2ea043;
    }
    QPushButton#playBtn:disabled {
        background-color: #21262d;
        color: #484f58;
    }
    QListWidget {
        background-color: #0d1117;
        color: #8b949e;
        border: 1px solid #30363d;
        border-radius: 4px;
        font-size: 11px;
    }
    QListWidget::item {
        padding: 4px 8px;
        border-bottom: 1px solid #21262d;
    }
    QListWidget::item:selected {
        background-color: #1f6feb33;
        color: #c9d1d9;
    }
    QLabel {
        color: #c9d1d9;
    }
    QLabel#titleLabel {
        color: #00ff88;
        font-size: 14px;
        font-weight: bold;
    }
    QLabel#eventLabel {
        color: #ffaa00;
        font-size: 12px;
        font-weight: bold;
    }
    QLabel#timestampLabel {
        color: #484f58;
        font-size: 10px;
    }
"""


class TraderPanel(QDockWidget):
    """Panel "El Trader" que muestra narraciones en vivo del mercado.

    Signals
    -------
    narration_played(str): Emitido cuando se reproduce una narración (event_id)
    event_detected(MarketEvent): Emitido cuando se detecta un evento nuevo
    """

    narration_played = pyqtSignal(str)
    event_detected = pyqtSignal(object)

    STATUS_IDLE = "⚪ Inactivo"
    STATUS_LISTENING = "🟢 Escuchando mercado..."
    STATUS_NARRATING = "🟡 Narrando..."
    STATUS_ERROR = "🔴 Error"

    def __init__(self, parent=None):
        super().__init__("👤 El Trader", parent)
        self.setObjectName("TraderPanel")
        self.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )

        self._last_event: Optional[MarketEvent] = None
        self._last_script: Optional[NarrationScript] = None
        self._is_narrating: bool = False
        self._auto_narrate: bool = True
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._type_next_char)
        self._typing_text: str = ""
        self._typing_pos: int = 0

        self._build_ui()
        self.setStyleSheet(PANEL_STYLE)
        self.set_status(self.STATUS_LISTENING)
        get_theme().register(self, self._apply_theme)

    def _build_ui(self):
        container = QWidget()
        container.setObjectName("traderPanel")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Header ──
        header = QHBoxLayout()
        title = QLabel("👤 El Trader")
        title.setObjectName("titleLabel")
        self.status_label = QLabel(self.STATUS_IDLE)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status_label)
        layout.addLayout(header)

        # ── Controls ──
        controls = QHBoxLayout()

        self.auto_check = QCheckBox("Auto-narrar")
        self.auto_check.setChecked(self._auto_narrate)
        self.auto_check.stateChanged.connect(self._toggle_auto)
        self.auto_check.setStyleSheet("color: #8b949e;")

        self.play_btn = QPushButton("▶ Repetir")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.clicked.connect(self._replay_last)
        self.play_btn.setEnabled(False)

        self.clear_btn = QPushButton("🗑 Limpiar")
        self.clear_btn.clicked.connect(self._clear)

        controls.addWidget(self.auto_check)
        controls.addStretch()
        controls.addWidget(self.play_btn)
        controls.addWidget(self.clear_btn)
        layout.addLayout(controls)

        # ── Script display ──
        self.script_display = QTextEdit()
        self.script_display.setReadOnly(True)
        self.script_display.setPlaceholderText(
            "Esperando eventos del mercado...\n\n"
            "Cuando ocurra un evento relevante, 'El Trader' aparecerá aquí\n"
            "con un análisis profesional en tiempo real."
        )
        self.script_display.setMinimumHeight(120)
        layout.addWidget(self.script_display)

        # ── Event info ──
        info_layout = QHBoxLayout()
        self.event_label = QLabel("")
        self.event_label.setObjectName("eventLabel")
        self.timestamp_label = QLabel("")
        self.timestamp_label.setObjectName("timestampLabel")
        info_layout.addWidget(self.event_label)
        info_layout.addStretch()
        info_layout.addWidget(self.timestamp_label)
        layout.addLayout(info_layout)

        # ── Separator ──
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #30363d; max-height: 1px;")
        layout.addWidget(sep)

        # ── History ──
        history_label = QLabel("📜 Historial de narraciones:")
        history_label.setStyleSheet("font-size: 11px; color: #8b949e;")
        layout.addWidget(history_label)

        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(150)
        self.history_list.itemClicked.connect(self._on_history_click)
        layout.addWidget(self.history_list)

        # ── Stats ──
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("font-size: 10px; color: #484f58;")
        layout.addWidget(self.stats_label)

        self.setWidget(container)

    # ── Public API ──────────────────────────────────────────────────

    def set_status(self, status: str):
        self.status_label.setText(status)
        if "Escuchando" in status:
            self.status_label.setStyleSheet(STYLE_ACTIVE)
        elif "Narrando" in status:
            self.status_label.setStyleSheet(STYLE_NARRATING)
        else:
            self.status_label.setStyleSheet(STYLE_IDLE)

    def _apply_theme(self):
        tm = get_theme()
        bg = tm.get_ui_color("panel_bg", "#0d1117")
        txt = tm.get_ui_color("text_primary", "#c9d1d9")
        border = tm.get_ui_color("accent_secondary", "#30363d")
        accent = tm.get_ui_color("accent_primary", "#00ff88")
        self.setStyleSheet(
            PANEL_STYLE.replace("#0d1117", bg).replace("#c9d1d9", txt)
        )
        self.status_label.setStyleSheet(self.status_label.styleSheet())

    def on_event(self, event: MarketEvent, script: Optional[NarrationScript] = None):
        """Maneja un nuevo evento de mercado.

        Parameters
        ----------
        event : MarketEvent
            Evento detectado.
        script : NarrationScript | None
            Guión generado (opcional, se genera si no se provee).
        """
        self._last_event = event
        self.event_detected.emit(event)

        self.event_label.setText(f"📊 {event.title}")
        self.timestamp_label.setText(
            datetime.fromtimestamp(event.timestamp, tz=timezone.utc).strftime("%H:%M:%S UTC")
        )

        if script:
            self._display_script(script)

        if self._auto_narrate and not self._is_narrating:
            self.set_status(self.STATUS_NARRATING)
            self._is_narrating = True

    def _display_script(self, script: NarrationScript):
        """Muestra el guión con efecto máquina de escribir."""
        self._last_script = script
        self.play_btn.setEnabled(True)

        self.script_display.clear()
        display_text = script.optimized_tts or script.raw_text

        self._typing_text = display_text
        self._typing_pos = 0
        self._typing_timer.start(15)

        self._add_to_history(script)

        stats = voice_narrator.get_stats()
        self.stats_label.setText(
            f"Total narraciones: {stats.get('total_generated', 0)} | "
            f"Reproducidas: {stats.get('total_played', 0)}"
        )

    def _type_next_char(self):
        if self._typing_pos < len(self._typing_text):
            self.script_display.insertPlainText(self._typing_text[self._typing_pos])
            self._typing_pos += 1
            scrollbar = self.script_display.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        else:
            self._typing_timer.stop()
            self._is_narrating = False
            self.set_status(self.STATUS_LISTENING)

    def _add_to_history(self, script: NarrationScript):
        dt = datetime.fromtimestamp(script.timestamp, tz=timezone.utc)
        time_str = dt.strftime("%H:%M:%S")
        text = f"[{time_str}] {script.title[:60]}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, script.event_id)
        item.setToolTip(script.optimized_tts[:200] if script.optimized_tts else script.raw_text[:200])
        self.history_list.insertItem(0, item)
        while self.history_list.count() > 20:
            self.history_list.takeItem(self.history_list.count() - 1)

    def _on_history_click(self, item: QListWidgetItem):
        event_id = item.data(Qt.UserRole)
        if event_id and self._last_script and self._last_script.event_id == event_id:
            self.script_display.clear()
            display_text = self._last_script.optimized_tts or self._last_script.raw_text
            self.script_display.setPlainText(display_text)
            self.play_btn.setEnabled(True)

    def _replay_last(self):
        if self._last_script and self._last_script.audio_path:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(voice_narrator.play_live(self._last_script.audio_path))
                else:
                    loop.run_until_complete(voice_narrator.play_live(self._last_script.audio_path))
                self.narration_played.emit(self._last_script.event_id)
            except Exception as e:
                log.error(f"[TraderPanel] Replay error: {e}")
        elif self._last_script:
            self.script_display.clear()
            self.script_display.setPlainText(
                self._last_script.optimized_tts or self._last_script.raw_text
            )

    def _toggle_auto(self, state):
        self._auto_narrate = state == Qt.Checked

    def _clear(self):
        self.script_display.clear()
        self.event_label.setText("")
        self.timestamp_label.setText("")
        self._last_event = None
        self._last_script = None
        self.play_btn.setEnabled(False)
        self.set_status(self.STATUS_LISTENING)
