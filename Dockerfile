FROM python:3.11-slim

# `uv` installs itself as a static binary into /usr/local/bin via its installer script's
# default install location — no separate package manager needed inside the image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first so an app-code-only change doesn't invalidate the dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY . .
RUN uv sync --no-dev

EXPOSE 8000

# Shell form (not exec-form) so `${PORT:-8000}` actually expands — platforms like Render inject
# their own $PORT at runtime and expect the container to bind to it; exec-form CMD doesn't do
# shell variable expansion, so `--port 8000` would be taken as a hardcoded literal. Falls back
# to 8000 when $PORT is unset (local `docker run`/`docker compose up`), so local usage is
# unchanged. No `--reload` here — that's a dev-server convenience, not something a container
# should run.
CMD ["sh", "-c", "uv run uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
