# BB-450 Mobile Terminal

Cliente TUI (Textual) para monitorear y operar el bot BB-450 desde **Termux en Android** vía WebSocket.

## Requisitos

- Android 8+ con [Termux](https://termux.com/) instalado
- Conexión WiFi (misma red que el PC donde corre el dashboard BB-450)
- [termux-api](https://wiki.termux.com/wiki/Termux:API) (opcional, para notificaciones nativas)

## Instalación en Termux

```bash
# Actualizar paquetes base
pkg update && pkg upgrade -y

# Instalar Python, Git y Termux API (para notificaciones)
pkg install python git termux-api -y

# Clonar el repositorio (o copiar la carpeta mobile_terminal/)
# git clone https://github.com/tuusuario/BB-450
# cd BB-450/mobile_terminal

# Instalar dependencias Python
pip install -r requirements.txt
```

## Configuración

Editar `config.py` y cambiar la IP del PC donde corre el dashboard:

```python
WS_HOST = "192.168.1.100"   # ← IP de tu PC (LAN)
```

Para encontrar la IP en el PC:
- **Windows:** `ipconfig` (buscar IPv4)
- **Linux/macOS:** `ip addr` o `ifconfig`

## Ejecución

```bash
cd mobile_terminal/
python main.py
```

Si la conexión es exitosa, verás los datos del dashboard en vivo.

## Controles

| Tecla | Acción |
|-------|--------|
| `b`   | **LONG** — Abrir posición larga |
| `s`   | **SHORT** — Abrir posición corta |
| `c`   | **Cerrar** — Cerrar todas las posiciones |
| `q`   | **Salir** — Cerrar la aplicación |

## ¿Cómo funciona?

1. El dashboard BB-450 en el PC debe exponer un servidor WebSocket en el puerto 8765.
2. Este cliente se conecta a ese WebSocket y recibe snapshots JSON del mercado cada ~1s.
3. Los datos se despliegan en una interfaz TUI profesional con 3 paneles.
4. Las órdenes se envían de vuelta al PC a través del mismo WebSocket.

## Widgets de la interfaz

```
┌─────────────────────────────────────────────┐
│  OrderFlow                                  │
│  PRICE  $67,890  TREND  LONG  CONF  92%     │
│  DELTA  +450     CVD    +1200               │
├─────────────────────────────────────────────┤
│  AI & Regime                                │
│  AI  LONG   REGIME  ABSORCION_INST...       │
│  GEMINI BIAS  +1.0                          │
├─────────────────────────────────────────────┤
│  Position & PnL                             │
│  POSITION  LONG   SIZE  0.050 BTC           │
│  PnL %    +2.34%   PnL USDT  +$78.12       │
├─────────────────────────────────────────────┤
│  🟢 Conectado  [b] LONG [s] SHORT [c] Cerrar│
└─────────────────────────────────────────────┘
```

## Arquitectura de archivos

```
mobile_terminal/
├── requirements.txt   # Dependencias Python
├── config.py          # Configuración (IP, puerto)
├── ws_client.py       # Cliente WebSocket asíncrono
├── tui_app.py         # Interfaz TUI (Textual)
├── main.py            # Punto de entrada
└── README.md          # Este archivo
```

## Solución de problemas

**"Cannot connect"**: Verificar que:
- El PC y el Android estén en la misma red WiFi
- El dashboard BB-450 esté corriendo con el servidor WebSocket activo
- `WS_HOST` en `config.py` tenga la IP correcta del PC
- No haya firewall bloqueando el puerto 8765

**"termux-notification: not found"**: Instalar `termux-api`:
```bash
pkg install termux-api
```

**Pantalla muy pequeña**: Usar el zoom de Termux o rotar el dispositivo a horizontal.
# BB-450-TRML
