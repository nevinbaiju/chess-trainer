"""Choosing the next puzzle from what the player actually gets wrong.

The selection rule is the feature. A puzzle trainer serves puzzles; this serves
the puzzle you are most likely to need, by reading the same weakness ranking the
Review tab shows and drawing from it in proportion.

Weighted rather than "always the top one" for two reasons: the tail still needs
practice, and the mix re-balances itself as the stats move — fix your hanging
pieces and hanging-piece puzzles quietly become rarer without anyone editing a
setting.

The player is never told the theme before solving. "This is a fork" hands over
the answer, and it would also be a lie for the seven motifs whose mapping is
inverted: a fork puzzle trains you to *play* forks, not to avoid them. So the
prompt is always "find the best move", and the motif appears afterwards as
feedback. See :mod:`app.puzzle_themes`.
"""

from __future__ import annotations

import random

import chess

from .puzzle_themes import MOTIF_THEMES, ThemeMap, themes_for


def weighted_motifs(weaknesses: list[dict]) -> list[tuple[str, int]]:
    """(motif, weight) for the weaknesses we have puzzles for.

    The weight is *games affected*, which is what the stats already rank by —
    one pathological game showing the same motif twenty times must not drown
    out a habit that shows up once in every game.
    """
    return [
        (w["motif"], max(1, int(w.get("games", 0))))
        for w in weaknesses
        if w.get("motif") in MOTIF_THEMES and w.get("games", 0) > 0
    ]


def pick_motif(weaknesses: list[dict], rng: random.Random | None = None) -> str | None:
    """Choose which weakness to drill, in proportion to how often it costs games."""
    weighted = weighted_motifs(weaknesses)
    if not weighted:
        return None
    rng = rng or random
    motifs, weights = zip(*weighted)
    return rng.choices(motifs, weights=weights, k=1)[0]


def pick_theme(motif: str, rng: random.Random | None = None) -> str | None:
    themes = themes_for(motif)
    if not themes:
        return None
    return (rng or random).choice(list(themes))


def describe(motif: str) -> ThemeMap | None:
    return MOTIF_THEMES.get(motif)


# --------------------------------------------------------------------------
# Playing one out
# --------------------------------------------------------------------------


def opening_position(fen: str, moves: list[str]) -> tuple[chess.Board, str]:
    """The position the player is shown, and the move that produced it.

    Lichess stores the FEN *before* the opponent's move, and the first entry of
    the move list is that move. So the puzzle proper starts one ply in — miss
    this and every puzzle is presented a move early, from the wrong side, with
    the solution looking illegal.
    """
    board = chess.Board(fen)
    first = chess.Move.from_uci(moves[0])
    board.push(first)
    return board, moves[0]


def your_plies(moves: list[str]) -> list[int]:
    """Indices of the moves the player has to find: 1, 3, 5 ..."""
    return list(range(1, len(moves), 2))


def is_solved(moves: list[str], played: int) -> bool:
    """Has the player made every move the solution asks for?"""
    return played >= len(moves)


def solution_line(fen: str, moves: list[str]) -> list[dict]:
    """Every position the solution passes through.

    Handing the front end a FEN per ply means it can animate the line and then
    step back and forth through it without needing any chess rules of its own —
    the board only ever sets a position it was given.
    """
    board, _ = opening_position(fen, moves)
    out = []
    for index, uci in enumerate(moves[1:]):
        move = chess.Move.from_uci(uci)
        san = board.san(move)
        board.push(move)
        out.append({
            "uci": uci,
            "san": san,
            "fen": board.fen(),
            "yours": index % 2 == 0,     # the solution starts with your move
        })
    return out
