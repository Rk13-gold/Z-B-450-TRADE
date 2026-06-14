from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections import deque
from typing import Any, Optional

from rich.panel import Panel
from rich.text import Text
from textual.app import App
from textual.reactive import reactive
from textual.widget import Widget

from config import WS_URI, SOUND_LONG, SOUND_SHORT, SOUND_CLOSE, NOTIFY_ENABLED

log = logging.getLogger(__name__)

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


def _notify_android(title: str, body: str):
    if not NOTIFY_ENABLED:
        return
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", body,
             "--led-color", "ff0000", "--priority", "high", "--sound"],
            timeout=2, capture_output=True,
        )
    except Exception:
        pass


def _play_sound(wav_path: str):
    if not NOTIFY_ENABLED or not wav_path:
        return
    try:
        subprocess.run(
            ["termux-media-player", "play", wav_path],
            timeout=2, capture_output=True,
        )
    except Exception:
        pass


# ── Widgets ────────────────────────────────────────────────────────

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
            sig_text = "\u26aa WAIT"

        text = Text.assemble(
            (f" {sig_text} ", f"bold {sig_color}"),
            (f"\n  Regimen: {regime[:25]}", COLORS["dim"]),
        )
        return Panel(text, title="SIGNAL", border_style=sig_color)


class NarrativeWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        bv = d.get("buy_volume", 0)
        sv = d.get("sell_volume", 0)
        total = bv + sv + 0.001
        delta = bv - sv
        delta_pct = (delta / total) * 100
        tick_sp = d.get("tick_speed", 0)
        hft = d.get("hft_speed", 0)
        cvd = d.get("cvd", 0)
        depth_imb = d.get("depth_imb_pct", 0)
        spoof = d.get("spoofing_risk", 0)
        decision = d.get("decision", "")
        active_trap = d.get("active_trap", "")

        lines = []

        # Whale sonar
        if abs(delta_pct) > 35 and total > 3:
            dir_emoji = "\U0001f7e3" if delta > 0 else "\U0001f534"
            w_text = f"{dir_emoji} BALLENA {'COMPRADORA' if delta > 0 else 'VENDEDORA'}"
            lines.append(f"  {w_text} \u0394 {delta:+.1f}\u20bf ({delta_pct:+.1f}%)")
        elif abs(delta_pct) > 15 and total > 3:
            dir_emoji = "\U0001f7e2" if delta > 0 else "\U0001f7e3"
            w_text = f"{dir_emoji} AGRESION {'COMPRADORA' if delta > 0 else 'VENDEDORA'}"
            lines.append(f"  {w_text} \u0394 {delta:+.1f}\u20bf ({delta_pct:+.1f}%)")
        else:
            lines.append(f"  \u26aa Vol: {total:.1f}\u20bf  \u0394 {delta:+.1f}\u20bf")

        hft_color = COLORS["red"] if hft > 5 else COLORS["gold"] if hft > 2 else COLORS["dim"]
        lines.append(f"   HFT: {hft:.1f}/s  Tick: {tick_sp:.0f}/s  Spoof: {spoof:.0f}%")

        # Traps
        if active_trap:
            lines.append(f"  \u26a0\ufe0f {active_trap[:40]}")

        # CVD + Depth imbalance
        cvd_label = "BULLISH" if cvd > 2 else "BEARISH" if cvd < -2 else "FLAT"
        cvd_color = COLORS["green"] if cvd > 2 else COLORS["red"] if cvd < -2 else COLORS["dim"]
        lines.append(f"  CVD: {cvd_label} ({cvd:+.1f})  Depth: {depth_imb:+.1f}%")

        # Decision
        if decision:
            if "LONG" in decision:
                d_color = COLORS["green"]
            elif "SHORT" in decision:
                d_color = COLORS["red"]
            elif "TRAMPA" in decision:
                d_color = COLORS["red"]
            elif "PARCIAL" in decision:
                d_color = COLORS["gold"]
            else:
                d_color = COLORS["dim"]
            lines.append(f"  {decision[:50]}")

        text = Text("\n".join(lines))
        border_col = COLORS["cyan"]
        if "TRAMPA" in decision:
            border_col = COLORS["red"]
        elif "LONG CONFIRMADO" in decision:
            border_col = COLORS["green"]
        elif "SHORT CONFIRMADO" in decision:
            border_col = COLORS["red"]
        return Panel(text, title="INSTITUTIONAL NARRATIVE", border_style=border_col)


