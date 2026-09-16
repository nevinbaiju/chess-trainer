"""Stockfish, as a supervised long-lived UCI subprocess.

Stockfish is the *judge*, never the opponent. Its floor is far above this app's
target player — the binary reports ``option name UCI_Elo type spin default 1320
min 1320 max 3190``, so even fully throttled it plays around 1320, roughly
double a 600-rated chess.com rapid rating. Weakening it further with
``Skill Level`` produces long stretches of perfect play punctuated by arbitrary
throwaways, which teaches nothing. Maia-3 is the opponent; this is the analyst.

Unlike Maia, Stockfish genuinely searches for seconds at a time, so it gets the
full subprocess treatment: a lock, a liveness check, and automatic restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass

import chess
import chess.engine

from .eval import Eval

log = logging.getLogger(__name__)

STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")

#: Sized for a home server whose cores are shared with whatever else it runs.
#: Two search threads keeps a full game review under a minute without the fans
#: spinning up or other services stalling.
DEFAULT_THREADS = int(os.environ.get("STOCKFISH_THREADS", "2"))
DEFAULT_HASH_MB = int(os.environ.get("STOCKFISH_HASH_MB", "256"))

#: Depth 18 is well past the point where a 600-level review changes its mind,
#: and cheap enough to run over a whole game interactively.
DEFAULT_DEPTH = int(os.environ.get("STOCKFISH_DEPTH", "18"))


@dataclass(frozen=True)
class Line:
    """One principal variation."""

    rank: int                 # 1 = best
    score: Eval               # always White-relative
    pv: tuple[chess.Move, ...]
    depth: int

    @property
    def best_move(self) -> chess.Move | None:
        return self.pv[0] if self.pv else None

    def pv_san(self, board: chess.Board, limit: int = 6) -> list[str]:
        """Render the PV in SAN from ``board``, for display and for the coach."""
        out: list[str] = []
        walker = board.copy()
        for move in self.pv[:limit]:
            if move not in walker.legal_moves:
                break  # engines occasionally emit a stale PV tail
            out.append(walker.san(move))
            walker.push(move)
        return out


class Stockfish:
    """A single supervised Stockfish process."""

    def __init__(
        self,
        path: str = STOCKFISH_PATH,
        threads: int = DEFAULT_THREADS,
        hash_mb: int = DEFAULT_HASH_MB,
        depth: int = DEFAULT_DEPTH,
        cache_size: int = 20_000,
    ):
        self.path = path
        self.threads = threads
        self.hash_mb = hash_mb
        self.depth = depth
        self._engine: chess.engine.UciProtocol | None = None
        self._transport = None
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[str, int, int], list[Line]] = OrderedDict()
        self._cache_size = cache_size
        self.id: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            await self._spawn()

    async def _spawn(self) -> None:
        self._transport, self._engine = await chess.engine.popen_uci(self.path)
        await self._engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        self.id = dict(self._engine.id)
        log.info("started %s (threads=%d hash=%dMB)", self.id.get("name", "stockfish"), self.threads, self.hash_mb)

    async def close(self) -> None:
        async with self._lock:
            if self._engine is not None:
                try:
                    await self._engine.quit()
                except Exception:  # noqa: BLE001 - shutting down regardless
                    pass
                self._engine = None

    async def _ensure_alive(self) -> chess.engine.UciProtocol:
        if self._engine is None:
            await self._spawn()
        return self._engine  # type: ignore[return-value]

    # -- analysis ----------------------------------------------------------

    async def analyse(
        self,
        board: chess.Board,
        depth: int | None = None,
        multipv: int = 1,
    ) -> list[Line]:
        """Analyse ``board``, returning ``multipv`` lines ranked best-first.

        Results are memoised on (position, depth, multipv). A game review
        analyses the position before and after every move, and the position
        after move *n* is the position before move *n+1*, so the cache roughly
        halves the engine work for free.
        """
        depth = depth or self.depth
        key = (board.epd(), depth, multipv)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        async with self._lock:
            lines = await self._analyse_locked(board, depth, multipv)

        self._cache[key] = lines
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return lines

    async def _analyse_locked(
        self, board: chess.Board, depth: int, multipv: int
    ) -> list[Line]:
        limit = chess.engine.Limit(depth=depth)
        try:
            engine = await self._ensure_alive()
            infos = await engine.analyse(board, limit, multipv=multipv)
        except chess.engine.EngineTerminatedError:
            # Crashed mid-search. Respawn once and retry; if it dies again the
            # exception propagates and the caller can surface a real error.
            log.warning("stockfish terminated unexpectedly; restarting")
            self._engine = None
            engine = await self._ensure_alive()
            infos = await engine.analyse(board, limit, multipv=multipv)

        if isinstance(infos, dict):  # multipv=1 returns a bare InfoDict
            infos = [infos]

        lines: list[Line] = []
        for rank, info in enumerate(infos, start=1):
            score = info.get("score")
            if score is None:
                continue
            lines.append(
                Line(
                    rank=rank,
                    score=Eval.from_pov_score(score),
                    pv=tuple(info.get("pv", ())),
                    depth=int(info.get("depth", depth)),
                )
            )
        return lines

    async def find_mate(
        self, board: chess.Board, max_moves: int = 3, depth: int = 12
    ) -> int | None:
        """Forced mate for the side to move, in ``N`` moves, or None.

        Only reports mates at or under ``max_moves``. A deeper mate is not a
        useful hint for a beginner — being told "there is a mate in 7 here"
        is discouraging rather than instructive, because finding it is not a
        realistic ask.

        Depth 12 is far past the 5 plies a mate in 3 needs, so this never
        misses one, and it costs tens of milliseconds rather than the seconds
        a review search takes.
        """
        if board.is_game_over():
            return None
        lines = await self.analyse(board, depth=depth, multipv=1)
        if not lines:
            return None
        pov = lines[0].score.pov(board.turn == chess.WHITE)
        if pov.mate is None or pov.mate <= 0:
            return None
        return pov.mate if pov.mate <= max_moves else None

    async def evaluate(self, board: chess.Board, depth: int | None = None) -> Eval:
        """Just the evaluation of a position, White-relative."""
        if board.is_checkmate():
            # Side to move is mated. Mate-in-0 against them.
            return Eval(mate=-1 if board.turn == chess.WHITE else 1)
        if board.is_game_over():
            return Eval(cp=0)  # stalemate, repetition, insufficient material
        lines = await self.analyse(board, depth=depth, multipv=1)
        return lines[0].score if lines else Eval(cp=0)
