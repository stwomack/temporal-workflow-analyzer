import re
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
STYLE = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


def test_single_page_with_inline_css_and_js():
    assert PAGE.count("<script") == 1
    assert "<script src" not in PAGE
    assert "<link" not in PAGE
    assert "@import" not in STYLE


def test_no_external_network_resources():
    assert not re.search(r"""(src|href|action)\s*=\s*["']?(https?:)?//""", PAGE)
    assert "url(" not in STYLE
    assert not re.search(r"""fetch\(\s*["'](https?:)?//""", SCRIPT)


@pytest.mark.parametrize("element_id", ["repo-url", "scan-button", "cancel-button", "events", "temporal-link",
                                        "stage", "error", "languages", "summary", "first-list", "later-list"])
def test_required_elements_exist(element_id):
    assert f'id="{element_id}"' in PAGE


def test_cancel_button_starts_hidden():
    assert re.search(r'<button id="cancel-button"[^>]*\bhidden\b', PAGE)


def test_group_headings_and_poll_interval():
    assert "<h2>Fix these first</h2>" in PAGE
    assert "<h2>Also worth fixing</h2>" in PAGE
    assert "POLL_INTERVAL_MS = 1500" in SCRIPT
    assert '"/api/scans"' in SCRIPT
    assert "/cancel" in SCRIPT


def test_untrusted_text_is_never_injected_as_html():
    assert "innerHTML" not in SCRIPT
    assert "outerHTML" not in SCRIPT
    assert "insertAdjacentHTML" not in SCRIPT
    assert "document.write" not in SCRIPT
    assert 'parsed.protocol === "http:" || parsed.protocol === "https:"' in SCRIPT


def test_light_and_dark_mode():
    assert "@media (prefers-color-scheme: dark)" in STYLE
    assert "color-scheme: light dark" in STYLE


def test_no_decorative_animation():
    assert "@keyframes" not in STYLE
    assert "animation" not in STYLE
    assert "transition" not in STYLE


def test_no_comments_emojis_or_em_dashes():
    assert "<!--" not in PAGE
    assert "/*" not in PAGE
    assert not re.search(r"^\s*//", SCRIPT, re.MULTILINE)
    assert chr(0x2014) not in PAGE
    assert not re.search(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]", PAGE)
    assert PAGE.isascii()
