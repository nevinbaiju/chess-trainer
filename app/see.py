"""Static Exchange Evaluation.

python-chess has no SEE, and we need one: "is this piece actually hanging?" is
the single most common question in a 600-Elo game review, and naive
attacker-vs-defender counting gets it wrong constantly (it can't tell a defended
knight from a knight defended by a queen and attacked by a pawn).

Standard recursive formulation. Pushing each capture onto the board makes
x-ray attackers appear on the next ``attackers()`` call for free, so batteries
(rook behind rook, queen behind bishop) resolve correctly without special code.

Known approximation, shared with virtually every engine's SEE: absolute pins are
ignored, so a defender that is pinned still counts as a defender. Detecting that
is the pin motif's job, not SEE's.
"""

from __future__ import annotations

import chess

#: Exchange values in centipawns. Deliberately blunt — SEE compares material,
#: not position, and fine-grained piece-square values would be noise here.
PIECE_VALUES: dict[int, int] = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 10000,
}

_PROMOTION_BONUS = PIECE_VALUES[chess.QUEEN] - PIECE_VALUES[chess.PAWN]


def piece_value(piece_type: int | None) -> int:
    return PIECE_VALUES.get(piece_type, 0) if piece_type else 0


def _least_valuable_attacker(board: chess.Board, square: int, color: bool) -> int | None:
    """Square of ``color``'s cheapest attacker of ``square``, or None."""
    best_sq, best_val = None, None
    for sq in board.attackers(color, square):
        val = piece_value(board.piece_type_at(sq))
        if best_val is None or val < best_val:
            best_sq, best_val = sq, val
    return best_sq


def _is_promoting(board: chess.Board, from_square: int, to_square: int) -> bool:
    if board.piece_type_at(from_square) != chess.PAWN:
        return False
    return chess.square_rank(to_square) in (0, 7)


def _see_recursive(board: chess.Board, square: int, color: bool) -> int:
    """Value ``color`` gains by continuing the exchange on ``square``."""
    attacker_sq = _least_valuable_attacker(board, square, color)
    if attacker_sq is None:
        return 0

    attacker_type = board.piece_type_at(attacker_sq)
    captured_value = piece_value(board.piece_type_at(square))

    # The king may only capture if the square is genuinely undefended,
    # otherwise the "exchange" is simply illegal.
    if attacker_type == chess.KING and board.attackers(not color, square):
        return 0

    promoting = _is_promoting(board, attacker_sq, square)
    move = chess.Move(
        attacker_sq, square, promotion=chess.QUEEN if promoting else None
    )

    board.push(move)
    try:
        gain = captured_value + (_PROMOTION_BONUS if promoting else 0)
        # Standing pat: we are never obliged to recapture, so a losing
        # continuation is simply declined.
        value = max(0, gain - _see_recursive(board, square, not color))
    finally:
        board.pop()
    return value


def see(board: chess.Board, move: chess.Move) -> int:
    """Net centipawn outcome of playing ``move``, assuming best play by both.

    Positive means the capture wins material. Non-captures evaluate to whatever
    the opponent can win on the destination square (usually 0, or negative if
    you just moved a piece somewhere it can be taken for free).
    """
    to_sq = move.to_square
    mover = board.turn

    if board.is_en_passant(move):
        captured_value = PIECE_VALUES[chess.PAWN]
    else:
        captured_value = piece_value(board.piece_type_at(to_sq))

    promoting = move.promotion is not None or _is_promoting(
        board, move.from_square, to_sq
    )

    board.push(move)
    try:
        gain = captured_value + (_PROMOTION_BONUS if promoting else 0)
        return gain - _see_recursive(board, to_sq, not mover)
    finally:
        board.pop()


def hangs_after(board: chess.Board, move: chess.Move) -> int:
    """How much material ``move`` leaves loose, as a positive number.

    Convenience wrapper for the most common review question: "did that move
    just drop something?" Returns 0 if nothing is loose.
    """
    return max(0, -see(board, move))


def best_capture_on(board: chess.Board, square: int) -> tuple[chess.Move, int] | None:
    """The side to move's most profitable capture of ``square``, if any wins material."""
    best: tuple[chess.Move, int] | None = None
    for move in board.legal_moves:
        if move.to_square != square:
            continue
        value = see(board, move)
        if best is None or value > best[1]:
            best = (move, value)
    return best if best and best[1] > 0 else None
