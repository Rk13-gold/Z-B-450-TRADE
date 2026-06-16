#!/usr/bin/env bash
set -e

echo "==> Instalando servicios systemd para BB-450..."

# Copy service files and tmpfiles config
sudo cp /home/RK13/RK13/BB-450/scripts/bb450-dashboard.service /etc/systemd/system/
sudo cp /home/RK13/RK13/BB-450/scripts/bb450-bore.service /etc/systemd/system/
sudo cp /home/RK13/RK13/BB-450/scripts/bb450-tmpfiles.conf /etc/tmpfiles.d/bb450.conf

sudo mkdir -p /tmp/bb450
sudo chmod 777 /tmp/bb450

# Reload systemd, enable and start
sudo systemctl daemon-reload

sudo systemctl enable bb450-dashboard.service
sudo systemctl enable bb450-bore.service

sudo systemctl start bb450-dashboard.service
sudo systemctl start bb450-bore.service

echo "==> Servicios instalados y arrancados."
echo "    Ver estado:  systemctl status bb450-dashboard bb450-bore"
echo "    Ver logs:    journalctl -u bb450-dashboard -f"
echo "    Ver puerto:  cat /tmp/bb450/bore_port.txt"
