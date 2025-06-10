# llmbasedos/Dockerfile

# --- Base Stage ---
FROM python:3.10-slim-bullseye AS base
LABEL maintainer="Luca Mucciaccio/mucciaccioluca@gmail.com"
LABEL description="LLMBasedOS - MCP Gateway and Services Environment"

# Set core environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=off 
ENV PIP_DISABLE_PIP_VERSION_CHECK=on
ENV PIP_DEFAULT_TIMEOUT=100
# Path for Hugging Face cache, will be owned by llmuser
ENV HF_HOME=/opt/app/llmbasedos_cache/huggingface
ENV TRANSFORMERS_CACHE=/opt/app/llmbasedos_cache/huggingface/hub
# Ensure pip installs user-specific packages into a path accessible later if needed,
# though we are installing system-wide here.
ENV PYTHONUSERBASE=/dev/null 

# Default log level for llmbasedos components
ENV LLMBDO_LOG_LEVEL=INFO

# Default paths inside the container (can be overridden by docker-compose env vars)
ENV LLMBDO_LICENCE_FILE_PATH=/etc/llmbasedos/lic.key
ENV LLMBDO_MCP_CAPS_DIR=/run/mcp
ENV LLMBDO_MAIL_ACCOUNTS_CONFIG=/etc/llmbasedos/mail_accounts.yaml
ENV LLMBDO_AGENT_WORKFLOWS_DIR=/etc/llmbasedos/workflows
ENV LLMBDO_FS_DATA_ROOT=/mnt/user_data

# Gateway settings
ENV LLMBDO_GATEWAY_HOST=0.0.0.0
ENV LLMBDO_GATEWAY_WEB_PORT=8000
ENV LLMBDO_GATEWAY_UNIX_SOCKET_PATH=/run/mcp/gateway.sock

# Default LLM Provider settings (OPENAI_API_KEY should be provided at runtime via .env)
ENV LLMBDO_DEFAULT_LLM_PROVIDER=openai
ENV OPENAI_DEFAULT_MODEL=gpt-4o
ENV LLAMA_CPP_URL=http://localhost:8080
ENV LLAMA_CPP_DEFAULT_MODEL=local-model

# Application root directory within the container
ENV APP_ROOT_DIR=/opt/app
WORKDIR ${APP_ROOT_DIR} 

# System dependencies installation
# Combine apt-get update, install, and clean into a single RUN layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    unzip \
    libmagic1 \
    # build-essential cmake swig # CRITICAL: Uncomment these ONLY if faiss-cpu or other libs MUST compile from source
                               # and if compiling is faster/more reliable than finding pre-built wheels.
                               # The goal is to AVOID compilation by using wheels.
    && echo "Installing rclone..." \
    && curl -fSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o rclone.zip \
    && unzip rclone.zip \
    && mv rclone-*-linux-amd64/rclone /usr/local/bin/ \
    && chown root:root /usr/local/bin/rclone \
    && chmod 755 /usr/local/bin/rclone \
    && rm -rf rclone.zip rclone-*-linux-amd64 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and group
ARG APP_USER=llmuser
ARG APP_UID=1000
ARG APP_GID=1000
# Using || true to prevent build failure if user/group already exists (e.g. in a modified base image)
RUN (groupadd -g ${APP_GID} ${APP_USER} || true) && \
    (useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash ${APP_USER} || true)

# Create base directories needed by the application and services
# Permissions will be adjusted by chown during build or entrypoint for volumes
RUN mkdir -p ${APP_ROOT_DIR} \
               /run/mcp \
               /run/supervisor \
               /etc/llmbasedos \
               /var/log/llmbasedos \
               /var/log/supervisor \
               /var/lib/llmbasedos/faiss_index \
               /mnt/user_data \
               ${HF_HOME}/hub
# Ensure the full HF cache path structure exists

# --- Builder Stage (for Python dependencies) ---
# --- Builder Stage (for Python dependencies) ---
# --- Builder Stage (for Python dependencies) ---
FROM base AS builder
# WORKDIR is inherited from base (/opt/app)

RUN mkdir -p /tmp/reqs_src

COPY llmbasedos_src/requirements.txt /tmp/reqs_src/core_requirements.txt
COPY llmbasedos_src/gateway/requirements.txt /tmp/reqs_src/gateway_requirements.txt
COPY llmbasedos_src/servers/fs/requirements.txt /tmp/reqs_src/servers_fs_requirements.txt
COPY llmbasedos_src/servers/sync/requirements.txt /tmp/reqs_src/servers_sync_requirements.txt
COPY llmbasedos_src/servers/mail/requirements.txt /tmp/reqs_src/servers_mail_requirements.txt
COPY llmbasedos_src/servers/agent/requirements.txt /tmp/reqs_src/servers_agent_requirements.txt

