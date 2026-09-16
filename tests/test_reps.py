"""The reps curriculum: ordering, unlocking, and deviation detection."""

import chess
import pytest

from app.openings import OpeningBook
from app.reps import (
    REVIEW_EVERY,
    family_side,
    MASTERY_STREAK,
    MAX_REP_PLIES,
    MIN_REP_PLIES,
    STARTING_UNLOCKED,
    Progress,
    build_curriculum,
    check_rep,
    families_for,
)


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return OpeningBook.load()


@pytest.fixture(scope="module")
def italian(book):
    return build_curriculum(book, "Italian Game", chess.WHITE)


def moves_of(sans: str) -> list[chess.Move]:
    board = chess.Board()
    out = []
    for san in sans.split():
        move = board.parse_san(san)
        out.append(move)
        board.push(move)
    return out


# --------------------------------------------------------------------------
# Curriculum ordering
# --------------------------------------------------------------------------


def test_no_rep_is_merely_the_start_of_another(italian):
    """The rule: a rep is a complete variation, never a waypoint into one.

    "Indian Defense" is 1.d4 Nf6 2.c4 g6 — four plies that dozens of real
    variations only pass through. Serving that as its own exercise teaches
    nothing the King's Indian would not.
    """
    seqs = [tuple(m.uci() for m in r.moves) for r in italian.reps]
    prefixes = [(a, b) for a in seqs for b in seqs if a != b and b[: len(a)] == a]
    assert prefixes == []


def test_a_waypoint_is_carried_forward_rather_than_dropped(book):
    """Dropping prefixes outright would delete every mainline.

    Mainlines are precisely the lines others extend, so each is instead carried
    along its most popular continuation into a full variation.
    """
    indian = build_curriculum(book, "Indian Defense", chess.BLACK)
    stub = ["d4", "Nf6", "c4", "g6"]
    assert not any(r.san_line() == stub for r in indian.reps)
    carried = [r for r in indian.reps if r.san_line()[: len(stub)] == stub]
    assert carried, "the line should survive as part of something longer"
    assert all(r.plies > len(stub) for r in carried)


def test_every_rep_is_long_enough_to_be_worth_drilling(italian):
    assert all(r.plies >= MIN_REP_PLIES for r in italian.reps)
    assert all(r.plies <= MAX_REP_PLIES for r in italian.reps)


def test_mainlines_come_before_sidelines(italian):
    """Once every line runs to its natural end, length says nothing about
    difficulty — it only reflects how deeply a variation happens to be
    catalogued. How mainline a line is has to lead instead."""
    top = italian.reps[:10]
    bottom = italian.reps[-10:]
    assert max(r.rarest_edge for r in bottom) <= min(r.rarest_edge for r in top)


def test_difficulty_is_dense_and_ordered(italian):
    assert [r.difficulty for r in italian.reps] == list(range(len(italian.reps)))


def test_lines_are_trimmed_to_the_opening(italian):
    assert all(r.plies <= MAX_REP_PLIES for r in italian.reps)
    assert all(r.plies >= MIN_REP_PLIES for r in italian.reps)


def test_truncation_does_not_leave_duplicate_lines(italian):
    lines = [tuple(r.san_line()) for r in italian.reps]
    assert len(lines) == len(set(lines))


def test_every_family_offered_can_be_built(book):
    for entry in families_for(book, limit=8):
        side = chess.WHITE if entry["side"] == "white" else chess.BLACK
        curriculum = build_curriculum(book, entry["family"], side)
        assert curriculum.reps, f"{entry['family']} produced no lines"


# --------------------------------------------------------------------------
# Whose opening is it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family,side",
    [
        ("Italian Game", chess.WHITE), ("Ruy Lopez", chess.WHITE),
        ("English Opening", chess.WHITE), ("Vienna Game", chess.WHITE),
        ("Sicilian Defense", chess.BLACK), ("French Defense", chess.BLACK),
        ("Caro-Kann Defense", chess.BLACK), ("Nimzo-Indian Defense", chess.BLACK),
        ("Slav Defense", chess.BLACK), ("Benko Gambit", chess.BLACK),
    ],
)
def test_openings_are_attributed_to_the_right_side(book, family, side):
    assert family_side(book, family) == side


def test_a_defense_whose_shortest_line_ends_on_a_white_move(book):
    """Why the name rule exists at all.

    The catalogue lists "King's Indian Defense" at 1.d4 Nf6 2.c4 g6 3.Nc3, so
    the move rule alone calls it White's. It is Black's opening.
    """
    for family in ("King's Indian Defense", "Old Indian Defense", "Rat Defense"):
        assert family_side(book, family) == chess.BLACK, family


