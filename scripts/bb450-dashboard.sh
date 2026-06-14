#!/usr/bin/env bash
set -e

DASHBOARD_DIR="${DASHBOARD_DIR:-/home/RK13/RK13/BB-450}"
cd "$DASHBOARD_DIR"

exec python dashboard_gui.py
