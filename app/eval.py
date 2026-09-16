"""Evaluation, move classification and accuracy — calibrated for a beginner.

Faithful ports of lichess's formulas, with ONE deliberate divergence: the win%
curve constant. Everything here is stdlib-only so it can be unit-tested without
an engine or a board.

The divergence, and why it exists
---------------------------------
lichess ships ``k = -0.00368208``, fitted in lila PR #11148 on ~75k positions
drawn from games rated **2300+**. The PR author wrote:

    "This value will be closer and closer to -0.002 as the ELO decreases."

At 600 Elo a +3.00 position is nowhere near 95% winning — beginners throw won
games constantly, and they also save lost ones. Using the master-fitted curve on
a beginner's games mislabels the whole review: it screams "blunder" at drops that
barely matter at this level, and shrugs at the quiet ones that actually decide
the game.

So ``WIN_PCT_K`` defaults to the beginner value. Set ``CHESS_WIN_PCT_K`` in the
environment to override it (``-0.00368208`` reproduces lichess exactly, which is
what ``tests/test_eval.py`` pins).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Logistic steepness for centipawns -> winning chances.
#: lichess (2300+ fit): -0.00368208.  Beginner (~600) fit: -0.002.
WIN_PCT_K: float = float(os.environ.get("CHESS_WIN_PCT_K", "-0.002"))

#: lichess clamps evaluations to +/- 1000cp before converting. Beyond this the
#: curve is flat anyway and huge evals would otherwise dominate the variance
#: weighting in game_accuracy().
CP_CEILING = 1000

#: lichess treats the starting position as +15cp for White, not 0.0.
CP_INITIAL = 15

# Judgement thresholds, in WIN% POINTS LOST on the 0-100 scale.
#
# TRAP: lichess's published values (0.30 / 0.20 / 0.10) live on the [-1, +1]
# winningChances scale, NOT on win% [0, 100]. Because win% = 50 + 50*wc, the
# same thresholds are 15 / 10 / 5 here. Reading the lila source and copying
# 0.3/0.2/0.1 into win% space would make the reviewer ~6x too lenient.
BLUNDER_THRESHOLD = 15.0
MISTAKE_THRESHOLD = 10.0
INACCURACY_THRESHOLD = 5.0


class Judgment(str, Enum):
    BLUNDER = "blunder"
    MISTAKE = "mistake"
    INACCURACY = "inaccuracy"


# Checked in this order; first match wins (lila Advice.scala uses .find, so a
# -40 win% move is a Blunder, not "also a mistake and an inaccuracy").
_JUDGMENT_LADDER: tuple[tuple[float, Judgment], ...] = (
    (BLUNDER_THRESHOLD, Judgment.BLUNDER),
    (MISTAKE_THRESHOLD, Judgment.MISTAKE),
    (INACCURACY_THRESHOLD, Judgment.INACCURACY),
)


# --------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Eval:
    """An engine evaluation, always from WHITE's point of view.

    Exactly one of ``cp`` / ``mate`` is set. ``mate`` is signed: +3 means White
    mates in 3, -3 means Black does.
    """

    cp: int | None = None
    mate: int | None = None

    def __post_init__(self) -> None:
        if (self.cp is None) == (self.mate is None):
            raise ValueError("Eval takes exactly one of cp= or mate=")

    @classmethod
    def from_pov_score(cls, score) -> "Eval":
        """Build from a python-chess ``PovScore``/``Score`` (White-relative)."""
        white = score.white() if hasattr(score, "white") else score
        mate = white.mate()
        if mate is not None:
            return cls(mate=mate)
        return cls(cp=white.score())

    def pov(self, white_to_move: bool) -> "Eval":
        """Flip to the given mover's point of view."""
        if white_to_move:
            return self
        return Eval(cp=-self.cp) if self.cp is not None else Eval(mate=-self.mate)

    @property
    def is_mate(self) -> bool:
        return self.mate is not None

    def cp_or_ceiling(self, graded: bool = False) -> int:
        """Centipawn value, mapping mate onto the ceiling.

        lila's server-side path flattens every mate to +/-1000cp. The browser's
        ceval instead grades it as ``(21 - min(10, |mate|)) * 100`` so that mate
        in 1 reads as better than mate in 8. Pass ``graded=True`` for the latter;
        classification never needs it (mates take a dedicated branch below) but
        it makes eval graphs far more readable.
        """
        if self.cp is not None:
            return max(-CP_CEILING, min(CP_CEILING, self.cp))
        sign = 1 if self.mate > 0 else -1
        if graded:
            return sign * (21 - min(10, abs(self.mate))) * 100
        return sign * CP_CEILING


