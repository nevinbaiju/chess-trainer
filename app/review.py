"""The review pipeline: Stockfish says what is true, Maia says what is human.

Chess.com's Game Review can tell you a move was bad. It cannot tell you whether
it was a *typical* bad move, because it only has an engine. We have both models,
and the interesting signal is the gap between them:

* Stockfish: this move lost 2.1 pawns.
* Maia at your rating: 38% of players here play it.
  -> a systematic trap. Worth a real lesson, because fixing it transfers.
* Maia at your rating: 3% play it.
  -> a one-off slip. Mention it and move on.
* Maia: only 2% of players at your level find the engine's move.
  -> do NOT say "you should have played Qh5". Teach the pattern instead.

That last rule is the difference between a tool that lectures you with engine
lines and one that teaches. It is implemented in :attr:`MoveReview.best_is_findable`
and enforced when the coach prompt is built.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import chess

from .engines import Line, Stockfish
from .eval import (
    CP_INITIAL,
    Eval,
    Judgment,
    accuracy_pct,
    acpl,
    classify,
    eval_win_percent,
    game_accuracy,
    win_percent,
)
from .motifs import Finding, facts_for_move
from .phases import Division, divide

log = logging.getLogger(__name__)

#: Below this, the engine's best move is not a realistic thing to ask of the
#: player, so the coach teaches the idea rather than the move.
FINDABLE_THRESHOLD = 0.05

#: A mistake this common at the player's rating is a pattern, not an accident.
SYSTEMATIC_THRESHOLD = 0.20

#: Win% a move must actually cost before it is worth calling a lesson. In an
#: already-lost position every move "loses" ~0, and those are not teachable.
MIN_LESSON_COST = 3.0


@dataclass
class MoveReview:
    ply: int
    san: str
    uci: str
    white_to_move: bool
    phase: str

    eval_before: Eval
    eval_after: Eval
    win_before: float          # mover's point of view, 0-100
    win_after: float
    accuracy: float
    judgment: Judgment | None

    best_move_san: str | None
    best_pv_san: list[str]
    refutation_san: list[str]
    findings: list[Finding] = field(default_factory=list)

    # UCI duplicates of the above. The browser has no chess library — it cannot
    # turn "Nf3" into a pair of squares — so anything it needs to draw as an
    # arrow has to arrive already in from/to form.
    best_move_uci: str | None = None
    refutation_uci: list[str] = field(default_factory=list)

    #: Win% from WHITE's point of view after this move. `win_before`/`win_after`
    #: are mover-relative and so flip perspective every ply, which makes them
    #: useless as a single timeline series.
    win_white_after: float = 50.0

    # Maia layer — all optional so a review still works without it.
    human_probability: float | None = None
    human_rank: int | None = None
    best_human_probability: float | None = None
    human_win_before: float | None = None

    @property
    def win_lost(self) -> float:
        return max(0.0, self.win_before - self.win_after)

    @property
    def is_error(self) -> bool:
        return self.judgment is not None

    @property
    def best_is_findable(self) -> bool:
        """Would a player at this rating plausibly find the engine's move?"""
        if self.best_human_probability is None:
            return True  # no Maia signal; don't suppress anything
        return self.best_human_probability >= FINDABLE_THRESHOLD

    @property
    def is_systematic(self) -> bool:
        """Is this a mistake the player's peers make too?"""
        return (self.human_probability or 0.0) >= SYSTEMATIC_THRESHOLD

    @property
    def teaching_priority(self) -> float:
        """How much is fixing this actually worth?

        Severity alone ranks one freak catastrophe above a habit that costs
        half a pawn every single game. Weighting by how often players at this
        rating repeat the mistake surfaces the habits, which are the things
        worth practising.
        """
        if not self.is_error:
            return 0.0
        commonality = self.human_probability if self.human_probability is not None else 0.10
        return self.win_lost * (0.2 + commonality)

    def to_dict(self) -> dict:
        return {
            "ply": self.ply,
            "san": self.san,
            "uci": self.uci,
            "color": "white" if self.white_to_move else "black",
            "phase": self.phase,
            "win_before": round(self.win_before, 1),
            "win_after": round(self.win_after, 1),
            "win_white": round(self.win_white_after, 1),
            "win_lost": round(self.win_lost, 1),
            "accuracy": round(self.accuracy, 1),
            "judgment": self.judgment.value if self.judgment else None,
            "best_move": self.best_move_san,
            "best_move_uci": self.best_move_uci,
            "best_line": self.best_pv_san,
            "refutation": self.refutation_san,
            "refutation_uci": self.refutation_uci,
            "findings": [f.as_fact() for f in self.findings],
            "human": {
                "played_pct": round((self.human_probability or 0) * 100, 1),
                "played_rank": self.human_rank,
                "best_pct": round((self.best_human_probability or 0) * 100, 1),
                "best_findable": self.best_is_findable,
                "systematic": self.is_systematic,
            },
            "teaching_priority": round(self.teaching_priority, 2),
        }


