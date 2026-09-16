"""Natural-language coaching, with the chess kept strictly outside the model.

The rule here is load-bearing, not stylistic. Measured on chess commentary
(arXiv 2608.04240), an LLM asked to judge move quality scores **12.5 F1**; the
same model handed an engine's verdict scores **97.6**. Overall false-claim rate
drops 22% -> 9.2%. A model asked to evaluate a position will invent tactics with
complete confidence, and a beginner has no way to tell.

So the division of labour is absolute:

* Stockfish decides what is good.  * python-chess decides what is legal.
* :mod:`app.motifs` decides what happened.  * Maia decides what is human.
* The model only writes prose about facts it is handed.

Two defences on top of that:

1. The prompt forbids introducing any square or move not present in the facts.
2. Every move-like token in the reply is parsed against the real position and
   the reply is rejected if any of them is illegal. This catches the residual
   ~9%, which no amount of prompting removes.

If the model is unavailable, slow, or produces something that fails validation,
the deterministic template renders instead. The review never depends on it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import chess
import httpx

from .deviation import Deviation, template_deviation
from .review import MoveReview

log = logging.getLogger(__name__)

# A LiteLLM gateway bound to 127.0.0.1 on the host cannot be reached through
# host.containers.internal — that address is the bridge gateway, not loopback.
# Join the gateway's own compose network and address it by service name instead.
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
COACH_MODEL = os.environ.get("COACH_MODEL", "chess-coach")
COACH_TIMEOUT = float(os.environ.get("COACH_TIMEOUT", "30"))
#: Reps are an interactive loop, not a background job — a correction that takes
#: half a minute to appear is worse than a plain one that appears at once.
REP_TIMEOUT = float(os.environ.get("REP_COACH_TIMEOUT", "12"))

#: Hyphen characters a model reaches for when it means "-". Left alone, they
#: are worse than cosmetic: "O\u2011O" does not match the move pattern below, so a
#: castling move written that way is never checked against the board at all.
#: Only true hyphen variants are rewritten globally; en and em dashes are real
#: punctuation in prose, so those are rewritten only between the O's.
_HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-"})
_CASTLE_DASH = re.compile(r"O[\u2010-\u2015\u2212]O")

#: Matches SAN-ish tokens: Nf3, exd5, O-O, Qxh7+, e8=Q#, plus bare squares.
_MOVE_TOKEN = re.compile(r"\b(?:O-O(?:-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?)\b")
_SQUARE_TOKEN = re.compile(r"\b[a-h][1-8]\b")

SYSTEM_PROMPT = """You are a patient chess coach writing for a beginner rated around {elo} on chess.com.

You will be given VERIFIED FACTS about one move from their game. Your only job is to turn those facts into two or three short sentences of plain English.

Hard rules:
- Use ONLY the facts given. Every square and every move you mention must appear verbatim in the facts.
- Do NOT evaluate the position yourself. The verdict is already decided and given to you.
- Do NOT invent variations, threats, or continuations beyond the lines provided.
- If the facts do not explain why the move was bad, say what is known and stop.
- No chess notation the player cannot act on. No engine jargon: never write "centipawns", "eval", or "+2.3".
- Never say "you should have seen that" or otherwise scold. Explain the pattern.
- Write at most 3 sentences. Plain prose, no headings, no lists, no markdown."""


def normalise(text: str) -> str:
    """Put a model's typography back into the alphabet the board speaks."""
    return _CASTLE_DASH.sub("O-O", text.translate(_HYPHENS))


@dataclass
class Explanation:
    text: str
    used_llm: bool
    fallback_reason: str | None = None


# --------------------------------------------------------------------------
# Fact bundle
# --------------------------------------------------------------------------


def _piece_list(board: chess.Board) -> str:
    """Spell the position out. Models misread raw FEN surprisingly often."""
    names = {
        chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
        chess.ROOK: "R", chess.QUEEN: "Q", chess.KING: "K",
    }
    out = {chess.WHITE: [], chess.BLACK: []}
    for square, piece in sorted(board.piece_map().items()):
        out[piece.color].append(f"{names[piece.piece_type]}{chess.square_name(square)}")
    return (
        f"White: {', '.join(out[chess.WHITE])}\n"
        f"Black: {', '.join(out[chess.BLACK])}"
    )


