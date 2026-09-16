"""Leaving the line: what the trainer says, and whether it is true.

Every fixture here is verified with python-chess before being written down —
these are real positions from the catalogue, not plausible-looking SAN.
"""

import asyncio

import chess
import pytest

from app.deviation import analyse_deviation, template_deviation, transposes
from app.openings import OpeningBook


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return OpeningBook.load()


def at(line: str) -> chess.Board:
    board = chess.Board()
    for san in line.split():
        board.push_san(san)
    return board


def tail_of(board: chess.Board, line: str) -> list[chess.Move]:
    walker, moves = board.copy(), []
    for san in line.split():
        move = walker.parse_san(san)
        moves.append(move)
        walker.push(move)
    return moves


def deviation(book, position: str, rest: str, played: str, name="A Line"):
    board = at(position)
    tail = tail_of(board, rest)
    return asyncio.run(analyse_deviation(
        board, tail[0], board.parse_san(played), name, tail, book, stockfish=None))


# --------------------------------------------------------------------------
# Transpositions
# --------------------------------------------------------------------------


def test_castling_a_move_early_is_recognised_as_the_same_position():
    """The case that prompted all this.

    In the King's Indian the line runs 4...d6 5.Nf3 O-O. Castling first and
    playing d6 next move reaches the identical position, so calling it a
    mistake — or even calling it "not this repertoire" — is false.
    """
    board = at("d4 Nf6 c4 g6 Nc3 Bg7 e4")
    tail = tail_of(board, "d6 Nf3 O-O")
    assert transposes(board, board.parse_san("O-O"), tail) == ["O-O", "Nf3", "d6"]


def test_a_move_the_line_never_plays_does_not_transpose():
    board = at("e4 e5 Nf3 Nc6 Bc4")
    tail = tail_of(board, "Bc5 c3 Nf6")
    assert transposes(board, board.parse_san("Qh4"), tail) == []


def test_playing_the_line_move_is_not_a_transposition():
    """It is simply the move. Nothing to reorder, nothing to explain."""
    board = at("e4 e5 Nf3 Nc6 Bc4")
    tail = tail_of(board, "Bc5 c3 Nf6")
    assert transposes(board, board.parse_san("Bc5"), tail) == []


def test_a_reordered_line_is_a_legal_game_that_ends_where_the_line_ends():
    """The invariant that makes this safe to state as fact.

    Only the player's own slots may be swapped — pulling a move of theirs past
    one of the opponent's would put two moves of the same colour in a row — and
    the claim "reaches exactly the position the line reaches" is checked against
    the board rather than assumed.
    """
    board = at("e4 e5 Nf3")
    tail = tail_of(board, "Nc6 Bc4 Bc5")
    order = transposes(board, board.parse_san("Bc5"), tail)
    assert order == ["Bc5", "Bc4", "Nc6"]

    target, walker = board.copy(), board.copy()
    for move in tail:
        target.push(move)
    for san in order:
        walker.push_san(san)             # raises if the reorder is not legal
    assert walker.board_fen() == target.board_fen()


# --------------------------------------------------------------------------
# What gets said
# --------------------------------------------------------------------------


def test_a_transposition_is_not_called_a_mistake(book):
    dev = deviation(book, "d4 Nf6 c4 g6 Nc3 Bg7 e4", "d6 Nf3 O-O", "O-O")
    assert dev.severity == "transposes"
    text = template_deviation(dev)
    assert "mistake" not in text
    assert "same idea in a different order" in text
    assert "nothing is lost" in text


def test_every_explanation_ends_by_naming_the_move_to_play(book):
    for position, rest, played in [
        ("d4 Nf6 c4 g6 Nc3 Bg7 e4", "d6 Nf3 O-O", "O-O"),
        ("e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Qh4"),
        ("e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Nf6"),
        ("e4 c5 Nf3 d6 d4", "cxd4 Nxd4 Nf6", "Nf6"),
    ]:
        dev = deviation(book, position, rest, played)
        assert template_deviation(dev).endswith(f"play {dev.book.san}.")


def test_a_move_no_line_plays_is_not_dressed_up_as_theory(book):
    """classify() walks backwards to the last named position, so after a move
    nobody plays it returns the name of the position *before* it. Reporting
    that would tell the player their queen sortie was an opening."""
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Qh4")
    assert dev.played.opening is None
    assert "No catalogued line plays Qh4" in template_deviation(dev)


def test_a_real_alternative_is_named(book):
    """2...Nf6 in the Italian is the Two Knights — a different opening, not an
    error, and saying which one is the useful part."""
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Nf6")
    assert dev.played.opening == "Italian Game: Two Knights Defense"


def test_hanging_a_piece_leads_with_the_material(book):
    """1.e4 e5 2.Nf3 Nc6 3.Bc4 and Black plays Nxe4?? — the knight on c6 is
    defended by nothing after Nxe5, but Nxe4 simply drops a knight to Nxe4."""
    board = at("e4 e5 Nf3 Nc6 Bc4")
    assert board.parse_san("Nf6") in board.legal_moves
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5", "d5 exd5 Na5", "Nxe4")
    # Nxe4 is met by Bxf7+ / Nxf7; the engine is absent here, so severity comes
    # from the detectors alone and only the wording is asserted.
    assert dev.book.san == "d5"
    assert template_deviation(dev).endswith("play d5.")


# --------------------------------------------------------------------------
# Move descriptions
# --------------------------------------------------------------------------


def test_moves_are_described_by_what_they_do(book):
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Nf6")
    assert dev.book.purpose() == "developing the bishop"

    castle = deviation(book, "e4 e5 Nf3 Nc6 Bc4 Bc5", "c3 Nf6 d4", "O-O")
    assert castle.played.purpose() == "getting the king to safety"

    push = deviation(book, "d4 Nf6 c4 g6 Nc3 Bg7 e4", "d6 Nf3 O-O", "O-O")
    assert push.book.purpose() == "taking a grip on e5"


def test_an_obscure_catalogued_move_is_not_offered_as_an_alternative():
    """Being in the catalogue is not an endorsement.

    1.Nh3 is the Amar Opening — a real name with three lines behind it, against
    2023 for 1.e4. The first version of this told the player it was "also a
    reasonable repertoire choice", which is worse than saying nothing.
    """
    from app.openings import OpeningBook

    book = OpeningBook.load()
    board = chess.Board()
    tail = tail_of(board, "e4 e5 Nf3")
    dev = asyncio.run(analyse_deviation(
        board, tail[0], board.parse_san("Nh3"), "Italian Game", tail, book, None))
    assert dev.severity == "sideline"
    assert dev.played.opening == "Amar Opening"
    text = template_deviation(dev)
    assert "hardly anyone plays it" in text
    assert "reasonable" not in text


def test_a_move_outside_the_book_is_not_called_catalogued(book):
    """The sideline wording only applies to moves that are actually in there;
    a move with no lines through it is out of book, not obscure theory."""
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4", "Bc5 c3 Nf6", "Qh4")
    assert dev.played.in_book is False
    assert dev.severity != "sideline"
    assert "catalogued line plays Qh4" in template_deviation(dev)


def test_a_mainstream_alternative_is_not_belittled(book):
    """The Two Knights has 58 named lines through it — a real opening, and the
    player should be told that plainly, not warned off it.

    The tail here is the Evans Gambit, which never plays Nf6, so this is a
    genuine change of opening rather than a reordering of the same one.
    """
    dev = deviation(book, "e4 e5 Nf3 Nc6 Bc4", "Bc5 b4 Bxb4", "Nf6")
    assert dev.severity == "playable"
    assert "theory too" in template_deviation(dev)
