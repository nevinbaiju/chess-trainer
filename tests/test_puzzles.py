"""Choosing puzzles from measured weaknesses, and playing one out."""

import random

import chess
import pytest

from app.motifs import Motif
from app.puzzle_themes import INGEST_THEMES, MOTIF_THEMES, DIRECT, INVERTED
from app.puzzles import (opening_position, pick_motif, pick_theme,
                         weighted_motifs, your_plies)


# --------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------


def test_every_motif_we_detect_has_a_theme():
    """A motif with no mapping is a weakness the trainer silently cannot help
    with, which is worse than one it refuses out loud."""
    assert {m.value for m in Motif} == set(MOTIF_THEMES)


def test_the_two_direct_mappings_are_the_misses():
    """Only failing to *take* free material and failing to *find* mate have
    puzzles that train the exact skill; everything else is the pattern seen
    from the winning side."""
    direct = {m for m, t in MOTIF_THEMES.items() if t.direction == DIRECT}
    assert direct == {"missed_free_piece", "missed_mate"}


def test_defensive_motifs_are_marked_inverted_not_direct():
    """A fork puzzle trains you to play forks. Labelling that as training you
    not to be forked would be a lie the UI would then repeat."""
    for motif in ("allowed_fork", "allowed_pin", "allowed_skewer", "hung_piece"):
        assert MOTIF_THEMES[motif].direction == INVERTED


def test_the_ingest_list_is_exactly_what_the_mapping_needs():
    used = {t for entry in MOTIF_THEMES.values() for t in entry.themes}
    assert set(INGEST_THEMES) == used


def test_lichess_defensive_move_is_not_used():
    """It is the theme that would train prophylaxis properly, and it sits at a
    median rating of 1898 — hopeless at this level. Deliberately absent."""
    assert "defensiveMove" not in INGEST_THEMES


# --------------------------------------------------------------------------
# Weighting
# --------------------------------------------------------------------------


WEAKNESSES = [
    {"motif": "hung_piece", "games": 32},
    {"motif": "allowed_pin", "games": 26},
    {"motif": "missed_mate", "games": 13},
]


def test_puzzles_are_drawn_in_proportion_to_games_lost():
    rng = random.Random(11)
    picks = [pick_motif(WEAKNESSES, rng) for _ in range(6000)]
    share = {m: picks.count(m) / len(picks) for m in {p for p in picks}}
    assert share["hung_piece"] > share["allowed_pin"] > share["missed_mate"]
    assert 0.40 < share["hung_piece"] < 0.52      # 32/71 of the weighted total


def test_the_tail_still_comes_up():
    """Weighted rather than "always the top one": the rarer mistakes need
    practice too, and the mix re-balances itself as the stats move."""
    rng = random.Random(3)
    picks = {pick_motif(WEAKNESSES, rng) for _ in range(400)}
    assert picks == {"hung_piece", "allowed_pin", "missed_mate"}


def test_a_motif_with_no_puzzles_is_never_drawn():
    assert pick_motif([{"motif": "not_a_motif", "games": 99}]) is None
    assert weighted_motifs([{"motif": "hung_piece", "games": 0}]) == []


def test_no_weaknesses_yet_means_no_pick():
    assert pick_motif([]) is None


def test_every_mapped_theme_resolves_to_something_drawable():
    for motif in MOTIF_THEMES:
        assert pick_theme(motif) in INGEST_THEMES


# --------------------------------------------------------------------------
# Playing the puzzle
# --------------------------------------------------------------------------


def test_the_puzzle_starts_one_ply_after_the_stored_fen():
    """Lichess stores the position BEFORE the opponent's move, and Moves[0] is
    that move. Miss this and every puzzle is shown a move early, from the wrong
    side, with the solution looking illegal.
    """
    fen = "r6k/pp2r2p/4Rp1Q/3p4/8/1N1P2R1/PqP2bPP/7K b - - 0 24"
    moves = "f2g3 e6e7 b2b1 b3c1 b1c1 h6c1".split()
    before = chess.Board(fen)
    assert before.turn == chess.BLACK

    board, first = opening_position(fen, moves)
    assert first == "f2g3"
    assert board.turn == chess.WHITE, "the solver plays the side to move after it"
    assert chess.Move.from_uci(moves[1]) in board.legal_moves


def test_the_solver_plays_the_odd_plies():
    moves = "f2g3 e6e7 b2b1 b3c1 b1c1 h6c1".split()
    assert your_plies(moves) == [1, 3, 5]


def test_a_one_move_puzzle_is_a_single_reply():
    fen = chess.Board().fen()
    moves = ["e2e4", "e7e5"]
    board, _ = opening_position(fen, moves)
    assert your_plies(moves) == [1]
    assert chess.Move.from_uci("e7e5") in board.legal_moves


def test_the_solvers_colour_does_not_change_when_they_solve_it():
    """It is fixed by the position they were handed. Recomputing it from the
    live board flips it on the final move and spins the board round at the
    moment of solving."""
    # The normal shape: the opponent's move, then alternating pairs, so the
    # list has even length and the last ply is the solver's. After it, the side
    # to move is the opponent — which is exactly when the naive version flips.
    fen = "1r4k1/5p1p/5Pp1/3B3P/3n2P1/P3r3/2p3K1/1R6 w - - 0 45"
    moves = "b1b8 e3e8 b8e8 g8g7".split()
    board, _ = opening_position(fen, moves)
    solver = board.turn
    for uci in moves[1:]:
        board.push(chess.Move.from_uci(uci))
    assert board.turn != solver, "fixture must end on the other side to be a test"

    # What the payload reports has to stay the solver's colour throughout.
    replay, _ = opening_position(fen, moves)
    assert replay.turn == solver


