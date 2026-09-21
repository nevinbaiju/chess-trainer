"""Which Lichess puzzles train which of our motifs.

The asymmetry here is the whole design problem. Our motifs are mostly records
of *defensive* failure — ``allowed_fork`` means the opponent forked **you** —
while every Lichess puzzle puts the solver on the winning side. So only two
motifs map directly, and the rest map to the same pattern seen from the other
chair.

That is real transfer rather than a fudge: you cannot avoid a pattern you
cannot recognise, and 200 forks from the winning side teach you what a fork
looks like. But it is not the same skill as prophylaxis, and the app must not
imply otherwise. It avoids implying it by never naming the theme up front — the
prompt is always "find the best move", and the motif is revealed afterwards as
feedback. See :mod:`app.puzzles`.

Lichess's own ``defensiveMove`` theme *would* train the defensive skill
properly, and is deliberately unused: its median rating is 1898, which is
hopeless at this level.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Direction of the mapping, kept because it is the difference between "this
#: trains exactly what you got wrong" and "this trains the pattern behind it" —
#: which will matter when we ask whether drilling actually moved a weakness.
DIRECT = "direct"        # the puzzle is the failing skill itself
INVERTED = "inverted"    # the same pattern, from the winning side
LOOSE = "loose"          # related, but not the same idea


@dataclass(frozen=True)
class ThemeMap:
    themes: tuple[str, ...]
    direction: str
    #: Shown after the attempt, never before.
    note: str


MOTIF_THEMES: dict[str, ThemeMap] = {
    # -- the two that train the exact failing skill ------------------------
    "missed_free_piece": ThemeMap(
        ("hangingPiece",), DIRECT,
        "You leave free material on the board. This is that exact skill: "
        "something is hanging and the move is to take it."),
    "missed_mate": ThemeMap(
        ("mateIn1", "mateIn2"), DIRECT,
        "You have walked past forced mates. Same thing here — the mate is "
        "there to be found."),

    # -- the pattern, from the other side ----------------------------------
    "hung_piece": ThemeMap(
        ("hangingPiece",), INVERTED,
        "You hang pieces more than anything else. This is a hanging piece seen "
        "from the winning side: learning to spot one is how you stop leaving "
        "them."),
    "allowed_fork": ThemeMap(
        ("fork",), INVERTED,
        "You get forked. Here is a fork from the other chair — spotting them is "
        "the first half of seeing them coming."),
    "allowed_pin": ThemeMap(
        ("pin",), INVERTED,
        "You get pinned. This is a pin from the winning side."),
    "allowed_skewer": ThemeMap(
        ("skewer",), INVERTED,
        "You get skewered. This is a skewer from the winning side."),
    "allowed_discovered_attack": ThemeMap(
        ("discoveredAttack",), INVERTED,
        "You walk into discovered attacks. Here is one to play yourself."),
    "allowed_mate": ThemeMap(
        ("mateIn1", "mateIn2"), INVERTED,
        "You get mated. Knowing the mating patterns is how you see one "
        "arriving."),
    "back_rank_weakness": ThemeMap(
        ("backRankMate",), INVERTED,
        "Your back rank keeps being loose. This is the mate that punishes it."),
    "trapped_piece": ThemeMap(
        ("trappedPiece",), INVERTED,
        "Your pieces get trapped. Here is the pattern from the other side."),

    # -- related, but not the same idea ------------------------------------
    "king_left_in_the_centre": ThemeMap(
        ("attackingF2F7",), LOOSE,
        "You leave your king in the centre. This is the attack that punishes "
        "an uncastled king — not the same idea, but the closest one that has "
        "puzzles."),
}

#: Every theme worth keeping a local copy of.
INGEST_THEMES: tuple[str, ...] = tuple(sorted(
    {theme for m in MOTIF_THEMES.values() for theme in m.themes}
))


def themes_for(motif: str) -> tuple[str, ...]:
    entry = MOTIF_THEMES.get(motif)
    return entry.themes if entry else ()
