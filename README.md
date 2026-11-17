# mcp-car-compare

Serveur MCP **stdio** qui récolte des annonces automobiles, les range dans un
corpus SQLite et les rend à une IA pour choisir un modèle à acheter.

Deux sources d'annonces, dont une lue par un navigateur embarqué. Tout tourne
dans Docker ; aucun port, aucun jeton.

## Construire et lancer

```sh
docker build -t mcp-car-compare:latest .     # ~5 Go : le navigateur est dedans
docker run --rm -i -v mcp-car-compare-data:/data mcp-car-compare:latest
```

`-i` est obligatoire (le serveur lit stdin) et `--tty` est interdit (un terminal
réécrirait le flux JSON-RPC). Le volume porte le corpus : sans lui, `--rm`
efface tout à chaque session.

## Brancher dans un client

```json
{
  "mcpServers": {
    "mcp-car-compare": {
      "command": "docker",
      "args": ["run", "--rm", "--interactive",
               "--volume", "mcp-car-compare-data:/data", "mcp-car-compare:latest"]
    }
  }
}
```

- **pi** : fusionner dans `~/.pi/agent/mcp.json`
- **Claude Desktop** : `~/.config/Claude/claude_desktop_config.json` (Linux),
  `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS),
  `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

Puis redémarrer le client. `mcp.example.json` est prêt à copier.

## Les quatre outils

| Outil | Rôle |
|---|---|
| `list_brands(source, market)` | les marques disponibles |
| `harvest(criteria, budget_s, resume)` | récolte bornée dans le temps, reprise sur jeton |
| `offers(job_id, cursor, limit)` | pages de 25, avec écart au marché et doublons signalés |
| `analyze_models(criteria, limit)` | médiane, quartiles et effectifs par modèle |

`criteria` : `make`, `model` (les libellés rendus par les annonces), `year_min`
/ `year_max`, `km_max`, `price_min` / `price_max`, `fuel`, `gear`, `markets`,
`sources`, `pages_max` (10 par défaut).

## Ce que les réponses disent toujours

Chaque `harvest` et `offers` porte un bloc `coverage` : annonces **annoncées par
la source**, **lues**, **uniques**, et si la lecture est tronquée. Le serveur
n'affirme jamais avoir vu tout le marché. Un prix sous 100 € (« prix non
communiqué ») est compté comme annonce mais exclu des médianes.

Une lecture profonde coûte du temps : la source qui passe par le navigateur
demande ~20 s de démarrage puis ~8 s par page. Rester sous 3 à 4 pages par
appel, et reprendre avec le jeton rendu.

## Tests

```sh
docker run --rm -v "$PWD":/app -w /app \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPYCACHEPREFIX=/tmp/pyc \
  python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Les tests qui rejouent des pages de sites sont **sautés** si les captures sont
absentes : elles ne sont pas publiées dans le dépôt.

## Documentation

- `PLAN.md` — conception, décisions prises et raisons.
- `RESTE-A-FAIRE.md` — état du projet, ce qui reste, ce qui est mesuré.
