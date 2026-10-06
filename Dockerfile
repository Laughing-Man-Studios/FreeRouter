# syntax=docker/dockerfile:1
#
# Multi-stage build for the Free LLM Router (spec 7, CONSTITUTION 3.2).
#
# The build stage resolves dependencies with uv into a self-contained venv; the
# runtime stage carries only that venv onto python:3.13-slim. Nothing from the
# build toolchain (uv itself, the project source, build caches) reaches the
# final image, so it stays small and has no package manager in it.
#
# Python 3.12 support is deliberately dropped, per spec 7.

# --- build stage -------------------------------------------------------------
FROM python:3.13-slim AS builder

# uv installs the project's dependencies quickly and reproducibly. Pinned to a
# major version so a rebuild cannot silently change the resolver.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed before the source is copied so that a source-only
# change does not invalidate the (slow) dependency layer.
#
# --no-install-project installs the declared dependencies but not the project
# itself; the application is then copied in and installed as a package. Note
# that hatchling needs the README, so it is copied too.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --no-editable is required, not stylistic. The default installs the project
# as a .pth pointing at /app/src, which only resolves because the source happens
# to sit in the image. Copying just the venv into the runtime stage discards
# /app/src and leaves the import unresolvable at runtime.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime stage -----------------------------------------------------------
FROM python:3.13-slim AS runtime

# Non-root by default (spec 7, CONSTITUTION 3.2). A uid/gid pair is pinned so
# the mounted volume's ownership is predictable on the host rather than
# depending on image-assigned uid.
ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --gid ${APP_GID} router \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --shell /usr/sbin/nologin router

# /data is the volume mount point. It is created here and handed to the
# non-root user, because SQLite must create and write the database file and a
# bind-mounted host directory inherits host ownership instead of this one.
RUN mkdir -p /data && chown ${APP_UID}:${APP_GID} /data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROUTER_DB_PATH=/data/router.db

WORKDIR /app

COPY --from=builder --chown=${APP_UID}:${APP_GID} /app/.venv /app/.venv

# config.yaml carries only the model mapping, never secrets (spec 6), so it is
# baked in. Secrets come from the environment at runtime.
COPY --chown=${APP_UID}:${APP_GID} config.yaml ./config.yaml

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

# uvloop is specified explicitly rather than left to autoloading: uvicorn will
# silently fall back to asyncio if the loop package is missing, so naming it
# turns a missing dependency into a visible startup error.
CMD ["uvicorn", "free_router.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--loop", "uvloop", \
     "--no-access-log"]

# The healthcheck hits a dedicated liveness endpoint rather than /v1/models or a
# chat completion, so a probe never consumes provider quota. `--fail` makes a
# non-200 exit non-zero, which is what Docker needs to mark the container
# unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
         "import sys,urllib.request;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]