"""P11 one-pass alert triage: review active alerts by distance, then keep, delete or move levels.

Pure helpers (triage_rows, plan_changes) plus a Streamlit renderer. Changes reuse the existing
services, so the same fingerprint checks, audit history and stop ownership rules apply:
- level edits: save_inline_edits (discretionary alerts only)
- deletes: apply_bulk 'purge' (discretionary alerts only)
- stops on tickers no longer held: removed from saved stops via save_structural_key_levels,
  the same CAS-protected path as P10 Save All, which also retires the stop alert.
Stops on tickers still held stay managed in P10.
"""
import copy
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from alerts.lifecycle import is_eligible, is_position_stop, valid_price
from alerts.management import apply_bulk, fingerprint, preview_action, save_inline_edits
from dashboard.alerts_workspace import distance_percent, quote_snapshot

BANDS = ("Near (<5%)", "Medium (5–25%)", "Far (>25%)", "No price")
DECISIONS = ("Keep", "Delete")


def _band(distance):
    if distance is None:
        return BANDS[3]
    return BANDS[0] if distance < 5 else BANDS[1] if distance <= 25 else BANDS[2]


def triage_rows(alerts, quotes, held, now=None):
    """Active alerts with price, distance, band and flags; nearest first within each band."""
    active = [a for a in alerts if is_eligible(a, now)]
    per_ticker = Counter(a["ticker"] for a in active)
    rows = []
    for alert in active:
        quote = quotes.get(alert["ticker"]) or {}
        distance = distance_percent(alert, quote)
        stop = is_position_stop(alert)
        flags = []
        if stop:
            flags.append("Stop, held — edit in P10" if alert["ticker"].upper() in held
                         else "Stop, not held — Delete removes the stop")
        if per_ticker[alert["ticker"]] > 1:
            flags.append(f"{per_ticker[alert['ticker']]} alerts on ticker")
        if distance is not None and distance >= 100:
            flags.append("Very far — check split/units")
        rows.append(dict(id=alert["id"], band=_band(distance), ticker=alert["ticker"],
                         condition=alert["condition"], level=float(alert["threshold"]),
                         price=quote.get("price"), distance=distance, label=alert.get("label") or "",
                         flags="; ".join(flags), is_stop=stop))
    rows.sort(key=lambda r: (BANDS.index(r["band"]), r["distance"] if r["distance"] is not None else 0, r["id"]))
    return rows


def plan_changes(alerts_by_id, records, held):
    """Editor records -> (deletes, edits, stop_tickers). Raises ValueError on invalid choices.

    A row is an edit when its New level differs from the current level and Decision is Keep.
    """
    deletes, edits, stop_tickers, problems = [], [], [], []
    for record in records:
        alert = alerts_by_id.get(record["ID"])
        if alert is None:
            problems.append(f"#{record['ID']} is no longer loaded; reload triage")
            continue
        name = f"#{alert['id']} {alert['ticker']}"
        level = record.get("New level")
        if not valid_price(level):
            problems.append(f"{name}: New level must be a number greater than zero")
            continue
        changed = float(level) != float(alert["threshold"])
        stop, owned = is_position_stop(alert), alert["ticker"].upper() in held
        if record["Decision"] == "Delete":
            if stop and owned:
                problems.append(f"{name} is a stop on a stock you hold; change it in P10")
            elif stop:
                stop_tickers.append(alert["ticker"])
            else:
                deletes.append(alert)
        elif changed:
            if stop:
                problems.append(f"{name} is a position stop; change its level in P10")
            else:
                edits.append(dict(id=alert["id"], ticker=alert["ticker"], fingerprint=fingerprint(alert),
                                  label=alert.get("label") or "", condition=alert["condition"],
                                  threshold=float(level), remove_expiry=False))
    if problems:
        raise ValueError("; ".join(problems))
    return deletes, edits, stop_tickers


def remove_unheld_stops(tickers):
    """Remove saved stops (and their notes) for tickers no longer held, as P10 Save All would."""
    from morning_brief.alert_compiler import save_structural_key_levels
    from morning_brief.fetchers import load_key_levels_from_db
    baseline = load_key_levels_from_db()
    updated = copy.deepcopy(baseline)
    positions = updated.get("positions") or {}
    wanted = {t.upper() for t in tickers}
    for ticker in [t for t in positions if t.upper() in wanted]:
        del positions[ticker]
    stats = save_structural_key_levels(updated, baseline)
    if stats.get("error"):
        raise ValueError(f"stops removed from P10 but alert refresh failed: {stats['error']}")
    return sorted(wanted)


