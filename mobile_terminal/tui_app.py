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
from ws_client import BB450WSClient

log = logging.getLogger(__name__)

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


def _format_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    return f"${p:.2f}"


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


def _bar_segments(pct: float, total_chars: int = 16) -> tuple[str, str]:
    filled = int(total_chars * pct / 100)
    empty = total_chars - filled
    return "\u2588" * filled, "\u2591" * empty


def _price_dist(current: float, wall_price: float) -> str:
    if current <= 0 or wall_price <= 0:
        return ""
    dist = (wall_price / current - 1) * 100
    return f"{dist:+.2f}%"


ALIGN_RIGHT = 40


# ── Widgets ────────────────────────────────────────────────────────

class BannerWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        connected = d.get("status") == "connected"
        emoji = "\U0001f7e2" if connected else "\U0001f534"
        status = "CONNECTED" if connected else "DISCONNECTED"
        color = COLORS["green"] if connected else COLORS["red"]
        host = d.get("host", WS_URI)
        port = d.get("port", "")
        text = Text.assemble(
            (f" {emoji} BB-450 ", COLORS["magenta"]),
            (f"| {status} ", color),
            (f"| {host} ", COLORS["dim"]),
            (f":{port}" if port else "", COLORS["dim"]),
        )
        return Panel(text, style=f"bold {COLORS['bg']}", border_style=color)


class PriceBarWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        p = d.get("price", 0)
        chg = d.get("change_pct", 0)
        color = COLORS["green"] if chg >= 0 else COLORS["red"]
        arrow = "\u25b2" if chg >= 0 else "\u25bc"
        hl = d.get("high", 0)
        ll = d.get("low", 0)
        text = Text.assemble(
            (f" {_format_price(p)} ", f"bold {COLORS['white']}"),
            (f"{arrow} {chg:+.2f}% ", color),
            (f"\n  H: {_format_price(hl)}  ", COLORS["dim"]),
            (f"L: {_format_price(ll)}", COLORS["dim"]),
        )
        return Panel(text, title="PRICE", border_style=color)


class StrengthBarWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        conf = d.get("confidence", 0)
        pressure = d.get("buy_pressure", 50)
        short_p = 100 - pressure
        regime = d.get("regime", "")

        if sig == "LONG":
            sig_color = COLORS["green"]
            sig_emoji = "\U0001f7e2"
        elif sig == "SHORT":
            sig_color = COLORS["red"]
            sig_emoji = "\U0001f7e3"
        else:
            sig_color = COLORS["gold"]
            sig_emoji = "\u26aa"

        long_fill, long_empty = _bar_segments(pressure)
        text = Text()
        text.append(f" LONG {pressure:.0f}% ", "bold green")
        text.append(long_fill, "green")
        text.append(long_empty, COLORS["dim"])
        text.append(f" {short_p:.0f}% SHORT", "bold red")
        text.append(f"\n  {sig_emoji} {sig} {conf:.0f}%", f"bold {sig_color}")
        if regime:
            text.append(f" \u00b7 {regime[:20]}", COLORS["dim"])

        border_col = sig_color
        return Panel(text, title="STRENGTH", border_style=border_col)


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
        ba = d.get("ba_ratio", 1.0)
        depth_imb = d.get("depth_imb_pct", 0)
        spoof = d.get("spoofing_risk", 0)
        decision = d.get("decision", "")
        active_trap = d.get("active_trap", "")

        lines = []

        # Whale sonar
        if abs(delta_pct) > 35 and total > 3:
            dir_emoji = "\U0001f7e3" if delta > 0 else "\U0001f534"
            w_text = f"{dir_emoji} BALLENA {'COMPRADORA' if delta > 0 else 'VENDEDORA'}"
            lines.append(f"  {w_text} \u0394{delta:+.1f}\u20bf ({delta_pct:+.1f}%)")
        elif abs(delta_pct) > 15 and total > 3:
            dir_emoji = "\U0001f7e2" if delta > 0 else "\U0001f7e3"
            w_text = f"{dir_emoji} AGRESION {'COMPRADORA' if delta > 0 else 'VENDEDORA'} \u0394"
            lines.append(f"  {w_text} {delta:+.1f}\u20bf ({delta_pct:+.1f}%)")
        else:
            lines.append(f"  \u26aa Vol: {total:.1f}\u20bf  \u0394{delta:+.1f}\u20bf")

        # Microstructure line
        hft_color = COLORS["red"] if hft > 5 else COLORS["gold"] if hft > 2 else COLORS["dim"]
        lines.append(f"   HFT: {hft:.1f}/s  Tick: {tick_sp:.0f}/s  Spoof: {spoof:.0f}%")

        # B/A ratio
        ba_color = COLORS["green"] if ba > 1.2 else COLORS["red"] if ba < 0.8 else COLORS["dim"]
        lines.append(f"   B/A: {ba:.2f}x  CVD: {cvd:+.1f}  Depth: {depth_imb:+.1f}%")

        # Trap alert
        if active_trap:
            lines.append(f"  \u26a0\ufe0f {active_trap[:45]}")

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
            lines.append(f"   {decision[:50]}")

        text = Text("\n".join(lines))
        border_col = COLORS["cyan"]
        if "TRAMPA" in decision:
            border_col = COLORS["red"]
        elif "LONG CONFIRMADO" in decision:
            border_col = COLORS["green"]
        elif "SHORT CONFIRMADO" in decision:
            border_col = COLORS["red"]
        return Panel(text, title="INSTITUTIONAL NARRATIVE", border_style=border_col)


class WhaleWallWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        price = d.get("price", 0)
        bid_walls: list = d.get("bid_walls", [])
        ask_walls: list = d.get("ask_walls", [])

        lines = []
        max_rows = max(len(bid_walls), len(ask_walls), 1)
        for i in range(max_rows):
            left = ""
            right = ""
            if i < len(bid_walls):
                w = bid_walls[i]
                w_price = float(w[0]) if isinstance(w, (list, tuple)) else 0
                w_qty = float(w[1]) if isinstance(w, (list, tuple)) else 0
                dist = _price_dist(price, w_price)
                left = f"\U0001f40b {w_qty:.1f}\u20bf @ {_format_price(w_price)} ({dist})"
            if i < len(ask_walls):
                w = ask_walls[i]
                w_price = float(w[0]) if isinstance(w, (list, tuple)) else 0
                w_qty = float(w[1]) if isinstance(w, (list, tuple)) else 0
                dist = _price_dist(price, w_price)
                right = f"\U0001f43b {w_qty:.1f}\u20bf @ {_format_price(w_price)} ({dist})"

            spacer = " " * max(1, ALIGN_RIGHT - len(left))
            lines.append(f"  {left}{spacer}{right}")

        if not bid_walls and not ask_walls:
            lines.append("  \u26aa No institutional walls detected")

        text = Text()
        for line in lines:
            text.append(f"{line}\n")

        border_col = COLORS["gold"] if bid_walls or ask_walls else COLORS["dim"]
        return Panel(text, title="WHALE WALLS", border_style=border_col)


class ImbalanceWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        imb = d.get("imbalance", 0)
        depth_imb = d.get("depth_imb_pct", 0)
        ba = d.get("ba_ratio", 1.0)
        bid_vol = d.get("book_depth_bids_volume", 0)
        ask_vol = d.get("book_depth_asks_volume", 0)

        # Map imbalance (-1..+1) to 0..100%
        imb_centered = max(0, min(100, (imb + 1) * 50))
        bid_fill, bid_empty = _bar_segments(imb_centered)

        # Color by direction
        bar_color = COLORS["green"] if imb > 0.2 else COLORS["red"] if imb < -0.2 else COLORS["dim"]

        text = Text()
        text.append(f"  BIDS ", COLORS["green"])
        text.append(bid_fill, bar_color)
        text.append(bid_empty, COLORS["dim"])
        text.append(f" ASKS", COLORS["red"])

        detail = f"  Depth: {depth_imb:+.1f}%  B/A: {ba:.2f}x  {bid_vol:.1f}/{ask_vol:.1f}\u20bf"
        text.append(f"\n  {detail}", COLORS["dim"])

        border_col = bar_color if abs(imb) > 0.2 else COLORS["dim"]
        return Panel(text, title="ORDER BOOK IMBALANCE", border_style=border_col)


class AccountWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        bal = d.get("balance", 0)
        pos = d.get("position")
        funding = d.get("funding_rate", 0)
        oi = d.get("oi_delta_5min", 0)

        text = Text()
        text.append(f" Balance: ${bal:,.2f}", COLORS["green"])
        text.append(f"  Funding: {funding:+.4f}%", COLORS["dim"])
        text.append(f"  OI: {oi:+.1f}%", COLORS["dim"])

        if pos:
            side = pos.get("direction", "?")
            qty = pos.get("amt", 0)
            entry = pos.get("entry_price", 0)
            pnl = pos.get("pnl", 0)
            pnl_color = COLORS["green"] if pnl >= 0 else COLORS["red"]
            text.append(f"\n Position: {side} {abs(qty):.4f} BTC", COLORS["gold"])
            text.append(f"  Entry: ${entry:,.0f}", COLORS["dim"])
            text.append(f"  PnL: ${pnl:+,.2f}", pnl_color)
        else:
            text.append(f"\n No open position", COLORS["dim"])

        return Panel(text, title="ACCOUNT", border_style=COLORS["gold"])


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
        "signal": "NEUTRAL",
        "decision": "",
        "in_position": False,
        "bidir": "",
    })

    def __init__(self, client: BB450WSClient, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client

    def render(self) -> Panel:
        d = self.data
        dir_text = f"[{'LONG' if d['direction'] == 'LONG' else 'SHORT'}]"
        focus = d.get("focus", 0)
        sig = d.get("signal", "NEUTRAL")
        decision = d.get("decision", "")
        in_pos = d.get("in_position", False)

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

        lines.append("")

        # Action buttons
        show_long = show_short = show_close = False
        if "LONG CONFIRMADO" in decision or (sig == "LONG" and not in_pos):
            show_long = True
        if "SHORT CONFIRMADO" in decision or (sig == "SHORT" and not in_pos):
            show_short = True
        if in_pos or ("TRAMPA" in decision and in_pos):
            show_close = True
        if "PARCIAL" not in decision and "SIN VENTAJA" not in decision:
            if not show_long and not show_short and not in_pos:
                show_long = True
                show_short = True

        if show_long:
            lines.append("  [\U0001f7e2 ENTRAR LONG]  (1/b)")
        if show_short:
            lines.append("  [\U0001f7e3 ENTRAR SHORT] (2/s)")
        if show_close:
            lines.append("  [\U0001f534 CERRAR POS]  (3/c)")

        status = d.get("status", "")
        if status:
            lines.append(f"\n  {status}")

        text = Text("\n".join(lines))
        border_col = COLORS["white"]
        if show_long and not show_short:
            border_col = COLORS["green"]
        elif show_short and not show_long:
            border_col = COLORS["red"]
        return Panel(text, title="TRADE", border_style=border_col)

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
    BannerWidget { height: 2; }
    PriceBarWidget { height: 4; }
    StrengthBarWidget { height: 3; }
    NarrativeWidget { height: 6; }
    WhaleWallWidget { height: 4; }
    ImbalanceWidget { height: 3; }
    AccountWidget { height: 3; }
    TradeWidget { height: 12; }
    """

    def __init__(self):
        super().__init__()
        self._client = BB450WSClient()
        self._ws_task: Optional[asyncio.Task] = None
        self._prev_signal = "NEUTRAL"
        self._prev_decision = ""

    def compose(self):
        yield BannerWidget()
        yield PriceBarWidget()
        yield StrengthBarWidget()
        yield NarrativeWidget()
        yield WhaleWallWidget()
        yield ImbalanceWidget()
        yield AccountWidget()
        yield TradeWidget(client=self._client)

    def on_mount(self):
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

    def _on_ws_status(self, connected: bool):
        if not self.is_mounted:
            return
        self.query_one(BannerWidget).data = {
            "status": "connected" if connected else "disconnected",
            "host": WS_URI,
            "port": "",
        }

    def _on_market_state(self, data: dict):
        if not self.is_mounted:
            return

        self.query_one(BannerWidget).data["port"] = data.get("bore_port", "")
        self.query_one(BannerWidget).refresh()

        self.query_one(PriceBarWidget).data = {
            "price": data.get("price", 0),
            "change_pct": data.get("change_pct", 0),
            "high": data.get("day_high", 0),
            "low": data.get("day_low", 0),
        }

        sig = data.get("signal", "NEUTRAL")
        conf = data.get("confidence", 0)

        self.query_one(StrengthBarWidget).data = {
            "signal": sig,
            "confidence": conf,
            "buy_pressure": data.get("buy_pressure", 50),
            "regime": data.get("regimen_mercado", ""),
        }

        self.query_one(NarrativeWidget).data = {
            "buy_volume": data.get("buy_volume", 0),
            "sell_volume": data.get("sell_volume", 0),
            "tick_speed": data.get("tick_speed", 0),
            "hft_speed": data.get("hft_speed", 0),
            "cvd": data.get("cvd", 0),
            "ba_ratio": data.get("ba_ratio", 1.0),
            "depth_imb_pct": data.get("depth_imb_pct", 0),
            "spoofing_risk": data.get("spoofing_risk", 0),
            "decision": data.get("decision", ""),
            "active_trap": data.get("active_trap", ""),
        }

        self.query_one(WhaleWallWidget).data = {
            "price": data.get("price", 0),
            "bid_walls": data.get("whale_bid_walls", []),
            "ask_walls": data.get("whale_ask_walls", []),
        }

        self.query_one(ImbalanceWidget).data = {
            "imbalance": data.get("imbalance", 0),
            "depth_imb_pct": data.get("depth_imb_pct", 0),
            "ba_ratio": data.get("ba_ratio", 1.0),
            "book_depth_bids_volume": data.get("book_depth_bids_volume", 0),
            "book_depth_asks_volume": data.get("book_depth_asks_volume", 0),
        }

        self.query_one(AccountWidget).data = {
            "balance": data.get("balance", 0),
            "position": data.get("position"),
            "funding_rate": data.get("funding_rate", 0),
            "oi_delta_5min": data.get("oi_delta_5min", 0),
        }

        decision = data.get("decision", "")
        in_pos = data.get("position") is not None
        tw = self.query_one(TradeWidget)
        tw.data["signal"] = sig
        tw.data["decision"] = decision
        tw.data["in_position"] = in_pos
        tw.refresh()

        # Sound on signal change
        if sig != self._prev_signal and sig in ("LONG", "SHORT"):
            _play_sound(SOUND_LONG if sig == "LONG" else SOUND_SHORT)
            _notify_android(f"BB-450 SENAL {sig}", f"Confianza: {conf:.0f}%")
            self._prev_signal = sig

        if decision != self._prev_decision and decision:
            if "TRAMPA" in decision:
                _notify_android("BB-450 TRAMPA", decision[:80])
            elif "CONFIRMADO" in decision:
                _notify_android(f"BB-450 {decision}", "Se\u00f1al confirmada")
            self._prev_decision = decision

    def _on_notification(self, notif: dict):
        pass  # Notifications disabled in v2 UI

    def _on_command_ack(self, action: str, status: str, result: dict):
        if not self.is_mounted:
            return
        tw = self.query_one(TradeWidget)
        if status == "ok":
            tw.data["status"] = f"\u2705 {action} OK"
        else:
            tw.data["status"] = f"\u274c {action} FAILED: {result.get('message', '')}"
        tw.refresh()


def run():
    app = BB450MobileApp()
    app.run()


if __name__ == "__main__":
    run()
