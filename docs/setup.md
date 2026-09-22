# Setup

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
