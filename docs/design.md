# Why it is built this way

Every quirk here is a response to something measured. The short version: every
off-the-shelf chess tool is tuned for strong players, in two independent
places, and both had to be moved.


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

## What that buys you

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

## The three opening modes

| Mode | Behaviour |
|---|---|
| **Drill** | Follows your chosen line for N plies, then plays on naturally. Leaving the line is flagged, not punished. |
| **Strict book** | Stays in theory as long as the book has a move. For memorising a repertoire. |
| **Realistic** | No book. Maia at your rating already plays what real players at that rating play, deviations included. |

## The opponent's strength dial

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

## The old proportional servo, retained

Because Maia's label cannot be trusted, the app never relies on it. It nudges
the conditioning Elo until you actually score ~45% and lets the number land
wherever it lands. Starting the dial 300 points wrong, it converges to within
~40 of the truth. In practice: **win 7 of your last 10 and it gets harder, lose
8 of 10 and it gets easier**, anything between is treated as noise.

The gain is a control-loop constant, not a taste setting — see the measured
table in `rating.py`. The first implementation used a gain of 150, which gives
a loop gain of 2.16 over a 10-game window and oscillates by ±139 Elo forever.

## The coach never judges

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
- **PWA** and **puzzles chosen by your measured weaknesses** — see
  [ROADMAP.md](ROADMAP.md).
