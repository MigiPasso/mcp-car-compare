"""Point d'entrée `python -m mcp_car_compare`.

Le module existe pour que l'image Docker n'ait pas à connaître un chemin de
script (`ENTRYPOINT` appelle `python -m mcp_car_compare`) : le paquet est sur
`PYTHONPATH` via `src`, donc le lancement ne dépend pas du répertoire courant.
"""

from __future__ import annotations

from mcp_car_compare.server import main

if __name__ == "__main__":
    main()
