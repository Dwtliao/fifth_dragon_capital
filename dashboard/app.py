import streamlit as st

st.set_page_config(
    page_title="Fifth Dragon Capital",
    page_icon="🐉",
    layout="wide",
)

st.title("Fifth Dragon Capital")
st.caption("Personal trading data pipeline & analytics")

st.markdown("""
**Navigation** — use the sidebar to switch pages.

| Page | Description |
|---|---|
| Pipeline Status | Job run history, table health, sync controls |
| *(more coming)* | Portfolio, Performance, Trading History, Risk |
""")
