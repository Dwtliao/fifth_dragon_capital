"""
formatter.py — renders morning brief sections as markdown strings.

Each render_* function takes the dict/list produced by the matching
fetcher and returns a markdown string. brief.py concatenates them.
"""

from __future__ import annotations

import datetime
from typing import Optional


# ── helpers ───────────────────────────────────────────────────────────────────

def _display_ticker(ticker: str) -> str:
    """Strip yfinance suffixes for clean display: NQ=F → NQ, ^VIX → VIX, DX-Y.NYB → DXY."""
    t = ticker.lstrip("^")
    t = t.replace("=F", "").replace("-Y.NYB", "Y").replace("=X", "")
    return t


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


def _market_value(value, unit="price") -> str:
    if value is None:
        return "—"
    if unit == "yield_pct":
        return f"{value:.3f}%"
    return f"{value:,.3f}" if abs(value) < 10 else f"{value:,.2f}"


def _market_change(value, unit="price") -> str:
    if value is None:
        return "—"
    suffix = " bp" if unit in ("yield_pct", "bp") else " USD/bbl" if unit == "USD/barrel" else "%"
    text = f"{value:+.2f}{suffix}"
    if value > 0:
        color = 'green'
    elif value == 0 or (suffix == '%' and value >= -2.5):
        color = 'orange'  # Streamlit's theme-aware amber/yellow text.
    else:
        color = 'red'
    return f":{color}[{text}]"


