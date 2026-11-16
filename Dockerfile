FROM python:3.12-slim

# PYTHONUNBUFFERED : sans lui, les lignes de log restent dans un tampon de
# bloc, et un serveur qui meurt avant le vidage ne laisse aucun diagnostic sur
# stderr — exactement le cas où on en a besoin.
# PYTHONPATH=/app/src : le paquet n'est pas installé (pas de build, pas de
# wheel), il est importé depuis l'arbre copié. C'est aussi ce qui donne un
# `python -m mcp_car_compare` indépendant du répertoire courant.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances système du Firefox que Camoufox embarque, plus Xvfb pour son
# headless « virtual » : c'est exactement la liste validée dans le lab du projet
# de référence (`ICH/lacentrale/Dockerfile`), pas une liste devinée. Sans Xvfb,
# `headless="virtual"` échoue au lancement ; sans `libgtk-3-0` et `libnss3`,
# Firefox ne démarre pas du tout.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo-gobject2 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdbus-glib-1-2 \
    libfontconfig1 \
    libfreetype6 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-shm0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxrender1 \
    libxtst6 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Les dépendances avant le code : une modification de `src/` ne réinstalle
# alors rien, seule une modification d'un requirements.txt invalide ce calque.
#
# `requirements-browser.txt` porte le side-car navigateur (Camoufox, FastAPI,
# uvicorn). Il est installé ici et nulle part ailleurs : le serveur MCP n'en a
# pas besoin, et `Dockerfile.test` ne l'installe pas — aucun test ne lance de
# navigateur.
COPY requirements.txt requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-browser.txt

# /data est un point de montage (volume), pas un répertoire de l'image : le
# corpus SQLite doit survivre à `docker run --rm`, sinon le curseur de reprise
# et la réutilisation du corpus entre appels — la raison d'être de la base —
# disparaissent à chaque conteneur.
#
# Ce conteneur ne tourne pas en root, parce que le corpus s'écrit dans /data :
# une base créée par root appartient à root sur l'hôte, et le prochain
# conteneur lancé avec un autre uid (`-u $(id -u)`), ou simplement l'opérateur
# qui veut lire le fichier, ne peut plus l'écrire. L'UID est fixe (10001) pour
# que ce propriétaire soit le même d'une reconstruction à l'autre ; /data lui
# appartient, donc SQLite crée `corpus.db` et son journal WAL sans privilège.
#
# L'utilisateur est créé AVANT le téléchargement du navigateur et AVANT `COPY
# src/`, pour que ce téléchargement (~1 Go, 70 s) ne soit pas refait à chaque
# modification du code : ces deux `RUN` n'ont aucune dépendance sur les sources.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mcc \
    && mkdir -p /data /home/mcc/.cache /home/mcc/.camoufox \
    && chown -R mcc:mcc /data /home/mcc
VOLUME ["/data"]

# HOME et XDG_CACHE_HOME sont figés dans l'image AVANT le téléchargement : c'est
# là que Camoufox range son navigateur (`user_cache_dir("camoufox")`). Fixer les
# deux rend l'emplacement prévisible, donc chownable, donc lisible au runtime
# par l'utilisateur non privilégié. `~/.camoufox` doit exister et être
# inscriptible : Firefox le sonde même quand Playwright fournit un profil
# temporaire, et un HOME en lecture seule fait échouer le lancement.
ENV HOME=/home/mcc \
    XDG_CACHE_HOME=/home/mcc/.cache

# Le navigateur est téléchargé À LA CONSTRUCTION, jamais au démarrage : une
# récolte ne doit pas dépendre d'un accès réseau à GitHub, et le conteneur n'a
# rien à télécharger pour servir une annonce. Le `chown` couvre les deux
# emplacements possibles de camoufox (~/.cache et le paquet Python), parce que
# le fetch écrit dans le premier et que le second doit rester lisible au runtime.
RUN python -m camoufox fetch \
    && chown -R mcc:mcc /home/mcc /usr/local/lib/python3.12/site-packages/camoufox

COPY src/ ./src/
COPY entrypoint.sh ./
# Le bit exécutable est posé ici plutôt que dans le dépôt : `ENTRYPOINT
# ["./entrypoint.sh"]` échoue sur un fichier non exécutable, et un checkout
# avec un umask différent ne doit pas casser l'image.
RUN chmod +x ./entrypoint.sh

# `USER` après le `chown` et avant l'entrypoint : le serveur MCP, le side-car
# navigateur et lui seul héritent de cet uid. `entrypoint.sh` garde son `exec`,
# qui est ce qui transmet SIGTERM au serveur au lieu de laisser un shell PID 1
# l'ignorer.
USER mcc

# Pas de HEALTHCHECK Docker, et c'est un choix. Le conteneur parle bien un port
# maintenant — celui du side-car (127.0.0.1:8899) — mais un `HEALTHCHECK` sur
# `/health` ne prouverait que la vie d'uvicorn : le navigateur peut être
# incapable de démarrer (bibliothèque manquante, Xvfb absent) et le side-car
# répondre « ok » à toutes les sondes. C'est `entrypoint.sh` qui porte la
# sonde qui compte, bornée à 30 s, et qui démarre le serveur MCP même si le
# side-car ne vient pas : perdre les autres sources pour un navigateur absent
# serait pire que d'avoir LaCentrale en erreur au premier appel.
#
# La preuve réelle du protocole reste `tests/smoke_stdio.py`, qui pilote le
# serveur en sous-processus et refuse le premier octet de stdout qui n'est pas
# du JSON-RPC :
#     docker run --rm -v "$PWD":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \
#       python:3.12-slim \
#       sh -c "pip install -q -r requirements-dev.txt && python tests/smoke_stdio.py"
# (`PYTHONDONTWRITEBYTECODE=1` n'est pas un détail : le conteneur tourne en root
# et laisserait sinon des `__pycache__` root dans l'arbre monté, qui bloquent le
# prochain `pytest` lancé depuis l'hôte.)
# S'il fallait vraiment un check, ce serait celui-là, avec une sonde MCP qui
# fait `initialize` puis `tools/list` : un client tiers, pas une simple socket.
#
# `chmod +x` plus haut est fait en root (les couches précédentes le sont),
# puis `USER mcc` : le serveur tourne non privilégié, et `id -u` dans
# l'image rend 10001 — c'est la preuve de cette directive.
ENTRYPOINT ["./entrypoint.sh"]
