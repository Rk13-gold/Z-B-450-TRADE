#!/usr/bin/env bash
set -e

BB_HOME="$(cd "$(dirname "$0")" && pwd)"
BORE_PORT_FILE="${BORE_PORT_FILE:-/tmp/bb450/bore_port.txt}"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║      BB-450 - START                  ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

mkdir -p /tmp/bb450

cleanup() {
    echo ""
    echo "[STOP] Cerrando servicios..."
    kill $DASH_PID $BORE_PID 2>/dev/null
    wait $DASH_PID $BORE_PID 2>/dev/null
    echo "[STOP] Todo detenido."
}
trap cleanup EXIT INT TERM

# Start dashboard in background
echo "[1/3] Arrancando Dashboard..."
cd "$BB_HOME"
python dashboard_gui.py &
DASH_PID=$!
echo "  Dashboard PID: $DASH_PID"

sleep 5

# Start bore tunnel in background, capture port
echo "[2/3] Arrancando Bore Tunnel..."
bore local 8765 --to bore.pub > /tmp/bb450/bore_raw.log 2>&1 &
BORE_PID=$!

# Wait for port announcement
sleep 3
BORE_PORT=$(grep -oP 'listening at bore\.pub:\K\d+' /tmp/bb450/bore_raw.log || echo "")
if [ -n "$BORE_PORT" ]; then
    echo "$BORE_PORT" > "$BORE_PORT_FILE"
    chmod 644 "$BORE_PORT_FILE"
    echo "  Bore PID: $BORE_PID  |  Puerto: $BORE_PORT"
else
    echo "  Bore PID: $BORE_PID  |  Puerto: (esperando...)"
fi

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║  TODO CORRIENDO                       ║"
echo "  ║  Dashboard: GUI abierta               ║"
echo "  ║  Bore:      bore.pub:${BORE_PORT:-?????}            ║"
echo "  ║                                       ║"
echo "  ║  Termux: actualizar config.py         ║"
echo "  ║  y correr python main.py              ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  [3/3] Mostrando logs de bore (Ctrl+C para detener todo):"
echo ""

# Follow bore logs
tail -f /tmp/bb450/bore_raw.log
