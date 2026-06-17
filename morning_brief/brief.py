"""
brief.py — morning brief entrypoint.

Usage:
    python -m morning_brief.brief            # generate and save
    python -m morning_brief.brief --print    # also print to stdout
    python -m morning_brief.brief --dry-run  # print only, do not write file

Reads key_levels.yml from the same directory.
Writes to $TRADING_DIARY/morning_brief.md  (overwrite, always today's brief)
    and $TRADING_DIARY/briefs/morning_brief_YYYYMMDD.md  (archive copy).

$TRADING_DIARY defaults to ~/Library/CloudStorage/Dropbox/Etrade/trading_diary
but can be overridden via the env var TRADING_DIARY or a .env file at the
project root.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# ── project root on sys.path so `python -m morning_brief.brief` works ────────
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import yaml  # PyYAML — add to requirements if not present

from morning_brief import fetchers, formatter

# ── paths ─────────────────────────────────────────────────────────────────────

DEFAULT_DIARY = Path.home() / "Library/CloudStorage/Dropbox/Etrade/trading_diary"

def _diary_path() -> Path:
    env_val = os.getenv("TRADING_DIARY")
    return Path(env_val) if env_val else DEFAULT_DIARY


def _load_key_levels() -> dict:
    yml_path = Path(__file__).parent / "key_levels.yml"
    if not yml_path.exists():
        return {}
    with open(yml_path) as f:
        return yaml.safe_load(f) or {}


# ── main ──────────────────────────────────────────────────────────────────────

def generate_brief() -> str:
    """Fetch all data, render, return the full markdown string."""
    key_levels = _load_key_levels()
    now        = datetime.datetime.now()

    sections = []

    # Header
    sections.append(formatter.render_header(now))

    # Events (skip silently if empty — no events.yml yet)
    try:
        events = fetchers.fetch_fed_events()
        if events:
            sections.append(formatter.render_events(events))
    except Exception as exc:
        sections.append(f"_Events fetch failed: {exc}_\n\n---\n")

    # Global indices
    try:
        sections.append(formatter.render_global_indices(fetchers.fetch_global_indices()))
    except Exception as exc:
        sections.append(f"_Global indices fetch failed: {exc}_\n\n---\n")

    # US futures
    try:
        sections.append(formatter.render_us_futures(fetchers.fetch_us_futures()))
    except Exception as exc:
        sections.append(f"_US futures fetch failed: {exc}_\n\n---\n")

    # Commodities
    try:
        sections.append(formatter.render_commodities(fetchers.fetch_commodities()))
    except Exception as exc:
        sections.append(f"_Commodities fetch failed: {exc}_\n\n---\n")

    # Currencies
    try:
        sections.append(formatter.render_currencies(fetchers.fetch_currencies()))
    except Exception as exc:
        sections.append(f"_Currencies fetch failed: {exc}_\n\n---\n")

    # Volatility
    try:
        sections.append(formatter.render_vol(fetchers.fetch_vol_proxies()))
    except Exception as exc:
        sections.append(f"_Vol proxies fetch failed: {exc}_\n\n---\n")

    # Positions (from key_levels.yml)
    try:
        pos_data = fetchers.fetch_positions(key_levels)
        if pos_data:
            sections.append(formatter.render_positions(pos_data))
    except Exception as exc:
        sections.append(f"_Positions fetch failed: {exc}_\n\n---\n")

    # Key levels (from key_levels.yml watch section)
    try:
        watch_data = fetchers.fetch_watch_levels(key_levels)
        if watch_data:
            sections.append(formatter.render_key_levels(watch_data))
    except Exception as exc:
        sections.append(f"_Key levels fetch failed: {exc}_\n\n---\n")

    # Footer
    sections.append(formatter.render_footer())

    return "\n".join(sections)


def save_brief(content: str) -> tuple[Path, Path]:
    """Write brief to trading_diary. Returns (latest_path, archive_path)."""
    diary = _diary_path()
    diary.mkdir(parents=True, exist_ok=True)

    archive_dir = diary / "briefs"
    archive_dir.mkdir(exist_ok=True)

    date_str      = datetime.date.today().strftime("%Y%m%d")
    latest_path   = diary / "morning_brief.md"
    archive_path  = archive_dir / f"morning_brief_{date_str}.md"

    latest_path.write_text(content, encoding="utf-8")
    archive_path.write_text(content, encoding="utf-8")

    return latest_path, archive_path


def notify(message: str) -> None:
    """Fire a macOS terminal-notifier alert if available."""
    import shutil, subprocess
    notifier = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
    if Path(notifier).exists():
        try:
            subprocess.run(
                [notifier, "-title", "Morning Brief", "-message", message, "-sound", "default"],
                check=False, capture_output=True
            )
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate morning brief")
    parser.add_argument("--print",   action="store_true", help="Print brief to stdout after saving")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout only, do not save")
    args = parser.parse_args()

    print("Fetching market data…", file=sys.stderr)
    brief_md = generate_brief()

    if args.dry_run:
        print(brief_md)
        return

    latest, archive = save_brief(brief_md)
    print(f"✅ Brief saved → {latest}", file=sys.stderr)
    print(f"   Archive     → {archive}", file=sys.stderr)

    if args.print:
        print(brief_md)

    notify(f"Morning brief ready — {datetime.date.today().strftime('%b %-d')}")


if __name__ == "__main__":
    main()
