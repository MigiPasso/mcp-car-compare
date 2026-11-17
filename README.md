# mcp-car-compare

Serveur MCP local (transport **stdio**) qui récolte des annonces automobiles,
les stocke dans un corpus SQLite et les expose à une IA pour choisir un modèle
à acheter. Il tourne dans Docker, ne parle pas HTTP, et n'utilise aucun
navigateur : la v1 lit AutoScout24 (`.fr`/`.de`) en `httpx` et `__NEXT_DATA__`.

**Périmètre de ce lot.** AutoScout24 seule, trois outils (`list_brands`,
`harvest`, `offers`). Pas de navigateur, pas d'Autohero, pas de LaCentrale, pas
d'`analyze_models` — voir PLAN.md §9 et §10. Un `coverage` obligatoire dit
toujours la part réellement lue : le serveur n'affirme jamais avoir vu tout le
marché.

## Construire et lancer

```sh
docker build -t mcp-car-compare .
docker run --rm --interactive --volume mcp-car-compare-data:/data mcp-car-compare
```

Trois détails du `docker run` ne sont pas décoratifs :

- **`--interactive` est obligatoire.** Le serveur lit ses requêtes sur stdin ;
  sans `-i`, Docker ferme stdin, le serveur voit EOF et s'arrête aussitôt. Un
  client MCP échouerait en « server disconnected ».
- **Jamais `--tty`.** Un terminal sur stdout réécrit les octets (échos,
  traductions de fin de ligne) ; le flux JSON-RPC n'y survivrait pas. C'est la
  même règle que « aucun `print` » vue de l'autre côté du tuyau.
- Le volume porte `/data` : sans lui, `docker run --rm` efface le corpus, donc
  le curseur de reprise et la réutilisation entre appels — la raison d'être de
  la base.

L'image tourne sous un utilisateur non privilégié (`mcc`, uid 10001) et `/data`
lui appartient : le corpus n'est donc pas créé par `root` dans le volume. Avec un
volume nommé, rien à faire. Avec un **bind mount** (`-v /un/chemin:/data`), c'est
le propriétaire du répertoire de l'hôte qui compte : donnez-lui l'uid 10001
(`chown 10001 /un/chemin`) ou lancez le conteneur avec `--user "$(id -u):$(id -g)"`.

## Preuve E0 : stdout ne contient que du JSON-RPC

```sh
docker run --rm --volume "$PWD":/app --workdir /app --entrypoint sh \
  -e PYTHONDONTWRITEBYTECODE=1 mcp-car-compare \
  -c 'python tests/smoke_stdio.py'
```

Aucun `pip install` : l'image porte déjà les dépendances d'exécution et le
script n'a besoin ni de pytest ni du réseau. C'est ce qui permet la preuve la
plus solide du lot — la même commande avec `--network none`, qui passe.

`--entrypoint sh` est nécessaire : l'image a `ENTRYPOINT ["./entrypoint.sh"]`,
donc une commande passée après le nom de l'image devient un **argument** de
l'entrypoint, que celui-ci ignore poliment. Sans `--entrypoint`, le conteneur
démarre le serveur, ne reçoit rien sur stdin (pas de `-i`), sort avec 0 — et on
croirait la preuve passée alors que rien n'a été testé.

Le script démarre le serveur en sous-processus, envoie `initialize`, `tools/list`,
un `tools/call` sur `offers` avec un `job_id` qui n'existe pas, puis un `ping`,
et enfin **relit chaque octet de stdout comme du JSON-RPC** : une barre de
progression, un `print` oublié ou un avertissement de bibliothèque font échouer
le test ici, pas chez l'utilisateur en pleine session. Il exige aussi trois
outils, une réponse structurée pour l'identifiant inconnu (erreur lisible ou
couverture à zéro, jamais un crash) et un serveur encore vivant après cet appel.

