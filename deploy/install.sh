#!/usr/bin/env bash
# Installation de Evy sur la Raspberry Pi (Raspberry Pi OS 64 bits, avec bureau).
set -e
cd "$(dirname "$0")/.."

sudo apt update
sudo apt install -y python3-venv v4l-utils curl

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo ">> Remplis le fichier .env (clé API, Skill ID)."; }

sudo usermod -aG video "$USER"
sed -e "s#/home/pi#$HOME#g" -e "s#User=pi#User=$USER#" deploy/evy.service | sudo tee /etc/systemd/system/evy.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now evy

mkdir -p ~/.config/labwc
grep -q kiosk.sh ~/.config/labwc/autostart 2>/dev/null || echo "$PWD/deploy/kiosk.sh &" >> ~/.config/labwc/autostart

echo ">> Evy tourne. Teste : curl localhost:5000/api/history"
