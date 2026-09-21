"""FastAPI application: play a game, review it, get coached.

Engine handles are process-wide and created once at startup — Maia's checkpoint
takes a moment to load and Stockfish's hash table is worth keeping warm, so
spawning either per request would be wasteful and slow.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import chess
import chess.pgn
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chesscom import ChessCom, convert as convert_game, wanted
from .coach import Coach
from .db import Database, now
from .engines import Stockfish
from .eval import eval_win_percent
from .motifs import GUARDABLE, facts_for_move
from .deviation import analyse_deviation
from .openings import BookMode, OpeningBook, book_move_for, deviation_ply
from .puzzle_themes import MOTIF_THEMES
from .puzzles import describe, opening_position, pick_motif, pick_theme
from .rating import Player, apply_offset, record_result, suggested_starting_elo
from .reps import (
    MASTERY_STREAK,
    Progress,
    Rep,
    build_curriculum,
    check_rep,
    families_for,
)
from .review import review_game
from .see import PIECE_VALUES, see
from .stats import aggregate

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("chess-trainer")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: Sampling temperature for the opponent. This is the single most important
#: knob in the app. At 0 Maia plays the *modal* move of its rating band, which
#: is materially stronger than that band actually plays — the reason the
#: Lichess "maia1100" bot performs around 1500. Sampling at 1.0 reproduces the
#: real distribution of human choices, blunders included.
OPPONENT_TEMPERATURE = float(os.environ.get("OPPONENT_TEMPERATURE", "1.0"))
OPPONENT_TOP_P = float(os.environ.get("OPPONENT_TOP_P", "1.0"))

REVIEW_DEPTH = int(os.environ.get("REVIEW_DEPTH", "16"))

#: User-editable preferences, stored as JSON on the player row. Kept in one
#: dict so a settings page can render every key without new endpoints, and so
#: adding a key needs no migration.
DEFAULT_SETTINGS: dict = {
    # Say when a forced mate is on the board. A training aid: it tells you one
    # is there, never which move it is — being told to look is the lesson.
    "mate_hints": True,
    # Cap on how deep a mate is worth mentioning. At beginner level "there is a
    # mate in 7 here" is discouraging rather than instructive, because finding
    # it is not a realistic ask.
    "mate_hint_max": 3,
    # Say when the opponent has left material hanging. This targets the single
    # biggest measured weakness in real play: over 16 reviewed games the
    # opponent blundered 44 times and only 20 were punished. Losing half the
    # free material you are offered decides more games at this level than
    # anything else. Like the mate hint it says *that* something is loose, never
    # which piece — noticing is the skill being trained.
    "material_hints": True,
    # Board sounds: moves, captures, checks, and the result. Synthesised in the
    # browser, so this is purely a client-side preference stored server-side so
    # it follows you between devices.
    "sounds": True,
    # Shift the opponent away from where the servo has settled it. The servo
    # aims for a good game; this is for when you would rather have an easy one.
    "opponent_offset": 0,
    # Mistakes that stop the game and offer a retry the moment you make them.
    # Defaults to the two the reviews actually flag: hanging a piece shows up
    # in 15 of 17 games, and half the material the opponent leaves hanging goes
    # untaken. Everything else is opt-in from the tag list.
    "guards": ["hung_piece", "missed_free_piece"],
    # Watch chess.com for games you have just finished and pull them in without
    # being asked. Only the current month is polled and only conditionally, so
    # a quiet minute costs one request that answers 304.
    "auto_import": True,
    # How often to look. A rapid game lasts ten minutes or more, so a minute is
    # already far finer than the thing being watched; it is here because it was
    # asked for, and because a conditional request is cheap.
    "auto_import_seconds": 60,
}

#: Shallow enough to stay on the move path, deep enough to see the refutation
#: that most of these motifs live in.
GUARD_DEPTH = 10


def effective_settings() -> dict:
    return {**DEFAULT_SETTINGS, **db().get_settings()}


class NewGame(BaseModel):
    color: str = Field("white", pattern="^(white|black|random)$")
    mode: str = Field(BookMode.DRILL, pattern="^(strict|drill|realistic)$")
    opening: str | None = None
    drill_plies: int = 12


class MoveIn(BaseModel):
    uci: str


class StartRep(BaseModel):
    family: str
    color: str = Field("white", pattern="^(white|black)$")
    #: A specific line's move sequence, to drill that one instead of the next
    #: in the queue. Omitted means "whatever the curriculum says is next".
    key: str | None = None


class ConnectChessCom(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class ImportChessCom(BaseModel):
    #: How many of the newest imported games to review straight away. Each is
    #: ~30-60s of engine time, so this is a deliberate budget, not a limit.
    review: int = Field(20, ge=0, le=200)
    time_classes: list[str] = Field(default_factory=lambda: ["rapid"])


class PuzzleMove(BaseModel):
    uci: str


class ResetReps(BaseModel):
    family: str
    color: str = Field("white", pattern="^(white|black)$")


class Settings(BaseModel):
    """All fields optional: a PATCH-style merge, so a partial save is safe."""

    chesscom_username: str | None = None
    chesscom_rapid: int | None = None
    mate_hints: bool | None = None
    mate_hint_max: int | None = Field(None, ge=1, le=5)
    material_hints: bool | None = None
    sounds: bool | None = None
    opponent_offset: int | None = Field(None, ge=-300, le=300)
    guards: list[str] | None = None
    auto_import: bool | None = None
    auto_import_seconds: int | None = Field(None, ge=30, le=3600)


app = FastAPI(title="Chess Trainer")
state: dict = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    state["db"] = Database()
    orphaned = state["db"].recover_orphaned_reviews()
    if orphaned:
        log.warning("marked %d review(s) failed: interrupted by a restart", orphaned)
    state["book"] = OpeningBook.load()
    state["stockfish"] = Stockfish(depth=REVIEW_DEPTH)
    await state["stockfish"].start()
    state["coach"] = Coach()
    state["chesscom"] = ChessCom()
    state["import"] = {"running": False, "step": "idle", "found": 0, "imported": 0,
                       "skipped": 0, "reviewed": 0, "to_review": 0, "error": None}
    state["watch"] = {"checked_at": None, "found_at": None, "new_games": 0, "error": None}
    state["watcher"] = asyncio.create_task(_watch_chesscom())
    state["rng"] = random.Random()
    state["curricula"] = {}
    reconcile_line_progress()

    # Maia is optional so the app still boots (review-only) if torch is absent.
    try:
        from .maia import Maia

        state["maia"] = Maia()
        log.info("Maia-3 ready")
    except Exception as exc:  # noqa: BLE001
        state["maia"] = None
        log.error("Maia unavailable, opponent disabled: %s", exc)

    log.info("chess-trainer ready (%d openings)", len(state["book"].openings))
    try:
        yield
    finally:
        await state["stockfish"].close()
        await state["coach"].close()
        watcher = state.get("watcher")
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await state["chesscom"].close()
        state["db"].close()


app.router.lifespan_context = lifespan


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def db() -> Database:
    return state["db"]


def board_of(row) -> chess.Board:
    board = chess.Board()
    for uci in (row["moves"] or "").split():
        board.push(chess.Move.from_uci(uci))
    return board


def game_payload(row) -> dict:
    board = board_of(row)
    opening = state["book"].classify(list(board.move_stack))
    result = row["result"]
    return {
        "id": row["id"],
        "fen": board.fen(),
        "moves": [m.uci() for m in board.move_stack],
        "san": _san_list(board),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "player_color": row["player_color"],
        "bot_elo": row["bot_elo"],
        "mode": row["mode"],
        "drill_line": row["drill_line"],
        "opening": {"eco": opening.eco, "name": opening.name} if opening else None,
        "result": result,
        "termination": row["termination"],
        "is_over": result is not None,
        "in_check": board.is_check(),
        "legal_moves": [m.uci() for m in board.legal_moves] if result is None else [],
        "pending": json.loads(row["pending"]) if row["pending"] else None,
        "takebacks": row["takebacks"] or 0,
    }


async def check_guards(before: chess.Board, move: chess.Move) -> dict | None:
    """Did this move make a mistake the player asked to be stopped on?

    Runs the same deterministic detectors the review uses, so a guard can never
    report something the post-game analysis would not. The engine call is
    shallow because this sits on the move path — most of these motifs live in
    the opponent's reply, which a depth-10 search already finds.
    """
    guards = set(effective_settings().get("guards") or [])
    if not guards:
        return None

    after = before.copy()
    after.push(move)
    refutation: list[chess.Move] = []
    try:
        lines = await state["stockfish"].analyse(after, depth=GUARD_DEPTH, multipv=1)
        if lines:
            refutation = list(lines[0].pv)
        pov_before = (await state["stockfish"].evaluate(before, depth=GUARD_DEPTH)).pov(
            before.turn == chess.WHITE
        )
        pov_after = (await state["stockfish"].evaluate(after, depth=GUARD_DEPTH)).pov(
            before.turn == chess.WHITE
        )
        had_mate = pov_before.is_mate and pov_before.mate > 0
        faces_mate = pov_after.is_mate and pov_after.mate < 0
        already_lost = (pov_before.cp is not None and pov_before.cp < -900) or (
            pov_before.is_mate and pov_before.mate < 0
        )
    except Exception:  # noqa: BLE001 - never let a guard break a move
        log.warning("guard analysis failed", exc_info=True)
        had_mate = faces_mate = already_lost = False

    findings = facts_for_move(
        before,
        move,
        refutation=refutation,
        had_mate=had_mate,
        faces_mate=faces_mate,
        already_lost=already_lost,
    )
    for finding in findings:
        if finding.motif.value in guards:
            return {
                "motif": finding.motif.value,
                "message": finding.phrase,
                "san": before.san(move),
                "uci": move.uci(),
                "material_cp": finding.material,
            }
    return None


def with_material_hint(payload: dict) -> dict:
    """Flag material the opponent has left hanging, without naming it.

    Pure static exchange evaluation — no engine call, so it costs nothing on the
    move path. Pawns are excluded: at this level the lesson is pieces, and
    flagging every loose pawn would fire constantly and be ignored.
    """
    payload.setdefault("material_hint", None)
    if not effective_settings().get("material_hints", True):
        return payload
    if payload["is_over"] or payload["turn"] != payload["player_color"]:
        return payload
    try:
        board = chess.Board(payload["fen"])
        best = 0
        for move in board.legal_moves:
            if not board.is_capture(move):
                continue
            best = max(best, see(board, move))
        if best >= PIECE_VALUES[chess.KNIGHT]:
            payload["material_hint"] = {"value": best}
    except Exception:  # noqa: BLE001 - a hint must never break a move
        log.warning("material hint failed", exc_info=True)
    return payload


async def with_mate_hint(payload: dict) -> dict:
    """Flag a forced mate that is available to the player right now.

    Deliberately says *that* a mate exists and how long it is, never which move
    starts it — the whole training value is being prompted to look. Only the
    player's own opportunities are flagged, and only on their turn.
    """
    payload.setdefault("mate_hint", None)
    settings = effective_settings()
    if not settings.get("mate_hints"):
        return payload
    if payload["is_over"] or payload["turn"] != payload["player_color"]:
        return payload
    try:
        mate_in = await state["stockfish"].find_mate(
            chess.Board(payload["fen"]),
            max_moves=int(settings.get("mate_hint_max", 3)),
        )
    except Exception:  # noqa: BLE001 - a hint is never worth failing a move over
        log.warning("mate hint lookup failed", exc_info=True)
        return payload
    if mate_in:
        payload["mate_hint"] = {"in": mate_in}
    return payload


def _san_list(board: chess.Board) -> list[str]:
    walker = chess.Board()
    out = []
    for move in board.move_stack:
        out.append(walker.san(move))
        walker.push(move)
    return out


def _outcome(board: chess.Board) -> tuple[str, str] | None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    return outcome.result(), outcome.termination.name.lower()


def _finish(row, board: chess.Board, result: str, termination: str) -> None:
    """Record the result and let the servo re-aim the opponent's strength."""
    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = "Chess Trainer"
    game.headers["Result"] = result
    db().update_game(
        row["id"],
        result=result,
        termination=termination,
        finished_at=now(),
        pgn=str(game),
    )
    if row["source"] != "trainer":
        return
    if row["takebacks"]:
        # Retries make the result a practice score, not a measurement — letting
        # it move the ladder would drag the opponent down to a level you only
        # beat with help. Clean games still drive it.
        log.info("game %d used %d takeback(s); not rating it", row["id"], row["takebacks"])
        return
    hero_white = row["player_color"] == "white"
    score = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[result]
    if not hero_white:
        score = 1.0 - score
    db().save_player(record_result(db().get_player(), score))


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/api/state")
async def get_state():
    player = db().get_player()
    offset = effective_settings().get("opponent_offset", 0)
    effective = apply_offset(player.bot_elo, offset)
    raw_shift = player.bot_elo + int(offset or 0)
    return {
        "rating": round(player.rating),
        "rd": round(player.rd),
        "confident": player.confident,
        "bot_elo": player.bot_elo,
        # What the next game will actually be played at. The header used to show
        # the pre-offset number while games ran at something else entirely.
        "effective_bot_elo": effective,
        # True when the offset is pushing the dial past Maia's trained range, so
        # the opponent cannot follow your rating any more. Worth surfacing: a
        # dial pinned at a bound is exactly "no learning gradient".
        "offset_clamped": effective != raw_shift,
        "recent": player.recent_scores,
        "chesscom_username": db().get_setting("chesscom_username"),
        "maia_available": state.get("maia") is not None,
        "engine": state["stockfish"].id.get("name"),
        "settings": effective_settings(),
    }


