from __future__ import annotations

import asyncio
import logging
import subprocess
import time
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
    "red": "#bb00ff",
    "gold": "#ffcc00",
    "magenta": "#bb00ff",
    "cyan": "#00ccff",
    "orange": "#ff8844",
    "dim": "#555555",
    "white": "#cccccc",
}

ALIGN_RIGHT = 38


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


def _bar(pct: float, n: int = 12) -> tuple[str, str]:
    f = int(n * pct / 100)
    e = n - f
    return "\u2588" * f, "\u2591" * e


def _price_dist(current: float, wall_price: float) -> str:
    if current <= 0 or wall_price <= 0:
        return ""
    return f"{(wall_price / current - 1) * 100:+.2f}%"


# ── Widgets ────────────────────────────────────────────────────────

class BannerWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        s = d.get("status", "disconnected")
        host = d.get("host", WS_URI)
        port = d.get("port", "")
        sig = d.get("signal", "")
        conf = d.get("confidence", 0)

        if s == "connected":
            if sig == "LONG":
                emoji = "\U0001f7e2"
                label = f"LONG {conf:.0f}%"
                col = COLORS["green"]
            elif sig == "SHORT":
                emoji = "\U0001f535"
                label = f"SHORT {conf:.0f}%"
                col = COLORS["red"]
            else:
                emoji = "\u25cf"
                label = "NEUTRAL"
                col = COLORS["gold"]
        elif s == "reconnecting":
            emoji = "\u26a1"
            label = "RECONNECTING"
            col = COLORS["gold"]
        else:
            emoji = "\u25cf"
            label = "DISCONNECTED"
            col = COLORS["dim"]

        text = Text.assemble(
            (f" {emoji} BB-450 ", f"bold {COLORS['magenta']}"),
            (f"\u2014 {label} \u2014 ", f"bold {col}"),
            (f"{host}", COLORS["dim"]),
            (f":{port}" if port else "", COLORS["dim"]),
        )
        return Panel(text, style=f"bold {COLORS['bg']}", border_style=col)


class PriceBarWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        p = d.get("price", 0)
        chg = d.get("change_pct", 0)
        c = COLORS["green"] if chg >= 0 else COLORS["red"]
        a = "\u25b2" if chg >= 0 else "\u25bc"
        hl = d.get("high", 0)
        ll = d.get("low", 0)
        text = Text.assemble(
            (f" {_format_price(p)} ", f"bold {COLORS['white']}"),
            (f"{a} {chg:+.2f}% ", c),
            (f"\n  H: {_format_price(hl)}  ", COLORS["dim"]),
            (f"L: {_format_price(ll)}", COLORS["dim"]),
        )
        return Panel(text, title="PRICE", border_style=c)


class StrengthBarWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        conf = d.get("confidence", 0)
        bp = d.get("buy_pressure", 50)
        sp = 100 - bp
        regime = d.get("regime", "")

        if sig == "LONG":
            sc = COLORS["green"]
            se = "\U0001f7e2"
        elif sig == "SHORT":
            sc = COLORS["red"]
            se = "\U0001f7e3"
        else:
            sc = COLORS["gold"]
            se = "\u26aa"

        lf, le = _bar(bp)
        text = Text()
        text.append(f" LONG {bp:.0f}% ", "bold green")
        text.append(lf, "green")
        text.append(le, COLORS["dim"])
        text.append(f" {sp:.0f}% SHORT", f"bold {COLORS['red']}")
        text.append(f"\n  {se} {sig} {conf:.0f}%", f"bold {sc}")
        if regime:
            text.append(f" \u00b7 {regime[:20]}", COLORS["dim"])
        return Panel(text, title="STRENGTH", border_style=sc)


class IndicatorsWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        rsi = d.get("rsi", 50)
        vol = d.get("volume", 0)
        atr = d.get("atr", 0)
        ke = d.get("kaufman_eff", 0.5)
        delta = d.get("delta", 0)
        cvd = d.get("cvd", 0)
        trend = d.get("trend_label", "")

        rsi_f, rsi_e = _bar(rsi, 10)
        rsi_c = COLORS["green"] if rsi > 60 else COLORS["red"] if rsi < 40 else COLORS["gold"]

        vol_s = f"{vol:,.0f}" if vol >= 1 else f"{vol:.2f}"
        dc = COLORS["green"] if delta > 0 else COLORS["red"] if delta < 0 else COLORS["dim"]
        cc = COLORS["green"] if cvd > 0 else COLORS["red"] if cvd < 0 else COLORS["dim"]
        ke_c = COLORS["green"] if ke > 0.6 else COLORS["gold"] if ke > 0.3 else COLORS["dim"]

        text = Text()
        text.append(f" RSI {rsi:.0f} ", rsi_c)
        text.append(rsi_f, rsi_c)
        text.append(rsi_e, COLORS["dim"])
        text.append(f"  Vol {vol_s}", COLORS["white"])
        text.append(f"\n ATR ${atr:.0f}  KE {ke:.2f}", ke_c)
        if trend:
            tc = COLORS["green"] if "LONG" in trend or "BULL" in trend else COLORS["red"] if "SHORT" in trend or "BEAR" in trend else COLORS["gold"]
            text.append(f"  {trend[:12]}", tc)
        text.append(f"\n {dc}\u0394DELTA{COLORS['dim']} {delta:+.1f}  ", dc)
        text.append(f"{cc}CVD{COLORS['dim']} {cvd:+.1f}", cc)
        return Panel(text, title="INDICATORS", border_style=COLORS["cyan"])


class NarrativeWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        bv = d.get("buy_volume", 0)
        sv = d.get("sell_volume", 0)
        total = bv + sv + 0.001
        delta = bv - sv
        dp = (delta / total) * 100
        tick = d.get("tick_speed", 0)
        hft = d.get("hft_speed", 0)
        cvd = d.get("cvd", 0)
        ba = d.get("ba_ratio", 1.0)
        imb = d.get("depth_imb_pct", 0)
        spoof = d.get("spoofing_risk", 0)
        decision = d.get("decision", "")
        trap = d.get("active_trap", "")
        regime = d.get("regime", "")

        text = Text()

        # Line 1: Volume dominance
        if abs(dp) > 35 and total > 3:
            de = "\U0001f7e3" if delta > 0 else "\U0001f534"
            text.append(f"  {de} BALLENA {'COMPRADORA' if delta > 0 else 'VENDEDORA'} ", COLORS["gold"])
            text.append(f"\u0394{delta:+.1f}\u20bf ({dp:+.1f}%)", COLORS["white"])
        elif abs(dp) > 15 and total > 3:
            de = "\U0001f7e2" if delta > 0 else "\U0001f7e3"
            text.append(f"  {de} AGRESION {'COMPRADORA' if delta > 0 else 'VENDEDORA'} ", COLORS["gold"])
            text.append(f"\u0394{delta:+.1f}\u20bf ({dp:+.1f}%)", COLORS["white"])
        else:
            text.append(f"  \u26aa Vol: {total:.1f}\u20bf  \u0394{delta:+.1f}\u20bf", COLORS["dim"])

        # Line 2: Flow metrics
        hc = COLORS["orange"] if hft > 5 else COLORS["gold"] if hft > 2 else COLORS["dim"]
        ba_c = COLORS["green"] if ba > 1.2 else COLORS["red"] if ba < 0.8 else COLORS["dim"]
        text.append(f"\n  \u0394Delta: {delta:+.1f}  CVD: {cvd:+.1f}  ", COLORS["dim"])
        text.append(f"B/A: {ba:.2f}x", ba_c)
        text.append(f"  HFT: {hft:.1f}/s", hc)
        text.append(f"  Tick: {tick:.0f}/s", COLORS["dim"])

        # Line 3: Risk metrics
        sp_c = COLORS["orange"] if spoof > 30 else COLORS["gold"] if spoof > 10 else COLORS["dim"]
        text.append(f"\n  Spoof: {spoof:.0f}% ", sp_c)
        text.append(f"Depth: {imb:+.1f}% ", COLORS["dim"])
        if regime:
            rc = COLORS["green"] if "TREND" in regime.upper() or "ALCISTA" in regime else COLORS["red"] if "BAJISTA" in regime else COLORS["gold"]
            text.append(f"Regimen: {regime[:18]}", rc)

        # Line 4: Trap / Decision
        if trap:
            text.append(f"\n  \u26a0\ufe0f {trap[:50]}", COLORS["orange"])
        if decision:
            dc = COLORS["green"] if "LONG" in decision else COLORS["red"] if "SHORT" in decision else COLORS["orange"] if "TRAMPA" in decision else COLORS["gold"] if "PARCIAL" in decision else COLORS["dim"]
            if trap:
                text.append(f"  {decision[:50]}", dc)
            else:
                text.append(f"\n  {decision[:55]}", dc)

        bc2 = COLORS["cyan"]
        if "TRAMPA" in decision:
            bc2 = COLORS["orange"]
        elif "LONG CONFIRMADO" in decision:
            bc2 = COLORS["green"]
        elif "SHORT CONFIRMADO" in decision:
            bc2 = COLORS["red"]
        return Panel(text, title="NARRATIVE", border_style=bc2)


class WhaleWallWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        price = d.get("price", 0)
        bids: list = d.get("bid_walls", [])
        asks: list = d.get("ask_walls", [])
        lines = []
        n = max(len(bids), len(asks), 1)
        for i in range(n):
            l = r = ""
            if i < len(bids):
                w = bids[i]
                wp = float(w.get("price", 0)) if isinstance(w, dict) else (float(w[0]) if isinstance(w, (list, tuple)) else 0)
                wq = float(w.get("quantity", 0)) if isinstance(w, dict) else (float(w[1]) if isinstance(w, (list, tuple)) else 0)
                l = f"\U0001f40b {wq:.2f}\u20bf {_format_price(wp)} ({_price_dist(price, wp)})"
            if i < len(asks):
                w = asks[i]
                wp = float(w.get("price", 0)) if isinstance(w, dict) else (float(w[0]) if isinstance(w, (list, tuple)) else 0)
                wq = float(w.get("quantity", 0)) if isinstance(w, dict) else (float(w[1]) if isinstance(w, (list, tuple)) else 0)
                r = f"\U0001f43b {wq:.2f}\u20bf {_format_price(wp)} ({_price_dist(price, wp)})"
            sp = " " * max(1, ALIGN_RIGHT - len(l))
            lines.append(f"  {l}{sp}{r}")
        if not bids and not asks:
            lines.append("  \u26aa No institutional walls")
        t = Text()
        for ln in lines:
            t.append(f"{ln}\n")
        return Panel(t, title="WHALE WALLS", border_style=COLORS["gold"] if bids or asks else COLORS["dim"])


class ImbalanceWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        imb = d.get("imbalance", 0)
        dimb = d.get("depth_imb_pct", 0)
        ba = d.get("ba_ratio", 1.0)
        bv = d.get("book_depth_bids_volume", 0)
        av = d.get("book_depth_asks_volume", 0)
        delta = d.get("delta", 0)
        spread_vel = d.get("spread_velocity", 0)
        cancel = d.get("cancel_rate", 0)
        ob = d.get("order_book", {})
        top_bid = ob.get("bids", [[0, 0]])[0] if ob.get("bids") else [0, 0]
        top_ask = ob.get("asks", [[0, 0]])[0] if ob.get("asks") else [0, 0]
        tbp = float(top_bid[0]) if isinstance(top_bid, (list, tuple)) else 0
        tbq = float(top_bid[1]) if isinstance(top_bid, (list, tuple)) else 0
        tap = float(top_ask[0]) if isinstance(top_ask, (list, tuple)) else 0
        taq = float(top_ask[1]) if isinstance(top_ask, (list, tuple)) else 0
        spread = tap - tbp

        ic = max(0, min(100, (imb + 1) * 50))
        bf, be = _bar(ic)
        bc2 = COLORS["green"] if imb > 0.2 else COLORS["red"] if imb < -0.2 else COLORS["dim"]
        dc = COLORS["green"] if delta > 0 else COLORS["red"] if delta < 0 else COLORS["dim"]
        text = Text.assemble(
            ("  BIDS ", COLORS["green"]),
            (bf, bc2),
            (be, COLORS["dim"]),
            (" ASKS", COLORS["red"]),
            "\n",
            (f"  Depth: {dimb:+.1f}%  B/A: {ba:.2f}x  BVol: {bv:.1f}/{av:.1f}\u20bf", COLORS["dim"]),
            "\n",
            (f"  \u0394Delta: {delta:+.1f}  ", dc),
            (f"Spread: ${spread:.1f}  Cancel: {cancel:.0f}%", COLORS["dim"]),
        )
        return Panel(text, title="ORDER BOOK", border_style=bc2 if abs(imb) > 0.2 else COLORS["dim"])


class OrderFlowWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        delta = d.get("delta", 0)
        cvd = d.get("cvd", 0)
        tick = d.get("tick_speed", 0)
        hft = d.get("hft_speed", 0)
        cancel = d.get("cancel_rate", 0)
        spoof = d.get("spoofing_risk", 0)
        spread_vel = d.get("spread_velocity", 0)

        dc = COLORS["green"] if delta > 0 else COLORS["red"] if delta < 0 else COLORS["dim"]
        cc = COLORS["green"] if cvd > 0 else COLORS["red"] if cvd < 0 else COLORS["dim"]
        hc = COLORS["orange"] if hft > 5 else COLORS["gold"] if hft > 2 else COLORS["dim"]
        sc = COLORS["orange"] if spoof > 30 else COLORS["gold"] if spoof > 10 else COLORS["dim"]
        canc = COLORS["orange"] if cancel > 20 else COLORS["gold"] if cancel > 10 else COLORS["dim"]

        text = Text.assemble(
            ("  \u0394Delta: ", COLORS["dim"]),
            (f"{delta:+.1f}  ", dc),
            ("CVD: ", COLORS["dim"]),
            (f"{cvd:+.1f}  ", cc),
            ("Tick: ", COLORS["dim"]),
            (f"{tick:.0f}/s  ", COLORS["white"]),
            ("HFT: ", COLORS["dim"]),
            (f"{hft:.1f}/s", hc),
            "\n",
            ("  Cancel: ", COLORS["dim"]),
            (f"{cancel:.0f}%  ", canc),
            ("Spoof: ", COLORS["dim"]),
            (f"{spoof:.0f}%  ", sc),
            ("SpreadVel: ", COLORS["dim"]),
            (f"{spread_vel:.1f}ms", COLORS["white"]),
        )
        bc = COLORS["cyan"]
        if abs(delta) > 20:
            bc = COLORS["green"] if delta > 0 else COLORS["red"]
        return Panel(text, title="ORDER FLOW", border_style=bc)


class AccountWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        bal = d.get("balance", 0)
        pos = d.get("position")
        funding = d.get("funding_rate", 0)
        oi = d.get("oi_delta_5min", 0)
        t = Text()
        t.append(f" Balance: ${bal:,.2f}", COLORS["green"])
        t.append(f"  Fund: {funding:+.4f}%", COLORS["dim"])
        if pos:
            side = pos.get("direction", "?")
            qty = pos.get("amt", 0)
            entry = pos.get("entry_price", 0)
            pnl = pos.get("pnl", 0)
            pc = COLORS["green"] if pnl >= 0 else COLORS["red"]
            t.append(f"\n {side} {abs(qty):.4f} BTC", COLORS["gold"])
            t.append(f"  Entry ${entry:,.0f}", COLORS["dim"])
            t.append(f"  PnL ${pnl:+,.2f}", pc)
        else:
            t.append(f"\n No position", COLORS["dim"])
            t.append(f"  OI: {oi:+.1f}%", COLORS["dim"])
        return Panel(t, title="ACCOUNT", border_style=COLORS["gold"])


