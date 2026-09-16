"""Guards: stopping play on a mistake and offering the move back."""

import json

import chess
import pytest

from app.db import Database
from app.motifs import Motif, facts_for_move
from app.see import see


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")


# --------------------------------------------------------------------------
# Which mistakes can stop play
# --------------------------------------------------------------------------


def test_guardable_motifs_are_all_real_motifs():
    from app.motifs import GUARDABLE

    known = {m.value for m in Motif}
    assert set(GUARDABLE) <= known, set(GUARDABLE) - known


def test_standing_features_of_a_position_are_not_guardable():
    """A guard has to be about a move you could sensibly take back.

    "Back-rank weakness" and "king left in the centre" describe where your king
    is, not a move you just made — offering a takeback for them would fire on
    moves that were fine.
    """
    from app.motifs import GUARDABLE

    assert Motif.BACK_RANK.value not in GUARDABLE
    assert Motif.LOST_CASTLING_SAFETY.value not in GUARDABLE


def test_the_default_guards_are_the_two_that_actually_happen():
    """Measured: hanging a piece appears in 15 of 17 reviewed games, and half
    the material the opponent leaves hanging goes untaken."""
    import re, pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
    body = src.read_text()
    match = re.search(r'"guards":\s*\[([^\]]*)\]', body)
    assert match, "DEFAULT_SETTINGS no longer declares guards"
    assert set(re.findall(r'"(\w+)"', match.group(1))) == {
        "hung_piece", "missed_free_piece"
    }


# --------------------------------------------------------------------------
# What a guard fires on
# --------------------------------------------------------------------------


def test_hanging_a_piece_produces_a_guardable_finding():
    """The guard reuses the review's detectors, so the two can never disagree."""
    board = chess.Board("rnbqkbnr/ppppppp1/7p/8/8/7N/PPPPPPPP/RNBQKB1R w KQkq - 0 1")
    move = board.parse_san("Ng5")
    assert see(board, move) < 0
    motifs = {f.motif for f in facts_for_move(board, move)}
    assert Motif.HUNG_PIECE in motifs


def test_a_sound_developing_move_produces_nothing_to_guard():
    board = chess.Board()
    assert facts_for_move(board, board.parse_san("e4")) == []


def test_an_even_trade_is_not_a_hung_piece():
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 1")
    move = board.parse_san("Nxe5")       # the e5 pawn is undefended here
    assert see(board, move) >= 0
    assert Motif.HUNG_PIECE not in {f.motif for f in facts_for_move(board, move)}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_a_new_game_starts_with_no_takebacks_and_nothing_pending(db):
    game_id = db.create_game(player_color="white", source="trainer")
    row = db.get_game(game_id)
    assert row["takebacks"] == 0
    assert row["pending"] is None


def test_a_pending_warning_survives_a_reload(db):
    """It is stored, not held in memory, so refreshing mid-warning is safe."""
    game_id = db.create_game(player_color="white", source="trainer", moves="e2e4")
    warning = {"motif": "hung_piece", "message": "…", "san": "Ng5", "uci": "h3g5"}
    db.update_game(game_id, pending=json.dumps(warning))
    db.close()

    reopened = Database(db.path)
    assert json.loads(reopened.get_game(game_id)["pending"])["motif"] == "hung_piece"


def test_takebacks_accumulate(db):
    game_id = db.create_game(player_color="white", source="trainer")
    for expected in (1, 2, 3):
        db.update_game(game_id, takebacks=db.get_game(game_id)["takebacks"] + 1)
        assert db.get_game(game_id)["takebacks"] == expected
