#!/usr/bin/env bash
# Install Bluetooth Battery for the current user (no root needed).
#   ./install.sh            install, start on login, and launch now
#   ./install.sh uninstall  remove everything
#
# The app runs as a systemd user service, so it lives in its own cgroup: it is
# not killed when the terminal (or tool) that installed it goes away, it starts
# with the graphical session, and it is restarted if it ever crashes.
set -euo pipefail

BIN="$HOME/.local/bin/btbattery"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications/btbattery.desktop"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/btbattery.service"
OLD_AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/btbattery.desktop"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Stop an instance not managed by systemd: only the process owning the app's D-Bus name.
stop_running() {
  local pid
  pid=$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        GetConnectionUnixProcessID s io.github.btbattery 2>/dev/null | awk '{print $2}' || true)
  [[ -n "$pid" ]] && kill "$pid" 2>/dev/null && sleep 0.5 || true
}

if [[ "${1:-}" == "uninstall" ]]; then
  systemctl --user disable --now btbattery.service 2>/dev/null || true
  stop_running
  rm -f "$BIN" "$APPS" "$UNIT" "$OLD_AUTOSTART"
  systemctl --user daemon-reload
  echo "Uninstalled. (Settings remain in ~/.config/btbattery/)"
  exit 0
fi

python3 -c "import PyQt6.QtDBus" 2>/dev/null || {
  echo "PyQt6 is missing. Install it with:  sudo pacman -S python-pyqt6"; exit 1; }

install -Dm755 "$HERE/btbattery.py" "$BIN"

install -Dm644 /dev/stdin "$APPS" <<EOF
[Desktop Entry]
Type=Application
Name=Bluetooth Battery
Comment=Battery levels of Bluetooth devices in the panel
Exec=systemctl --user start btbattery.service
Icon=bluetooth
Terminal=false
Categories=Utility;System;
EOF

install -Dm644 /dev/stdin "$UNIT" <<'EOF'
[Unit]
Description=Bluetooth Battery tray icons
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=%h/.local/bin/btbattery
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

rm -f "$OLD_AUTOSTART"          # replaced by the systemd unit
systemctl --user daemon-reload
systemctl --user stop btbattery.service 2>/dev/null || true
stop_running                    # e.g. a copy started by hand from a terminal
systemctl --user enable --now btbattery.service
echo "Installed to $BIN as the btbattery systemd user service: running now and on every login."
echo "Status / logs:  systemctl --user status btbattery   ·   journalctl --user -u btbattery"
