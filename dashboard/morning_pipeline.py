"""Run morning tasks in order and retain results for the dashboard."""

import os
import subprocess
import sys


def positions_sync_message(added, removed, stats):
    """Describe saved metadata and reconciliation without overstating archive changes."""
    lines = ['Positions metadata synced successfully.']
    if added:
        lines.append(f"Added {len(added)} {'symbol' if len(added)==1 else 'symbols'}: {', '.join(added)}.")
    if removed:
        lines.append(f"Removed {len(removed)} closed-position {'entry' if len(removed)==1 else 'entries'}: {', '.join(removed)}.")
    if not added and not removed:
        lines.append('No changes to the saved symbol list were needed.')
    created, updated = stats.get('created', 0), stats.get('updated', 0)
    if not created and not updated:
        lines.append('Managed alert check: no alerts created or updated.')
    else:
        lines.append(f'Managed alert check: {created} new; {updated} updated.')
    if stats.get('archived'):
        lines.append(f"{stats['archived']} alerts marked or kept archived (includes previously archived alerts).")
    if stats.get('held_archived'):
        lines.append(f"{stats['held_archived']} additional held-symbol watch alerts archived.")
    if stats.get('deduped'):
        lines.append(f"{stats['deduped']} duplicate {'alert' if stats['deduped']==1 else 'alerts'} archived.")
    lines.append('Saved stops and notes were preserved. No trades were placed.')
    return '\n\n'.join(lines)


def run_command(name, command, project_root, source="p10_morning_pipeline"):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, cwd=str(project_root),
            env={**os.environ, "SYNC_TRIGGERED_BY": source},
        )
        return {
            "name": name,
            "status": "success" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "output": (result.stdout + result.stderr).strip(),
        }
    except OSError as exc:
        return {"name": name, "status": "failed", "returncode": None, "output": str(exc)}


def run_morning_pipeline(project_root, diary, token_is_fresh, runner=run_command):
    steps = []
    # Refresh broker state before journal review/brief; extraction never activates alerts.
    if token_is_fresh():
        steps.append(runner("E*TRADE sync", [sys.executable, "-m", "etrade_sync", "sync"], project_root))
    else:
        steps.append({"name": "E*TRADE sync", "status": "skipped", "returncode": None,
                      "output": "E*TRADE token is missing or was not written today. Re-authenticate and rerun the pipeline."})
    journals = sorted(diary.glob("trading_journal_*.md"), key=lambda p: p.stat().st_mtime)
    if journals:
        steps.append(runner(
            "Journal", [sys.executable, "-m", "morning_brief.journal_sync", "--file", str(journals[-1])],
            project_root,
        ))
    else:
        steps.append({"name": "Journal", "status": "skipped", "returncode": None,
                      "output": "No journal files found."})

    # A partial sync can still update holdings; generate the brief, but expose
    # its freshness limitation rather than treating the whole pipeline as OK.
    steps.append(runner("Brief", [sys.executable, "-m", "morning_brief.brief"], project_root))
    return steps
