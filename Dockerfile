# syntax=docker/dockerfile:1
#
# One image, two roles.
#
# The `agent` service and the CLI `client` are the same installed package
# invoked with two different commands. They share a dependency set, a settings
# object and a wire contract; the only thing that differs is the process
# entrypoint. Two near-identical Dockerfiles would be duplication wearing an
# architecture costume, and would also let the two drift apart — a client that
# is built differently from the service it drives measures something else.
#
# Two stages: the builder has uv and a compiler-free wheel install; the runtime
# has neither uv nor build tooling, just the virtualenv and the package.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# uv arrives as a static binary from its own image rather than via pip, so the
# runtime stage never inherits a Python-level installer.
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

ARG UV_EXTRAS=""

# Dependencies before source. They change on the order of once a month and the
# source changes every commit, so this layer survives almost every rebuild.
#
# `--locked` is the point of the lock file: it asserts uv.lock still matches
# pyproject.toml and fails the build if it does not, so the image can never be
# built against a resolution nobody reviewed.
#
# What actually keeps pytest and ruff out of the runtime image is that `dev` is
# an entry in [project.optional-dependencies] and `uv sync` installs no extras
# unless asked. `--no-dev` excludes PEP 735 dependency *groups*, of which this
# project has none, so it is belt to that braces: it costs nothing and it holds
# the line if `dev` ever moves into a group.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project ${UV_EXTRAS}

# Then the package itself, built as a wheel rather than installed editable:
# the image should carry the code, not a path pointing at a build directory
# the runtime stage does not have.
COPY src/ ./src/
COPY examples/ ./examples/
# `--reinstall-package trail` is not belt and braces, it is the fix for a bug
# that costs an afternoon. The uv cache mount survives between builds, and uv
# keys a local project's built wheel on its declared version — so with the
# version unchanged it reuses the cached wheel and **source edits never reach
# the image**. The build succeeds, the container starts, and it runs the code
# from before your change. Reinstalling only this package keeps the dependency
# cache, which is where the build time actually is.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package trail ${UV_EXTRAS}

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Non-root. The uid is fixed so a bind-mounted volume has predictable
# ownership across machines.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin trail

COPY --from=builder --chown=trail:trail /opt/venv /opt/venv

WORKDIR /app

# `examples/` is copied in with the package rather than mounted, because the
# agent is selected by name at runtime (TRAIL_AGENT) and an image that cannot
# resolve the name it is configured with fails at startup instead of at the
# first request.
#
# The documents come too, and this is load-bearing rather than tidy: the guide
# example answers *from* them, and in the image the package lives under
# site-packages, whose parents hold no README. Without these the agent runs,
# looks healthy, and answers every question with "not documented".
# `db/` ships because `trail.evals.store` applies this same file to create its
# two tables on a volume that predates the harness. One DDL definition, read
# by both the Postgres init hook and the harness.
COPY --chown=trail:trail README.md docker-compose.yml ./
COPY --chown=trail:trail db/ ./db/
COPY --chown=trail:trail docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER trail

EXPOSE 8080

# Image-level liveness makes the contract portable beyond this Compose file.
# urllib is in the standard library, so the runtime gains no diagnostic tools.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

# The entrypoint chooses the observability backend: compose keeps the default
# `langfuse` path (trail.telemetry owns OTLP export); AgentCore sets
# TRAIL_OTEL_MODE=adot so opentelemetry-instrument wraps uvicorn before import.
CMD ["/app/entrypoint.sh"]
