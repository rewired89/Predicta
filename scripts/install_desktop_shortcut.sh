#!/usr/bin/env bash
# Adds a "Predicta" icon to your Desktop and app menu. Double-click it to start Predicta.
# Run once:   ./scripts/install_desktop_shortcut.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
chmod +x "$ROOT/scripts/run_local.sh"
ENTRY="[Desktop Entry]
Type=Application
Name=Predicta
Comment=Start Predicta and open it in the browser
Exec=$ROOT/scripts/run_local.sh
Path=$ROOT
Terminal=true
Categories=Office;Finance;
"
mkdir -p "$HOME/.local/share/applications"
printf '%s' "$ENTRY" > "$HOME/.local/share/applications/predicta.desktop"
chmod +x "$HOME/.local/share/applications/predicta.desktop"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
    printf '%s' "$ENTRY" > "$DESK/Predicta.desktop"
    chmod +x "$DESK/Predicta.desktop"
    gio set "$DESK/Predicta.desktop" metadata::trusted true 2>/dev/null || true
fi
echo "Done. Look for 'Predicta' on your Desktop or in the app menu."
echo "If the Desktop icon says 'untrusted', right-click it and choose 'Allow Launching'."