# --------------------------------------------------------------------------
# Hints, disclosure and the replay line
# --------------------------------------------------------------------------


def test_the_solution_line_carries_a_position_for_every_ply():
    """So the front end can animate it and step through without needing any
    chess rules of its own."""
    from app.puzzles import solution_line

    fen = "1r4k1/5p1p/5Pp1/3B3P/3n2P1/P3r3/2p3K1/1R6 w - - 0 45"
    moves = "b1b8 e3e8 b8e8".split()
    line = solution_line(fen, moves)

    assert [s["uci"] for s in line] == moves[1:]
    assert [s["yours"] for s in line] == [True, False], \
        "the solution starts with the solver's move, then alternates"
    assert all(s["san"] and s["fen"] for s in line)

    board = chess.Board(line[-1]["fen"])          # every FEN must be legal
    assert board.is_valid()


def test_the_line_starts_where_the_player_was_left():
    from app.puzzles import solution_line

    fen = "1r4k1/5p1p/5Pp1/3B3P/3n2P1/P3r3/2p3K1/1R6 w - - 0 45"
    moves = "b1b8 e3e8 b8e8".split()
    shown, _ = opening_position(fen, moves)
    first = chess.Board(solution_line(fen, moves)[0]["fen"])

    replay = shown.copy()
    replay.push(chess.Move.from_uci(moves[1]))
    assert replay.fen() == first.fen()


def test_hints_are_recorded_so_a_solve_with_one_is_not_called_clean(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "t.db")
    db.conn.execute(
        "INSERT INTO puzzles (id, fen, moves, rating, themes) VALUES "
        "('p1', ?, 'e2e4 e7e5', 900, 'hangingPiece')", (chess.Board().fen(),))
    served = db.record_puzzle_served("p1", "hung_piece", "hangingPiece")
    assert db.current_puzzle()["hints"] == 0

    db.add_puzzle_hint("p1", served, 1)
    assert db.current_puzzle()["hints"] == 1
    db.add_puzzle_hint("p1", served, 2)
    assert db.current_puzzle()["hints"] == 2
    # A later, weaker hint must not walk the count backwards.
    db.add_puzzle_hint("p1", served, 1)
    assert db.current_puzzle()["hints"] == 2


def test_a_bookmark_survives_and_can_be_listed(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "t.db")
    db.conn.execute(
        "INSERT INTO puzzles (id, fen, moves, rating, themes) VALUES "
        "('p1', ?, 'e2e4 e7e5', 900, 'fork')", (chess.Board().fen(),))
    db.record_puzzle_served("p1", "allowed_fork", "fork")
    assert db.bookmark_count() == 0

    db.set_bookmark("p1", True)
    assert db.bookmark_count() == 1
    assert [b["id"] for b in db.bookmarks()] == ["p1"]

    db.set_bookmark("p1", False)
    assert db.bookmarks() == []


def test_replaying_a_puzzle_does_not_leave_two_open_attempts(tmp_path):
    """Otherwise current_puzzle() returns whichever sorted first and the board
    shows one puzzle while the server scores another."""
    from app.db import Database

    db = Database(tmp_path / "t.db")
    for pid in ("p1", "p2"):
        db.conn.execute(
            "INSERT INTO puzzles (id, fen, moves, rating, themes) VALUES "
            "(?, ?, 'e2e4 e7e5', 900, 'fork')", (pid, chess.Board().fen()))
    db.record_puzzle_served("p1", "allowed_fork", "fork")
    db.abandon_open_puzzles()
    db.record_puzzle_served("p2", "allowed_fork", "fork")

    open_rows = db.conn.execute(
        "SELECT COUNT(*) FROM puzzle_attempts WHERE solved IS NULL").fetchone()[0]
    assert open_rows == 1
    assert db.current_puzzle()["puzzle_id"] == "p2"


def test_the_live_puzzle_row_carries_every_column_of_both_tables(tmp_path):
    """current_puzzle() selects named columns rather than *, so a column added
    to `puzzles` later goes missing here — and the endpoint 500s on a lookup
    that no unit test of the payload alone would catch. It did, once."""
    from app.db import Database

    db = Database(tmp_path / "t.db")
    db.conn.execute(
        "INSERT INTO puzzles (id, fen, moves, rating, themes) VALUES "
        "('p1', ?, 'e2e4 e7e5', 900, 'fork')", (chess.Board().fen(),))
    db.record_puzzle_served("p1", "allowed_fork", "fork")

    row = db.current_puzzle()
    have = set(row.keys())
    for table in ("puzzles", "puzzle_attempts"):
        for column in (c[1] for c in db.conn.execute(f"PRAGMA table_info({table})")):
            if column == "id":
                continue          # joined in as puzzle_id
            assert column in have, f"current_puzzle() does not select {table}.{column}"
