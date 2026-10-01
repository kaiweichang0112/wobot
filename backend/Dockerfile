# Both stages must use the same Python: the virtualenv links to the builder's interpreter.
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /bin/uv

WORKDIR /app
# Precompile bytecode for faster cold starts, copy files out of uv's cache instead of
# hardlinking, and fail rather than download a Python the runtime stage doesn't have.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first: this layer is reused until .python-version, pyproject.toml or uv.lock change.
COPY .python-version pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev --no-editable

# tiktoken downloads its encoding on first use. Fetch it into the image, so a job needs
# neither that host nor the download at run time.
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken
RUN .venv/bin/python -c "from wobot.knowledge.tokens import count_tokens; count_tokens('warm')"

# Runtime stage: the application and its virtualenv, without uv or its download cache.
FROM ${PYTHON_IMAGE}

RUN useradd --uid 10001 wobot
WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken
USER wobot

# Cloud Run sets $PORT; `exec` lets uvicorn receive SIGTERM for a graceful shutdown.
# The jobs reuse this image: `alembic upgrade head` migrates, `wobot-ingest run` ingests.
CMD ["sh", "-c", "exec uvicorn wobot.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
