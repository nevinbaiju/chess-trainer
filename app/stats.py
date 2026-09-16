"""What is going wrong across all your games, not just in one of them.

The review tells you about a single game. That is not a learning gradient: a
player who hangs a piece in every game gets told about it sixteen separate
times and never once told it is a *pattern*. This module does the aggregation —
which mistake recurs, whether it is getting rarer, and what single thing is
worth working on next.

Everything here reads the stored per-move review data. Nothing calls an engine.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict

PIECE_FROM_SAN = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen", "K": "king"}

#: Human-readable names, and how to fix each one. The advice is deliberately
#: concrete: "look at what your opponent attacks before you move" is actionable,
#: "improve your tactics" is not.
MOTIF_ADVICE = {
    "hung_piece": (
        "Leaving pieces where they can be taken",
        "Before you commit a move, check what the piece you are moving would be "
        "attacked by once it lands, and whether anything it was defending comes loose.",
    ),
    "missed_free_piece": (
        "Not taking material your opponent left hanging",
        "Every move, scan your opponent's pieces for one that nothing is defending. "
        "At this level free material is offered constantly.",
    ),
    "allowed_fork": (
        "Walking into forks",
        "Knights fork; so do queens and pawns. When two of your pieces sit a knight's "
        "move apart — especially with your king — check the squares that hit both.",
    ),
    "allowed_pin": (
        "Getting pinned",
        "A piece in front of your king or queen on an open line cannot move. Watch for "
        "enemy bishops and rooks lining up before you block the line yourself.",
    ),
    "allowed_skewer": (
        "Getting skewered",
        "Two valuable pieces on the same line invite a rook or bishop to attack through "
        "them. Avoid lining up your king and queen.",
    ),
    "trapped_piece": (
        "Trapping your own pieces",
        "Before sending a piece deep into your opponent's half, count its escape squares.",
    ),
    "allowed_mate": (
        "Allowing forced mate",
        "When your king has few escape squares, treat every enemy check as urgent.",
    ),
    "missed_mate": (
        "Missing forced mate",
        "When your opponent's king is short of squares, look for checks first.",
    ),
    "back_rank_weakness": (
        "Back-rank weakness",
        "Once enemy rooks reach an open file, give your king an escape square.",
    ),
    "king_left_in_the_centre": (
        "Leaving the king in the centre",
        "Castle early. An uncastled king on an opening file is what most of these "
        "games are decided by.",
    ),
    "allowed_discovered_attack": (
        "Discovered attacks",
        "Watch for enemy pieces that are shielding a rook, bishop or queen behind them.",
    ),
}


def piece_from_san(san: str) -> str:
    if san.startswith("O-O"):
        return "king"
    return PIECE_FROM_SAN.get(san[0], "pawn")


def _hero_moves(data: dict, hero_white: bool) -> list[dict]:
    return [m for m in data["moves"] if (m["color"] == "white") == hero_white]


def _opponent_moves(data: dict, hero_white: bool) -> list[dict]:
    return [m for m in data["moves"] if (m["color"] == "white") != hero_white]


def awareness(data: dict, hero_white: bool) -> tuple[int, int]:
    """(chances, taken) — did you punish your opponent's blunders?

    lichess calls this Awareness, and at beginner level it is probably the most
    actionable single number there is: your opponent hands you material
    constantly, and the games turn on whether you notice.

    A chance is an opponent mistake or blunder; you took it if your very next
    move was not itself a mistake.
    """
    moves = {m["ply"]: m for m in data["moves"]}
    chances = taken = 0
    for ply, move in moves.items():
        if (move["color"] == "white") == hero_white:
            continue
        if move["judgment"] not in ("mistake", "blunder"):
            continue
        reply = moves.get(ply + 1)
        if reply is None:
            continue
        chances += 1
        if reply["judgment"] is None:
            taken += 1
    return chances, taken


def aggregate(games: list[tuple[dict, dict]]) -> dict:
    """Roll up every reviewed game.

    ``games`` is a list of ``(meta, review_data)``, oldest first, where ``meta``
    carries at least ``player_color`` and ``result``.
    """
    if not games:
        return {"games": 0}

    motif_counts: Counter = Counter()
    motif_games: defaultdict = defaultdict(set)
    piece_blunders: Counter = Counter()
    phase_moves: Counter = Counter()
    phase_blunders: Counter = Counter()
    accuracies: list[float] = []
    per_game_blunders: list[int] = []
    blunder_rates: list[float] = []
    first_errors: list[int] = []
    chances = taken = 0
    wins = losses = draws = 0

    for index, (meta, data) in enumerate(games):
        hero_white = meta["player_color"] == "white"
        mine = _hero_moves(data, hero_white)

        result = meta.get("result")
        if result in ("1-0", "0-1"):
            if (result == "1-0") == hero_white:
                wins += 1
            else:
                losses += 1
        elif result == "1/2-1/2":
            draws += 1

        accuracy = data["accuracy"]["white" if hero_white else "black"]
        if accuracy is not None:
            accuracies.append(accuracy)

        blunders = 0
        my_move_count = len(mine)
        for move in mine:
            phase_moves[move["phase"]] += 1
            if move["judgment"] == "blunder":
                blunders += 1
                piece_blunders[piece_from_san(move["san"])] += 1
                phase_blunders[move["phase"]] += 1
            # Only count what went wrong on moves that actually went wrong. A
            # finding attached to a perfectly good move is a description of the
            # position, not a mistake, and counting those buries the real ones.
            if move["judgment"] is None:
                continue
            for finding in move["findings"]:
                motif_counts[finding["motif"]] += 1
                motif_games[finding["motif"]].add(index)
        per_game_blunders.append(blunders)
        # Rate, not count. A weaker opponent means longer games, so the raw
        # count per game rises even when you are blundering no more often —
        # measured on real data, 1.8 -> 3.2 per game was 15.9 -> 16.3 per 100
        # moves. Reporting the count told the player they were getting worse
        # while they were flat, which is the worst thing a learning tool can do.
        blunder_rates.append(100 * blunders / my_move_count if my_move_count else 0.0)

        serious = [m["ply"] for m in mine if m["judgment"] in ("blunder", "mistake")]
        if serious:
            first_errors.append(min(serious))

        got, hit = awareness(data, hero_white)
        chances += got
        taken += hit

    # Ranked by how many *games* a problem shows up in, not by raw count.
    # One endgame where mate was available for twenty moves running would
    # otherwise outrank a mistake made in every single game.
    weaknesses = []
    ranked = sorted(
        motif_counts,
        key=lambda m: (len(motif_games[m]), motif_counts[m]),
        reverse=True,
    )
    for motif in ranked[:6]:
        title, advice = MOTIF_ADVICE.get(motif, (motif.replace("_", " "), ""))
        weaknesses.append(
            {
                "motif": motif,
                "title": title,
                "advice": advice,
                "count": motif_counts[motif],
                "games": len(motif_games[motif]),
                "share": round(100 * len(motif_games[motif]) / len(games)),
            }
        )

    return {
        "games": len(games),
        "record": {"wins": wins, "losses": losses, "draws": draws},
        "accuracy": {
            "mean": round(statistics.mean(accuracies), 1) if accuracies else None,
            "trend": _trend(accuracies),
        },
        "blunders": {
            "total": sum(per_game_blunders),
            "per_game": round(statistics.mean(per_game_blunders), 1),
            "per_100": round(statistics.mean(blunder_rates), 1),
            # Trended on the rate so game length cannot fake a decline.
            "trend": _trend(blunder_rates, lower_is_better=True),
        },
        "first_error": {
            "median_ply": int(statistics.median(first_errors)) if first_errors else None,
            "median_move": int(statistics.median(first_errors)) // 2 + 1 if first_errors else None,
        },
        "awareness": {
            "chances": chances,
            "taken": taken,
            "pct": round(100 * taken / chances) if chances else None,
        },
        "by_piece": [
            {"piece": p, "blunders": n} for p, n in piece_blunders.most_common()
        ],
        "by_phase": [
            {
                "phase": phase,
                "moves": phase_moves[phase],
                "blunders": phase_blunders[phase],
                "per_100": round(100 * phase_blunders[phase] / phase_moves[phase], 1)
                if phase_moves[phase]
                else 0,
            }
            for phase in ("opening", "middlegame", "endgame")
            if phase_moves[phase]
        ],
        "weaknesses": weaknesses,
        "headline": _headline(weaknesses, chances, taken, per_game_blunders, len(games)),
    }


def _trend(values: list[float], lower_is_better: bool = False) -> dict | None:
    """Compare the first half of the games with the second.

    Fewer than six games is not a trend, it is noise, and saying otherwise would
    tell someone they are improving on the strength of one good game.
    """
    if len(values) < 6:
        return None
    half = len(values) // 2
    early = statistics.mean(values[:half])
    late = statistics.mean(values[half:])
    change = late - early
    improving = change < 0 if lower_is_better else change > 0
    return {
        "early": round(early, 1),
        "late": round(late, 1),
        "change": round(change, 1),
        "improving": improving,
    }


def _headline(
    weaknesses: list[dict],
    chances: int,
    taken: int,
    blunders: list[int],
    total_games: int,
) -> str:
    """The one thing worth saying first."""
    if chances >= 5 and taken / chances < 0.5:
        missed = chances - taken
        return (
            f"Your opponent blundered {chances} times and you punished {taken} of them. "
            f"Spotting those {missed} would have changed more games than anything else."
        )
    if weaknesses:
        top = weaknesses[0]
        return (
            f"{top['title']} is your most common mistake — it shows up in "
            f"{top['games']} of your {total_games} games. {top['advice']}"
        )
    if blunders and statistics.mean(blunders) > 2:
        return (
            f"You are averaging {statistics.mean(blunders):.1f} blunders a game. "
            "Slowing down for one safety check per move is worth more than any opening study."
        )
    return "Not enough reviewed games yet to see a pattern."
