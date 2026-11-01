#!/usr/bin/env bash
# Run Predicta on your own Linux PC (Zorin OS / Ubuntu / Debian) and use it from your browser.
#   First time:   cp .env.example .env   and fill in your keys (nano .env)
#   Then:         ./scripts/run_local.sh
# It sets up Python the first time, starts the server on 127.0.0.1:8000 and opens
# http://127.0.0.1:8000/trading in your browser. Keep this window open: the scans run inside it.
# Set NO_BROWSER=1 to skip opening the browser (used by the autostart service).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Install it with:  sudo apt install -y python3 python3-venv python3-pip" >&2
    exit 1
fi
if [ ! -f .env ]; then
    echo "No .env file found. Run:  cp .env.example .env   then   nano .env   and add your keys." >&2
    exit 1
fi
if [ ! -d .venv ]; then
    echo "Setting things up (first run only, takes a few minutes)..."
    if ! python3 -m venv .venv; then
        echo "Could not create the Python environment. Install the missing piece with:  sudo apt install -y python3-venv" >&2
        rm -rf .venv
        exit 1
    fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

PORT="${PORT:-8000}"
URL="http://127.0.0.1:${PORT}/trading"

if [ "${NO_BROWSER:-0}" != "1" ] && command -v xdg-open >/dev/null 2>&1; then
    ( sleep 8; xdg-open "$URL" >/dev/null 2>&1 || true ) &
fi

echo
echo "Predicta is starting. Your browser will open $URL"
echo "KEEP THIS WINDOW OPEN while you want Predicta to run (the daily scans happen inside it). Ctrl+C to stop."
echo

# Ask the system not to sleep while Predicta is running (if systemd-inhibit is available).
CMD=(uvicorn app:app --host 127.0.0.1 --port "$PORT")
if command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --what=sleep:idle --who=Predicta --why="scheduled market scans" true >/dev/null 2>&1; then
    exec systemd-inhibit --what=sleep:idle --who=Predicta --why="scheduled market scans" "${CMD[@]}"
fi
exec "${CMD[@]}"
