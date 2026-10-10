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

# No reload here — `--reload` is a dev-server convenience, not something a container should run.
CMD ["uv", "run", "uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
