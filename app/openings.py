"""Opening book and classification.

Data: https://github.com/lichess-org/chess-openings — 3,810 named openings,
**CC0**, the same set that powers lichess itself, so our opening names match
theirs exactly. Shipped as five TSVs in ``data/openings/`` and loaded offline,
which is deliberate: the Lichess *explorer* API (the source of real per-rating
move frequencies) started requiring an OAuth token in March 2026 and is capped
at 25 requests/minute, so it cannot sit on a request path. The book works with
no network at all; explorer frequencies are a later enrichment, not a
dependency.

Classification walks the game's positions **backwards** and takes the first
named hit. That is lichess's own documented method and it is not
interchangeable with forward matching: openings transpose constantly, and
scanning forwards stops at the first name it sees rather than the most specific
one reached.
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import chess
import chess.pgn

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "openings"


class BookMode:
    """How much of the opening the bot is required to follow."""

    STRICT = "strict"        # stay in book as long as the book has a move
    DRILL = "drill"          # follow the chosen line, then play naturally
    REALISTIC = "realistic"  # no book at all; just label what happened


@dataclass(frozen=True)
class Opening:
    eco: str
    name: str
    moves: tuple[chess.Move, ...]

    @property
    def family(self) -> str:
        """"Sicilian Defense: Najdorf, English Attack" -> "Sicilian Defense"."""
        return self.name.split(":")[0].strip()

    @property
    def variation(self) -> str | None:
        _, _, rest = self.name.partition(":")
        return rest.strip() or None

    def san_line(self) -> list[str]:
        board = chess.Board()
        out = []
        for move in self.moves:
            out.append(board.san(move))
            board.push(move)
        return out


@dataclass(frozen=True)
class BookMove:
    move: chess.Move
    weight: int          # named lines running through this move
    leads_to: str | None  # opening name reached, if this move names one

    @property
    def san_hint(self) -> str | None:
        return self.leads_to


class OpeningBook:
    """Named openings, a move tree over them, and classification."""

    def __init__(self, openings: list[Opening]):
        self.openings = openings

        # EPD -> the *shortest* line naming that position. Upstream guarantees
        # each name has a unique shortest line, and preferring it keeps
        # classification stable.
        self.by_epd: dict[str, Opening] = {}
        # EPD -> {uci: [weight, name reached]}
        tree: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, None]))
        self.by_name: dict[str, Opening] = {}

        for opening in openings:
            board = chess.Board()
            for move in opening.moves:
                entry = tree[board.epd()][move.uci()]
                entry[0] += 1
                board.push(move)
            epd = board.epd()
            existing = self.by_epd.get(epd)
            if existing is None or len(opening.moves) < len(existing.moves):
                self.by_epd[epd] = opening
            self.by_name.setdefault(opening.name, opening)

        # Annotate each edge with the opening it completes, for UI hints.
        for opening in openings:
            board = chess.Board()
            for move in opening.moves[:-1]:
                board.push(move)
            if opening.moves:
                edge = tree[board.epd()].get(opening.moves[-1].uci())
                if edge is not None and edge[1] is None:
                    edge[1] = opening.name

        self.tree = {
            epd: [
                BookMove(chess.Move.from_uci(uci), weight, name)
                for uci, (weight, name) in sorted(
                    moves.items(), key=lambda kv: kv[1][0], reverse=True
                )
            ]
            for epd, moves in tree.items()
        }

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, directory: Path | None = None) -> "OpeningBook":
        directory = directory or DATA_DIR
        openings: list[Opening] = []
        for path in sorted(directory.glob("*.tsv")):
            with path.open(encoding="utf-8") as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    moves = _parse_line(row["pgn"])
                    if moves:
                        openings.append(
                            Opening(eco=row["eco"], name=row["name"], moves=tuple(moves))
                        )
        if not openings:
            raise FileNotFoundError(f"no opening TSVs found in {directory}")
        return cls(openings)

    # -- classification ----------------------------------------------------

    def classify(self, moves: Iterable[chess.Move]) -> Opening | None:
        """Name a game by walking its positions backwards.

        Backwards, not forwards: 1.e4 c5 2.Nf3 d6 3.d4 is reached by several
        move orders, and only the *last* named position describes what was
        actually played.
        """
        board = chess.Board()
        epds = [board.epd()]
        for move in moves:
            board.push(move)
            epds.append(board.epd())

        for epd in reversed(epds):
            found = self.by_epd.get(epd)
            if found is not None:
                return found
        return None

    def search(self, query: str, limit: int = 25) -> list[Opening]:
        """Find openings by (case-insensitive) name fragment."""
        needle = query.strip().lower()
        if not needle:
            return []
        hits = [o for o in self.by_name.values() if needle in o.name.lower()]
        hits.sort(key=lambda o: (len(o.moves), o.name))
        return hits[:limit]

    def families(self) -> list[str]:
        return sorted({o.family for o in self.openings})

    # -- playing -----------------------------------------------------------

    def continuations(self, board: chess.Board) -> list[BookMove]:
        """Book replies in this position, most-played first."""
        candidates = self.tree.get(board.epd(), [])
        return [bm for bm in candidates if bm.move in board.legal_moves]

    def pick(self, board: chess.Board, rng: random.Random | None = None) -> chess.Move | None:
        """Sample a book move weighted by how many named lines use it.

        Weighted rather than "always the main line" so a drill session doesn't
        replay an identical game every time.
        """
        options = self.continuations(board)
        if not options:
            return None
        rng = rng or random
        total = sum(bm.weight for bm in options) or len(options)
        roll = rng.uniform(0, total)
        running = 0.0
        for bm in options:
            running += bm.weight or 1
            if roll <= running:
                return bm.move
        return options[0].move


def _parse_line(pgn: str) -> list[chess.Move]:
    board = chess.Board()
    moves: list[chess.Move] = []
    for token in pgn.split():
        if token.endswith(".") or token[0].isdigit() and "." in token:
            continue
        try:
            move = board.parse_san(token)
        except ValueError:
            return []
        moves.append(move)
        board.push(move)
    return moves


# --------------------------------------------------------------------------
# Choosing the bot's reply
# --------------------------------------------------------------------------


def book_move_for(
    board: chess.Board,
    book: OpeningBook,
    mode: str,
    line: Opening | None = None,
    drill_plies: int = 12,
    rng: random.Random | None = None,
) -> chess.Move | None:
    """The bot's book reply, or ``None`` to hand over to Maia.

    * ``STRICT``     — follow the book for as long as it has a move.
    * ``DRILL``      — follow ``line`` exactly for ``drill_plies``, so the
      player gets repetitions of the variation they are studying, then play on
      naturally.
    * ``REALISTIC``  — no book: Maia at the player's rating already plays the
      openings real players at that rating play, deviations included.
    """
    if mode == BookMode.REALISTIC:
        return None

    if mode == BookMode.DRILL:
        if line is None:
            return None
        ply = len(board.move_stack)
        if ply >= min(drill_plies, len(line.moves)):
            return None
        # Only stay on the rails while the game has actually followed the line.
        if list(board.move_stack) != list(line.moves[:ply]):
            return None
        candidate = line.moves[ply]
        return candidate if candidate in board.legal_moves else None

    return book.pick(board, rng=rng)


def deviation_ply(board: chess.Board, line: Opening) -> int | None:
    """First ply at which the game left ``line``, or None if still on it."""
    for index, played in enumerate(board.move_stack):
        if index >= len(line.moves):
            return index
        if played != line.moves[index]:
            return index
    return None
