#!/bin/bash
# Daily sync — accounts, balances, positions (fast refresh)
# Runs every day at 6:00 AM

set -euo pipefail

PROJECT_DIR="/Users/davidliao/git_repos/fifth_dragon_capital"
PYTHON="/Users/davidliao/git_repos/py312/venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/sync_daily.log"
TOKEN_FILE="$HOME/.config/etrade/tokens.json"

cd "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/logs"

echo "======================================" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Daily sync started" >> "$LOG_FILE"

# E*TRADE tokens expire at midnight ET. Check that the token file exists and
# was written today (in ET). If stale, notify and exit rather than hitting 401s.
check_token_freshness() {
    if [[ ! -f "$TOKEN_FILE" ]]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Token file missing — re-authentication required" >> "$LOG_FILE"
        osascript -e 'display notification "Run: python -m etrade_sync auth" with title "Fifth Dragon Capital" subtitle "Re-authentication required" sound name "Basso"'
        exit 0
    fi
    # Get today's date in ET (UTC-4 summer / UTC-5 winter)
    today_et=$(TZ="America/New_York" date '+%Y-%m-%d')
    token_date=$(TZ="America/New_York" date -r "$TOKEN_FILE" '+%Y-%m-%d')
    if [[ "$token_date" != "$today_et" ]]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Token is stale (written $token_date, today is $today_et ET) — re-authentication required" >> "$LOG_FILE"
        osascript -e 'display notification "Run: python -m etrade_sync auth" with title "Fifth Dragon Capital" subtitle "Token expired — re-auth needed" sound name "Basso"'
        exit 0
    fi
}

check_token_freshness

run_sync() {
    "$PYTHON" -m etrade_sync sync --only accounts   >> "$LOG_FILE" 2>&1
    "$PYTHON" -m etrade_sync sync --only balances   >> "$LOG_FILE" 2>&1
    "$PYTHON" -m etrade_sync sync --only positions  >> "$LOG_FILE" 2>&1
    "$PYTHON" -m etrade_sync sync --only transactions >> "$LOG_FILE" 2>&1
    "$PYTHON" -m etrade_sync sync --only orders     >> "$LOG_FILE" 2>&1
}

if run_sync; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Daily sync completed successfully" >> "$LOG_FILE"
    osascript -e 'display notification "Daily sync completed successfully" with title "Fifth Dragon Capital" subtitle "E*TRADE Pipeline"'
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: Daily sync failed" >> "$LOG_FILE"
    osascript -e 'display notification "Daily sync FAILED — check logs" with title "Fifth Dragon Capital" subtitle "E*TRADE Pipeline" sound name "Basso"'
fi
