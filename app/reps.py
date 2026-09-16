"""Opening repetitions: drill a repertoire, easiest lines first.

The problem this solves is the one you hit playing drill mode: the bot followed
one named line every single game, and the moment it left the line it played a
strong reply you had no answer to. Reps mode does the opposite — the bot stays
in book for the whole exercise, and the *variety* comes from working through a
curriculum of related lines rather than from the engine improvising.

How the curriculum is ordered
-----------------------------
Both signals come free with the CC0 catalogue:

* **Depth.** "Italian Game" is 5 plies; "Italian Game: Giuoco Piano" is 6;
  "Italian Game: Giuoco Pianissimo, Italian Four Knights" is 10. A short line is
  a prefix of the longer ones, so shortest-first is already a progression from
  "know the opening" to "know the variation".
* **Popularity.** Each edge in the move tree carries the number of named lines
  running through it. After 1.e4 e5 2.Nf3 Nc6 3.Bc4, Bc5 scores 118 and Nf6 58,
  while Rousseau's f5 scores 1. The rarest step on a line is what makes it
  obscure, so a line is ranked by its weakest edge.

Difficulty is ``depth - POPULARITY_WEIGHT * log2(popularity)``. Sorting on depth
alone puts the 6-ply Rousseau Gambit (1 named line through it) ahead of the
7-ply Evans Gambit (51), which is backwards for someone learning what they will
actually face. The log is what keeps a very popular line from outranking
everything on sheer count. Anything from 0.8 to 1.8 produces the same sensible
top of the curriculum, so the exact value is not delicate.

Nothing here unlocks on a timer. A line counts as known once you have played it
correctly :data:`MASTERY_STREAK` times in a row, and each line you master opens
one more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import chess

from .openings import Opening, OpeningBook

#: How much a line's popularity offsets its depth when ordering the curriculum.
#: See the module docstring: 0 would order purely by length and teach obscure
#: short lines before common longer ones.
POPULARITY_WEIGHT = 1.0

#: Correct repetitions in a row before a line counts as known.
MASTERY_STREAK = 2

#: How many lines are playable before you have mastered anything. Enough to not
#: feel like a single flashcard, small enough to stay a curriculum.
STARTING_UNLOCKED = 3

#: How far a rep runs. Five moves a side: enough to be a real variation rather
#: than a stub, short enough to actually learn. Lines are carried out to this
#: depth along their main continuation, so the cap sets the size of the
#: exercise — at 16 the first Italian rep is eight moves of the Evans Gambit,
#: which is not a first exercise for anyone at this level.
MAX_REP_PLIES = 10

#: Lines shorter than this teach nothing — "1. e4" is not a repetition.
MIN_REP_PLIES = 4

#: Every Nth rep revisits a line you already know instead of advancing.
#:
#: Without this the curriculum never looks back: mastering a line unlocks
#: another, so there is always a fresh unmastered line queued and a "known"
#: line is never seen again. Two correct repetitions is enough to move on, not
#: enough to remember, so a quarter of reps are recall checks.
REVIEW_EVERY = 4


@dataclass(frozen=True)
class Rep:
    """One line to drill, from one side of the board."""

    opening: Opening
    color: bool                 # the side the player takes
    difficulty: int             # position in the curriculum, 0 = easiest
    rarest_edge: int            # named lines through this line's least-used move

    @property
    def name(self) -> str:
        return self.opening.name

    @property
    def key(self) -> str:
        """What this rep *is*, for storing progress against.

        Deliberately the moves and not the name. Names are derived by
        classifying the position a line reaches, so they move whenever the
        catalogue or the curriculum is rebuilt — and progress keyed on a name
        that moved looks, to the player, exactly like progress that was wiped.
        """
        return " ".join(m.uci() for m in self.moves)

    @property
    def moves(self) -> tuple[chess.Move, ...]:
        return trim_to_your_move(self.opening.moves[:MAX_REP_PLIES], self.color)

    @property
    def plies(self) -> int:
        return len(self.moves)

    def your_plies(self) -> list[int]:
        """Ply indices where it is the player's turn."""
        first = 0 if self.color == chess.WHITE else 1
        return list(range(first, self.plies, 2))

    def san_line(self) -> list[str]:
        board = chess.Board()
        out = []
        for move in self.moves:
            out.append(board.san(move))
            board.push(move)
        return out


