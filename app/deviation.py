"""Why the repertoire plays *this* move and not the one you played.

Reps used to end the moment you left the line, tell you the book move, and — if
the engine agreed the two were close — add that it was "not really a mistake,
just not this repertoire". That is the least useful thing a trainer can say. It
is true, it is unarguable, and it teaches nothing: you leave knowing that your
move was fine and that some book somewhere prefers another one.

What a drill owes you instead is the *reason*, and then the chance to play the
right move yourself. So this module assembles everything concrete that can be
said about the fork in the road:

* what each move does to the position (develops, castles, takes, gives check,
  what it starts attacking in the centre),
* what the engine thinks of each, in winning chances rather than centipawns,
* whether your move drops material, and to which reply,
* whether your move is itself theory — it often is, and naming the opening it
  becomes is far more honest than calling it a mistake — and *how much* theory,
  because the catalogue names curiosities as readily as main lines and "1.Nh3
  is the Amar Opening" must never be handed over as a recommendation,
* whether it merely reorders the line and reaches the very same position a move
  later, which is the commonest reason a drill "breaks" and the one thing it
  would be actively wrong to call an error,
* where the line was going next.

Those facts are the whole input to the explanation. The language model writes
the prose and is allowed no chess of its own (see :mod:`app.coach`); if it is
unavailable, :func:`template_deviation` renders the same facts directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

from .engines import Stockfish
from .eval import eval_win_percent
from .motifs import facts_for_move
from .openings import OpeningBook

#: Squares worth naming as "the centre" when a move starts attacking one.
CENTRE = ("d4", "e4", "d5", "e5")

#: Winning-chance points below which two moves are simply alternatives. Opening
#: choices are rarely separated by more than a few points, so this is not the
#: review's blunder threshold — it is "the engine has a real preference".
WORSE_BY = 6.0

#: Named lines a move needs behind it before being called theory without
#: qualification. Being in the catalogue means only that somebody named it: the
#: Amar Opening (1.Nh3) is in there, and a move with one line through it is a
#: curiosity, not an alternative repertoire.
MAINSTREAM = 8

PIECE_NAMES = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}


@dataclass(frozen=True)
class MoveNote:
    """What one move does, in facts rather than adjectives."""

    san: str
    uci: str
    piece: str
    from_square: str
    to_square: str
    castles: bool = False
    develops: bool = False        # a minor piece leaving its starting square
    captures: str | None = None
    gives_check: bool = False
    attacks_centre: list[str] = field(default_factory=list)
    opening: str | None = None    # what the position is called after this move
    in_book: bool = False         # some named line plays this here
    weight: int = 0               # named lines running through it
    reply: list[str] = field(default_factory=list)   # engine's best answer, SAN
    win_pct: float | None = None  # winning chances for the player, after it
    costs: list[str] = field(default_factory=list)   # motif statements
    material: int = 0             # centipawns this move drops, 0 if none

    def purpose(self) -> str:
        """A clause saying what the move is for: "developing the knight"."""
        if self.castles:
            return "getting the king to safety"
        if self.captures:
            return f"taking the {self.captures} on {self.to_square}"
        if self.develops:
            return f"developing the {self.piece}"
        if self.piece == "pawn" and self.attacks_centre:
            return f"taking a grip on {' and '.join(self.attacks_centre)}"
        if self.attacks_centre:
            return f"aiming the {self.piece} at {' and '.join(self.attacks_centre)}"
        return f"bringing the {self.piece} to {self.to_square}"


@dataclass(frozen=True)
class Deviation:
    """The comparison between the move you played and the move of the line."""

    ply: int
    line_name: str
    book: MoveNote
    played: MoveNote
    remaining: list[str]          # the rest of the line, in SAN
    severity: str                 # material | worse | transposes | sideline | playable
    transposition: list[str] = field(default_factory=list)  # order that rejoins

    @property
    def cost(self) -> float | None:
        if self.book.win_pct is None or self.played.win_pct is None:
            return None
        return self.book.win_pct - self.played.win_pct


def transposes(board: chess.Board, played: chess.Move,
               tail: list[chess.Move]) -> list[str]:
    """Does playing ``played`` now just reach the same position a move later?

    The commonest way a drill breaks is not an error at all — it is castling on
    move four instead of move five. If the move the player chose appears further
    down the line, try it first and play the rest in order; if that arrives at
    exactly the position the line arrives at, the two orders are the same thing
    and saying otherwise would be false.

    Returns the reordered line in SAN, or ``[]`` if it does not rejoin.
    """
    if played in tail[:1] or played not in tail:
        return []

    # Only the player's own slots can be reordered. Pulling a move of theirs
    # forward past one of the opponent's would put two moves of the same colour
    # in a row, which is not a transposition — it is not even a game.
    index = tail.index(played)
    if index % 2:
        return []
    order = list(tail)
    order[0], order[index] = order[index], order[0]

    target = board.copy()
    for move in tail:
        target.push(move)

    walker = board.copy()
    san: list[str] = []
    for move in order:
        if move not in walker.legal_moves:
            return []
        san.append(walker.san(move))
        walker.push(move)

    return san if walker.board_fen() == target.board_fen() else []


def _note(board: chess.Board, move: chess.Move, book: OpeningBook) -> MoveNote:
    """Everything derivable about a move without an engine."""
    piece = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    if captured is None and board.is_en_passant(move):
        captured = chess.Piece(chess.PAWN, not board.turn)

    after = board.copy()
    after.push(move)

    back_rank = 0 if board.turn == chess.WHITE else 7
    develops = (
        piece is not None
        and piece.piece_type in (chess.KNIGHT, chess.BISHOP)
        and chess.square_rank(move.from_square) == back_rank
    )
    attacks = [
        name for name in CENTRE
        if chess.parse_square(name) in after.attacks(move.to_square)
    ]

    # classify() walks backwards to the last *named* position, so after a move
    # nobody plays it happily returns the name of the position before it. Only
    # a name that lands on this exact position says the move is theory.
    named = book.classify(after.move_stack)
    if named is not None and len(named.moves) != len(after.move_stack):
        named = None

    return MoveNote(
        san=board.san(move),
        uci=move.uci(),
        piece=PIECE_NAMES[piece.piece_type] if piece else "piece",
        from_square=chess.square_name(move.from_square),
        to_square=chess.square_name(move.to_square),
        castles=board.is_castling(move),
        develops=develops,
        captures=PIECE_NAMES[captured.piece_type] if captured else None,
        gives_check=after.is_check(),
        attacks_centre=attacks,
        opening=named.name if named else None,
        in_book=any(c.move == move for c in book.continuations(board)),
        weight=next((c.weight for c in book.continuations(board)
                     if c.move == move), 0),
    )


async def _with_engine(
    note: MoveNote, board: chess.Board, move: chess.Move,
    stockfish: Stockfish, player_white: bool, depth: int,
) -> MoveNote:
    """Add the engine's verdict and the refutation to a note."""
    after = board.copy()
    after.push(move)
    lines = await stockfish.analyse(after, depth=depth, multipv=1)
    if not lines:
        return note

    best = lines[0]
    reply = best.pv_san(after, limit=4)
    findings = facts_for_move(board, move, refutation=best.pv[:4])
    return MoveNote(
        **{
            **note.__dict__,
            "reply": reply,
            "win_pct": round(eval_win_percent(best.score.pov(player_white)), 1),
            "costs": [f.phrase for f in findings],
            "material": max((f.material for f in findings), default=0),
        }
    )