@app.get("/api/settings")
async def get_settings_route():
    """Settings, plus the tag list the guard checkboxes are built from.

    Each tag carries how many of your reviewed games it shows up in, so the
    list doubles as "here is what you actually keep doing".
    """
    from .stats import MOTIF_ADVICE

    seen = {}
    try:
        stats = await get_stats()
        seen = {w["motif"]: w["games"] for w in stats.get("weaknesses", [])}
        reviewed = stats.get("games", 0)
    except Exception:  # noqa: BLE001 - the tag list must render regardless
        reviewed = 0

    active = set(effective_settings().get("guards") or [])
    tags = [
        {
            "motif": motif,
            "title": MOTIF_ADVICE.get(motif, (motif.replace("_", " "), ""))[0],
            "advice": MOTIF_ADVICE.get(motif, ("", ""))[1],
            "games": seen.get(motif, 0),
            "guarded": motif in active,
        }
        for motif in GUARDABLE
    ]
    # The ones you actually make come first; the rest stay available.
    tags.sort(key=lambda t: (-t["games"], t["title"]))
    return {
        "settings": effective_settings(),
        "defaults": DEFAULT_SETTINGS,
        "chesscom_username": db().get_setting("chesscom_username"),
        "tags": tags,
        "reviewed_games": reviewed,
    }


