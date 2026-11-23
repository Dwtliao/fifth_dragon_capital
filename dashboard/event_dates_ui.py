"""P10 Event Dates tab: reminders only. No DB, network, alerts or orders."""
import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

from morning_brief.event_dates import contract_table, upcoming_events

NOTES_DIR = Path(__file__).resolve().parent.parent / "market_events"


def list_event_notes(notes_dir=NOTES_DIR):
    """Newest-named first, so market_events_2026-12.md sorts above 2026-11."""
    return sorted(Path(notes_dir).glob("*.md"), reverse=True) if Path(notes_dir).is_dir() else []


def _fmt(d, today):
    return f"{d:%a %b %d, %Y}", (d - today).days


# RGB triplets used with transparency so the tints read on both dark and light themes.
CATEGORY_RGB = {"Options": "59, 130, 246", "Futures": "245, 158, 11", "Holiday": "148, 163, 184"}
STATUS_RGB = {"Chart this": "34, 197, 94", "Rolling off": "245, 158, 11"}


def _urgency(days):
    """Red within 3 days, amber within 7; dim anything already past."""
    if days < 0:
        return "opacity: 0.55"
    if days <= 3:
        return "color: rgb(239, 68, 68); font-weight: 700"
    if days <= 7:
        return "color: rgb(245, 158, 11); font-weight: 700"
    return ""


def style_events(df):
    def tint_row(row):
        rgb = CATEGORY_RGB.get(row["Category"])
        return [f"background-color: rgba({rgb}, 0.12)" if rgb else ""] * len(row)

    def category_cell(value):
        rgb = CATEGORY_RGB.get(value)
        return f"color: rgb({rgb}); font-weight: 600" if rgb else ""

    return (df.style.apply(tint_row, axis=1)
            .map(category_cell, subset=["Category"])
            .map(_urgency, subset=["In (days)"]))


def style_contracts(df):
    def status_cell(value):
        rgb = STATUS_RGB.get(value)
        return f"background-color: rgba({rgb}, 0.25); font-weight: 600" if rgb else ""

    return (df.style.map(status_cell, subset=["Status"])
            .map(_urgency, subset=["Roll in (days)", "Expires in (days)"]))


def render_event_dates(today=None):
    today = today or dt.date.today()
    st.caption("Reminders only; nothing here triggers alerts or orders. Dates are computed from "
               "published Cboe/CME rules and a US exchange holiday calendar. Spot-check against the "
               "Cboe options calendar and CME contract calendars, especially around holidays.")

    st.subheader("Upcoming dates")
    col_window, col_cat = st.columns([1, 3])
    days = col_window.selectbox("Look ahead", [30, 60, 90], index=1, format_func=lambda n: f"{n} days",
                                key="event_dates_window")
    events = upcoming_events(today, days)
    categories = sorted({e["category"] for e in events})
    chosen = col_cat.multiselect("Categories", categories, default=categories, key="event_dates_categories")
    rows = []
    for e in events:
        if e["category"] in chosen:
            label, away = _fmt(e["date"], today)
            rows.append({"Date": label, "In (days)": away, "Category": e["category"],
                         "Event": e["event"], "Detail": e["detail"]})
    if rows:
        st.dataframe(style_events(pd.DataFrame(rows)), width="stretch", hide_index=True)
        st.caption("🔵 Options  🟠 Futures  ⚪ Holiday. Days-away turns amber within 7 days and red within 3.")
    else:
        st.info("No events in this window for the selected categories.")

    st.subheader("Futures: which contract to chart")
    st.caption("Chart this = first contract whose roll marker has not passed. Roll marker is first notice "
               "day for metals, first position day for Treasuries and last trade for crude; volume "
               "typically moves to the next contract around that date. Rolling off = marker passed but "
               "not yet expired. Yahoo's continuous symbols (GC=F, CL=F, ...) pick their own roll date.")
    table = []
    for r in contract_table(today):
        roll, roll_away = _fmt(r["roll_date"], today)
        last, last_away = _fmt(r["last_trade"], today)
        table.append({"Product": r["product"], "Contract": r["contract"], "Status": r["status"],
                      "Roll marker": f"{roll} ({r['roll_label']})", "Roll in (days)": roll_away,
                      "Last trade": last, "Expires in (days)": last_away})
    st.dataframe(style_contracts(pd.DataFrame(table)), width="stretch", hide_index=True)

    st.subheader("Notes")
    notes = list_event_notes()
    if not notes:
        st.info(f"No notes yet. Add files such as market_events_{today:%Y-%m}.md to {NOTES_DIR.name}/.")
        return
    picked = st.selectbox("Month", notes, format_func=lambda p: p.stem, key="event_dates_note")
    st.markdown(picked.read_text(encoding="utf-8"))
