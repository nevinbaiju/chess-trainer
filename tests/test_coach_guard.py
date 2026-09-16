"""The grounding check on what the model writes.

The division of labour in app/coach.py is that the model never does chess. That
only holds if every move it writes is actually checked, which is why the
typography matters: a castling move written with the wrong hyphen used to slip
past the pattern and reach the player unverified.
"""

import chess
import pytest

from app.coach import normalise, validate


@pytest.mark.parametrize("dash", ["‐", "‑", "–", "—", "−"])
def test_castling_written_with_any_dash_becomes_a_real_move(dash):
    assert normalise(f"you can play O{dash}O here") == "you can play O-O here"


def test_prose_dashes_are_left_alone():
    """Rewriting every dash would mangle the writing to fix the chess."""
    assert normalise("O-O is safe — the king is tucked away") == (
        "O-O is safe — the king is tucked away"
    )


def test_a_castling_move_the_facts_never_mentioned_is_caught():
    """The hole this closes.

    Before normalising, "O‑O" did not match the move pattern, so it was never
    looked at — the one move a beginner is most likely to be told to play, and
    the only one written with a hyphen.
    """
    board = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 0 1")
    facts = {"Nf3", "e4"}                      # deliberately excludes castling
    assert validate("play O‑O now", board, facts) is None      # slipped through
    assert validate(normalise("play O‑O now"), board, facts) is not None
