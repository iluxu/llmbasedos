# llmbasedos/Dockerfile - VERSION FINALE OPTIMISÉE

FROM python:3.10-slim-bullseye

LABEL maintainer="Luca Mucciaccio <mucciaccioluca@gmail.com>"
LABEL description="llmbasedos - Cognitive Agent PaaS"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    APP_ROOT_DIR=/opt/app

WORKDIR ${APP_ROOT_DIR}

# --- 1. Dépendances Système (couche lente, mise en cache) ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    supervisor \
    curl \
    docker.io \
    libmagic1 \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# --- 2. Installation des dépendances Python (couche lente, mise en cache) ---
# Copie unique des fichiers de dépendances pour maximiser l'utilisation du cache.
COPY llmbasedos_src/requirements.txt                      /tmp/reqs/00-root.txt
COPY llmbasedos_src/gateway/requirements.txt              /tmp/reqs/01-gateway.txt
COPY llmbasedos_src/servers/fs/requirements.txt           /tmp/reqs/02-fs.txt
COPY llmbasedos_src/servers/mail/requirements.txt         /tmp/reqs/03-mail.txt
COPY llmbasedos_src/servers/executor/requirements.txt     /tmp/reqs/04-executor.txt
COPY llmbasedos_src/servers/crypto_data/requirements.txt  /tmp/reqs/05-crypto_data.txt
COPY llmbasedos_src/servers/football_data/requirements.txt /tmp/reqs/06-football_data.txt
COPY llmbasedos_src/servers/horse_racing_data/requirements.txt /tmp/reqs/07-horse_racing_data.txt
COPY llmbasedos_src/shell/requirements.txt                /tmp/reqs/08-shell.txt
COPY llmbasedos_src/servers/arc_manager/requirements.txt  /tmp/reqs/09-arc-manager.txt
COPY llmbasedos_src/servers/browser/requirements.txt /tmp/reqs/10-browser.txt

# Installation des dépendances. Cette couche ne sera reconstruite que si un .txt change.
RUN cat /tmp/reqs/*.txt | sed '/^[ \t]*#/d' | sed '/^$/d' | sort -u > /tmp/all_requirements.txt && \
    pip install --no-cache-dir -r /tmp/all_requirements.txt && \
    rm -rf /tmp/reqs /tmp/all_requirements.txt

# --- 3. Création de l'utilisateur et des dossiers (couche rapide, mise en cache) ---
ARG APP_USER=llmuser
RUN useradd -ms /bin/bash ${APP_USER} && \
    usermod -aG docker ${APP_USER} && \
    mkdir -p /run/mcp /var/log/supervisor /data

# --- 4. Copie du code et des configs (couches qui changent souvent) ---
# Ces étapes sont à la fin. Si seul le code change, la build sera quasi instantanée.
COPY ./llmbasedos_src ${APP_ROOT_DIR}/llmbasedos_src
COPY ./supervisord.conf /etc/supervisor/conf.d/llmbasedos.conf
COPY ./entrypoint.sh /opt/app/entrypoint.sh
RUN chmod +x /opt/app/entrypoint.sh

# --- 5. Configuration finale ---
ENV PYTHONPATH=/opt/app

# Donner les permissions sur le code de l'application. 
# Les permissions pour /data et /run/mcp sont gérées par l'entrypoint.
RUN chown -R ${APP_USER}:${APP_USER} ${APP_ROOT_DIR}

# Le point d'entrée s'occupera de la configuration finale avant de lancer la CMD.
ENTRYPOINT ["/opt/app/entrypoint.sh"]

# Commande par défaut, passée en argument à l'entrypoint.
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/llmbasedos.conf"]