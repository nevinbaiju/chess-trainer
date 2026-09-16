"""Importing real games: the conversion, and the filters that guard it.

The API entries here are trimmed from a real response, so the field names and
the result vocabulary are chess.com's own rather than what the docs imply.
"""

import asyncio

import chess
import pytest

from app.chesscom import DRAWN, _termination, convert, wanted
from app.openings import OpeningBook


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return OpeningBook.load()


def entry(pgn_moves="1. e4 e5 2. Nf3 Nc6 3. Bb5 a6", *, hero="white",
          hero_result="win", other_result="resigned", result="1-0",
          time_class="rapid", rated=True, rules="chess", uuid="u-1"):
    us = {"username": "testplayer", "rating": 556, "result": hero_result}
    them = {"username": "Someone", "rating": 354, "result": other_result}
    white, black = (us, them) if hero == "white" else (them, us)
    return {
        "uuid": uuid, "end_time": 1789466987, "rated": rated, "rules": rules,
        "time_class": time_class, "white": white, "black": black,
        "pgn": f'[Event "Live Chess"]\n[Result "{result}"]\n[ECO "C60"]\n\n{pgn_moves} *\n',
    }


# --------------------------------------------------------------------------
# What gets imported
# --------------------------------------------------------------------------


def test_a_rated_rapid_standard_game_is_wanted():
    assert wanted(entry()) is True


@pytest.mark.parametrize(
    "change",
    [{"rated": False}, {"rules": "chess960"}, {"time_class": "blitz"},
     {"time_class": "bullet"}, {"time_class": "daily"}],
)
def test_everything_else_is_left_alone(change):
    assert wanted(entry(**change)) is False


def test_blitz_can_be_asked_for_explicitly():
    assert wanted(entry(time_class="blitz"), ("rapid", "blitz")) is True


# --------------------------------------------------------------------------
# Which side you were, and what happened
# --------------------------------------------------------------------------


def test_your_colour_comes_from_matching_the_username(book):
    assert convert(entry(hero="white"), "testplayer", book)["player_color"] == "white"
    assert convert(entry(hero="black"), "testplayer", book)["player_color"] == "black"


def test_the_username_is_matched_case_insensitively(book):
    """chess.com preserves the case you signed up with and the API does not."""
    assert convert(entry(), "TESTPLAYER", book) is not None
    assert convert(entry(), "TestPlayer", book) is not None


def test_a_game_neither_side_is_yours_is_skipped(book):
    assert convert(entry(), "someone-else", book) is None


def test_a_game_with_no_moves_is_skipped(book):
    """Abandoned before a move. There is nothing to review and the review
    pipeline would divide by zero working out accuracy."""
    assert convert(entry(pgn_moves=""), "testplayer", book) is None


@pytest.mark.parametrize(
    "hero_result,other_result,expected",
    [("win", "resigned", "resigned"), ("win", "checkmated", "checkmated"),
     ("checkmated", "win", "checkmated"), ("timeout", "win", "timeout"),
     ("abandoned", "win", "abandoned")],
)
def test_the_termination_is_the_losing_sides_own_word(hero_result, other_result, expected):
    assert _termination(hero_result, other_result) == expected


@pytest.mark.parametrize("word", sorted(DRAWN))
def test_a_draw_is_recorded_as_the_way_it_was_drawn(word):
    """Both sides carry the same word for a draw, so either will do."""
    assert _termination(word, word) == word


# --------------------------------------------------------------------------
# The row that comes out
# --------------------------------------------------------------------------


def test_the_moves_are_stored_as_uci_like_a_trainer_game(book):
    fields = convert(entry(), "testplayer", book)
    assert fields["moves"] == "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6"
    board = chess.Board()
    for uci in fields["moves"].split():
        board.push(chess.Move.from_uci(uci))      # raises if any is illegal


def test_ratings_on_the_day_are_kept(book):
    """There is no rating-history endpoint; the curve is rebuilt from these."""
    fields = convert(entry(), "testplayer", book)
    assert fields["player_rating"] == 556
    assert fields["opponent_rating"] == 354


def test_the_opening_is_named_by_our_own_book(book):
    """Not by chess.com's ECO tag: an imported game and a trainer game have to
    be named by the same rules or they will not group together in the stats."""
    fields = convert(entry(), "testplayer", book)
    assert fields["opening_name"] == "Ruy Lopez"
    assert fields["opening_eco"] == "C70"      # our book, not the tag's C60


def test_the_uuid_becomes_the_external_id(book):
    """games.external_id is UNIQUE, which is what makes re-importing safe."""
    assert convert(entry(uuid="abc"), "testplayer", book)["external_id"] == "abc"
    assert convert(entry(), "testplayer", book)["source"] == "chesscom"


def test_the_game_is_dated_when_it_was_played_not_when_it_was_imported(book):
    fields = convert(entry(), "testplayer", book)
    assert fields["created_at"].startswith("2026-09")
    assert fields["finished_at"] == fields["created_at"]


# --------------------------------------------------------------------------
# Politeness
# --------------------------------------------------------------------------