async def analyse_deviation(
    board: chess.Board,
    book_move: chess.Move,
    played_move: chess.Move,
    line_name: str,
    tail: list[chess.Move],
    book: OpeningBook,
    stockfish: Stockfish | None,
    depth: int = 12,
) -> Deviation:
    """Compare the two moves at the fork. Works without an engine, less well."""
    ply = len(board.move_stack)
    player_white = board.turn == chess.WHITE
    book_note = _note(board, book_move, book)
    played_note = _note(board, played_move, book)

    if stockfish is not None:
        book_note = await _with_engine(
            book_note, board, book_move, stockfish, player_white, depth)
        played_note = await _with_engine(
            played_note, board, played_move, stockfish, player_white, depth)

    reordered = transposes(board, played_move, tail)

    severity = "playable"
    if played_note.material >= 100:
        severity = "material"
    elif (book_note.win_pct is not None and played_note.win_pct is not None
          and book_note.win_pct - played_note.win_pct >= WORSE_BY):
        severity = "worse"
    elif reordered:
        severity = "transposes"
    elif (played_note.in_book and played_note.weight < MAINSTREAM
          and book_note.weight >= MAINSTREAM):
        # in_book is load-bearing: without it a move no line plays at all has
        # weight 0 and would be described as an obscure *catalogued* choice.
        severity = "sideline"

    walker = board.copy()
    remaining = []
    for move in tail:
        remaining.append(walker.san(move))
        walker.push(move)

    return Deviation(
        ply=ply,
        line_name=line_name,
        book=book_note,
        played=played_note,
        remaining=remaining[1:],
        severity=severity,
        transposition=reordered,
    )


