#!/bin/bash
# Weekly sync — full history refresh for transactions and orders
# Runs every Sunday at 7:00 AM

set -euo pipefail

PROJECT_DIR="/Users/davidliao/git_repos/fifth_dragon_capital"
PYTHON="/Users/davidliao/git_repos/py312/venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/sync_weekly.log"
TOKEN_FILE="$HOME/.config/etrade/tokens.json"

cd "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/logs"

echo "======================================" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Weekly full sync started" >> "$LOG_FILE"

check_token_freshness() {
    if [[ ! -f "$TOKEN_FILE" ]]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Token file missing — re-authentication required" >> "$LOG_FILE"
        osascript -e 'display notification "Run: python -m etrade_sync auth" with title "Fifth Dragon Capital" subtitle "Re-authentication required" sound name "Basso"'
        exit 0
    fi
    today_et=$(TZ="America/New_York" date '+%Y-%m-%d')
    token_date=$(TZ="America/New_York" date -r "$TOKEN_FILE" '+%Y-%m-%d')
    if [[ "$token_date" != "$today_et" ]]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Token is stale (written $token_date, today is $today_et ET) — re-authentication required" >> "$LOG_FILE"
        osascript -e 'display notification "Run: python -m etrade_sync auth" with title "Fifth Dragon Capital" subtitle "Token expired — re-auth needed" sound name "Basso"'
        exit 0
    fi
}

check_token_freshness

if "$PYTHON" -m etrade_sync sync --days 30 >> "$LOG_FILE" 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Weekly sync completed successfully" >> "$LOG_FILE"
    osascript -e 'display notification "Weekly sync completed successfully" with title "Fifth Dragon Capital" subtitle "E*TRADE Pipeline"'
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Weekly sync failed" >> "$LOG_FILE"
    osascript -e 'display notification "Weekly sync FAILED — check logs" with title "Fifth Dragon Capital" subtitle "E*TRADE Pipeline" sound name "Basso"'
fi
