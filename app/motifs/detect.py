"""Turn "the evaluation dropped" into "here is what actually happened".

Everything in this module is deterministic. Nothing here asks a language model
anything — that is the whole point. Measured on chess commentary, an LLM asked
to judge a position scores ~12.5 move-quality F1; handed precomputed facts it
scores ~97.6. So the facts get computed here, and the model only narrates them.

The central trick: most motifs are **not visible in the move that was played**.
"You allowed a knight fork" is invisible in your own move — the fork appears in
the *opponent's reply*. So detectors run over the engine's refutation line, not
just the position in front of you.

Scope: this catches the concrete, material-losing errors that decide games at
500-800 Elo. It does not detect slow positional errors (bad structure, wrong
plan, a misplaced piece) — those need a different approach entirely, and
pretending otherwise would produce confident nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import chess

from ..see import PIECE_VALUES, see
from .primitives import (
    KING_VALUES,
    attacked_opponent_squares,
    is_hanging,
    is_in_bad_spot,
    is_trapped,
    loose_pieces,
    value_of,
)

PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


class Motif(str, Enum):
    HUNG_PIECE = "hung_piece"
    MISSED_FREE_PIECE = "missed_free_piece"
    MISSED_MATE = "missed_mate"
    ALLOWED_MATE = "allowed_mate"
    ALLOWED_FORK = "allowed_fork"
    ALLOWED_PIN = "allowed_pin"
    ALLOWED_SKEWER = "allowed_skewer"
    ALLOWED_DISCOVERED = "allowed_discovered_attack"
    TRAPPED_PIECE = "trapped_piece"
    BACK_RANK = "back_rank_weakness"
    LOST_CASTLING_SAFETY = "king_left_in_the_centre"


#: Motifs that can stop play and offer the move back.
#:
#: Not every motif qualifies. BACK_RANK and LOST_CASTLING_SAFETY describe where
#: your king *is* rather than a move you just made, so offering a takeback for
#: them would interrupt moves that were perfectly fine.
GUARDABLE: tuple[str, ...] = (
    Motif.HUNG_PIECE.value,
    Motif.MISSED_FREE_PIECE.value,
    Motif.ALLOWED_FORK.value,
    Motif.ALLOWED_PIN.value,
    Motif.ALLOWED_SKEWER.value,
    Motif.ALLOWED_DISCOVERED.value,
    Motif.TRAPPED_PIECE.value,
    Motif.ALLOWED_MATE.value,
    Motif.MISSED_MATE.value,
)


@dataclass
class Finding:
    """One concrete, checkable fact about a move.

    ``phrase`` is written to be true on its own. It is both the fallback text
    when the language model is unavailable and the grounded statement the model
    is asked to expand — never a hint for it to elaborate speculatively.
    """

    motif: Motif
    phrase: str
    squares: list[str] = field(default_factory=list)
    material: int = 0  # centipawns at stake, positive = you lose this much

    def as_fact(self) -> dict:
        return {
            "motif": self.motif.value,
            "statement": self.phrase,
            "squares": self.squares,
            "material_cp": self.material,
        }


def _name(board: chess.Board, square: int) -> str:
    piece = board.piece_at(square)
    return PIECE_NAMES.get(piece.piece_type, "piece") if piece else "piece"


def _sq(square: int) -> str:
    return chess.square_name(square)


# --------------------------------------------------------------------------
# Individual detectors
# --------------------------------------------------------------------------


def detect_fork(board: chess.Board, from_square: int) -> list[int] | None:
    """Targets forked by the piece on ``from_square``, or None.

    Ports lichess-puzzler's rules, which are stricter than "attacks two things":
    the forking piece must itself be safe, pawns don't count as targets, and a
    target only counts if it is worth more than the forker *or* is hanging and
    isn't defending the fork square.
    """
    attacker = board.piece_at(from_square)
    if attacker is None or attacker.piece_type == chess.KING:
        return None
    if is_in_bad_spot(board, from_square):
        return None  # a forking piece you can just take isn't forking anything

    attacker_value = KING_VALUES.get(attacker.piece_type, 0)
    targets: list[int] = []
    for piece, square in attacked_opponent_squares(board, from_square, attacker.color):
        if piece.piece_type == chess.PAWN:
            continue
        worth_more = KING_VALUES.get(piece.piece_type, 0) > attacker_value
        undefended = is_hanging(board, piece, square) and square not in board.attackers(
            piece.color, from_square
        )
        if worth_more or undefended:
            targets.append(square)

    return targets if len(targets) > 1 else None


def detect_pin(board: chess.Board, victim_color: bool) -> list[tuple[int, int]]:
    """Pinned pieces of ``victim_color`` that are also under attack."""
    found: list[tuple[int, int]] = []
    king_square = board.king(victim_color)
    if king_square is None:
        return found
    for square, piece in board.piece_map().items():
        if piece.color != victim_color or piece.piece_type == chess.KING:
            continue
        if board.pin(victim_color, square) == chess.BB_ALL:
            continue
        if board.attackers(not victim_color, square):
            found.append((square, king_square))
    return found


def detect_skewer(board: chess.Board, before: chess.Board, move: chess.Move) -> bool:
    """A slider move that lines up two enemy pieces, the front one worth more."""
    piece = board.piece_at(move.to_square)
    if piece is None or piece.piece_type not in (chess.QUEEN, chess.ROOK, chess.BISHOP):
        return False
    victim_color = not piece.color
    attacked = [
        (p, sq) for p, sq in attacked_opponent_squares(board, move.to_square, piece.color)
    ]
    for front_piece, front_sq in attacked:
        ray = chess.SquareSet.ray(move.to_square, front_sq)
        if not ray:
            continue
        for behind_sq in ray:
            if behind_sq in (move.to_square, front_sq):
                continue
            behind = board.piece_at(behind_sq)
            if behind is None or behind.color != victim_color:
                continue
            between = chess.SquareSet.between(front_sq, behind_sq)
            if any(board.piece_at(s) for s in between):
                continue
            if value_of(front_piece.piece_type) > value_of(behind.piece_type):
                return True
    return False


def detect_discovered_check(board: chess.Board, move: chess.Move) -> bool:
    """The checking piece is not the piece that just moved."""
    checkers = board.checkers()
    return bool(checkers) and move.to_square not in checkers


def _has_major_piece(board: chess.Board, color: bool) -> bool:
    return bool(board.pieces(chess.ROOK, color) or board.pieces(chess.QUEEN, color))


def _can_reach_back_rank(board: chess.Board, attacker: bool, back_rank: int) -> bool:
    """Can a rook or queen of ``attacker`` actually get to that back rank?

    Owning a rook somewhere is not a back-rank threat. Every castled king in
    chess sits behind three unmoved pawns with no escape squares, so a detector
    that asks only "is the king boxed in?" fires in essentially every game and
    drowns out the real mistakes. Requiring a major piece that can *land* on the
    rank turns it back into a fact worth reporting.
    """
    squares = chess.SquareSet(chess.BB_RANK_1 if back_rank == 0 else chess.BB_RANK_8)

    # Already bearing down on it.
    for square in squares:
        for attacker_square in board.attackers(attacker, square):
            if board.piece_type_at(attacker_square) in (chess.ROOK, chess.QUEEN):
                return True

    # Or one legal move away from landing on it.
    probe = board.copy()
    if probe.turn != attacker:
        if probe.is_check():
            return False  # cannot hand the move over while in check
        probe.push(chess.Move.null())
    for move in probe.legal_moves:
        if (
            probe.piece_type_at(move.from_square) in (chess.ROOK, chess.QUEEN)
            and move.to_square in squares
        ):
            return True
    return False


def detect_back_rank(board: chess.Board, victim_color: bool) -> bool:
    """King stuck on its back rank with no escape squares.

    Requires the opponent to still own a rook or queen: with only minor pieces
    left there is no back-rank threat to warn about, and a warning that never
    cashes out teaches the player to ignore warnings.
    """
    king_square = board.king(victim_color)
    if king_square is None:
        return False
    if not _has_major_piece(board, not victim_color):
        return False
    back_rank = 0 if victim_color == chess.WHITE else 7
    if chess.square_rank(king_square) != back_rank:
        return False
    if not _can_reach_back_rank(board, not victim_color, back_rank):
        return False
    # A king still on d/e has not castled: that is "stuck in the centre", a
    # different lesson with a different fix, reported separately below.
    if chess.square_file(king_square) in (3, 4):
        return False

    forward = 8 if victim_color == chess.WHITE else -8
    escapes = [king_square + forward]
    file_index = chess.square_file(king_square)
    if file_index > 0:
        escapes.append(king_square + forward - 1)
    if file_index < 7:
        escapes.append(king_square + forward + 1)

    for square in escapes:
        if not 0 <= square <= 63:
            continue
        occupant = board.piece_at(square)
        blocked = occupant is not None and occupant.color == victim_color
        covered = bool(board.attackers(not victim_color, square))
        if not blocked and not covered:
            return False  # the king has air
    return True


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


def facts_for_move(
    before: chess.Board,
    move: chess.Move,
    refutation: Sequence[chess.Move] = (),
    best_move: chess.Move | None = None,
    had_mate: bool = False,
    faces_mate: bool = False,
    already_lost: bool = False,
) -> list[Finding]:
    """Everything concrete we can say about one move.

    ``before`` is the position the mover faced, ``move`` is what they played,
    and ``refutation`` is the engine's best line *after* that move — which is
    where "you allowed X" motifs actually become visible.
    """
    mover = before.turn
    after = before.copy()
    after.push(move)

    findings: list[Finding] = []
    findings += _missed_material(before, move, mover)
    if had_mate:
        findings.append(
            Finding(
                motif=Motif.MISSED_MATE,
                phrase=(
                    "There was a forced mate here"
                    + (f", starting with {before.san(best_move)}." if best_move else ".")
                ),
                squares=[_sq(best_move.to_square)] if best_move else [],
            )
        )
    findings += _self_inflicted(before, move, after, mover)
    findings += _allowed_by_refutation(after, refutation, mover)
    if faces_mate and not already_lost:
        # Suppressed when the position was already resignable: "this allows
        # mate" is technically true of almost any move once you are down a
        # queen, and saying so buries the move that actually lost the game.
        findings.append(
            Finding(
                motif=Motif.ALLOWED_MATE,
                phrase="This allows a forced checkmate.",
            )
        )

    # Strongest claim first, so the coach leads with the thing that matters.
    findings.sort(key=lambda f: f.material, reverse=True)
    return findings


def _missed_material(before: chess.Board, move: chess.Move, mover: bool) -> list[Finding]:
    """Free enemy material that was sitting there before the move.

    This is the "Awareness" signal — your opponent hung something and you did
    not take it. At beginner level it is probably the most actionable single
    line a review can print.
    """
    best: tuple[int, chess.Move] | None = None
    for candidate in before.legal_moves:
        if not before.is_capture(candidate):
            continue
        gain = see(before, candidate)
        if gain > 0 and (best is None or gain > best[0]):
            best = (gain, candidate)
    if best is None or best[0] < PIECE_VALUES[chess.KNIGHT]:
        return []  # winning a pawn is not the lesson at this level

    # Taking *something* is not the same as taking the best thing: grabbing a
    # pawn while a rook hangs is still a miss.
    played = see(before, move) if before.is_capture(move) else 0
    if played >= best[0]:
        return []

    gain, capture = best
    target = _name(before, capture.to_square)
    return [
        Finding(
            motif=Motif.MISSED_FREE_PIECE,
            phrase=(
                f"Your opponent's {target} on {_sq(capture.to_square)} was free — "
                f"{before.san(capture)} wins it."
            ),
            squares=[_sq(capture.to_square)],
            material=gain,
        )
    ]


def _self_inflicted(
    before: chess.Board, move: chess.Move, after: chess.Board, mover: bool
) -> list[Finding]:
    """Material this move put at risk."""
    findings: list[Finding] = []

    exchange = see(before, move)
    if exchange < -PIECE_VALUES[chess.PAWN]:
        moved = _name(after, move.to_square)
        findings.append(
            Finding(
                motif=Motif.HUNG_PIECE,
                phrase=(
                    f"{before.san(move)} leaves your {moved} on {_sq(move.to_square)} "
                    f"where it can simply be taken, and you do not get enough back "
                    f"for it."
                ),
                squares=[_sq(move.to_square)],
                material=-exchange,
            )
        )

    # Something *else* of yours was left loose by moving away from it.
    for square, piece in loose_pieces(after, mover):
        if square == move.to_square:
            continue  # already reported above
        capture = _best_enemy_capture(after, square)
        if capture is None or capture < PIECE_VALUES[chess.KNIGHT]:
            continue
        findings.append(
            Finding(
                motif=Motif.HUNG_PIECE,
                phrase=(
                    f"Your {PIECE_NAMES[piece.piece_type]} on {_sq(square)} is left "
                    f"undefended and can be taken."
                ),
                squares=[_sq(square)],
                material=capture,
            )
        )

    # is_trapped() scans board.legal_moves for escape squares, so it only means
    # anything when the *owner* of the piece is to move. `after` has the
    # opponent to move, so flip the turn with a null move first.
    probe = after.copy()
    if not probe.is_check():
        probe.push(chess.Move.null())
    for square, piece in probe.piece_map().items():
        if piece.color != mover or piece.piece_type in (chess.PAWN, chess.KING):
            continue
        if probe.turn == mover and is_trapped(probe, square):
            findings.append(
                Finding(
                    motif=Motif.TRAPPED_PIECE,
                    phrase=(
                        f"Your {PIECE_NAMES[piece.piece_type]} on {_sq(square)} is "
                        f"trapped — it is attacked and has nowhere safe to go."
                    ),
                    squares=[_sq(square)],
                    material=PIECE_VALUES[piece.piece_type],
                )
            )
    return findings


def _best_enemy_capture(board: chess.Board, square: int) -> int | None:
    """Best SEE the side to move can get by capturing on ``square``."""
    best: int | None = None
    for move in board.legal_moves:
        if move.to_square != square:
            continue
        gain = see(board, move)
        if best is None or gain > best:
            best = gain
    return best if best and best > 0 else None


def _allowed_by_refutation(
    after: chess.Board, refutation: Sequence[chess.Move], mover: bool
) -> list[Finding]:
    """Motifs that only become visible in the opponent's reply.

    This is the part most tools miss. "You allowed a fork" cannot be seen in
    your own move; it is a property of what your opponent gets to play next.
    """
    if not refutation:
        return []

    reply = refutation[0]
    if reply not in after.legal_moves:
        return []  # stale PV from the engine

    board = after.copy()
    board.push(reply)
    findings: list[Finding] = []

    forked = detect_fork(board, reply.to_square)
    if forked:
        names = " and ".join(f"{_name(board, s)} on {_sq(s)}" for s in forked[:3])
        findings.append(
            Finding(
                motif=Motif.ALLOWED_FORK,
                phrase=(
                    f"This runs into {after.san(reply)}, forking your {names}."
                ),
                squares=[_sq(reply.to_square)] + [_sq(s) for s in forked],
                # Report the material actually at stake. The king carries a
                # sentinel value of 10000 so that a check counts as a fork
                # prong, but "you lose 100 pawns" would be nonsense to hand the
                # coach, so exclude it here.
                material=max(
                    (
                        PIECE_VALUES.get(board.piece_type_at(s), 0)
                        for s in forked
                        if board.piece_type_at(s) != chess.KING
                    ),
                    default=0,
                ),
            )
        )

    for pinned_square, king_square in detect_pin(board, mover):
        piece = board.piece_at(pinned_square)
        if piece is None:
            continue
        findings.append(
            Finding(
                motif=Motif.ALLOWED_PIN,
                phrase=(
                    f"After {after.san(reply)} your {PIECE_NAMES[piece.piece_type]} on "
                    f"{_sq(pinned_square)} is pinned against your king on "
                    f"{_sq(king_square)} and cannot move away."
                ),
                squares=[_sq(pinned_square), _sq(king_square)],
                material=PIECE_VALUES[piece.piece_type] // 2,
            )
        )

    if detect_skewer(board, after, reply):
        findings.append(
            Finding(
                motif=Motif.ALLOWED_SKEWER,
                phrase=(
                    f"{after.san(reply)} skewers two of your pieces on the same line — "
                    f"when the front one moves, the one behind it drops."
                ),
                squares=[_sq(reply.to_square)],
            )
        )

    if detect_discovered_check(board, reply):
        findings.append(
            Finding(
                motif=Motif.ALLOWED_DISCOVERED,
                phrase=(
                    f"{after.san(reply)} uncovers a discovered check from a piece "
                    f"behind it."
                ),
                squares=[_sq(reply.to_square)],
            )
        )

    king_square = board.king(mover)
    if detect_back_rank(board, mover):
        findings.append(
            Finding(
                motif=Motif.BACK_RANK,
                phrase=(
                    "Your king has no escape squares on the back rank — it is boxed "
                    "in by its own pawns."
                ),
                squares=[_sq(king_square)] if king_square else [],
            )
        )
    elif (
        king_square is not None
        and chess.square_file(king_square) in (3, 4)
        and chess.square_rank(king_square) == (0 if mover == chess.WHITE else 7)
        and _has_major_piece(board, not mover)
        and board.is_check()
    ):
        findings.append(
            Finding(
                motif=Motif.LOST_CASTLING_SAFETY,
                phrase=(
                    f"Your king is still in the centre on {_sq(king_square)} and is "
                    f"being checked along an open file. Castling early avoids this."
                ),
                squares=[_sq(king_square)],
            )
        )

    return findings