class AIAnalysisWidget(Widget):
    data = reactive({})

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        conf = d.get("confidence", 0)
        decision = d.get("decision", "")
        diag: list = d.get("signal_diagnostics", [])
        trend = d.get("trend", "")
        regime = d.get("regime", "")
        trap = d.get("active_trap", "")

        sc = COLORS["green"] if sig == "LONG" else COLORS["red"] if sig == "SHORT" else COLORS["gold"]
        tc = COLORS["green"] if "ALCISTA" in trend else COLORS["red"] if "BAJISTA" in trend else COLORS["dim"]

        text = Text.assemble(
            ("  \U0001f916 AI ", COLORS["magenta"]),
            (f"{sig} ", f"bold {sc}"),
            (f"{conf:.0f}% ", f"bold {sc}"),
        )
        if trend:
            text.append(f"Trend: {trend[:10]} ", tc)
        text.append("\n")

        if diag:
            for di in diag[:3]:
                di_c = COLORS["green"] if any(w in di.upper() for w in ["LONG", "BUY", "ALZA", "BULL"]) else COLORS["red"] if any(w in di.upper() for w in ["SHORT", "SELL", "BAJA", "BEAR"]) else COLORS["orange"] if any(w in di.upper() for w in ["TRAP", "SPOOF"]) else COLORS["dim"]
                text.append(f"  \u2022 {di[:55]}", di_c)
                text.append("\n")
        else:
            text.append("  \u26aa No active signal analysis", COLORS["dim"])
            text.append("\n")

        if trap:
            text.append(f"  \u26a0\ufe0f Trap: {trap[:50]}", COLORS["orange"])
        elif decision and "CONFIRMADO" in decision:
            text.append(f"  \u2705 Senal confirmada: {decision[:50]}", COLORS["green"])
        else:
            text.append(f"  Esperando senal...", COLORS["dim"])

        bc = sc if sig != "NEUTRAL" else COLORS["dim"]
        return Panel(text, title="AI ANALYSIS", border_style=bc)


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
        "price": 0,
    })

    def __init__(self, client: BB450WSClient, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client

    def render(self) -> Panel:
        d = self.data
        sig = d.get("signal", "NEUTRAL")
        decision = d.get("decision", "")
        in_pos = d.get("in_position", False)
        price = d.get("price", 0)

        dt = f"[{'LONG' if d['direction'] == 'LONG' else 'SHORT'}]"
        dir_col = COLORS["green"] if d["direction"] == "LONG" else COLORS["red"]
        sl_val = d.get("sl", "") or "auto"
        tp_val = d.get("tp", "") or "auto"

        lines = []
        lines.append(f"  DIR {dt}  SL {sl_val}  TP {tp_val}")
        lines.append(f"  LEV {d['leverage']}x  RISK {d['risk_pct']:.1f}%  SPLIT {'YES' if d['split'] else 'NO'}")

        lines.append("  " + "\u2500" * 28)

        sig_col = COLORS["green"] if sig == "LONG" else COLORS["red"] if sig == "SHORT" else COLORS["gold"]
        dec_col = COLORS["green"] if "LONG" in decision else COLORS["red"] if "SHORT" in decision else COLORS["orange"] if "TRAMPA" in decision else COLORS["gold"] if "CONFIRMADO" in decision else COLORS["dim"]
        lines.append(f"  Signal: {sig}  {decision[:45] if decision else 'Esperando...'}")

        status = d.get("status", "")
        if status:
            lines.append(f"  {status}")
        else:
            lines.append(f"  In position: {'YES' if in_pos else 'NO'}  Price: {_format_price(price)}")

        t = Text("\n".join(lines))
        bc3 = COLORS["white"]
        if "LONG" in decision and "CONFIRMADO" in decision:
            bc3 = COLORS["green"]
        elif "SHORT" in decision:
            bc3 = COLORS["red"]
        elif "TRAMPA" in decision:
            bc3 = COLORS["orange"]
        return Panel(t, title="TRADE", border_style=bc3)

    async def on_key(self, event):
        focus = self.data.get("focus", 0)
        mf = 5

        if event.key == "tab":
            focus = (focus + 1) % (mf + 1) if focus < mf else 0
            self.data["focus"] = focus
            event.stop()
            self.refresh()

        elif event.key in ("up", "down"):
            d2 = 1 if event.key == "up" else -1
            if focus == 0:
                self.data["direction"] = "SHORT" if self.data["direction"] == "LONG" else "LONG"
            elif focus == 5:
                self.data["split"] = not self.data.get("split", False)
            else:
                step = {1: 10, 2: 10, 3: 5, 4: 0.1}.get(focus, 1)
                km = {1: "sl", 2: "tp", 3: "leverage", 4: "risk_pct"}
                if focus in km:
                    cur = self.data.get(km[focus], 0)
                    if isinstance(cur, str):
                        try:
                            cur = float(cur) if cur else 0
                        except ValueError:
                            cur = 0
                    nv = cur + step * d2
                    if focus == 3:
                        nv = max(1, min(100, nv))
                    elif focus == 4:
                        nv = max(0.1, min(100, nv))
                    elif focus in (1, 2):
                        nv = max(0, nv)
                    self.data[km[focus]] = str(int(nv)) if focus == 3 else f"{nv:.1f}"
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
        lev = self.data.get("leverage", 40)
        rp = self.data.get("risk_pct", 1.0)
        sp = self.data.get("split", False)
        ed = "ALZA" if direction == "LONG" else "BAJA"
        ok = await self._client.trade(direction=ed, sl=sl, tp=tp, leverage=lev, risk_pct=rp, split=sp)
        self.data["status"] = "\u2705 ENVIADA" if ok else "\u274c FALLIDA"
        if ok:
            _play_sound(SOUND_LONG if direction == "LONG" else SOUND_SHORT)
        self.refresh()


# ── Main App ───────────────────────────────────────────────────────

class BB450MobileApp(App):
    CSS = """
    Screen { background: #0a0a0a; }
    BannerWidget { height: 3; border: none; }
    PriceBarWidget { height: 4; border: none; }
    StrengthBarWidget { height: 3; border: none; }
    IndicatorsWidget { height: 4; border: none; }
    NarrativeWidget { height: 5; border: none; }
    WhaleWallWidget { height: 5; border: none; }
    ImbalanceWidget { height: 4; border: none; }
    OrderFlowWidget { height: 3; border: none; }
    AIAnalysisWidget { height: 5; border: none; }
    AccountWidget { height: 3; border: none; }
    TradeWidget { height: 6; border: none; }
    """

    def __init__(self):
        super().__init__()
        self.dark = True
        self._client = BB450WSClient()
        self._ws_task: Optional[asyncio.Task] = None
        self._prev_signal = "NEUTRAL"
        self._prev_decision = ""

    def compose(self):
        yield BannerWidget()
        yield PriceBarWidget()
        yield StrengthBarWidget()
        yield IndicatorsWidget()
        yield NarrativeWidget()
        yield WhaleWallWidget()
        yield ImbalanceWidget()
        yield OrderFlowWidget()
        yield AIAnalysisWidget()
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
        ok = await self._client.trade(direction="ALZA", sl=0, tp=0, leverage=40, risk_pct=1.0, split=False)
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 RAPIDA" if ok else "\u274c FALLIDA"
        tw.refresh()
        if ok:
            _play_sound(SOUND_LONG)
            _notify_android("BB-450 LONG", "LONG enviada")

    def _quick_short(self):
        asyncio.create_task(self._do_quick_short())

    async def _do_quick_short(self):
        ok = await self._client.trade(direction="BAJA", sl=0, tp=0, leverage=40, risk_pct=1.0, split=False)
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 RAPIDA" if ok else "\u274c FALLIDA"
        tw.refresh()
        if ok:
            _play_sound(SOUND_SHORT)
            _notify_android("BB-450 SHORT", "SHORT enviada")

    def _quick_close(self):
        asyncio.create_task(self._do_quick_close())

    async def _do_quick_close(self):
        ok = await self._client.close_all()
        tw = self.query_one(TradeWidget)
        tw.data["status"] = "\u2705 CERRADA" if ok else "\u274c FALLIDA"
        tw.refresh()
        if ok:
            _play_sound(SOUND_CLOSE)
            _notify_android("BB-450 CLOSE", "Posicion cerrada")

    def _on_ws_status(self, connected: bool):
        if not self.is_mounted:
            return
        try:
            self.query_one(BannerWidget).data = {
                "status": "connected" if connected else "disconnected",
                "host": WS_URI,
                "port": "",
            }
        except Exception as e:
            log.error(f"[TUI] _on_ws_status: {e}")

    def _on_market_state(self, data: dict):
        if not self.is_mounted:
            return
        sig = data.get("signal", "NEUTRAL")
        conf = data.get("confidence", 0)
        decision = data.get("decision", "")

        def _set(w, d):
            try:
                self.query_one(w).data = d
            except Exception as e:
                log.error(f"[TUI] _on_market_state {w.__name__}: {e}")

        _set(BannerWidget, {
            "status": "connected",
            "host": WS_URI,
            "port": data.get("bore_port", ""),
            "signal": sig,
            "confidence": conf,
        })
        _set(PriceBarWidget, {
            "price": data.get("price", 0),
            "change_pct": data.get("change_pct", 0),
            "high": data.get("day_high", 0),
            "low": data.get("day_low", 0),
        })
        _set(StrengthBarWidget, {
            "signal": sig,
            "confidence": conf,
            "buy_pressure": data.get("buy_pressure", 50),
            "regime": data.get("regimen_mercado", ""),
        })
        _set(IndicatorsWidget, {
            "rsi": data.get("rsi", 50),
            "volume": data.get("volume", 0),
            "atr": data.get("atr", 0),
            "kaufman_eff": data.get("kaufman_eff", 0.5),
            "delta": data.get("delta", 0),
            "cvd": data.get("cvd", 0),
            "trend_label": data.get("trend_label", ""),
        })
        _set(NarrativeWidget, {
            "buy_volume": data.get("buy_volume", 0),
            "sell_volume": data.get("sell_volume", 0),
            "tick_speed": data.get("tick_speed", 0),
            "hft_speed": data.get("hft_speed", 0),
            "cvd": data.get("cvd", 0),
            "ba_ratio": data.get("ba_ratio", 1.0),
            "depth_imb_pct": data.get("depth_imb_pct", 0),
            "spoofing_risk": data.get("spoofing_risk", 0),
            "active_trap": data.get("active_trap", ""),
            "decision": decision,
            "regime": data.get("regimen_mercado", ""),
        })
        _set(WhaleWallWidget, {
            "price": data.get("price", 0),
            "bid_walls": data.get("whale_bid_walls", []),
            "ask_walls": data.get("whale_ask_walls", []),
        })
        _set(ImbalanceWidget, {
            "imbalance": data.get("imbalance", 0),
            "depth_imb_pct": data.get("depth_imb_pct", 0),
            "ba_ratio": data.get("ba_ratio", 1.0),
            "book_depth_bids_volume": data.get("book_depth_bids_volume", 0),
            "book_depth_asks_volume": data.get("book_depth_asks_volume", 0),
            "delta": data.get("delta", 0),
            "spread_velocity": data.get("spread_velocity", 0),
            "cancel_rate": data.get("cancel_rate", 0),
            "order_book": data.get("order_book", {}),
        })
        _set(OrderFlowWidget, {
            "delta": data.get("delta", 0),
            "cvd": data.get("cvd", 0),
            "tick_speed": data.get("tick_speed", 0),
            "hft_speed": data.get("hft_speed", 0),
            "cancel_rate": data.get("cancel_rate", 0),
            "spoofing_risk": data.get("spoofing_risk", 0),
            "spread_velocity": data.get("spread_velocity", 0),
        })
        _set(AIAnalysisWidget, {
            "signal": sig,
            "confidence": conf,
            "decision": decision,
            "signal_diagnostics": data.get("signal_diagnostics", []),
            "trend": data.get("trend", ""),
            "regime": data.get("regimen_mercado", ""),
            "active_trap": data.get("active_trap", ""),
        })
        _set(AccountWidget, {
            "balance": data.get("balance", 0),
            "position": data.get("position"),
            "funding_rate": data.get("funding_rate", 0),
            "oi_delta_5min": data.get("oi_delta_5min", 0),
        })
        in_pos = data.get("position") is not None
        _set(TradeWidget, {
            "direction": "LONG",
            "sl": "",
            "tp": "",
            "leverage": 40,
            "risk_pct": 1.0,
            "split": False,
            "focus": 0,
            "status": "",
            "signal": sig,
            "decision": decision,
            "in_position": in_pos,
            "price": data.get("price", 0),
        })

        if sig != self._prev_signal and sig in ("LONG", "SHORT"):
            _play_sound(SOUND_LONG if sig == "LONG" else SOUND_SHORT)
            _notify_android(f"BB-450 {sig}", f"Conf: {conf:.0f}%")
            self._prev_signal = sig
        if decision != self._prev_decision and decision:
            if "TRAMPA" in decision:
                _notify_android("BB-450 TRAMPA", decision[:80])
            elif "CONFIRMADO" in decision:
                _notify_android(f"BB-450 {decision}", "Confirmada")
            self._prev_decision = decision

    def _on_notification(self, notif: dict):
        pass

    def _on_command_ack(self, action: str, status: str, result: dict):
        if not self.is_mounted:
            return
        try:
            tw = self.query_one(TradeWidget)
            if status == "ok":
                tw.data["status"] = f"\u2705 {action} OK"
            else:
                tw.data["status"] = f"\u274c {action}: {result.get('message', '')}"
            tw.refresh()
        except Exception as e:
            log.error(f"[TUI] _on_command_ack: {e}")


def run():
    BB450MobileApp().run()


if __name__ == "__main__":
    run()
