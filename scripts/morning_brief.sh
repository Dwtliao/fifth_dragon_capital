#!/bin/bash
# Morning brief generator — called by launchd at 6:45 AM
# Produces: ~/Library/CloudStorage/Dropbox/Etrade/trading_diary/morning_brief.md

set -euo pipefail

PROJECT_DIR="/Users/davidliao/git_repos/fifth_dragon_capital"
PYTHON="/Users/davidliao/git_repos/py312/venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/morning_brief.log"

mkdir -p "$PROJECT_DIR/logs"

echo "======================================" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Morning brief started" >> "$LOG_FILE"

cd "$PROJECT_DIR"

"$PYTHON" -m morning_brief.brief >> "$LOG_FILE" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') Morning brief finished" >> "$LOG_FILE"
