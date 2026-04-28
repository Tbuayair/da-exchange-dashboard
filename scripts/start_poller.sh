#!/bin/zsh
# Start the snapshot poller in the background. Logs to data/poller.log.
# Stop with: scripts/stop_poller.sh

set -euo pipefail
PROJECT_DIR="/Users/anthonyb/Desktop/Claude/01-Projects/Active07_DAExchangeDashboard"
PID_FILE="$PROJECT_DIR/data/poller.pid"
LOG_FILE="$PROJECT_DIR/data/poller.log"

mkdir -p "$PROJECT_DIR/data"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Poller already running (pid $(cat "$PID_FILE")). Use scripts/stop_poller.sh first."
  exit 1
fi

cd "$PROJECT_DIR"
nohup .venv/bin/python -m ingest.poller >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Poller started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"
