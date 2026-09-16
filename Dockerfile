# syntax=docker/dockerfile:1
#
# The host runs Python 3.14, which torch does not support. That — not
# preference — is why this container exists.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------ Stockfish
# Upstream now ships ONE "universal" binary that selects its instruction set at
# runtime, so there is no avx2/bmi2 variant to match against the host CPU.
# (Note the naming change: sf_18 was stockfish-ubuntu-x86-64-avx2.tar.)
ARG STOCKFISH_URL=https://github.com/official-stockfish/Stockfish/releases/download/sf_19/stockfish-linux-x86-64-universal.tar.gz
RUN curl -fsSL "$STOCKFISH_URL" -o /tmp/sf.tgz \
 && tar -xzf /tmp/sf.tgz -C /opt \
 && rm /tmp/sf.tgz \
 && ln -s /opt/stockfish/stockfish-linux-x86-64-universal /usr/local/bin/stockfish \
 && printf 'uci\nquit\n' | stockfish | grep -q '^uciok'

# ---------------------------------------------------------------------- torch
# CPU-only wheel. The default PyPI wheel drags in ~2GB of CUDA this box cannot
# use (integrated GPU only, no nvidia).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# --------------------------------------------------------------------- Maia-3
# Not published to PyPI, and the repo carries no version tags, so pin the
# commit rather than tracking a moving main.
ARG MAIA3_REF=1e13597c42d4858b7cfd7cfdae01e297263364b2
RUN pip install --no-cache-dir "maia3 @ git+https://github.com/CSSLab/maia3.git@${MAIA3_REF}"

# Bake the 5M checkpoint (~20MB) into the image so startup needs no network.
# Override HF_HOME onto a volume if you later want the 23m/79m nets.
RUN maia3-cache --model maia3-5m

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /srv
COPY app/ /srv/app/
COPY static/ /srv/static/
# The CC0 opening catalogue (3,810 named lines). Baked in, not fetched: the
# Lichess explorer API now needs a token and is capped at 25 req/min, so the
# book must work with no network at all.
COPY data/openings/ /srv/data/openings/

EXPOSE 5020
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5020"]