# --------------------------------------------------------------------------
# Prose without a model
# --------------------------------------------------------------------------


def template_deviation(dev: Deviation) -> str:
    """The explanation rendered from facts alone.

    This is what shows when the coach model is down, so it has to stand on its
    own rather than read as a stub. Three cases, because there are genuinely
    three different things that can have happened.
    """
    played, bookm = dev.played, dev.book
    out: list[str] = []

    if dev.severity == "material":
        out.append(played.costs[0] if played.costs
                   else f"{played.san} gives material away for nothing.")
        if played.reply:
            out.append(f"The reply is {played.reply[0]}.")
        out.append(f"The line plays {bookm.san}, {bookm.purpose()}.")
    elif dev.severity == "worse":
        out.append(
            f"{played.san} is not losing, but the engine puts you about "
            f"{abs(dev.cost):.0f} points of winning chances worse off than after "
            f"{bookm.san}."
        )
        if played.reply:
            out.append(f"It expects {' '.join(played.reply[:2])} in reply.")
        out.append(f"{bookm.san} keeps more, {bookm.purpose()}.")
    elif dev.severity == "transposes":
        out.append(
            f"{played.san} is the same idea in a different order: "
            f"{' '.join(dev.transposition)} reaches exactly the position the "
            f"line reaches, so nothing is lost."
        )
        out.append(
            f"It is worth drilling the order the repertoire uses, though, so "
            f"that you play it without having to work it out."
        )
    elif dev.severity == "sideline":
        named = f", the {played.opening}," if played.opening else ""
        out.append(
            f"{played.san}{named} is catalogued, but hardly anyone plays it: "
            f"{played.weight} named line{'' if played.weight == 1 else 's'} "
            f"run{'s' if played.weight == 1 else ''} through it against "
            f"{bookm.weight} for {bookm.san}."
        )
        out.append(
            f"You will almost never meet it, so it is not worth the space in "
            f"your head. The line plays {bookm.san}, {bookm.purpose()}."
        )
    elif played.in_book:
        named = f" — the {played.opening}" if played.opening else ""
        out.append(
            f"{played.san} is theory too{named}, so this is a different opening "
            f"rather than a mistake."
        )
        out.append(f"This drill is the {dev.line_name}, which plays {bookm.san} "
                   f"here, {bookm.purpose()}.")
    else:
        out.append(
            f"No catalogued line plays {played.san} here, so from this point "
            f"you would be out of book and working it out over the board."
        )
        out.append(f"The line plays {bookm.san}, {bookm.purpose()}.")

    # The transposition branch has already shown a move order; a second one
    # immediately afterwards reads as a contradiction rather than a plan.
    if dev.remaining and dev.severity != "transposes":
        out.append(f"It then goes {' '.join(dev.remaining[:4])}.")
    out.append(f"Take it back and play {bookm.san}.")
    return " ".join(out)
