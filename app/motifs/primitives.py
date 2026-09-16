"""Tactical primitives, ported from lichess-puzzler's ``tagger/util.py``.

Upstream: https://github.com/ornicar/lichess-puzzler (AGPL-3.0). These are the
building blocks lichess itself uses to auto-tag its puzzle database, and they
are the only serious open motif-detection code that exists for chess. Ported
rather than vendored wholesale because upstream's detectors are shaped around a
*puzzle mainline* (``ChildNode`` walks), whereas we need to ask questions about
an arbitrary position and an engine's refutation line.

Two deliberate changes from upstream:

1. ``is_trapped`` no longer leaks a pushed move. Upstream returns early from
   inside its escape loop while the board still has the trial move applied::

       board.push(escape)
       if not is_in_bad_spot(board, escape.to_square):
           return False          # <- board still mutated
       board.pop()

   That is harmless there because it always operates on a throwaway
   ``node.board()``, but we pass live boards around, so it is fixed here.
2. ``assert piece`` becomes a real guard returning ``False``. An assertion that
   fires mid-review would take down a whole game's analysis over one odd square.

Note the value scale: these are *pawn units* (1/3/3/5/9), matching upstream, and
are intentionally coarser than the centipawn values in :mod:`app.see`. They
answer "is this piece worth more than that one", not "what does this exchange
net".
"""

from __future__ import annotations

import chess
from chess import BISHOP, KING, KNIGHT, PAWN, QUEEN, ROOK, Board, Color, Piece, Square

VALUES: dict[int, int] = {PAWN: 1, KNIGHT: 3, BISHOP: 3, ROOK: 5, QUEEN: 9}

#: Same scale but with the king at 99, so "is the target worth more than the
#: attacker" is automatically true for a check. This is how upstream makes a
#: check count as one prong of a fork.
KING_VALUES: dict[int, int] = {**VALUES, KING: 99}

RAY_PIECE_TYPES = (QUEEN, ROOK, BISHOP)


def value_of(piece_type: int | None) -> int:
    return VALUES.get(piece_type, 0) if piece_type else 0


def material_count(board: Board, side: Color) -> int:
    return sum(len(board.pieces(pt, side)) * v for pt, v in VALUES.items())


def material_diff(board: Board, side: Color) -> int:
    return material_count(board, side) - material_count(board, not side)


def attacked_opponent_squares(
    board: Board, from_square: Square, pov: Color
) -> list[tuple[Piece, Square]]:
    """Enemy pieces the piece on ``from_square`` currently attacks."""
    out: list[tuple[Piece, Square]] = []
    for square in board.attacks(from_square):
        piece = board.piece_at(square)
        if piece and piece.color != pov:
            out.append((piece, square))
    return out


def is_defended(board: Board, piece: Piece, square: Square) -> bool:
    """Does ``piece`` on ``square`` have any defender, including through an x-ray?

    The x-ray clause matters: a piece defended only *through* an enemy slider
    still counts as defended, because capturing removes the blocker.
    """
    if board.attackers(piece.color, square):
        return True
    for attacker_square in board.attackers(not piece.color, square):
        attacker = board.piece_at(attacker_square)
        if attacker and attacker.piece_type in RAY_PIECE_TYPES:
            probe = board.copy(stack=False)
            probe.remove_piece_at(attacker_square)
            if probe.attackers(piece.color, square):
                return True
    return False


def is_hanging(board: Board, piece: Piece, square: Square) -> bool:
    """No defender at all.

    Deliberately naive — this is *not* SEE. "Your bishop on c5 has nothing
    defending it" is the right level of explanation for a beginner, and it is
    true independently of whether the capture happens to win material.
    """
    return not is_defended(board, piece, square)


def can_be_taken_by_lower_piece(board: Board, piece: Piece, square: Square) -> bool:
    for attacker_square in board.attackers(not piece.color, square):
        attacker = board.piece_at(attacker_square)
        if (
            attacker
            and attacker.piece_type != KING
            and value_of(attacker.piece_type) < value_of(piece.piece_type)
        ):
            return True
    return False


def is_in_bad_spot(board: Board, square: Square) -> bool:
    """Attacked, and either undefended or attackable by something cheaper."""
    piece = board.piece_at(square)
    if piece is None:
        return False
    return bool(board.attackers(not piece.color, square)) and (
        is_hanging(board, piece, square)
        or can_be_taken_by_lower_piece(board, piece, square)
    )


def is_trapped(board: Board, square: Square) -> bool:
    """In a bad spot with no square to run to.

    Unlike upstream, this never leaves the board mutated.
    """
    if board.is_check() or board.is_pinned(board.turn, square):
        return False
    piece = board.piece_at(square)
    if piece is None or piece.piece_type in (PAWN, KING):
        return False
    if not is_in_bad_spot(board, square):
        return False

    for escape in board.legal_moves:
        if escape.from_square != square:
            continue
        # Trading itself off for something of equal or greater value is an
        # escape, not a trap.
        capturing = board.piece_at(escape.to_square)
        if capturing and value_of(capturing.piece_type) >= value_of(piece.piece_type):
            return False
        board.push(escape)
        try:
            if not is_in_bad_spot(board, escape.to_square):
                return False
        finally:
            board.pop()
    return True


def loose_pieces(board: Board, color: Color) -> list[tuple[Square, Piece]]:
    """Every piece of ``color`` sitting in a bad spot. Kings excluded."""
    out: list[tuple[Square, Piece]] = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == KING:
            continue
        if is_in_bad_spot(board, square):
            out.append((square, piece))
    return out


def is_pinned_against(board: Board, square: Square) -> Square | None:
    """If the piece on ``square`` is pinned, the square it is pinned against."""
    piece = board.piece_at(square)
    if piece is None:
        return None
    ray = board.pin(piece.color, square)
    if ray == chess.BB_ALL:  # python-chess's "not pinned" sentinel
        return None
    king_square = board.king(piece.color)
    return king_square


def squares_are_collinear(sq1: Square, sq2: Square, sq3: Square) -> bool:
    r1, f1 = chess.square_rank(sq1), chess.square_file(sq1)
    r2, f2 = chess.square_rank(sq2), chess.square_file(sq2)
    r3, f3 = chess.square_rank(sq3), chess.square_file(sq3)
    return (
        r1 == r2 == r3
        or f1 == f2 == f3
        or (r1 - f1) == (r2 - f2) == (r3 - f3)
        or (r1 + f1) == (r2 + f2) == (r3 + f3)
    )


def opponent_can_pass_safely(board: Board) -> bool:
    """Null-move probe: could the side to move afford to do nothing?

    Pushing a null move answers "what is my opponent actually threatening?",
    which is how upstream detects zugzwang and how we detect a concrete threat.
    """
    return board.is_valid() and not board.is_check()
