"""Cheap static guards on the frontend.

These cannot replace running the page, but they catch the specific mistakes
that already bit once and are invisible in unit tests.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text()


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text()


def test_hidden_attribute_cannot_be_overridden_by_a_class(css):
    """The bug this guards against cost a working board.

    `.board-overlay { display: flex }` outbids the browser default
    `[hidden] { display: none }`, so the result banner sat invisibly over the
    board and swallowed every click — no piece could be picked up. Two other
    elements had the same problem. The global rule settles it; if it is ever
    removed, this fails instead of the board silently going dead.
    """
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "style.css must force [hidden] to display:none, or any class that sets "
        "display will leave hidden elements on screen"
    )


def test_every_element_that_starts_hidden_has_an_id(html):
    """Hidden elements are toggled from JS, so they need to be addressable."""
    for match in re.finditer(r"<(\w+)([^>]*\shidden[^>]*)>", html):
        attrs = match.group(2)
        assert "id=" in attrs, f"hidden <{match.group(1)}> has no id: {attrs.strip()[:60]}"


def test_vendored_board_library_is_present(html):
    """The app deliberately has no build step; the vendored files must exist."""
    for path in re.findall(r'(?:href|src)="(/static/[^"]+)"', html):
        assert (STATIC / path[len("/static/"):]).exists(), f"missing asset: {path}"


def test_sound_module_covers_every_move_type():
    sound = (STATIC / "sound.js").read_text()
    for voice in ("move", "capture", "castle", "check", "promote", "win", "lose", "draw"):
        assert re.search(rf"^  {voice}\(", sound, re.M), f"no {voice} sound defined"


def test_no_leftover_debug_logging():
    app = (STATIC / "app.js").read_text()
    assert "console.log(" not in app, "stray console.log left in app.js"


# --------------------------------------------------------------------------
# Cache control
# --------------------------------------------------------------------------


def test_static_files_are_served_with_no_cache():
    """The bug this prevents is invisible from the server.

    Starlette sends an ETag and Last-Modified but no Cache-Control, so a browser
    is free to apply heuristic freshness and reuse app.js without asking. The
    API then answers with new fields while the page runs the old JavaScript
    against them — which is exactly how a working feature shipped and showed
    nothing in the browser.
    """
    source = (STATIC.parent / "app" / "main.py").read_text()
    assert '"Cache-Control": "no-cache' in source
    assert "class RevalidatedStatic(StaticFiles)" in source
    assert "RevalidatedStatic(directory=STATIC_DIR)" in source


def test_the_index_versions_the_scripts_it_loads():
    """no-cache governs future responses only; a browser that already cached
    app.js keys on the URL, so the URL has to change for a deploy to arrive."""
    source = (STATIC.parent / "app" / "main.py").read_text()
    assert "def asset_version()" in source
    assert 'f"/static/{name}?v={asset_version()}"' in source

    html = (STATIC / "index.html").read_text()
    for name in ("app.js", "style.css"):
        assert f"/static/{name}" in html, f"index.html no longer loads {name}"


def test_the_stats_filter_is_not_inside_the_panel_it_filters():
    """Selecting a source with nothing in it hides the panel. If the filter
    lives inside that panel it disappears too, stranding you on an empty screen
    with no way back — the dead end the Review tab already had once."""
    html = (STATIC / "index.html").read_text()
    card = html[html.index('id="progress-card"'):html.index('id="review-list"')]
    assert 'id="stats-scope"' not in card, "the filter moved back inside the panel"
    assert 'id="stats-scope"' in html
    assert 'id="stats-empty"' in html


def test_every_id_the_chesscom_code_touches_exists_in_the_page():
    """A typo in an element id is invisible until someone opens the tab: $()
    returns null and the listener throws on load, taking the rest with it."""
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    section = js[js.index("chess.com --"):]
    for name in set(re.findall(r'\$\("([a-z0-9-]+)"\)', section)):
        assert f'id="{name}"' in html, f"app.js uses #{name}, which the page lacks"