@app.post("/api/settings")
async def put_settings(settings: Settings):
    if settings.chesscom_username is not None:
        db().set_chesscom_username(settings.chesscom_username.strip() or None)
    if settings.chesscom_rapid:
        player = db().get_player()
        player.bot_elo = suggested_starting_elo(settings.chesscom_rapid)
        db().save_player(player)

    changes = settings.model_dump(
        exclude_none=True, include=set(DEFAULT_SETTINGS)
    )
    if "guards" in changes:
        unknown = set(changes["guards"]) - set(GUARDABLE)
        if unknown:
            raise HTTPException(400, f"not guardable: {sorted(unknown)}")
    if changes:
        db().save_settings({**db().get_settings(), **changes})
    return await get_settings_route()


@app.get("/api/openings")
async def search_openings(q: str = "", limit: int = 25):
    hits = state["book"].search(q, limit=limit)
    return [
        {"eco": o.eco, "name": o.name, "line": o.san_line(), "plies": len(o.moves)}
        for o in hits
    ]


@app.get("/api/games")
async def list_games(limit: int = 50):
    rows = db().list_games(limit=limit)
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "source": r["source"],
            "player_color": r["player_color"],
            "opening": r["opening_name"],
            "result": r["result"],
            "bot_elo": r["bot_elo"],
            # An imported game has no bot: the opponent was a person with a
            # rating, and that rating is the useful column.
            "opponent_rating": r["opponent_rating"],
            "player_rating": r["player_rating"],
            "moves": len((r["moves"] or "").split()),
        }
        for r in rows
    ]


@app.post("/api/games")
async def new_game(spec: NewGame):
    if state.get("maia") is None:
        raise HTTPException(503, "Maia is unavailable, so the opponent cannot play.")

    color = spec.color
    if color == "random":
        color = state["rng"].choice(["white", "black"])

    line = None
    if spec.opening:
        line = state["book"].by_name.get(spec.opening)
        if line is None:
            matches = state["book"].search(spec.opening, limit=1)
            line = matches[0] if matches else None
        if line is None and spec.mode == BookMode.DRILL:
            raise HTTPException(404, f"No opening matching {spec.opening!r}")

    player = db().get_player()
    bot_elo = apply_offset(player.bot_elo, effective_settings().get("opponent_offset", 0))
    game_id = db().create_game(
        player_color=color,
        bot_elo=bot_elo,
        mode=spec.mode,
        drill_line=line.name if line else None,
        opening_eco=line.eco if line else None,
        opening_name=line.name if line else None,
    )

    # If the bot has White it must open.
    row = db().get_game(game_id)
    if color == "black":
        await _bot_reply(row)
        row = db().get_game(game_id)
    return with_material_hint(await with_mate_hint(game_payload(row)))


@app.get("/api/stats")
async def get_stats(source: str | None = None):
    """Everything the reviews say when you look at them together.

    ``source`` narrows to "trainer" or "chesscom". Keeping them separable is
    the point of importing: the question is not how you play against Maia, it
    is whether what you practise shows up in the games that count.
    """
    games = []
    for row, data in db().reviews_for_stats(source):
        games.append(({"player_color": row["player_color"], "result": row["result"]}, data))
    out = aggregate(games)
    trainer_total, trainer_done = db().count_games("trainer")
    cc_total, cc_done = db().count_games("chesscom")
    out["sources"] = {
        "trainer": {"games": trainer_total, "reviewed": trainer_done},
        "chesscom": {"games": cc_total, "reviewed": cc_done},
        "showing": source or "all",
    }
    return out


# --------------------------------------------------------------------------
# chess.com
# --------------------------------------------------------------------------


@app.get("/api/chesscom")
async def chesscom_status():
    total, reviewed = db().count_games("chesscom")
    settings = effective_settings()
    return {
        "username": db().get_chesscom_username(),
        "games": total,
        "reviewed": reviewed,
        "progress": dict(state["import"]),
        "watch": {
            **state.get("watch", {}),
            "enabled": bool(settings.get("auto_import", True)),
            "every": int(settings.get("auto_import_seconds", 60)),
        },
    }


@app.post("/api/chesscom/connect")
async def chesscom_connect(spec: ConnectChessCom):
    """Check the account exists before storing it.

    A typo here is otherwise only discovered as an empty import, and an empty
    import is indistinguishable from "you have no games".
    """
    try:
        profile = await state["chesscom"].profile(spec.username.strip())
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"chess.com is unreachable: {exc}") from exc
    if profile is None:
        raise HTTPException(404, f"No chess.com player called {spec.username!r}")

    username = profile["username"]
    db().set_chesscom_username(username)
    return {
        "username": username,
        "url": profile.get("url"),
        "joined": profile.get("joined"),
        "country": profile.get("country", "").rsplit("/", 1)[-1],
    }


@app.delete("/api/chesscom/connect")
async def chesscom_disconnect():
    """Forget the account. Games already imported stay: they are your games."""
    db().set_chesscom_username(None)
    return {"username": None}


