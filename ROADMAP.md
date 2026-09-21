# Where this is

A self-hosted trainer calibrated for a ~550 player. Maia-3 is the opponent,
sampled rather than argmax so it does not play above its label; Stockfish only
judges, and the win% curve uses a beginner constant instead of lichess's 2300+
fit. Reviews combine engine verdicts, motif detectors ported from
lichess-puzzler, and an LLM allowed no chess of its own — every move it names is
parsed against the real board. Guards stop play the moment you hang a piece.
Reps drill complete opening variations, taking wrong moves back with an
explanation. Real chess.com games import through the same pipeline and are
watched for every minute.

## Next

**PWA.** Mostly done already: the viewport, `theme-color`, dark mode and the
880/620px breakpoints are in place. What is missing is a `manifest.webmanifest`,
PNG icons at 192/512 (the `apple-touch-icon` currently points at an SVG, which
iOS ignores), and a service worker if Android installability matters — iOS needs
none. Half a day at most, and the service worker is the part to be careful with:
it is a far more aggressive cache than the one that already shipped a working
feature to a browser still running the previous version.

**Puzzles chosen by weakness.** Scoped and measured 2026-09-21 against a
59k-row sample of the CC0 dump; the findings are below and they changed the
design twice. Originally skipped, and worth revisiting now for
a reason that did not exist then: the app knows what you actually get wrong.
Six motifs rank by how many games they appear in — currently hanging pieces in
7 of 9, pins in 6, king left in the centre in 5. Lichess publishes its whole
puzzle database CC0 with a `Themes` column and a rating per puzzle, so the
mapping is direct: our `hung_piece` to their `hangingPiece`, `allowed_fork` to
`fork`, `back_rank_weakness` to `backRankMate`. Serve puzzles for the two motifs
costing the most games, at a rating band just above the player's, and re-rank as
the weakness list moves. That makes it a feedback loop rather than a puzzle
trainer bolted on the side.


---

## Puzzle selection: what the measurements say

### The mapping is directional

This is the thing the first plan missed. Our motifs are mostly records of
**defensive failure** — `allowed_fork` means the opponent forked *you* — while
every Lichess puzzle puts the solver on the **winning** side. A naive
`allowed_fork -> fork` map therefore trains you to execute forks, not to avoid
them. That transfer is real (you cannot avoid a pattern you cannot see) but it
is not the same skill, and the UI must not claim otherwise.

| our motif | dir | lichess theme | puzzles ≤1200, ≤3 moves |
|---|---|---|---|
| `missed_free_piece` | **direct** | `hangingPiece` | ~82,000 |
| `missed_mate` | **direct** | `mateIn1`, `mateIn2` | ~717k / ~511k |
| `hung_piece` | inverted | `hangingPiece` | ~82,000 |
| `allowed_fork` | inverted | `fork` | ~278,000 |
| `allowed_skewer` | inverted | `skewer` | ~43,000 |
| `allowed_discovered_attack` | inverted | `discoveredAttack` | ~81,000 |
| `allowed_pin` | inverted | `pin` | ~54,000 (p25 is 1179 — top of band) |
| `allowed_mate` | inverted | `mateIn1`, `mateIn2` | plentiful |
| `back_rank_weakness` | inverted | `backRankMate` | ~141,000 |
| `king_left_in_the_centre` | loose | `attackingF2F7` | ~28,000 |
| `trapped_piece` | — | *excluded* | ~4,000, median **1587** |

Only two motifs map directly, and both are *misses* rather than blunders —
which is a quiet argument for weighting Awareness higher than it currently is.

### Two things are excluded, deliberately

`trapped_piece` has ~4,000 beginner puzzles at a median rating of 1587, and
lichess's own `defensiveMove` theme — the one that would train prophylaxis
properly — sits at a median of **1898**. Both are far out of reach at 550.
Serving them would be worse than saying "no puzzles for this one yet".

### The rating band cannot be tight

At rating ≤900 with a one-move solution, **99.6% of the entire dump is
mate-in-1**: fork, skewer, trappedPiece and defensiveMove have literally zero
puzzles there. Lichess puzzle ratings run well above player ratings, so the
band has to reach ~1300 and allow 2–3 move solutions before the motifs we
actually measure have any volume at all.

### Local dump, not the API

The API decision was reversed within the hour, on evidence:
`/api/puzzle/next` returned **429 after nine requests in seven seconds**,
`angle=` accepts one theme so it cannot be combined with a rating band, and its
only difficulty control is five buckets relative to an anonymous 1500 player.
The dump costs one 304 MB download and yields a ~2 MB local slice (~200 puzzles
per theme per 100-point bucket) with exact control and no rate limit.
