# ==========================
# Base Stage
# ==========================

FROM python:3.10-slim-bullseye AS base

LABEL maintainer="Luca Mucciaccio <mucciaccioluca@gmail.com>"
LABEL description="LLMBasedOS - MCP Gateway and Services Environment"

# Core environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    HF_HOME=/opt/app/llmbasedos_cache/huggingface \
    TRANSFORMERS_CACHE=/opt/app/llmbasedos_cache/huggingface/hub \
    PYTHONUSERBASE=/dev/null \
    LLMBDO_LOG_LEVEL=INFO \
    APP_ROOT_DIR=/opt/app

WORKDIR ${APP_ROOT_DIR}

# System dependencies - On garde votre logique, on ajoute juste socat.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    unzip \
    libmagic1 \
    docker.io \
    socat \
    && curl -fSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o rclone.zip \
    && unzip rclone.zip \
    && mv rclone-*-linux-amd64/rclone /usr/local/bin/ \
    && chmod 755 /usr/local/bin/rclone \
    && rm -rf rclone.zip rclone-*-linux-amd64 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
ARG APP_USER=llmuser
ARG APP_UID=1000
ARG APP_GID=1000
RUN (groupadd -g ${APP_GID} ${APP_USER} || true) && \
    (useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash ${APP_USER} || true)

RUN usermod -aG docker ${APP_USER}

# Create required directories
RUN mkdir -p \
    ${APP_ROOT_DIR} \
    /run/mcp \
    /run/supervisor \
    /etc/llmbasedos \
    /var/log/llmbasedos \
    /var/log/supervisor \
    /var/lib/llmbasedos/faiss_index \
    /mnt/user_data \
    ${HF_HOME}/hub

# ==========================
# Builder Stage (Python deps)
# ==========================

FROM base AS builder

# On garde votre logique de copie qui fonctionnait
RUN mkdir -p /tmp/reqs_src
COPY llmbasedos_src/requirements.txt /tmp/reqs_src/core_requirements.txt
COPY llmbasedos_src/gateway/requirements.txt /tmp/reqs_src/gateway_requirements.txt
COPY llmbasedos_src/servers/fs/requirements.txt /tmp/reqs_src/servers_fs_requirements.txt
COPY llmbasedos_src/servers/sync/requirements.txt /tmp/reqs_src/servers_sync_requirements.txt
COPY llmbasedos_src/servers/mail/requirements.txt /tmp/reqs_src/servers_mail_requirements.txt
COPY llmbasedos_src/servers/agent/requirements.txt /tmp/reqs_src/servers_agent_requirements.txt
# Ajouter les autres si vous en avez
COPY llmbasedos_src/shell/requirements.txt /tmp/reqs_src/shell_requirements.txt
COPY llmbasedos_src/servers/tiktok/requirements.txt /tmp/reqs_src/tiktok_requirements.txt

# On garde votre logique de concaténation qui fonctionnait
RUN echo "--extra-index-url https://download.pytorch.org/whl/cpu" > /tmp/all_requirements.txt && \
    echo "torch" >> /tmp/all_requirements.txt && \
    echo "torchvision" >> /tmp/all_requirements.txt && \
    echo "torchaudio" >> /tmp/all_requirements.txt && \
    for f in /tmp/reqs_src/*_requirements.txt; do \
      if [ -f "$f" ]; then cat "$f" >> /tmp/all_requirements.txt && echo "" >> /tmp/all_requirements.txt; fi \
    done

# On garde votre logique d'installation qui fonctionnait
# (J'enlève juste le `pip install docker` qui était la source de confusion initiale)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-warn-script-location --upgrade pip && \
    pip install --no-warn-script-location -r /tmp/all_requirements.txt && \
    rm -rf /tmp/reqs_src /tmp/all_requirements.txt

# ==========================
# Final Stage
# ==========================

FROM base AS final

ARG APP_USER

COPY --from=builder /usr/local/lib/python3.10/site-packages/ /usr/local/lib/python3.10/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

COPY ./llmbasedos_src ${APP_ROOT_DIR}/llmbasedos
COPY supervisord.conf /etc/supervisor/conf.d/llmbasedos_supervisor.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONPATH="${APP_ROOT_DIR}:${PYTHONPATH}"

# CORRECTION FINALE ET UNIQUE : Appliquer les permissions à la toute fin
RUN chown -R ${APP_USER}:${APP_USER} ${APP_ROOT_DIR} \
    && chown -R ${APP_USER}:${APP_USER} /etc/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/log/llmbasedos \
    && chown -R ${APP_USER}:${APP_USER} /var/lib/llmbasedos \
    && chown ${APP_USER}:${APP_USER} /mnt/user_data

EXPOSE 8000

VOLUME /etc/llmbasedos /run/mcp /var/log/llmbasedos /var/log/supervisor /var/lib/llmbasedos /mnt/user_data /opt/app/llmbasedos_cache

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/llmbasedos_supervisor.conf"]