@app.post("/api/chesscom/import")
async def chesscom_import(spec: ImportChessCom, background: BackgroundTasks):
    username = db().get_chesscom_username()
    if not username:
        raise HTTPException(400, "Connect a chess.com account first")
    if state["import"]["running"]:
        raise HTTPException(409, "An import is already running")

    state["import"] = {"running": True, "step": "starting", "found": 0, "imported": 0,
                       "skipped": 0, "reviewed": 0, "to_review": 0, "error": None}
    background.add_task(_run_import, username, tuple(spec.time_classes), spec.review)
    return dict(state["import"])


async def _run_import(username: str, time_classes: tuple[str, ...], review: int,
                      months_to_check: int | None = None) -> None:
    """Fetch months, store what is new, then review the newest few.

    Months are walked oldest-first and revalidated by ETag, so a repeat import
    is a handful of 304s and one real fetch of the current month.
    ``months_to_check`` limits it to the newest N, which is what the watcher
    uses: closed months cannot gain games, so re-checking them every minute
    would be a request per month per minute to learn nothing.
    """
    progress = state["import"]
    client: ChessCom = state["chesscom"]
    try:
        # The filter set is part of the cache key: a month read as "rapid only"
        # must be re-read if blitz is asked for later.
        filters = ",".join(sorted(time_classes))
        months = await _archive_list(username, cached=bool(months_to_check))
        if months_to_check:
            months = months[-months_to_check:]
        progress["step"] = f"0/{len(months)} months"

        for index, url in enumerate(months, start=1):
            archive = await client.month(url, db().archive_etag(url, filters))
            progress["step"] = f"{index}/{len(months)} months"
            if archive.unchanged:
                continue

            kept = 0
            for entry in archive.games:
                if not wanted(entry, time_classes):
                    continue
                progress["found"] += 1
                if db().game_exists(entry["uuid"]):
                    progress["skipped"] += 1
                    continue
                fields = convert_game(entry, username, state["book"])
                if fields is None:
                    progress["skipped"] += 1
                    continue
                db().create_game(**fields)
                progress["imported"] += 1
                kept += 1
            db().save_archive(url, archive.etag, kept, filters)

        if review:
            await _review_pending(review)

        progress["step"] = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.warning("chess.com import failed", exc_info=True)
        progress["error"] = str(exc)
        progress["step"] = "failed"
    finally:
        progress["running"] = False


#: How long the list of archive months is reused before being asked for again.
#: It only changes when a new month starts, so re-fetching it every minute is a
#: second request per minute to be told the same thing — and the claim that an
#: idle check costs one conditional request has to actually be true.
ARCHIVE_LIST_TTL = 3 * 3600


async def _archive_list(username: str, cached: bool) -> list[str]:
    """The player's months. Cached for the watcher, always fresh for a manual
    import, where the user has asked for a full pass and is waiting for it."""
    entry = state.get("archive_list")
    if cached and entry and entry["username"] == username:
        fresh = time.monotonic() - entry["at"] < ARCHIVE_LIST_TTL
        # A month boundary invalidates it however recent it is, or the watcher
        # keeps polling last month while you play this one.
        same_month = entry["month"] == datetime.now(timezone.utc).strftime("%Y/%m")
        if fresh and same_month:
            return entry["months"]

    months = await state["chesscom"].archives(username)
    state["archive_list"] = {
        "username": username,
        "months": months,
        "at": time.monotonic(),
        "month": datetime.now(timezone.utc).strftime("%Y/%m"),
    }
    return months


async def _review_pending(limit: int) -> None:
    """Analyse imported games that have no review yet, newest first.

    Serially, and through the same pipeline as a trainer game, so the two land
    in one metric space. Stockfish is shared with the rest of the box, so this
    must never fan out.
    """
    progress = state["import"]
    pending = db().unreviewed_games("chesscom", limit)
    progress["to_review"] = len(pending)
    for row in pending:
        progress["step"] = f"reviewing {progress['reviewed'] + 1}/{len(pending)}"
        db().upsert_review(row["id"], "pending")
        await _run_review(row["id"])
        progress["reviewed"] += 1


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------

#: How long a trainer game must have been idle before a background review is
#: allowed to start. Reviews and your moves share one Stockfish behind one
#: lock, so a review that starts mid-game puts a minute or two in front of your
#: next mate hint. Importing is only HTTP and is never held back.
QUIET_BEFORE_REVIEW = timedelta(minutes=10)


async def _watch_chesscom() -> None:
    """Poll for games you have just finished, and pull them in.

    Politeness is the whole design here. Only the newest month is asked for,
    always conditionally, so an idle minute is one request that answers 304 with
    no body — chess.com's own guidance is that serial access is unlimited and
    this is one serial request a minute. Closed months are never re-read: they
    cannot gain games.
    """
    await asyncio.sleep(10)          # let the engines finish starting
    while True:
        settings = effective_settings()
        interval = int(settings.get("auto_import_seconds", 60))
        try:
            if settings.get("auto_import", True):
                await _watch_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad minute must not end the watch
            log.warning("auto-import check failed", exc_info=True)
            state["watch"]["error"] = "last check failed"
        await asyncio.sleep(max(30, interval))


async def _watch_once() -> None:
    username = db().get_chesscom_username()
    if not username or state["import"]["running"]:
        return

    before, _ = db().count_games("chesscom")
    state["import"] = {"running": True, "step": "checking", "found": 0, "imported": 0,
                       "skipped": 0, "reviewed": 0, "to_review": 0, "error": None}
    try:
        # Fetch first and record the check straight away. Reviewing can take
        # minutes, and a status that only updates afterwards leaves the page
        # showing a watcher that has never looked at anything.
        await _run_import(username, ("rapid",), review=0, months_to_check=1)

        after, _ = db().count_games("chesscom")
        state["watch"]["checked_at"] = now()
        state["watch"]["error"] = state["import"]["error"]
        if after > before:
            state["watch"]["found_at"] = now()
            state["watch"]["new_games"] = (
                state["watch"].get("new_games", 0) + (after - before))
            log.info("auto-imported %d new chess.com game(s)", after - before)

        # Reviewing is what costs; skip it while a game is live and let the next
        # check pick the games up, rather than stealing the engine mid-move.
        if not _game_in_progress():
            state["import"]["running"] = True
            await _review_pending(5)
    finally:
        state["import"]["running"] = False


def _game_in_progress() -> bool:
    """Is a trainer game live enough that the engine should be left alone?

    Keyed on the last move rather than when the game started: a rapid game can
    run half an hour, and a review that began because the *start* looked old
    enough would land in the middle of it. A restart clears this, which fails
    in the right direction — the watcher then imports without reviewing until
    the first move of the next game.
    """
    played = state.get("last_move_at")
    if played is None or time.monotonic() - played > QUIET_BEFORE_REVIEW.total_seconds():
        return False
    return any(
        row["result"] is None and (row["moves"] or "").strip()
        for row in db().list_games(limit=5, source="trainer")
    )