Le scénario est rejoué **deux fois**, avec `MCC_LOG_LEVEL=INFO` puis
`MCC_LOG_LEVEL=WARNING` : un serveur correct doit rendre le même stdout intact
aux deux niveaux. C'est la propriété du protocole ; l'ancienne assertion
« `stderr` n'est pas vide » n'en était qu'un proxy et échouait sur un serveur
parfait dès que le niveau de log le rendait silencieux. `stderr` est donc
compté et affiché, jamais exigé. Sortie attendue :

```
SMOKE OK — preuve E0
  initialize [INFO]: mcp-car-compare protocole 2025-06-18
  tools/list [INFO]: 3 outils — list_brands, harvest, offers
  tools/call offers(job_id inconnu) [INFO]: erreur lisible — …récolte inconnue : 'smoke-job-inexistant'
  ping après l'appel [INFO]: serveur vivant
  stdout [INFO]: 4 ligne(s), N octet(s), toutes JSON-RPC
  stderr [INFO]: N ligne(s) — informatif, le critère est stdout
  initialize [WARNING]: …
  …
  stdout [WARNING]: 4 ligne(s), N octet(s), toutes JSON-RPC
  stderr [WARNING]: 0 ligne(s) — informatif, le critère est stdout
```

**Aucun appel réseau par défaut**, et c'est une décision : `list_brands` fait un
GET réel chez AutoScout24 (~0,5 s, une table de marques figée dériverait en
silence). Une preuve qui dépendrait de ce site virerait au rouge quand il
éternue et le taperait à chaque exécution — le scénario par défaut se contente
donc d'un `offers` sur un identifiant inconnu, et passe même avec
`docker run --network none`. Pour voir le chemin réel sans en faire un critère :

```sh
# tolère explicitement les deux issues (liste, ou `error`) : informatif, pas une preuve
python tests/smoke_stdio.py --with-list-brands
```

Le validateur est lui-même attaqué au démarrage (barre de progression, ligne de
log, `jsonrpc` 1.0, traceback) : un validateur devenu permissif ne peut pas
rendre ce test vert.

## Brancher le serveur dans un client

La configuration est la même partout — `mcpServers`, un `command` et ses `args`.
`mcp.example.json` est prêt à copier :

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

**pi** — `~/.pi/agent/mcp.json` : fusionner le bloc `mcpServers` ci-dessus.
Pas de `--name` fixe, volontairement : un nom fixe empêche un second client de
démarrer et bloque tout si une session se termine mal — mesuré, `docker: Conflict.
The container name is already in use`. Deux clients partagent donc le volume, ce
que la base supporte (`WAL` + `busy_timeout`, et huit récoltes concurrentes
testées sans erreur). Pour nettoyer les conteneurs orphelins d'une session tuée :
`docker ps --filter ancestor=mcp-car-compare:latest -q | xargs -r docker rm -f`.

**Claude Desktop** — fusionner le même bloc dans :

| Système | Fichier |
|---|---|
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Puis redémarrer l'application. Les deux clients lancent l'image et parlent
stdio : rien d'autre à configurer, aucun port, aucun jeton.

## Les trois outils

| Outil | Arguments | Rend |
|---|---|---|
| `list_brands` | `source?` (`autoscout24`), `market?` (`fr` ou `de`) | `{source, market, brands: [{value, label}], error}` — un **GET réel** chez le site (~0,5 s), aucun `coverage` (aucune récolte) ; un site en échec rend `error` et `brands` vide, l'outil ne lève pas. `market` compte : les libellés sont rendus dans la langue du marché (`Série 3` en `.fr`, `3er` en `.de`), et c'est le libellé rendu ici que `harvest` attend. `models` **n'est pas rempli en v1** : la taxonomie ne porte les modèles d'une marque que sur une page déjà filtrée sur elle, donc la clé est absente plutôt que présente et toujours vide |
| `harvest` | `criteria`, `budget_s`, `resume?` | `{job_id, coverage, cursor, warnings, resumes}` — une ligne de `coverage` par source, celle de **cette** récolte. `resumes` porte un **jeton de reprise** par source qui n'est pas allée au bout : `resume="<jeton>"` reprend cette source seule, à sa page suivante, dans le même `job_id` (une récolte profonde se découpe donc en appels courts). `warnings` prévient quand la source annonce plus d'annonces qu'on n'en a lu, et cite ce jeton — jamais un `pages_max` plus grand |
| `offers` | `job_id`, `cursor?`, `limit` | `{items, next_cursor, has_more, coverage}` — le corpus, jamais la somme des récoltes |