# DEBUG: Afficher le contenu des fichiers copiés individuellement
RUN echo "--- DEBUG: Content of /tmp/reqs_src/core_requirements.txt ---" && \
    cat /tmp/reqs_src/core_requirements.txt ; \
    echo "\n--- DEBUG: Content of /tmp/reqs_src/gateway_requirements.txt ---" && \
    cat /tmp/reqs_src/gateway_requirements.txt ; \
    echo "\n--- FIN DEBUG INDIVIDUAL FILES ---"

# Combine all requirements into one file for a single pip install command.
RUN echo "--extra-index-url https://download.pytorch.org/whl/cpu" > /tmp/all_requirements.txt && \
    echo "torch" >> /tmp/all_requirements.txt && \
    echo "torchvision" >> /tmp/all_requirements.txt && \
    echo "torchaudio" >> /tmp/all_requirements.txt && \
    (if [ -f /tmp/reqs_src/core_requirements.txt ]; then cat /tmp/reqs_src/core_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: core_requirements.txt not found, skipping."; fi) && \
    (if [ -f /tmp/reqs_src/gateway_requirements.txt ]; then cat /tmp/reqs_src/gateway_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: gateway_requirements.txt not found, skipping."; fi) && \
    (if [ -f /tmp/reqs_src/servers_fs_requirements.txt ]; then cat /tmp/reqs_src/servers_fs_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: servers_fs_requirements.txt not found, skipping."; fi) && \
    (if [ -f /tmp/reqs_src/servers_sync_requirements.txt ]; then cat /tmp/reqs_src/servers_sync_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: servers_sync_requirements.txt not found, skipping."; fi) && \
    (if [ -f /tmp/reqs_src/servers_mail_requirements.txt ]; then cat /tmp/reqs_src/servers_mail_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: servers_mail_requirements.txt not found, skipping."; fi) && \
    (if [ -f /tmp/reqs_src/servers_agent_requirements.txt ]; then cat /tmp/reqs_src/servers_agent_requirements.txt >> /tmp/all_requirements.txt; else echo "Warning: servers_agent_requirements.txt not found, skipping."; fi)

# DEBUG: Afficher le contenu du fichier concaténé
RUN echo "--- DEBUG: Content of /tmp/all_requirements.txt ---" && \
    cat /tmp/all_requirements.txt && \
    echo "--- FIN DEBUG ALL_REQUIREMENTS ---"

# Install Python dependencies using BuildKit cache for pip
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-warn-script-location -r /tmp/all_requirements.txt && \
    rm -rf /tmp/reqs_src /tmp/all_requirements.txt

# ... reste du Dockerfile ...
   

# --- Final Application Stage ---
FROM base AS final
ARG APP_USER # Make APP_USER arg available

# WORKDIR is inherited (/opt/app)

# Copy installed Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.10/site-packages/ /usr/local/lib/python3.10/site-packages/
# Copy any binaries/scripts installed by pip (e.g., uvicorn, other CLIs) from builder stage
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY ./llmbasedos_src ${APP_ROOT_DIR}/llmbasedos

# Set PYTHONPATH to include the application root so 'import llmbasedos' works
ENV PYTHONPATH="${APP_ROOT_DIR}:${PYTHONPATH}"

# Copy Supervisord configuration
COPY supervisord.conf /etc/supervisor/conf.d/llmbasedos_supervisor.conf

# Copy the entrypoint script and make it executable
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Set ownership of application code and HF_CACHE_DIR_BASE within the image.
# The entrypoint.sh will handle permissions for mounted volumes at runtime.
# HF_HOME points to /opt/app/llmbasedos_cache/huggingface
# HF_HOME%/* gives /opt/app/llmbasedos_cache
RUN chown -R ${APP_USER}:${APP_USER} ${APP_ROOT_DIR}/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /opt/app/llmbasedos_cache

# Expose the gateway port
EXPOSE 8000

# Define volumes that can be mounted from docker-compose.
# The entrypoint.sh is responsible for ensuring correct permissions on these at runtime if mounted.
VOLUME /etc/llmbasedos /run/mcp /var/log/llmbasedos /var/log/supervisor /var/lib/llmbasedos /mnt/user_data /opt/app/llmbasedos_cache

# Set the entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Default command (passed to entrypoint.sh)
# Supervisord runs as root (default user at this stage) to manage processes,
# which then drop to llmuser as configured in supervisord.conf.
# The -n flag (nodaemon) is important for Docker.
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/llmbasedos_supervisor.conf"]