@dataclass
class Progress:
    """What the player has done with one line."""

    streak: int = 0
    attempts: int = 0
    successes: int = 0

    @property
    def mastered(self) -> bool:
        return self.streak >= MASTERY_STREAK


@dataclass
class Curriculum:
    """An ordered set of lines for one family and one colour."""

    family: str
    color: bool
    reps: list[Rep] = field(default_factory=list)

    def unlocked(self, progress: dict[str, Progress]) -> list[Rep]:
        """Lines the player is allowed to see, given what they have mastered."""
        mastered = sum(1 for r in self.reps if progress.get(r.key, Progress()).mastered)
        return self.reps[: STARTING_UNLOCKED + mastered]

    def next_rep(self, progress: dict[str, Progress]) -> Rep | None:
        """The line to serve now: usually the next new one, sometimes a recall check."""
        available = self.unlocked(progress)
        if not available:
            return None

        known = [r for r in available if progress.get(r.key, Progress()).mastered]
        done = sum(p.attempts for p in progress.values())
        due_for_review = known and done > 0 and done % REVIEW_EVERY == 0

        if not due_for_review:
            for rep in available:
                if not progress.get(rep.key, Progress()).mastered:
                    return rep

        if known:
            # Weakest memory first: fewest consecutive successes, then the line
            # that has been practised least.
            return min(
                known,
                key=lambda r: (
                    progress.get(r.key, Progress()).streak,
                    progress.get(r.key, Progress()).attempts,
                    r.difficulty,
                ),
            )
        return available[0]

    def find(self, key: str) -> "Rep | None":
        """The rep with this move sequence, for drilling one on request."""
        return next((r for r in self.reps if r.key == key), None)

    def summary(self, progress: dict[str, Progress]) -> dict:
        """Every line in the family, with what you have done with each.

        All of them, not just the unlocked ones: the curriculum decides what to
        serve next, but it should never be the only way to reach a line. You
        cannot revise what you cannot find, and a list that shows three of
        eighty makes the other seventy-seven look like they do not exist.
        """
        unlocked = {r.key for r in self.unlocked(progress)}
        queued = self.next_rep(progress)
        mastered = [r for r in self.reps if progress.get(r.key, Progress()).mastered]
        return {
            "family": self.family,
            "color": "white" if self.color else "black",
            "total": len(self.reps),
            "unlocked": len(unlocked),
            "mastered": len(mastered),
            "next": queued.key if queued else None,
            "lines": [
                {
                    "key": r.key,
                    "name": r.name,
                    "eco": r.opening.eco,
                    "plies": r.plies,
                    "line": r.san_line(),
                    "popularity": r.rarest_edge,
                    "streak": progress.get(r.key, Progress()).streak,
                    "needed": MASTERY_STREAK,
                    "attempts": progress.get(r.key, Progress()).attempts,
                    "successes": progress.get(r.key, Progress()).successes,
                    "mastered": progress.get(r.key, Progress()).mastered,
                    "unlocked": r.key in unlocked,
                }
                for r in self.reps
            ],
        }


#: Stop extending when the only continuations are one-off curiosities. A
#: weight of 1 means a single named line goes that way, which is where the
#: mainline ends and trivia begins.
EXTEND_MIN_WEIGHT = 2


def is_your_ply(index: int, color: bool) -> bool:
    return (index % 2 == 0) == (color == chess.WHITE)


def trim_to_your_move(moves, color: bool):
    """Cut any trailing opponent move off a line.

    A rep tests your moves; the opponent's are given to you. So a line that
    ends on the opponent's move ends with a ply that tests nothing — and worse,
    two lines that differ *only* there are the same exercise wearing two names.

    That is not hypothetical. As White the Ruy Lopez Closed and Open run
    1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O and then diverge on Black's
    fifth move, which White never answers inside the rep. They were the first
    two lines of the curriculum: the same five moves, twice, under two names.
    """
    end = len(moves)
    while end and not is_your_ply(end - 1, color):
        end -= 1
    return moves[:end]


