#!/bin/bash
# Weekly full-database backup via pg_dump.
# Runs every Sunday at 5:00 AM (before the 6:00 AM daily sync and 7:00 AM weekly sync).
# Stores backups in Dropbox for off-machine durability — same folder tree
# morning_brief.md already writes to, so no new sync setup needed.
# Retention: deletes any backup older than RETENTION_WEEKS.

set -euo pipefail

PROJECT_DIR="/Users/davidliao/git_repos/fifth_dragon_capital"
PG_DUMP="/Applications/Postgres.app/Contents/Versions/18/bin/pg_dump"
NOTIFY="/opt/homebrew/bin/terminal-notifier"
BACKUP_DIR="$HOME/Library/CloudStorage/Dropbox/Etrade/db_backups"
LOG_FILE="$PROJECT_DIR/logs/backup_db.log"
RETENTION_WEEKS=8

cd "$PROJECT_DIR"
mkdir -p "$BACKUP_DIR" "$PROJECT_DIR/logs"

# .env is plain KEY=value, no quoting — safe to source directly for DATABASE_URL.
set -a
source "$PROJECT_DIR/.env"
set +a

timestamp=$(date '+%Y%m%d_%H%M%S')
backup_file="$BACKUP_DIR/fifth_dragon_capital_${timestamp}.dump"

echo "======================================" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Backup started -> $backup_file" >> "$LOG_FILE"

if "$PG_DUMP" "$DATABASE_URL" -Fc -f "$backup_file" >> "$LOG_FILE" 2>&1; then
    size=$(du -h "$backup_file" | cut -f1 | xargs)
    echo "$(date '+%Y-%m-%d %H:%M:%S') Backup completed successfully ($size)" >> "$LOG_FILE"
    "$NOTIFY" -title "Fifth Dragon Capital" -subtitle "DB Backup" \
              -message "Weekly backup completed successfully ($size)"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: pg_dump failed" >> "$LOG_FILE"
    rm -f "$backup_file"
    "$NOTIFY" -title "Fifth Dragon Capital" -subtitle "DB Backup" \
              -message "Weekly backup FAILED — check logs" -sound Basso
    exit 1
fi

# Retention: remove backups older than RETENTION_WEEKS.
deleted=$(find "$BACKUP_DIR" -name "fifth_dragon_capital_*.dump" -mtime "+$((RETENTION_WEEKS * 7))" -print -delete)
if [[ -n "$deleted" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Pruned backups older than ${RETENTION_WEEKS} weeks:" >> "$LOG_FILE"
    echo "$deleted" >> "$LOG_FILE"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Backup job finished" >> "$LOG_FILE"