def build_facts(review: MoveReview, board: chess.Board, elo: int) -> str:
    """Everything true we know about one move, and nothing else."""
    mover = "White" if review.white_to_move else "Black"
    lines = [
        f"Position (FEN): {board.fen()}",
        _piece_list(board),
        f"Side to move: {mover} (this is the player you are coaching)",
        f"Game phase: {review.phase}",
        f"Move played: {review.san}",
        f"Verdict (already decided, do not second-guess): {review.judgment.value if review.judgment else 'acceptable'}",
        f"Winning chances before: {review.win_before:.0f}%, after: {review.win_after:.0f}% "
        f"(lost {review.win_lost:.0f} points)",
    ]

    if review.human_probability is not None:
        pct = review.human_probability * 100
        lines.append(
            f"How common this move is at {elo}: {pct:.0f}% of players choose it "
            f"(it is their #{review.human_rank} choice)"
        )
        if review.is_systematic:
            lines.append(
                "This is a COMMON mistake at this rating, not a one-off slip. "
                "Emphasise the recurring pattern so the player can avoid it next time."
            )

    if review.best_move_san:
        if review.best_is_findable:
            lines.append(f"Better move: {review.best_move_san}")
            if review.best_pv_san:
                lines.append(f"It would continue: {' '.join(review.best_pv_san[:5])}")
            if review.best_human_probability is not None:
                lines.append(
                    f"{review.best_human_probability * 100:.0f}% of players at this "
                    f"rating find that move."
                )
        else:
            # Telling a 600 player to find a move 2% of their peers find is
            # not teaching, it is showing off the engine.
            lines.append(
                f"NOTE: the engine prefers {review.best_move_san}, but only "
                f"{(review.best_human_probability or 0) * 100:.1f}% of players at this "
                f"rating find it. Do NOT tell the player they should have played it. "
                f"Explain the underlying idea instead."
            )

    if review.refutation_san:
        lines.append(f"How the opponent punishes it: {' '.join(review.refutation_san[:5])}")

    if review.findings:
        lines.append("What concretely went wrong:")
        for finding in review.findings[:4]:
            lines.append(f"  - {finding.phrase}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(text: str, board: chess.Board, allowed: set[str]) -> str | None:
    """Reject prose containing a move or square the facts never mentioned.

    Returns None if clean, otherwise the reason it was rejected.
    """
    for token in _MOVE_TOKEN.findall(text):
        if token in allowed:
            continue
        # A bare square is fine if the facts named it; a full move is not.
        if _SQUARE_TOKEN.fullmatch(token):
            if token in allowed:
                continue
            return f"mentions square {token} which is not in the facts"
        stripped = token.rstrip("+#")
        if stripped in allowed or token in allowed:
            continue
        # Last chance: is it at least a legal move here? Illegal is fatal.
        try:
            board.parse_san(token)
        except (ValueError, AssertionError):
            return f"mentions {token!r}, which is not a legal move in this position"
        return f"introduces move {token} that the facts never mentioned"
    return None


def _allowed_tokens(review: MoveReview, board: chess.Board) -> set[str]:
    allowed: set[str] = {review.san, review.san.rstrip("+#")}
    for source in (review.best_pv_san, review.refutation_san):
        for san in source:
            allowed.add(san)
            allowed.add(san.rstrip("+#"))
    if review.best_move_san:
        allowed.add(review.best_move_san)
        allowed.add(review.best_move_san.rstrip("+#"))
    for finding in review.findings:
        allowed.update(finding.squares)
        allowed.update(_MOVE_TOKEN.findall(finding.phrase))

    # Squares currently occupied are safe to name...
    allowed.update(chess.square_name(sq) for sq in board.piece_map())
    # ...and so is every square the played move or either line actually
    # touches. Without this, describing "the rook moved from h1 to g1" is
    # rejected because g1 happened to be empty beforehand — a true statement
    # thrown away, which costs a good explanation for no safety gain.
    allowed.update(_line_squares(board, review))
    return allowed


def _line_squares(board: chess.Board, review: MoveReview) -> set[str]:
    """Every square touched by the move played, the best line, or the refutation."""
    squares: set[str] = set()

    def walk(start: chess.Board, sans: list[str]) -> chess.Board:
        walker = start.copy()
        for san in sans:
            try:
                move = walker.parse_san(san)
            except ValueError:
                break
            squares.add(chess.square_name(move.from_square))
            squares.add(chess.square_name(move.to_square))
            walker.push(move)
        return walker

    try:
        played = chess.Move.from_uci(review.uci)
        squares.add(chess.square_name(played.from_square))
        squares.add(chess.square_name(played.to_square))
        after = board.copy()
        after.push(played)
    except (ValueError, AssertionError):
        return squares

    walk(board, list(review.best_pv_san))
    walk(after, list(review.refutation_san))
    return squares


# --------------------------------------------------------------------------
# Fallback
# --------------------------------------------------------------------------


def template_explanation(review: MoveReview) -> str:
    """Deterministic prose. Never wrong, occasionally wooden."""
    parts: list[str] = []
    if review.findings:
        parts.append(review.findings[0].phrase)
    else:
        verdict = review.judgment.value if review.judgment else "inaccurate"
        parts.append(
            f"{review.san} is a {verdict} here — it drops "
            f"{review.win_lost:.0f} points of winning chances."
        )

    if review.is_systematic and review.human_probability:
        parts.append(
            f"You are in good company: {review.human_probability * 100:.0f}% of "
            f"players at your rating play it too, which makes it a pattern worth "
            f"learning rather than a one-off slip."
        )

    if review.best_move_san and review.best_is_findable:
        line = " ".join(review.best_pv_san[:3])
        parts.append(f"{review.best_move_san} was better{f' ({line})' if line else ''}.")
    elif review.refutation_san:
        parts.append(f"The punishment is {' '.join(review.refutation_san[:3])}.")

    return " ".join(parts)


# --------------------------------------------------------------------------
# The coach
# --------------------------------------------------------------------------


class Coach:
    """Narrates a reviewed move. Degrades to templates, never to silence."""

    def __init__(self, url: str = LITELLM_URL, key: str = LITELLM_KEY, model: str = COACH_MODEL):
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=COACH_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def explain(self, review: MoveReview, board: chess.Board, elo: int) -> Explanation:
        facts = build_facts(review, board, elo)
        try:
            text = await self._ask(
                SYSTEM_PROMPT.format(elo=elo),
                f"VERIFIED FACTS\n{facts}\n\nExplain this move to the player.",
            )
        except Exception as exc:  # noqa: BLE001 - any failure falls back
            log.warning("coach LLM unavailable: %s", exc)
            return Explanation(template_explanation(review), False, f"llm error: {exc}")

        if not text:
            return Explanation(template_explanation(review), False, "empty response")

        text = normalise(text)
        problem = validate(text, board, _allowed_tokens(review, board))
        if problem:
            log.warning("coach output rejected (%s): %s", problem, text[:160])
            return Explanation(template_explanation(review), False, problem)

        return Explanation(text.strip(), True)

    async def explain_deviation(self, dev: Deviation, board: chess.Board, elo: int) -> Explanation:
        """Why the line plays its move and yours is not it.

        Falls back to the template on any failure, as the review does — but on a
        much shorter clock, because this one is blocking a drill rather than a
        background job.
        """
        facts = build_deviation_facts(dev, board, elo)
        try:
            text = await self._ask(
                DEVIATION_PROMPT.format(elo=elo),
                f"VERIFIED FACTS\n{facts}\n\nExplain this fork to the player.",
                timeout=REP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - any failure falls back
            log.warning("coach LLM unavailable: %s", exc)
            return Explanation(template_deviation(dev), False, f"llm error: {exc}")

        if not text:
            return Explanation(template_deviation(dev), False, "empty response")

        text = normalise(text)
        problem = validate(text, board, _deviation_tokens(dev, board))
        if problem:
            log.warning("deviation prose rejected (%s): %s", problem, text[:160])
            return Explanation(template_deviation(dev), False, problem)

        return Explanation(text.strip(), True)

    async def _ask(self, system: str, user: str, timeout: float | None = None) -> str:
        client = await self._http()
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            # Generous, because the models behind this gateway are REASONING
            # models: gpt-oss-120b and gemini-flash spend tokens on hidden
            # reasoning that counts against max_tokens before any prose is
            # emitted. At 220 the reply came back empty or cut mid-sentence.
            "max_tokens": 1500,
            # Dropped silently by providers that don't support it (LiteLLM is
            # configured with drop_params: true).
            "reasoning_effort": "low",
        }
        response = await client.post(
            f"{self.url}/v1/chat/completions", json=payload, headers=headers,
            timeout=timeout or COACH_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# Reps: why the line plays this move
# --------------------------------------------------------------------------

DEVIATION_PROMPT = """You are a patient chess coach writing for a beginner rated around {elo} on chess.com. They are drilling an opening line and have just played a different move from the one the line plays.

You will be given VERIFIED FACTS about that fork in the road. Write two or three short sentences telling them why the line plays its move, what theirs does differently, and that they should take it back and play the line's move.

Hard rules:
- Use ONLY the facts given. Every square and every move you mention must appear verbatim in the facts.
- Do NOT evaluate the position yourself and do NOT invent variations. The verdict is given to you.
- If the facts say their move is playable, say so plainly. Do NOT call a sound move a mistake, and never imply they lost the game by it.
- If the facts say the move transposes, lead with that: it reaches the same position, and the only reason to take it back is to drill the order.
- Do not claim to know opening theory beyond the facts: no "the main line continues", no historical names that are not given.
- End by telling them the move to play.
- No engine jargon: never write "centipawns", "eval", or "+2.3".
- At most 3 sentences. Plain prose, no headings, no lists, no markdown."""


def build_deviation_facts(dev: Deviation, board: chess.Board, elo: int) -> str:
    """Everything true about the fork, and nothing else."""
    mover = "White" if board.turn == chess.WHITE else "Black"
    lines = [
        f"Position (FEN): {board.fen()}",
        _piece_list(board),
        f"Side to move: {mover} (this is the player you are coaching)",
        f"Line being drilled: {dev.line_name}",
        f"The line plays here: {dev.book.san} ({dev.book.purpose()})",
        f"They played instead: {dev.played.san} ({dev.played.purpose()})",
        f"Verdict (already decided, do not second-guess): {_verdict(dev)}",
    ]
    if dev.remaining:
        lines.append(f"The line continues: {' '.join(dev.remaining)}")
    if dev.transposition:
        lines.append(
            f"Their move transposes: {' '.join(dev.transposition)} reaches the "
            f"identical position, one move later"
        )
    if dev.played.opening:
        lines.append(f"Their move is itself theory: it is the {dev.played.opening}")
    if dev.played.in_book or dev.book.in_book:
        lines.append(
            f"How mainstream each move is (named lines in the catalogue running "
            f"through it): {dev.book.san} {dev.book.weight}, "
            f"{dev.played.san} {dev.played.weight}"
        )
    elif not dev.played.in_book and not dev.transposition:
        # Pointless next to a transposition fact, and reads as a contradiction:
        # the move reaches a catalogued position, just not by this move order.
        lines.append("No catalogued opening line plays their move in this position")
    if dev.book.win_pct is not None and dev.played.win_pct is not None:
        lines.append(
            f"Winning chances for them: {dev.book.win_pct:.0f}% after "
            f"{dev.book.san}, {dev.played.win_pct:.0f}% after {dev.played.san}"
        )
    for statement in dev.played.costs:
        lines.append(f"Concrete problem with their move: {statement}")
    if dev.played.reply:
        lines.append(f"Best reply to their move: {' '.join(dev.played.reply)}")
    return "\n".join(lines)


def _verdict(dev: Deviation) -> str:
    return {
        "material": "their move gives away material",
        "worse": "their move is playable but clearly worse than the line's",
        "transposes": "their move is just as good and reaches the same position",
        "sideline": ("their move is catalogued but obscure; it is not a mistake, "
                     "but it is not a serious alternative either and must not be "
                     "described as a reasonable choice"),
        "playable": "both moves are reasonable; this is a repertoire choice",
    }[dev.severity]


def _deviation_tokens(dev: Deviation, board: chess.Board) -> set[str]:
    allowed: set[str] = set()
    for san in ([dev.book.san, dev.played.san] + dev.remaining
                + dev.transposition + dev.played.reply + dev.book.reply):
        allowed.add(san)
        allowed.add(san.rstrip("+#"))
    for note in (dev.book, dev.played):
        allowed.update({note.from_square, note.to_square})
        allowed.update(note.attacks_centre)
        for statement in note.costs:
            allowed.update(_MOVE_TOKEN.findall(statement))
    allowed.update(chess.square_name(sq) for sq in board.piece_map())
    return allowed
