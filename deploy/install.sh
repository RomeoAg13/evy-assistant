#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
DIR="$PWD"
if [ "$EUID" -eq 0 ]; then echo "Lance ce script SANS sudo."; exit 1; fi

chmod +x deploy/*.sh

sudo apt update
sudo apt install -y python3-venv v4l-utils curl

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo ">> Remplis le fichier .env (clé API)."; }

sudo usermod -aG video "$USER"

sudo tee /etc/systemd/system/evy.service >/dev/null <<SERVICE
[Unit]
Description=Evy (skill Alexa + écran télé)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python server.py
Restart=always
RestartSec=3
SupplementaryGroups=video

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable --now evy

mkdir -p ~/.config/labwc
grep -q kiosk.sh ~/.config/labwc/autostart 2>/dev/null || echo "$DIR/deploy/kiosk.sh &" >> ~/.config/labwc/autostart

echo ">> Evy est installé dans $DIR"
