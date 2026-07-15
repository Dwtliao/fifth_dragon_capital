#!/bin/bash
# Price alert poller — called by launchd every 5 minutes
# Fires/re-arms alerts in the price_alerts table via alerts.poller

set -euo pipefail

PROJECT_DIR="/Users/davidliao/git_repos/fifth_dragon_capital"
PYTHON="/Users/davidliao/git_repos/py312/venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/alert_poller.log"

mkdir -p "$PROJECT_DIR/logs"

cd "$PROJECT_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') Poll started" >> "$LOG_FILE"
"$PYTHON" -m alerts.poller --once >> "$LOG_FILE" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') Poll finished" >> "$LOG_FILE"
