#!/usr/bin/env bash
# Install Bluetooth Battery for the current user (no root needed).
#   ./install.sh            install + start on login + launch now
#   ./install.sh uninstall  remove everything
set -euo pipefail

BIN="$HOME/.local/bin/btbattery"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications/btbattery.desktop"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/btbattery.desktop"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Stop a running instance: only the process that owns the app's D-Bus name.
stop_running() {
  local pid
  pid=$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        GetConnectionUnixProcessID s io.github.btbattery 2>/dev/null | awk '{print $2}')
  [[ -n "$pid" ]] && kill "$pid" 2>/dev/null && sleep 0.5 || true
}

if [[ "${1:-}" == "uninstall" ]]; then
  stop_running
  rm -f "$BIN" "$APPS" "$AUTOSTART"
  echo "Uninstalled. (Settings remain in ~/.config/btbattery/)"
  exit 0
fi

python3 -c "import PyQt6.QtDBus" 2>/dev/null || {
  echo "PyQt6 is missing. Install it with:  sudo pacman -S python-pyqt6"; exit 1; }

install -Dm755 "$HERE/btbattery.py" "$BIN"

DESKTOP="[Desktop Entry]
Type=Application
Name=Bluetooth Battery
Comment=Battery levels of Bluetooth devices in the panel
Exec=$BIN
Icon=bluetooth
Terminal=false
Categories=Utility;System;
X-KDE-autostart-after=panel"

install -Dm644 /dev/stdin "$APPS" <<<"$DESKTOP"
install -Dm644 /dev/stdin "$AUTOSTART" <<<"$DESKTOP"

stop_running
nohup "$BIN" >/dev/null 2>&1 &
disown
echo "Installed to $BIN, set to start on login, and launched."
