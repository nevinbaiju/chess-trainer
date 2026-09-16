"""Motif detection.

Every fixture here was checked against python-chess before being written down —
FEN validity, move legality, and that the tactic being tested actually exists in
the position. That discipline matters more than usual here: a plausible-looking
FEN that doesn't contain the tactic produces a silently passing "detects
nothing" test and hides a broken detector.
"""

import chess
import pytest

from app.motifs import Motif, facts_for_move
from app.motifs.detect import detect_back_rank, detect_fork
from app.motifs.primitives import is_in_bad_spot, is_trapped, loose_pieces


def _facts(fen: str, san: str, refutation: tuple[str, ...] = ()):
    board = chess.Board(fen)
    assert board.is_valid(), f"fixture FEN is not a legal position: {fen}"
    move = board.parse_san(san)

    after = board.copy()
    after.push(move)
    reply_moves = []
    for reply in refutation:
        parsed = after.parse_san(reply)
        reply_moves.append(parsed)
        after.push(parsed)

    return facts_for_move(board, move, refutation=reply_moves)


def _motifs(findings) -> set[Motif]:
    return {f.motif for f in findings}


# --------------------------------------------------------------------------
# Hanging material
# --------------------------------------------------------------------------


def test_detects_a_queen_walking_in_front_of_a_pawn():
    findings = _facts("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "Qd5")
    assert Motif.HUNG_PIECE in _motifs(findings)
    hung = next(f for f in findings if f.motif is Motif.HUNG_PIECE)
    assert hung.material == 900
    assert "d5" in hung.squares


def test_detects_the_classic_early_queen_sortie():
    """1.e4 e5 2.Nf3 Qg5?? — the move a 600-rated player actually plays."""
    findings = _facts(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2", "Qg5"
    )
    assert Motif.HUNG_PIECE in _motifs(findings)


def test_a_safe_developing_move_produces_no_findings():
    """Bc4 after 1.e4 e5 attacks nothing and hangs nothing. Silence is correct."""
    findings = _facts(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2", "Bc4"
    )
    assert findings == []


def test_a_defended_piece_is_not_reported_as_free():
    """The knight on e5 is defended by the c6 knight, so Nxe5 is an even trade."""
    findings = _facts(
        "r1bqkb1r/pppp1ppp/2n5/4n3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 5", "d3"
    )
    assert Motif.MISSED_FREE_PIECE not in _motifs(findings)


# --------------------------------------------------------------------------
# Missed material — the "awareness" signal
# --------------------------------------------------------------------------


def test_detects_ignoring_a_free_rook():
    findings = _facts("3r4/8/8/8/7k/8/4P3/3QK3 w - - 0 1", "e3")
    missed = next(f for f in findings if f.motif is Motif.MISSED_FREE_PIECE)
    assert missed.material == 500
    assert "d8" in missed.squares
    assert "Qxd8" in missed.phrase


def test_taking_something_small_still_counts_as_missing_something_big():
    """Grabbing a pawn while a rook hangs is a miss.

    The first implementation skipped the check entirely whenever the move was a
    capture, which let this straight through.
    """
    findings = _facts("3r4/8/8/8/7k/1p6/8/3QK3 w - - 0 1", "Qxb3")
    missed = next(f for f in findings if f.motif is Motif.MISSED_FREE_PIECE)
    assert missed.material == 500


def test_taking_the_best_thing_available_is_not_a_miss():
    findings = _facts("3r4/8/8/8/7k/1p6/8/3QK3 w - - 0 1", "Qxd8+")
    assert Motif.MISSED_FREE_PIECE not in _motifs(findings)


def test_a_hanging_pawn_is_below_the_reporting_threshold():
    """At 600 Elo the lesson is pieces, not pawns; noise drowns the signal."""
    findings = _facts("4k3/8/8/8/7p/8/8/3QK3 w - - 0 1", "Qd2")
    assert Motif.MISSED_FREE_PIECE not in _motifs(findings)


# --------------------------------------------------------------------------
# Motifs visible only in the refutation
# --------------------------------------------------------------------------


def test_detects_a_fork_allowed_by_the_move():
    """The fork is in the opponent's reply, not in your own move."""
    findings = _facts(
        "r2qk3/8/8/1N6/8/8/8/4K3 b - - 0 1", "Qg5", refutation=("Nc7+",)
    )
    fork = next(f for f in findings if f.motif is Motif.ALLOWED_FORK)
    assert "Nc7+" in fork.phrase
    assert "a8" in fork.squares and "e8" in fork.squares


def test_fork_material_excludes_the_king_sentinel():
    """The king is valued at 10000 so a check counts as a prong.

    That sentinel must not leak into the material figure handed to the coach —
    "this costs you 100 pawns" is nonsense.
    """
    findings = _facts(
        "r2qk3/8/8/1N6/8/8/8/4K3 b - - 0 1", "Qg5", refutation=("Nc7+",)
    )
    fork = next(f for f in findings if f.motif is Motif.ALLOWED_FORK)
    assert fork.material == 500  # the rook, not the king


def test_no_refutation_means_no_allowed_motifs():
    findings = _facts("r2qk3/8/8/1N6/8/8/8/4K3 b - - 0 1", "Qg5")
    assert not {m for m in _motifs(findings) if m.value.startswith("allowed_")}


def test_a_forking_piece_that_can_simply_be_captured_is_not_a_fork():
    """Upstream's rule, and a good one: if you can take the forker, it isn't a fork."""
    board = chess.Board("r2qk3/8/8/1N6/8/8/8/4K3 w - - 0 1")
    board.push(board.parse_san("Nc7+"))
    # The queen on d8 covers c7, so the knight is hanging there.
    assert detect_fork(board, chess.C7) is None


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


def test_is_trapped_does_not_mutate_the_board():
    """Upstream returns early from inside its escape loop with a move still pushed."""
    board = chess.Board("4k3/pp6/8/8/8/8/8/B3K3 w - - 0 1")
    before = board.fen()
    for square in list(board.piece_map()):
        is_trapped(board, square)
    assert board.fen() == before


def test_loose_pieces_ignores_kings():
    board = chess.Board("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    assert all(
        board.piece_at(sq).piece_type != chess.KING for sq, _ in loose_pieces(board, chess.WHITE)
    )


def test_is_in_bad_spot_requires_an_attacker():
    board = chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 0 1")
    assert not is_in_bad_spot(board, chess.D1)


def test_back_rank_detects_a_boxed_in_king():
    # King on g8 behind f7/g7/h7 pawns, White rook on the open d-file.
    board = chess.Board("6k1/5ppp/8/8/8/8/8/3R2K1 w - - 0 1")
    assert detect_back_rank(board, chess.BLACK)


def test_a_rook_that_cannot_reach_the_back_rank_is_not_a_threat():
    """Owning a rook is not a back-rank threat; being able to get there is.

    Here the rook sits on h1 behind Black's own h7 pawn, so it cannot reach the
    eighth rank at all. Without this check the detector fired on essentially
    every castled position — it was the single most reported "weakness" in real
    games, which buried the mistakes that actually decided them.
    """
    stuck = chess.Board("6k1/5ppp/8/8/8/8/8/4K2R w - - 0 1")
    assert not detect_back_rank(stuck, chess.BLACK)


def test_an_untroubled_castled_king_is_not_a_weakness():
    board = chess.Board(
        "rnbq1rk1/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w - - 0 1"
    )
    assert not detect_back_rank(board, chess.BLACK)


def test_back_rank_is_false_when_the_king_has_air():
    # h7 pawn pushed to h6 gives the king an escape square.
    board = chess.Board("6k1/5pp1/7p/8/8/8/8/4K2R w - - 0 1")
    assert not detect_back_rank(board, chess.BLACK)


def test_findings_are_ordered_by_material_at_stake():
    findings = _facts("3r4/8/8/8/7k/8/4P3/3QK3 w - - 0 1", "e3")
    materials = [f.material for f in findings]
    assert materials == sorted(materials, reverse=True)


def test_back_rank_needs_an_enemy_major_piece():
    """A warning that can never cash out teaches the player to ignore warnings."""
    with_rook = chess.Board("6k1/5ppp/8/8/8/8/8/3R2K1 w - - 0 1")
    no_major = chess.Board("6k1/5ppp/8/8/8/8/5N2/4K3 w - - 0 1")
    assert detect_back_rank(with_rook, chess.BLACK)
    assert not detect_back_rank(no_major, chess.BLACK)


def test_an_uncastled_king_is_not_a_back_rank_weakness():
    """A king still on d/e is stuck in the centre — a different lesson.

    Reporting "boxed in by its own pawns" for an e1 king on move 10 is
    technically true and pedagogically useless.
    """
    board = chess.Board("4r1k1/5ppp/8/8/8/8/3PPP2/4K3 w - - 0 1")
    assert not detect_back_rank(board, chess.WHITE)


def test_king_in_the_centre_is_reported_instead():
    findings = _facts(
        "4r1k1/5ppp/8/8/8/7P/3P1P2/3K4 w - - 0 1", "h4", refutation=("Re1+",)
    )
    assert Motif.LOST_CASTLING_SAFETY in _motifs(findings)
