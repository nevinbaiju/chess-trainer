# Chess Trainer

A self-hosted chess trainer for a beginner — built around a ~550 chess.com
rapid player, and calibrated for that level throughout. An opponent that plays
like a human at *your* level in openings you choose, and a reviewer that
explains why your moves were bad in plain English.

Runs as one container on `127.0.0.1:5020`. Put it behind whatever you already
use to reach your home server.

---

## The idea

Every off-the-shelf chess tool is tuned for strong players, in two places that
matter, and fixing both is most of what this app *is*.

**1. The opponent.** Stockfish cannot be weak in a human way. Its own binary
reports `option name UCI_Elo type spin default 1320 min 1320 max 3190` — the
floor is 1320, more than double a 550 chess.com rating, and below that it plays
perfectly for ten moves and then throws a piece at random. So the opponent here
is **Maia-3**, a transformer trained to predict what a human of a given rating
actually plays.

But Maia has its own trap: the Lichess bot `maia1` carries a "1100" net and
performs around **1440–1700**. Argmax over a human policy returns the *modal*
move of a rating band, and the mode is stronger than the average, because
blunders are diverse and individually rare while the good move is concentrated.
The fix is to **sample** (`OPPONENT_TEMPERATURE=1.0`) rather than take the
argmax — note that upstream's `maia3-5m` launcher hard-forces `--temperature 0`,
so this app calls `maia3-uci` directly.

