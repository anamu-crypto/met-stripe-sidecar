# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# Single image for the sidecar: receiver, worker, and migrator all run from it.
# The compose file overrides the command per service.
#
# Multi-stage so the final image doesn't carry pip caches or build metadata.
# We use psycopg2-binary (Alembic's sync driver) so no C toolchain is needed.
# -----------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Copy only what's needed to install — keeps the install layer cacheable.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
 && python -m pip install --prefix=/install .

# -----------------------------------------------------------------------------
# Final image
# -----------------------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim

ARG PYTHON_VERSION=3.12

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python${PYTHON_VERSION}/site-packages:/app/src"

# tini gives us a proper PID 1 that reaps children on shutdown signals.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /bin/bash sidecar

COPY --from=builder /install /install
WORKDIR /app
COPY --chown=sidecar:sidecar src/ ./src/
COPY --chown=sidecar:sidecar alembic/ ./alembic/
COPY --chown=sidecar:sidecar alembic.ini ./

USER sidecar
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command runs the receiver. The compose file overrides this for the
# worker and migrate services.
CMD ["uvicorn", "sidecar.server:app", "--host", "0.0.0.0", "--port", "8000"]
