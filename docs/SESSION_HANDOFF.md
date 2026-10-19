# Session handoff — October 18, 2026

Read this first when resuming, then CLAUDE.md and PLAN.md's current-status section.
This records completed work; do not restart the old October 14 phased plan.

## Git and current scope

- Main workspace: `/Users/davidliao/git_repos/fifth_dragon_capital`.
- Branch: `dev/journal-review-refinements`; HEAD `368f514`.
- `85f8e2e`: checkpoint opt-in alerts, disabled email, inline editing/no-expiry work.
- `368f514`: deployed Watch Levels retirement and preservation evidence.
- October 18: `git fetch origin` confirmed the remote branch now includes both
  implementation commits through `368f514`; no divergence before this update.
- User accepted alert testing on October 18 and explicitly requested saving,
  committing, pushing and creating a PR. **Do not merge the PR.**
- This final update records acceptance and corrects P1's misleading “90d” default
  label to incremental sync, with help explaining overlap and initial fallback.
  Syntax parsing and `git diff --check` passed; no production sync was run.
- Deployment and user acceptance are complete. Remaining work is publishing this
  update and opening the PR for review, not another migration or feature phase.

## Decisions and implemented behavior

- P10 has **Position Stops** and notes. Existing automatic stop-alert maintenance
  remains as before. Dashboard alerts are not broker stop orders.
- P11 is the discretionary monitoring inventory. Edit Label/Condition/Level in
  the table and click **Save changes**; row selection is only for bulk controls
  and history. Stops remain editable in P10, not in this grid.
- New manual and explicitly approved journal alerts have **no expiry**. Normal
  creation has no timestamp/day-expiry input. Existing expirations are retained
  unless the user explicitly removes them. Ordinary schema refresh no longer
  replays migration 069's legacy expiry backfill once the column exists.
- **Email alerts are disabled in code, regardless of SMTP credentials.** User
  does not want email. Do not re-enable it or restart scheduled polling.
- P11 **Refresh quotes** shows on-page notices for satisfied conditions using
  fresh available snapshots, respecting pause/snooze/expiry/archive/disabled
  states. No background monitoring while the page is closed; no confirmed-cross
  guarantee. Quotes older than ten minutes are excluded from these notices.
- Source reconciliation maintains only exact position-stop identities. It cannot
  create/recreate, overwrite, reactivate or retire discretionary alerts.
- Discretionary Delete retains scoped confirmation and audit/event snapshots,
  but creates no new purge block. Stop deletion retains purge protection.
  Historical purge intent and journal approval guards remain intact.
- Saved **Watch Levels** are completely retired as an active reference feature:
  removed from P10, YAML seeds, new brief tables and analysis evidence. Older
  cached briefs hide the retired table when displayed; saved files are unchanged.
- Journal extraction still stages ideas/evidence. Its `watch_levels` output field
  names suggestions, not a saved reference inventory. Explicit creation/approval
  is still required. Do not remove shared journal tables or regenerate alerts
  from old journal/saved-level data.

## Production migration and evidence

Migration `data_model/078_retire_saved_watch_levels.sql` was applied on October 15
at 18:04:57 CDT. **Do not apply it again as unfinished work.**

- Removed 27 watch-reference rows and `support`, `resistance`, `alert_above` columns.
- Kept shared `key_levels` for position metadata, restricting its section to positions.
- Preserved all 23 position rows (including IDs/stops/notes/timestamps), 39 alerts,
  all 50 other public/recovery tables, all sequence values and shared triggers.
- Counts are October 15 deployment evidence, not a claim about today's inventory.
- Legacy `key_levels_watch` / `watch:*` alert identities remain intentionally for
  control/history continuity. They do not mean a saved Watch Levels feature survives.
- Production migration verified preserved data before commit, then read-only
  checks reconfirmed it. No broker sync/order, real extraction/poll/email occurred.
- Final isolated regression: **222 tests passed, none skipped**. Streamlit AppTest
  and live Chrome P10/P11 checks passed. No later behavioral edits are implied.
- Evidence files:
  - `docs/watch_levels_step4_verification_20261015.json`
  - `docs/watch_levels_step5_deployment_20261015.json`

## Backups, recovery and rollback

Latest deployment backup (confirmed present October 18):

`/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/db_backups/fifth_dragon_capital_20261015_180344.dump`

- PostgreSQL custom dump, 1,977,174 bytes; archive listing verified.
- SHA-256: `f713ef03ab4aa634a90c8fd7df89fdd39c6fd9b9a75d19d416a3968cbbf68081`.
- Earlier October 15 backup `fifth_dragon_capital_20261015_175416.dump` was restored
  into an isolated cluster to test retirement and repeatability.
- Retain `recovery_alert_journal_20261014_123231` and the October 14 full backup.
  They contain the pre-blank-slate recovery state (including 92 original alerts);
  that is distinct from the October 15 working inventory of 39 preserved alerts.
- Read `docs/alert_journal_blank_slate_recovery_20261014.md` before any restoration.
- No restoration or backup cleanup is authorized. Never restore a full old dump
  over newer trading data, reset sequences, or silently reinstate original alerts.
- Old code expects dropped columns: reverting to `85f8e2e` alone is not a valid
  post-migration rollback. Prefer a forward fix; any targeted schema/data recovery
  needs review and explicit direction. Take a fresh backup before new DB changes.

## Operational state

- October 18: `launchctl print gui/$(id -u)/com.fifthdragon.alert-poller` confirmed
  the service absent. Keep it unloaded. Recheck after reboot/login; it previously
  reloaded after reboot. Do not run manual polling during routine verification.
- User starts Streamlit manually. The agent's October 15 verification instance
  was stopped; today's Streamlit process state was not inspected.
- Start from the main checkout using the existing venv:
  `/Users/davidliao/git_repos/py312/venv/bin/streamlit run dashboard/app.py`.
- No live broker/API/extraction calls are needed to resume this task.

## Isolated test artifacts

- Registered detached worktree: `/private/tmp/fdc-watch-levels-step4`, base `85f8e2e`.
  Contains the tested prototype and uncommitted follow-up copies. Production
  source at `368f514` is authoritative now; do not blindly copy this worktree
  back over newer documentation or code. Keep until acceptance; no cleanup done.
- Disposable PostgreSQL cluster: `/private/tmp/fdc_section1_pg`, socket
  `/private/tmp`, port 55441; stopped after verification on October 15.
- Restored disposable database: `fdc_watch_levels_step4` (already migrated).
- `scripts/verify_watch_levels_retirement.py` requires a **fresh pre-migration**
  isolated restore, exact guarded local URL and a checkout without `.env`.
  It is not a production migration/restoration command.
- Temporary test logs may disappear; committed JSON reports and actual backups
  are the durable evidence.
- For future DB tests use disposable schemas in an isolated database and dummy
  broker credentials, mocked delivery/quotes/LLMs. `.env` overrides inherited
  config, so do not copy the production `.env` into an isolated test checkout.

## Next actions

1. Commit this handoff/plan and P1 label update, then normally push
   `dev/journal-review-refinements`; never force-push.
2. Open a PR against main covering the completed alert simplification and saved
   Watch Levels retirement. Check GitHub for its current publication status.
3. Leave the PR unmerged for user review. Merge requires explicit direction.
4. Keep email disabled and polling unloaded. Do not assume older unchecked plan
   sections authorize table consolidation, holding-stop policy changes, journal
   dismissal/recurrence redesign, production cleanup/restoration, history cleanup
   or notification resumption. Preserve recovery snapshots and test artifacts.
