import json
import logging
import time
from collections import deque
from enum import Enum
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class NotificationSeverity(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationCategory(Enum):
    WHALE = "whale"
    SIGNAL = "signal"
    TRADE_RESULT = "trade_result"
    AI_ANALYSIS = "ai_analysis"
    DIAGNOSTIC = "diagnostic"
    REGIME = "regime"
    CRASH_PUMP = "crash_pump"
    VOLUME = "volume"
    FORCE = "force"
    COUNTERPARTY = "counterparty"
    TRAP = "trap"
    WALL_IMPACT = "wall_impact"
    FLASH_MOVE = "flash_move"
    REVERSAL = "reversal"
    ACCOUNT = "account"
    SENTIMENT = "sentiment"


ROUTE_ALL = {
    NotificationCategory.SIGNAL,
    NotificationCategory.TRADE_RESULT,
    NotificationCategory.DIAGNOSTIC,
    NotificationCategory.WHALE,
    NotificationCategory.CRASH_PUMP,
    NotificationCategory.VOLUME,
    NotificationCategory.FORCE,
    NotificationCategory.COUNTERPARTY,
    NotificationCategory.TRAP,
    NotificationCategory.FLASH_MOVE,
    NotificationCategory.REVERSAL,
    NotificationCategory.ACCOUNT,
    NotificationCategory.WALL_IMPACT,
}

ROUTE_TELEGRAM = {
    NotificationCategory.AI_ANALYSIS,
    NotificationCategory.REGIME,
    NotificationCategory.SENTIMENT,
}


class NotificationManager:
    """Central notification hub for BB-450.

    Routes events to:
      - WebSocket broadcast (ALL notifications — mobile terminal)
      - Telegram (only SENTIMENT, REGIME, AI_ANALYSIS, CRITICAL severity)
    """

    def __init__(self):
        self._ws_broadcaster: Optional[Callable[[str], None]] = None
        self._telegram_sender: Optional[Callable[[str], None]] = None
        self._history: deque = deque(maxlen=200)

    def set_ws_broadcaster(self, fn: Callable[[dict], None]):
        """Set callback that sends a JSON-serializable dict to all WS clients."""
        self._ws_broadcaster = fn

    def set_telegram_sender(self, fn: Callable[[str], None]):
        """Set callback that sends a text message to Telegram."""
        self._telegram_sender = fn

    def notify(
        self,
        category: NotificationCategory,
        title: str,
        body: str,
        severity: NotificationSeverity = NotificationSeverity.INFO,
        data: Optional[dict] = None,
    ):
        """Route a notification to all enabled channels."""
        record = {
            "type": "notification",
            "category": category.value,
            "severity": severity.value,
            "title": title,
            "body": body,
            "data": data or {},
            "timestamp": time.time(),
        }
        self._history.append(record)

        # Always broadcast to WebSocket
        if self._ws_broadcaster:
            try:
                self._ws_broadcaster(record)
            except Exception as e:
                log.error(f"[Notify] WS broadcast error: {e}")

        # Telegram: only SENTIMENT, REGIME, AI_ANALYSIS, or CRITICAL
        send_to_tg = (
            category in ROUTE_TELEGRAM
            or severity == NotificationSeverity.CRITICAL
        )
        if send_to_tg and self._telegram_sender:
            try:
                self._telegram_sender(f"{_emoji(category)} <b>{title}</b>\n{body}")
            except Exception as e:
                log.error(f"[Notify] Telegram send error: {e}")

    def notify_signal(self, direction: str, confidence: float, price: float,
                      sl: float = 0, tp: float = 0, reasoning: str = "",
                      regime: str = ""):
        """Shortcut for trade signal notification."""
        emoji = "\U0001f7e2" if direction in ("LONG", "ALZA") else "\U0001f7e3"
        title = f"{emoji} {direction} DETECTED ({confidence:.0f}%)"
        body = (
            f"Price: ${price:,.0f}\n"
            f"Confidence: {confidence:.0f}%\n"
            f"Regime: {regime}"
        )
        if sl:
            body += f"\nSL: ${sl:,.0f}"
        if tp:
            body += f"\nTP: ${tp:,.0f}"
        if reasoning:
            body += f"\n\n{reasoning}"
        self.notify(
            NotificationCategory.SIGNAL, title, body,
            severity=NotificationSeverity.INFO,
            data={"direction": direction, "confidence": confidence,
                  "price": price, "sl": sl, "tp": tp,
                  "reasoning": reasoning, "regime": regime},
        )

    def notify_diagnostic(self, reasons: list, trend_label: str = "",
                          regimen: str = ""):
        """Shortcut for signal diagnostic notification."""
        if not reasons:
            return
        title = "\U0001f50d Signal Diagnostics"
        body = "\n".join(f"\u2022 {r}" for r in reasons[:4])
        if len(reasons) > 4:
            body += f"\n... y {len(reasons) - 4} mas"
        self.notify(
            NotificationCategory.DIAGNOSTIC, title, body,
            severity=NotificationSeverity.DEBUG,
            data={"reasons": reasons, "trend_label": trend_label,
                  "regimen": regimen},
        )

    def notify_ai_analysis(self, text: str, regime: str = "",
                           sentiment_score: float = 0.0):
        """Shortcut for AI analysis notification."""
        title = "\U0001f9e0 AI Market Analysis"
        body = text
        if regime:
            body = f"Regime: {regime}\n\n{body}"
        self.notify(
            NotificationCategory.AI_ANALYSIS, title, body,
            severity=NotificationSeverity.INFO,
            data={"text": text, "regime": regime,
                  "sentiment_score": sentiment_score},
        )

    def notify_trade_result(self, success: bool, side: str,
                            qty: float = 0, fill_price: float = 0,
                            order_id: str = "", message: str = ""):
        """Shortcut for order execution result."""
        icon = "\u2705" if success else "\u274c"
        title = f"{icon} Order {side}"
        body = message or (
            f"{side} {qty:.4f} @ ${fill_price:,.0f} id={order_id}"
            if success else f"Execution failed: {message}"
        )
        self.notify(
            NotificationCategory.TRADE_RESULT, title, body,
            severity=NotificationSeverity.INFO if success else NotificationSeverity.WARNING,
            data={"success": success, "side": side, "qty": qty,
                  "fill_price": fill_price, "order_id": order_id,
                  "message": message},
        )

    def notify_regime_change(self, old_regime: str, new_regime: str,
                             reason: str = ""):
        """Shortcut for market regime change."""
        title = f"\U0001f3af Regime Change: {old_regime} \u2192 {new_regime}"
        body = reason or f"Market regime changed from {old_regime} to {new_regime}"
        self.notify(
            NotificationCategory.REGIME, title, body,
            severity=NotificationSeverity.WARNING,
            data={"old_regime": old_regime, "new_regime": new_regime,
                  "reason": reason},
        )

    def notify_market_alert(self, category: NotificationCategory,
                            alert_type: str, body: str,
                            severity: NotificationSeverity = NotificationSeverity.INFO,
                            data: Optional[dict] = None):
        """Generic market alert (whale, crash, pump, force, trap, etc)."""
        title = _alert_title(alert_type)
        self.notify(category, title, body, severity=severity, data=data)

    def get_history(self, limit: int = 20) -> list:
        return list(self._history)[-limit:]

    def get_recent_by_category(self, category: NotificationCategory,
                               limit: int = 5) -> list:
        return [r for r in self._history if r["category"] == category.value][-limit:]


# ── Helpers ────────────────────────────────────────────────────────────

_NOTIF_EMOJIS = {
    NotificationCategory.WHALE: "\U0001f40b",
    NotificationCategory.SIGNAL: "\U0001f4e1",
    NotificationCategory.TRADE_RESULT: "\U0001f4b0",
    NotificationCategory.AI_ANALYSIS: "\U0001f9e0",
    NotificationCategory.DIAGNOSTIC: "\U0001f50d",
    NotificationCategory.REGIME: "\U0001f3af",
    NotificationCategory.CRASH_PUMP: "\u26a1",
    NotificationCategory.VOLUME: "\U0001f4ca",
    NotificationCategory.FORCE: "\U0001f4aa",
    NotificationCategory.COUNTERPARTY: "\U0001f91d",
    NotificationCategory.TRAP: "\U0001f6ab",
    NotificationCategory.WALL_IMPACT: "\U0001f9f1",
    NotificationCategory.FLASH_MOVE: "\u26a1",
    NotificationCategory.REVERSAL: "\U0001f504",
    NotificationCategory.ACCOUNT: "\U0001f464",
    NotificationCategory.SENTIMENT: "\U0001f4ac",
}

_ALERT_TITLES = {
    "crash": "\u26a1 FLASH CRASH",
    "pump": "\u26a1 FLASH PUMP",
    "whale_buy": "\U0001f40b WHALE BUYING",
    "whale_sell": "\U0001f40b WHALE SELLING",
    "force": "\U0001f4aa Force Meter",
    "volume_spike": "\U0001f4ca Volume Spike",
    "trap": "\U0001f6ab Trap Detected",
    "wall_impact": "\U0001f9f1 Wall Absorbed",
    "flash_move": "\u26a1 Flash Move",
    "counterparty": "\U0001f91d Counterparty Detected",
    "reversal": "\U0001f504 Reversal Probable",
    "radar": "\U0001f4a1 Radar Alert",
}


def _emoji(cat: NotificationCategory) -> str:
    return _NOTIF_EMOJIS.get(cat, "\U0001f514")


def _alert_title(alert_type: str) -> str:
    return _ALERT_TITLES.get(alert_type, f"\U0001f514 {alert_type.replace('_', ' ').title()}")


# ── Singleton ──────────────────────────────────────────────────────────

_notify: Optional[NotificationManager] = None


def get_notifier() -> NotificationManager:
    global _notify
    if _notify is None:
        _notify = NotificationManager()
    return _notify
