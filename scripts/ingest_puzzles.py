#!/usr/bin/env python3
"""Download the Lichess CC0 puzzle database and keep a slice of it locally.

Run once (and again whenever you want fresher puzzles):

    python scripts/ingest_puzzles.py --db data/chess-trainer/chess.db

The dump is ~304 MB of zstd-compressed CSV, about 6 million puzzles. We keep a
capped random sample of the ten themes our motifs map to — a few thousand each,
which is years of solving for one player and a couple of MB on disk.

**No rating filter.** Any puzzle carrying the theme is fair game. That was a
deliberate call: filtering to a beginner band collapses the set to almost
nothing but mate-in-1 (at rating <=900 with a one-move solution, 99.6% of the
entire database is mateIn1, and fork and skewer have literally zero puzzles).
The rating is stored anyway, because it is one column and it means the decision
can be revisited without re-downloading 304 MB.

Reservoir sampling is used rather than "take the first N", because the dump is
ordered by puzzle id and the first N of anything would be a biased slice.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.puzzle_themes import INGEST_THEMES  # noqa: E402

DUMP_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
PER_THEME = 3000


def stream_rows(source: str):
    """Yield CSV rows, decompressing as we go rather than landing 900 MB.

    stderr is swallowed because a deliberately truncated dump — handy for
    testing against a range request rather than the whole file — makes zstd
    complain about an incomplete frame after emitting everything it could read.
    """
    proc = subprocess.Popen(["zstd", "-dcq", source], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    reader = csv.DictReader(io.TextIOWrapper(proc.stdout, encoding="utf-8",
                                             errors="replace"))
    try:
        yield from reader
    finally:
        proc.stdout.close()
        proc.wait()


def download(dest: Path, refresh: bool = False) -> Path:
    """Fetch the dump unless we already have one.

    Any existing file is reused, whatever its size: a partial file is a
    deliberate test fixture as often as it is an interrupted download, and
    silently replacing one with a 304 MB fetch is a nasty surprise. Pass
    --refresh to re-download on purpose.
    """
    if dest.exists() and not refresh:
        print(f"using existing dump: {dest} ({dest.stat().st_size/1e6:.0f} MB)"
              + ("  [partial — pass --refresh for the whole thing]"
                 if dest.stat().st_size < 300_000_000 else ""))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {DUMP_URL} -> {dest}")
    req = urllib.request.Request(DUMP_URL, headers={"User-Agent": "chess-trainer/0.1"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        total = int(r.headers.get("content-length", 0))
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done/1e6:6.0f} / {total/1e6:.0f} MB", end="", flush=True)
    print()
    return dest


def sample(path: Path, per_theme: int, seed: int = 0) -> dict[str, dict]:
    """One reservoir per theme; a puzzle with two of our themes lands in both."""
    rng = random.Random(seed)
    reservoirs: dict[str, list] = {t: [] for t in INGEST_THEMES}
    seen: dict[str, int] = {t: 0 for t in INGEST_THEMES}
    scanned = 0

    for row in stream_rows(str(path)):
        scanned += 1
        if scanned % 500_000 == 0:
            print(f"  scanned {scanned/1e6:.1f}M rows", flush=True)
        themes = row.get("Themes") or ""
        hits = [t for t in themes.split() if t in reservoirs]
        if not hits or not row.get("Moves"):
            continue
        for theme in hits:
            seen[theme] += 1
            pool = reservoirs[theme]
            if len(pool) < per_theme:
                pool.append(row)
            else:
                j = rng.randrange(seen[theme])
                if j < per_theme:
                    pool[j] = row

    print(f"scanned {scanned:,} puzzles")
    kept = {}
    for theme, pool in reservoirs.items():
        print(f"  {theme:18} {seen[theme]:>8,} available -> kept {len(pool):,}")
        kept[theme] = pool
    return kept


def store(db_path: Path, reservoirs: dict[str, list]) -> int:
    conn = sqlite3.connect(db_path)
    rows = {}
    for pool in reservoirs.values():
        for r in pool:
            rows[r["PuzzleId"]] = r          # a puzzle in two themes is one row
    conn.executemany(
        "INSERT OR REPLACE INTO puzzles (id, fen, moves, rating, themes) "
        "VALUES (?, ?, ?, ?, ?)",
        [(r["PuzzleId"], r["FEN"], r["Moves"],
          int(r["Rating"]) if r.get("Rating", "").isdigit() else None,
          r["Themes"]) for r in rows.values()],
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM puzzles").fetchone()[0]
    conn.close()
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("CHESS_DB", "data/chess.db"))
    ap.add_argument("--dump", default="/tmp/lichess_db_puzzle.csv.zst",
                    help="where to keep the downloaded dump")
    ap.add_argument("--per-theme", type=int, default=PER_THEME)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-download even if a dump is already there")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path} — start the app once first")

    dump = download(Path(args.dump), args.refresh)
    reservoirs = sample(dump, args.per_theme, args.seed)
    total = store(db_path, reservoirs)
    print(f"\nstored {total:,} puzzles in {db_path}")


if __name__ == "__main__":
    main()
