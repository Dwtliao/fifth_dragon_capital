import unittest

from dashboard.brief_titles import TITLE_COLORS, render_brief

BRIEF = """# Morning Brief — Sun Nov 22 2026

## What needs my attention?

### Protect holdings (4)
- item <b>raw html in body</b>

## What matters this morning

### Key developments
text

### Conditions to watch
more

## Market context

### Global equity indices
table
"""


def _render(content):
    calls = []
    render_brief(content, markdown=lambda body, **kw: calls.append((body, kw)))
    return calls


class TestRenderBrief(unittest.TestCase):
    def test_five_titles_get_five_distinct_colors(self):
        self.assertEqual(len(set(TITLE_COLORS.values())), 5)
        titled = [c for c in _render(BRIEF) if c[1].get("unsafe_allow_html")]
        self.assertEqual(len(titled), 5)
        self.assertEqual(len({body.split('color: ')[1].split(';')[0] for body, _ in titled}), 5)

    def test_heading_levels_are_kept(self):
        bodies = [b for b, kw in _render(BRIEF) if kw.get("unsafe_allow_html")]
        self.assertTrue(bodies[0].startswith("<h2") and "What needs my attention?" in bodies[0])
        self.assertTrue(bodies[2].startswith("<h3") and "Key developments" in bodies[2])

    def test_body_text_is_never_rendered_as_html(self):
        for body, kw in _render(BRIEF):
            if not kw.get("unsafe_allow_html"):
                self.assertNotIn("unsafe_allow_html", kw)

    def test_other_headings_and_content_are_preserved_in_order(self):
        plain = "\n".join(b for b, kw in _render(BRIEF) if not kw.get("unsafe_allow_html"))
        for text in ("# Morning Brief", "### Protect holdings (4)", "raw html in body",
                     "### Global equity indices", "table"):
            self.assertIn(text, plain)
        self.assertNotIn("What needs my attention?", plain)

    def test_title_matching_ignores_case(self):
        calls = _render("## Market Context\n\nbody\n")
        self.assertTrue(calls[0][1].get("unsafe_allow_html"))

    def test_brief_without_known_titles_renders_unchanged(self):
        calls = _render("## Something else\n\nbody")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "## Something else\n\nbody")


if __name__ == "__main__":
    unittest.main()
