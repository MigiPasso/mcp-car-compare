"""Assemblage du serveur MCP : transport stdio, et rien d'autre.

stdout est le canal JSON-RPC. Le piège est mesuré, pas théorique : la
bibliothèque Camoufox écrit sa barre de progression sur stdout
(PLAN.md §2), et une seule ligne de ce genre corrompt la session du client.
D'où deux règles tenues ici :

1. Aucun `print` dans le paquet, et `logging` est lié à `stderr` explicitement
   (`stream=sys.stderr`) : le handler par défaut écrit sur stderr, mais le dire
   évite qu'un `logging.basicConfig(level=...)` ajouté plus tard, sans `stream`,
   dépende de l'état d'un handler déjà installé ailleurs.
2. **On ne redirige pas le descripteur 1.** PLAN.md §3 le proposait comme
   seconde ceinture (`os.dup2(2, 1)`), et c'est justement le geste à ne pas
   faire ici : `dup2(2, 1)` sans conserver un duplicata du fd 1 envoie les
   réponses JSON-RPC elles-mêmes vers stderr, donc tue le protocole ; avec un
   duplicata, il faut réussir à faire écrire le transport sur ce duplicata, ce
   que `FastMCP.run("stdio")` n'expose pas. La protection reste donc la règle
   1, plus le test `tests/smoke_stdio.py` qui refuse le premier octet non
   JSON-RPC sur stdout.

Le module n'implémente aucun outil : il importe `register` de `tools.py`, qui
les enregistre sur l'instance passée. Une seule liste d'outils existe ainsi,
celle de `tools.py`.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from mcp_car_compare.config import load_config
from mcp_car_compare.tools import register

LOGGER = logging.getLogger(__name__)


def build_server() -> FastMCP:
    """Construit le serveur MCP et y enregistre les outils.

    Séparé de `main()` pour qu'un test puisse inspecter la surface d'outils
    (`await server.list_tools()`) sans lancer de transport ni consommer stdin.
    """
    server = FastMCP(
        "mcp-car-compare",
        instructions=(
            "Récolte d'annonces automobiles depuis **toutes** les sources servies "
            "(deux aujourd'hui, voir `list_brands`) vers un corpus SQLite local. "
            "`harvest` sans `sources` les interroge toutes : restreindre est un "
            "choix explicite. Chaque réponse de harvest et offers porte un bloc "
            "coverage : lire `read`/`unique`/`truncated` avant de conclure quoi "
            "que ce soit sur la couverture du marché."
        ),
    )
    register(server)
    return server


def main() -> None:
    """Démarre le serveur sur le transport stdio."""
    config = load_config()
    logging.basicConfig(
        stream=sys.stderr,
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    LOGGER.info(
        "démarrage stdio — db=%s budget_s=%d pages_max=%d",
        config.db_path,
        config.budget_s,
        config.pages_max,
    )
    build_server().run("stdio")
