# syntax=docker/dockerfile:1.6

# =========================================================
# == STAGE 1: BUILDER - Build Python wheels
# =========================================================
FROM python:3.10-slim AS builder
LABEL stage=builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/app

# Install system deps needed for building python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libmagic1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy all requirements files into one place
COPY llmbasedos_src/ ./llmbasedos_src/
RUN mkdir -p /tmp/reqs && \
    find ./llmbasedos_src -name "requirements.txt" -exec cat {} + >> /tmp/reqs/all.txt

# Install all dependencies into a wheelhouse
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /tmp/wheels -r /tmp/reqs/all.txt

# =========================================================
# == STAGE 2: RUNTIME - Final image
# =========================================================
FROM python:3.10-slim
LABEL description="LLMbasedOS DEV image - All services"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ROOT_DIR=/opt/app

WORKDIR ${APP_ROOT_DIR}

# Minimal runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor curl libmagic1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy pre-built wheels from builder stage
COPY --from=builder /tmp/wheels /tmp/wheels
COPY --from=builder /tmp/reqs/all.txt /tmp/reqs/all.txt

# Install all dependencies from local wheels
RUN pip install --no-index --find-links=/tmp/wheels -r /tmp/reqs/all.txt

# Cleanup
RUN rm -rf /tmp/wheels /tmp/reqs

# Create app user
ARG APP_USER=llmuser
RUN useradd -ms /bin/bash ${APP_USER} && \
    mkdir -p /run/mcp /var/log/supervisor /data && \
    chown -R ${APP_USER}:${APP_USER} ${APP_ROOT_DIR} /data /run/mcp

# Copy application code and configs
# This will be overwritten by the volume mount in dev, which is what we want.
COPY ./llmbasedos_src ${APP_ROOT_DIR}/llmbasedos_src
COPY ./supervisord.conf /etc/supervisor/conf.d/llmbasedos.conf
COPY ./entrypoint.sh /opt/app/entrypoint.sh
RUN chmod +x /opt/app/entrypoint.sh

ENV PYTHONPATH=/opt/app

USER ${APP_USER} # Run as non-root user

ENTRYPOINT ["/opt/app/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/llmbasedos.conf"]