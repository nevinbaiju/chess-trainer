"""Static exchange evaluation.

Every expected value here is hand-calculable from the position, which is the
point: SEE is the primitive the whole "did you just hang that?" layer rests on,
so it must be trustworthy in isolation.
"""

import chess
import pytest

from app.see import PIECE_VALUES, best_capture_on, hangs_after, see

P = PIECE_VALUES[chess.PAWN]
N = PIECE_VALUES[chess.KNIGHT]
R = PIECE_VALUES[chess.ROOK]
Q = PIECE_VALUES[chess.QUEEN]


def _see(fen: str, uci: str) -> int:
    board = chess.Board(fen)
    return see(board, chess.Move.from_uci(uci))


def test_capturing_a_free_pawn_wins_a_pawn():
    assert _see("4k3/8/8/4p3/8/3N4/8/4K3 w - - 0 1", "d3e5") == P


def test_knight_takes_pawn_defended_by_pawn_loses_the_exchange():
    # +100 for the pawn, -300 when the d6 pawn recaptures the knight.
    assert _see("4k3/8/3p4/4p3/8/3N4/8/4K3 w - - 0 1", "d3e5") == P - N


def test_rook_takes_pawn_defended_by_pawn():
    assert _see("4k3/8/3p4/4p3/8/8/4R3/4K3 w - - 0 1", "e2e5") == P - R


def test_doubled_rooks_recover_part_of_the_exchange():
    # Rxe5 (+100), dxe5 (-500), Rxe5 (+100). The x-ray of the back rook is
    # discovered automatically once the front rook's capture is pushed.
    assert _see("4k3/8/3p4/4p3/8/8/4R3/4RK2 w - - 0 1", "e2e5") == P - R + P


def test_even_queen_trade_is_neutral():
    assert _see("3qk3/8/8/8/8/8/8/3QK3 w - - 0 1", "d1d8") == 0


def test_moving_a_piece_en_prise_scores_its_full_value():
    # Qd5?? walks in front of the c6 pawn with nothing defending d5.
    assert _see("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "d1d5") == -Q


def test_hangs_after_reports_loose_material_as_positive():
    fen = "4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1"
    assert hangs_after(chess.Board(fen), chess.Move.from_uci("d1d5")) == Q
    # A safe developing move hangs nothing.
    assert hangs_after(chess.Board(fen), chess.Move.from_uci("d1d2")) == 0


def test_a_king_may_not_recapture_into_a_defended_square():
    """The king is a legal attacker only when the square is truly undefended.

    Without that guard SEE would happily "recapture" with the king and report a
    losing exchange as even.
    """
    # Black king on c6 is the only defender of d5; White's rook covers d5, so
    # after exd5 Black simply cannot take back.
    guarded = "8/8/2k5/3p4/4P3/8/8/3RK3 w - - 0 1"
    assert _see(guarded, "e4d5") == P

    # Remove the rook and the king recaptures, making it an even trade.
    unguarded = "8/8/2k5/3p4/4P3/8/8/4K3 w - - 0 1"
    assert _see(unguarded, "e4d5") == 0


def test_promotion_is_counted_in_the_exchange():
    # Pushing to the eighth rank with capture: pawn becomes a queen.
    value = _see("4k1n1/6P1/8/8/8/8/8/4K3 w - - 0 1", "g7g8q")
    assert value > PIECE_VALUES[chess.KNIGHT]


def test_en_passant_capture_is_valued_as_a_pawn():
    board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
    move = chess.Move.from_uci("e5d6")
    assert board.is_en_passant(move)
    assert see(board, move) == P


def test_best_capture_on_finds_the_winning_capture():
    board = chess.Board("4k3/8/8/4p3/8/3N4/8/4K3 w - - 0 1")
    found = best_capture_on(board, chess.E5)
    assert found is not None
    move, value = found
    assert board.san(move) == "Nxe5"
    assert value == P


def test_best_capture_on_declines_a_losing_capture():
    # e5 is defended by the d6 pawn, so there is no profitable capture.
    board = chess.Board("4k3/8/3p4/4p3/8/3N4/8/4K3 w - - 0 1")
    assert best_capture_on(board, chess.E5) is None


def test_see_leaves_the_board_untouched():
    board = chess.Board("4k3/8/3p4/4p3/8/3N4/8/4K3 w - - 0 1")
    before = board.fen()
    see(board, chess.Move.from_uci("d3e5"))
    assert board.fen() == before


# --------------------------------------------------------------------------
# The rule behind the "something is hanging" hint
# --------------------------------------------------------------------------


def _best_capture_value(fen: str) -> int:
    """What the side to move can win by capturing — the hint's trigger."""
    board = chess.Board(fen)
    return max(
        (see(board, m) for m in board.legal_moves if board.is_capture(m)),
        default=0,
    )


def test_a_hanging_knight_triggers_the_hint():
    assert _best_capture_value("4k3/8/8/4n3/8/8/1B6/4K3 w - - 0 1") >= PIECE_VALUES[chess.KNIGHT]


def test_a_defended_knight_does_not():
    # The d6 pawn recaptures, so Bxe5 is not free material.
    assert _best_capture_value("4k3/8/3p4/4n3/8/8/1B6/4K3 w - - 0 1") < PIECE_VALUES[chess.KNIGHT]


def test_a_loose_pawn_is_below_the_threshold():
    """Pawns are excluded on purpose.

    At this level the lesson is pieces; firing on every loose pawn would make
    the hint constant and therefore ignored.
    """
    value = _best_capture_value("4k3/8/8/4p3/8/8/1B6/4K3 w - - 0 1")
    assert 0 < value < PIECE_VALUES[chess.KNIGHT]


def test_nothing_to_take_means_no_hint():
    assert _best_capture_value("4k3/8/8/8/8/8/8/4K2R w - - 0 1") == 0


def test_a_hanging_queen_triggers_it_too():
    assert _best_capture_value("4k3/8/8/4q3/8/8/1B6/4K3 w - - 0 1") >= PIECE_VALUES[chess.KNIGHT]
