#!/usr/bin/env bash
echo "Deteniendo BB-450..."
pkill -f "python dashboard_gui.py" 2>/dev/null && echo "  Dashboard detenido" || echo "  Dashboard no estaba corriendo"
pkill -f "bore local 8765" 2>/dev/null && echo "  Bore detenido" || echo "  Bore no estaba corriendo"
echo "Listo."
