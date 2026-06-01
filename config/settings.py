import os
import sys
from dotenv import load_dotenv

dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(dotenv_path)


class Settings:
    # Real Binance Futures API (production only)
    BINANCE_REAL_API_KEY = os.getenv("BINANCE_REAL_API_KEY", "")
    BINANCE_REAL_SECRET_KEY = os.getenv("BINANCE_REAL_SECRET_KEY", "")
    SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
    LEVERAGE = int(os.getenv("LEVERAGE", "100"))

    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0.05"))
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "1"))

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
        print(f"[✅ CONFIG] Variables de entorno validadas — PRODUCCIÓN REAL")


settings = Settings()