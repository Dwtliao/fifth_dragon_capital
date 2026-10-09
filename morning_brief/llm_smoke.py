"""Test low/medium Claude calls with synthetic inputs and no database writes."""

import argparse
import json

from morning_brief.journal_sync import extract_from_journal
from morning_brief.llm import call_claude, print_usage


JOURNAL_SAMPLE = """Synthetic journal for API testing, not live trading instructions.
Portfolio: URNM — hold, stop at 54.95.
Watch gold: support 4300, resistance 4489.
If NQ is above 30200, consider a VIXY rebuy.
Historical observation only: gold hit 4400 last week; this is not an action level.
"""

BRIEF_SAMPLE = """Use only this SYNTHETIC test data, not current market facts.
Write a short market note with three headings: Observations, Portfolio relevance,
Conditions to watch. Distinguish facts from possible interpretations. Do not
invent news, forecasts, holdings or new trade levels. Do not recommend a trade.

All bars have the same synthetic date. 10Y quoted Treasury yield is 4.25%,
up 15 basis points over one session. Gold is down 1.2% over one session and
below its 20-session average. Nasdaq futures are down 0.8%. Dollar index is
up 0.6%. RSP/SPY is down 0.5% over five sessions (relative ETF performance,
not an advance/decline measure). The sole synthetic holding is URNM, with
an existing stop of 54.95; no current URNM price is supplied. Explain what
cannot be concluded from these inputs. Keep the answer under 250 words.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override configured Claude model")
    args = parser.parse_args()

    print("Synthetic journal extraction (low effort):")
    extraction = extract_from_journal(JOURNAL_SAMPLE, model=args.model, effort="low")
    print(json.dumps(extraction, indent=2))
    assert any(p.get("ticker") == "URNM" and p.get("stop") == 54.95 for p in extraction["positions"]), \
        "Expected URNM stop was not extracted"
    assert any(a.get("ticker") == "NQ=F" and a.get("threshold") == 30200 for a in extraction["price_alerts"]), \
        "Expected NQ trigger was not extracted"
    assert all(w.get("support") != 4400 and w.get("resistance") != 4400 for w in extraction["watch_levels"]), \
        "Historical observation became an action level"

    print("\nSynthetic market interpretation (medium effort):")
    result = call_claude(BRIEF_SAMPLE, "brief", model=args.model)
    print_usage(result)
    print(result["text"])
    print("\nNo database, brief, key-level or alert changes were made.")


if __name__ == "__main__":
    main()
