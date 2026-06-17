#!/usr/bin/env python3
"""
BB-450 — Entry point for Render.

Usage:
    python render_main.py

Environment:
    PORT                Web server port (set by Render automatically)
    All .env variables  Binance, Telegram, Gemini config
"""

import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"[RenderMain] 🚀 Iniciando BB-450 en puerto {port}")
    uvicorn.run(
        "src.api.render_server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
