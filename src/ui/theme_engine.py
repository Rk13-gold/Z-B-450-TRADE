"""
theme_engine.py — Sistema de temas global para BB-450.

ThemeManager es un singleton que gestiona temas predefinidos
y emite una señal cuando el tema cambia para que todos los
widgets se refresquen automáticamente.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

THEMES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "themes"
PREFS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "theme_preference.json"

# ── Temas integrados (fallback si no existen los archivos JSON) ────────────
BUILTIN_THEMES = {
    "classic": {
        "name": "🌑 Classic",
        "chart": {
            "bg": "#000000",
            "candle_bull": "#00ff66",
            "candle_bear": "#bb00ff",
            "candle_bull_wick": "#00ff66",
            "candle_bear_wick": "#bb00ff",
            "grid": "#1a1a1a",
            "grid_major": "#2a2a2a",
            "text": "#ffffff",
            "text_dim": "#666666",
            "vwap": "#ffcc00",
            "ema_9": "#00aaff",
            "ema_21": "#ff8800",
            "dpoc": "#ff66ff",
            "dpoc_trail": "#440044",
            "imbalance_buy": "rgba(0,255,102,80)",
            "imbalance_sell": "rgba(187,0,255,80)",
            "volume_bull": "#00ff66",
            "volume_bear": "#bb00ff",
            "signal_strip_rvol": "#ffaa00",
            "liquidity_arrow": "#ffffff",
            "pulse_entry": "#00ff66",
            "cone_bull": "rgba(0,255,102,30)",
            "cone_bear": "rgba(187,0,255,30)",
            "footprint_buy": "#00ff66",
            "footprint_sell": "#bb00ff",
            "entry_line": "#ffffff",
            "sl_line": "#ff4444",
            "tp_line": "#00ff88",
            "position_bg_bull": "rgba(0,255,102,15)",
            "position_bg_bear": "rgba(255,68,68,15)",
            "vp_buy": "#00ff66",
            "vp_sell": "#bb00ff",
            "sr_resistance": "#ff8800",
            "sr_support": "#00ff88",
            "ob_bull": "rgba(0,170,255,40)",
            "ob_bear": "rgba(255,68,68,40)",
            "fvg": "rgba(255,204,0,25)",
            "swing_high": "#ff4444",
            "swing_low": "#00ff88",
            "fib_line": "#ffcc00",
            "fib_zone": "rgba(255,204,0,10)",
            "entry_zone": "rgba(0,255,136,15)",
            "ict_killzone": "rgba(255,170,0,8)",
            "oi_imbalance_buy": "#00ff66",
            "oi_imbalance_sell": "#bb00ff",
            "premium_fill": "rgba(255,68,68,8)",
            "discount_fill": "rgba(0,255,136,8)",
        },
        "ui": {
            "panel_bg": "#0d0d1a",
            "panel_border": "#1a1a2e",
            "text_primary": "#ffffff",
            "text_secondary": "#aaaaaa",
            "text_dim": "#666666",
            "accent": "#00ff88",
            "accent2": "#ffaa00",
            "accent3": "#ff4444",
            "accent4": "#0088ff",
            "button_bg": "#1a1a2e",
            "button_hover": "#2a2a3e",
            "input_bg": "#111122",
            "input_border": "#333355",
            "progress_bg": "#1a1a2e",
            "progress_chunk": "#00ff88",
            "badge_bg": "#002211",
            "badge_bear_bg": "#220000",
            "badge_wait_bg": "#111122",
            "log_bg": "#0a0a14",
            "log_text": "#666666",
        },
    },
    "day_trader": {
        "name": "🌞 Day Trader",
        "chart": {
            "bg": "#f5f0e8",
            "candle_bull": "#006600",
            "candle_bear": "#cc0000",
            "candle_bull_wick": "#006600",
            "candle_bear_wick": "#cc0000",
            "grid": "#e0dbd0",
            "grid_major": "#d0cbc0",
            "text": "#222222",
            "text_dim": "#999999",
            "vwap": "#0044cc",
            "ema_9": "#0066ff",
            "ema_21": "#ff6600",
            "dpoc": "#6600cc",
            "dpoc_trail": "#cc88ff",
            "imbalance_buy": "rgba(0,102,0,60)",
            "imbalance_sell": "rgba(204,0,0,60)",
            "volume_bull": "#006600",
            "volume_bear": "#cc0000",
            "signal_strip_rvol": "#cc8800",
            "liquidity_arrow": "#222222",
            "pulse_entry": "#006600",
            "cone_bull": "rgba(0,102,0,20)",
            "cone_bear": "rgba(204,0,0,20)",
            "footprint_buy": "#006600",
            "footprint_sell": "#cc0000",
            "entry_line": "#222222",
            "sl_line": "#ff0000",
            "tp_line": "#008800",
            "position_bg_bull": "rgba(0,102,0,12)",
            "position_bg_bear": "rgba(255,0,0,12)",
            "vp_buy": "#006600",
            "vp_sell": "#cc0000",
            "sr_resistance": "#cc6600",
            "sr_support": "#006600",
            "ob_bull": "rgba(0,102,204,40)",
            "ob_bear": "rgba(204,0,0,40)",
            "fvg": "rgba(0,0,0,20)",
            "swing_high": "#cc0000",
            "swing_low": "#006600",
            "fib_line": "#0044cc",
            "fib_zone": "rgba(0,68,204,10)",
            "entry_zone": "rgba(0,102,0,12)",
            "ict_killzone": "rgba(204,136,0,8)",
            "oi_imbalance_buy": "#006600",
            "oi_imbalance_sell": "#cc0000",
            "premium_fill": "rgba(204,0,0,8)",
            "discount_fill": "rgba(0,102,0,8)",
        },
        "ui": {
            "panel_bg": "#f0ebe3",
            "panel_border": "#d0cbc0",
            "text_primary": "#222222",
            "text_secondary": "#555555",
            "text_dim": "#999999",
            "accent": "#0044cc",
            "accent2": "#cc8800",
            "accent3": "#cc0000",
            "accent4": "#0066cc",
            "button_bg": "#e0dbd0",
            "button_hover": "#d0cbc0",
            "input_bg": "#ffffff",
            "input_border": "#cccccc",
            "progress_bg": "#e0dbd0",
            "progress_chunk": "#0044cc",
            "badge_bg": "#d0ffd0",
            "badge_bear_bg": "#ffd0d0",
            "badge_wait_bg": "#e8e8e8",
            "log_bg": "#faf5ef",
            "log_text": "#888888",
        },
    },
    "neon_pulse": {
        "name": "💙 Neon Pulse",
        "chart": {
            "bg": "#000d1a",
            "candle_bull": "#00ccff",
            "candle_bear": "#ff0066",
            "candle_bull_wick": "#00ccff",
            "candle_bear_wick": "#ff0066",
            "grid": "#001a33",
            "grid_major": "#002244",
            "text": "#e0f0ff",
            "text_dim": "#446688",
            "vwap": "#ffaa00",
            "ema_9": "#00ffcc",
            "ema_21": "#ff6600",
            "dpoc": "#ff00ff",
            "dpoc_trail": "#660066",
            "imbalance_buy": "rgba(0,204,255,70)",
            "imbalance_sell": "rgba(255,0,102,70)",
            "volume_bull": "#00ccff",
            "volume_bear": "#ff0066",
            "signal_strip_rvol": "#ffaa00",
            "liquidity_arrow": "#e0f0ff",
            "pulse_entry": "#00ccff",
            "cone_bull": "rgba(0,204,255,25)",
            "cone_bear": "rgba(255,0,102,25)",
            "footprint_buy": "#00ccff",
            "footprint_sell": "#ff0066",
            "entry_line": "#ffffff",
            "sl_line": "#ff4444",
            "tp_line": "#00ff88",
            "position_bg_bull": "rgba(0,204,255,12)",
            "position_bg_bear": "rgba(255,0,102,12)",
            "vp_buy": "#00ccff",
            "vp_sell": "#ff0066",
            "sr_resistance": "#ff6600",
            "sr_support": "#00ccff",
            "ob_bull": "rgba(0,204,255,35)",
            "ob_bear": "rgba(255,0,102,35)",
            "fvg": "rgba(255,170,0,20)",
            "swing_high": "#ff0066",
            "swing_low": "#00ccff",
            "fib_line": "#ffaa00",
            "fib_zone": "rgba(255,170,0,8)",
            "entry_zone": "rgba(0,204,255,12)",
            "ict_killzone": "rgba(255,170,0,8)",
            "oi_imbalance_buy": "#00ccff",
            "oi_imbalance_sell": "#ff0066",
            "premium_fill": "rgba(255,0,102,8)",
            "discount_fill": "rgba(0,204,255,8)",
        },
        "ui": {
            "panel_bg": "#001122",
            "panel_border": "#002244",
            "text_primary": "#e0f0ff",
            "text_secondary": "#88aacc",
            "text_dim": "#446688",
            "accent": "#00ccff",
            "accent2": "#ffaa00",
            "accent3": "#ff0066",
            "accent4": "#00ffcc",
            "button_bg": "#002244",
            "button_hover": "#003366",
            "input_bg": "#001a33",
            "input_border": "#003366",
            "progress_bg": "#002244",
            "progress_chunk": "#00ccff",
            "badge_bg": "#003322",
            "badge_bear_bg": "#330011",
            "badge_wait_bg": "#001a33",
            "log_bg": "#000d1a",
            "log_text": "#446688",
        },
    },
    "matrix_ambar": {
        "name": "🟠 Matrix Ámbar",
        "chart": {
            "bg": "#0a0800",
            "candle_bull": "#ffaa00",
            "candle_bear": "#ff4400",
            "candle_bull_wick": "#ffaa00",
            "candle_bear_wick": "#ff4400",
            "grid": "#1a1400",
            "grid_major": "#2a2000",
            "text": "#ffdd88",
            "text_dim": "#887744",
            "vwap": "#ffee00",
            "ema_9": "#ffcc44",
            "ema_21": "#ff6600",
            "dpoc": "#ff0088",
            "dpoc_trail": "#550033",
            "imbalance_buy": "rgba(255,170,0,70)",
            "imbalance_sell": "rgba(255,68,0,70)",
            "volume_bull": "#ffaa00",
            "volume_bear": "#ff4400",
            "signal_strip_rvol": "#ffee00",
            "liquidity_arrow": "#ffdd88",
            "pulse_entry": "#ffaa00",
            "cone_bull": "rgba(255,170,0,25)",
            "cone_bear": "rgba(255,68,0,25)",
            "footprint_buy": "#ffaa00",
            "footprint_sell": "#ff4400",
            "entry_line": "#ffffff",
            "sl_line": "#ff4444",
            "tp_line": "#44ff44",
            "position_bg_bull": "rgba(255,170,0,12)",
            "position_bg_bear": "rgba(255,68,0,12)",
            "vp_buy": "#ffaa00",
            "vp_sell": "#ff4400",
            "sr_resistance": "#ff6600",
            "sr_support": "#ffaa00",
            "ob_bull": "rgba(255,170,0,35)",
            "ob_bear": "rgba(255,68,0,35)",
            "fvg": "rgba(255,238,0,20)",
            "swing_high": "#ff4400",
            "swing_low": "#ffaa00",
            "fib_line": "#ffee00",
            "fib_zone": "rgba(255,238,0,8)",
            "entry_zone": "rgba(255,170,0,12)",
            "ict_killzone": "rgba(255,170,0,8)",
            "oi_imbalance_buy": "#ffaa00",
            "oi_imbalance_sell": "#ff4400",
            "premium_fill": "rgba(255,68,0,8)",
            "discount_fill": "rgba(255,170,0,8)",
        },
        "ui": {
            "panel_bg": "#0f0c00",
            "panel_border": "#2a2000",
            "text_primary": "#ffdd88",
            "text_secondary": "#aa8844",
            "text_dim": "#887744",
            "accent": "#ffaa00",
            "accent2": "#ffee00",
            "accent3": "#ff4400",
            "accent4": "#ffcc44",
            "button_bg": "#2a2000",
            "button_hover": "#3a2c00",
            "input_bg": "#1a1400",
            "input_border": "#3a2c00",
            "progress_bg": "#2a2000",
            "progress_chunk": "#ffaa00",
            "badge_bg": "#332200",
            "badge_bear_bg": "#331100",
            "badge_wait_bg": "#1a1400",
            "log_bg": "#0a0800",
            "log_text": "#887744",
        },
    },
    "purple_haze": {
        "name": "🟣 Purple Haze",
        "chart": {
            "bg": "#0d001a",
            "candle_bull": "#aa66ff",
            "candle_bear": "#ff44aa",
            "candle_bull_wick": "#aa66ff",
            "candle_bear_wick": "#ff44aa",
            "grid": "#1a0033",
            "grid_major": "#2a0044",
            "text": "#e0ccff",
            "text_dim": "#664499",
            "vwap": "#ffcc00",
            "ema_9": "#8888ff",
            "ema_21": "#ff66aa",
            "dpoc": "#ff00ff",
            "dpoc_trail": "#660066",
            "imbalance_buy": "rgba(170,102,255,70)",
            "imbalance_sell": "rgba(255,68,170,70)",
            "volume_bull": "#aa66ff",
            "volume_bear": "#ff44aa",
            "signal_strip_rvol": "#ffcc00",
            "liquidity_arrow": "#e0ccff",
            "pulse_entry": "#aa66ff",
            "cone_bull": "rgba(170,102,255,25)",
            "cone_bear": "rgba(255,68,170,25)",
            "footprint_buy": "#aa66ff",
            "footprint_sell": "#ff44aa",
            "entry_line": "#ffffff",
            "sl_line": "#ff4444",
            "tp_line": "#44ff88",
            "position_bg_bull": "rgba(170,102,255,12)",
            "position_bg_bear": "rgba(255,68,170,12)",
            "vp_buy": "#aa66ff",
            "vp_sell": "#ff44aa",
            "sr_resistance": "#ff66aa",
            "sr_support": "#aa66ff",
            "ob_bull": "rgba(136,136,255,35)",
            "ob_bear": "rgba(255,68,170,35)",
            "fvg": "rgba(255,204,0,20)",
            "swing_high": "#ff44aa",
            "swing_low": "#aa66ff",
            "fib_line": "#ffcc00",
            "fib_zone": "rgba(255,204,0,8)",
            "entry_zone": "rgba(170,102,255,12)",
            "ict_killzone": "rgba(255,204,0,8)",
            "oi_imbalance_buy": "#aa66ff",
            "oi_imbalance_sell": "#ff44aa",
            "premium_fill": "rgba(255,68,170,8)",
            "discount_fill": "rgba(170,102,255,8)",
        },
        "ui": {
            "panel_bg": "#12001f",
            "panel_border": "#2a0044",
            "text_primary": "#e0ccff",
            "text_secondary": "#9966bb",
            "text_dim": "#664499",
            "accent": "#cc88ff",
            "accent2": "#ffcc00",
            "accent3": "#ff44aa",
            "accent4": "#8888ff",
            "button_bg": "#2a0044",
            "button_hover": "#3a0055",
            "input_bg": "#1a0033",
            "input_border": "#3a0055",
            "progress_bg": "#2a0044",
            "progress_chunk": "#cc88ff",
            "badge_bg": "#220044",
            "badge_bear_bg": "#330022",
            "badge_wait_bg": "#1a0033",
            "log_bg": "#0d001a",
            "log_text": "#664499",
        },
    },
}


class ThemeManager(QObject):
    """Singleton global que gestiona el tema activo.

    Carga el tema desde ``theme_preference.json`` al iniciar.
    Al cambiar de tema, emite ``theme_changed`` para que todos
    los widgets registrados refresquen sus estilos.
    """

    theme_changed = pyqtSignal(str)   # nombre del tema

    _instance: Optional["ThemeManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._initialized = True
        self._themes: dict = {}
        self._active_name: str = "classic"
        self._widgets: list[tuple[QObject, callable]] = []
        self._load_themes()
        self._load_preference()

    # ── Carga de temas ────────────────────────────────────────────────────

    def _load_themes(self):
        """Carga temas desde JSON en config/themes/ + builtins."""
        self._themes = {}
        for key, data in BUILTIN_THEMES.items():
            self._themes[key] = data
        if THEMES_DIR.exists():
            for fpath in sorted(THEMES_DIR.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    key = fpath.stem
                    self._themes[key] = data
                except Exception as exc:
                    log.warning(f"[Theme] Error loading {fpath.name}: {exc}")

    def _load_preference(self):
        """Carga la preferencia de tema guardada."""
        try:
            if PREFS_FILE.exists():
                data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
                name = data.get("theme", "classic")
                if name in self._themes:
                    self._active_name = name
        except Exception as exc:
            log.warning(f"[Theme] Error loading preference: {exc}")

    def _save_preference(self):
        """Guarda la preferencia de tema."""
        try:
            PREFS_FILE.write_text(
                json.dumps({"theme": self._active_name}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning(f"[Theme] Error saving preference: {exc}")

    # ── API pública ───────────────────────────────────────────────────────

    @property
    def active_name(self) -> str:
        return self._active_name

    @property
    def active(self) -> dict:
        return self._themes.get(self._active_name, self._themes["classic"])

    def get_chart_color(self, key: str, fallback: str = "#ffffff") -> str:
        return self.active.get("chart", {}).get(key, fallback)

    def get_ui_color(self, key: str, fallback: str = "#ffffff") -> str:
        return self.active.get("ui", {}).get(key, fallback)

    def get_color(self, key: str, fallback: str = "#ffffff") -> str:
        """Busca primero en ui, luego en chart (shorthand)."""
        theme = self.active
        return (
            theme.get("ui", {}).get(key)
            or theme.get("chart", {}).get(key)
            or fallback
        )

    @property
    def theme_names(self) -> list[str]:
        return [t.get("name", k) for k, t in self._themes.items()]

    @property
    def theme_keys(self) -> list[str]:
        return list(self._themes.keys())

    def set_theme(self, key: str):
        """Cambia el tema activo y notifica a todos los widgets."""
        if key not in self._themes:
            log.warning(f"[Theme] Unknown theme: {key}")
            return
        self._active_name = key
        self._save_preference()
        self._notify_all()

    def register(self, widget: QObject, apply_fn: callable):
        """Registra un widget para que se refresque al cambiar tema.

        ``apply_fn`` se llama inmediatamente y en cada cambio de tema.
        """
        self._widgets.append((widget, apply_fn))
        try:
            apply_fn()
        except Exception as exc:
            log.warning(f"[Theme] Error applying initial theme to {widget}: {exc}")

    def unregister(self, widget: QObject):
        self._widgets = [(w, fn) for w, fn in self._widgets if w is not widget]

    def _notify_all(self):
        self.theme_changed.emit(self._active_name)
        for widget, apply_fn in self._widgets:
            try:
                apply_fn()
            except RuntimeError:
                pass  # widget deleted
            except Exception as exc:
                log.warning(f"[Theme] Error applying theme: {exc}")


# ── Singleton global ─────────────────────────────────────────────────────
_THEME: ThemeManager | None = None


def theme() -> ThemeManager:
    global _THEME
    if _THEME is None:
        _THEME = ThemeManager()
    return _THEME
