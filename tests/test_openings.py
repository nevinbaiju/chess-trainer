"""Opening book: classification, search, and the three play modes."""

import random

import chess
import pytest

from app.openings import (
    BookMode,
    OpeningBook,
    book_move_for,
    deviation_ply,
)


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return OpeningBook.load()


def moves_of(sans: str) -> list[chess.Move]:
    board = chess.Board()
    out = []
    for san in sans.split():
        move = board.parse_san(san)
        out.append(move)
        board.push(move)
    return out


def board_after(sans: str) -> chess.Board:
    board = chess.Board()
    for san in sans.split():
        board.push(board.parse_san(san))
    return board


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_catalogue_loads(book):
    assert len(book.openings) > 3000
    assert "Sicilian Defense" in book.families()
    assert "Italian Game" in book.families()


def test_family_and_variation_split(book):
    opening = book.by_name["Sicilian Defense: Najdorf Variation"]
    assert opening.family == "Sicilian Defense"
    assert opening.variation == "Najdorf Variation"


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sans,expected",
    [
        ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Italian Game: Giuoco Piano"),
        ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6", "Sicilian Defense: Najdorf Variation"),
        ("d4 d5 c4 e6", "Queen's Gambit Declined"),
        ("e4 e6 d4 d5", "French Defense"),
    ],
)
def test_classifies_common_openings(book, sans, expected):
    opening = book.classify(moves_of(sans))
    assert opening is not None
    assert opening.name == expected


def test_reverse_walk_beats_forward_matching(book):
    """Walking backwards finds the most specific name, not the first one.

    Forward-first-hit would label a Najdorf "King's Pawn Game" because that is
    the first named position the game passes through.
    """
    game = moves_of("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6")
    assert book.classify(game).name == "Sicilian Defense: Najdorf Variation"

    board = chess.Board()
    first_hit = None
    for move in game:
        board.push(move)
        hit = book.by_epd.get(board.epd())
        if hit and first_hit is None:
            first_hit = hit
    assert first_hit is not None
    assert first_hit.name != "Sicilian Defense: Najdorf Variation"


def test_unplayed_position_falls_back_to_an_ancestor(book):
    """An unnamed position is described by the last named one before it."""
    opening = book.classify(moves_of("e4 e5 Bc4 Nc6 Qh5"))
    assert opening is not None
    assert opening.name == "Bishop's Opening"


def test_transposition_to_an_unnamed_position_is_path_dependent(book):
    """A known and deliberately un-fixed limitation of lichess's own method.

    1.d4 d5 2.c4 e6 3.Nf3 Nf6 and 1.Nf3 d5 2.d4 Nf6 3.c4 e6 reach an identical
    position, but the catalogue does not name it — many lines pass *through* it,
    none *ends* there. Walking back therefore lands on whichever named position
    each move order actually visited.

    Indexing intermediate positions would make this path-independent, but it
    would also let the Queen's Gambit *Accepted* (D24, which also runs through
    this position) name a Queen's Gambit *Declined*. A vague name beats a
    confidently wrong one, so we keep lichess's behaviour and pin it here so the
    tradeoff stays a decision rather than a surprise.
    """
    via_d4 = board_after("d4 d5 c4 e6 Nf3 Nf6")
    via_nf3 = board_after("Nf3 d5 d4 Nf6 c4 e6")
    assert via_d4.epd() == via_nf3.epd()
    assert via_d4.epd() not in book.by_epd

    named_d4 = book.classify(moves_of("d4 d5 c4 e6 Nf3 Nf6"))
    named_nf3 = book.classify(moves_of("Nf3 d5 d4 Nf6 c4 e6"))
    assert named_d4.name == "Queen's Gambit Declined"
    assert named_nf3.name != named_d4.name  # the documented limitation


def test_classifying_an_empty_game_returns_nothing(book):
    assert book.classify([]) is None


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def test_search_is_case_insensitive_and_shortest_first(book):
    hits = book.search("italian game")
    assert hits
    assert hits[0].name == "Italian Game"
    assert all("italian game" in o.name.lower() for o in hits)


def test_search_on_empty_query_returns_nothing(book):
    assert book.search("   ") == []


# --------------------------------------------------------------------------
# The three modes
# --------------------------------------------------------------------------


def test_realistic_mode_never_uses_the_book(book):
    assert book_move_for(chess.Board(), book, BookMode.REALISTIC) is None


def test_strict_mode_plays_book_moves(book):
    board = chess.Board()
    rng = random.Random(1)
    for _ in range(6):
        move = book_move_for(board, book, BookMode.STRICT, rng=rng)
        assert move is not None and move in board.legal_moves
        board.push(move)
    assert book.classify(list(board.move_stack)) is not None


def test_drill_mode_follows_the_chosen_line_exactly(book):
    line = book.by_name["Italian Game: Giuoco Piano"]
    board = chess.Board()
    played = []
    while True:
        move = book_move_for(board, book, BookMode.DRILL, line=line, drill_plies=12)
        if move is None:
            break
        played.append(board.san(move))
        board.push(move)
    assert played == line.san_line()


def test_drill_mode_stops_at_the_ply_budget(book):
    line = book.by_name["Italian Game: Giuoco Piano"]
    board = chess.Board()
    count = 0
    while True:
        move = book_move_for(board, book, BookMode.DRILL, line=line, drill_plies=4)
        if move is None:
            break
        board.push(move)
        count += 1
    assert count == 4


def test_drill_mode_hands_over_once_the_player_leaves_the_line(book):
    """Leaving the line is the player's choice; the bot should not drag them back."""
    line = book.by_name["Italian Game: Giuoco Piano"]
    board = board_after("e4 e5 Nf3 Nc6 Bc4 Nf6")  # ...Nf6 instead of ...Bc5
    assert book_move_for(board, book, BookMode.DRILL, line=line, drill_plies=12) is None


def test_deviation_ply_reports_where_the_line_was_left(book):
    line = book.by_name["Italian Game: Giuoco Piano"]
    assert deviation_ply(board_after("e4 e5 Nf3 Nc6 Bc4 Nf6"), line) == 5
    assert deviation_ply(board_after("e4 e5 Nf3 Nc6"), line) is None


def test_book_moves_are_always_legal(book):
    rng = random.Random(99)
    for _ in range(40):
        board = chess.Board()
        for _ in range(8):
            move = book.pick(board, rng=rng)
            if move is None:
                break
            assert move in board.legal_moves
            board.push(move)


def test_weighted_pick_produces_variety(book):
    """A drill session that replays the identical game every time is useless."""
    rng = random.Random(5)
    firsts = {chess.Board().san(book.pick(chess.Board(), rng=rng)) for _ in range(40)}
    assert len(firsts) > 1
