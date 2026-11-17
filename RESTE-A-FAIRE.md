# Reste à faire

État au 2026-11-28, dernier commit `1bcb5ec`. À lire en premier si la session reprend.

## Ce qui marche

Serveur MCP stdio, 4 outils, **377 tests**, image Docker 5 Go (navigateur inclus).

| Outil | Ce qu'il fait |
|---|---|
| `list_brands(source, market)` | marques du site (~0,5 s, un GET) |
| `harvest(criteria, budget_s, resume)` | récolte AutoScout24 + LaCentrale, 10 pages par défaut, plafond 20 ; **reprenable** par jeton |
| `offers(job_id, cursor, limit)` | pages de 25, avec écart au marché et doublons marqués |
| `analyze_models(criteria, limit)` | médiane / quartiles / effectifs par modèle |

Branché dans `~/.pi/agent/mcp.json`.

## En attente : Autohero (3ᵉ source)

**La mesure est faite** — ne pas la refaire :

- `/fr/search/` : 12 annonces en HTML brut, **29 après chargement complet**, virtualisées (55 cartes montées pour 246 chargées).
- Le défilement charge **24 annonces par cran**, jusqu'à **486 en 20 défilements** (46 s). Stock annoncé : 4 045.
- **Aucune pagination par URL** : `page=`, `p=`, `start=`, `offset=` rendent la même page, en curl *et* dans le navigateur.
- Compteur du site : `[data-qa-selector="filters-car-counter"]`.
- Extraction qui marche : crochets `data-qa-selector` + défilement (le JSON-LD plafonne à 12 et ne porte ni année, ni km, ni carburant).

**Le blocage** : à travers le side-car, `harvest` reçoit **401** sur `https://www.autohero.com/fr/search/?sort=published_at_desc`, alors qu'une sonde Camoufox isolée sur la même adresse rend 29 cartes. Le travail est dans `git stash` (`stash@{0}`, message « lot3 autohero ») — **non commité, non perdu**.

Pistes, dans l'ordre : comparer en-têtes et cookies entre la sonde isolée et `sources/sidecar.py` ; vérifier si le side-car suit la redirection `&_abe=1` ; vérifier si le 401 vient de la navigation ou d'une requête déclenchée par l'extraction.

## Limites connues — pas des bugs

- **LaCentrale ne donne ni vendeur ni code postal** (0/231 mesuré) : pas de filtre par distance, jamais.
- **LaCentrale ne filtre ni carburant ni boîte** : si `criteria` en porte un, la requête est **refusée** en nommant la source, plutôt que de filtrer à moitié.
- **AutoScout24 plafonne à 400 annonces** (20 pages) par requête. Au-delà, découper la recherche.
- **Les deux sites nomment les gammes différemment** (`500/595/695` contre `500`) : deux lignes, jamais fusionnées — l'outil ne devine pas qu'elles désignent la même voiture.
- **Pas de veille de prix, pas de contournement de captcha, pas de calcul de frais.** Décidé, pas oublié.

## Fait : une récolte profonde se découpe en appels courts

Le défaut mesuré (2026-11-28) : `harvest(sources=["lacentrale"], pages_max=20,
budget_s=300)` puis `pages_max=10, budget_s=100` expiraient **tous les deux**
côté client (`Request timed out`) sans rien écrire en base, et le seul jeton
rendu était le `cursor` d'`offers` — qui pagine des annonces déjà lues et ne
reprend pas la récolte. Le message de couverture conseillait en plus « relance
avec `pages_max=20` », c'est-à-dire une action qui échoue.

Corrigé :

a. `harvest(criteria, budget_s, resume)` rend `resumes`, une liste de jetons
   `{source, market, token}` (vide quand tout est lu). Un jeton porte UNE source
   et la page suivante (`resume.py`), et `resume=<jeton>` reprend cette source
   seule, dans le même `job_id` ; la couverture cumule alors le total depuis le
   début du job.
b. L'avertissement cite le jeton (`relance avec resume="…" pour élargir`),
   jamais `pages_max=20`. Quand il n'y a plus de page à demander, il le dit au
   lieu de proposer un levier qui ne lit rien de plus.
c. Le side-car accepte `start_page` : LaCentrale reprend à sa page sans relire
   (ni repayer) les précédentes.

**Contrainte de pas, mesurée après coup** : une reprise relance le navigateur
(~20 s) puis lit ~8 s par page. `pages_max=2` puis `resume` avec `pages_max=6` a
pris **68 s** — au-delà de 3-4 pages par reprise, l'appel approche le délai du
client MCP (~60 s) et expire. Pour lire les 524 annonces LaCentrale d'une
recherche : plusieurs reprises de 3 pages, jamais une de 20.

## Petits chantiers repérés mais non faits

1. **`analyze_models` compte les doublons**, `offers.market_n` non : deux chiffres pour la même idée selon l'outil. À aligner (probablement dédupliquer côté analyse aussi).
2. **`list_brands` rend 301 marques d'un coup** (~20 Ko) : lourd pour un contexte de modèle. Un filtre par préfixe ou une limite serait plus propre.
3. **Rien ne purge le corpus** : il grossit à chaque récolte et les médianes portent sur tout l'historique. Une rétention (ou un `harvest` qui remplace) serait à décider.
4. **LaCentrale peut se défendre demain.** L'outil dira alors « défi anti-bot » — il ne dira jamais « 0 annonce ». Rien à coder, juste à savoir.

## Pièges de l'environnement

- **Le guard bloque les commandes contenant `.key`** (il y voit un fichier de clé privée) : `Listing.key()` en commentaire déclenche un faux positif. Passer par l'outil d'édition.
- **Une image reconstruite ne change pas un conteneur déjà lancé** : recycler avant de tester, sinon on valide l'ancien code. Arrivé deux fois.
- **L'image de test fige `tests/` au build** : après avoir touché un test, la commande par montage du dépôt fait foi.

## Commandes

```sh
cd ~/Documents/perso/projet-perso/mcp-car-compare

# la suite (377 tests, ~7 s)
docker run --rm -v "$PWD":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPYCACHEPREFIX=/tmp/pyc python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"

# reconstruire l'image et repartir propre
docker build -t mcp-car-compare:latest .
docker ps -q --filter ancestor=mcp-car-compare:latest | xargs -r docker rm -f

# reprendre le travail Autohero
git stash list && git stash pop

# reprendre la session
pi -c
```
