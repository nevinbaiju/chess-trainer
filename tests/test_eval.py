"""Pin the lichess formulas, then pin our deliberate divergence from them.

The point of the first half of this file is to prove our implementation is a
faithful port: fed lichess's own constant, it must reproduce lichess. Only then
is the beginner constant a *choice* rather than a bug.
"""

import math

import pytest

from app import eval as ev

LICHESS_K = -0.00368208
BEGINNER_K = -0.002


# --------------------------------------------------------------------------
# Win percentage
# --------------------------------------------------------------------------


@pytest.mark.parametrize("k", [LICHESS_K, BEGINNER_K])
def test_equal_position_is_fifty_percent(k):
    assert ev.win_percent(0, k) == pytest.approx(50.0)


@pytest.mark.parametrize("k", [LICHESS_K, BEGINNER_K])
@pytest.mark.parametrize("cp", [10, 100, 350, 900, 5000])
def test_win_percent_is_symmetric(k, cp):
    assert ev.win_percent(cp, k) + ev.win_percent(-cp, k) == pytest.approx(100.0)


def test_reproduces_lichess_curve():
    """Recompute the reference formula independently and require a match."""
    for cp in (-2000, -500, -100, 0, 25, 100, 300, 800, 2000):
        clamped = max(-ev.CP_CEILING, min(ev.CP_CEILING, cp))
        expected = 50 + 50 * (2 / (1 + math.exp(LICHESS_K * clamped)) - 1)
        assert ev.win_percent(cp, LICHESS_K) == pytest.approx(expected, abs=1e-9)


def test_evals_are_clamped_at_the_ceiling():
    assert ev.win_percent(1000, LICHESS_K) == ev.win_percent(99999, LICHESS_K)


def test_beginner_curve_is_flatter_than_lichess():
    """The whole point of the recalibration.

    At +3.00 lichess (fitted on 2300+ players) calls it ~75% winning. A 600
    player converts that far less reliably, so the beginner curve must sit
    closer to 50%.
    """
    lichess = ev.win_percent(300, LICHESS_K)
    beginner = ev.win_percent(300, BEGINNER_K)
    assert lichess == pytest.approx(75.1, abs=0.5)
    assert beginner == pytest.approx(64.6, abs=0.5)
    assert beginner < lichess


def test_beginner_curve_downgrades_a_borderline_blunder():
    """Concretely: throwing away +3.00 is a blunder to lichess, a mistake to us.

    This is the single behavioural difference that motivates the whole module,
    so it gets an explicit test rather than being left implicit.
    """
    before, after = ev.Eval(cp=300), ev.Eval(cp=0)
    assert ev.classify(before, after, True, LICHESS_K) is ev.Judgment.BLUNDER
    assert ev.classify(before, after, True, BEGINNER_K) is ev.Judgment.MISTAKE


# --------------------------------------------------------------------------
# The scale trap
# --------------------------------------------------------------------------


def test_thresholds_match_lichess_winning_chances_scale():
    """lichess publishes 0.30/0.20/0.10 on [-1,+1]; ours are on [0,100].

    win% = 50 + 50*wc, so the conversion factor is exactly 50. If someone ever
    "fixes" our thresholds to 30/20/10 this test fails loudly.
    """
    assert ev.BLUNDER_THRESHOLD == pytest.approx(0.30 * 50)
    assert ev.MISTAKE_THRESHOLD == pytest.approx(0.20 * 50)
    assert ev.INACCURACY_THRESHOLD == pytest.approx(0.10 * 50)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _drop_to_reach(target_loss: float, k: float) -> tuple[ev.Eval, ev.Eval]:
    """A White move losing exactly ``target_loss`` win% points from equality."""
    before = ev.Eval(cp=0)
    want = 50.0 - target_loss
    # invert win% -> cp for the given k
    wc = (want - 50) / 50
    cp = int(round(math.log((2 / (wc + 1)) - 1) / k))
    return before, ev.Eval(cp=cp)


