"""
config.py — Configuración centralizada para el terminal móvil BB-450.

Todas las constantes editables por el usuario están aquí.
"""

# ── WebSocket ────────────────────────────────────────────────────────────
# IP del PC donde corre el dashboard BB-450 (Tailscale — funciona desde cualquier red).
WS_HOST: str = "100.97.238.10"
WS_PORT: int = 8765
WS_URI: str = f"ws://{WS_HOST}:{WS_PORT}"

# Re-intento de conexión
WS_RECONNECT_DELAY: float = 5.0   # segundos entre reintentos
WS_PING_INTERVAL: float = 20.0    # ping cada 20s
WS_PING_TIMEOUT: float = 10.0     # timeout del ping

# ── UI ───────────────────────────────────────────────────────────────────
UI_REFRESH_INTERVAL: float = 0.5  # segundos entre refrescos de pantalla

# ── Sonidos (comandos Termux opcionales) ─────────────────────────────────
SOUND_LONG: str = "sound/long.wav"    # reproducido al enviar orden LONG
SOUND_SHORT: str = "sound/short.wav"
SOUND_CLOSE: str = "sound/close.wav"

# ── Notificaciones ───────────────────────────────────────────────────────
NOTIFY_ENABLED: bool = True