def apply_plan(deletes, edits, stop_tickers):
    """Edits, then deletes, then stop removals; each step is all-or-nothing. Returns a summary."""
    done = []
    steps = [
        (edits, lambda: f"edited levels for IDs {save_inline_edits(edits)}"),
        (deletes, lambda: f"deleted IDs {apply_bulk(preview_action(deletes, 'purge'), 'purge')}"),
        (stop_tickers, lambda: f"removed stops for {', '.join(remove_unheld_stops(stop_tickers))}"),
    ]
    for items, run in steps:
        if not items:
            continue
        try:
            done.append(run())
        except ValueError as exc:
            saved = f" Already saved: {'; '.join(done)}." if done else ""
            raise ValueError(f"Stopped: {exc}.{saved}") from exc
    return "Triage applied: " + "; ".join(done) + "." if done else "Nothing to change; every alert was kept."


def _held_symbols():
    from morning_brief.fetchers import fetch_holdings
    return {r["symbol"].upper() for r in fetch_holdings()}


def render_triage(alerts, notify):
    """`notify(message)` is P11's existing success-message-and-rerun helper."""
    st.caption("One pass over every active alert, nearest to its level first. Set Decision to Delete, "
               "or change New level to move an alert, then review the summary and apply once. Stops on "
               "stocks you no longer hold can be deleted here; stops on stocks you hold stay in P10. "
               "Deletes keep audit snapshots.")
    if st.button("Load triage (fetch current prices)", key="triage_load"):
        active = [a for a in alerts if is_eligible(a)]
        try:
            held = _held_symbols()
        except Exception as exc:
            st.error(f"Holdings unavailable, so stops can't be classified: {exc}")
            return
        quote_snapshot.clear()
        with st.spinner("Fetching prices…"):
            quotes = quote_snapshot(tuple(sorted({a["ticker"] for a in active})))
        st.session_state["triage"] = dict(alerts={a["id"]: a for a in active}, held=held,
                                          rows=triage_rows(active, quotes, held),
                                          at=datetime.now(timezone.utc))
        st.session_state["triage_epoch"] = st.session_state.get("triage_epoch", 0) + 1
    snap = st.session_state.get("triage")
    if not snap:
        return
    if not snap["rows"]:
        st.info("No active alerts to triage.")
        return
    age_min = (datetime.now(timezone.utc) - snap["at"]).total_seconds() / 60
    counts = Counter(r["band"] for r in snap["rows"])
    st.caption(f"Prices fetched {snap['at']:%H:%M UTC} ({age_min:.0f} min ago). "
               + " · ".join(f"{band}: {counts.get(band, 0)}" for band in BANDS))
    frame = pd.DataFrame([{
        "Band": r["band"], "Ticker": r["ticker"], "Cond": r["condition"], "Level": r["level"],
        "Price": r["price"], "Distance %": r["distance"], "Label": r["label"], "Flags": r["flags"],
        "Decision": "Keep", "New level": r["level"], "ID": r["id"],
    } for r in snap["rows"]])
    edited = st.data_editor(
        frame, key=f"triage_editor_{st.session_state.get('triage_epoch', 0)}", hide_index=True, width="stretch",
        disabled=[c for c in frame.columns if c not in ("Decision", "New level")],
        column_config={
            "Decision": st.column_config.SelectboxColumn(options=list(DECISIONS), required=True),
            "New level": st.column_config.NumberColumn(min_value=0.0001, format="%.4f",
                                                       help="Change this number to move the alert level"),
            "Level": st.column_config.NumberColumn(format="%.4f"),
            "Price": st.column_config.NumberColumn(format="%.4f"),
            "Distance %": st.column_config.NumberColumn(format="%.1f"),
        })
    try:
        deletes, edits, stop_tickers = plan_changes(snap["alerts"], edited.to_dict("records"), snap["held"])
    except ValueError as exc:
        st.error(str(exc))
        return
    if not (deletes or edits or stop_tickers):
        st.caption("No changes yet. Set Decision to Delete, or type a different New level.")
        return
    st.markdown(f"**Pending:** delete {len(deletes)} · move {len(edits)} · remove stops {len(stop_tickers)}")
    if deletes:
        st.caption("Delete: " + ", ".join(f"#{a['id']} {a['ticker']} {a['condition']} {float(a['threshold']):g}"
                                          for a in deletes))
    if edits:
        st.caption("Move: " + ", ".join(f"#{e['id']} {e['ticker']} → {e['threshold']:g}" for e in edits))
    if stop_tickers:
        st.caption("Remove saved stop and note (not held): " + ", ".join(sorted(stop_tickers)))
    total = len(deletes) + len(edits) + len(stop_tickers)
    confirm_key = "triage_confirm_" + str(hash((tuple(a["id"] for a in deletes),
                                                 tuple((e["id"], e["threshold"]) for e in edits),
                                                 tuple(sorted(stop_tickers)))))
    confirmed = st.checkbox(f"Confirm these {total} changes", key=confirm_key)
    if st.button("Apply triage", key="triage_apply", type="primary", disabled=not confirmed):
        try:
            message = apply_plan(deletes, edits, stop_tickers)
        except ValueError as exc:
            st.error(f"{exc} Reload triage and try again.")
        else:
            st.session_state.pop("triage", None)
            notify(message)