@dataclass
class GameReview:
    moves: list[MoveReview]
    division: Division
    accuracy_white: float | None
    accuracy_black: float | None
    acpl_white: float | None
    acpl_black: float | None
    hero: bool | None = None  # which colour the user played, if known

    def errors(self, color: bool | None = None) -> list[MoveReview]:
        color = self.hero if color is None else color
        return [
            m
            for m in self.moves
            if m.is_error and (color is None or m.white_to_move == color)
        ]

    def lessons(self, limit: int = 3, color: bool | None = None) -> list[MoveReview]:
        """The handful of moments actually worth showing the player.

        Filtered, not just ranked. Once a game is already lost every subsequent
        slip scores near-zero win% loss, and padding the list out to ``limit``
        surfaces those as "lessons" — which teaches the player to distrust the
        list. Three real mistakes beats three real ones plus two fillers.
        """
        ranked = sorted(
            self.errors(color), key=lambda m: m.teaching_priority, reverse=True
        )
        worthwhile = [m for m in ranked if m.win_lost >= MIN_LESSON_COST]
        return worthwhile[:limit]

    def to_dict(self) -> dict:
        return {
            "accuracy": {"white": self.accuracy_white, "black": self.accuracy_black},
            "acpl": {"white": self.acpl_white, "black": self.acpl_black},
            "phases": {
                "middlegame_ply": self.division.middle,
                "endgame_ply": self.division.end,
            },
            "moves": [m.to_dict() for m in self.moves],
            "lessons": [m.ply for m in self.lessons()],
        }


async def review_game(
    moves: list[chess.Move],
    stockfish: Stockfish,
    maia=None,
    player_elo: int = 1000,
    hero: bool | None = None,
    depth: int | None = None,
    root: chess.Board | None = None,
    progress=None,
) -> GameReview:
    """Analyse a whole game move by move.

    One engine call per *position*, not per move: the position after move n is
    the position before move n+1, and the analysis of position n+1 doubles as
    the refutation of move n. That halves the engine work versus the obvious
    before/after-per-move loop.
    """
    board = (root or chess.Board()).copy()
    positions: list[chess.Board] = [board.copy()]
    for move in moves:
        board.push(move)
        positions.append(board.copy())

    # MultiPV 2 so we can tell "only move" situations from positions with
    # several equally good options later on.
    lines: list[list[Line]] = []
    for index, position in enumerate(positions):
        lines.append(await stockfish.analyse(position, depth=depth, multipv=2))
        if progress:
            progress(index + 1, len(positions))

    evals: list[Eval] = []
    for position, position_lines in zip(positions, lines):
        if position.is_checkmate():
            evals.append(Eval(mate=-1 if position.turn == chess.WHITE else 1))
        elif position.is_game_over():
            evals.append(Eval(cp=0))
        elif position_lines:
            evals.append(position_lines[0].score)
        else:
            evals.append(Eval(cp=0))
    evals[0] = evals[0] if moves else Eval(cp=CP_INITIAL)

    division = divide(positions)
    reviews: list[MoveReview] = []

    for ply, move in enumerate(moves):
        before, after = positions[ply], positions[ply + 1]
        mover = before.turn
        ev_before, ev_after = evals[ply], evals[ply + 1]

        pov_before = ev_before.pov(mover)
        pov_after = ev_after.pov(mover)
        win_before = eval_win_percent(pov_before)
        win_after = eval_win_percent(pov_after)

        best_line = lines[ply][0] if lines[ply] else None
        best_move = best_line.best_move if best_line else None
        refutation_line = lines[ply + 1][0] if lines[ply + 1] else None
        refutation = list(refutation_line.pv) if refutation_line else []

        findings = facts_for_move(
            before,
            move,
            refutation=refutation,
            best_move=best_move,
            had_mate=pov_before.is_mate and pov_before.mate > 0,
            faces_mate=pov_after.is_mate and pov_after.mate < 0,
            already_lost=(pov_before.cp is not None and pov_before.cp < -900)
            or (pov_before.is_mate and pov_before.mate < 0),
        )

        reviews.append(
            MoveReview(
                ply=ply,
                san=before.san(move),
                uci=move.uci(),
                white_to_move=(mover == chess.WHITE),
                phase=division.phase_at(ply),
                eval_before=ev_before,
                eval_after=ev_after,
                win_before=win_before,
                win_after=win_after,
                accuracy=accuracy_pct(win_before, win_after),
                judgment=classify(ev_before, ev_after, mover == chess.WHITE),
                best_move_san=before.san(best_move) if best_move else None,
                best_move_uci=best_move.uci() if best_move else None,
                best_pv_san=best_line.pv_san(before) if best_line else [],
                refutation_san=refutation_line.pv_san(after) if refutation_line else [],
                refutation_uci=[m.uci() for m in refutation[:2]],
                win_white_after=eval_win_percent(ev_after),
                findings=findings,
            )
        )

    if maia is not None:
        await _attach_human_read(reviews, positions, maia, player_elo, hero)

    white_wp = [win_percent(CP_INITIAL)] + [eval_win_percent(e) for e in evals[1:]]
    return GameReview(
        moves=reviews,
        division=division,
        accuracy_white=game_accuracy(white_wp, white=True),
        accuracy_black=game_accuracy(white_wp, white=False),
        acpl_white=acpl(white_wp, evals[1:], white=True),
        acpl_black=acpl(white_wp, evals[1:], white=False),
        hero=hero,
    )


async def _attach_human_read(
    reviews: list[MoveReview],
    positions: list[chess.Board],
    maia,
    player_elo: int,
    hero: bool | None,
) -> None:
    """Ask Maia how a player at ``player_elo`` sees each position.

    Only the hero's moves are read: Maia costs ~15ms per position, so reading
    both sides would double the cost for a signal we never show.
    """
    for review in reviews:
        if hero is not None and review.white_to_move != hero:
            continue
        board = positions[review.ply]
        try:
            read = await maia.read(board, player_elo)
        except Exception:  # noqa: BLE001 - Maia is an enhancement, never a hard dep
            log.warning("maia read failed at ply %d", review.ply, exc_info=True)
            continue

        move = chess.Move.from_uci(review.uci)
        review.human_probability = read.probability_of(move)
        review.human_rank = read.rank_of(move)
        review.human_win_before = read.win
        if review.best_move_san:
            try:
                best = board.parse_san(review.best_move_san)
                review.best_human_probability = read.probability_of(best)
            except ValueError:
                pass
