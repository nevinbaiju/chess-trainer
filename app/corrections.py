"""Replaying your own blunders.

The puzzle tab drills patterns you are statistically bad at. This drills the
actual positions you lost from — the same board, the same move number, the same
choice — and asks you to make it again.

Two design decisions worth stating, because both could reasonably have gone the
other way:

**A correction is judged live, not against a stored best move.** At this level
several moves are usually fine, and failing someone for playing a good move
that happens not to be Stockfish's first choice teaches them nothing except
that the app is arbitrary. So the attempt is analysed and passes if it gives
away less than :data:`FORGIVEN` win% — the same threshold the review uses for
"not even an inaccuracy". The engine's own pick is shown afterwards regardless.

**Only blunders, ranked by what they cost.** A queue of every inaccuracy is a
queue nobody works through. The worst mistake in the worst game is the one
worth replaying first.
"""

from __future__ import annotations

from dataclasses import dataclass

import chess

#: Win% a correction may give away and still count. Matches the review's
#: inaccuracy threshold: below this, nothing was thrown away.
FORGIVEN = 5.0


@dataclass(frozen=True)
class Blunder:
    """One position worth replaying, pulled out of a finished review."""

    game_id: int
    ply: int
    fen: str                  # the position you faced
    played_san: str           # what you actually did
    played_uci: str
    best_uci: str | None      # what the engine wanted, at review depth
    best_san: str | None
    win_lost: float           # win% thrown away
    color: bool               # the side you had
    motifs: tuple[str, ...]   # what the detectors said about the move
    opening: str | None
    played_at: str | None

    @property
    def key(self) -> str:
        return f"{self.game_id}:{self.ply}"


def position_before(moves: list[str], ply: int) -> chess.Board:
    """The board as it stood when the move was chosen."""
    board = chess.Board()
    for uci in moves[:ply]:
        board.push(chess.Move.from_uci(uci))
    return board


def blunders_in(game_row, review: dict) -> list[Blunder]:
    """Every blunder the player themselves made in one reviewed game.

    Only the player's own moves: being shown the opponent's blunder and asked
    to improve it is a different exercise, and not one they asked for.
    """
    hero_white = game_row["player_color"] == "white"
    moves = (game_row["moves"] or "").split()
    out: list[Blunder] = []

    for move in review.get("moves", []):
        if move.get("judgment") != "blunder":
            continue
        if (move.get("color") == "white") != hero_white:
            continue
        ply = move["ply"]
        if ply >= len(moves):
            continue

        board = position_before(moves, ply)
        best_uci = move.get("best_move_uci")
        out.append(Blunder(
            game_id=game_row["id"],
            ply=ply,
            fen=board.fen(),
            played_san=move.get("san", ""),
            played_uci=moves[ply],
            best_uci=best_uci,
            best_san=move.get("best_move"),
            win_lost=float(move.get("win_lost") or 0.0),
            color=chess.WHITE if hero_white else chess.BLACK,
            motifs=tuple(f["motif"] for f in move.get("findings", [])),
            opening=game_row["opening_name"],
            played_at=game_row["created_at"],
        ))
    return out


def rank(blunders: list[Blunder]) -> list[Blunder]:
    """Worst first. The mistake that cost the most is the one to replay."""
    return sorted(blunders, key=lambda b: (-b.win_lost, b.game_id, b.ply))


def verdict(win_best: float, win_played: float) -> dict:
    """Did the replacement move hold?

    Measured against the best available move rather than against the original
    blunder, because "better than the catastrophe" is a low bar that would pass
    almost anything.
    """
    lost = max(0.0, win_best - win_played)
    return {
        "held": lost < FORGIVEN,
        "lost": round(lost, 1),
        "win_played": round(win_played, 1),
        "win_best": round(win_best, 1),
    }
