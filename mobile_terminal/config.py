"""
config.py — Configuración centralizada para el terminal móvil BB-450.

Todas las constantes editables por el usuario están aquí.
"""

# ── WebSocket ────────────────────────────────────────────────────────────
# Opción A — Tailscale (IP fija si el teléfono también tiene Tailscale):
WS_HOST: str = "100.97.238.10"
WS_PORT: int = 8765

# Opción B — bore (no necesita nada en el teléfono):
#   1. En el PC:  bore local 8765 --to bore.pub
#   2. Descomenta la línea de abajo con el puerto que te dé
WS_URI: str = "wss://bb-450.onrender.com/ws"

# Puedes sobrescribir WS_URI con la URL que te asigne Render:
# WS_URI = "wss://TU-SERVICIO.onrender.com/ws"

# Si WS_URI no está definido, se construye desde WS_HOST:WS_PORT
try:
    WS_URI
except NameError:
    WS_URI = f"ws://{WS_HOST}:{WS_PORT}"

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
