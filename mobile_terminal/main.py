#!/usr/bin/env python3
"""
main.py — Punto de entrada del terminal móvil BB-450 para Termux.

Inicializa el cliente WebSocket y la interfaz Textual, ejecutándolos
como tareas asíncronas simultáneas.

Uso
---
  python main.py

Configuración
-------------
  Editar WS_HOST en config.py con la IP del PC donde corre el dashboard.
"""

import asyncio
import logging
import sys

from config import WS_URI
from tui_app import BB450MobileApp as BB450App

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    # Verificación rápida de dependencias
    try:
        import textual  # noqa: F401
    except ImportError:
        print("[ERROR] textual no instalado — pip install -r requirements.txt")
        sys.exit(1)

    try:
        import websockets  # noqa: F401
    except ImportError:
        print("[ERROR] websockets no instalado — pip install -r requirements.txt")
        sys.exit(1)

    print(f"\n  ╔══════════════════════════════════════╗")
    print(f"  ║      BB-450 MOBILE TERMINAL           ║")
    print(f"  ║  Scalping 1m / 40x · BTCUSDT          ║")
    print(f"  ║                                       ║")
    print(f"  ║  Conectando a: {WS_URI:<20s}║")
    print(f"  ║                                       ║")
    print(f"  ║  [b] LONG    [s] SHORT                ║")
    print(f"  ║  [c] Cerrar  [q] Salir                ║")
    print(f"  ╚══════════════════════════════════════╝\n")

    # Crear instancia de la app (el cliente WS se crea internamente)
    app = BB450App()

    # Ejecutar la app Textual (bloquea hasta que el usuario cierra)
    app.run()


if __name__ == "__main__":
    main()
