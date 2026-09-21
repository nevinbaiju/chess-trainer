"""SQLite persistence.

One user, one process, a few thousand games: SQLite in WAL mode is the right
size of tool and Postgres would be pure operational overhead. Plain ``sqlite3``
rather than an ORM for the same reason — the schema is five tables and the
queries are short.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .rating import DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOLATILITY, Player

DB_PATH = Path(os.environ.get("CHESS_DB", "/data/chess.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS player (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    rating            REAL    NOT NULL,
    rd                REAL    NOT NULL,
    volatility        REAL    NOT NULL,
    bot_elo           INTEGER NOT NULL,
    recent_scores     TEXT    NOT NULL DEFAULT '[]',
    chesscom_username TEXT,
    settings          TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    source        TEXT NOT NULL DEFAULT 'trainer',   -- trainer | chesscom
    external_id   TEXT UNIQUE,                       -- chess.com game uuid
    player_color  TEXT NOT NULL,                     -- white | black
    bot_elo       INTEGER,
    mode          TEXT,
    opening_eco   TEXT,
    opening_name  TEXT,
    drill_line    TEXT,
    result        TEXT,                              -- 1-0 | 0-1 | 1/2-1/2
    termination   TEXT,
    moves         TEXT NOT NULL DEFAULT '',          -- space-separated UCI
    pgn           TEXT,
    player_rating   INTEGER,                         -- chess.com: your rating then
    opponent_rating INTEGER,                         -- chess.com: theirs
    takebacks     INTEGER NOT NULL DEFAULT 0,        -- guarded retries used
    pending       TEXT,                              -- a guard warning awaiting your answer
    corrections   TEXT                               -- reps: the moves you needed help with
);
CREATE INDEX IF NOT EXISTS games_created  ON games(created_at DESC);
CREATE INDEX IF NOT EXISTS games_opening  ON games(opening_name);

CREATE TABLE IF NOT EXISTS reviews (
    game_id    INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL,       -- pending | running | done | error
    progress   REAL NOT NULL DEFAULT 0,
    error      TEXT,
    data       TEXT                 -- the serialised GameReview
);

-- Per-line mastery for Reps mode. Keyed by the catalogue's line name, which is
-- stable across rebuilds of the curriculum.
-- Keyed by the MOVES, not the name. A line's name is derived: it comes from
-- classifying the position the line reaches, so it changes whenever the
-- curriculum is rebuilt differently, and progress keyed on it silently
-- orphans. The moves are the exercise and never change meaning.
CREATE TABLE IF NOT EXISTS line_progress (
    family    TEXT NOT NULL,
    color     TEXT NOT NULL,          -- white | black
    moves     TEXT NOT NULL,          -- space-separated UCI: the identity
    line      TEXT NOT NULL,          -- display name, refreshed on each write
    streak    INTEGER NOT NULL DEFAULT 0,
    attempts  INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    PRIMARY KEY (family, color, moves)
);

-- One row per month of chess.com games. Closed months never change, so after
-- the first fetch they cost a conditional request and a 304.
CREATE TABLE IF NOT EXISTS chesscom_archives (
    url        TEXT PRIMARY KEY,
    etag       TEXT,
    fetched_at TEXT NOT NULL,
    games      INTEGER NOT NULL DEFAULT 0,
    filters    TEXT                        -- time classes this month was read with
);

-- A local slice of the Lichess CC0 puzzle database. Not rating-filtered: any
-- puzzle carrying the theme is fair game, so the only sampling is a cap per
-- theme to keep the table small.
CREATE TABLE IF NOT EXISTS puzzles (
    id      TEXT PRIMARY KEY,        -- lichess puzzle id
    fen     TEXT NOT NULL,           -- position BEFORE the opponent's first move
    moves   TEXT NOT NULL,           -- UCI; [0] is the opponent's, then alternating
    rating  INTEGER,                 -- stored but not used to select; see ROADMAP
    themes  TEXT NOT NULL            -- space separated, lichess vocabulary
);
CREATE INDEX IF NOT EXISTS puzzles_rating ON puzzles(rating);

-- One row per puzzle served, so nothing repeats and progress per motif can be
-- counted. `motif` is ours, `theme` is the lichess one it was drawn from.
CREATE TABLE IF NOT EXISTS puzzle_attempts (
    puzzle_id TEXT NOT NULL REFERENCES puzzles(id) ON DELETE CASCADE,
    motif     TEXT NOT NULL,
    theme     TEXT NOT NULL,
    served_at TEXT NOT NULL,
    solved    INTEGER,               -- NULL = served but not finished
    wrong     INTEGER NOT NULL DEFAULT 0,
    -- How far through the solution they are. Starts at 1: index 0 is the
    -- opponent's move, already on the board. Server-authoritative, so the
    -- client cannot advance itself past a move it did not find.
    played    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (puzzle_id, served_at)
);
CREATE INDEX IF NOT EXISTS puzzle_attempts_motif ON puzzle_attempts(motif);

CREATE TABLE IF NOT EXISTS explanations (
    game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply      INTEGER NOT NULL,
    text     TEXT    NOT NULL,
    used_llm INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, ply)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the review job write while the UI reads.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._rekey_line_progress()
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._ensure_player()
        self.conn.commit()

    #: (table, column, definition) added by later versions.
    MIGRATIONS = (
        ("player", "settings", "TEXT NOT NULL DEFAULT '{}'"),
        ("games", "takebacks", "INTEGER NOT NULL DEFAULT 0"),
        ("games", "pending", "TEXT"),
        ("games", "corrections", "TEXT"),
        ("games", "player_rating", "INTEGER"),
        ("games", "opponent_rating", "INTEGER"),
        ("chesscom_archives", "filters", "TEXT"),
    )

    def _rekey_line_progress(self) -> None:
        """Move an old name-keyed progress table aside for reconciliation.

        The rows cannot be converted here: turning a line name into a move
        sequence needs the opening book and the curriculum, which live a layer
        up. So the old table is kept under another name and app.main resolves it
        at startup, once it can build the curricula to match against.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(line_progress)")}
        if not have or "moves" in have:
            return
        self.conn.execute("ALTER TABLE line_progress RENAME TO line_progress_legacy")
        self.conn.executescript(SCHEMA)

    def legacy_line_progress(self) -> list[dict]:
        """Name-keyed rows still waiting to be matched to a line."""
        try:
            rows = self.conn.execute("SELECT * FROM line_progress_legacy").fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def drop_legacy_line_progress(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS line_progress_legacy")
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for databases created by an earlier version.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
        so new columns have to be added explicitly or an upgrade breaks every
        install that already has data.
        """
        for table, column, definition in self.MIGRATIONS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    # -- player ------------------------------------------------------------

    def _ensure_player(self) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO player
               (id, rating, rd, volatility, bot_elo, recent_scores)
               VALUES (1, ?, ?, ?, ?, '[]')""",
            (DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOLATILITY, 1000),
        )

    def get_player(self) -> Player:
        row = self.conn.execute("SELECT * FROM player WHERE id = 1").fetchone()
        return Player(
            rating=row["rating"],
            rd=row["rd"],
            volatility=row["volatility"],
            bot_elo=row["bot_elo"],
            recent_scores=json.loads(row["recent_scores"]),
        )

    def save_player(self, player: Player) -> None:
        self.conn.execute(
            """UPDATE player SET rating=?, rd=?, volatility=?, bot_elo=?, recent_scores=?
               WHERE id = 1""",
            (
                player.rating,
                player.rd,
                player.volatility,
                player.bot_elo,
                json.dumps(player.recent_scores),
            ),
        )
        self.conn.commit()

    def get_settings(self) -> dict:
        row = self.conn.execute("SELECT settings FROM player WHERE id = 1").fetchone()
        try:
            return json.loads(row["settings"]) if row and row["settings"] else {}
        except (TypeError, ValueError):
            return {}

    def save_settings(self, settings: dict) -> None:
        self.conn.execute(
            "UPDATE player SET settings = ? WHERE id = 1", (json.dumps(settings),)
        )
        self.conn.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute(f"SELECT {key} AS v FROM player WHERE id = 1").fetchone()
        return row["v"] if row else None

    def get_chesscom_username(self) -> str | None:
        row = self.conn.execute(
            "SELECT chesscom_username FROM player WHERE id = 1"
        ).fetchone()
        return row["chesscom_username"] if row else None

    def set_chesscom_username(self, username: str | None) -> None:
        self.conn.execute("UPDATE player SET chesscom_username=? WHERE id=1", (username,))
        self.conn.commit()

    # -- games -------------------------------------------------------------

    def create_game(self, **fields: Any) -> int:
        fields.setdefault("created_at", now())
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cursor = self.conn.execute(
            f"INSERT INTO games ({columns}) VALUES ({placeholders})", tuple(fields.values())
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_game(self, game_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE games SET {assignments} WHERE id=?", (*fields.values(), game_id)
        )
        self.conn.commit()

    def get_game(self, game_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()

    def list_games(self, limit: int = 50, source: str | None = None) -> list[sqlite3.Row]:
        """Newest first.

        `id DESC` is not decoration: created_at has second resolution, so two
        games started in the same second tie and the order becomes arbitrary —
        which silently made "resume my current game" pick the wrong one.
        created_at leads so that imported games, which carry historical dates,
        still sort by when they were played rather than when they were stored.
        """
        if source:
            return self.conn.execute(
                "SELECT * FROM games WHERE source=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM games ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- puzzles -----------------------------------------------------------

    def puzzle_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM puzzles").fetchone()[0])

    def puzzles_by_theme(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT themes FROM puzzles").fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            for theme in r["themes"].split():
                counts[theme] = counts.get(theme, 0) + 1
        return counts

    def random_puzzle(self, theme: str) -> sqlite3.Row | None:
        """An unseen puzzle carrying this theme, chosen at random.

        `themes` is a space separated list, so the LIKE has to match a whole
        word — without the padding, "pin" would also match "pinnedPiece" style
        neighbours and, worse, any theme that merely contains it.
        """
        return self.conn.execute(
            """SELECT * FROM puzzles
               WHERE ' ' || themes || ' ' LIKE ?
                 AND id NOT IN (SELECT puzzle_id FROM puzzle_attempts)
               ORDER BY RANDOM() LIMIT 1""",
            (f"% {theme} %",),
        ).fetchone()

    def get_puzzle(self, puzzle_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM puzzles WHERE id=?", (puzzle_id,)
        ).fetchone()

    def record_puzzle_served(self, puzzle_id: str, motif: str, theme: str) -> str:
        served_at = now()
        self.conn.execute(
            """INSERT INTO puzzle_attempts (puzzle_id, motif, theme, served_at)
               VALUES (?, ?, ?, ?)""",
            (puzzle_id, motif, theme, served_at),
        )
        self.conn.commit()
        return served_at

    def open_attempt(self, puzzle_id: str) -> sqlite3.Row | None:
        """The unfinished attempt at this puzzle, if there is one."""
        return self.conn.execute(
            "SELECT * FROM puzzle_attempts WHERE puzzle_id=? AND solved IS NULL "
            "ORDER BY served_at DESC LIMIT 1", (puzzle_id,)
        ).fetchone()

    def current_puzzle(self) -> sqlite3.Row | None:
        """The puzzle in front of the player, so a refresh does not lose it."""
        return self.conn.execute(
            """SELECT a.*, p.fen, p.moves, p.rating, p.themes
               FROM puzzle_attempts a JOIN puzzles p ON p.id = a.puzzle_id
               WHERE a.solved IS NULL ORDER BY a.served_at DESC LIMIT 1"""
        ).fetchone()

    def advance_puzzle(self, puzzle_id: str, served_at: str, played: int,
                       wrong: int) -> None:
        self.conn.execute(
            "UPDATE puzzle_attempts SET played=?, wrong=? "
            "WHERE puzzle_id=? AND served_at=?",
            (played, wrong, puzzle_id, served_at),
        )
        self.conn.commit()

    def finish_puzzle(self, puzzle_id: str, served_at: str, solved: bool,
                      wrong: int) -> None:
        self.conn.execute(
            "UPDATE puzzle_attempts SET solved=?, wrong=? "
            "WHERE puzzle_id=? AND served_at=?",
            (1 if solved else 0, wrong, puzzle_id, served_at),
        )
        self.conn.commit()

    def puzzle_scores(self) -> dict[str, dict]:
        """Solved / tried per motif, for the "is this working" line."""
        rows = self.conn.execute(
            """SELECT motif, COUNT(*) AS tried,
                      SUM(CASE WHEN solved=1 THEN 1 ELSE 0 END) AS solved
               FROM puzzle_attempts WHERE solved IS NOT NULL
               GROUP BY motif"""
        ).fetchall()
        return {r["motif"]: {"tried": r["tried"], "solved": int(r["solved"] or 0)}
                for r in rows}

    def archive_etag(self, url: str, filters: str) -> str | None:
        """The stored tag, but only if the month was read with these filters.

        Otherwise a month that was imported as "rapid only" would 304 forever
        and the blitz games in it would never arrive, however many times the
        import was re-run with blitz asked for.
        """
        row = self.conn.execute(
            "SELECT etag, filters FROM chesscom_archives WHERE url=?", (url,)
        ).fetchone()
        if row is None or row["filters"] != filters:
            return None
        return row["etag"]

    def save_archive(self, url: str, etag: str | None, games: int, filters: str) -> None:
        self.conn.execute(
            """INSERT INTO chesscom_archives (url, etag, fetched_at, games, filters)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   etag=excluded.etag, fetched_at=excluded.fetched_at,
                   games=excluded.games, filters=excluded.filters""",
            (url, etag, now(), games, filters),
        )
        self.conn.commit()

    def unreviewed_games(self, source: str, limit: int = 20) -> list[sqlite3.Row]:
        """Finished games of one source with no completed review, newest first."""
        return self.conn.execute(
            """SELECT g.* FROM games g
               LEFT JOIN reviews r ON r.game_id = g.id AND r.status = 'done'
               WHERE g.source = ? AND g.result IS NOT NULL AND r.game_id IS NULL
               ORDER BY g.created_at DESC, g.id DESC
               LIMIT ?""",
            (source, limit),
        ).fetchall()

    def count_games(self, source: str) -> tuple[int, int]:
        """(games, of which reviewed) for one source."""
        row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN r.status='done' THEN 1 ELSE 0 END) AS done
               FROM games g LEFT JOIN reviews r ON r.game_id = g.id
               WHERE g.source = ?""",
            (source,),
        ).fetchone()
        return int(row["total"] or 0), int(row["done"] or 0)

    def game_exists(self, external_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM games WHERE external_id=?", (external_id,)
            ).fetchone()
            is not None
        )

    # -- reviews -----------------------------------------------------------

    def upsert_review(
        self,
        game_id: int,
        status: str,
        progress: float = 0.0,
        data: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO reviews (game_id, created_at, status, progress, data, error)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id) DO UPDATE SET
                   status=excluded.status,
                   progress=excluded.progress,
                   data=COALESCE(excluded.data, reviews.data),
                   error=excluded.error""",
            (game_id, now(), status, progress, json.dumps(data) if data else None, error),
        )
        self.conn.commit()

    def recover_orphaned_reviews(self) -> int:
        """Fail any review that was mid-flight when the process died.

        Reviews run as in-process background tasks, so a restart abandons them
        silently and the row sits at 'running' forever — the UI polls a job that
        no longer exists and waits indefinitely. Marking them failed at startup
        means they can simply be run again.
        """
        cursor = self.conn.execute(
            """UPDATE reviews SET status='error',
                   error='interrupted by a restart — run it again'
               WHERE status IN ('pending', 'running')"""
        )
        self.conn.commit()
        return cursor.rowcount

    def get_review(self, game_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE game_id=?", (game_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
            "data": json.loads(row["data"]) if row["data"] else None,
        }

    # -- explanations ------------------------------------------------------

    def save_explanation(self, game_id: int, ply: int, text: str, used_llm: bool) -> None:
        self.conn.execute(
            """INSERT INTO explanations (game_id, ply, text, used_llm) VALUES (?,?,?,?)
               ON CONFLICT(game_id, ply) DO UPDATE SET
                   text=excluded.text, used_llm=excluded.used_llm""",
            (game_id, ply, text, int(used_llm)),
        )
        self.conn.commit()

    def get_explanations(self, game_id: int) -> dict[int, dict]:
        rows = self.conn.execute(
            "SELECT ply, text, used_llm FROM explanations WHERE game_id=?", (game_id,)
        ).fetchall()
        return {r["ply"]: {"text": r["text"], "used_llm": bool(r["used_llm"])} for r in rows}

    # -- rep progress ------------------------------------------------------

    def get_line_progress(self, family: str, color: str) -> dict:
        rows = self.conn.execute(
            "SELECT moves, streak, attempts, successes FROM line_progress "
            "WHERE family=? AND color=?",
            (family, color),
        ).fetchall()
        return {
            r["moves"]: {
                "streak": r["streak"],
                "attempts": r["attempts"],
                "successes": r["successes"],
            }
            for r in rows
        }

    def record_line_attempt(
        self, family: str, color: str, moves: str, line: str, passed: bool
    ) -> None:
        """A pass extends the streak; a miss resets it to zero.

        Resetting rather than decrementing is deliberate: the streak is meant to
        mean "I have played this correctly N times running", and a line you just
        got wrong does not meet that however many times you got it right before.
        """
        self.conn.execute(
            """INSERT INTO line_progress
                   (family, color, moves, line, streak, attempts, successes, last_seen)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(family, color, moves) DO UPDATE SET
                   streak    = CASE WHEN ? THEN line_progress.streak + 1 ELSE 0 END,
                   attempts  = line_progress.attempts + 1,
                   successes = line_progress.successes + ?,
                   line      = excluded.line,
                   last_seen = ?""",
            (family, color, moves, line, 1 if passed else 0, 1 if passed else 0, now(),
             passed, 1 if passed else 0, now()),
        )
        self.conn.commit()

    def restore_line_progress(self, family: str, color: str, moves: str, line: str,
                              streak: int, attempts: int, successes: int,
                              last_seen: str | None) -> None:
        """Write a reconciled row, keeping the best of any that merged."""
        self.conn.execute(
            """INSERT INTO line_progress
                   (family, color, moves, line, streak, attempts, successes, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(family, color, moves) DO UPDATE SET
                   streak    = MAX(line_progress.streak, excluded.streak),
                   attempts  = line_progress.attempts + excluded.attempts,
                   successes = line_progress.successes + excluded.successes,
                   last_seen = MAX(COALESCE(line_progress.last_seen, ''),
                                   COALESCE(excluded.last_seen, ''))""",
            (family, color, moves, line, streak, attempts, successes, last_seen),
        )
        self.conn.commit()

    def reset_line_progress(self, family: str, color: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM line_progress WHERE family=? AND color=?", (family, color)
        )
        self.conn.commit()
        return cursor.rowcount

    # -- stats -------------------------------------------------------------

    def finished_games(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM games WHERE result IS NOT NULL ORDER BY created_at"
        ).fetchall()

    def reviews_for_stats(self, source: str | None = None) -> Iterable[tuple[sqlite3.Row, dict]]:
        """Reviewed games, optionally from one source only.

        Filtering matters here: trainer games and chess.com games answer
        different questions, and averaging them hides the one worth knowing —
        whether what you practise is showing up in games that count.
        """
        rows = self.conn.execute(
            """SELECT g.*, r.data AS review_data
               FROM games g JOIN reviews r ON r.game_id = g.id
               WHERE r.status = 'done' AND r.data IS NOT NULL
                 AND (? IS NULL OR g.source = ?)
               ORDER BY g.created_at""",
            (source, source),
        ).fetchall()
        for row in rows:
            yield row, json.loads(row["review_data"])