@app.get("/api/games/current")
async def current_game():
    """The most recent unfinished trainer game, so a refresh doesn't strand it.

    Declared before /api/games/{game_id} because FastAPI matches routes in
    order and "current" would otherwise be parsed as an id.
    """
    for row in db().list_games(limit=20, source="trainer"):
        if row["result"] is None and (row["moves"] or "").strip():
            return with_material_hint(await with_mate_hint(game_payload(row)))
    return None


@app.get("/api/games/{game_id}")
async def get_game(game_id: int):
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    return with_material_hint(await with_mate_hint(game_payload(row)))


@app.post("/api/games/{game_id}/moves")
async def play_move(game_id: int, move_in: MoveIn):
    # Tells the chess.com watcher to keep off the engine: reviews and your
    # moves share one Stockfish behind one lock.
    state["last_move_at"] = time.monotonic()
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    if row["result"] is not None:
        raise HTTPException(409, "That game is already finished")

    board = board_of(row)
    if ("white" if board.turn == chess.WHITE else "black") != row["player_color"]:
        raise HTTPException(409, "It is not your turn")

    try:
        move = chess.Move.from_uci(move_in.uci)
    except ValueError:
        raise HTTPException(400, f"{move_in.uci!r} is not a move")
    if move not in board.legal_moves:
        raise HTTPException(400, f"{move_in.uci} is not legal here")

    line = state["book"].by_name.get(row["drill_line"]) if row["drill_line"] else None
    left_line = None
    if line is not None:
        before = deviation_ply(board, line)
        board.push(move)
        after = deviation_ply(board, line)
        if before is None and after is not None:
            left_line = {
                "ply": after,
                "expected": chess.Board().variation_san(line.moves[: after + 1]).split()[-1],
            }
        board.pop()

    before_move = board.copy()
    board.push(move)
    db().update_game(game_id, moves=" ".join(m.uci() for m in board.move_stack))

    finished = _outcome(board)
    if finished:
        _finish(db().get_game(game_id), board, *finished)
        payload = game_payload(db().get_game(game_id))
        payload["left_line"] = left_line
        return with_material_hint(await with_mate_hint(payload))

    # Stop here if the move tripped a guard: the bot must not reply while a
    # takeback is on the table, or there would be nothing to take back to.
    warning = await check_guards(before_move, move)
    if warning:
        db().update_game(game_id, pending=json.dumps(warning))
        payload = game_payload(db().get_game(game_id))
        payload["left_line"] = left_line
        return payload

    await _bot_reply(db().get_game(game_id))
    payload = game_payload(db().get_game(game_id))
    payload["left_line"] = left_line
    return with_material_hint(await with_mate_hint(payload))


@app.post("/api/games/{game_id}/undo")
async def undo_move(game_id: int):
    """Take back the move that tripped a guard, and try again."""
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    if not row["pending"]:
        raise HTTPException(409, "There is nothing to take back")

    board = board_of(row)
    board.pop()
    db().update_game(
        game_id,
        moves=" ".join(m.uci() for m in board.move_stack),
        pending=None,
        takebacks=(row["takebacks"] or 0) + 1,
    )
    return with_material_hint(await with_mate_hint(game_payload(db().get_game(game_id))))


@app.post("/api/games/{game_id}/keep")
async def keep_move(game_id: int):
    """Stand by the move; the bot replies as normal."""
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    if not row["pending"]:
        raise HTTPException(409, "There is no warning to answer")

    db().update_game(game_id, pending=None)
    row = db().get_game(game_id)
    board = board_of(row)
    finished = _outcome(board)
    if finished:
        _finish(row, board, *finished)
    else:
        await _bot_reply(row)
    return with_material_hint(await with_mate_hint(game_payload(db().get_game(game_id))))


async def _bot_reply(row) -> None:
    board = board_of(row)
    if board.is_game_over():
        return

    line = state["book"].by_name.get(row["drill_line"]) if row["drill_line"] else None
    move = book_move_for(
        board,
        state["book"],
        row["mode"] or BookMode.REALISTIC,
        line=line,
        drill_plies=12,
        rng=state["rng"],
    )
    source = "book"
    if move is None:
        source = "maia"
        move = await state["maia"].play(
            board,
            row["bot_elo"],
            temperature=OPPONENT_TEMPERATURE,
            top_p=OPPONENT_TOP_P,
        )
    if move is None or move not in board.legal_moves:
        log.error("bot produced no legal move (source=%s)", source)
        return

    board.push(move)
    db().update_game(row["id"], moves=" ".join(m.uci() for m in board.move_stack))
    finished = _outcome(board)
    if finished:
        _finish(db().get_game(row["id"]), board, *finished)


@app.post("/api/games/{game_id}/resign")
async def resign(game_id: int):
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    if row["result"] is not None:
        return game_payload(row)
    result = "0-1" if row["player_color"] == "white" else "1-0"
    _finish(row, board_of(row), result, "resignation")
    return game_payload(db().get_game(game_id))




# --------------------------------------------------------------------------
# Reps — drilling opening theory, easiest lines first
# --------------------------------------------------------------------------


def curriculum_for(family: str, color: bool):
    """Cached curriculum. Building one walks every line in the family (~90ms)."""
    key = (family, color)
    cached = state["curricula"].get(key)
    if cached is None:
        cached = build_curriculum(state["book"], family, color)
        state["curricula"][key] = cached
    return cached


def _shares_a_prefix(a: list[str], b: list[str]) -> bool:
    """One line is the start of the other, whichever way round."""
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return bool(shorter) and longer[: len(shorter)] == shorter