**2. The judgement.** Lichess's win% curve uses `k = -0.00368208`, fitted in
[lila#11148](https://github.com/lichess-org/lila/pull/11148) on 75k positions
from games rated **2300+**. The author wrote plainly that the right value
"will be closer and closer to `-0.002` as the ELO decreases". Uncalibrated, a
reviewer screams "blunder" at drops a 600 player still wins half the time. The
default here is `-0.002`; set `CHESS_WIN_PCT_K=-0.00368208` to reproduce
lichess exactly (the tests pin both).

### What that buys you

Three engines, three jobs, and **the coaching comes from the gap between them**:

| | |
|---|---|
| **Maia-3** | the opponent, and "what would someone at my level play here?" |
| **Stockfish 19** | objective truth. Never the opponent. |
| **Opening book** | 3,810 named lines (CC0), for the openings you pick |

So instead of "Mistake", the review can say:

> **Move 9. cxd5 — 64% of players at your rating play this → COMMON TRAP**

A move that is both wrong *and* the most popular choice at your level is the
highest-leverage thing you can possibly learn, because fixing it transfers to
every future game. Conversely, when only 2% of your peers would find
Stockfish's move, the coach is told **not** to say "you should have played it"
and to teach the pattern instead.

### While you play

- **Mate hints** (on by default): when a forced mate is available *to you*, the
  app says so — how long it is, never which move starts it. Being prompted to
  look is the whole lesson. Capped at mate in 3 by default, because "there is a
  mate in 7 here" is discouraging rather than instructive at this level. It
  never warns you about mates against *you*.
- **Step back through the game** while it is still running: Prev/Next, arrow
  keys, or click any move in the list. The board becomes a viewer — move input
  is off until you return to the live position, and playing a move returns you
  there automatically.
- **Opponent strength** can be shifted up or down from wherever the ladder has
  settled it. The servo optimises for a close game; sometimes you would rather
  have an easy one, and waiting a dozen games for the dial to drift is not an
  answer. The servo's gain now also scales with how many games it has actually
  seen, so it moves properly over your first dozen games instead of barely at
  all — the stability limit depends on the window length, so a 5-game window
  tolerates twice the gain of a 10-game one.
- **Free-material hints** (on by default): when the opponent leaves a piece
  hanging, the board says so — not *which* piece, because noticing is the skill.
  This targets the biggest measured weakness in real play: across 16 reviewed
  games the opponent blundered 44 times and only 20 were punished. Pure static
  exchange evaluation, no engine call, and pawns are excluded so it does not
  fire every move and get ignored.
- **Resume**: refreshing mid-game no longer strands it; the most recent
  unfinished game loads automatically, and the games list offers "resume"
  rather than "review" for an unfinished one.
- **Legal moves** appear as dots (empty squares) and bevels (captures) as soon
  as you pick a piece up, and a piece with nowhere to go cannot be lifted.
- **Sounds** for moves, captures, castling, checks, promotions and the result.
  They are **synthesised in the browser** with the Web Audio API rather than
  shipped as files: chess sounds are short percussive hits that generate
  convincingly in a few lines, and lichess's and chess.com's sound packs are not
  ours to copy. Nothing is downloaded and nothing is added to the image.
- **Result banner** over the board — win, loss, stalemate or any other draw,
  each naming the specific reason — with the mated or stalemated king marked.
  Click it away to study the final position.

Preferences live in a JSON blob on the player row (`settings`), read through
`GET/POST /api/settings` with defaults in `DEFAULT_SETTINGS`. That shape is
deliberate: a settings page can render every key generically, and new keys need
no migration. Right now only the mate-hint options have UI, on the New game
card.

### Reps — drilling opening theory

A separate tab from Play. You pick a family (Italian Game, French Defense, …)
and the bot plays **book moves for the whole exercise** while your job is to
find your side of the line.

**Every rep is a complete variation.** The catalogue names positions, not
variations, so it lists "Indian Defense" at 1.d4 Nf6 2.c4 g6 — four plies that
dozens of real openings merely pass through. Drilling that on its own teaches
nothing the King's Indian would not. So each line is first carried forward along
its most popular continuation, up to `MAX_REP_PLIES` (10) or until the
continuations run out, and is then renamed by classifying where it ended up; any
line still a prefix of another after that is dropped as a duplicate. Deleting
prefixes without extending them first would have been worse than doing nothing —
mainlines are *precisely* the lines that others extend, so it deletes every one
of them and leaves you drilling sidelines.

Extension deliberately crosses family boundaries. Confined to its own family,
1.d4 Nf6 2.c4 g6 has nowhere to go — every continuation is a King's Indian or a
Grünfeld — and the stub survives.

Ordering is by `-log2(popularity)`, with length only breaking ties. Once every
line runs to its natural end, depth says nothing about difficulty; it only
reflects how deeply that corner of the catalogue happens to be written up.

**A rep ends on your move.** A rep tests your moves; the opponent's are given
to you, so a line ending on the opponent's move ends with a ply that tests
nothing — and two lines differing *only* there are the same exercise under two
names. As White the Ruy Lopez Closed and Open both run 1.e4 e5 2.Nf3 Nc6 3.Bb5
a6 4.Ba4 Nf6 5.O-O and then diverge on Black's fifth, which White never answers
inside the rep; they were lines 1 and 2 of the curriculum. Trailing opponent
moves are now trimmed *before* deduplication, so those merge. From Black's side
the same two lines stay separate, because Black is the one playing the move that
tells them apart. The Ruy Lopez went from 80 White reps to 43.

**Progress is stored against the moves, not the name.** A line's name is
derived — it comes from classifying the position the line reaches — so it moves
whenever the curriculum is rebuilt, and progress keyed on a name that moved is
indistinguishable, from the player's seat, from progress that was wiped. The
move sequence is the exercise and never changes meaning. An install upgrading
from the name-keyed table keeps its rows: they are set aside at startup and
matched back by name, or by sharing a prefix with exactly one line in either
direction, with merged rows keeping the best streak and the summed attempts.

**You can pick a line.** The progress panel lists every line in the family, not
just the unlocked ones, filtered by All / To learn / Known, and clicking one
drills it. The curriculum still decides what "Start" serves and marks it in the
list, but it is not the only way to reach a line: you cannot revise what you
cannot find, and going back over a line you have already banked is the whole
point of knowing which ones those are.

**Each family knows whose it is**, and the colour picker defaults to that side
with a line saying why. Four rules decide it, since the catalogue itself does
not record it: a name ending in "Defense" is Black's (needed because "King's
Indian Defense" is listed at 1.d4 Nf6 2.c4 g6 3.Nc3, ending on a White move);
one ending in "Game", "Opening", "Attack" or "System" is White's; "… Accepted"
and "… Declined" defer to the parent gambit, so the King's Gambit Accepted is
White's; and otherwise it is whoever played the last move of the family's
shortest line, the move that creates the named position. Names containing a
comma skip the first three, where the trailing word labels a sub-variation
rather than the family — "Vienna Gambit, with Max Lange Defense" is White's.
You can still pick the other side; drilling a defense from White is how you
learn to meet it.

**A wrong move does not end the rep.** It used to, and that was the worst thing
in the app: you were told the book move at the exact moment you could no longer
play it, and a line you got wrong on move four was never played through at all.
Worse, when the engine agreed the two moves were close it said so — "not really
a mistake, just not this repertoire" — which is true, unarguable and teaches
nothing.

Now the move is simply not made. The position stands, the reason is spelled out,
the move you owed is drawn on the board, and you play on. A line banks only when
you play it start to finish **with no corrections**, so being shown a move is
never mistaken for knowing it.

The explanation is assembled from facts before any prose is written, and which
of five things it says depends on what is actually true:

| | when | what it says |
|---|---|---|
| **Drops material** | the motif detectors find hanging material | names the piece and the reply that takes it |
| **Worse** | Stockfish gap ≥ 6 win% points | how much it hands back, and the reply it expects |
| **Transposes** | reordering your move into the line reaches the identical position | says so first — this is not an error, and calling it one would be false |
| **Sideline** | catalogued, but under 8 named lines run through it | names it and how rare it is |
| **A different opening** | catalogued and mainstream | names the opening it becomes |

The transposition check is the important one. The single commonest way a drill
"breaks" is castling on move four instead of move five, and the line reaches the
same position either way. It is verified on the board rather than assumed: your
move is swapped into the line's move order (only among *your* slots — pulling it
past one of the opponent's would put two moves of the same colour in a row), the
reordering is played out, and the final position is compared. If it matches, the
trainer says so and asks for the line's order only to drill the order.

The "sideline" tier exists because being in the catalogue is not an endorsement.
1.Nh3 is the Amar Opening, a real name with three lines behind it against 2023
for 1.e4; an earlier version told the player it was "also a reasonable
repertoire choice", which is worse than saying nothing.

Those facts go to the coach model, which writes the prose and is allowed no
chess of its own — same grounding rules as the review, on a 12-second clock
because a drill is an interactive loop rather than a background job. If it is
slow, down, or writes a move the facts never mentioned, the facts render
directly instead.

Three lines are unlocked at the start. Playing one cleanly **twice running**
banks it and unlocks one more; a run that needed a correction resets that streak
to zero.
Every fourth rep is a **recall check** on a line you already know rather than a
new one — mastering a line always unlocks another, so without that the
curriculum would never look back and nothing would ever be re-tested.

Reps stop at the end of the line, so this is opening training and does not
wander into a middlegame. They are recorded separately from games and never
touch your rating.

### Guards — being caught in the act

The review tells you about a mistake after the game. A guard stops you the
moment you make one: the bot's reply is held back, the board freezes, and you
are offered the move back.

Which mistakes stop play is yours to choose, from a tag list that doubles as
"here is what you actually keep doing" — each tag shows how many of your
reviewed games it appears in. Defaults are the two the data actually flags:
hanging a piece (15 of 17 games) and walking past material the opponent left
hanging (half of it goes untaken).

Not every motif is guardable. "Back-rank weakness" and "king left in the
centre" describe where your king *is* rather than a move you just made, so a
takeback for them would interrupt moves that were fine.

**A game where you take a move back does not move your rating.** Retries make
the result a practice score rather than a measurement, and letting it count
would drag the opponent down to a level you only beat with help. Clean games
still drive the ladder, so the gradient survives.

The guard runs the same deterministic detectors the review does — it can never
flag something the post-game analysis would not — with one shallow depth-10
search for the refutation most of these motifs live in. Measured at 12ms once
the engine is warm.

### Progress — the cross-game view

At the top of the Review tab. A per-game review says what happened once; this is
the only thing in the app that can say *this is a pattern*, which is what a
learning gradient actually needs. It reports the recurring mistake (ranked by
how many **games** it appears in, not raw count — one won endgame can show
"missed mate" twenty moves running), whether blunders and accuracy are trending
the right way, and **Awareness**: how many of your opponent's blunders you
actually punished. At beginner level that last number tends to decide more games
than anything else.

Trends need at least six games. Below that it is noise, and telling someone they
are improving on the strength of one good game is worse than saying nothing.

### chess.com — your real games

At the top of the Review tab: connect an account, import, and your games are
reviewed by **the same pipeline** as the trainer ones — the same beginner win%
curve, the same motif detectors, the same accuracy formula. That is the whole
point. "You hang a piece in 15 of 17 trainer games" only means something next to
how often you do it for real, and the filter above the progress card switches
between **All games / Trainer / chess.com** so the two can be compared rather
than averaged.

Importing fetches everything; **reviewing** is the expensive part, so the newest
20 unreviewed games are analysed straight away (~30-60s each, serially — Stockfish
shares the box with whatever else you run) and the rest stay in History with
a review button. Rated standard rapid only by default: blitz and bullet mistakes
are mostly clock artifacts, and counting them would drown the weakness ranking in
errors better chess would not have prevented.

**It watches for new games.** With an account connected, a background task
checks once a minute and pulls in anything you have just finished, so a game
played on your phone is reviewed and in the stats by the time you open the tab.
The toggle and the interval are on the same card, and the line under it says
when it last looked — "watching" without a timestamp is indistinguishable from a
task that died.

Politeness is the whole design of that loop. It asks for **only the newest
month**, because a closed month cannot gain games, and always **conditionally**,
so an idle minute is one request that answers `304` with no body. chess.com's
own guidance is that serial access is unlimited; this is one serial request a
minute. The interval floors at 30s however the setting is edited.

The one thing it holds back is **reviewing**. Reviews and your moves share a
single Stockfish behind a single lock, so a review that started mid-game would
put a minute or two in front of your next mate hint. While a trainer game is
live the watcher still imports — that is only HTTP — and leaves the analysis for
the next check. "Live" means a move in the last ten minutes rather than a game
started recently, because a rapid game can run half an hour.

Four things about that API, each checked against it rather than the docs:

- **An empty `User-Agent` is a 403.** Verified live. chess.com ask for contact
  details in it, so ours carries an address.
- **The JSON month endpoint beats `/pgn`** — it carries `accuracies`,
  `time_class`, `rated`, per-side ratings *and* the full PGN string, so the PGN
  endpoint gives strictly less for the same request.
- **Closed months never change.** Each month's ETag is stored, so a repeat import
  is a handful of 304s and one real fetch of the current month.
- **Serial requests only.** chess.com's guidance is that sequential access is
  unlimited while parallel access may be throttled, so there is no `gather()`.

Games are named by **our** opening book rather than the ECO tag in the PGN, so an
imported game and a trainer game group together in the stats. Ratings on the day
are kept for both players: there is no rating-history endpoint, so the curve has
to be rebuilt from them. Re-importing is safe three times over — the ETag, a
`game_exists` check, and a UNIQUE constraint on `external_id`.

Their own move classifications are not obtainable at all: staff confirmed they
are regenerated on page load and never stored, so only the two `accuracies`
floats exist. We run our own analysis and treat theirs as a sanity check.

### Getting around

Three tabs: **Play**, **Reps**, **Review**. Review opens on the list of your
games — a review is a detail view *inside* that tab, reached by picking a game
and left by the "All games" button. It used to be its own destination that
opened on an empty "nothing loaded" panel with no way back, which was a dead
end in both directions.

### Reading a review

- **The swing graph** plots your winning chances after every move, always from
  your side of the board, so "down" means "I lost ground" whichever colour you
  had. Your own mistakes are marked on it; click anywhere to jump there. The
  y-axis is always a full 0-100 — auto-scaling to the data would exaggerate
  trivial swings and make two games incomparable.
- **Arrows** on any mistake: red is what you played, blue is what to play
  instead, amber is how the opponent punishes it. The blue arrow is suppressed
  when almost nobody at your rating finds that move — the point there is the
  pattern, not the move.

Severity on the graph is encoded by marker **size**, not by three colours: the
app's amber/orange/red severity colours measure ΔE 0.6 apart under
deuteranopia, which is fine as text (they always sit beside "?!" / "?" / "??")
but would be indistinguishable as bare dots. For the same reason the board
arrows override cm-chessboard's stock red/green — that pair separates by only
ΔE 7.0 under protanopia, versus 20.2 for red/blue, on the most important
distinction on the screen.

---

## Running it

```bash
cp .env.example .env      # LITELLM_MASTER_KEY, and the network your gateway is on
podman compose up -d --build
```

Starts on `127.0.0.1:5020`. It binds to loopback on purpose: put a reverse proxy
in front of it, or reach it over a private network such as Tailscale, rather
than exposing it. A systemd **user** unit with `WantedBy=default.target` and
`loginctl enable-linger` is enough to autostart it on boot.

The coach is the only part that needs LiteLLM, and it degrades to deterministic
templates without it, so the app is usable before you have a gateway configured.
Everything else — the opponent, the review, the reps — runs entirely locally.

```bash
# tests (no engines needed — the pure layers run on the host)
python3 -m venv .venv && .venv/bin/pip install chess pytest httpx
PYTHONPATH=. .venv/bin/python -m pytest -q
```

The image is ~1.4 GB, almost all of it the CPU-only torch wheel. Maia's 5M
checkpoint is baked in, so startup needs no network.

---

## Layout

```
app/
  eval.py       win%, move classification, accuracy, ACPL   <- the calibration
  phases.py     opening/middlegame/endgame (port of scalachess Divider)
  see.py        static exchange evaluation (python-chess has none)
  motifs/       what concretely went wrong: hung pieces, forks, pins, ...
  engines.py    Stockfish, as a supervised UCI subprocess
  maia.py       Maia-3, in-process (see "deviations" below)
  openings.py   the 3,810-line catalogue + the three play modes
  review.py     the pipeline that fuses all of the above
  coach.py      LLM narration, with the chess kept outside the model
  rating.py     Glicko-2 for you; a servo for the opponent's strength dial
  db.py         SQLite
  main.py       FastAPI
static/         vanilla ES modules + vendored cm-chessboard (no build step)
```

### The three opening modes

| Mode | Behaviour |
|---|---|
| **Drill** | Follows your chosen line for N plies, then plays on naturally. Leaving the line is flagged, not punished. |
| **Strict book** | Stays in theory as long as the book has a move. For memorising a repertoire. |
| **Realistic** | No book. Maia at your rating already plays what real players at that rating play, deviations included. |

### The opponent's strength dial

The dial is **derived from your Glicko rating**, not crawled toward it:
`opponent = rating + 400·log₁₀((1−target)/target)`, which is just the Elo
expectation inverted.

It used to be a proportional servo, and real play showed why that was wrong:
over 25 games of scoring 25%, it moved the opponent from 1000 to 946 — about a
dozen games behind where the evidence already pointed. Glicko-2 is already an
estimator of your strength and it integrates every result with proper
uncertainty weighting, so reading the dial off it lands on the answer
immediately.

The dial is not a true Elo — Maia's conditioning is only roughly calibrated —
but it does not need to be, because your rating is measured *in dial units*, so
the loop closes on whatever setting actually produces the target score. What it
does need is to be monotonic, and that was measured rather than assumed:

| dial | score vs a Maia-1200 reference | implied gap |
|---|---|---|
| 600 | 4% | −573 |
| 800 | 14% | −311 |
| 1000 | 29% | −159 |
| 1200 | 36% | −102 |

Between 600 and 1000 the dial moves real strength 414 points for a nominal 400
— essentially 1:1 in the range this app cares about. (14 games per pairing, so
the 1200-vs-1200 row sitting at 36% rather than 50% is within about one standard
error.) Reproduce with `scripts/calibrate.py`.

### The old proportional servo, retained

Because Maia's label cannot be trusted, the app never relies on it. It nudges
the conditioning Elo until you actually score ~45% and lets the number land
wherever it lands. Starting the dial 300 points wrong, it converges to within
~40 of the truth. In practice: **win 7 of your last 10 and it gets harder, lose
8 of 10 and it gets easier**, anything between is treated as noise.

The gain is a control-loop constant, not a taste setting — see the measured
table in `rating.py`. The first implementation used a gain of 150, which gives
a loop gain of 2.16 over a 10-game window and oscillates by ±139 Elo forever.

### The coach never judges

Measured on chess commentary ([arXiv 2608.04240](https://arxiv.org/html/2608.04240)),
an LLM asked to judge move quality scores **12.5 F1**; handed an engine's
verdict it scores **97.6**. So Stockfish decides what is good, python-chess
decides what is legal, `motifs/` decides what happened, Maia decides what is
human — and the model only writes prose about facts it is handed. Every
move-like token in its reply is then parsed against the real board, and the
reply is discarded if any of them is illegal. If the model is unavailable or
fails validation, a deterministic template renders instead; the review never
depends on it.

---

## Known gaps

The app can now *tell* you what is going wrong, but the only practice loop it
has is Reps, which drills openings. Real games here are decided around move 8 by
hanging pieces, and opening theory does not fix that. The tool that does is
tactics puzzles — skipped by choice, and worth reconsidering now that the data
says what it says.

## Deviations from the approved plan

Three, all forced by things found while building:

1. **Maia runs in-process, not as a UCI subprocess.** Its `uci.py` computes a
   `policy` probability for every candidate move and then `cmd_go` throws it
   away, printing only `score cp` derived from the WDL head. That discarded
   probability — *how many players at your rating would play this* — is the
   single most valuable signal in the app, and it is unavailable over the wire.
   Stockfish, which really does search for seconds at a time, keeps the full
   subprocess treatment.
2. **The coach reaches LiteLLM over the `brain_default` network**, not
   `host.containers.internal`. LiteLLM binds `127.0.0.1` on the host, and
   `host.containers.internal` resolves to the bridge gateway, so that route is
   refused. The brain itself talks to it by service name for the same reason.
3. **No Lichess explorer integration yet.** It began requiring an OAuth token
   in March 2026 and is capped at 25 req/min, so it cannot sit on a request
   path. The book is built from the offline CC0 catalogue instead; explorer
   frequencies would be a background pre-crawl, not a dependency.

## Not built yet

Deliberately, per the agreed build order — the first slice was play + review:

- **The rating curve** from imported per-game ratings — the data is stored
  (`games.player_rating`), nothing plots it yet.
- **Blunder rate by seconds spent.** The clock times are in the imported PGNs as
  `{[%clk ...]}` and python-chess reads them with `node.clock()`; nothing uses
  them yet. At beginner level "61% of your blunders came after under two
  seconds" is probably the most actionable line the app could print.
- **Explorer pre-crawl** for real per-rating move frequencies.
- **Puzzles** — explicitly skipped.

## Licences

**AGPL-3.0**, and not by preference — by inheritance. Maia-3 is AGPL-3.0 and is
loaded in-process rather than shelled out to; the motif primitives are ported
from [lichess-puzzler](https://github.com/ornicar/lichess-puzzler), also
AGPL-3.0; and `python-chess` is GPL-3.0+. Any one of those would make this
GPL-family, and the AGPL ones make it AGPL specifically. Since this is a web
application, §13 matters: anyone you let use a modified copy over a network is
entitled to its source.

An earlier version of this file said none of that binds a personal,
non-distributed instance. That was true while it lived on one machine and is
not true of a public repository.

`cm-chessboard` is MIT (vendored under `static/vendor/`), the opening catalogue
from [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings)
is CC0, and Stockfish is GPL-3.0, invoked as a separate binary over UCI.