@pytest.mark.parametrize(
    "loss,expected",
    [
        (0.0, None),
        (4.9, None),
        (5.5, ev.Judgment.INACCURACY),
        (9.9, ev.Judgment.INACCURACY),
        (10.5, ev.Judgment.MISTAKE),
        (14.9, ev.Judgment.MISTAKE),
        (15.5, ev.Judgment.BLUNDER),
        (40.0, ev.Judgment.BLUNDER),
    ],
)
def test_classification_ladder(loss, expected):
    before, after = _drop_to_reach(loss, BEGINNER_K)
    assert ev.classify(before, after, True, BEGINNER_K) is expected


def test_improving_your_position_is_never_punished():
    assert ev.classify(ev.Eval(cp=0), ev.Eval(cp=400), True, BEGINNER_K) is None


def test_black_perspective_is_mirrored():
    """Same White-relative evals, opposite movers, opposite verdicts."""
    before, after = ev.Eval(cp=0), ev.Eval(cp=-400)
    assert ev.classify(before, after, white_to_move=True, k=BEGINNER_K) is ev.Judgment.BLUNDER
    assert ev.classify(before, after, white_to_move=False, k=BEGINNER_K) is None


# --------------------------------------------------------------------------
# Mate handling
# --------------------------------------------------------------------------


def test_walking_into_mate_while_already_lost_is_only_an_inaccuracy():
    """You were dead. Telling you it was a blunder teaches nothing."""
    assert (
        ev.classify(ev.Eval(cp=-1200), ev.Eval(mate=-2), True, BEGINNER_K)
        is ev.Judgment.INACCURACY
    )


def test_walking_into_mate_from_a_playable_position_is_a_blunder():
    assert (
        ev.classify(ev.Eval(cp=-100), ev.Eval(mate=-3), True, BEGINNER_K)
        is ev.Judgment.BLUNDER
    )


def test_throwing_away_a_forced_mate_is_a_blunder():
    assert (
        ev.classify(ev.Eval(mate=3), ev.Eval(cp=50), True, BEGINNER_K)
        is ev.Judgment.BLUNDER
    )


def test_losing_mate_but_staying_completely_winning_is_an_inaccuracy():
    assert (
        ev.classify(ev.Eval(mate=5), ev.Eval(cp=1500), True, BEGINNER_K)
        is ev.Judgment.INACCURACY
    )


def test_delaying_mate_is_not_penalised():
    assert ev.classify(ev.Eval(mate=2), ev.Eval(mate=6), True, BEGINNER_K) is None


def test_finding_a_mate_is_not_penalised():
    assert ev.classify(ev.Eval(cp=400), ev.Eval(mate=2), True, BEGINNER_K) is None


def test_escaping_a_mate_is_not_penalised():
    assert ev.classify(ev.Eval(mate=-2), ev.Eval(cp=-500), True, BEGINNER_K) is None


def test_graded_mate_ranks_faster_mates_higher():
    assert ev.Eval(mate=1).cp_or_ceiling(graded=True) > ev.Eval(mate=8).cp_or_ceiling(graded=True)
    # The flat server-side path deliberately does not distinguish them.
    assert ev.Eval(mate=1).cp_or_ceiling() == ev.Eval(mate=8).cp_or_ceiling()


# --------------------------------------------------------------------------
# Accuracy
# --------------------------------------------------------------------------


def test_accuracy_is_one_hundred_when_not_worsening():
    assert ev.accuracy_pct(60.0, 60.0) == 100.0
    assert ev.accuracy_pct(60.0, 80.0) == 100.0


def test_accuracy_decreases_monotonically_with_loss():
    scores = [ev.accuracy_pct(80.0, 80.0 - d) for d in (0, 5, 10, 20, 40, 60)]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_accuracy_matches_lichess_constants():
    """Recompute from the constants in AccuracyPercent.scala independently."""
    for d in (1, 5, 12, 30):
        expected = 103.1668100711649 * math.exp(-0.04354415386753951 * d) - 3.166924740191411
        assert ev.accuracy_pct(70.0, 70.0 - d) == pytest.approx(
            min(100.0, expected + 1), abs=1e-9
        )


def test_game_accuracy_rewards_a_clean_game():
    flat = [50.0] * 41
    assert ev.game_accuracy(flat, white=True) == pytest.approx(100.0)