def _extend(book: OpeningBook, moves: tuple[chess.Move, ...]) -> list[chess.Move]:
    """Carry a line forward along its most popular continuation.

    Stops at ``MAX_REP_PLIES``, when the catalogue has nothing further, or when
    the only continuations left are played by a single named line each — past
    that point it is not the main line any more, just one arbitrary branch.

    Extension deliberately does *not* stop at family boundaries. The Indian
    Defense is 1.d4 Nf6 2.c4 g6, and every continuation of it is catalogued as
    a King's Indian or a Grunfeld, so refusing to cross would leave exactly the
    four-move stub this exists to remove. Crossing is honest: the position
    really is the one it transposes into, reached by the move order you asked
    to practise, and the rep is renamed for where it ends up.
    """
    board = chess.Board()
    out: list[chess.Move] = []
    for move in moves[:MAX_REP_PLIES]:
        out.append(move)
        board.push(move)

    while len(out) < MAX_REP_PLIES:
        options = book.continuations(board)
        if not options or options[0].weight < EXTEND_MIN_WEIGHT:
            break
        out.append(options[0].move)
        board.push(options[0].move)
    return out


def _as_full_line(book: OpeningBook, opening: Opening, key: tuple[str, ...]) -> Opening:
    """The extended line, renamed for the deepest position it actually reaches."""
    moves = tuple(chess.Move.from_uci(u) for u in key)
    named = book.classify(moves) or opening
    return Opening(eco=named.eco, name=named.name, moves=moves)


def family_side(book: OpeningBook, family: str) -> bool:
    """Which colour an opening belongs to.

    Three rules, in order:

    1. A family named "... Defense" is Black's. This is a genuine override, not
       a shortcut: for five families (King's Indian, Old Indian, Lion, Rat,
       Pterodactyl) the catalogue's shortest entry happens to end on a White
       move — "King's Indian Defense" is listed at 1.d4 Nf6 2.c4 g6 3.Nc3 — so
       the move rule alone gets them backwards.
    2. A family named "... Game", "... Opening", "... Attack" or "... System"
       is White's — the four families where this decides anything (Four Knights
       Game, Colle System, Marienbad System, Amsterdam Attack) are all White
       setups whose catalogue entry happens to end on Black's reply.
    3. A family named "... Accepted" or "... Declined" belongs to whoever
       *offered* the gambit, so the question is passed to the parent family:
       the King's Gambit Accepted is White's because the King's Gambit is,
       even though Black plays the last move of 1.e4 e5 2.f4 exf4.
    4. Otherwise, whoever played the last move of the family's shortest line —
       that move is what creates the named position. Bc4 makes the Italian
       Game, ...c5 makes the Sicilian.

    Rules 1-3 are skipped for names containing a comma, where the trailing
    word describes a sub-variation rather than the family itself ("Vienna
    Gambit, with Max Lange Defense" is White's).

    Checked against 25 well-known openings; the combination gets all of them.
    """
    lines = [o for o in book.openings if o.family == family]
    if not lines:
        return chess.WHITE

    if "," not in family:
        lowered = family.lower()
        if lowered.endswith(("defense", "defence")):
            return chess.BLACK
        if lowered.endswith(("game", "opening", "attack", "system")):
            return chess.WHITE
        if lowered.endswith((" accepted", " declined")):
            parent = family.rsplit(" ", 1)[0]
            if any(o.family == parent for o in book.openings):
                return family_side(book, parent)

    shortest = min(lines, key=lambda o: (len(o.moves), o.name))
    board = chess.Board()
    for move in shortest.moves[:-1]:
        board.push(move)
    return board.turn


def _rarest_edge(book: OpeningBook, opening: Opening) -> int:
    """Named lines running through this line's least-travelled move.

    A line is only as obscure as its most obscure step, so the minimum is the
    right summary — one offbeat move makes the whole variation a sideline.
    """
    board = chess.Board()
    rarest = 10_000
    for move in opening.moves[:MAX_REP_PLIES]:
        for candidate in book.tree.get(board.epd(), []):
            if candidate.move == move:
                rarest = min(rarest, candidate.weight)
                break
        board.push(move)
    return rarest