def reconcile_line_progress() -> None:
    """Carry name-keyed progress over to the move-keyed table.

    Progress used to be stored against a line's *name*. Names are derived from
    classifying the position a line reaches, so rebuilding the curriculum
    renames lines, and every rename silently orphaned the progress attached to
    it — which to the player is indistinguishable from the app forgetting.

    Two ways a stored name is matched to a line, in order: the name still
    exists, or the moves it named share a prefix with exactly one line. The
    prefix goes in *either* direction, because both happened: "Ruy Lopez" was a
    five-ply line that got carried forward into a nine-ply rep, while "Ruy Lopez:
    Closed" was a ten-ply line trimmed back to nine. Where two old rows land on
    one rep — Closed and Open merged into a single White rep once the trailing
    Black move was trimmed — the better streak wins and the attempts add up.
    """
    legacy = db().legacy_line_progress()
    if not legacy:
        return

    book = state["book"]
    matched = 0
    for row in legacy:
        color = chess.WHITE if row["color"] == "white" else chess.BLACK
        curriculum = curriculum_for(row["family"], color)
        rep = next((r for r in curriculum.reps if r.name == row["line"]), None)
        if rep is None:
            named = next((o for o in book.openings if o.name == row["line"]), None)
            if named is not None:
                want = [m.uci() for m in named.moves]
                rep = next(
                    (r for r in curriculum.reps
                     if _shares_a_prefix(want, [m.uci() for m in r.moves])),
                    None,
                )
        if rep is None:
            log.warning("line progress for %r no longer matches a line", row["line"])
            continue
        db().restore_line_progress(
            row["family"], row["color"], rep.key, rep.name,
            row["streak"], row["attempts"], row["successes"], row.get("last_seen"),
        )
        matched += 1

    log.info("reconciled %d/%d stored lines onto the current curriculum",
             matched, len(legacy))
    if matched == len(legacy):
        db().drop_legacy_line_progress()


def _progress_map(family: str, color_name: str) -> dict[str, Progress]:
    raw = db().get_line_progress(family, color_name)
    return {name: Progress(**vals) for name, vals in raw.items()}


def _rep_of(row) -> Rep | None:
    color = chess.WHITE if row["player_color"] == "white" else chess.BLACK
    curriculum = curriculum_for(row["opening_name"], color)
    by_name = next((r for r in curriculum.reps if r.name == row["drill_line"]), None)
    if by_name is not None:
        return by_name

    # A rep in progress when the curriculum was rebuilt has a name that no
    # longer exists. The moves played so far still identify it: they are a
    # prefix of exactly one line, unless the rep has barely started.
    played = (row["moves"] or "").split()
    candidates = [r for r in curriculum.reps
                  if [m.uci() for m in r.moves][: len(played)] == played]
    return candidates[0] if len(candidates) == 1 else None


def rep_payload(row, rep: Rep, extra: dict | None = None) -> dict:
    board = board_of(row)
    played = len(board.move_stack)
    payload = {
        "id": row["id"],
        "family": row["opening_name"],
        "line_name": rep.name,
        "eco": rep.opening.eco,
        "color": row["player_color"],
        "fen": board.fen(),
        "moves": [m.uci() for m in board.move_stack],
        "san": _san_list(board),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "plies": rep.plies,
        "played_plies": played,
        "your_turn": played < rep.plies
        and (board.turn == chess.WHITE) == (rep.color == chess.WHITE),
        "difficulty": rep.difficulty,
        "popularity": rep.rarest_edge,
        "legal_moves": [m.uci() for m in board.legal_moves] if row["result"] is None else [],
        "status": "playing",
        "corrections": json.loads(row["corrections"] or "[]"),
        # Held in the row rather than in memory, so reloading mid-correction
        # shows the same explanation instead of losing it.
        "correction": json.loads(row["pending"]) if row["pending"] else None,
    }
    payload.update(extra or {})
    return payload


@app.get("/api/reps/families")
async def rep_families():
    """Every family on offer, each tagged with the side it belongs to."""
    return families_for(state["book"])


@app.get("/api/reps/progress")
async def rep_progress(family: str, color: str = "white"):
    side = chess.WHITE if color == "white" else chess.BLACK
    curriculum = curriculum_for(family, side)
    return curriculum.summary(_progress_map(family, color))


@app.post("/api/reps/reset")
async def rep_reset(spec: ResetReps):
    removed = db().reset_line_progress(spec.family, spec.color)
    return {"cleared": removed}


@app.post("/api/reps/start")
async def rep_start(spec: StartRep):
    side = chess.WHITE if spec.color == "white" else chess.BLACK
    curriculum = curriculum_for(spec.family, side)
    if not curriculum.reps:
        raise HTTPException(404, f"No lines catalogued for {spec.family!r}")

    progress = _progress_map(spec.family, spec.color)
    rep = curriculum.find(spec.key) if spec.key else curriculum.next_rep(progress)
    if rep is None and spec.key:
        raise HTTPException(404, "That line is not in this curriculum")
    game_id = db().create_game(
        source="rep",
        player_color=spec.color,
        mode="reps",
        opening_name=spec.family,
        opening_eco=rep.opening.eco,
        drill_line=rep.name,
    )

    # If the player has Black, the line's first move belongs to the bot.
    row = db().get_game(game_id)
    if side == chess.BLACK:
        board = board_of(row)
        board.push(rep.moves[0])
        db().update_game(game_id, moves=" ".join(m.uci() for m in board.move_stack))
        row = db().get_game(game_id)

    return rep_payload(row, rep, {"total_unlocked": len(curriculum.unlocked(
        _progress_map(spec.family, spec.color)))})


@app.post("/api/reps/{game_id}/move")
async def rep_move(game_id: int, move_in: MoveIn):
    row = db().get_game(game_id)
    if row is None or row["source"] != "rep":
        raise HTTPException(404, "No such rep")
    if row["result"] is not None:
        raise HTTPException(409, "That rep is already finished")

    rep = _rep_of(row)
    if rep is None:
        raise HTTPException(410, "That line is no longer in the curriculum")

    board = board_of(row)
    ply = len(board.move_stack)
    try:
        move = chess.Move.from_uci(move_in.uci)
    except ValueError:
        raise HTTPException(400, f"{move_in.uci!r} is not a move")
    if move not in board.legal_moves:
        raise HTTPException(400, f"{move_in.uci} is not legal here")

    expected = rep.moves[ply]
    if move != expected:
        return await _correct(game_id, rep, board, expected, move)

    board.push(move)
    db().update_game(game_id, moves=" ".join(m.uci() for m in board.move_stack),
                     pending=None)

    # In book: the bot answers with the line's next move.
    if len(board.move_stack) < rep.plies:
        board.push(rep.moves[len(board.move_stack)])
        db().update_game(game_id, moves=" ".join(m.uci() for m in board.move_stack))

    if len(board.move_stack) >= rep.plies:
        return await _end_rep(game_id, rep)

    return rep_payload(db().get_game(game_id), rep)


