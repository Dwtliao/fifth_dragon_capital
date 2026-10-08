"""Run morning tasks in order and retain results for the dashboard."""

import os
import subprocess
import sys


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
    journals = sorted(diary.glob("trading_journal_*.md"), key=lambda p: p.stat().st_mtime)
    if journals:
        steps.append(runner(
            "Journal", [sys.executable, "-m", "morning_brief.journal_sync", "--file", str(journals[-1])],
            project_root,
        ))
    else:
        steps.append({"name": "Journal", "status": "skipped", "returncode": None,
                      "output": "No journal files found."})

    # Check immediately before sync: journal processing may take some time.
    if token_is_fresh():
        steps.append(runner("E*TRADE sync", [sys.executable, "-m", "etrade_sync", "sync"], project_root))
    else:
        steps.append({"name": "E*TRADE sync", "status": "skipped", "returncode": None,
                      "output": "E*TRADE token is missing or was not written today. Re-authenticate and rerun the pipeline."})

    # A partial sync can still update holdings; generate the brief, but expose
    # its freshness limitation rather than treating the whole pipeline as OK.
    steps.append(runner("Brief", [sys.executable, "-m", "morning_brief.brief"], project_root))
    return steps