def build_curriculum(book: OpeningBook, family: str, color: bool) -> Curriculum:
    """Order a family's lines into a progression of complete variations."""
    # Every line in the family, short ones included — a two-ply stub is not a
    # rep, but it has to be here for extension and the prefix sweep to see it.
    candidates = [o for o in book.openings if o.family == family]

    # A line other lines continue is a waypoint, not an exercise. Dropping such
    # lines outright would delete every mainline, since mainlines are precisely
    # the lines others extend, so each is instead carried forward along its most
    # popular continuation until the theory genuinely forks or runs out.
    # Trimming happens here rather than at the end because it decides what
    # counts as a duplicate: two lines that differ only on the opponent's last
    # move collapse onto one key and merge, instead of being served twice.
    extended: dict[tuple[str, ...], Opening] = {}
    for opening in candidates:
        full = _extend(book, opening.moves)
        key = tuple(m.uci() for m in trim_to_your_move(full, color))
        if len(key) < MIN_REP_PLIES:
            continue
        # Several short names can collapse onto one full line; keep the one
        # whose own moves reach furthest, as the most specific label for it.
        existing = extended.get(key)
        if existing is None or len(opening.moves) > len(existing.moves):
            extended[key] = opening

    # Extension stops early where the theory forks, so a handful of lines can
    # still sit inside a longer one. Sweep those up: whatever survives, no rep
    # is a prefix of another.
    keys = sorted(extended)
    complete = {
        key: opening
        for key, opening in extended.items()
        if not any(other != key and other[: len(key)] == key for other in keys)
    }

    # Once every line runs to its natural end, length stops being a difficulty
    # signal — it only reflects how deeply that variation happens to be
    # catalogued, which would rank the Giuoco Piano (long, heavily analysed)
    # below the Rousseau Gambit (short, nobody plays it). How mainline a line is
    # now leads, with length breaking ties.
    scored: list[tuple[float, int, Opening]] = []
    for key, opening in complete.items():
        popularity = _rarest_edge(book, opening)
        score = -POPULARITY_WEIGHT * math.log2(max(popularity, 1)) + 0.1 * len(key)
        scored.append((score, popularity, _as_full_line(book, opening, key)))

    scored.sort(key=lambda t: (t[0], t[2].name))
    keep = [(pop, o) for _, pop, o in scored if len(o.moves) >= MIN_REP_PLIES]
    return Curriculum(
        family=family,
        color=color,
        reps=[
            Rep(opening=o, color=color, difficulty=i, rarest_edge=pop)
            for i, (pop, o) in enumerate(keep)
        ],
    )


def families_for(book: OpeningBook, limit: int = 40) -> list[dict]:
    """Families worth offering, biggest first, each tagged with whose it is."""
    grouped: dict[str, list[Opening]] = {}
    for opening in book.openings:
        grouped.setdefault(opening.family, []).append(opening)

    ranked = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out = []
    for family, lines in ranked[:limit]:
        shortest = min(lines, key=lambda o: (len(o.moves), o.name))
        side = family_side(book, family)
        out.append(
            {
                "family": family,
                "lines": len(lines),
                "opening_move": shortest.san_line()[0],
                "side": "white" if side == chess.WHITE else "black",
            }
        )
    return out


def check_rep(rep: Rep, played: list[chess.Move]) -> dict:
    """Compare what was played against the line.

    Returns the ply the player first went off book (``None`` if they didn't),
    plus the move that was expected there.
    """
    for index, move in enumerate(played):
        if index >= rep.plies:
            break
        if move != rep.moves[index]:
            board = chess.Board()
            for earlier in rep.moves[:index]:
                board.push(earlier)
            return {
                "deviated_at": index,
                "expected": board.san(rep.moves[index]),
                "expected_uci": rep.moves[index].uci(),
                "played": board.san(move) if move in board.legal_moves else move.uci(),
                "yours": (index % 2 == 0) == (rep.color == chess.WHITE),
            }
    return {"deviated_at": None}