def test_one_catastrophe_drags_the_game_score_down():
    """The harmonic mean is what makes this true; a plain mean would barely move."""
    clean = [50.0] * 41
    blown = list(clean)
    for i in range(21, 41):
        blown[i] = 5.0  # White hands over the game on move 11 and never recovers

    good = ev.game_accuracy(clean, white=True)
    bad = ev.game_accuracy(blown, white=True)
    assert bad < good
    assert bad < 80.0


def test_game_accuracy_scores_each_colour_separately():
    # White blunders on move 1, Black never does.
    wp = [50.0, 5.0] + [5.0] * 20
    white = ev.game_accuracy(wp, white=True)
    black = ev.game_accuracy(wp, white=False)
    assert white < black


def test_game_accuracy_handles_a_stub_game():
    assert ev.game_accuracy([50.0], white=True) is None


# --------------------------------------------------------------------------
# ACPL
# --------------------------------------------------------------------------


def test_acpl_ignores_gains_and_caps_losses():
    evals = [ev.Eval(cp=15), ev.Eval(cp=-200), ev.Eval(cp=-180)]
    assert ev.acpl(None, evals, white=True) is not None
    assert ev.acpl(None, [], white=True) is None


# --------------------------------------------------------------------------
# Mate has to survive the trip to the browser
# --------------------------------------------------------------------------


def _move(**over):
    from app.eval import Eval
    from app.review import MoveReview

    fields = dict(
        ply=7, san="h6", uci="h7h6", white_to_move=False, phase="opening",
        eval_before=Eval(cp=20), eval_after=Eval(cp=30),
        win_before=53.0, win_after=12.0, accuracy=40.0, judgment=None,
        best_move_san=None, best_pv_san=[], refutation_san=[],
    )
    fields.update(over)
    return MoveReview(**fields)


def test_a_review_move_carries_the_mate_not_just_a_percentage():
    """The bug: to_dict() serialised win% only, so a forced mate arrived in the
    browser as "88% winning" — the number the win% curve returns once mate is
    flattened to its centipawn ceiling. Both are wrong and neither is what the
    engine said."""
    from app.eval import Eval

    out = _move(eval_after=Eval(mate=1)).to_dict()
    assert out["mate_white"] == 1, "signed, White's point of view"
    assert out["mate_before_white"] is None
    assert out["win_white"] is not None, "the percentage is still there for the graph"


def test_a_position_with_no_mate_reports_none():
    from app.eval import Eval

    out = _move(eval_after=Eval(cp=30)).to_dict()
    assert out["mate_white"] is None
    assert out["mate_before_white"] is None


def test_the_win_curve_really_does_flatten_mate():
    """Why the mate field is needed at all: at the beginner constant a forced
    mate comes out around 88%, not 100%."""
    from app.eval import Eval, eval_win_percent

    assert 85 < eval_win_percent(Eval(mate=1)) < 92


def test_a_review_carries_its_format_version():
    """Without it there is no way to tell a review that is missing a field from
    one that never had it, and the player finds out by noticing a feature does
    nothing on their older games."""
    from app.review import REVIEW_VERSION, GameReview
    from app.phases import Division

    review = GameReview(moves=[], division=Division(plies=40, middle=10, end=40),
                        accuracy_white=0.0, accuracy_black=0.0,
                        acpl_white=0, acpl_black=0)
    assert review.to_dict()["version"] == REVIEW_VERSION


def test_reviews_without_a_version_are_treated_as_the_oldest(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "t.db")
    game = db.create_game(player_color="white", result="1-0")
    db.upsert_review(game, "done", 1.0, {"moves": []})       # no version key
    assert db.stale_reviews(2) == [game]

    db.upsert_review(game, "done", 1.0, {"moves": [], "version": 2})
    assert db.stale_reviews(2) == []


def test_a_running_review_is_not_counted_as_stale(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "t.db")
    game = db.create_game(player_color="white", result="1-0")
    db.upsert_review(game, "running", 0.4)
    assert db.stale_reviews(2) == []
