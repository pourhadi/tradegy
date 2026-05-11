FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=America/New_York \
    TRADEGY_DATA_DIR=/var/lib/tradegy/data \
    TRADEGY_MARKET_TZ=America/New_York

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY scripts/live_mes_0dte.py ./scripts/live_mes_0dte.py

RUN uv sync --locked --no-dev

RUN useradd --create-home --shell /usr/sbin/nologin tradegy \
    && mkdir -p /var/lib/tradegy/data/live_options \
    && chown -R tradegy:tradegy /app /var/lib/tradegy

USER tradegy

ENV PATH=/app/.venv/bin:$PATH

ENTRYPOINT ["python", "scripts/live_mes_0dte.py"]
