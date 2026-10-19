# Recovery handoff — temporary blank alert/journal test

Created October 14, 2026 (America/Chicago). Read this before restoring alerts or
resuming polling. The user requested backing up and clearing alert and journal
data to test ingestion of a new journal. This is a temporary production test.

## Durable recovery sources

Primary targeted snapshot: PostgreSQL schema
`recovery_alert_journal_20261014_123231` in the same configured database as the
application (`etrade_sync.db.get_connection()`, configuration from `.env`).
These are persistent ordinary tables, NOT connection-scoped TEMP tables. They
survive closing the terminal and restarting the application.

Full database backup (created successfully, archive listing verified):

`/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/db_backups/fifth_dragon_capital_20261014_123231.dump`

Format: PostgreSQL custom dump; approximately 2.1 MB. Created using
`bash scripts/backup_db.sh --no-prune`. No old backups were pruned by that run.
The full dump precedes the recovery-schema creation; use its original public
tables if the targeted snapshot is unavailable. Do not restore the entire
database over production merely to recover alerts: that would overwrite newer
unrelated trading data.

## Snapshot inventory

| Table | Rows saved | Working table after reset |
|---|---:|---|
| price_alerts | 92 | Empty |
| alert_notification_events | 42 | Empty |
| alert_controls | 50 | Empty |
| alert_purge_blocks | 7 | Empty |
| alert_action_history | 18 | Empty |
| alert_poll_runs | 1466 | Empty |
| journal_review_items | 57 | Empty |
| journal_review_evidence | 60 | Empty |
| journal_review_decisions | 0 | Empty |
| journal_sync_log | 19 | Empty |
| key_levels | 51 | Preserved, NOT cleared |

Every snapshot table was compared against its source with bidirectional
`EXCEPT ALL` before deletion. Snapshot creation and clearing were committed in
one transaction under table locks and the shared poller advisory lock.
Sequences were NOT reset. A separate read-only connection subsequently verified
zero alerts, review items, and sync logs; 92 backed-up alerts; 51 saved key levels.
Trading/holdings tables were not changed. Existing local PLAN.md edits were
preserved; no code, commit, push, or migration was performed.

## Operational state and test expectations

The launchd agent `com.fifthdragon.alert-poller` was unloaded using
`launchctl bootout gui/$(id -u)/com.fifthdragon.alert-poller`.
Its installed plist remains at
`~/Library/LaunchAgents/com.fifthdragon.alert-poller.plist`.
The service was confirmed absent after reset. Keep it paused until explicitly
ready to resume live notifications. Manual polling remains possible; do not use
it during this test. Recheck scheduler state in a new login/session.

Journal ingestion stages review proposals and should create zero live alerts
until explicit approval. Refresh P10 and use Sync Latest Journal for the test.
The sync log was cleared, so old files are eligible for ingestion again; avoid
Sync Last 5 Journals if testing only one new file.

Saved position/watch levels remain populated. P10 Save All, Morning Brief
generation, or other source reconciliation can recreate alerts. Avoid those
operations during the ingestion-only test. No journal was ingested by the agent
as part of the reset. Approvals may change saved stops as well as create alerts.
Controls and purge blocks were temporarily cleared and MUST be recovered along
with alerts when returning to the original state.

The ordinary Sunday full backup still runs through the daily 6 AM launchd sync
job. It is separate from the paused alert poller.

## Restoration workflow for a future session

1. Read CLAUDE.md, PLAN.md's latest handoff, and this file; preserve local changes.
   Inspect current table counts and poller status. Do not repeat the reset.
2. Confirm the user wants restoration now and whether any test-created alerts or
   proposals should be retained. Snapshot/export current test data first. Do not
   blindly merge snapshots with test rows or overwrite newer review decisions.
3. Keep scheduled/manual polling stopped. Acquire the poller advisory lock
   `(75075, 0)` and appropriate table locks, with bounded lock timeouts. Abort if
   another worker owns the lock. Inspect current foreign keys and schema before
   preparing a transaction; no unbounded CASCADE or destructive schema rollback.
4. Prepare a targeted transaction to replace the 10 working alert/journal tables
   from the identically named recovery-schema tables, preserving original IDs,
   timestamps, controls, purge blocks, and notification statuses. Use explicit
   column lists after confirming schema compatibility. Delete dependent rows
   before parent rows; restore parents before dependents. In particular restore
   price_alerts before alert_notification_events, and journal_review_items before
   journal_review_evidence/journal_review_decisions. Restore controls/purge blocks
   after price_alerts to avoid recreation triggers suppressing historical rows.
   Inspect triggers first; do not disable them casually.
5. key_levels was NOT cleared: compare it with the saved snapshot. Restore only
   specifically agreed test-induced changes; do not replace all saved levels and
   lose legitimate later edits.
6. Verify restored rows against the snapshot using bidirectional EXCEPT ALL,
   validate references and expected counts, and ensure sequences remain at least
   as high as both their current values and the restored maximum IDs. Never
   rewind sequences. Commit only after verification; roll back on any mismatch.
7. Review expirations and notification retry state before resuming. Restoring old
   rows does not prevent notifications: armed alerts and pending/failed deliveries
   may send on the next poll. Keep polling paused until the user directs resumption.
8. When authorized, reload the installed agent with
   `launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.fifthdragon.alert-poller.plist"`.
   Its RunAtLoad setting runs a poll immediately. Verify the job and review logs.
9. Retain the recovery schema and full dump until recovery is accepted. Do not
   automatically drop backups or restore unrelated production tables.

This file deliberately provides a reviewed restoration procedure rather than an
auto-running restore script. The temporary reset helper at
`/private/tmp/blank_alert_journal_20261014.py` is NOT a restore tool and may disappear.
Do not rerun it. Recovery relies on the persistent database schema and full dump.
