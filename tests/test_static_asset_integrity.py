"""Static assets must be structurally intact.

Born from a real break: a merge resolved a style.css conflict by keeping both
sides, but the conflict boundary fell INSIDE a rule — our side ended just
before its closing brace. The result was one unterminated block that swallowed
the rest of the file. The browser parsed 121 rules out of ~6500 and the app
rendered as unstyled markup.

Nothing caught it. The file existed, served HTTP 200, was 1.34MB, contained
every selector anyone grepped for, and the whole Python test suite passed. Only
a CSS parser would have noticed, so the check has to be structural rather than
"does the text appear somewhere".
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


def _strip_css_noise(text: str) -> str:
    """Remove comments and quoted strings — both can hold stray braces."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    return text


def _css_files():
    return sorted(p for p in STATIC.rglob("*.css") if ".min." not in p.name)


@pytest.mark.parametrize("path", _css_files(), ids=lambda p: p.name)
def test_css_braces_balance(path):
    """An unbalanced brace silently kills every rule after it."""
    body = _strip_css_noise(path.read_text(encoding="utf-8", errors="replace"))
    opened, closed = body.count("{"), body.count("}")
    assert opened == closed, (
        f"{path.name} has {opened} '{{' vs {closed} '}}' — an unterminated "
        f"rule swallows the remainder of the stylesheet, so most of the app "
        f"renders unstyled while the file still serves 200 OK"
    )


# A line-shape heuristic ("declaration followed by a selector") was tried here
# and removed: it false-positives on multi-line declaration values, e.g. a
# box-shadow list inside a @keyframes step, whose continuation lines end in ','
# and look exactly like a selector list. Brace balance already catches the real
# defect deterministically -- verified by reintroducing the bug and watching
# test_css_braces_balance fail -- and a check that flags correct CSS is worse
# than no check, because it trains people to ignore the failure.


def test_style_css_has_a_plausible_rule_count():
    """A structurally broken stylesheet still has the right byte count, so
    assert the file actually contains the rules the app needs."""
    css = STATIC / "style.css"
    body = _strip_css_noise(css.read_text(encoding="utf-8", errors="replace"))
    assert body.count("{") > 5000, (
        f"style.css declares only {body.count('{')} rules — the fork's UI "
        f"(Round Table, theme, terminal chat) needs far more; a truncated or "
        f"mis-merged file would land here"
    )
