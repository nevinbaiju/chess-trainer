"""Rating and the opponent-strength servo.

The servo test is the important one. Maia's Elo label is known to be
unreliable — the Lichess ``maia1100`` bot performs around 1440-1700 — so the
app must never depend on the label being right. The test below starts the dial
300 points *wrong* on purpose and requires it to find the truth anyway.
"""

import random

import pytest

from app.rating import (
    DEFAULT_RATING,
    MAX_BOT_ELO,
    MIN_BOT_ELO,
    MIN_GAMES_TO_ADJUST,
    Player,
    next_bot_elo,
    record_result,
    suggested_starting_elo,
    update_rating,
)


# --------------------------------------------------------------------------
# Glicko-2
# --------------------------------------------------------------------------


def test_a_win_raises_the_rating_and_a_loss_lowers_it():
    start = Player()
    after_win = update_rating(start, 1200, 1.0)
    after_loss = update_rating(start, 1200, 0.0)
    assert after_win.rating > start.rating
    assert after_loss.rating < start.rating


def test_uncertainty_shrinks_as_games_are_played():
    player = Player()
    first_rd = player.rd
    for _ in range(15):
        player = update_rating(player, 1200, 0.5)
    assert player.rd < first_rd
    assert player.rd >= 30.0  # floored, so the rating can still move later


def test_a_new_player_is_not_treated_as_confident():
    assert not Player().confident


def test_rating_becomes_confident_after_enough_games():
    player = Player()
    for _ in range(40):
        player = update_rating(player, 1000, random.choice([0.0, 1.0]))
    assert player.confident


def test_beating_a_much_stronger_opponent_moves_the_rating_more():
    player = Player(rating=1000, rd=80)
    small = update_rating(player, 900, 1.0).rating - player.rating
    large = update_rating(player, 1600, 1.0).rating - player.rating
    assert large > small


def test_rating_defaults_to_a_beginner_not_the_glicko_midpoint():
    """1500 is Glicko's default; it is a bad guess for someone rated 550."""
    assert DEFAULT_RATING < 1500


# --------------------------------------------------------------------------
# The servo
# --------------------------------------------------------------------------


def test_dial_does_not_move_before_enough_games():
    assert next_bot_elo(1000, [1.0] * (MIN_GAMES_TO_ADJUST - 1)) == 1000


def test_dial_rises_on_a_winning_streak_and_falls_on_a_losing_one():
    assert next_bot_elo(1000, [1.0] * 10) > 1000
    assert next_bot_elo(1000, [0.0] * 10) < 1000


def test_dial_holds_still_when_the_player_is_scoring_on_target():
    on_target = [1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0]  # 40%
    assert next_bot_elo(1000, on_target) == 1000


def test_dial_steps_are_capped():
    """One lucky streak should nudge the opponent, not teleport it."""
    assert next_bot_elo(1000, [1.0] * 10) - 1000 <= 75


def test_dial_respects_maia_trained_range():
    assert next_bot_elo(MIN_BOT_ELO + 20, [0.0] * 10) >= MIN_BOT_ELO
    assert next_bot_elo(MAX_BOT_ELO - 20, [1.0] * 10) <= MAX_BOT_ELO


def test_servo_converges_even_when_the_label_is_badly_wrong():
    """The whole reason the dial is a servo rather than a trusted number.

    The opponent's real strength is 1050 but the dial starts at 1350. If the
    servo only worked when the label was already right it would be pointless.
    """
    rng = random.Random(11)
    true_strength = 1050
    player = Player(bot_elo=1350)

    for _ in range(140):
        expected = 1 / (1 + 10 ** ((player.bot_elo - true_strength) / 400))
        score = 1.0 if rng.random() < expected else 0.0
        player = record_result(player, score)

    assert abs(player.bot_elo - true_strength) < 120  # started 300 out
    assert abs(player.rating - true_strength) < 150
    assert player.confident


def test_recent_scores_are_windowed():
    player = Player()
    for i in range(25):
        player = record_result(player, float(i % 2), window=10)
    assert len(player.recent_scores) == 10


# --------------------------------------------------------------------------
# Starting guess
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chesscom,expected_min", [(550, 900), (900, 1200), (1400, 1600)])
def test_chesscom_ratings_map_above_themselves(chesscom, expected_min):
    """Lichess ratings run higher than chess.com, most of all at the bottom."""
    assert suggested_starting_elo(chesscom) >= expected_min


def test_the_gap_narrows_as_rating_rises():
    low_gap = suggested_starting_elo(600) - 600
    high_gap = suggested_starting_elo(1800) - 1800
    assert low_gap > high_gap


def test_starting_guess_stays_inside_the_trained_range():
    assert suggested_starting_elo(100) >= MIN_BOT_ELO
    assert suggested_starting_elo(3000) <= MAX_BOT_ELO


def test_a_clear_winning_record_still_raises_the_dial():
    """The deadband must not let genuine improvement plateau.

    Winning 7 of the last 10 is the trigger for "you have outgrown this
    level"; anything closer to even is left alone as noise.
    """
    clearly_winning = [1.0] * 7 + [0.0] * 3
    assert next_bot_elo(1000, clearly_winning) > 1000

    roughly_even = [1.0] * 5 + [0.0] * 5
    assert next_bot_elo(1000, roughly_even) == 1000


# --------------------------------------------------------------------------
# Deriving the dial from the rating
# --------------------------------------------------------------------------


def test_the_dial_is_derived_from_the_rating_not_crawled_toward():
    """Why the proportional servo was replaced.

    Measured on real play: over 25 games of scoring 25%, the old controller
    moved the opponent from 1000 to 946 — roughly a dozen games behind where
    the evidence already pointed. Glicko-2 is already an estimator of the
    player's strength, so the dial is read straight off it.
    """
    from app.rating import target_bot_elo

    assert target_bot_elo(816) == pytest.approx(851, abs=1)
    assert target_bot_elo(600) > 600
    # Monotonic: a stronger player always gets a stronger opponent.
    dials = [target_bot_elo(r) for r in (600, 700, 800, 900, 1000)]
    assert dials == sorted(dials)


def test_target_score_sets_how_far_above_you_the_opponent_sits():
    from app.rating import target_bot_elo

    even = target_bot_elo(1000, target=0.50)
    harder = target_bot_elo(1000, target=0.45)
    easier = target_bot_elo(1000, target=0.55)
    assert even == 1000                # 50% means an equal opponent
    assert harder > even               # scoring less means a stronger opponent
    assert easier < even


def test_the_dial_stays_inside_maias_trained_range():
    from app.rating import target_bot_elo

    assert target_bot_elo(100) >= MIN_BOT_ELO
    assert target_bot_elo(5000) <= MAX_BOT_ELO


def test_a_losing_run_now_moves_the_dial_quickly():
    """The old controller shifted about 14 points per loss; this tracks the rating."""
    player = Player(rating=900.0, rd=90.0, bot_elo=935)
    start = player.bot_elo
    for _ in range(4):
        player = record_result(player, 0.0)
    assert start - player.bot_elo > 50