@pytest.mark.parametrize(
    "family,side",
    [
        # the gambiteer owns the opening, not whoever replies to it
        ("King's Gambit Accepted", chess.WHITE),
        ("King's Gambit Declined", chess.WHITE),
        ("Queen's Gambit Accepted", chess.WHITE),
        ("Queen's Gambit Declined", chess.WHITE),
        ("Benko Gambit Accepted", chess.BLACK),
        # a White setup catalogued up to Black's reply
        ("Four Knights Game", chess.WHITE),
        ("Colle System", chess.WHITE),
    ],
)
def test_the_suffix_rules_beat_the_last_move(book, family, side):
    assert any(o.family == family for o in book.openings), family
    assert family_side(book, family) == side


def test_a_sub_variation_named_defense_does_not_flip_a_white_opening(book):
    """The name rule is skipped where a comma shows it labels a variation."""
    family = "Vienna Gambit, with Max Lange Defense"
    assert any(o.family == family for o in book.openings)
    assert family_side(book, family) == chess.WHITE


def test_families_are_offered_with_their_side(book):
    offered = {f["family"]: f["side"] for f in families_for(book, limit=40)}
    assert offered.get("Sicilian Defense") == "black"
    assert offered.get("Italian Game") == "white"


# --------------------------------------------------------------------------
# Unlocking
# --------------------------------------------------------------------------


def test_only_a_few_lines_are_available_at_the_start(italian):
    assert len(italian.unlocked({})) == STARTING_UNLOCKED
    assert italian.next_rep({}).difficulty == 0


def test_mastering_a_line_unlocks_exactly_one_more(italian):
    progress = {italian.reps[0].key: Progress(streak=MASTERY_STREAK)}
    assert len(italian.unlocked(progress)) == STARTING_UNLOCKED + 1


def test_a_single_success_is_not_mastery(italian):
    progress = {italian.reps[0].key: Progress(streak=1)}
    assert not progress[italian.reps[0].key].mastered
    assert len(italian.unlocked(progress)) == STARTING_UNLOCKED
    assert italian.next_rep(progress).name == italian.reps[0].name


def test_the_easiest_unmastered_line_is_served_next(italian):
    progress = {italian.reps[0].key: Progress(streak=MASTERY_STREAK)}
    assert italian.next_rep(progress).name == italian.reps[1].name


def test_known_lines_come_back_round_for_a_recall_check(italian):
    """Mastering a line unlocks another, so there is always a new line queued.

    Left alone the curriculum would never look back and a "known" line would
    never be seen again — two correct repetitions is enough to move on, not
    enough to remember. Every REVIEW_EVERY reps is a recall check instead.
    """
    progress = {
        r.key: Progress(streak=MASTERY_STREAK + 2, attempts=4) for r in italian.reps[:4]
    }
    weakest = italian.reps[2]
    progress[weakest.key] = Progress(streak=MASTERY_STREAK, attempts=4)

    total = sum(p.attempts for p in progress.values())
    assert total % REVIEW_EVERY == 0, "fixture should land on a review rep"
    assert italian.next_rep(progress).key == weakest.key


def test_between_recall_checks_the_curriculum_advances(italian):
    progress = {
        r.key: Progress(streak=MASTERY_STREAK, attempts=3) for r in italian.reps[:3]
    }
    total = sum(p.attempts for p in progress.values())
    assert total % REVIEW_EVERY != 0
    assert not progress.get(italian.next_rep(progress).key, Progress()).mastered


def test_a_beginner_with_no_history_is_never_shown_a_review(italian):
    assert italian.next_rep({}).difficulty == 0


def test_progress_survives_lines_that_are_no_longer_unlocked(italian):
    """Stored progress is keyed by name, so unknown names must not break it."""
    progress = {"Some Opening That Was Renamed": Progress(streak=9)}
    assert italian.next_rep(progress).difficulty == 0


# --------------------------------------------------------------------------
# Which side plays what
# --------------------------------------------------------------------------


def test_white_plays_the_even_plies(italian):
    assert all(i % 2 == 0 for i in italian.reps[0].your_plies())


def test_black_plays_the_odd_plies(book):
    black = build_curriculum(book, "French Defense", chess.BLACK)
    rep = black.reps[0]
    assert rep.your_plies()[0] == 1
    assert all(i % 2 == 1 for i in rep.your_plies())


