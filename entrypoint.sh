#!/bin/sh
# Point d'entrée de l'image : un seul processus, le serveur MCP.
#
# `exec` n'est pas décoratif. Sans lui, ce shell reste PID 1, ne relaie pas
# SIGTERM, et le client qui ferme la session (pi, Claude Desktop) laisserait un
# serveur orphelin attaché au volume SQLite du corpus.
#
# Aucun side-car, aucun uvicorn dans ce lot : la v1 parle AutoScout24 en httpx,
# donc il n'y a pas de second processus à superviser ici. Le jour où la source
# navigateur arrive (E4 du plan), c'est ce fichier qui devra porter son
# healthcheck et son redémarrage — un side-car meurt silencieusement, et
# `coverage.status="error"` ne le verrait pas.
#
# Les arguments éventuels ne sont pas transmis (`"$@"` absent), et c'est
# volontaire : `python -m mcp_car_compare` ne prend aucune option, tandis qu'un
# `docker run image sh -c '...'` deviendrait silencieusement un démarrage de
# serveur sans preuve. Ce piège a mordu une fois : le README doit donc
# préciser `--entrypoint sh` pour lancer quoi que ce soit d'autre que le serveur.
set -eu

exec python -m mcp_car_compare
