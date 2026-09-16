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

**Puzzles chosen by weakness.** Originally skipped, and worth revisiting now for
a reason that did not exist then: the app knows what you actually get wrong.
Six motifs rank by how many games they appear in — currently hanging pieces in
7 of 9, pins in 6, king left in the centre in 5. Lichess publishes its whole
puzzle database CC0 with a `Themes` column and a rating per puzzle, so the
mapping is direct: our `hung_piece` to their `hangingPiece`, `allowed_fork` to
`fork`, `back_rank_weakness` to `backRankMate`. Serve puzzles for the two motifs
costing the most games, at a rating band just above the player's, and re-rank as
the weakness list moves. That makes it a feedback loop rather than a puzzle
trainer bolted on the side.
