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

# Les dépendances avant le code : une modification de `src/` ne réinstalle
# alors rien, seule une modification de requirements.txt invalide ce calque.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY entrypoint.sh ./
# Le bit exécutable est posé ici plutôt que dans le dépôt : `ENTRYPOINT
# ["./entrypoint.sh"]` échoue sur un fichier non exécutable, et un checkout
# avec un umask différent ne doit pas casser l'image.
RUN chmod +x ./entrypoint.sh

# /data est un point de montage (volume), pas un répertoire de l'image : le
# corpus SQLite doit survivre à `docker run --rm`, sinon le curseur de reprise
# et la réutilisation du corpus entre appels — la raison d'être de la base —
# disparaissent à chaque conteneur.
RUN mkdir -p /data
VOLUME ["/data"]

# Pas de HEALTHCHECK, et c'est un choix : ce conteneur ne parle ni HTTP ni TCP,
# il parle JSON-RPC sur stdin/stdout, donc il n'y a pas de port à interroger ici.
# Un `HEALTHCHECK` qui testerait la vie du processus ne prouverait rien de plus
# que le fait qu'il tourne — un serveur qui écrit sur stdout et corrompt le
# protocole serait « healthy ».
#
# La preuve réelle est `tests/smoke_stdio.py`, qui pilote le serveur en
# sous-processus et refuse le premier octet de stdout qui n'est pas du JSON-RPC :
#     docker run --rm -v "$PWD":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \
#       python:3.12-slim \
#       sh -c "pip install -q -r requirements-dev.txt && python tests/smoke_stdio.py"
# (`PYTHONDONTWRITEBYTECODE=1` n'est pas un détail : le conteneur tourne en root
# et laisserait sinon des `__pycache__` root dans l'arbre monté, qui bloquent le
# prochain `pytest` lancé depuis l'hôte.)
# S'il fallait vraiment un check, ce serait celui-là, avec une sonde MCP qui
# fait `initialize` puis `tools/list` : un client tiers, pas une simple socket.

ENTRYPOINT ["./entrypoint.sh"]