async def _correct(game_id: int, rep: Rep, board: chess.Board,
                   expected: chess.Move, played: chess.Move) -> dict:
    """Explain the move the line wanted, and hand the position back.

    The old behaviour was to end the rep here. That taught nothing: you were
    told the book move at the exact moment you could no longer play it, and a
    line you got wrong on move four was never played through at all. Now the
    move is simply not made — the position stands, the reason is spelled out,
    and you play on. The cost is that the line no longer counts as known: see
    :func:`_end_rep`.
    """
    ply = len(board.move_stack)
    dev = await analyse_deviation(
        board=board,
        book_move=expected,
        played_move=played,
        line_name=rep.name,
        tail=list(rep.moves[ply:]),
        book=state["book"],
        stockfish=state["stockfish"],
    )
    explanation = await state["coach"].explain_deviation(dev, board, round(db().get_player().rating))

    correction = {
        "ply": ply,
        "played": dev.played.san,
        "played_uci": dev.played.uci,
        "expected": dev.book.san,
        "expected_uci": dev.book.uci,
        "severity": dev.severity,
        "explanation": explanation.text,
        "coached": explanation.used_llm,
        "book_win_pct": dev.book.win_pct,
        "your_win_pct": dev.played.win_pct,
        "transposition": dev.transposition,
    }
    row = db().get_game(game_id)
    history = json.loads(row["corrections"] or "[]")
    history.append({"ply": ply, "played": dev.played.san, "expected": dev.book.san})
    db().update_game(game_id, pending=json.dumps(correction),
                     corrections=json.dumps(history))
    return rep_payload(db().get_game(game_id), rep)


async def _end_rep(game_id: int, rep: Rep) -> dict:
    """Close a rep at the end of the line and record whether it was clean.

    Every rep now reaches the end of the line — you cannot leave it — so the
    question is no longer "did you finish" but "did you need telling". A line
    counts as known only when played start to finish with no corrections, which
    is the whole point: being shown the move is not the same as knowing it.
    """
    row = db().get_game(game_id)
    family = row["opening_name"]
    color_name = row["player_color"]
    corrections = json.loads(row["corrections"] or "[]")
    clean = not corrections

    db().record_line_attempt(family, color_name, rep.key, rep.name, clean)
    db().update_game(game_id, result="*", pending=None,
                     termination="rep_passed" if clean else "rep_corrected",
                     finished_at=now())

    extra: dict = {
        "status": "passed" if clean else "corrected",
        "line": rep.san_line(),
        "your_plies": rep.your_plies(),
        "corrected_plies": [c["ply"] for c in corrections],
    }

    progress = _progress_map(family, color_name)
    curriculum = curriculum_for(family, rep.color)
    entry = progress.get(rep.key, Progress())
    extra["progress"] = {
        "streak": entry.streak,
        "needed": MASTERY_STREAK,
        "mastered": entry.mastered,
        "unlocked": len(curriculum.unlocked(progress)),
        "total": len(curriculum.reps),
        "mastered_total": sum(
            1 for r in curriculum.reps if progress.get(r.key, Progress()).mastered
        ),
    }
    return rep_payload(db().get_game(game_id), rep, extra)


# --------------------------------------------------------------------------
# Puzzles
# --------------------------------------------------------------------------


def puzzle_payload(row, *, reveal: bool = False) -> dict:
    """What the client is allowed to know.

    The solution and the theme are withheld until the puzzle is over. Sending
    either would answer the question: the prompt is "find the best move", and a
    client that has been told "this is a fork" has been told the move.
    """
    moves = row["moves"].split()
    board, first = opening_position(row["fen"], moves)
    # The solver's colour is fixed by the position they were handed, not by
    # whose turn it happens to be now. Recomputing it from the live board flips
    # it on the last move and turns the board round at the moment of solving.
    solver = "white" if board.turn == chess.WHITE else "black"
    played = row["played"]
    for uci in moves[1:played]:
        board.push(chess.Move.from_uci(uci))

    payload = {
        "id": row["puzzle_id"] if "puzzle_id" in row.keys() else row["id"],
        "fen": board.fen(),
        "opponent_move": moves[played - 1],
        "you_play": solver,
        "legal_moves": [m.uci() for m in board.legal_moves],
        "moves_found": (played - 1 + 1) // 2,
        "moves_total": (len(moves)) // 2,
        "wrong": row["wrong"],
        "rating": row["rating"],
        "status": "playing",
    }
    if reveal:
        entry = describe(row["motif"])
        payload.update({
            "status": "solved" if row["wrong"] == 0 else "solved_with_help",
            "motif": row["motif"],
            "theme": row["theme"],
            "direction": entry.direction if entry else None,
            "note": entry.note if entry else None,
            "solution": moves[1:],
        })
    return payload


@app.get("/api/puzzles")
async def puzzle_status():
    counts = db().puzzles_by_theme()
    return {
        "total": db().puzzle_count(),
        "by_theme": counts,
        "scores": db().puzzle_scores(),
        "ready": db().puzzle_count() > 0,
    }


@app.post("/api/puzzles/next")
async def next_puzzle():
    """Draw a puzzle for whichever weakness is currently costing most games."""
    if not db().puzzle_count():
        raise HTTPException(
            409, "No puzzles stored yet — run scripts/ingest_puzzles.py")

    open_row = db().current_puzzle()
    if open_row is not None:
        return puzzle_payload(open_row)          # resume rather than lose it

    stats = await get_stats()
    motif = pick_motif(stats.get("weaknesses", []), state["rng"])
    if motif is None:
        # Nothing reviewed yet, so there is no ranking to draw from. Hanging
        # pieces is the right default: it is the commonest error at this level
        # by a distance, in every set of games measured so far.
        motif = "hung_piece"

    for candidate in (motif, *MOTIF_THEMES):
        theme = pick_theme(candidate, state["rng"])
        row = db().random_puzzle(theme) if theme else None
        if row is not None:
            motif = candidate
            break
    else:
        raise HTTPException(409, "Every stored puzzle has been served already")

    served_at = db().record_puzzle_served(row["id"], motif, theme)
    return puzzle_payload(db().current_puzzle())


@app.post("/api/puzzles/{puzzle_id}/move")
async def puzzle_move(puzzle_id: str, move_in: PuzzleMove):
    row = db().current_puzzle()
    if row is None or row["puzzle_id"] != puzzle_id:
        raise HTTPException(404, "That puzzle is not the one in progress")

    moves = row["moves"].split()
    played, wrong = row["played"], row["wrong"]
    expected = moves[played]

    if move_in.uci != expected:
        # Wrong: counted once, then the position is handed straight back. Same
        # bargain as a rep — you may finish it, but it no longer counts clean.
        db().advance_puzzle(puzzle_id, row["served_at"], played, wrong + 1)
        again = db().current_puzzle()
        payload = puzzle_payload(again)
        payload["last_move_wrong"] = True
        return payload

    played += 1                                   # yours
    if played < len(moves):
        played += 1                               # and the reply that follows

    if played >= len(moves):
        db().advance_puzzle(puzzle_id, row["served_at"], played, wrong)
        done = db().current_puzzle()
        payload = puzzle_payload(done, reveal=True)
        db().finish_puzzle(puzzle_id, row["served_at"], wrong == 0, wrong)
        return payload

    db().advance_puzzle(puzzle_id, row["served_at"], played, wrong)
    return puzzle_payload(db().current_puzzle())