class ActionWidget(Widget):
    data = reactive({
        "signal": "NEUTRAL",
        "decision": "",
        "in_position": False,
        "bidir": "ALZA",
    })

    def __init__(self, client: BB450WSClient, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        decision = d.get("decision", "")
        in_pos = d.get("in_position", False)

        lines = []
        show_long = False
        show_short = False
        show_close = False

        if "LONG CONFIRMADO" in decision or sig == "LONG":
            show_long = True
        if "SHORT CONFIRMADO" in decision or sig == "SHORT":
            show_short = True
        if "TRAMPA" in decision and in_pos:
            show_close = True
        if in_pos:
            show_close = True
        if "PARCIAL" not in decision and "SIN VENTAJA" not in decision:
            if not show_long and not show_short and not in_pos:
                show_long = True
                show_short = True

        if show_long:
            lines.append("  [\U0001f7e2 ENTRAR LONG]  (presiona 1)")
        if show_short:
            lines.append("  [\U0001f7e3 ENTRAR SHORT] (presiona 2)")
        if show_close:
            lines.append("  [\U0001f534 CERRAR POS]  (presiona 3)")

        if not lines:
            lines.append("  \u26aa Esperando se\u00f1al...")

        text = Text("\n".join(lines))
        border_col = COLORS["gold"]
        if show_long and not show_short:
            border_col = COLORS["green"]
        elif show_short and not show_long:
            border_col = COLORS["red"]
        return Panel(text, title="QUICK ACTIONS", border_style=border_col)

    async def action_long(self):
        ok = await self._client.trade(
            direction="ALZA", sl=0, tp=0,
            leverage=40, risk_pct=1.0, split=False,
        )
        self.app.query_one(TradeWidget).data["status"] = \
            "\u2705 LONG ENVIADA" if ok else "\u274c LONG FALLIDA"
        self.app.query_one(TradeWidget).refresh()

    async def action_short(self):
        ok = await self._client.trade(
            direction="BAJA", sl=0, tp=0,
            leverage=40, risk_pct=1.0, split=False,
        )
        self.app.query_one(TradeWidget).data["status"] = \
            "\u2705 SHORT ENVIADA" if ok else "\u274c SHORT FALLIDA"
        self.app.query_one(TradeWidget).refresh()

    async def action_close(self):
        ok = await self._client.close_all()
        self.app.query_one(TradeWidget).data["status"] = \
            "\u2705 CIERRE ENVIADO" if ok else "\u274c CIERRE FALLIDO"
        self.app.query_one(TradeWidget).refresh()


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
        for r in reasons[:3]:
            text.append(f" \u26a0 {r}\n", COLORS["orange"])
        if len(reasons) > 3:
            text.append(f" ... y {len(reasons)-3} mas", COLORS["dim"])
        return Panel(text, title="DIAGNOSTICS", border_style=COLORS["orange"])


class TradeWidget(Widget):
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
        focus = d.get("focus", 0)

        lines = []
        sel = " <" if focus == 0 else "  "
        lines.append(f"{sel}DIR: {dir_text} (TAB){sel}")
        sel = " <" if focus == 1 else "  "
        sl_val = d.get("sl", "") or "auto"
        lines.append(f"{sel}SL: {sl_val}{sel}")
        sel = " <" if focus == 2 else "  "
        tp_val = d.get("tp", "") or "auto"
        lines.append(f"{sel}TP: {tp_val}{sel}")
        sel = " <" if focus == 3 else "  "
        lines.append(f"{sel}LEV: {d['leverage']}x{sel}")
        sel = " <" if focus == 4 else "  "
        lines.append(f"{sel}RISK: {d['risk_pct']:.1f}%{sel}")
        sel = " <" if focus == 5 else "  "
        split_txt = "YES" if d["split"] else "NO"
        lines.append(f"{sel}SPLIT: {split_txt}{sel}")

        status = d.get("status", "")
        if status:
            lines.append(f"\n {status}")

        text = Text("\n".join(lines))
        return Panel(text, title="TRADE", border_style=COLORS["white"])

    async def on_key(self, event):
        focus = self.data.get("focus", 0)
        max_focus = 5

        if event.key == "tab":
            focus = (focus + 1) % (max_focus + 1) if focus < max_focus else 0
            self.data["focus"] = focus
            event.stop()
            self.refresh()

        elif event.key in ("up", "down"):
            direction = 1 if event.key == "up" else -1
            if focus == 0:
                self.data["direction"] = "SHORT" if self.data["direction"] == "LONG" else "LONG"
            elif focus == 5:
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
                    if focus == 3:
                        new_val = max(1, min(100, new_val))
                    elif focus == 4:
                        new_val = max(0.1, min(100, new_val))
                    elif focus in (1, 2):
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
        if ok:
            _play_sound(SOUND_LONG if direction == "LONG" else SOUND_SHORT)
        self.refresh()


# ── Main App ───────────────────────────────────────────────────────

class BB450MobileApp(App):
    CSS = """
    Screen { background: #0a0a0a; }
    BannerWidget { height: 3; }
    PriceWidget { height: 4; }
    SignalWidget { height: 3; }
    NarrativeWidget { height: 8; }
    ActionWidget { height: 4; }
    NotificationWidget { height: 6; }
    AIWidget { height: 5; }
    AccountWidget { height: 4; }
    DiagnosticsWidget { height: 3; }
    TradeWidget { height: 12; }
    """

    def __init__(self):
        super().__init__()
        self._client = BB450WSClient()
        self._notifications: deque = deque(maxlen=100)
        self._ws_task: Optional[asyncio.Task] = None
        self._prev_signal = "NEUTRAL"
        self._prev_decision = ""

    def compose(self):
        yield BannerWidget()
        yield PriceWidget()
        yield SignalWidget()
        yield NarrativeWidget()
        yield ActionWidget(client=self._client)
        yield NotificationWidget()
        yield AIWidget()
        yield AccountWidget()
        yield DiagnosticsWidget()
        yield TradeWidget(client=self._client)

    def on_mount(self):
        self.set_interval(0.5, self._blink_reconnect)
        self._ws_task = asyncio.create_task(self._run_client())

        self._client.on_market_state = self._on_market_state
        self._client.on_notification = self._on_notification
        self._client.on_command_ack = self._on_command_ack
        self._client.on_status = self._on_ws_status

    async def _run_client(self):
        await self._client.connect()

    # ── Keyboard shortcuts ─────────────────────────────────────────

    def key_b(self):
        self._quick_long()

    def key_s(self):
        self._quick_short()

    def key_c(self):
        self._quick_close()

    def key_q(self):
        self.exit()

    def key_1(self):
        self._quick_long()

    def key_2(self):
        self._quick_short()

    def key_3(self):
        self._quick_close()

    def _quick_long(self):
        asyncio.create_task(self._do_quick_long())

    async def _do_quick_long(self):
        ok = await self._client.trade(
            direction="ALZA", sl=0, tp=0,
            leverage=40, risk_pct=1.0, split=False,
        )
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 LONG RAPIDA" if ok else "\u274c LONG FALLIDA"
        tw.refresh()
        if ok:
            _play_sound(SOUND_LONG)
            _notify_android("BB-450 LONG", "Orden LONG enviada")

    def _quick_short(self):
        asyncio.create_task(self._do_quick_short())

    async def _do_quick_short(self):
        ok = await self._client.trade(
            direction="BAJA", sl=0, tp=0,
            leverage=40, risk_pct=1.0, split=False,
        )
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 SHORT RAPIDA" if ok else "\u274c SHORT FALLIDA"
        tw.refresh()
        if ok:
            _play_sound(SOUND_SHORT)
            _notify_android("BB-450 SHORT", "Orden SHORT enviada")

    def _quick_close(self):
        asyncio.create_task(self._do_quick_close())

    async def _do_quick_close(self):
        ok = await self._client.close_all()
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 CIERRE RAPIDO" if ok else "\u274c CIERRE FALLIDO"
        tw.refresh()
        if ok:
            _play_sound(SOUND_CLOSE)
            _notify_android("BB-450 CLOSE", "Posicion cerrada")

    # ── Callbacks ──────────────────────────────────────────────────

    def _on_market_state(self, data: dict):
        if not self.is_mounted:
            return
        self.query_one(PriceWidget).data = {
            "price": data.get("price", 0),
            "change_pct": data.get("change_pct", 0),
            "day_high": data.get("day_high", 0),
            "day_low": data.get("day_low", 0),
        }
        self.query_one(SignalWidget).data = {
            "signal": data.get("signal", "NEUTRAL"),
            "confidence": data.get("confidence", 0),
            "regimen_mercado": data.get("regimen_mercado", ""),
        }
        self.query_one(NarrativeWidget).data = {
            "buy_volume": data.get("buy_volume", 0),
            "sell_volume": data.get("sell_volume", 0),
            "tick_speed": data.get("tick_speed", 0),
            "hft_speed": data.get("hft_speed", 0),
            "cvd": data.get("cvd", 0),
            "depth_imb_pct": data.get("depth_imb_pct", 0),
            "spoofing_risk": data.get("spoofing_risk", 0),
            "decision": data.get("decision", ""),
            "active_trap": data.get("active_trap", ""),
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

        sig = data.get("signal", "NEUTRAL")
        decision = data.get("decision", "")
        in_pos = data.get("position") is not None
        self.query_one(ActionWidget).data = {
            "signal": sig,
            "decision": decision,
            "in_position": in_pos,
        }

        if sig != self._prev_signal and sig in ("LONG", "SHORT"):
            _play_sound(SOUND_LONG if sig == "LONG" else SOUND_SHORT)
            _notify_android(f"BB-450 SENAL {sig}", f"Confianza: {data.get('confidence', 0):.0f}%")
            self._prev_signal = sig

        if decision != self._prev_decision and decision:
            if "TRAMPA" in decision:
                _notify_android("BB-450 TRAMPA", decision[:80])
            elif "CONFIRMADO" in decision:
                _notify_android(f"BB-450 {decision}", "Se\u00f1al confirmada")
            self._prev_decision = decision

    def _on_notification(self, notif: dict):
        if not self.is_mounted:
            return
        self._notifications.append(notif)
        self.query_one(NotificationWidget).data = {
            "items": list(self._notifications),
        }
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
        pass


def run():
    app = BB450MobileApp()
    app.run()


if __name__ == "__main__":
    run()
