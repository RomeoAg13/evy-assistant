#!/usr/bin/env bash
# Écran Evy en plein écran sur la télé (Wayland / labwc).
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

wlr-randr --output HDMI-A-2 --mode 1920x1080@60.000000 2>/dev/null

until curl -s -o /dev/null http://localhost:5000/; do sleep 2; done

BROWSER=$(command -v chromium || command -v chromium-browser)
exec "$BROWSER" --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars \
  --incognito --disable-session-crashed-bubble --check-for-update-interval=31536000 \
  http://localhost:5000/
