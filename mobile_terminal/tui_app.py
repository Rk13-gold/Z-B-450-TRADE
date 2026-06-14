"""
tui_app.py — Textual-based TUI for BB-450 mobile terminal (Termux).

7 panels: BANNER | PRICE | SIGNAL | NOTIFICATIONS | AI | ACCOUNT | TRADE
Keyboard-driven, full trading capabilities via WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Optional

from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from textual.app import App
from textual.reactive import reactive
from textual.widget import Widget

from config import WS_URI
from ws_client import BB450WSClient

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

RECONNECT_BLINK = 0.5
COLORS = {
    "bg": "#0a0a0a",
    "green": "#00ff66",
    "red": "#ff4444",
    "gold": "#ffcc00",
    "magenta": "#bb00ff",
    "cyan": "#00ccff",
    "orange": "#ff8844",
    "dim": "#555555",
    "white": "#cccccc",
}

# ── Utility ────────────────────────────────────────────────────────────

_notif_severity_colors = {
    "critical": COLORS["red"],
    "warning": COLORS["gold"],
    "info": COLORS["green"],
    "debug": COLORS["dim"],
}


def _format_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    return f"${p:.2f}"


def _format_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _severity_color(sev: str) -> str:
    return _notif_severity_colors.get(sev, COLORS["white"])


# ── Widgets (each with reactive({}) + render()) ─────────────────────

class BannerWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        status = d.get("status", "disconnected")
        connected = status == "connected"
        status_emoji = "\U0001f7e2" if connected else "\U0001f534"
        status_text = "CONNECTED" if connected else "DISCONNECTED"
        host = d.get("host", WS_URI)
        color = COLORS["green"] if connected else COLORS["red"]
        text = Text.assemble(
            (f" {status_emoji} BB-450 Mobile Terminal ", COLORS["magenta"]),
            (f"| {status_text} ", color),
            (f"| {host} ", COLORS["dim"]),
        )
        return Panel(text, style=f"bold {COLORS['bg']}", border_style=color)


class PriceWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        p = d.get("price", 0)
        chg = d.get("change_pct", 0)
        color = COLORS["green"] if chg >= 0 else COLORS["red"]
        arrow = "\u25b2" if chg >= 0 else "\u25bc"
        text = Text.assemble(
            (f" {_format_price(p)} ", f"bold {COLORS['white']}"),
            (f"{arrow} {chg:+.2f}% ", color),
            (f"\n  H: {_format_price(d.get('day_high', 0))}  ", COLORS["dim"]),
            (f"L: {_format_price(d.get('day_low', 0))}", COLORS["dim"]),
        )
        return Panel(text, title="PRICE", border_style=color)


class SignalWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        conf = d.get("confidence", 0)
        regime = d.get("regimen_mercado", "")

        if sig == "LONG":
            sig_color = COLORS["green"]
            sig_text = f"\U0001f7e2 LONG {conf:.0f}%"
        elif sig == "SHORT":
            sig_color = COLORS["red"]
            sig_text = f"\U0001f7e3 SHORT {conf:.0f}%"
        else:
            sig_color = COLORS["gold"]
            sig_text = f"\u26aa WAIT"

        text = Text.assemble(
            (f" {sig_text} ", f"bold {sig_color}"),
            (f"\n  Regimen: {regime[:25]}", COLORS["dim"]),
        )
        return Panel(text, title="SIGNAL", border_style=sig_color)


class NotificationWidget(Widget):
    data = reactive({"items": []})

    def render(self) -> Panel:
        items = self.data.get("items", [])
        if not items:
            return Panel(" No notifications yet", title="NOTIFICATIONS",
                         border_style=COLORS["dim"])
        text = Text()
        for n in items[-8:]:
            sev = n.get("severity", "info")
            ts = n.get("timestamp", 0)
            title = n.get("title", "")
            color = _severity_color(sev)
            text.append(f" {_format_time(ts)} ", COLORS["dim"])
            text.append(f"{title}\n", color)
        return Panel(text, title="NOTIFICATIONS", border_style=COLORS["cyan"])


class AIWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        text_str = d.get("text", "")
        regime = d.get("regime", "")
        if not text_str:
            return Panel(" Awaiting AI analysis...", title="AI ANALYSIS",
                         border_style=COLORS["dim"])
        t = Text()
        if regime:
            t.append(f" Regime: {regime}\n\n", COLORS["gold"])
        t.append(f" {text_str[:200]}", COLORS["white"])
        if len(text_str) > 200:
            t.append("\n ...", COLORS["dim"])
        return Panel(t, title="AI ANALYSIS", border_style=COLORS["magenta"])


class AccountWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        bal = d.get("balance", 0)
        pos = d.get("position")
        funding = d.get("funding_rate", 0)
        oi = d.get("oi_delta_5min", 0)

        text = Text()
        text.append(f" Balance: ${bal:,.2f}\n", COLORS["green"])
        text.append(f" Funding: {funding:+.4f}%  ", COLORS["dim"])
        text.append(f"OI Delta: {oi:+.1f}%\n", COLORS["dim"])

        if pos:
            side = pos.get("direction", "?")
            qty = pos.get("amt", 0)
            entry = pos.get("entry_price", 0)
            pnl = pos.get("pnl", 0)
            pnl_color = COLORS["green"] if pnl >= 0 else COLORS["red"]
            text.append(f" Position: {side} {abs(qty):.4f} BTC\n", COLORS["gold"])
            text.append(f" Entry: ${entry:,.0f}  ", COLORS["dim"])
            text.append(f"PnL: ${pnl:+,.2f}", pnl_color)
        else:
            text.append(" No open position", COLORS["dim"])

        return Panel(text, title="ACCOUNT", border_style=COLORS["gold"])


class DiagnosticsWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        reasons = d.get("reasons", [])
        if not reasons:
            return Panel(" No active blocks", title="DIAGNOSTICS",
                         border_style=COLORS["dim"])
        text = Text()
        for r in reasons[:4]:
            text.append(f" \u26a0 {r}\n", COLORS["orange"])
        if len(reasons) > 4:
            text.append(f" ... y {len(reasons)-4} mas", COLORS["dim"])
        return Panel(text, title="DIAGNOSTICS", border_style=COLORS["orange"])


class TradeWidget(Widget):
    """Interactive trade execution panel.

    Keyboard-driven:
      Tab       → move between fields
      Up/Down   → change value (direction toggle, increment/decrement)
      Enter     → execute trade
    """

    data = reactive({
        "direction": "LONG",
        "sl": "",
        "tp": "",
        "leverage": 40,
        "risk_pct": 1.0,
        "split": False,
        "focus": 0,
        "status": "",
    })

    def __init__(self, client: BB450WSClient, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client

    def render(self) -> Panel:
        d = self.data
        dir_text = f"[{'LONG' if d['direction'] == 'LONG' else 'SHORT'}]"
        dir_color = COLORS["green"] if d["direction"] == "LONG" else COLORS["red"]
        focus = d.get("focus", 0)

        lines = []
        # Direction
        sel = " <" if focus == 0 else "  "
        lines.append(f"{sel}DIR: {dir_text} (TAB to change){sel}")
        # SL
        sel = " <" if focus == 1 else "  "
        sl_val = d.get("sl", "") or "auto"
        lines.append(f"{sel}SL: {sl_val}{sel}")
        # TP
        sel = " <" if focus == 2 else "  "
        tp_val = d.get("tp", "") or "auto"
        lines.append(f"{sel}TP: {tp_val}{sel}")
        # Leverage
        sel = " <" if focus == 3 else "  "
        lines.append(f"{sel}LEV: {d['leverage']}x{sel}")
        # Risk %
        sel = " <" if focus == 4 else "  "
        lines.append(f"{sel}RISK: {d['risk_pct']:.1f}%{sel}")
        # Split entry
        sel = " <" if focus == 5 else "  "
        split_txt = "YES" if d["split"] else "NO"
        lines.append(f"{sel}SPLIT: {split_txt}{sel}")

        status = d.get("status", "")
        if status:
            lines.append(f"\n {status}")

        text = Text("\n".join(lines))
        return Panel(text, title="TRADE", border_style=COLORS["white"])

    async def on_key(self, event):
        """Handle keyboard input for the trade widget."""
        focus = self.data.get("focus", 0)
        max_focus = 5

        if event.key == "tab":
            focus = (focus + 1) % (max_focus + 1) if focus < max_focus else 0
            self.data["focus"] = focus
            event.stop()
            self.refresh()

        elif event.key == "up" or event.key == "down":
            direction = 1 if event.key == "up" else -1
            if focus == 0:  # direction toggle
                self.data["direction"] = "SHORT" if self.data["direction"] == "LONG" else "LONG"
            elif focus == 5:  # split toggle
                self.data["split"] = not self.data.get("split", False)
            else:
                step = {1: 10, 2: 10, 3: 5, 4: 0.1}.get(focus, 1)
                key_map = {1: "sl", 2: "tp", 3: "leverage", 4: "risk_pct"}
                if focus in key_map:
                    current = self.data.get(key_map[focus], 0)
                    if isinstance(current, str):
                        try:
                            current = float(current) if current else 0
                        except ValueError:
                            current = 0
                    new_val = current + step * direction
                    if focus == 3:  # leverage
                        new_val = max(1, min(100, new_val))
                    elif focus == 4:  # risk pct
                        new_val = max(0.1, min(100, new_val))
                    elif focus in (1, 2):  # sl/tp
                        new_val = max(0, new_val)
                    self.data[key_map[focus]] = str(int(new_val)) if focus == 3 else f"{new_val:.1f}"
            event.stop()
            self.refresh()

        elif event.key == "enter":
            event.stop()
            await self._execute()

        elif event.key == "escape":
            self.data["status"] = ""
            self.refresh()

    async def _execute(self):
        direction = self.data.get("direction", "LONG")
        try:
            sl = float(self.data.get("sl", 0)) if self.data.get("sl", "") else 0
        except ValueError:
            sl = 0
        try:
            tp = float(self.data.get("tp", 0)) if self.data.get("tp", "") else 0
        except ValueError:
            tp = 0
        leverage = self.data.get("leverage", 40)
        risk_pct = self.data.get("risk_pct", 1.0)
        split = self.data.get("split", False)

        exec_dir = "ALZA" if direction == "LONG" else "BAJA"
        ok = await self._client.trade(
            direction=exec_dir, sl=sl, tp=tp,
            leverage=leverage, risk_pct=risk_pct,
            split=split,
        )
        self.data["status"] = "\u2705 Order sent!" if ok else "\u274c Send failed"
        self.refresh()


# ── Main App ───────────────────────────────────────────────────────

class BB450MobileApp(App):
    CSS = """
    Screen { background: #0a0a0a; }
    BannerWidget { height: 3; }
    PriceWidget { height: 5; }
    SignalWidget { height: 4; }
    NotificationWidget { height: 10; }
    AIWidget { height: 8; }
    AccountWidget { height: 6; }
    DiagnosticsWidget { height: 5; }
    TradeWidget { height: 12; }
    """

    def __init__(self):
        super().__init__()
        self._client = BB450WSClient()
        self._notifications: deque = deque(maxlen=100)
        self._ws_task: Optional[asyncio.Task] = None

    def compose(self):
        """Layout: 3 columns top, full-width panels below."""
        yield BannerWidget()
        yield PriceWidget()
        yield SignalWidget()
        yield NotificationWidget()
        yield AIWidget()
        yield AccountWidget()
        yield DiagnosticsWidget()
        yield TradeWidget(client=self._client)

    def on_mount(self):
        self.set_interval(0.5, self._blink_reconnect)
        self._ws_task = asyncio.create_task(self._run_client())

        # Wire client callbacks
        self._client.on_market_state = self._on_market_state
        self._client.on_notification = self._on_notification
        self._client.on_command_ack = self._on_command_ack
        self._client.on_status = self._on_ws_status

    async def _run_client(self):
        await self._client.connect()

    # ── Callbacks ──────────────────────────────────────────────────

    def _on_market_state(self, data: dict):
        if not self.is_mounted:
            return
        self.query_one(PriceWidget).data = {
            "price": data.get("price", 0),
            "change_pct": data.get("change_pct", 0),
            "day_high": data.get("day_high", 0),
            "day_low": data.get("day_low", 0),
            "funding_rate": data.get("funding_rate", 0),
            "oi_delta_5min": data.get("oi_delta_5min", 0),
        }
        self.query_one(SignalWidget).data = {
            "signal": data.get("signal", "NEUTRAL"),
            "confidence": data.get("confidence", 0),
            "regimen_mercado": data.get("regimen_mercado", ""),
        }
        self.query_one(AccountWidget).data = {
            "balance": data.get("balance", 0),
            "position": data.get("position"),
            "funding_rate": data.get("funding_rate", 0),
            "oi_delta_5min": data.get("oi_delta_5min", 0),
        }
        diag = data.get("signal_diagnostics", [])
        if diag:
            self.query_one(DiagnosticsWidget).data = {"reasons": diag}

    def _on_notification(self, notif: dict):
        if not self.is_mounted:
            return
        self._notifications.append(notif)
        self.query_one(NotificationWidget).data = {
            "items": list(self._notifications),
        }

        # Route to AI panel
        if notif.get("category") in ("ai_analysis", "sentiment"):
            self.query_one(AIWidget).data = {
                "text": notif.get("body", ""),
                "regime": notif.get("data", {}).get("regime", ""),
            }

    def _on_command_ack(self, action: str, status: str, result: dict):
        if not self.is_mounted:
            return
        tw = self.query_one(TradeWidget)
        if status == "ok":
            tw.data["status"] = f"\u2705 {action} OK"
        else:
            tw.data["status"] = f"\u274c {action} FAILED: {result.get('message', '')}"
        tw.refresh()

    def _on_ws_status(self, connected: bool):
        if not self.is_mounted:
            return
        self.query_one(BannerWidget).data = {
            "status": "connected" if connected else "disconnected",
            "host": WS_URI,
        }

    def _blink_reconnect(self):
        """Periodic refresh to keep UI alive."""
        pass


def run():
    app = BB450MobileApp()
    app.run()


if __name__ == "__main__":
    run()
