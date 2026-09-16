"""Player rating, and the servo that picks how strong the bot should be.

Two separate jobs, deliberately kept apart:

**Your rating** is tracked with Glicko-2, which carries a rating *deviation*
alongside the number. That matters here because the app starts with no data at
all: a plain Elo would lurch 32 points per game forever, whereas Glicko-2 moves
fast while it is uncertain and settles down once it knows you.

**The bot's strength dial** is a servo, not a rating. This is the important
part. Maia's Elo label is known to be unreliable — the Lichess bot ``maia1``
carries a "1100" net and performs around 1440-1700, because argmax over a human
policy returns the *modal* move of a rating band, and the mode is stronger than
the average (blunders are diverse and individually rare; the good move is
concentrated). Sampling at temperature ~1 recovers most of that, but the
residual error is unknown and probably position-dependent.

So we never trust the label. We measure: nudge the conditioning Elo until the
player actually scores ``TARGET_SCORE``, and let the dial land wherever it
lands. If Maia's label is off by 400 points, the servo simply converges to a
different number and nothing downstream cares.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# -- Glicko-2 ---------------------------------------------------------------

GLICKO_SCALE = 173.7178
DEFAULT_RATING = 1200.0   # a beginner, not the 1500 Glicko default
DEFAULT_RD = 350.0
DEFAULT_VOLATILITY = 0.06
TAU = 0.5                 # system constant; smaller = less volatile
CONVERGENCE = 1e-6

# -- The servo --------------------------------------------------------------

#: Aim for the player scoring a bit under half. Losing slightly more often than
#: winning is where the learning is; 50/50 stops being a stretch and 30% is
#: demoralising.
TARGET_SCORE = 0.45

#: Games required before the dial moves at all. Chess results are noisy: on
#: three games a coin flip looks like a trend.
MIN_GAMES_TO_ADJUST = 5

#: Maximum Elo points to move in one adjustment. With the gain below this never
#: binds; it is a guard against a future gain change going unnoticed.
MAX_STEP = 75.0

#: Proportional gain, in Elo points per unit of score error.
#:
#: This is a control-loop constant, not a taste setting. Because the score is
#: measured over a rolling 10-game window, the same game influences ten
#: consecutive corrections, which delays the feedback by ~10 games. Stability
#: needs ``window * gain * d(score)/d(Elo) < 1``; the logistic slope at parity is
#: 0.25*ln(10)/400 = 0.00144, so the ceiling is a gain of about 70.
#:
#: Measured over 8 seeds, correcting a deliberate 300-point error in 120 games:
#:
#:     gain   loop gain   settled error   wobble
#:      150      2.16          69          +/-139   oscillates
#:      100      1.44          62          +/-103   oscillates
#:       60      0.86          50          +/- 46
#:       40      0.58          38          +/- 20   <- chosen
#:       25      0.36         181          +/-  9   too slow to converge
SERVO_GAIN = 40.0

#: Minimum step worth taking, in Elo points. Counter-intuitively a *wider*
#: deadband settles closer to the truth, because a narrow one lets ordinary
#: win/loss noise drive the dial continuously. Measured over 10 seeds:
#:
#:     deadband   settled error   wobble   moves when you score
#:        10           49          +/-13    outside 20-70%
#:         6           57          +/-30    outside 30-60%
#:         4           70          +/-38    outside 35-55%
#:
#: In practice: winning 7 of your last 10 makes the opponent harder, losing 8
#: of 10 makes it easier, and anything in between leaves it alone. A settled
#: error of ~50 Elo means you score somewhere in 38-52% rather than exactly
#: 45%, which is well inside "slightly harder than me".
#: Set to 9 rather than 10 so the 7-of-10 trigger is not a knife edge: at 10
#: the step for a 70% score is 40*(0.7-0.45) = 9.999999999999998 in floating
#: point, which silently fails the test it was meant to pass.
SERVO_DEADBAND = 9.0

#: Maia-3's trained conditioning range on the Lichess scale.
MIN_BOT_ELO, MAX_BOT_ELO = 600, 2600

#: How far the player may shift the opponent from where the servo has it. The
#: servo optimises for "a good game"; sometimes you just want an easier one,
#: and waiting a dozen games for the dial to drift is not an answer.
MAX_OPPONENT_OFFSET = 300


def apply_offset(bot_elo: int, offset: int) -> int:
    offset = max(-MAX_OPPONENT_OFFSET, min(MAX_OPPONENT_OFFSET, int(offset or 0)))
    return int(max(MIN_BOT_ELO, min(MAX_BOT_ELO, bot_elo + offset)))


@dataclass
class Player:
    rating: float = DEFAULT_RATING
    rd: float = DEFAULT_RD
    volatility: float = DEFAULT_VOLATILITY
    bot_elo: int = 1000
    recent_scores: list[float] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        """Has the rating settled enough to be worth showing without a caveat?"""
        return self.rd < 110


def _g(phi: float) -> float:
    return 1 / math.sqrt(1 + 3 * phi**2 / math.pi**2)


def _expected(mu: float, mu_j: float, phi_j: float) -> float:
    return 1 / (1 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update_rating(player: Player, opponent_elo: float, score: float) -> Player:
    """One Glicko-2 update from a single game.

    ``score`` is 1.0 win, 0.5 draw, 0.0 loss. The opponent (the bot) is treated
    as having a fixed, certain rating, because its strength is a dial we set
    rather than something we are estimating.
    """
    mu = (player.rating - 1500) / GLICKO_SCALE
    phi = player.rd / GLICKO_SCALE
    sigma = player.volatility

    mu_j = (opponent_elo - 1500) / GLICKO_SCALE
    phi_j = 0.0  # the bot's setting is exact by construction

    g_j = _g(phi_j)
    e = _expected(mu, mu_j, phi_j)

    v = 1 / (g_j**2 * e * (1 - e)) if 0 < e < 1 else 1e6
    delta = v * g_j * (score - e)

    # Volatility, by Mark Glickman's illinois-algorithm iteration.
    a = math.log(sigma**2)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta**2 - phi**2 - v - ex)
        den = 2 * (phi**2 + v + ex) ** 2
        return num / den - (x - a) / TAU**2

    A = a
    if delta**2 > phi**2 + v:
        B = math.log(delta**2 - phi**2 - v)
    else:
        k = 1
        while f(a - k * TAU) < 0 and k < 100:
            k += 1
        B = a - k * TAU

    fa, fb = f(A), f(B)
    for _ in range(100):
        if abs(B - A) <= CONVERGENCE:
            break
        C = A + (A - B) * fa / (fb - fa)
        fc = f(C)
        if fc * fb <= 0:
            A, fa = B, fb
        else:
            fa /= 2
        B, fb = C, fc

    new_sigma = math.exp(A / 2)
    phi_star = math.sqrt(phi**2 + new_sigma**2)
    new_phi = 1 / math.sqrt(1 / phi_star**2 + 1 / v)
    new_mu = mu + new_phi**2 * g_j * (score - e)

    return Player(
        rating=round(new_mu * GLICKO_SCALE + 1500, 1),
        rd=round(max(30.0, new_phi * GLICKO_SCALE), 1),
        volatility=round(new_sigma, 6),
        bot_elo=player.bot_elo,
        recent_scores=player.recent_scores,
    )


def target_bot_elo(rating: float, target: float = TARGET_SCORE) -> int:
    """The opponent rating at which this player would score ``target``.

    Inverting the Elo expectation: if expected score is
    ``1 / (1 + 10**((E - R)/400))``, then ``E = R + 400*log10((1-s)/s)``.

    This replaces the proportional servo that used to inch the dial along, and
    the reason is visible in real data: over 25 games of scoring 25%, the old
    controller moved the opponent from 1000 to 946 — about a dozen games behind
    where the evidence already pointed. Glicko-2 is *already* an estimator of
    the player's strength against these opponents, and it integrates every
    result with proper uncertainty weighting. Deriving the dial from it lands on
    the right answer immediately instead of crawling toward it.

    The dial is still not a true Elo — Maia's conditioning is only roughly
    calibrated — but it does not need to be. The rating is measured *in dial
    units*, so the loop closes on whatever setting actually produces ``target``.
    Measured: Maia at 600 scores 4% against Maia at 1200, so the dial really
    does control strength, which is the only property this relies on.
    """
    gap = 400 * math.log10((1 - target) / target)
    return int(max(MIN_BOT_ELO, min(MAX_BOT_ELO, round(rating + gap))))


def record_result(player: Player, score: float, window: int = 10) -> Player:
    """Log a result and re-aim the bot's strength dial."""
    updated = update_rating(player, player.bot_elo, score)
    updated.recent_scores = (player.recent_scores + [score])[-window:]
    updated.bot_elo = target_bot_elo(updated.rating)
    return updated