# --------------------------------------------------------------------------
# Win percentage
# --------------------------------------------------------------------------


def winning_chances(cp: int, k: float | None = None) -> float:
    """Centipawns -> winning chances on [-1, +1], White-relative."""
    cp = max(-CP_CEILING, min(CP_CEILING, cp))
    kk = WIN_PCT_K if k is None else k
    return max(-1.0, min(1.0, 2 / (1 + math.exp(kk * cp)) - 1))


def win_percent(cp: int, k: float | None = None) -> float:
    """Centipawns -> win% on [0, 100], White-relative."""
    return 50 + 50 * winning_chances(cp, k)


def eval_win_percent(ev: Eval, k: float | None = None, graded_mate: bool = False) -> float:
    return win_percent(ev.cp_or_ceiling(graded=graded_mate), k)


# --------------------------------------------------------------------------
# Move classification
# --------------------------------------------------------------------------


def classify(
    before: Eval,
    after: Eval,
    white_to_move: bool,
    k: float | None = None,
) -> Judgment | None:
    """Judge the move that took the position from ``before`` to ``after``.

    Both evals are White-relative; ``white_to_move`` says who played the move.
    Returns ``None`` for an acceptable move.
    """
    prev = before.pov(white_to_move)
    cur = after.pov(white_to_move)

    # Mate transitions bypass the win% delta entirely (lila Advice.scala).
    # Without this, walking into mate from a dead-lost position scores as a
    # catastrophic blunder, which is useless feedback — you were already lost.
    if prev.is_mate or cur.is_mate:
        return _classify_mate(prev, cur)

    lost = eval_win_percent(prev, k) - eval_win_percent(cur, k)
    for threshold, judgment in _JUDGMENT_LADDER:
        if threshold <= lost:
            return judgment
    return None


def _classify_mate(prev: Eval, cur: Eval) -> Judgment | None:
    """Mate-specific rules, in the mover's point of view."""
    prev_mate, cur_mate = prev.mate, cur.mate

    # Mate delayed: still winning by force, just slower. Not penalised at all.
    if prev_mate is not None and cur_mate is not None:
        if prev_mate > 0 and cur_mate > 0:
            return None
        if prev_mate < 0 and cur_mate < 0:
            return None
        # Winning mate turned into being mated: treat as MateLost.
        if prev_mate > 0 > cur_mate:
            return _mate_lost(prev.cp_or_ceiling())
        return None

    # MateCreated: opponent now has forced mate against us.
    if cur_mate is not None and cur_mate < 0:
        prev_cp = prev.cp if prev.cp is not None else 0
        if prev_cp < -999:
            return Judgment.INACCURACY  # already completely lost
        if prev_cp < -700:
            return Judgment.MISTAKE
        return Judgment.BLUNDER

    # We found a mate for ourselves. Never a demerit.
    if cur_mate is not None and cur_mate > 0:
        return None

    # MateLost: we had forced mate, now we only have a cp evaluation.
    if prev_mate is not None and prev_mate > 0:
        return _mate_lost(cur.cp if cur.cp is not None else 0)

    # We were being mated and escaped. Good for us.
    return None


def _mate_lost(current_cp: int) -> Judgment:
    if current_cp > 999:
        return Judgment.INACCURACY  # still completely winning
    if current_cp > 700:
        return Judgment.MISTAKE
    return Judgment.BLUNDER


