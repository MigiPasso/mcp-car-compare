# TODO

État au 2026-11-17. À lire en premier si la session reprend.

## Ce qui marche

Serveur MCP stdio, **4 outils**, **2 sources**, 406 tests, image Docker 5 Go (le
navigateur est dedans). CI GitHub qui rejoue la suite à chaque push. Branché dans
pi et dans Claude Desktop.

| Outil | Ce qu'il fait |
|---|---|
| `list_brands(source, market)` | les marques du site (~0,5 s) |
| `harvest(criteria, budget_s, resume)` | récolte **toutes les sources servies** par défaut, reprenable par jeton |
| `offers(job_id, cursor, limit)` | pages de 25, avec écart au marché et doublons signalés |
| `analyze_models(criteria, limit)` | médiane, quartiles, effectifs ; `n` = annonces, `n_distinct` = voitures |

## Ce qui reste ouvert

### 1. LaCentrale s'arrête à 441 annonces sur 524 — diagnostic arrêté

Mesuré le 2026-11-28, dans cet ordre :

1. Sonde du navigateur **en passant d'abord par la page 1** : pages 21 (24
   annonces), 22 (23), 23 (19), 24+ vides. Le site a donc bien **23 pages**.
2. Une page rechargée deux fois : la 22 a rendu 23 annonces, puis **0** —
   intermittent, et une page vide coûte **25 s** d'attente.
3. Chaîne `harvest` + `resume` : progresse jusqu'à **441/524** puis n'avance
   plus (pages_max 21, 24, 27 rendent tous 441, en 60–120 s).
4. Side-car interrogé directement sur `start_page` 20, 22, 23 : **409 défi
   anti-robot**, après ~76 s.

Lecture retenue : **attaquer une page profonde à froid se fait challenger** ; les
sondes qui marchaient passaient d'abord par la page 1. Et la campagne de mesure
elle-même (une quinzaine de récoltes en une heure) a peut-être déclenché un
durcissement — le mur n'est donc pas forcément structurel.

Un correctif « deux pages vides consécutives » a été **essayé puis annulé** : il
coûtait deux pages sondées et 10 s d'attente sans lire une annonce de plus (441
avant comme après).

Pour reprendre :
1. Ne rien mesurer sur cette source pendant 24–48 h, puis rejouer la chaîne une
   fois. Si 441 revient, c'est structurel.
2. Si c'est structurel, le seul levier que la mesure désigne est de **réchauffer
   la session** : naviguer la page 1, jeter ses annonces, puis attaquer
   `start_page`. Une navigation, pas un mécanisme.
3. Ne pas réintroduire la sonde des deux pages vides sans preuve qu'elle lit plus.

**Contrainte de pas** : une reprise relance le navigateur (~20 s) puis lit ~8 s
par page. `pages_max=6` a pris **68 s** — au-delà de 3-4 pages par reprise, l'appel
frise le délai du client (~60 s) et expire. Pour 524 annonces : plusieurs reprises
de 3 pages, jamais une de 20.

### 2. `list_brands` rend 301 marques d'un coup

~20 Ko dans le contexte d'un modèle, pour en choisir une. Un filtre par préfixe,
ou une limite, serait plus propre.

### 3. ~~Rien ne purge le corpus~~ — FAIT autrement (2026-11-17)

Décision : **pas de purge**, qui détruit et n'apprend rien à l'appelant. On a rendu
l'âge **visible** à la place : chaque annonce porte `last_seen_at` (rafraîchie à
chaque relecture), et `offers` comme `analyze_models` rendent un bloc `corpus_age`
à côté de `coverage`. Au-delà de sept jours, `analyze_models` ajoute un
avertissement en jours — et se tait en deçà.

Limite écrite dans les docstrings : `corpus_age` à `null`/`null` veut dire
« aucune annonce datée » (corpus vide **ou** base migrée dont les annonces
précèdent la colonne), et dans ce second cas aucun avertissement de fraîcheur ne
peut parler — c'est `coverage.listings_distinct` qui sépare les deux cas.

Reste possible plus tard, non fait : décider d'une rétention. Ce n'est plus un
mensonge, seulement un choix de volume.

### 4. La source navigateur peut se défendre demain

L'outil dira alors « défi anti-bot » — jamais « 0 annonce ». Rien à coder, juste à
savoir.

## Abandonné : Autohero (3ᵉ source)

Décision de l'opérateur : on ne la fait pas. C'était la plus petite des trois et la
seule bloquée (401 à travers le side-car, alors qu'une sonde isolée rendait ses
cartes). Le code écrit pour elle a été perdu au nettoyage de l'historique, et
personne ne le regrette. Mesure conservée : 12 annonces en HTML brut, 29 après
chargement, 24 par défilement jusqu'à 486, aucune pagination par URL.

## Limites connues — pas des bugs

- **LaCentrale ne donne ni vendeur ni code postal** (0/231) : pas de filtre par distance.
- **LaCentrale ne filtre ni carburant ni boîte** : la demande est **refusée** en nommant la source, plutôt que filtrée à moitié.
- **AutoScout24 plafonne à 400 annonces** (20 pages) par requête : au-delà, découper la recherche.
- **Les deux sources nomment les gammes différemment** : deux lignes, jamais fusionnées — l'outil ne devine pas.
- **GPL n'est pas dans les tables de carburant** : une bicarburation GPL ressort « inconnu », donc côté essence chez AutoScout24.
- **Pas de veille de prix, pas de contournement de captcha, pas de calcul de frais.** Décidé, pas oublié.

## Pièges de l'environnement

- **Le guard bloque les commandes contenant `.key`** (il y voit une clé privée) : `Listing.key()` en commentaire déclenche un faux positif. Passer par l'outil d'édition.
- **Une image reconstruite ne change pas un conteneur déjà lancé** : recycler avant de tester, sinon on valide l'ancien code. Arrivé trois fois.
- **Un client qui tourne garde les descriptions d'outils en mémoire** : après un changement de description, relancer Claude ou pi.
- **L'image de test fige `tests/` au build** : après avoir touché un test, la commande par montage du dépôt fait foi.

## Commandes

```sh
cd ~/Documents/perso/projet-perso/mcp-car-compare

# la suite (406 tests, ~8 s)
docker run --rm -v "$PWD":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPYCACHEPREFIX=/tmp/pyc python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"

# reconstruire l'image et repartir propre
docker build -t mcp-car-compare:latest .
docker ps -q --filter ancestor=mcp-car-compare:latest | xargs -r docker rm -f

# voir la CI
gh run list --limit 3

# reprendre la session
pi -c
```
