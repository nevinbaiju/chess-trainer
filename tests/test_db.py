"""Storage: the parts where a schema change could quietly lose your history."""

import sqlite3

import pytest

from app.db import Database


# --------------------------------------------------------------------------
# Line progress survives a rebuilt curriculum
# --------------------------------------------------------------------------


def test_progress_is_keyed_by_the_moves(tmp_path):
    """Not by the name: a line's name comes from classifying the position it
    reaches, so it changes whenever the curriculum is rebuilt."""
    db = Database(tmp_path / "t.db")
    moves = "e2e4 e7e5 g1f3 b8c6 f1b5"
    db.record_line_attempt("Ruy Lopez", "white", moves, "Ruy Lopez", True)
    db.record_line_attempt("Ruy Lopez", "white", moves, "Ruy Lopez: Renamed", True)

    stored = db.get_line_progress("Ruy Lopez", "white")
    assert list(stored) == [moves], "a rename must not split the row"
    assert stored[moves]["streak"] == 2


def test_an_old_name_keyed_table_is_set_aside_for_reconciliation(tmp_path):
    """Upgrading an install must not drop what is already there; the rows are
    moved aside because turning a name into moves needs the opening book."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """CREATE TABLE line_progress (
               family TEXT NOT NULL, color TEXT NOT NULL, line TEXT NOT NULL,
               streak INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
               successes INTEGER NOT NULL DEFAULT 0, last_seen TEXT,
               PRIMARY KEY (family, color, line));
           INSERT INTO line_progress VALUES ('Ruy Lopez','white','Ruy Lopez: Open',2,3,2,'x');"""
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    assert db.get_line_progress("Ruy Lopez", "white") == {}
    waiting = db.legacy_line_progress()
    assert len(waiting) == 1
    assert waiting[0]["line"] == "Ruy Lopez: Open"
    assert waiting[0]["streak"] == 2


def test_two_old_rows_landing_on_one_line_keep_the_better_streak(tmp_path):
    """Trimming trailing opponent moves merged the Ruy Lopez Closed and Open
    into a single White rep. Two histories, one line: the best streak stands
    and the attempts add up."""
    db = Database(tmp_path / "t.db")
    moves = "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1"
    db.restore_line_progress("Ruy Lopez", "white", moves, "Closed", 3, 5, 3, "a")
    db.restore_line_progress("Ruy Lopez", "white", moves, "Open", 2, 2, 2, "b")

    stored = db.get_line_progress("Ruy Lopez", "white")[moves]
    assert stored["streak"] == 3
    assert stored["attempts"] == 7
    assert stored["successes"] == 5


# --------------------------------------------------------------------------
# chess.com archive cache
# --------------------------------------------------------------------------


def test_an_archive_etag_round_trips(tmp_path):
    """Closed months never change, so after the first fetch they should cost a
    conditional request and a 304 rather than a download."""
    db = Database(tmp_path / "t.db")
    url = "https://api.chess.com/pub/player/x/games/2026/09"
    assert db.archive_etag(url, "rapid") is None
    db.save_archive(url, '"abc123"', 9, "rapid")
    assert db.archive_etag(url, "rapid") == '"abc123"'
    db.save_archive(url, '"def456"', 11, "rapid")
    assert db.archive_etag(url, "rapid") == '"def456"'


def test_asking_for_a_new_time_class_re_reads_the_month(tmp_path):
    """A month imported as "rapid only" would otherwise 304 forever, and the
    blitz games in it would never arrive however often the import was re-run."""
    db = Database(tmp_path / "t.db")
    url = "https://api.chess.com/pub/player/x/games/2026/09"
    db.save_archive(url, '"abc123"', 9, "rapid")
    assert db.archive_etag(url, "rapid") == '"abc123"'
    assert db.archive_etag(url, "blitz,rapid") is None


def test_reimporting_the_same_game_is_refused_by_the_schema(tmp_path):
    """games.external_id is UNIQUE — the last line of defence if the ETag cache
    and the game_exists check both miss."""
    db = Database(tmp_path / "t.db")
    db.create_game(player_color="white", source="chesscom", external_id="uuid-1")
    assert db.game_exists("uuid-1") is True
    with pytest.raises(sqlite3.IntegrityError):
        db.create_game(player_color="white", source="chesscom", external_id="uuid-1")


def test_only_unreviewed_finished_games_are_queued(tmp_path):
    db = Database(tmp_path / "t.db")
    done = db.create_game(player_color="white", source="chesscom",
                          external_id="a", result="1-0")
    db.create_game(player_color="white", source="chesscom", external_id="b", result="0-1")
    db.create_game(player_color="white", source="chesscom", external_id="c")   # unfinished
    db.create_game(player_color="white", source="trainer", result="1-0")       # other source
    db.upsert_review(done, "done")

    queued = [r["external_id"] for r in db.unreviewed_games("chesscom", 10)]
    assert queued == ["b"]
    assert db.count_games("chesscom") == (3, 1)
