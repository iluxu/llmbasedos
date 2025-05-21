# llmbasedos/Dockerfile
# --- Base Stage ---
FROM python:3.10-slim-bullseye AS base
LABEL maintainer="[Your Name/Email]"
LABEL description="LLMBasedOS - MCP Gateway and Services Environment"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    # Default log level for llmbasedos components
    LLMBDO_LOG_LEVEL=INFO \
    # Default paths inside the container (can be overridden by docker run -e or docker-compose)
    LLMBDO_LICENCE_FILE_PATH=/etc/llmbasedos/lic.key \
    LLMBDO_MCP_CAPS_DIR=/run/mcp \
    LLMBDO_MAIL_ACCOUNTS_CONFIG=/etc/llmbasedos/mail_accounts.yaml \
    LLMBDO_AGENT_WORKFLOWS_DIR=/etc/llmbasedos/workflows \
    LLMBDO_FS_DATA_ROOT=/mnt/user_data \
    # Gateway settings
    LLMBDO_GATEWAY_HOST=0.0.0.0 \
    LLMBDO_GATEWAY_WEB_PORT=8000 \
    LLMBDO_GATEWAY_UNIX_SOCKET_PATH=/run/mcp/gateway.sock \
    # Default LLM Provider
    LLMBDO_DEFAULT_LLM_PROVIDER=openai \
    OPENAI_DEFAULT_MODEL=gpt-3.5-turbo \
    LLAMA_CPP_URL=http://localhost:8080 \
    LLAMA_CPP_DEFAULT_MODEL=local-model
    # OPENAI_API_KEY should be provided at runtime

# System dependencies
# Add any system libraries needed by Python packages (e.g., for cryptography, Pillow, FAISS, rclone, etc.)
# libmagic1 for python-magic, libfaiss-dev for faiss-cpu (if building from source, often not needed if installing wheel)
# rclone needs to be installed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    libmagic1 \
    # Add rclone installation (example: download binary)
    && curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip \
    && unzip rclone-current-linux-amd64.zip \
    && mv rclone-*-linux-amd64/rclone /usr/local/bin/ \
    && chown root:root /usr/local/bin/rclone \
    && chmod 755 /usr/local/bin/rclone \
    && rm -rf rclone-* \
    # Add faiss-cpu system package if available via apt, otherwise pip will try to build/fetch wheel
    # For Bullseye, faiss might not be directly available or outdated. Pip install is usually preferred.
    # Example for if you had a deb: apt-get install -y ./faiss-cpu.deb
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for running applications
ARG APP_USER=llmuser
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash ${APP_USER}

# Create necessary directories and set permissions
# These will be owned by root initially, runtime volumes will be mounted by user or Docker engine.
# Supervisor needs /run/supervisor, /var/log/supervisor
RUN mkdir -p /opt/llmbasedos /run/mcp /run/supervisor \
             /etc/llmbasedos /var/log/llmbasedos /var/log/supervisor \
             /var/lib/llmbasedos/faiss_index \
             /mnt/user_data \
    && chown -R ${APP_USER}:${APP_USER} /opt/llmbasedos \
    && chown ${APP_USER}:${APP_USER} /run/mcp \
    && chown -R ${APP_USER}:${APP_USER} /etc/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/log/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/lib/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /mnt/user_data
    # Supervisor logs will be root if supervisor runs as root, or app_user if it runs as app_user.

WORKDIR /opt/llmbasedos

# --- Builder Stage (for Python dependencies) ---
FROM base AS builder
# Copy only requirement files first to leverage Docker cache
COPY gateway/requirements.txt servers/fs/requirements.txt servers/sync/requirements.txt servers/mail/requirements.txt servers/agent/requirements.txt shell/requirements.txt /tmp/reqs/
# Consolidate requirements or install one by one
RUN pip install --no-warn-script-location \
    -r /tmp/reqs/gateway/requirements.txt \
    -r /tmp/reqs/servers/fs/requirements.txt \
    -r /tmp/reqs/servers/sync/requirements.txt \
    -r /tmp/reqs/servers/mail/requirements.txt \
    -r /tmp/reqs/servers/agent/requirements.txt \
    # Shell requirements are for external client, not usually needed in server container
    # -r /tmp/reqs/shell/requirements.txt
    # If faiss-cpu is not available as system package and needs build tools:
    # RUN apt-get update && apt-get install -y build-essential cmake && pip install faiss-cpu ... && apt-get purge -y build-essential cmake && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
    # For now, assume pip can find a wheel for faiss-cpu.

# --- Final Application Stage ---
FROM base AS final
# Copy Python dependencies from builder stage
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . /opt/llmbasedos
RUN chown -R ${APP_USER}:${APP_USER} /opt/llmbasedos

# Copy supervisord configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose gateway port
EXPOSE 8000

# Volume for licence key, configs, logs, data
# These are suggestions, users will map them.
VOLUME /etc/llmbasedos
VOLUME /run/mcp
VOLUME /var/log/llmbasedos
VOLUME /var/lib/llmbasedos
VOLUME /mnt/user_data

# By default, supervisord runs as root to manage processes.
# Processes themselves will be started as APP_USER via supervisord config.
USER root
# USER ${APP_USER} # If supervisord itself can run as non-root and manage child processes effectively.
                  # Typically, supervisord runs as root.

# Entrypoint
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

# Healthcheck (optional, add a /health endpoint to your gateway for this)
# HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
#   CMD curl -f http://localhost:8000/api/v1/health || exit 1