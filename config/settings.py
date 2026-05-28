import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Testnet API (used when USE_TESTNET=True in order_executor)
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
    BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "True").lower() == "true"

    # Real API (used when USE_TESTNET=False in order_executor)
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


settings = Settings()