def test_the_client_always_identifies_itself():
    """Verified against the live API: an empty User-Agent is a 403, and
    chess.com ask for contact details in the header."""
    from app.chesscom import USER_AGENT, ChessCom

    assert USER_AGENT.strip(), "an empty User-Agent is a 403 — verified live"
    assert "CHESSCOM_USER_AGENT" in USER_AGENT, "the default must say how to set it"
    assert ChessCom().headers["User-Agent"] == USER_AGENT


def test_requests_are_not_fanned_out():
    """chess.com's guidance is that serial access is unlimited while parallel
    access may be throttled, so the importer must not gather()."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()
    body = source[source.index("async def _run_import"):source.index("# ---", source.index("async def _run_import"))]
    assert "gather" not in body
    assert "for index, url in enumerate(months" in body


# --------------------------------------------------------------------------
# Conditional requests
# --------------------------------------------------------------------------


def test_a_closed_month_is_revalidated_not_redownloaded():
    """Closed months never change. After the first fetch each one should cost a
    conditional request and a 304 — otherwise a weekly import re-downloads
    years of games to find the handful that are new."""
    import httpx

    from app.chesscom import ChessCom

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200, json={"games": [{"uuid": "a"}]}, headers={"etag": '"v1"'}
        )

    async def run():
        client = ChessCom()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = await client.month("https://example.test/2026/01")
        again = await client.month("https://example.test/2026/01", first.etag)
        await client.close()
        return first, again

    first, again = asyncio.run(run())
    assert first.unchanged is False
    assert first.etag == '"v1"'
    assert len(first.games) == 1
    assert again.unchanged is True
    assert again.games == []
    assert again.etag == '"v1"', "the tag must survive a 304 to be reused next time"
    assert seen == [None, '"v1"']


def test_an_unknown_player_is_reported_as_such_not_as_an_empty_history():
    """A typo is otherwise indistinguishable from "you have no games"."""
    import httpx

    from app.chesscom import ChessCom

    async def run():
        client = ChessCom()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        )
        try:
            with pytest.raises(LookupError):
                await client.archives("nobody")
            assert await client.profile("nobody") is None
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------


def _main_source() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()


def test_the_watcher_only_asks_for_the_current_month():
    """Closed months cannot gain games. Re-reading them every minute would be a
    request per month per minute to learn nothing, and the politeness of this
    whole thing rests on an idle minute costing exactly one conditional GET."""
    source = _main_source()
    body = source[source.index("async def _watch_once"):source.index("def _game_in_progress")]
    assert "months_to_check=1" in body


def test_an_idle_check_is_conditional():
    """It must reuse the stored ETag, or a quiet minute downloads the month."""
    source = _main_source()
    body = source[source.index("async def _run_import"):source.index("# ---", source.index("async def _run_import"))]
    assert "db().archive_etag(url, filters)" in body


def test_a_failed_check_does_not_end_the_watch():
    """chess.com being down for a minute must not silently stop the service
    until the next container restart."""
    source = _main_source()
    body = source[source.index("async def _watch_chesscom"):source.index("async def _watch_once")]
    assert "while True:" in body
    assert "except Exception" in body
    assert "raise" in body, "CancelledError must still propagate on shutdown"


def test_the_watcher_never_overlaps_a_running_import():
    source = _main_source()
    body = source[source.index("async def _watch_once"):source.index("def _game_in_progress")]
    assert 'state["import"]["running"]' in body


def test_reviews_are_held_back_while_a_game_is_live():
    """Reviews and your moves share one Stockfish behind one lock, so a review
    that starts mid-game puts a minute or two in front of your next mate hint.
    Importing is only HTTP and is never held back."""
    source = _main_source()
    body = source[source.index("async def _watch_once"):source.index("def _game_in_progress")]
    assert "if not _game_in_progress():" in body
    assert "review=0" in body, "the fetch itself must never wait on a game"

    start = source.index("def _game_in_progress")
    guard = source[start:source.index("\n\n\n", start)]
    assert guard, "the slice must not be empty or this test asserts nothing"
    assert "last_move_at" in guard, "idleness must key on the last move, not the game's start"
    assert "created_at" not in guard


def test_the_poll_interval_has_a_floor():
    """A settings value of 1 would be a request a second at somebody else's
    expense, whatever the field says."""
    source = _main_source()
    assert "max(30, interval)" in source
    assert "auto_import_seconds: int | None = Field(None, ge=30, le=3600)" in source


def test_the_month_list_is_not_re_fetched_every_minute():
    """It only changes when a new month starts. Asking every minute is a second
    request per minute to be told the same thing, and it would make the claim
    that an idle check costs one conditional request untrue."""
    source = _main_source()
    assert "async def _archive_list" in source
    body = source[source.index("async def _archive_list"):source.index("# ---", source.index("async def _archive_list"))]
    assert "ARCHIVE_LIST_TTL" in body
    assert 'entry["month"] ==' in body, "a month boundary must invalidate the cache"
    # A manual import is a user waiting on a full pass: it does not use the cache.
    watch = source[source.index("async def _watch_once"):source.index("def _game_in_progress")]
    assert "months_to_check=1" in watch
