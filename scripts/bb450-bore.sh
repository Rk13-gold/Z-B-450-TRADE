#!/usr/bin/env bash
set -e

BORE_PORT_FILE="${BORE_PORT_FILE:-/tmp/bb450/bore_port.txt}"
DASHBOARD_DIR="${DASHBOARD_DIR:-/home/RK13/RK13/BB-450}"

cd "$DASHBOARD_DIR"

# Start bore, capture stderr to get the port announcement
bore local 8765 --to bore.pub 2>&1 | while IFS= read -r line; do
    echo "$line"
    # Extract port from: listening at bore.pub:PORT
    if [[ "$line" =~ listening\ at\ bore\.pub:([0-9]+) ]]; then
        echo "${BASH_REMATCH[1]}" > "$BORE_PORT_FILE"
        chmod 644 "$BORE_PORT_FILE"
    fi
done
