# playwright_service/Dockerfile

# On part d'une image Node.js complète et stable.
FROM node:18-bookworm

# Installer les dépendances système requises par Playwright.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libgtk-3-0 libgbm1 libasound2 libx11-xcb1 \
    libxss1 libxtst6 xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Créer un utilisateur non-root et le répertoire de travail.
RUN useradd -ms /bin/bash appuser
USER appuser
WORKDIR /home/appuser/app

# Installer les dépendances Node.js.
RUN npm install express playwright uuid

# LA CORRECTION EST ICI :
# On télécharge les navigateurs nécessaires (Chromium par défaut).
# Cette commande est fournie par Playwright lui-même.
RUN npx playwright install

# Copier le script de notre serveur.
COPY --chown=appuser:appuser mcp-server.js .

# Exposer le port.
EXPOSE 5678

# La commande de démarrage finale.
CMD ["node", "mcp-server.js"]