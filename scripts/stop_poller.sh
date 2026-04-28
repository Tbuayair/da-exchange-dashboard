#!/bin/zsh
# Stop the background poller started by scripts/start_poller.sh.

set -euo pipefail
PID_FILE="/Users/anthonyb/Desktop/Claude/01-Projects/Active07_DAExchangeDashboard/data/poller.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file at $PID_FILE — poller probably not running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped poller pid $PID."
else
  echo "Poller pid $PID is not alive — clearing stale PID file."
fi
rm -f "$PID_FILE"
