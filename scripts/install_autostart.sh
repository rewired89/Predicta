#!/usr/bin/env bash
# Make Predicta start by itself when your computer starts (no window needed).
# Run once:        ./scripts/install_autostart.sh
# Check on it:     systemctl --user status predicta        (logs: journalctl --user -u predicta -f)
# Remove later:    systemctl --user disable --now predicta
# Optional, to keep it running even when you are signed out:  sudo loginctl enable-linger "$USER"
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
chmod +x "$ROOT/scripts/run_local.sh"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/predicta.service" <<UNIT
[Unit]
Description=Predicta (paper trading research app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=NO_BROWSER=1
ExecStart=$ROOT/scripts/run_local.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now predicta
echo "Done. Predicta now starts automatically. Open http://127.0.0.1:8000/trading"