# --------------------------------------------------------------------------
# Accuracy
# --------------------------------------------------------------------------


def accuracy_pct(win_before: float, win_after: float) -> float:
    """Per-move accuracy on [0, 100] from the mover's point of view.

    Constants are lila's ``AccuracyPercent.scala`` verbatim — an exponential fit
    whose scipy ``curve_fit`` call is preserved in a comment in that file.
    """
    if win_after >= win_before:
        return 100.0  # improving your position is always 100% accurate
    d = win_before - win_after
    raw = 103.1668100711649 * math.exp(-0.04354415386753951 * d) - 3.166924740191411
    return max(0.0, min(100.0, raw + 1))  # +1 "uncertainty bonus"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / n)


def _harmonic_mean(xs: Sequence[float]) -> float | None:
    vals = [max(x, 1e-9) for x in xs]  # guard a 0% move against ZeroDivisionError
    if not vals:
        return None
    return len(vals) / sum(1 / v for v in vals)


def _weighted_mean(pairs: Sequence[tuple[float, float]]) -> float | None:
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in pairs) / total_w


def move_win_percents(evals: Iterable[Eval], k: float | None = None) -> list[float]:
    """White-relative win% for each position, with lichess's +15cp start."""
    out = [win_percent(CP_INITIAL, k)]
    out.extend(eval_win_percent(e, k) for e in evals)
    return out


def game_accuracy(win_percents: Sequence[float], white: bool) -> float | None:
    """Aggregate per-move accuracy into a single game accuracy for one colour.

    Deliberately *not* a plain mean, mirroring lila's ``gameAccuracy``:

    * each move is weighted by the standard deviation of win% in a sliding
      window around it, clamped to [0.5, 12] — volatile moments are the ones
      that mattered, a dead-drawn shuffle shouldn't inflate the score;
    * the final figure averages that volatility-weighted mean with the
      *harmonic* mean, which is what makes one catastrophe actually hurt.

    ``win_percents`` is White-relative and includes the starting position, i.e.
    ``len == n_moves + 1``.
    """
    if len(win_percents) < 2:
        return None

    n_moves = len(win_percents) - 1
    window_size = int(_clamp(n_moves // 10, 2, 8))

    # lila pads the start so every move gets a weight: (window_size - 2) copies
    # of the first window, then every sliding window.
    effective = min(window_size, len(win_percents))
    windows: list[Sequence[float]] = [win_percents[:window_size]] * max(0, effective - 2)
    windows += [
        win_percents[i : i + window_size]
        for i in range(0, len(win_percents) - window_size + 1)
    ]
    weights = [_clamp(_stdev(w), 0.5, 12) for w in windows]

    accuracies: list[float] = []
    for i in range(n_moves):
        before, after = win_percents[i], win_percents[i + 1]
        if white:
            accuracies.append(accuracy_pct(before, after))
        else:
            # Win% is White-relative, so Black's before/after are swapped
            # rather than negated.
            accuracies.append(accuracy_pct(100 - before, 100 - after))

    # Moves by this colour: White plays plies 0, 2, 4...; Black 1, 3, 5...
    offset = 0 if white else 1
    mine = [(accuracies[i], weights[i]) for i in range(offset, n_moves, 2)]
    if not mine:
        return None

    weighted = _weighted_mean(mine)
    harmonic = _harmonic_mean([a for a, _ in mine])
    if weighted is None or harmonic is None:
        return None
    return (weighted + harmonic) / 2


def acpl(win_percents: Sequence[float], evals: Sequence[Eval], white: bool) -> float | None:
    """Average centipawn loss for one colour, capped per-move at 1000."""
    if not evals:
        return None
    cps = [CP_INITIAL] + [e.cp_or_ceiling() for e in evals]
    offset = 0 if white else 1
    losses = []
    for i in range(offset, len(cps) - 1, 2):
        before, after = cps[i], cps[i + 1]
        loss = (before - after) if white else (after - before)
        losses.append(max(0, min(CP_CEILING, loss)))
    return sum(losses) / len(losses) if losses else None