# --------------------------------------------------------------------------
# Deviation
# --------------------------------------------------------------------------


def test_playing_the_line_is_not_a_deviation(italian):
    rep = next(r for r in italian.reps if r.name == "Italian Game: Giuoco Piano")
    assert check_rep(rep, moves_of("e4 e5 Nf3 Nc6 Bc4 Bc5"))["deviated_at"] is None


def test_deviation_reports_the_ply_and_the_expected_move(italian):
    rep = next(r for r in italian.reps if r.name == "Italian Game: Giuoco Piano")
    result = check_rep(rep, moves_of("e4 e5 Nf3 Nc6 d4"))
    assert result["deviated_at"] == 4
    assert result["expected"] == "Bc4"
    assert result["played"] == "d4"
    assert result["yours"] is True


def test_deviation_on_the_very_first_move(italian):
    result = check_rep(italian.reps[0], moves_of("d4"))
    assert result["deviated_at"] == 0
    assert result["expected"] == "e4"


def test_a_partial_line_is_not_yet_a_deviation(italian):
    rep = next(r for r in italian.reps if r.name == "Italian Game: Giuoco Piano")
    assert check_rep(rep, moves_of("e4 e5 Nf3"))["deviated_at"] is None


def test_moves_past_the_end_of_the_line_are_ignored(italian):
    """A rep ends at the line; anything after it is not the exercise."""
    rep = italian.reps[0]
    played = list(rep.moves)
    board = chess.Board()
    for move in played:
        board.push(move)
    played.append(next(iter(board.legal_moves)))
    assert check_rep(rep, played)["deviated_at"] is None


# --------------------------------------------------------------------------
# A rep ends on your move
# --------------------------------------------------------------------------


def test_two_lines_that_differ_only_on_the_opponents_last_move_merge(book):
    """The Ruy Lopez bug, reported from the curriculum itself.

    Closed and Open are 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O and then
    Black's fifth move. As White you never answer that move inside the rep, so
    the two were the same five moves listed twice — the first two lines of the
    curriculum.
    """
    white = build_curriculum(book, "Ruy Lopez", chess.WHITE)
    lines = [" ".join(r.san_line()) for r in white.reps]
    assert lines.count("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O") == 1
    assert len(set(lines)) == len(lines)

    # From Black's side those same two lines genuinely differ: Black plays the
    # move that distinguishes them, so both are kept.
    black = [" ".join(r.san_line()) for r in build_curriculum(book, "Ruy Lopez", chess.BLACK).reps]
    assert "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7" in black
    assert "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Nxe4" in black


def test_no_rep_ends_on_the_opponents_move(book):
    for family in ("Ruy Lopez", "Italian Game", "Sicilian Defense", "French Defense"):
        for color in (chess.WHITE, chess.BLACK):
            for rep in build_curriculum(book, family, color).reps:
                assert rep.your_plies()[-1] == rep.plies - 1, f"{family}: {rep.name}"


def test_your_moves_are_never_fewer_than_half_the_line(italian):
    for rep in italian.reps:
        assert len(rep.your_plies()) == (rep.plies + 1) // 2


# --------------------------------------------------------------------------
# Progress is keyed by the moves
# --------------------------------------------------------------------------


def test_a_reps_identity_is_its_moves_not_its_name(italian):
    """Names are derived from classifying the position a line reaches, so they
    move when the curriculum is rebuilt. Progress keyed on a name that moved is
    indistinguishable, to the player, from progress that was wiped."""
    rep = italian.reps[0]
    assert rep.key == " ".join(m.uci() for m in rep.moves)
    assert italian.find(rep.key) is rep
    assert italian.find("nonsense") is None


def test_the_summary_lists_every_line_not_just_the_unlocked_ones(italian):
    summary = italian.summary({})
    assert len(summary["lines"]) == len(italian.reps)
    assert summary["unlocked"] == STARTING_UNLOCKED
    assert sum(1 for line in summary["lines"] if line["unlocked"]) == STARTING_UNLOCKED
    assert summary["next"] == italian.reps[0].key


def test_the_summary_marks_what_is_known(italian):
    progress = {italian.reps[1].key: Progress(streak=MASTERY_STREAK)}
    lines = {line["key"]: line for line in italian.summary(progress)["lines"]}
    assert lines[italian.reps[1].key]["mastered"] is True
    assert lines[italian.reps[0].key]["mastered"] is False
