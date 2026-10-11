#!/usr/bin/env bash
# Lance l'écran Evy en plein écran sur la télé.
# Raspberry Pi OS (Wayland/labwc) : ajoute cette ligne à ~/.config/labwc/autostart :
#   /home/pi/evy/deploy/kiosk.sh &

# Attendre que le serveur Evy réponde
until curl -s -o /dev/null http://localhost:5000/; do sleep 2; done

BROWSER=$(command -v chromium || command -v chromium-browser)
exec "$BROWSER" \
  --kiosk --noerrdialogs --disable-infobars --incognito \
  --disable-session-crashed-bubble --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required \
  http://localhost:5000/
