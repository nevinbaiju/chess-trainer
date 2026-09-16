"""Cross-game aggregation — the part that can say "this is a pattern"."""

import pytest

from app.stats import aggregate, awareness, piece_from_san


def move(ply, color="white", judgment=None, findings=(), phase="middlegame", san="Nf3"):
    return {
        "ply": ply,
        "san": san,
        "uci": "g1f3",
        "color": color,
        "phase": phase,
        "judgment": judgment,
        "win_lost": 20.0 if judgment else 0.0,
        "findings": [
            {"motif": m, "statement": m, "squares": [], "material_cp": 300} for m in findings
        ],
        "human": {"played_pct": 10, "played_rank": 2, "best_pct": 30,
                  "best_findable": True, "systematic": False},
    }


def game(moves, white_acc=60.0, black_acc=70.0):
    return {
        "accuracy": {"white": white_acc, "black": black_acc},
        "acpl": {"white": 90, "black": 60},
        "phases": {"middlegame_ply": 10, "endgame_ply": 40},
        "moves": moves,
        "lessons": [],
    }


def meta(color="white", result="0-1"):
    return {"player_color": color, "result": result}


# --------------------------------------------------------------------------
# Piece attribution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "san,piece",
    [("Nf3", "knight"), ("Bb5+", "bishop"), ("Rxe8#", "rook"), ("Qh5", "queen"),
     ("Kg1", "king"), ("e4", "pawn"), ("exd5", "pawn"), ("O-O", "king"),
     ("O-O-O", "king"), ("e8=Q", "pawn")],
)
def test_piece_is_read_from_the_san(san, piece):
    assert piece_from_san(san) == piece


# --------------------------------------------------------------------------
# Awareness
# --------------------------------------------------------------------------


def test_awareness_counts_punished_opponent_blunders():
    data = game([
        move(0, "white"),
        move(1, "black", judgment="blunder"),   # a chance
        move(2, "white"),                       # taken
        move(3, "black", judgment="blunder"),   # another chance
        move(4, "white", judgment="mistake"),   # missed
    ])
    assert awareness(data, hero_white=True) == (2, 1)


def test_your_own_blunders_are_not_chances():
    data = game([
        move(0, "white", judgment="blunder"),
        move(1, "black"),
    ])
    assert awareness(data, hero_white=True) == (0, 0)


def test_a_blunder_on_the_last_move_offers_no_chance():
    """There is no reply to punish it with."""
    data = game([move(0, "white"), move(1, "black", judgment="blunder")])
    assert awareness(data, hero_white=True) == (0, 0)


def test_awareness_works_from_black_side():
    data = game([
        move(0, "white", judgment="blunder"),
        move(1, "black"),
    ])
    assert awareness(data, hero_white=False) == (1, 1)


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def test_findings_on_good_moves_are_not_counted_as_weaknesses():
    """A motif attached to a fine move describes the position, not a mistake.

    Counting those made "back-rank weakness" the top weakness in real data
    simply because a castled king is always on its back rank.
    """
    data = game([
        move(0, "white", findings=["back_rank_weakness"]),              # fine move
        move(2, "white", judgment="blunder", findings=["hung_piece"]),  # real error
    ])
    out = aggregate([(meta(), data)])
    motifs = {w["motif"] for w in out["weaknesses"]}
    assert motifs == {"hung_piece"}


def test_weaknesses_rank_by_games_affected_not_raw_count():
    """One pathological game must not outrank a habit.

    A won endgame can show "missed mate" on twenty consecutive moves; hanging a
    piece once in every game matters far more.
    """
    spammy = game([
        move(ply, "white", judgment="inaccuracy", findings=["missed_mate"])
        for ply in range(0, 40, 2)
    ])
    everyday = [
        (meta(), game([move(0, "white", judgment="blunder", findings=["hung_piece"])]))
        for _ in range(3)
    ]
    out = aggregate([(meta(), spammy)] + everyday)
    assert out["weaknesses"][0]["motif"] == "hung_piece"
    assert out["weaknesses"][0]["games"] == 3
    missed = next(w for w in out["weaknesses"] if w["motif"] == "missed_mate")
    assert missed["count"] > out["weaknesses"][0]["count"]  # more raw hits, lower rank


