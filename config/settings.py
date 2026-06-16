import os
import sys
import threading
from dotenv import load_dotenv

dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(dotenv_path)


class Settings:
    # Real Binance Futures API (production only)
    BINANCE_REAL_API_KEY = os.getenv("BINANCE_REAL_API_KEY", "")
    BINANCE_REAL_SECRET_KEY = os.getenv("BINANCE_REAL_SECRET_KEY", "")
    # Testnet Binance Futures API
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
    BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "False").lower() in ("true", "1", "yes")
    SYMBOL = os.getenv("SYMBOL", "BTCUSDT")

    # ── Thread-safe active symbol (hot-swappable via Telegram) ──────────
    ACTIVE_SYMBOL: str = SYMBOL
    _symbol_lock = threading.Lock()

    @classmethod
    def get_symbol(cls) -> str:
        with cls._symbol_lock:
            return cls.ACTIVE_SYMBOL

    @classmethod
    def set_symbol(cls, new_symbol: str) -> str:
        """Change active symbol. Returns old symbol for rollback."""
        with cls._symbol_lock:
            old = cls.ACTIVE_SYMBOL
            cls.ACTIVE_SYMBOL = new_symbol
            return old
    LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", os.getenv("LEVERAGE", "40")))

    RISK_PER_TRADE = float(os.getenv("RISK_PERCENT", os.getenv("RISK_PER_TRADE", "10.0")))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0.05"))
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "1"))

    # Global position sizing (set via dashboard F2 or Telegram numeric message)
    GLOBAL_TRADE_AMOUNT: float = 1.00   # USD, used when USE_ALL_IN is False
    USE_ALL_IN: bool = os.getenv("USEALLIN", "False").lower() in ("true", "1", "yes")

    @classmethod
    def set_global_trade_amount(cls, amount: float) -> None:
        """Central write-point for GLOBAL_TRADE_AMOUNT.

        Called from:
          - dashboard F2 → _save_sizing_config()
          - Telegram bot → numeric message handler
        Automatically disables USE_ALL_IN when a fixed amount is set.
        """
        cls.GLOBAL_TRADE_AMOUNT = round(float(amount), 2)
        cls.USE_ALL_IN = False


    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

    REFRESH_RATE = float(os.getenv("REFRESH_RATE", "0.1"))

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    ALERT_CRASH_PCT = float(os.getenv("ALERT_CRASH_PCT", "3.0"))
    ALERT_VOLUME_SPIKE = float(os.getenv("ALERT_VOLUME_SPIKE", "3.0"))

    @classmethod
    def validate(cls) -> None:
        """Fail fast on missing critical environment variables."""
        missing = []
        is_testnet = cls.BINANCE_TESTNET
        if is_testnet:
            if not cls.BINANCE_API_KEY:
                missing.append("BINANCE_API_KEY (testnet)")
            if not cls.BINANCE_SECRET_KEY:
                missing.append("BINANCE_SECRET_KEY (testnet)")
        else:
            if not cls.BINANCE_REAL_API_KEY:
                missing.append("BINANCE_REAL_API_KEY")
            if not cls.BINANCE_REAL_SECRET_KEY:
                missing.append("BINANCE_REAL_SECRET_KEY")
        if not cls.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            print(f"[🔴 FATAL] BB-450 — Variables de entorno CRÍTICAS faltantes:")
            for v in missing:
                print(f"           - {v}")
            print(f"[🔴 FATAL] Revisa que exista el archivo:")
            print(f"           {dotenv_path}")
            sys.exit(1)
        tag = "TESTNET" if is_testnet else "PRODUCCIÓN REAL"
        print(f"[✅ CONFIG] Variables de entorno validadas — {tag}")


settings = Settings()