def effective_gain(window: int, gain: float = SERVO_GAIN) -> float:
    """Scale the gain to the amount of evidence actually in hand.

    The stability limit is ``window * gain * d(score)/d(Elo) < 1``, so the
    ceiling depends on how many games the average is taken over. SERVO_GAIN is
    sized for a full 10-game window; with only 5 games the feedback delay is
    halved and twice the gain is equally stable. Without this the dial barely
    moves over a player's first dozen games, which is exactly when it is most
    likely to be set wrong.
    """
    window = max(1, min(window, 10))
    return min(gain * 10.0 / window, 120.0)


def next_bot_elo(  # noqa: D401 - retained for the tests that pin its behaviour
    current: int,
    recent_scores: list[float],
    target: float = TARGET_SCORE,
    gain: float = SERVO_GAIN,
) -> int:
    """Move the dial toward whatever setting makes the player score ``target``.

    Proportional control with a step cap, so a lucky streak nudges rather than
    lurches. The dial is *not* an estimate of the bot's true strength and should
    never be displayed as one — it is just the number that produces good games.
    """
    if len(recent_scores) < MIN_GAMES_TO_ADJUST:
        return current
    observed = sum(recent_scores) / len(recent_scores)
    scaled = effective_gain(len(recent_scores), gain)
    step = max(-MAX_STEP, min(MAX_STEP, scaled * (observed - target)))
    if abs(step) < SERVO_DEADBAND:
        return current  # don't twitch when it is already about right
    return int(max(MIN_BOT_ELO, min(MAX_BOT_ELO, round(current + step))))


def suggested_starting_elo(chesscom_rapid: int) -> int:
    """Map a chess.com rapid rating onto Maia's Lichess-scale conditioning.

    Lichess ratings run materially higher than chess.com at the beginner end —
    Lichess seeds everyone at 1500 while chess.com starts most players far
    lower, so the gap is widest (300-500 points) exactly where this app is
    aimed and narrows higher up. This is a starting guess only; the servo above
    corrects it within a handful of games, which is the whole reason it exists.
    """
    if chesscom_rapid <= 800:
        offset = 450
    elif chesscom_rapid <= 1200:
        offset = 350
    elif chesscom_rapid <= 1600:
        offset = 250
    else:
        offset = 150
    return int(max(MIN_BOT_ELO, min(MAX_BOT_ELO, chesscom_rapid + offset)))
