"""
formatter.py — renders morning brief sections as markdown strings.

Each render_* function takes the dict/list produced by the matching
fetcher and returns a markdown string. brief.py concatenates them.
"""

from __future__ import annotations

import datetime
from typing import Optional


# ── helpers ───────────────────────────────────────────────────────────────────

def _pct_arrow(pct: Optional[float]) -> str:
    if pct is None:
        return "  —  "
    if pct >= 0:
        return f"+{pct:.2f}%"
    return f"{pct:.2f}%"


def _pct_emoji(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    if pct >= 1.0:
        return "🟢"
    if pct >= 0:
        return "🟡"
    if pct >= -1.0:
        return "🟡"
    return "🔴"


def _row(label: str, last: float, pct: Optional[float], note: str = "") -> str:
    emoji  = _pct_emoji(pct)
    pct_s  = _pct_arrow(pct)
    note_s = f"  _{note}_" if note else ""
    return f"  {emoji} **{label}**  {last:,.2f}  ({pct_s}){note_s}"


def _error_row(label: str, error: str) -> str:
    return f"  ⚪ **{label}**  _(fetch error: {error})_"


# ── section renderers ─────────────────────────────────────────────────────────

def render_header(generated_at: Optional[datetime.datetime] = None) -> str:
    if generated_at is None:
        generated_at = datetime.datetime.now()
    date_str = generated_at.strftime("%a %b %-d %Y")
    time_str = generated_at.strftime("%-I:%M%p EDT")
    return (
        f"# Morning Brief — {date_str}\n"
        f"_generated {time_str}_\n\n"
        f"---\n"
    )


def render_events(events: list[dict]) -> str:
    if not events:
        return ""

    lines = ["## ⚠️  EVENTS & CATALYSTS\n"]
    for e in events:
        days   = e.get("days_away", "?")
        date   = e.get("date", "")
        title  = e.get("title", "")
        detail = e.get("detail", "")

        if days == 0:
            prefix = "**TODAY**"
        elif days == 1:
            prefix = "**TOMORROW**"
        elif isinstance(days, int) and days < 0:
            prefix = f"_{abs(days)}d ago_"
        else:
            prefix = f"in {days}d  ({date})"

        detail_s = f" — {detail}" if detail else ""
        lines.append(f"  📅 {prefix}  {title}{detail_s}")

    return "\n".join(lines) + "\n\n---\n"


def render_global_indices(data: list[dict]) -> str:
    lines = ["## 🌏  OVERNIGHT GLOBAL\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
        else:
            lines.append(_row(row["label"], row["last"], row.get("pct")))
    return "\n".join(lines) + "\n\n---\n"


def render_us_futures(data: list[dict]) -> str:
    lines = ["## 📊  US FUTURES (pre-market)\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
        else:
            lines.append(_row(row["label"], row["last"], row.get("pct")))
    return "\n".join(lines) + "\n\n---\n"


def render_commodities(data: list[dict]) -> str:
    lines = ["## 🏅  COMMODITIES\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
        else:
            lines.append(_row(row["label"], row["last"], row.get("pct")))
    return "\n".join(lines) + "\n\n---\n"


def render_currencies(data: list[dict]) -> str:
    lines = ["## 💱  CURRENCIES & FX\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
        else:
            lines.append(_row(row["label"], row["last"], row.get("pct")))
    return "\n".join(lines) + "\n\n---\n"


def render_vol(data: list[dict]) -> str:
    lines = ["## 🌡️  VOLATILITY\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
        else:
            note = ""
            last = row.get("last", 0)
            if row["label"] == "VIX":
                if last < 15:
                    note = "complacency zone"
                elif last > 25:
                    note = "⚠ elevated"
            lines.append(_row(row["label"], last, row.get("pct"), note))
    return "\n".join(lines) + "\n\n---\n"


def render_positions(data: list[dict]) -> str:
    if not data:
        return ""

    lines = ["## 💼  YOUR POSITIONS\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
            continue

        label   = row["label"]
        last    = row.get("last", 0.0)
        pct     = row.get("pct")
        warn    = row.get("warn", "")
        note    = row.get("note", "")
        stop    = row.get("stop")
        cost    = row.get("cost_basis")
        unreal  = row.get("unrealized_pnl_pct")

        emoji = _pct_emoji(pct)
        pct_s = _pct_arrow(pct)

        # Cost basis + unrealized P/L from DB
        db_s = ""
        if cost is not None:
            db_s = f"  cost {cost:,.2f}"
        if unreal is not None:
            sign  = "+" if unreal >= 0 else ""
            db_s += f"  unreal {sign}{unreal:.1f}%"

        stop_s = f"  stop {stop}" if stop else ""
        warn_s = f"  {warn}" if warn else ""
        note_s = f"  _{note}_" if note else ""

        lines.append(f"  {emoji} **{label}**  {last:,.2f}  ({pct_s}){db_s}{stop_s}{warn_s}{note_s}")

    return "\n".join(lines) + "\n\n---\n"


def render_key_levels(data: list[dict]) -> str:
    if not data:
        return ""

    lines = ["## 🔑  KEY LEVELS\n"]
    for row in data:
        if "error" in row:
            lines.append(_error_row(row["label"], row["error"]))
            continue

        label       = row["label"]
        last        = row.get("last", 0.0)
        pct         = row.get("pct")
        level_notes = row.get("level_notes", [])
        key_note    = row.get("key_note", "")
        emoji       = _pct_emoji(pct)
        pct_s       = _pct_arrow(pct)

        lines.append(f"  {emoji} **{label}**  {last:,.2f}  ({pct_s})")
        for ln in level_notes:
            lines.append(f"    → {ln}")
        if key_note:
            lines.append(f"    _{key_note}_")

    return "\n".join(lines) + "\n\n---\n"


def render_footer() -> str:
    return (
        "\n_To regenerate: `python -m morning_brief.brief` "
        "or wait for launchd at 6:45am._\n"
    )