`criteria` suit le contrat figé de `src/mcp_car_compare/models.py` :
`make`, `model`, `year_min/year_max`, `km_max`, `price_min/price_max`, `fuel`,
`gear`, `markets`, `pages_max`, `sort`. Trois règles décidées dans PLAN.md §8 :

- `budget_s` s'applique **par source** et gagne toujours sur `pages_max` ; la
  source qui ne rentre pas rend `status: "capped"` avec un jeton de reprise,
  jamais une erreur silencieuse ni un `read` inventé.
- Changer de `sort` invalide le curseur : un curseur porte son tri et le
  serveur refuse un curseur repris avec un autre tri plutôt que de rendre des
  pages incohérentes.
- `announced_total` peut être `null` quand le site se tait. L'absence est dite,
  pas comblée par `len(items)`.

`sources` n'accepte que ce que ce lot sait interroger (`autoscout24`) : toute
autre source du plan (`autohero`, `lacentrale`) est **refusée au parsage des
critères**, par une erreur qui nomme la source et celles servies. C'est dit
avant la première requête et non rendu en `status: "error"`, parce qu'un client
lirait « autohero a échoué » là où la vérité est « autohero n'est pas
implémentée ».

## Configuration

Tout par variables d'environnement, préfixe `MCC_`, lues **à chaque appel**
d'outil (jamais figées à l'import : sinon les tests dépendent de l'ordre
d'exécution).

| Variable | Défaut | Rôle |
|---|---|---|
| `MCC_DB` | `/data/corpus.db` | Fichier SQLite du corpus |
| `MCC_BUDGET_S` | `60` | Budget par source quand l'appelant n'en donne pas (entier ≥ 1, au plus `600`) |
| `MCC_PAGES_MAX` | `5` | Plafond de pages par source (entier ≥ 1, au plus `20`, le plafond du site) |
| `MCC_LOG_LEVEL` | `INFO` | Niveau des logs (sur stderr) |

Une valeur illisible, nulle, négative ou au-dessus du plafond est **refusée en
nommant la variable**, jamais rabattue sur le défaut : `MCC_PAGES_MAX=0` n'est
pas « aucune page » mais une récolte qui ne lit rien en ayant l'air de tourner.

Aucun secret, aucune clé d'API, aucun fichier de credentials : le corpus est un
fichier local. Il ne doit jamais y en avoir, et `.dockerignore` refuse déjà
`.env*` pour que ça ne rentre pas par accident dans une image.

## Trois contraintes qui expliquent le code