def test_blunders_are_attributed_to_the_piece_that_moved():
    data = game([
        move(0, "white", judgment="blunder", san="Qh5"),
        move(2, "white", judgment="blunder", san="Qd8"),
        move(4, "white", judgment="blunder", san="Nf3"),
    ])
    out = aggregate([(meta(), data)])
    assert out["by_piece"][0] == {"piece": "queen", "blunders": 2}


def test_only_your_own_moves_count_against_you():
    data = game([
        move(0, "white", judgment="blunder", findings=["hung_piece"]),
        move(1, "black", judgment="blunder", findings=["hung_piece"]),
    ])
    out = aggregate([(meta(), data)])
    assert out["blunders"]["total"] == 1


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------


def test_no_trend_is_claimed_from_too_few_games():
    """Five games is noise. Telling someone they are improving on that is a lie."""
    games = [(meta(), game([move(0, "white", judgment="blunder")])) for _ in range(5)]
    assert aggregate(games)["blunders"]["trend"] is None


def _games(n, moves_each, blunders_each):
    """n games of equal length, so only the blunder *rate* differs."""
    return [
        (
            meta(),
            game([
                move(p, "white", judgment="blunder" if p // 2 < blunders_each else None)
                for p in range(0, moves_each * 2, 2)
            ]),
        )
        for _ in range(n)
    ]


def test_a_falling_blunder_rate_reads_as_improvement():
    trend = aggregate(_games(3, 10, 3) + _games(3, 10, 1))["blunders"]["trend"]
    assert trend["improving"] is True
    assert trend["early"] > trend["late"]


def test_a_rising_blunder_rate_is_not_dressed_up_as_improvement():
    trend = aggregate(_games(3, 10, 1) + _games(3, 10, 3))["blunders"]["trend"]
    assert trend["improving"] is False


def test_longer_games_at_the_same_rate_are_not_called_a_decline():
    """The artifact this metric was changed to avoid."""
    trend = aggregate(_games(3, 10, 2) + _games(3, 20, 4))["blunders"]["trend"]
    assert trend["change"] == 0


# --------------------------------------------------------------------------
# The headline
# --------------------------------------------------------------------------


def test_headline_leads_with_unpunished_blunders_when_that_is_the_problem():
    data = game(
        [move(0, "white")]
        + [m for ply in range(1, 13, 2) for m in
           (move(ply, "black", judgment="blunder"), move(ply + 1, "white", judgment="mistake"))]
    )
    out = aggregate([(meta(), data)])
    assert "punished" in out["headline"]
    assert out["awareness"]["chances"] >= 5


def test_headline_falls_back_to_the_commonest_mistake():
    games = [
        (meta(), game([move(0, "white", judgment="blunder", findings=["hung_piece"])]))
        for _ in range(4)
    ]
    out = aggregate(games)
    assert "Leaving pieces where they can be taken" in out["headline"]
    assert "check what the piece you are moving" in out["headline"]


def test_no_games_is_handled():
    assert aggregate([])["games"] == 0


def test_record_is_counted_from_the_players_side():
    out = aggregate([
        (meta("white", "1-0"), game([move(0, "white")])),
        (meta("black", "1-0"), game([move(0, "white")])),
        (meta("black", "0-1"), game([move(0, "white")])),
    ])
    assert out["record"] == {"wins": 2, "losses": 1, "draws": 0}


def test_the_blunder_trend_is_a_rate_not_a_count():
    """Longer games must not read as getting worse.

    Real data: as the opponent weakened, games ran 43 -> 51 of the player's
    moves, so blunders per game went 1.8 -> 3.2 while the rate went 15.9 -> 16.3
    per 100 — flat. Trending the count told the player they were deteriorating
    when they were steady.
    """
    short_sloppy = [
        (meta(), game([move(p, "white", judgment="blunder" if p < 4 else None)
                       for p in range(0, 20, 2)]))
        for _ in range(3)
    ]                                   # 10 moves, 2 blunders  -> 20 per 100
    long_same_rate = [
        (meta(), game([move(p, "white", judgment="blunder" if p < 8 else None)
                       for p in range(0, 40, 2)]))
        for _ in range(3)
    ]                                   # 20 moves, 4 blunders  -> 20 per 100
    out = aggregate(short_sloppy + long_same_rate)
    assert out["blunders"]["per_game"] > 2          # raw count doubled
    assert out["blunders"]["trend"]["change"] == 0  # the rate did not move
