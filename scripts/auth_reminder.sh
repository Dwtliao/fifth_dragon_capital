#!/bin/bash
# Nightly reminder to re-authenticate E*TRADE before tokens expire at midnight ET.
# Runs at 10:00 PM. If the token is already fresh (re-authed today), stays silent.

TOKEN_FILE="$HOME/.config/etrade/tokens.json"

today_et=$(TZ="America/New_York" date '+%Y-%m-%d')

if [[ ! -f "$TOKEN_FILE" ]]; then
    osascript -e 'display notification "Run: python -m etrade_sync auth" with title "Fifth Dragon Capital" subtitle "No token file found — auth required" sound name "Basso"'
    exit 0
fi

token_date=$(TZ="America/New_York" date -r "$TOKEN_FILE" '+%Y-%m-%d')

if [[ "$token_date" != "$today_et" ]]; then
    osascript -e 'display notification "Run: python -m etrade_sync auth before midnight ET" with title "Fifth Dragon Capital" subtitle "Re-authenticate tonight" sound name "Ping"'
fi
# If token is already fresh, do nothing.
