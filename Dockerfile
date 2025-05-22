# llmbasedos/Dockerfile

# --- Base Stage ---
FROM python:3.10-slim-bullseye AS base
LABEL maintainer="Luca Mucciaccio/mucciaccioluca@gmail.com"
LABEL description="LLMBasedOS - MCP Gateway and Services Environment"

# Set core environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

# Default log level for llmbasedos components
ENV LLMBDO_LOG_LEVEL=INFO

# Default paths inside the container
ENV LLMBDO_LICENCE_FILE_PATH=/etc/llmbasedos/lic.key \
    LLMBDO_MCP_CAPS_DIR=/run/mcp \
    LLMBDO_MAIL_ACCOUNTS_CONFIG=/etc/llmbasedos/mail_accounts.yaml \
    LLMBDO_AGENT_WORKFLOWS_DIR=/etc/llmbasedos/workflows \
    LLMBDO_FS_DATA_ROOT=/mnt/user_data

# Gateway settings
ENV LLMBDO_GATEWAY_HOST=0.0.0.0 \
    LLMBDO_GATEWAY_WEB_PORT=8000 \
    LLMBDO_GATEWAY_UNIX_SOCKET_PATH=/run/mcp/gateway.sock

# Default LLM Provider settings
ENV LLMBDO_DEFAULT_LLM_PROVIDER=openai \
    OPENAI_DEFAULT_MODEL=gpt-3.5-turbo \
    LLAMA_CPP_URL=http://localhost:8080 \
    LLAMA_CPP_DEFAULT_MODEL=local-model
# OPENAI_API_KEY should be provided at runtime

# Application root directory within the container
# This directory will be added to PYTHONPATH and will contain the 'llmbasedos' package.
ENV APP_ROOT_DIR=/opt/app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    unzip \
    libmagic1 \
    # build-essential cmake # Uncomment if faiss-cpu or other needs them
    && echo "Installing rclone..." \
    && curl -fSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o rclone.zip \
    && unzip rclone.zip \
    && mv rclone-*-linux-amd64/rclone /usr/local/bin/ \
    && chown root:root /usr/local/bin/rclone \
    && chmod 755 /usr/local/bin/rclone \
    && rm -rf rclone.zip rclone-*-linux-amd64 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
ARG APP_USER=llmuser
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash ${APP_USER}

# Create necessary base directories
RUN mkdir -p ${APP_ROOT_DIR} /run/mcp /run/supervisor \
             /etc/llmbasedos /var/log/llmbasedos /var/log/supervisor \
             /var/lib/llmbasedos/faiss_index \
             /mnt/user_data

WORKDIR ${APP_ROOT_DIR}

# --- Builder Stage (for Python dependencies) ---
FROM base AS builder
# ARG APP_USER # Not strictly needed in builder unless used for chown or something

# WORKDIR ${APP_ROOT_DIR} # Inherited from base
COPY llmbasedos_src/requirements.txt /tmp/reqs/core_requirements.txt 
COPY llmbasedos_src/gateway/requirements.txt /tmp/reqs/gateway_requirements.txt
COPY llmbasedos_src/servers/fs/requirements.txt /tmp/reqs/servers_fs_requirements.txt
COPY llmbasedos_src/servers/sync/requirements.txt /tmp/reqs/servers_sync_requirements.txt
COPY llmbasedos_src/servers/mail/requirements.txt /tmp/reqs/servers_mail_requirements.txt
COPY llmbasedos_src/servers/agent/requirements.txt /tmp/reqs/servers_agent_requirements.txt

RUN pip install --no-warn-script-location \
	-r /tmp/reqs/core_requirements.txt \ 
    -r /tmp/reqs/gateway_requirements.txt \
    -r /tmp/reqs/servers_fs_requirements.txt \
    -r /tmp/reqs/servers_sync_requirements.txt \
    -r /tmp/reqs/servers_mail_requirements.txt \
    -r /tmp/reqs/servers_agent_requirements.txt

# --- Final Application Stage ---
FROM base AS final
ARG APP_USER

# WORKDIR ${APP_ROOT_DIR} # Inherited from base

COPY --from=builder /usr/local/lib/python3.10/site-packages/ /usr/local/lib/python3.10/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

COPY ./llmbasedos_src ${APP_ROOT_DIR}/llmbasedos 

ENV PYTHONPATH="${APP_ROOT_DIR}:${PYTHONPATH}"

RUN chown -R ${APP_USER}:${APP_USER} ${APP_ROOT_DIR}/llmbasedos \
    && chown ${APP_USER}:${APP_USER} /run/mcp \
    && chown -R ${APP_USER}:${APP_USER} /etc/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/log/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/log/supervisor \
    && chown -R ${APP_USER}:${APP_USER} /var/lib/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /mnt/user_data

COPY supervisord.conf /etc/supervisor/conf.d/llmbasedos_supervisor.conf

EXPOSE 8000

VOLUME /etc/llmbasedos /run/mcp /var/log/llmbasedos /var/log/supervisor /var/lib/llmbasedos /mnt/user_data

USER root
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/llmbasedos_supervisor.conf"]