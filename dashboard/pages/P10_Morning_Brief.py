import subprocess
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

st.set_page_config(page_title="Morning Brief — Fifth Dragon Capital", layout="wide")
st.title("Morning Brief")

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_DIARY = Path.home() / "Library/CloudStorage/Dropbox/Etrade/trading_diary"

from dotenv import load_dotenv
import os
load_dotenv(PROJECT_ROOT / ".env")
diary = Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))
brief_path = diary / "morning_brief.md"


def _run_brief():
    result = subprocess.run(
        [sys.executable, "-m", "morning_brief.brief"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    return result.stdout + result.stderr


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Generate Brief**")
if st.sidebar.button("▶ Run Morning Brief", type="primary", use_container_width=True):
    with st.spinner("Fetching market data…"):
        output = _run_brief()
    st.sidebar.code(output.strip(), language=None)
    st.rerun()

st.sidebar.divider()
if brief_path.exists():
    mtime = brief_path.stat().st_mtime
    import datetime
    st.sidebar.caption(f"Last generated:\n{datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}")

# ── main content ───────────────────────────────────────────────────────────────

if not brief_path.exists():
    st.info(
        f"No brief found at `{brief_path}`. "
        "Click **▶ Run Morning Brief** in the sidebar to generate one."
    )
else:
    content = brief_path.read_text(encoding="utf-8")
    st.markdown(content)
