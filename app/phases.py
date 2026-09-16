"""Opening / middlegame / endgame division.

A direct port of scalachess ``Divider.scala``. Purely structural — no engine
call — which is why per-phase stats are cheap enough to compute on every game.

The rules, in lichess's own terms:

* **middlegame** begins at the first position where *any* of
  - at most 10 non-king, non-pawn pieces remain, or
  - a back rank holds fewer than 4 pieces (i.e. you've developed), or
  - the "mixedness" score exceeds 150 (the armies have interlocked);
* **endgame** begins at the first position with at most 6 such pieces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import chess

# 49 overlapping 2x2 regions covering a1..g7. 0x0303 is the 2x2 block at a1/b1/a2/b2.
_SMALL_SQUARE = 0x0303
_MIXEDNESS_REGIONS: tuple[int, ...] = tuple(
    _SMALL_SQUARE << (x + 8 * y) for y in range(7) for x in range(7)
)


@dataclass(frozen=True)
class Division:
    """Ply indices at which each phase starts. ``None`` means never reached."""

    middle: int | None
    end: int | None
    plies: int

    def phase_at(self, ply: int) -> str:
        if self.end is not None and ply >= self.end:
            return "endgame"
        if self.middle is not None and ply >= self.middle:
            return "middlegame"
        return "opening"


def _majors_and_minors(board: chess.Board) -> int:
    return chess.popcount(board.occupied & ~(board.kings | board.pawns))


def _backrank_sparse(board: chess.Board) -> bool:
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    return (
        chess.popcount(chess.BB_RANK_1 & white) < 4
        or chess.popcount(chess.BB_RANK_8 & black) < 4
    )


def _score(y: int, white: int, black: int) -> int:
    """Hand-tuned interlock score for one 2x2 region. Verbatim from Divider.scala."""
    if white == 0:
        if black == 1:
            return 1 + y
        if black == 2:
            return 2 + (6 - y) if y < 6 else 0
        if black == 3:
            return 3 + (7 - y) if y < 7 else 0
        if black == 4:
            return 3 + (7 - y) if y < 7 else 0
        return 0
    if white == 1:
        if black == 0:
            return 1 + (8 - y)
        if black == 1:
            return 5 + abs(4 - y)
        if black == 2:
            return 4 + (7 - y)
        if black == 3:
            return 5 + (7 - y)
        return 0
    if white == 2:
        if black == 0:
            return 2 + (y - 2) if y > 2 else 0
        if black == 1:
            return 4 + (y - 1)
        if black == 2:
            return 7
        return 0
    if white == 3:
        if black == 0:
            return 3 + (y - 1) if y > 1 else 0
        if black == 1:
            return 5 + (y - 1)
        return 0
    if white == 4:
        # A group of 4 on its own home row means nothing has happened yet.
        return 3 + (y - 1) if y > 1 and black == 0 else 0
    return 0


def _mixedness(board: chess.Board) -> int:
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    acc = 0
    for i, region in enumerate(_MIXEDNESS_REGIONS):
        y = i // 7 + 1
        acc += _score(y, chess.popcount(white & region), chess.popcount(black & region))
    return acc


def divide(boards: Sequence[chess.Board]) -> Division:
    """Divide a game, given every position in order (including the start)."""
    middle = None
    for index, board in enumerate(boards):
        if (
            _majors_and_minors(board) <= 10
            or _backrank_sparse(board)
            or _mixedness(board) > 150
        ):
            middle = index
            break

    end = None
    if middle is not None:
        for index, board in enumerate(boards):
            if _majors_and_minors(board) <= 6:
                end = index
                break

    # lila drops the middlegame marker if the endgame somehow starts first.
    if middle is not None and end is not None and middle >= end:
        middle = None

    return Division(middle=middle, end=end, plies=len(boards))


def divide_game(board_start: chess.Board, moves: Sequence[chess.Move]) -> Division:
    """Convenience wrapper: replay ``moves`` and divide the resulting game."""
    board = board_start.copy()
    boards = [board.copy(stack=False)]
    for move in moves:
        board.push(move)
        boards.append(board.copy(stack=False))
    return divide(boards)