@app.post("/api/puzzles/{puzzle_id}/give_up")
async def puzzle_give_up(puzzle_id: str):
    row = db().current_puzzle()
    if row is None or row["puzzle_id"] != puzzle_id:
        raise HTTPException(404, "That puzzle is not the one in progress")
    payload = puzzle_payload(row, reveal=True)
    payload["status"] = "gave_up"
    db().finish_puzzle(puzzle_id, row["served_at"], False, row["wrong"] + 1)
    return payload


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------


@app.post("/api/games/{game_id}/review")
async def start_review(game_id: int, background: BackgroundTasks):
    row = db().get_game(game_id)
    if row is None:
        raise HTTPException(404, "No such game")
    existing = db().get_review(game_id)
    if existing and existing["status"] in ("running", "done"):
        return existing
    db().upsert_review(game_id, "pending")
    background.add_task(_run_review, game_id)
    return {"status": "pending", "progress": 0.0, "data": None, "error": None}


def _review_elo(row) -> int:
    """What level to pitch the review at.

    For an imported game that is your chess.com rating *on the day it was
    played*, which is both more accurate and more stable than today's — a game
    from six months ago should be explained at the level you were then. For a
    trainer game it is the opponent dial, and failing both, your current one.
    """
    return row["player_rating"] or row["bot_elo"] or db().get_player().bot_elo


async def _run_review(game_id: int) -> None:
    """Analyse in the background so the request returns immediately.

    A 40-move game is roughly a minute of engine time; blocking a request for
    that would time out any proxy in front of us, and the UI wants to stream
    progress anyway.
    """
    row = db().get_game(game_id)
    if row is None:
        return
    try:
        db().upsert_review(game_id, "running")
        board = board_of(row)
        moves = list(board.move_stack)
        if not moves:
            db().upsert_review(game_id, "error", error="That game has no moves")
            return

        hero = chess.WHITE if row["player_color"] == "white" else chess.BLACK
        total = len(moves) + 1

        def progress(done: int, _total: int) -> None:
            db().upsert_review(game_id, "running", progress=done / total)

        report = await review_game(
            moves,
            state["stockfish"],
            maia=state.get("maia"),
            player_elo=_review_elo(row),
            hero=hero,
            depth=REVIEW_DEPTH,
            progress=progress,
        )
        db().upsert_review(game_id, "done", progress=1.0, data=report.to_dict())
        log.info("reviewed game %d (%d plies)", game_id, len(moves))
    except Exception as exc:  # noqa: BLE001
        log.exception("review of game %d failed", game_id)
        db().upsert_review(game_id, "error", error=str(exc))


@app.get("/api/games/{game_id}/review")
async def get_review(game_id: int):
    review = db().get_review(game_id)
    if review is None:
        raise HTTPException(404, "That game has not been reviewed")
    review["explanations"] = db().get_explanations(game_id)
    return review


@app.get("/api/games/{game_id}/explain/{ply}")
async def explain(game_id: int, ply: int):
    """Narrate one reviewed move. Cached: the same move never costs twice."""
    cached = db().get_explanations(game_id).get(ply)
    if cached:
        return cached

    row = db().get_game(game_id)
    review = db().get_review(game_id)
    if row is None or review is None or review["status"] != "done":
        raise HTTPException(409, "Review that game first")

    entry = next((m for m in review["data"]["moves"] if m["ply"] == ply), None)
    if entry is None:
        raise HTTPException(404, "No such move in that game")

    board = chess.Board()
    for uci in (row["moves"] or "").split()[:ply]:
        board.push(chess.Move.from_uci(uci))

    move_review = _rehydrate(entry)
    explanation = await state["coach"].explain(
        move_review, board, _review_elo(row)
    )
    db().save_explanation(game_id, ply, explanation.text, explanation.used_llm)
    return {"text": explanation.text, "used_llm": explanation.used_llm}


def _rehydrate(entry: dict):
    """Rebuild the subset of MoveReview the coach needs from stored JSON."""
    from .eval import Judgment
    from .motifs import Finding, Motif
    from .review import MoveReview

    human = entry.get("human", {})
    return MoveReview(
        ply=entry["ply"],
        san=entry["san"],
        uci=entry["uci"],
        white_to_move=entry["color"] == "white",
        phase=entry["phase"],
        eval_before=None,
        eval_after=None,
        win_before=entry["win_before"],
        win_after=entry["win_after"],
        accuracy=entry["accuracy"],
        judgment=Judgment(entry["judgment"]) if entry["judgment"] else None,
        best_move_san=entry["best_move"],
        best_pv_san=entry["best_line"],
        refutation_san=entry["refutation"],
        findings=[
            Finding(
                motif=Motif(f["motif"]),
                phrase=f["statement"],
                squares=f["squares"],
                material=f["material_cp"],
            )
            for f in entry["findings"]
        ],
        human_probability=(human.get("played_pct") or 0) / 100 or None,
        human_rank=human.get("played_rank"),
        best_human_probability=(human.get("best_pct") or 0) / 100 or None,
    )


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------


#: Sent with the page and every asset. See RevalidatedStatic below.
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "maia": state.get("maia") is not None}


@lru_cache(maxsize=1)
def asset_version() -> str:
    """A short hash of the front end, used to bust an already-stale cache.

    "no-cache" only governs responses sent from now on. A browser that cached
    app.js under the old rules still holds it, and the URL is what it keys on —
    so the entry points are versioned, and a deploy arrives without anyone
    having to know to hard-refresh.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "style.css"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text()
    for name in ("app.js", "style.css"):
        html = html.replace(f"/static/{name}", f"/static/{name}?v={asset_version()}")
    return HTMLResponse(html, headers=NO_CACHE)


class RevalidatedStatic(StaticFiles):
    """Static files the browser must check before reusing.

    Starlette sends an ETag and Last-Modified but no Cache-Control, which leaves
    the browser free to apply *heuristic* freshness — roughly a tenth of the
    file's age — and serve a stale copy without asking. That is invisible on the
    server: the app is redeployed, the API answers with the new fields, and the
    page carries on running last week's JavaScript against them. It cost a
    release of the reps correction panel, which worked in every test and showed
    nothing in the browser.

    "no-cache" does not mean "do not store"; it means "revalidate first". The
    browser still keeps the file and still gets a 304 when nothing changed, so
    on a single-user LAN app this is a couple of conditional requests per load
    in exchange for deploys that actually arrive.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


app.mount("/static", RevalidatedStatic(directory=STATIC_DIR), name="static")
