"""Render the Morning Brief markdown with colored section titles.

Only the title lines become HTML. All other brief text (LLM output, market data, journal
text) is still rendered as plain markdown, never as raw HTML.
"""
import html
import re

import streamlit as st

# casefolded title -> hex color; levels come from the file ("##" -> h2, "###" -> h3)
TITLE_COLORS = {
    "what needs my attention?": "#f87171",   # light red
    "what matters this morning": "#c084fc",  # violet
    "key developments": "#22d3ee",           # cyan
    "conditions to watch": "#fbbf24",        # amber
    "market context": "#4ade80",             # green
}

_HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*$")


def render_brief(content, markdown=st.markdown):
    buffer = []

    def flush():
        if buffer:
            markdown("\n".join(buffer))
            buffer.clear()

    for line in content.split("\n"):
        match = _HEADING.match(line)
        color = TITLE_COLORS.get(match.group(2).casefold()) if match else None
        if color:
            flush()
            tag = f"h{len(match.group(1))}"
            markdown(f'<{tag} style="color: {color};">{html.escape(match.group(2))}</{tag}>',
                     unsafe_allow_html=True)
        else:
            buffer.append(line)
    flush()
