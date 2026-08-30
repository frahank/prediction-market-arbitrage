# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Multi-stage build for the arbx paper-only research cockpit.
#
# Stage 1 (builder) installs the pinned dependency set from the committed
# lockfiles and the package itself into an isolated virtualenv. Stage 2
# (runtime) copies only that virtualenv onto a slim base and runs as a
# non-root user, so the image carries no build toolchain and no source cruft.
#
# Security note: the launcher calls enforce_localhost()
# (src/arbx/launcher.py), so the cockpit binds 127.0.0.1
# even if config tries to move it. Inside a container that means the UI is
# reachable only with `--network host`; the same invariant that keeps the
# research cockpit off the network in dev keeps it off the network in a
# container. Run the image with --network host for the local cockpit, or use
# it for the packaged CLIs (arbx-release-check, pair-health, capture).
# ---------------------------------------------------------------------------

# --- Stage 1: builder -------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install the pinned runtime dependency set first for layer-cache friendliness.
COPY requirements.lock ./
RUN pip install -r requirements.lock

# Then install the package itself (no deps: they are already pinned above).
COPY pyproject.toml README.md MANIFEST.in ./
COPY src ./src
COPY configs ./configs
RUN pip install --no-deps .

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    ARBX_MODE=paper

# Non-root runtime user.
RUN useradd --create-home --uid 10001 arbx

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --from=builder /build/configs ./configs

USER arbx

EXPOSE 8710

# Default to the localhost research cockpit; override with any packaged CLI
# (arbx-release-check, arbx-store-credentials) or a capture entrypoint.
ENTRYPOINT ["arbx-ui"]
