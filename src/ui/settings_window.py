"""
settings_window.py — Panel de configuración del chart y el programa.

Ventana flotante no-modal con 4 pestañas:
  1. 📊 INDICADORES — toggles on/off + colores para cada overlay
  2. ⏱ TEMPORALIDAD — selector de timeframe
  3. 🎨 TEMAS — selector de tema + preview
  4. ⚡ PERSONALIZADOS — crear/editar/eliminar indicadores por fórmula
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSlider, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
    QColorDialog, QGroupBox, QGridLayout, QSizePolicy, QMessageBox,
)

from src.ui.theme_engine import ThemeManager, theme
from src.ui.indicator_engine import (
    CompiledIndicator, FormulaParser, FormulaError, OUTPUT_TYPES,
)

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
FLAGS_FILE = CONFIG_DIR / "indicator_flags.json"
CUSTOM_FILE = CONFIG_DIR / "custom_indicators.json"
TIMEFRAME_FILE = CONFIG_DIR / "timeframe.json"


# ── Helper para guardar/cargar configs ─────────────────────────────────

def _load_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"[Settings] Error loading {path.name}: {exc}")
    return default


def _save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning(f"[Settings] Error saving {path.name}: {exc}")


# ── Widget reutilizable: entrada de color ──────────────────────────────

class ColorInput(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, initial: str = "#ffffff", label: str = ""):
        super().__init__()
        self._color = initial
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if label:
            layout.addWidget(QLabel(label))
        self._swatch = QPushButton()
        self._swatch.setFixedSize(24, 20)
        self._swatch.clicked.connect(self._pick)
        self._update_swatch()
        layout.addWidget(self._swatch)
        self._hex_input = QLineEdit(initial)
        self._hex_input.setFixedWidth(80)
        self._hex_input.textChanged.connect(self._on_text)
        layout.addWidget(self._hex_input)

    def _pick(self):
        color = QColorDialog.getColor(QColor(self._color), self, "Seleccionar color")
        if color.isValid():
            self._color = color.name()
            self._update_swatch()
            self._hex_input.setText(self._color)
            self.changed.emit(self._color)

    def _on_text(self, text: str):
        if QColor(text).isValid():
            self._color = text
            self._update_swatch()
            self.changed.emit(self._color)

    def _update_swatch(self):
        self._swatch.setStyleSheet(
            f"QPushButton {{ background: {self._color}; border: 1px solid #555; "
            f"border-radius: 3px; }}"
        )

    @property
    def color(self) -> str:
        return self._color

    @color.setter
    def color(self, c: str):
        self._color = c
        self._update_swatch()
        self._hex_input.setText(c)


# ── SettingsWindow ─────────────────────────────────────────────────────

class SettingsWindow(QDialog):
    """Ventana de configuración del chart y el programa.

    Señales
    -------
    indicators_changed : pyqtSignal(dict)
        Emitida cuando cambian los toggles/colores de indicadores.
    timeframe_changed : pyqtSignal(str)
        Emitida cuando se selecciona un nuevo timeframe.
    theme_changed : pyqtSignal(str)
        Emitida cuando se aplica un tema (también lo hace ThemeManager).
    custom_indicators_changed : pyqtSignal(list)
        Emitida cuando se añade/elimina un indicador personalizado.
    """

    indicators_changed = pyqtSignal(dict)
    timeframe_changed = pyqtSignal(str)
    custom_indicators_changed = pyqtSignal(list)

    TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Configuración del Chart")
        self.setMinimumWidth(500)
        self.setMinimumHeight(550)
        self.setModal(False)  # no-modal

        # Estado interno
        self._flags: dict = _load_json(FLAGS_FILE, {
            "ema_cloud": False,
            "vwap": False,
            "dpoc": False,
            "imbalance_circles": False,
            "volatility_cone": False,
            "liquidity_arrow": False,
            "pulse_entry": False,
            "signal_strip": False,
            "grid_lines": True,
            "footprint_numbers": False,
            "volume_profile": False,
            "price_bar_extras": False,
            "candle_annotations": False,
        })
        self._timeframe: str = _load_json(TIMEFRAME_FILE, {}).get("timeframe", "1m")
        raw_custom = _load_json(CUSTOM_FILE, [])
        self._custom_indicators: list = raw_custom if isinstance(raw_custom, list) else []
        self._theme_mgr = theme()

        self._build_ui()
        self._apply_dialog_theme()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_tab_indicators(), "📊 Indicadores")
        self._tabs.addTab(self._build_tab_timeframe(), "⏱ Temporalidad")
        self._tabs.addTab(self._build_tab_themes(), "🎨 Temas")
        self._tabs.addTab(self._build_tab_custom(), "⚡ Personalizados")

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("✖ Cerrar")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ── Pestaña 1: Indicadores ─────────────────────────────────────────

    def _build_tab_indicators(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(3)

        self._indicator_widgets: dict[str, tuple[QCheckBox, ColorInput]] = {}

        groups = [
            ("📊 ORDER FLOW", [
                ("signal_strip",       "Signal Strip (RVOL/Delta)", True),
                ("imbalance_circles",  "Círculos de desbalance",     True),
                ("footprint_numbers",  "Footprint (volumen bid/ask)", False),
                ("volume_profile",     "Perfil de volumen lateral", False),
                ("price_bar_extras",   "Barra de precio VAH/VAL/POC", False),
                ("candle_annotations", "Anotaciones ballena",       False),
            ]),
            ("📐 TENDENCIA", [
                ("ema_cloud",          "Nube EMA (9/21)",           True),
                ("vwap",               "VWAP",                      True),
                ("dpoc",               "dPOC dinámico + trail",     True),
                ("volatility_cone",    "Cono de volatilidad (ATR)", True),
            ]),
            ("🎯 SOPORTES Y RESISTENCIAS", [
                ("sr_levels",          "S/R Automático (pivotes)",  True),
                ("order_blocks",       "Order Blocks (OB)",         True),
                ("fvg",                "Fair Value Gaps (FVG)",     True),
                ("swing_liquidity",    "Liquidez Swing Hi/Lo",      True),
            ]),
            ("🌀 FIBONACCI", [
                ("fib_retracement",    "Fib Retracement (0.236-0.786)", True),
                ("fib_extension",      "Fib Extension (1.272-3.618)",   True),
                ("fib_time_zones",     "Fib Time Zones",             False),
            ]),
            ("🎯 ENTRY ZONES", [
                ("entry_zones",        "Zonas de entrada (Bid/Ask)", True),
                ("ict_killzones",      "ICT Killzones (horarios)",   True),
                ("orderflow_imbalance","Flechas de imbalance OI",    True),
                ("premium_discount",   "Zonas Premium/Discount",     True),
                ("liquidity_arrow",    "Flecha de liquidez",        True),
                ("pulse_entry",        "Pulso de entrada (radar)",  True),
            ]),
            ("🔧 EXTRAS", [
                ("grid_lines",         "Grid de fondo",             True),
            ]),
        ]

        for group_name, items in groups:
            group_label = QLabel(group_name)
            group_label.setStyleSheet(
                "color: #ffaa00; font-weight: bold; font-size: 11px; "
                "margin-top: 8px; margin-bottom: 2px;"
            )
            layout.addWidget(group_label)
            for key, label, has_color in items:
                row = QHBoxLayout()
                row.setContentsMargins(12, 0, 0, 0)
                cb = QCheckBox(label)
                cb.setChecked(self._flags.get(key, False))
                cb.stateChanged.connect(lambda _, k=key: self._emit_indicators())
                row.addWidget(cb)
                if has_color:
                    ci = ColorInput(self._get_indicator_color(key), "")
                    ci.changed.connect(lambda _, k=key: self._emit_indicators())
                    self._indicator_widgets[key] = (cb, ci)
                    row.addWidget(ci)
                    row.addStretch()
                else:
                    self._indicator_widgets[key] = (cb, None)
                    row.addStretch()
                layout.addLayout(row)

        layout.addStretch()
        return tab

    def _emit_indicators(self):
        flags = {}
        for key, (cb, ci) in self._indicator_widgets.items():
            flags[key] = cb.isChecked()
        self._flags.update(flags)
        _save_json(FLAGS_FILE, self._flags)
        self.indicators_changed.emit(self._flags)

    def _get_indicator_color(self, key: str) -> str:
        """Color por defecto para cada indicador según el tema activo."""
        mapping = {
            "ema_cloud": "ema_9",
            "vwap": "vwap",
            "dpoc": "dpoc",
            "imbalance_circles": "imbalance_buy",
            "volatility_cone": "cone_bull",
            "liquidity_arrow": "liquidity_arrow",
            "pulse_entry": "pulse_entry",
            "signal_strip": "signal_strip_rvol",
            "footprint_numbers": "footprint_buy",
            "volume_profile": "vp_buy",
            "price_bar_extras": "dpoc",
            "candle_annotations": "imbalance_buy",
            "sr_levels": "sr_resistance",
            "order_blocks": "ob_bull",
            "fvg": "fvg",
            "swing_liquidity": "swing_high",
            "fib_retracement": "fib_line",
            "fib_extension": "fib_line",
            "fib_time_zones": "fib_line",
            "entry_zones": "entry_zone",
            "ict_killzones": "ict_killzone",
            "orderflow_imbalance": "oi_imbalance_buy",
            "premium_discount": "premium_fill",
        }
        chart_key = mapping.get(key, "")
        if chart_key:
            return self._theme_mgr.get_chart_color(chart_key, "#ffffff")
        return "#ffffff"

    # ── Pestaña 2: Timeframe ───────────────────────────────────────────

    def _build_tab_timeframe(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel("Selecciona la temporalidad del chart:"))
        layout.addSpacing(8)

        tf_row = QHBoxLayout()
        self._tf_btns: dict[str, QPushButton] = {}
        for tf in self.TIMEFRAMES:
            btn = QPushButton(tf)
            btn.setCheckable(True)
            btn.setFixedSize(60, 36)
            btn.setStyleSheet(
                f"QPushButton {{ background: #1a1a2e; color: #aaa; "
                f"border: 1px solid #333; border-radius: 4px; font-size: 11px; }}"
                f"QPushButton:hover {{ border-color: #ffaa00; }}"
                f"QPushButton:checked {{ background: #ffaa00; color: #000; "
                f"font-weight: bold; border-color: #ffaa00; }}"
            )
            btn.clicked.connect(lambda checked, t=tf: self._on_timeframe(t))
            self._tf_btns[tf] = btn
            tf_row.addWidget(btn)
        tf_row.addStretch()
        layout.addLayout(tf_row)

        # Marcar el actual
        if self._timeframe in self._tf_btns:
            self._tf_btns[self._timeframe].setChecked(True)

        layout.addSpacing(12)
        info = QLabel(
            "Al cambiar de temporalidad, todos los indicadores\n"
            "se recalculan automáticamente con los nuevos datos."
        )
        info.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(info)
        layout.addStretch()
        return tab

    def _on_timeframe(self, tf: str):
        for btn in self._tf_btns.values():
            btn.setChecked(False)
        self._tf_btns[tf].setChecked(True)
        self._timeframe = tf
        _save_json(TIMEFRAME_FILE, {"timeframe": tf})
        self.timeframe_changed.emit(tf)

    # ── Pestaña 3: Temas ───────────────────────────────────────────────

    def _build_tab_themes(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel("Tema visual para todo el programa:"))
        layout.addSpacing(8)

        self._theme_combo = QComboBox()
        for key in self._theme_mgr.theme_keys:
            self._theme_combo.addItem(self._theme_mgr.theme_names[self._theme_mgr.theme_keys.index(key)], key)
        idx = self._theme_combo.findData(self._theme_mgr.active_name)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_select)
        layout.addWidget(self._theme_combo)

        layout.addSpacing(12)

        # Preview visual
        preview_group = QGroupBox("Preview de colores")
        preview_layout = QGridLayout(preview_group)
        self._preview_labels: dict[str, QLabel] = {}
        preview_items = [
            ("chart.bg", "Fondo chart"),
            ("chart.candle_bull", "Vela Bull"),
            ("chart.candle_bear", "Vela Bear"),
            ("chart.grid", "Grid"),
            ("ui.panel_bg", "Panel BG"),
            ("ui.text_primary", "Texto"),
            ("ui.accent", "Acento"),
        ]
        for row, (key, label) in enumerate(preview_items):
            preview_layout.addWidget(QLabel(label), row, 0)
            swatch = QLabel("████")
            swatch.setFixedWidth(60)
            self._preview_labels[key] = swatch
            preview_layout.addWidget(swatch, row, 1)
        layout.addWidget(preview_group)

        # Botón aplicar
        apply_btn = QPushButton("✓ Aplicar tema a todo el programa")
        apply_btn.setStyleSheet(
            f"QPushButton {{ background: #ffaa00; color: #000; font-weight: bold; "
            f"font-size: 12px; padding: 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background: #ffbb22; }}"
        )
        apply_btn.clicked.connect(self._on_theme_apply)
        layout.addWidget(apply_btn)

        self._update_preview()
        layout.addStretch()
        return tab

    def _on_theme_select(self, idx: int):
        self._update_preview()

    def _on_theme_apply(self):
        key = self._theme_combo.currentData()
        if key:
            self._theme_mgr.set_theme(key)
            self._update_preview()
            self._apply_dialog_theme()

    def _update_preview(self):
        key = self._theme_combo.currentData() or self._theme_mgr.active_name
        t = self._theme_mgr._themes.get(key, {})
        chart = t.get("chart", {})
        ui = t.get("ui", {})
        for full_key, label in self._preview_labels.items():
            section, k = full_key.split(".")
            color = (chart if section == "chart" else ui).get(k, "#888")
            label.setStyleSheet(
                f"color: {color}; font-size: 14px; font-weight: bold; "
                f"background: transparent;"
            )

    def _apply_dialog_theme(self):
        """Aplica el tema actual a esta ventana."""
        tm = self._theme_mgr
        bg = tm.get_ui_color("panel_bg", "#0d0d1a")
        txt = tm.get_ui_color("text_primary", "#ffffff")
        self.setStyleSheet(
            f"QDialog {{ background: {bg}; color: {txt}; }}"
            f"QLabel {{ color: {txt}; font-size: 10px; }}"
            f"QGroupBox {{ color: {txt}; font-size: 10px; font-weight: bold; "
            f"border: 1px solid {tm.get_ui_color('panel_border', '#333')}; "
            f"border-radius: 4px; margin-top: 10px; padding-top: 10px; }}"
        )

    # ── Pestaña 4: Indicadores Personalizados ─────────────────────────

    def _build_tab_custom(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Formulario
        form = QGroupBox("Crear nuevo indicador")
        form_layout = QGridLayout(form)

        form_layout.addWidget(QLabel("Nombre:"), 0, 0)
        self._ci_name = QLineEdit()
        self._ci_name.setPlaceholderText("ej: Mi EMA50")
        form_layout.addWidget(self._ci_name, 0, 1)

        form_layout.addWidget(QLabel("Fórmula:"), 1, 0)
        self._ci_formula = QLineEdit()
        self._ci_formula.setPlaceholderText("ej: EMA(close, 50)")
        form_layout.addWidget(self._ci_formula, 1, 1)

        form_layout.addWidget(QLabel("Color:"), 2, 0)
        self._ci_color = ColorInput("#00ff88")
        form_layout.addWidget(self._ci_color, 2, 1)

        form_layout.addWidget(QLabel("Tipo:"), 3, 0)
        self._ci_type = QComboBox()
        for ot in OUTPUT_TYPES:
            self._ci_type.addItem(ot.capitalize(), ot)
        form_layout.addWidget(self._ci_type, 3, 1)

        form_layout.addWidget(QLabel("Timeframe:"), 4, 0)
        self._ci_tf = QComboBox()
        self._ci_tf.addItems(["Todos", "1m", "5m", "15m", "30m", "1h", "4h", "1d"])
        form_layout.addWidget(self._ci_tf, 4, 1)

        add_btn = QPushButton("➕ AÑADIR")
        add_btn.clicked.connect(self._on_add_custom)
        form_layout.addWidget(add_btn, 5, 0, 1, 2)

        layout.addWidget(form)

        # Lista de indicadores guardados
        layout.addWidget(QLabel("Indicadores guardados:"))
        self._ci_list = QListWidget()
        self._refresh_custom_list()
        layout.addWidget(self._ci_list)

        btn_row = QHBoxLayout()
        del_btn = QPushButton("✖ Eliminar seleccionado")
        del_btn.clicked.connect(self._on_delete_custom)
        btn_row.addWidget(del_btn)
        test_btn = QPushButton("▶ Probar fórmula")
        test_btn.clicked.connect(self._on_test_formula)
        btn_row.addWidget(test_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        return tab

    def _on_add_custom(self):
        name = self._ci_name.text().strip()
        formula = self._ci_formula.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "El nombre no puede estar vacío.")
            return
        if not formula:
            QMessageBox.warning(self, "Error", "La fórmula no puede estar vacía.")
            return
        # Validar fórmula
        parser = FormulaParser()
        try:
            dummy = [{
                "t": 0, "o": 50000, "h": 50100, "l": 49900,
                "c": 50050, "v": 100
            }] * 100
            # Convertir a formato kline
            klines = [[d["t"], d["o"], d["h"], d["l"], d["c"], d["v"]] for d in dummy]
            parser.eval(formula, klines)
        except FormulaError as exc:
            QMessageBox.warning(self, "Error en fórmula", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"No se pudo validar la fórmula: {exc}")
            return

        ind = CompiledIndicator(
            name=name,
            formula=formula,
            color=self._ci_color.color,
            output_type=self._ci_type.currentData(),
            timeframe=self._ci_tf.currentText(),
        )
        self._custom_indicators.append(ind.to_dict())
        self._save_custom()
        self._refresh_custom_list()
        self.custom_indicators_changed.emit(self._custom_indicators)

    def _on_delete_custom(self):
        row = self._ci_list.currentRow()
        if row < 0:
            return
        del self._custom_indicators[row]
        self._save_custom()
        self._refresh_custom_list()
        self.custom_indicators_changed.emit(self._custom_indicators)

    def _on_test_formula(self):
        formula = self._ci_formula.text().strip()
        if not formula:
            QMessageBox.information(self, "Probar fórmula",
                                     "Escribe una fórmula primero.")
            return
        try:
            dummy = [[0, 50000, 50100, 49900, 50050, 100]] * 100
            parser = FormulaParser()
            result = parser.eval(formula, dummy)
            last = result[-1] if len(result) > 0 else 0
            QMessageBox.information(
                self, "✅ Fórmula válida",
                f"Último valor calculado: {last:.4f}\n"
                f"Array length: {len(result)}"
            )
        except FormulaError as exc:
            QMessageBox.warning(self, "❌ Error en fórmula", str(exc))
        except Exception as exc:
            QMessageBox.warning(self, "❌ Error", str(exc))

    def _refresh_custom_list(self):
        self._ci_list.clear()
        for ind in self._custom_indicators:
            name = ind.get("name", "?")
            formula = ind.get("formula", "")
            tf = ind.get("timeframe", "Todos")
            item = QListWidgetItem(f"{name}  |  {formula}  |  {tf}")
            item.setToolTip(
                f"Tipo: {ind.get('output_type', 'line')}\n"
                f"Color: {ind.get('color', '#fff')}"
            )
            self._ci_list.addItem(item)

    def _save_custom(self):
        _save_json(CUSTOM_FILE, self._custom_indicators)

    # ── API pública ────────────────────────────────────────────────────

    def get_flags(self) -> dict:
        """Retorna el dict de flags on/off con colores."""
        result = dict(self._flags)
        for key, (cb, ci) in self._indicator_widgets.items():
            result[key] = cb.isChecked()
            if ci:
                result[f"{key}_color"] = ci.color
        return result

    def get_timeframe(self) -> str:
        return self._timeframe

    def get_custom_indicators(self) -> list:
        return self._custom_indicators

    def apply_flags(self, flags: dict):
        """Actualiza los toggles desde un dict externo."""
        for key, (cb, ci) in self._indicator_widgets.items():
            if key in flags:
                cb.setChecked(bool(flags[key]))
            color_key = f"{key}_color"
            if ci and color_key in flags:
                ci.color = str(flags[color_key])
        self._flags = dict(flags)
