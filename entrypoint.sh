#!/bin/sh
# Point d'entrée de l'image : le side-car navigateur d'abord, le serveur MCP
# ensuite.
#
# `exec` sur le serveur n'est pas décoratif. Sans lui, ce shell reste PID 1, ne
# relaie pas SIGTERM, et le client qui ferme la session (pi, Claude Desktop)
# laisserait un serveur orphelin attaché au volume SQLite du corpus.
#
# **Le stdout du side-car va sur stderr, et c'est la ligne la plus importante de
# ce fichier.** stdout EST le canal JSON-RPC du serveur MCP : la bibliothèque
# Camoufox écrit sa barre de progression sur stdout (PLAN.md §2, mesuré), et
# uvicorn y met ses logs. Les laisser passer enverrait ces lignes au client
# comme des messages du protocole, et la session serait corrompue à la première
# récolte. Le side-car est un processus séparé pour la même raison ; la
# redirection est la seconde ceinture.
#
# **Un side-car absent ne bloque pas le serveur.** La boucle d'attente est
# bornée à 30 s, et elle sort aussi dès que le processus meurt : perdre les
# quatre outils pour un navigateur qui ne démarre pas serait pire que d'avoir
# LaCentrale en erreur au premier appel — les trois autres sources parlent
# httpx et n'ont besoin de rien de tout ça.
set -eu

# Le port du side-car, écrit ici en clair : `browser_sidecar.LISTEN_PORT` est la
# seule autre occurrence, et `tests/test_browser_sidecar.py` vérifie que cette
# valeur est bien celle que le code écoute (un désaccord ferait attendre 30 s
# pour rien, puis LaCentrale en erreur).
SIDECAR_HEALTH="http://127.0.0.1:8899/health"
SIDECAR_START_TIMEOUT_S=30

python -m mcp_car_compare.browser_sidecar >&2 2>&1 &
sidecar_pid=$!

waited=0
while [ "$waited" -lt "$SIDECAR_START_TIMEOUT_S" ]; do
    if MCC_SIDECAR_HEALTH="$SIDECAR_HEALTH" python -c \
        'import os, urllib.request; urllib.request.urlopen(os.environ["MCC_SIDECAR_HEALTH"], timeout=1)' \
        2>/dev/null; then
        break
    fi
    if ! kill -0 "$sidecar_pid" 2>/dev/null; then
        echo "entrypoint: side-car navigateur mort au démarrage" \
             "(voir stderr ci-dessus) — le serveur MCP démarre sans lui," \
             "LaCentrale sera en erreur au premier appel" >&2
        break
    fi
    sleep 1
    waited=$((waited + 1))
done

if [ "$waited" -ge "$SIDECAR_START_TIMEOUT_S" ]; then
    echo "entrypoint: side-car navigateur muet après ${SIDECAR_START_TIMEOUT_S} s" \
         "— le serveur MCP démarre sans lui" >&2
fi

# Les arguments éventuels ne sont pas transmis (`"$@"` absent), et c'est
# volontaire : `python -m mcp_car_compare` ne prend aucune option, tandis qu'un
# `docker run image sh -c '...'` deviendrait silencieusement un démarrage de
# serveur sans preuve. Ce piège a mordu une fois : le README doit donc préciser
# `--entrypoint sh` pour lancer quoi que ce soit d'autre que le serveur.
exec python -m mcp_car_compare