1. **stdout appartient au protocole.** Aucun `print` dans le paquet ; `logging`
   est lié explicitement à `sys.stderr` et l'image pose `PYTHONUNBUFFERED=1`
   (sinon un serveur qui meurt perd ses dernières lignes de log, précisément
   celles qu'on veut lire). Le fd 1 n'est **pas** redirigé : le
   `os.dup2(2, 1)` proposé dans PLAN.md §3 tuerait le protocole s'il ne garde
   pas un duplicata du fd 1, et le transport ne sait pas écrire sur ce
   duplicata. La ceinture réelle est `tests/smoke_stdio.py`.
2. **`mcp` est épinglé `<2`, volontairement.** mcp 2.x renomme `FastMCP` en
   `MCPServer` et `from mcp.server.fastmcp import FastMCP` y lève une erreur ;
   tout ce dépôt est écrit contre l'API 1.x. Le plafond garantit que
   `docker build` redonne le même comportement dans un an. Le porter sur l'API
   2.x est une migration, pas un `pip install -U`.
3. **Pas de `HEALTHCHECK`, et c'est un choix.** Ce conteneur ne parle ni HTTP
   ni TCP : il n'y a pas de port à interroger, et un processus vivant qui
   écrirait sur stdout serait « healthy » tout en cassant le protocole. La
   sonde utile, c'est le client de `tests/smoke_stdio.py` qui fait
   `initialize` + `tools/list`.

## Développer

```sh
# l'image de test : même base que l'image de production, plus pytest, plus
# `tests/` (construite une fois, la suite se relance en une seconde)
docker build -f Dockerfile.test -t mcp-car-compare:test .
docker run --rm mcp-car-compare:test

# preuve du transport stdio (aucun réseau)
docker run --rm mcp-car-compare:test python tests/smoke_stdio.py
```

`docker run --rm mcp-car-compare:test` imprime le compte de tests passé :
`Dockerfile.test` porte `tests/`, `pyproject.toml` (pour `pythonpath`) et
`-p no:cacheprovider`, donc rien n'a besoin d'être monté — **à une condition
lue plus bas** : après avoir touché un test, c'est la commande par montage qui
fait foi. `Dockerfile.test` et
`Dockerfile` partagent le même `.dockerignore`, qui n'exclut **pas** `tests/` ni
`requirements-dev.txt` : un `.dockerignore` vaut pour tous les builds du
répertoire, et les exclure casserait l'image de test en silence.

**Cette image fige `tests/` au moment du build** (`COPY tests/`). Une image non
reconstruite rend donc un vert qui ne voit pas une modification de test :
mesuré, un test ajouté sans rebuild donne « 211 passed », et le même test après
`docker build` échoue. La commande **autoritaire** est celle qui monte le dépôt
(ci-dessous) — c'est elle qui lit l'arbre de travail ; l'image de test est un
raccourci pour itérer sur `src/`, pas une preuve de la suite. Après avoir touché
un test, il faut soit `docker build`, soit le montage.

Les tests peuvent aussi se lancer depuis un conteneur jetable qui monte le
dépôt, sans construire d'image :

```sh
# l'image de test déjà construite, mais le dépôt monté : c'est l'arbre de
# travail qui tourne, sans rebuild et sans réinstaller les dépendances
docker run --rm --volume "$PWD":/app --workdir /app -e PYTHONDONTWRITEBYTECODE=1 \
  mcp-car-compare:test python -m pytest -q -p no:cacheprovider

# la même chose depuis l'image de base, quand aucune image de test n'existe
docker run --rm --volume "$PWD":/app --workdir /app -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.12-slim \
  sh -c 'pip install -q -r requirements-dev.txt && python -m pytest -p no:cacheprovider'
```

`PYTHONDONTWRITEBYTECODE=1` et `-p no:cacheprovider` ne sont pas des détails
dans cette variante : le conteneur tourne en `root`, et sans eux l'arbre monté
se remplit de `__pycache__` et de `.pytest_cache` appartenant à `root`, que le
prochain `pytest` lancé depuis l'hôte ne peut plus écrire. L'image de test, elle,
ne monte rien : les tests y sont copiés, donc aucun artefact ne peut atterrir
dans l'arbre.

Tout s'exécute dans Docker, y compris les tests : la différence de version de
Python entre l'hôte et l'image est exactement le genre d'écart qui ne se voit
qu'en production.

## Ce que ce lot ne fait pas

Pas de veille de prix, pas de scheduler, pas de serveur HTTP public, pas de
contournement de captcha, pas de calcul de frais (TVA, import, carte grise),
pas de classement « meilleure affaire » : `coverage` et les distributions
disent ce qui a été lu, le choix reste à l'IA et à toi.
