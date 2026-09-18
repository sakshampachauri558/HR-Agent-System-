# syntax=docker/dockerfile:1
#
# Multi-stage build. Targets `dev` and `runtime` are referenced by name from
# docker-compose.override.yml and docker-compose.yml respectively — do not
# rename them.
#
# Stage order matters for the rebuild loop: requirements.txt is copied and
# installed in `deps` BEFORE application source is copied in `dev`/`runtime`,
# so editing app code never invalidates the pip-install layer.

ARG PYTHON_VERSION=3.12-slim

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# ---------------------------------------------------------------------------
# deps: compiles/installs python packages into an isolated prefix. The
# build toolchain (gcc etc.) lives only in this stage and never reaches
# `dev` or `runtime`.
FROM base AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# models: bakes the FastEmbed embedding model into the image at BUILD time.
# Deliberate: if the ~130MB model instead downloaded on the first request,
# the opening query of the demo would stall. Baking it in means it's
# already on disk before the container ever starts.
FROM base AS models
COPY --from=deps /install /usr/local
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN mkdir -p /opt/models \
    && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/models')"

# ---------------------------------------------------------------------------
# dev: deps + baked model + source. docker-compose.override.yml supplies
# the actual `command:` (uvicorn --reload) and bind-mounts app/ and
# scripts/ over what's copied here; this CMD is just the standalone default.
FROM base AS dev
COPY --from=deps /install /usr/local
COPY --from=models /opt/models /opt/models
ENV FASTEMBED_CACHE_PATH=/opt/models
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
# runtime: deps + baked model + source, non-root. What the base
# docker-compose.yml actually deploys.
FROM base AS runtime
COPY --from=deps /install /usr/local
COPY --from=models /opt/models /opt/models
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN groupadd -g 10001 appuser \
    && useradd -u 10001 -g appuser -M -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app /opt/models
COPY --chown=appuser:appuser . .
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
