"""Maia-3: the opponent, and the model of "what would someone at my level play?"

Why this is in-process while Stockfish is a subprocess
------------------------------------------------------
The plan called for both engines to run behind UCI. Reading maia3's
``uci.py`` ruled that out for Maia: ``score_moves()`` computes a ``policy``
probability for every candidate move and then ``cmd_go`` **throws it away**,
printing only ``score cp`` derived from the WDL head::

    info depth 1 multipv 1 score cp 214 wdl 607 200 193 pv e2e4

That discarded probability — *how many players at your rating would play this
move* — is the single most valuable thing Maia gives us. It is what separates
"you blundered" from "you fell into a trap that 38% of players at your level
fall into, and here is the pattern". Over plain UCI we could only recover the
*ranking* of the top moves, not the mass.

The isolation a subprocess buys is also worth much less here than for
Stockfish: Maia performs exactly one forward pass with no search, so there is
no long-running computation to interrupt, time out, or wedge. Stockfish, which
really does search for seconds at a time, stays a subprocess.

Practical consequence: this module imports torch, so it only works inside the
container. Keep it out of import paths that must run on the host.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import chess

# maia3 (and torch) only exist inside the container. Import lazily so the pure
# modules — eval, see, phases — stay testable on the host's Python 3.14.
try:  # pragma: no cover - exercised only in the container
    import torch
    from maia3.dataset import get_legal_moves_mask
    from maia3.uci import Maia3UCIEngine, parse_args, sample_from_logits

    MAIA_AVAILABLE = True
except ImportError:  # pragma: no cover
    MAIA_AVAILABLE = False
    torch = None  # type: ignore
    Maia3UCIEngine = object  # type: ignore


DEFAULT_MODEL = "maia3-5m"

#: Maia-3 is conditioned on the **Lichess** rating scale and was trained over
#: roughly 600-2600. chess.com rapid runs materially lower than Lichess at the
#: beginner end, so a chess.com 550 is somewhere near Lichess 1000.
MIN_ELO, MAX_ELO = 600, 2600


@dataclass(frozen=True)
class MoveOdds:
    """How a human at a given rating sees one move."""

    move: chess.Move
    policy: float          # P(a player at self_elo plays this move), 0-1
    rank: int              # 1 = most likely human move

    @property
    def percent(self) -> float:
        return self.policy * 100


@dataclass(frozen=True)
class HumanRead:
    """Maia's whole view of a position, from the side to move's perspective."""

    odds: list[MoveOdds]                 # every legal move, most likely first
    win: float                           # human-perceived W/D/L, 0-1 each
    draw: float
    loss: float

    def by_move(self) -> dict[chess.Move, MoveOdds]:
        return {o.move: o for o in self.odds}

    def probability_of(self, move: chess.Move) -> float:
        for o in self.odds:
            if o.move == move:
                return o.policy
        return 0.0

    def rank_of(self, move: chess.Move) -> int | None:
        for o in self.odds:
            if o.move == move:
                return o.rank
        return None

    @property
    def top(self) -> MoveOdds | None:
        return self.odds[0] if self.odds else None


def _position_command(board: chess.Board) -> str:
    """Render a board as a UCI ``position`` line, preserving its move history.

    Maia conditions on up to 8 previous positions, so the move stack is not
    optional — handing it a bare FEN silently degrades its play. Anything that
    passes ``board.copy(stack=False)`` here is a bug.
    """
    root = board.root()
    head = (
        "position startpos"
        if root.fen() == chess.STARTING_FEN
        else f"position fen {root.fen()}"
    )
    if board.move_stack:
        head += " moves " + " ".join(m.uci() for m in board.move_stack)
    return head


class _Core(Maia3UCIEngine):  # type: ignore[misc]
    """Upstream's engine object, extended to expose the full policy.

    Subclassing rather than reimplementing keeps tokenisation, board mirroring
    for Black, history padding and checkpoint loading on upstream's code.
    """

    def full_read(self, self_elo: int, oppo_elo: int) -> HumanRead:
        if self.board.is_game_over():
            return HumanRead(odds=[], win=0.0, draw=0.0, loss=0.0)

        legal_mask = get_legal_moves_mask(self.board, self.all_moves_dict)
        if not bool(legal_mask.any()):
            return HumanRead(odds=[], win=0.0, draw=0.0, loss=0.0)

        with torch.no_grad():
            tokens = self._tokens_from_history(self.history)
            tokens = tokens.unsqueeze(0).to(self.cfg.device)
            selves = torch.tensor([self_elo], dtype=torch.long, device=self.cfg.device)
            oppos = torch.tensor([oppo_elo], dtype=torch.long, device=self.cfg.device)
            move_logits, value_logits, _ = self.model(tokens, selves, oppos)

            logits = move_logits[0].float()
            logits = logits.masked_fill(~legal_mask.to(self.cfg.device), float("-inf"))
            probs = torch.softmax(logits, dim=-1)

            n_legal = int(legal_mask.sum().item())
            top_probs, top_idxs = torch.topk(probs, k=n_legal)

            # Value head labels are [loss, draw, win] for the side to move.
            loss_p, draw_p, win_p = torch.softmax(value_logits[0].float(), dim=-1).tolist()

        odds: list[MoveOdds] = []
        for prob, idx in zip(top_probs.tolist(), top_idxs.tolist()):
            move = self._move_from_index(idx)
            if move is not None:
                odds.append(MoveOdds(move=move, policy=float(prob), rank=len(odds) + 1))

        return HumanRead(odds=odds, win=win_p, draw=draw_p, loss=loss_p)

    def sample(self, self_elo: int, oppo_elo: int, temperature: float, top_p: float):
        """Pick a move the way a human at ``self_elo`` plausibly would."""
        if self.board.is_game_over():
            return None
        legal_mask = get_legal_moves_mask(self.board, self.all_moves_dict)
        if not bool(legal_mask.any()):
            return None

        with torch.no_grad():
            tokens = self._tokens_from_history(self.history)
            tokens = tokens.unsqueeze(0).to(self.cfg.device)
            selves = torch.tensor([self_elo], dtype=torch.long, device=self.cfg.device)
            oppos = torch.tensor([oppo_elo], dtype=torch.long, device=self.cfg.device)
            move_logits, _, _ = self.model(tokens, selves, oppos)
            logits = move_logits[0].float()
            logits = logits.masked_fill(~legal_mask.to(self.cfg.device), float("-inf"))
            idx = sample_from_logits(logits, temperature, top_p)

        return self._move_from_index(idx)


class Maia:
    """Thread-safe handle on one loaded Maia-3 network.

    Inference is synchronous and CPU-bound, so every public method is async and
    hops to a worker thread. A single lock serialises access because the
    underlying engine object carries mutable board/history state.
    """

    def __init__(self, model: str = DEFAULT_MODEL, device: str = "cpu"):
        if not MAIA_AVAILABLE:
            raise RuntimeError(
                "maia3/torch are not importable. This module only runs inside "
                "the container image."
            )
        cfg = parse_args(
            [
                "--model", model,
                "--device", device,
                "--no-use-amp",
                # Feed Maia the real move history rather than the current
                # position repeated 8 times. Upstream defaults this OFF.
                "--use-uci-history",
            ]
        )
        self._core = _Core(cfg)
        self._core.ensure_model_loaded()
        self._lock = threading.Lock()
        self.model_name = model

    # -- internals ---------------------------------------------------------

    def _with_position(self, board: chess.Board, fn):
        with self._lock:
            self._core.cmd_position(_position_command(board))
            return fn()

    @staticmethod
    def clamp_elo(elo: int) -> int:
        """Keep conditioning inside the range Maia-3 was actually trained on."""
        return max(MIN_ELO, min(MAX_ELO, int(elo)))

    # -- public API --------------------------------------------------------

    def read_sync(self, board: chess.Board, self_elo: int, oppo_elo: int | None = None) -> HumanRead:
        self_elo = self.clamp_elo(self_elo)
        oppo_elo = self.clamp_elo(oppo_elo if oppo_elo is not None else self_elo)
        return self._with_position(board, lambda: self._core.full_read(self_elo, oppo_elo))

    def play_sync(
        self,
        board: chess.Board,
        self_elo: int,
        oppo_elo: int | None = None,
        temperature: float = 1.0,
        top_p: float = 1.0,
    ) -> chess.Move | None:
        self_elo = self.clamp_elo(self_elo)
        oppo_elo = self.clamp_elo(oppo_elo if oppo_elo is not None else self_elo)
        return self._with_position(
            board, lambda: self._core.sample(self_elo, oppo_elo, temperature, top_p)
        )

    async def read(self, board: chess.Board, self_elo: int, oppo_elo: int | None = None) -> HumanRead:
        return await asyncio.to_thread(self.read_sync, board, self_elo, oppo_elo)

    async def play(
        self,
        board: chess.Board,
        self_elo: int,
        oppo_elo: int | None = None,
        temperature: float = 1.0,
        top_p: float = 1.0,
    ) -> chess.Move | None:
        return await asyncio.to_thread(
            self.play_sync, board, self_elo, oppo_elo, temperature, top_p
        )
