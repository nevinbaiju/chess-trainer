"""Replaying your own blunders: which positions, and what counts as fixing one."""

import chess
import pytest

from app.corrections import FORGIVEN, blunders_in, position_before, rank, verdict


class Row(dict):
    """sqlite3.Row is subscript-only; a dict stands in for one in tests."""


def game(moves, color="white", gid=1, opening="Italian Game"):
    return Row(id=gid, player_color=color, moves=" ".join(moves),
               opening_name=opening, created_at="2026-09-20T10:00:00+00:00")


def move(ply, *, judgment=None, color="white", san="Qh5", win_lost=0.0,
         best="Nf3", findings=()):
    return {
        "ply": ply, "san": san, "uci": "d1h5", "color": color,
        "judgment": judgment, "win_lost": win_lost,
        "best_move": best, "best_move_uci": "g1f3",
        "findings": [{"motif": m, "statement": m, "squares": []} for m in findings],
    }


LINE = "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5".split()


# --------------------------------------------------------------------------
# Which positions become drills
# --------------------------------------------------------------------------


def test_only_your_own_blunders_are_collected():
    """Being shown the opponent's blunder and asked to improve it is a
    different exercise, and not the one that was asked for."""
    review = {"moves": [
        move(2, judgment="blunder", color="white", win_lost=30),
        move(3, judgment="blunder", color="black", win_lost=40),
    ]}
    found = blunders_in(game(LINE, color="white"), review)
    assert [b.ply for b in found] == [2]


def test_mistakes_and_inaccuracies_are_left_out():
    review = {"moves": [
        move(0, judgment="inaccuracy", win_lost=6),
        move(2, judgment="mistake", win_lost=12),
        move(4, judgment="blunder", win_lost=25),
    ]}
    assert [b.ply for b in blunders_in(game(LINE), review)] == [4]


def test_the_position_is_the_one_you_faced_not_the_one_you_made():
    """The drill puts you back before the move, with it still to play."""
    review = {"moves": [move(4, judgment="blunder", win_lost=25)]}
    blunder = blunders_in(game(LINE), review)[0]
    board = chess.Board(blunder.fen)
    assert board.turn == chess.WHITE
    assert board.fullmove_number == 3
    assert chess.Move.from_uci(LINE[4]) in board.legal_moves
    assert blunder.played_uci == LINE[4]


def test_a_blunder_past_the_end_of_the_game_is_skipped():
    """Defensive: a review and its game can disagree if one was re-analysed."""
    review = {"moves": [move(99, judgment="blunder", win_lost=25)]}
    assert blunders_in(game(LINE), review) == []


def test_the_worst_one_comes_first():
    review = {"moves": [
        move(0, judgment="blunder", win_lost=10),
        move(2, judgment="blunder", win_lost=55),
        move(4, judgment="blunder", win_lost=30),
    ]}
    order = [b.win_lost for b in rank(blunders_in(game(LINE), review))]
    assert order == [55, 30, 10]


def test_what_you_played_is_recorded_for_the_reveal():
    review = {"moves": [move(4, judgment="blunder", san="Ng5", win_lost=25,
                             findings=["hung_piece"])]}
    b = blunders_in(game(LINE), review)[0]
    assert b.played_san == "Ng5"
    assert b.motifs == ("hung_piece",)
    assert b.key == "1:4"


def test_black_blunders_are_collected_for_a_black_player():
    review = {"moves": [
        move(3, judgment="blunder", color="black", win_lost=25),
        move(4, judgment="blunder", color="white", win_lost=40),
    ]}
    found = blunders_in(game(LINE, color="black"), review)
    assert [b.ply for b in found] == [3]
    assert found[0].color == chess.BLACK


# --------------------------------------------------------------------------
# What counts as fixing it
# --------------------------------------------------------------------------


def test_a_move_as_good_as_the_best_holds():
    assert verdict(win_best=80.0, win_played=80.0)["held"] is True


def test_a_move_that_gives_a_little_away_still_holds():
    """Several moves are usually fine. Failing someone for playing a good move
    that is not Stockfish's first choice teaches only that the app is
    arbitrary."""
    out = verdict(win_best=80.0, win_played=80.0 - (FORGIVEN - 0.1))
    assert out["held"] is True


def test_a_move_that_throws_it_away_does_not():
    out = verdict(win_best=85.6, win_played=16.4)
    assert out["held"] is False
    assert out["lost"] == 69.2


def test_it_is_judged_against_the_best_move_not_against_the_blunder():
    """"Better than the catastrophe" is a bar almost anything clears."""
    assert verdict(win_best=90.0, win_played=40.0)["held"] is False


def test_finding_something_better_than_the_engine_is_not_punished():
    out = verdict(win_best=70.0, win_played=95.0)
    assert out["held"] is True
    assert out["lost"] == 0.0


# --------------------------------------------------------------------------
# Position reconstruction
# --------------------------------------------------------------------------


def test_position_before_ply_zero_is_the_start():
    assert position_before(LINE, 0).fen() == chess.Board().fen()


def test_position_before_replays_exactly_that_many_plies():
    board = position_before(LINE, 4)
    assert len(board.move_stack) == 4


# --------------------------------------------------------------------------
# Not making the player wait
# --------------------------------------------------------------------------


def _main() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()


def test_the_position_is_swept_when_it_is_served_not_when_a_move_arrives():
    """Analysing after the fact costs an engine call per attempt and leaves the
    player watching a spinner. One sweep of the position they are already
    looking at answers every attempt, retries included."""
    source = _main()
    body = source[source.index("async def next_correction"):source.index("async def correction_move")]
    assert "_warm_ranking" in body
    assert "create_task" in body, "the sweep must not block serving the position"


def test_an_unranked_move_is_refused_without_a_second_opinion():
    """Everything outside the sweep is worse than its weakest entry, so if that
    already fails there is nothing left to ask."""
    source = _main()
    body = source[source.index("async def correction_move"):source.index("async def correction_hint")]
    assert 'ranking["best_win"] - ranking["floor"] >= FORGIVEN' in body
    assert "unranked" in body


def test_background_jobs_stand_aside_for_any_engine_use_not_just_playing():
    """Judging a correction and reading a puzzle continuation queue behind the
    backfill exactly as a move does. This is how 'checking that move' came to
    take two seconds."""
    source = _main()
    assert "_engine_in_demand()" in source
    backfill = source[source.index("async def _run_backfill"):]
    backfill = backfill[:backfill.index("\n@app")]
    assert "_game_in_progress() or _engine_in_demand()" in backfill