def render_market_overview(overview: dict) -> str:
    """Render daily context with honest units, dates and missing data."""
    fetched = datetime.datetime.fromisoformat(overview["fetched_at"])
    lines = [
        "## Market context\n",
        f"_Yahoo Finance daily history, fetched {fetched.strftime('%Y-%m-%d %H:%M %Z')}._\n",
        "Changes use 1, 5 and 20 trading observations. The latest daily bar may still be forming; "
        "these are not guaranteed live pre-market quotes. Dates vary by exchange. "
        "⚠ marks a bar older than four calendar days.\n",
        "_Session colors: green > 0%; yellow −2.5% to 0%; red < −2.5%. "
        "Non-percentage changes (bp / USD per barrel) use green positive, red negative, "
        "yellow zero. Colors indicate direction, not whether the move is favorable._\n",
    ]
    titles = {
        "global": "Global equity indices", "us_futures": "US equity futures",
        "rates": "Rates & Treasury markets", "energy": "Energy futures",
        "commodities": "Metals, agriculture & uranium equity proxies",
        "currencies": "Currencies & dollar", "volatility": "Volatility",
        "risk": "Equity leadership, defensive sectors & credit proxies",
    }
    for group, title in titles.items():
        lines.extend([
            f"### {title}\n",
            "| Market | Latest | 1 session | 5 sessions | 20 sessions | vs 20 / 50 MA | 20-session close range | Bar date |",
            "|---|---:|---:|---:|---:|---|---|---|",
        ])
        for row in overview["groups"].get(group, []):
            if "error" in row:
                # Keep arbitrary fetch messages from breaking Markdown tables.
                error = str(row["error"]).replace("|", "/").replace("\n", " ")
                lines.append(f"| {row['label']} | Unavailable: {error} | — | — | — | — | — | — |")
                continue
            unit = row["unit"]
            trend = " / ".join(
                "—" if row.get(f"above_ma{days}") is None else
                ":green[above]" if row[f"above_ma{days}"] else ":red[at/below]"
                for days in (20, 50)
            )
            low, high = row.get("low_20d"), row.get("high_20d")
            close_range = f"{_market_value(low, unit)} – {_market_value(high, unit)}" if low is not None else "—"
            date = row["as_of"] + (" ⚠" if row.get("stale") else "")
            changes = " | ".join(_market_change(row.get(f"change_{days}d"), unit) for days in (1, 5, 20))
            lines.append(f"| {row['label']} | {_market_value(row['last'], unit)} | {changes} | {trend} | {close_range} | {date} |")
        lines.append("")
        if group == "rates":
            lines.append("_Yield levels are percentages; yield changes are basis points. "
                         "Treasury futures and bond ETFs show price changes, not yields. "
                         "The 13-week bill is a quoted discount yield; TIP prices are not inflation expectations._\n")
        elif group in ("energy", "commodities"):
            lines.append("_Futures use Yahoo's front-contract series; contract rolls can affect comparisons. "
                         "URA and URNM track uranium equities, not spot uranium._\n")

    lines.extend([
        "### Cross-market comparisons\n",
        "| Comparison | Latest | 1 session change | 5 session change | 20 session change | Shared bar date |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for row in overview.get("signals", []):
        if "error" in row:
            lines.append(f"| {row['label']} | Unavailable | — | — | — | — |")
            continue
        unit = row["unit"]
        value = _market_value(row["last"]) + (" bp" if unit == "bp" else " USD/bbl" if unit == "USD/barrel" else "")
        changes = " | ".join(_market_change(row.get(f"change_{days}d"), unit) for days in (1, 5, 20))
        date = row["as_of"] + (" ⚠" if row.get("stale") else "")
        lines.append(f"| {row['label']} | {value} | {changes} | {date} |")
    lines.append("\n_Ratios use dates shared by both instruments. Rising RSP/SPY means equal weight "
                 "outperformed the capitalization-weighted ETF; it is a participation proxy, not an advance/decline count. "
                 "HYG/LQD is relative ETF performance, not a measured credit spread. "
                 "ETF histories are adjusted for distributions and splits._\n\n---\n")
    return "\n".join(lines)


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

    sources = [r.get("price_source") for r in data if "error" not in r]
    etrade_count = sources.count("etrade")
    yf_count     = sources.count("yfinance")
    if etrade_count and not yf_count:
        source_note = "_(prices: E*TRADE real-time)_"
    elif etrade_count and yf_count:
        source_note = f"_(prices: E*TRADE real-time for {etrade_count}, yfinance delayed for {yf_count})_"
    else:
        source_note = "_(prices: yfinance ~15min delayed)_"

    lines = [f"## 💼  YOUR POSITIONS  {source_note}\n"]

    # Table header
    lines.append("| | Symbol | Price | Day % | Cost Basis | Unreal P/L % | Stop | Note |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")

    for row in data:
        if "error" in row:
            lines.append(f"| ⚪ | **{row['label']}** | — | — | — | — | — | _{row.get('error', 'fetch error')}_ |")
            continue

        label  = row["label"]
        last   = row.get("last")
        pct    = row.get("pct")
        cost   = row.get("cost_basis")
        unreal = row.get("unrealized_pnl_pct")
        stop   = row.get("stop")
        note   = row.get("note", "")
        warn   = row.get("warn", "")

        emoji  = _pct_emoji(pct)
        pct_s  = _pct_arrow(pct) if pct is not None else "—"
        last_s = f"{last:,.2f}" if last is not None else "—"
        cost_s = f"{cost:,.2f}" if cost is not None else "—"

        if unreal is not None:
            sign = "+" if unreal >= 0 else ""
            unreal_s = f"{sign}{unreal:.1f}%"
        else:
            unreal_s = "—"

        stop_s = f"{stop:,.2f}" if stop else "—"
        note_s = f"⚠ {warn}  {note}".strip(" ⚠") if warn else note

        lines.append(
            f"| {emoji} | **{label}** | {last_s} | {pct_s} | {cost_s} | {unreal_s} | {stop_s} | {note_s} |"
        )

    return "\n".join(lines) + "\n\n---\n"


def render_key_levels(data: list[dict]) -> str:
    if not data:
        return ""

    lines = ["## 🔑  KEY LEVELS\n"]
    lines.append("| | Ticker | Price | Day % | Support | Resistance | Alert Above | Note |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")

    for row in data:
        if "error" in row:
            lines.append(f"| ⚪ | **{row['label']}** | — | — | — | — | — | _{row.get('error', 'fetch error')}_ |")
            continue

        label   = _display_ticker(row["label"])
        last    = row.get("last")
        pct     = row.get("pct")

        emoji  = _pct_emoji(pct)
        pct_s  = _pct_arrow(pct) if pct is not None else "—"
        last_s = f"{last:,.2f}" if last is not None else "—"

        # Extract levels from level_notes back or from config directly
        level_notes = row.get("level_notes", [])
        sup_s = res_s = alrt_s = "—"
        for ln in level_notes:
            if "Resistance" in ln:
                res_s = ln.split("→")[0].replace("Resistance", "").strip()
            elif "Support" in ln and "⚠" not in ln:
                sup_s = ln.split("→")[0].replace("Support", "").strip()
            elif "alert level" in ln:
                alrt_s = ln.split("alert level")[-1].strip()

        note_s = row.get("key_note", "")

        lines.append(
            f"| {emoji} | **{label}** | {last_s} | {pct_s} | {sup_s} | {res_s} | {alrt_s} | {note_s} |"
        )

    return "\n".join(lines) + "\n\n---\n"


def render_footer() -> str:
    return (
        "\n_To regenerate: `python -m morning_brief.brief` "
        "or wait for launchd at 6:45am._\n"
    )
