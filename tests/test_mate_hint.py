"""Mate hints, and the settings storage behind them.

The engine tests skip on the host (no Stockfish there) and run inside the
container. The storage tests run everywhere.
"""

import asyncio
import io
import shutil

import chess
import chess.pgn
import pytest

from app.db import Database

HAS_STOCKFISH = shutil.which("stockfish") is not None
needs_engine = pytest.mark.skipif(HAS_STOCKFISH is False, reason="stockfish not on PATH")

# Morphy's Opera Game: 16.Qb8+ Nxb8 17.Rd8#. So before White's 17th it is mate
# in 1, and before the 16th it is mate in 2 — distances we can assert exactly.
OPERA = """
1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6
7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5 10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O Rd8
13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0
"""


def opera_positions() -> list[chess.Board]:
    board = chess.Board()
    out = [board.copy()]
    for move in chess.pgn.read_game(io.StringIO(OPERA)).mainline_moves():
        board.push(move)
        out.append(board.copy())
    return out


def run(coro):
    return asyncio.run(coro)


async def _with_engine(fn):
    from app.engines import Stockfish

    engine = Stockfish()
    await engine.start()
    try:
        return await fn(engine)
    finally:
        await engine.close()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


@needs_engine
def test_finds_mate_in_one_and_two():
    positions = opera_positions()

    async def check(engine):
        return (
            await engine.find_mate(positions[32], max_moves=3),  # before 17.Rd8#
            await engine.find_mate(positions[30], max_moves=3),  # before 16.Qb8+
        )

    mate_in_one, mate_in_two = run(_with_engine(check))
    assert mate_in_one == 1
    assert mate_in_two == 2


@needs_engine
def test_cap_suppresses_a_mate_that_is_too_deep():
    """The cap is the whole point: a mate in 7 is not a useful beginner hint."""
    positions = opera_positions()

    async def check(engine):
        return [
            await engine.find_mate(positions[30], max_moves=cap) for cap in (1, 2, 3, 5)
        ]

    assert run(_with_engine(check)) == [None, 2, 2, 2]


@needs_engine
def test_no_mate_in_ordinary_positions():
    async def check(engine):
        quiet = chess.Board(
            "r2q1rk1/1b2bppp/p2ppn2/1p6/3NPP2/2N1B3/PPPQ2PP/2KR1B1R w - - 0 13"
        )
        return (
            await engine.find_mate(chess.Board(), max_moves=3),
            await engine.find_mate(quiet, max_moves=3),
        )

    assert run(_with_engine(check)) == (None, None)


@needs_engine
def test_being_mated_is_not_reported_as_an_opportunity():
    """The hint is about mates *you* can play, not ones about to land on you."""
    positions = opera_positions()

    async def check(engine):
        return await engine.find_mate(positions[31], max_moves=3)  # Black, getting mated

    assert run(_with_engine(check)) is None


@needs_engine
def test_finished_games_are_not_probed():
    async def check(engine):
        mated = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
        assert mated.is_game_over()
        return await engine.find_mate(mated, max_moves=3)

    assert run(_with_engine(check)) is None


# --------------------------------------------------------------------------
# Settings storage
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")


def test_settings_start_empty_and_round_trip(db):
    assert db.get_settings() == {}
    db.save_settings({"mate_hints": False, "mate_hint_max": 2})
    assert db.get_settings() == {"mate_hints": False, "mate_hint_max": 2}


def test_settings_survive_reopening(db, tmp_path):
    db.save_settings({"mate_hints": False})
    db.close()
    assert Database(tmp_path / "t.db").get_settings() == {"mate_hints": False}


def test_corrupt_settings_degrade_to_defaults(db):
    """A preference blob must never be able to take the app down."""
    db.conn.execute("UPDATE player SET settings='not json' WHERE id=1")
    db.conn.commit()
    assert db.get_settings() == {}


def test_migration_adds_settings_to_an_older_database(tmp_path):
    """Databases created before this column exists must still open.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so without an
    explicit ALTER an upgrade would break every existing install.
    """
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE player (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rating REAL NOT NULL, rd REAL NOT NULL, volatility REAL NOT NULL,
            bot_elo INTEGER NOT NULL, recent_scores TEXT NOT NULL DEFAULT '[]',
            chesscom_username TEXT
        );
        INSERT INTO player VALUES (1, 1200, 350, 0.06, 1000, '[]', 'someone');
        """
    )
    old.commit()
    old.close()

    migrated = Database(path)
    assert migrated.get_settings() == {}
    assert migrated.get_setting("chesscom_username") == "someone"
    assert migrated.get_player().bot_elo == 1000  # existing data preserved
    migrated.save_settings({"mate_hints": False})
    assert migrated.get_settings() == {"mate_hints": False}


def test_games_started_in_the_same_second_still_order_newest_first(db):
    """created_at has second resolution, so id has to break the tie.

    Without it, "resume my current game" could hand back an older game — which
    is exactly what happened before this was fixed.
    """
    stamp = "2026-09-14T10:00:00+00:00"
    ids = [
        db.create_game(created_at=stamp, player_color="white", source="trainer", moves="e2e4")
        for _ in range(5)
    ]
    listed = [r["id"] for r in db.list_games(limit=10)]
    assert listed == sorted(ids, reverse=True)
    assert db.list_games(limit=10, source="trainer")[0]["id"] == max(ids)


def test_a_review_interrupted_by_a_restart_is_recoverable(db):
    """Reviews run as in-process background tasks, so a restart abandons them.

    Left alone the row sits at 'running' forever and the UI polls a job that no
    longer exists — it waits indefinitely with a progress bar that never moves.
    """
    game_id = db.create_game(player_color="white", source="trainer", moves="e2e4")
    db.upsert_review(game_id, "running", progress=0.4)

    assert db.recover_orphaned_reviews() == 1
    review = db.get_review(game_id)
    assert review["status"] == "error"
    assert "restart" in review["error"]


def test_recovery_leaves_completed_reviews_alone(db):
    game_id = db.create_game(player_color="white", source="trainer", moves="e2e4")
    db.upsert_review(game_id, "done", progress=1.0, data={"moves": []})
    assert db.recover_orphaned_reviews() == 0
    assert db.get_review(game_id)["status"] == "done"
