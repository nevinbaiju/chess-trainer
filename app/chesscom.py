"""Importing your real games from chess.com.

The point of this is comparison. The trainer knows a great deal about the games
you play against Maia and nothing at all about the games that actually decide
your rating, and those are the ones worth fixing. Pulling them in and running
them through the *same* review pipeline puts both in one metric space: the same
beginner-calibrated win% curve, the same motif detectors, the same accuracy
formula. "You hang a piece in 15 of 17 trainer games" only means something next
to how often you do it for real.

Four things about this API, each verified against it rather than the docs:

* **An empty User-Agent is a 403.** Confirmed live: no header, no data. chess.com
  also ask that it carry contact details, which is what ``CHESSCOM_USER_AGENT``
  is for.
* **Use the JSON month endpoint, not ``/pgn``.** It carries ``accuracies``,
  ``time_class``, ``rated``, per-side ratings *and* the full PGN string, so the
  PGN endpoint gives strictly less for the same request.
* **Closed months never change.** They are fetched once and revalidated by
  ETag afterwards; only the current month is worth re-polling.
* **Serial requests only.** chess.com's own guidance is that sequential access
  is unlimited while parallel access may be throttled, so there is one lock and
  no gather().
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import chess
import chess.pgn
import httpx

from .openings import OpeningBook

log = logging.getLogger(__name__)

BASE = "https://api.chess.com/pub"

#: chess.com answer 403 to an empty User-Agent — verified live — and ask that
#: it carry a way to contact you. Set CHESSCOM_USER_AGENT to your own address
#: before running this against their API in anger; the default identifies the
#: software but can tell them nothing about who is calling.
USER_AGENT = os.environ.get(
    "CHESSCOM_USER_AGENT",
    "chess-trainer/0.1 (self-hosted; set CHESSCOM_USER_AGENT to your contact)",
)
TIMEOUT = float(os.environ.get("CHESSCOM_TIMEOUT", "30"))

#: Per-side result words that mean the game was drawn. Anything else that is
#: not "win" is a loss for that side.
DRAWN = {
    "agreed", "repetition", "stalemate", "insufficient",
    "50move", "timevsinsufficient",
}

#: Time classes worth importing by default. Blitz and bullet mistakes are
#: mostly clock artifacts; counting them would drown the weakness ranking in
#: errors that better chess would not have prevented.
DEFAULT_TIME_CLASSES = ("rapid",)


@dataclass
class Archive:
    """One month of games."""

    url: str
    games: list[dict]
    etag: str | None
    unchanged: bool = False


class ChessCom:
    """A polite, serial client. One request at a time, always identified."""

    def __init__(self, user_agent: str = USER_AGENT):
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT, headers=self.headers)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def profile(self, username: str) -> dict | None:
        client = await self._http()
        response = await client.get(f"{BASE}/player/{username.lower()}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def archives(self, username: str) -> list[str]:
        """Every month this player has games in, oldest first."""
        client = await self._http()
        response = await client.get(f"{BASE}/player/{username.lower()}/games/archives")
        if response.status_code == 404:
            raise LookupError(f"chess.com has no player called {username!r}")
        response.raise_for_status()
        return response.json().get("archives", [])

    async def month(self, url: str, etag: str | None = None) -> Archive:
        """One month, revalidated by ETag so closed months cost nothing."""
        client = await self._http()
        headers = {"If-None-Match": etag} if etag else {}
        response = await client.get(url, headers=headers)
        if response.status_code == 304:
            return Archive(url=url, games=[], etag=etag, unchanged=True)
        response.raise_for_status()
        return Archive(
            url=url,
            games=response.json().get("games", []),
            etag=response.headers.get("etag"),
        )


# --------------------------------------------------------------------------
# One API entry -> one row in our games table
# --------------------------------------------------------------------------


def wanted(entry: dict, time_classes: tuple[str, ...] = DEFAULT_TIME_CLASSES) -> bool:
    """Is this a rated standard game in a format we are importing?"""
    return (
        entry.get("rules") == "chess"
        and entry.get("rated") is True
        and entry.get("time_class") in time_classes
    )


def convert(entry: dict, username: str, book: OpeningBook | None = None) -> dict | None:
    """Turn one API entry into fields for ``Database.create_game``.

    Returns None for a game we cannot use — no moves recorded, or a username
    that does not appear on either side, which would leave us unable to say
    which colour the player had.
    """
    hero = username.lower()
    white = entry.get("white", {})
    black = entry.get("black", {})
    if white.get("username", "").lower() == hero:
        color, us, them = "white", white, black
    elif black.get("username", "").lower() == hero:
        color, us, them = "black", black, white
    else:
        return None

    game = chess.pgn.read_game(io.StringIO(entry.get("pgn", "")))
    if game is None:
        return None
    moves = list(game.mainline_moves())
    if not moves:
        return None      # abandoned before a move was played; nothing to review

    board = chess.Board()
    for move in moves:
        board.push(move)

    ended = datetime.fromtimestamp(entry["end_time"], tz=timezone.utc).isoformat()
    fields = {
        "source": "chesscom",
        "external_id": entry["uuid"],
        "created_at": ended,
        "finished_at": ended,
        "player_color": color,
        "result": game.headers.get("Result", "*"),
        "termination": _termination(us.get("result"), them.get("result")),
        "moves": " ".join(m.uci() for m in moves),
        "pgn": entry.get("pgn"),
        "player_rating": us.get("rating"),
        "opponent_rating": them.get("rating"),
        "mode": entry.get("time_class"),
    }

    # Classified with our own book rather than chess.com's ECO tag, so an
    # imported game and a trainer game are named by the same rules and group
    # together in the stats.
    if book is not None:
        opening = book.classify(moves)
        if opening is not None:
            fields["opening_eco"] = opening.eco
            fields["opening_name"] = opening.family
    fields.setdefault("opening_eco", game.headers.get("ECO"))
    return fields


def _termination(ours: str | None, theirs: str | None) -> str:
    """How the game ended, from the losing or drawing side's own word for it."""
    if ours in DRAWN or theirs in DRAWN:
        return ours if ours in DRAWN else str(theirs)
    return ours if ours != "win" else str(theirs)
