"""Game-phase division — a port of scalachess ``Divider.scala``.

The strongest correctness signal available without running lila itself is that
the hand-tuned mixedness table scores the *starting position at exactly zero*.
The table is constructed to do that (note the "group of 4 on the homerow = 0"
comment in the original), so any indexing or rank-orientation slip shows up
immediately as a non-zero score.
"""

import io

import chess
import chess.pgn
import pytest

from app.phases import (
    _MIXEDNESS_REGIONS,
    _backrank_sparse,
    _majors_and_minors,
    _mixedness,
    divide,
    divide_game,
)

# Morphy - Duke of Brunswick & Count Isouard, Paris 1858.
OPERA_GAME = """
1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6
7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5 10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O Rd8
13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0
"""


def _moves(pgn: str) -> list[chess.Move]:
    game = chess.pgn.read_game(io.StringIO(pgn))
    return list(game.mainline_moves())


def test_there_are_fortynine_mixedness_regions():
    assert len(_MIXEDNESS_REGIONS) == 49


def test_starting_position_has_zero_mixedness():
    """The canary for the whole port. If this drifts, the indexing is wrong."""
    assert _mixedness(chess.Board()) == 0


def test_mixedness_rises_as_the_armies_meet():
    start = _mixedness(chess.Board())
    opened = _mixedness(
        chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
    )
    engaged = _mixedness(
        chess.Board("r2q1rk1/1b2bppp/p2ppn2/1p6/3NPP2/2N1B3/PPPQ2PP/2KR1B1R w - - 0 13")
    )
    assert start < opened < engaged
    assert engaged > 150  # crosses lichess's middlegame trigger


def test_majors_and_minors_excludes_kings_and_pawns():
    assert _majors_and_minors(chess.Board()) == 14  # 2R 2N 2B 1Q per side
    assert _majors_and_minors(chess.Board("4k3/8/8/8/8/8/8/R3K3 w Q - 0 1")) == 1


def test_backrank_sparse_detects_development():
    assert not _backrank_sparse(chess.Board())
    # Both bishops and the queen developed, king castled: only Ra1, Rf1, Kg1 left.
    developed = chess.Board(
        "r1bq1rk1/pppp1ppp/2n2n2/2b1p1B1/2B1P3/2N2N2/PPPPQPPP/R4RK1 w - - 0 1"
    )
    assert _backrank_sparse(developed)


def test_opening_is_the_default_phase():
    division = divide([chess.Board()])
    assert division.phase_at(0) == "opening"


def test_bare_rook_endgame_is_an_endgame_immediately():
    board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w Q - 0 1")
    division = divide([board])
    assert division.end == 0
    assert division.phase_at(0) == "endgame"


def test_middlegame_marker_is_dropped_when_the_endgame_starts_first():
    """Mirrors lila's ``midGame.filter(m => m < end)``.

    A position that is already an endgame satisfies the middlegame condition
    too; reporting both at ply 0 would be nonsense.
    """
    division = divide([chess.Board("4k3/8/8/8/8/8/8/R3K3 w Q - 0 1")])
    assert division.middle is None


def test_opera_game_divides_into_three_phases_in_order():
    division = divide_game(chess.Board(), _moves(OPERA_GAME))
    assert division.middle is not None
    assert division.end is not None
    assert 0 < division.middle < division.end <= division.plies


def test_opera_game_phase_lookup_is_monotonic():
    division = divide_game(chess.Board(), _moves(OPERA_GAME))
    order = {"opening": 0, "middlegame": 1, "endgame": 2}
    seen = [order[division.phase_at(ply)] for ply in range(division.plies)]
    assert seen == sorted(seen)
    assert division.phase_at(0) == "opening"
    assert division.phase_at(division.plies - 1) == "endgame"


def test_divide_game_does_not_mutate_the_caller_board():
    board = chess.Board()
    divide_game(board, _moves(OPERA_GAME))
    assert board.fen() == chess.STARTING_FEN
