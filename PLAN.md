# mcp-car-compare — plan

Serveur MCP local (stdio) qui récolte des annonces automobiles sur trois sources
et les expose à une IA pour choisir un modèle à acheter.

Statut : proposition, non implémentée. Chaque étape porte sa preuve ; une étape
sans preuve n'est pas une étape.

---

## 1. Décisions déjà prises (et par qui)

| Décision | Choix | Motif |
|---|---|---|
| Client | pi, Claude — MCP **stdio** | ChatGPT abandonné (exigerait HTTPS public + OAuth) |
| Langage | **Python 3.12**, FastMCP, httpx | Camoufox est Python ; le Go n'achète rien, 28–37 ms/annonce est du réseau |
| Déploiement | **une image Docker**, config par variables d'env | règle maison : tout tourne dans Docker |
| Sources | autoscout24.fr, autoscout24.de, autohero.com/fr, lacentrale.fr | |
| Veille de prix | **non** — ni scheduler, ni crawl planifié | demandé |
| Surface | **4 outils par fonction**, pas par source | arbitré au council : la dédup est une propriété du corpus |
| Base | SQLite, un fichier, volume Docker | curseur de reprise + corpus réutilisable entre appels |

Repris de l'ancien outil `ICH` (à portée de main sur disque) : les tables de
libellés, les fixtures HTML capturées, le side-car Camoufox.

---

## 2. Faits mesurés sur lesquels le plan repose

| Fait | Valeur | Source |
|---|---|---|
| AS24 `.fr`/`.de` page `/lst` : JSON `__NEXT_DATA__` dans le HTML | 200, 686 Ko, 0,54 s | sonde du jour, Docker |
| AS24 débit | 20 annonces/page, **20 pages max = 400/requête**, 28–37 ms/annonce | banc ICH 12/11 |
| AS24 robots | 0 défi sur la campagne | banc ICH 12/11 |
| Autohero URL vivante | `www.autohero.com/fr/search/` → 302 `&_abe=1` → 200, 900 Ko SSR ; `www.autohero.fr` **ne répond pas** (DNS OK, :443 fermé) | sonde du jour |
| Autohero débit navigateur | 806 ms/annonce en Chromium à scroller | banc ICH 12/11 |
| LaCentrale | DataDome 403 ; Camoufox lit 24 cartes en ~6,5 s ; Firefox piloté par Playwright échoue (`navigator.webdriver=true`) | recon ICH 12/11 |
| LaCentrale débit | 331–334 ms/annonce, ~8 s/page, 24/page | banc ICH 12/11 |
| Fixtures disponibles | 4 pages réelles capturées (36 Ko) + ~80 cas de test tabulés | `ICH/market/autoscout/testdata/` |
| Piège mesuré | la lib Camoufox écrit sa barre de progression **sur stdout** | `ICH/docs/recon/lacentrale-firefox-vs-camoufox.md` |
| Coût LaCentrale | lancement navigateur ~30 s, résolution DataDome 6–75 s (payés une fois), puis 0,5–3 s/page ; **60 s de délai mini entre deux lancements** ; garde 150 s | `ICH/lacentrale/server.py:77-92` |
| Profil navigateur | Camoufox **neuf** à chaque récolte, aucun profil persistant | `ICH/lacentrale/server.py:601` |

Le dernier point est décisif : en MCP stdio, **stdout est le canal du protocole**.
Une barre de progression y corrompt le flux JSON-RPC. D'où la section 3.

---

## 3. Architecture

```
┌─ conteneur ──────────────────────────────────────────────┐
│  entrypoint.sh                                            │
│   ├─ uvicorn 127.0.0.1:8899  (side-car Camoufox)          │
│   └─ exec python -m mcp_car_compare  → stdio JSON-RPC     │
│         httpx (AS24, Autohero)   httpx → 127.0.0.1:8899   │
└──────────────┬───────────────────────────────┬───────────┘
           volumes:/data (SQLite)     /profile (profil navigateur)
```

> **Correction après revue** : ICH ne garde aucun profil persistant — il lance
> un Camoufox neuf à chaque récolte (`async with AsyncCamoufox(...)`, aucun
> `user_data_dir` dans `lacentrale/server.py`). Le volume `/profile` est donc
> **une hypothèse à valider**, pas une reprise. Voir R-4.

Trois règles qui ne se discutent pas :

1. **stdout est sacré.** Tout log va sur **stderr**. Aucun `print`. Pas de barre
   de progression. Le side-car est un **processus séparé** précisément pour que
   son stdout ne puisse pas atteindre le protocole.
2. **Un seul navigateur à la fois.** Le side-car sérialise (file d'attente + 409
   si un défi DataDome est en cours) : deux contextes Camoufox simultanés sur la
   même IP font monter le risque de blocage sans rien accélérer.
3. **Le side-car n'écoute que sur 127.0.0.1**, jeton en variable d'env. Il n'est
   pas exposé, mais un processus voisin dans le conteneur non plus ne doit pas
   pouvoir le piloter.

On garde le side-car (plutôt que d'importer Camoufox dans le processus MCP)
malgré le coût d'un port et d'un jeton : c'est le prix de la règle 1, et le
protocole HTTP du side-car, lui, existe et est testé dans ICH
(`lacentrale/server.py`, jeton, file sérialisée, 409 sur défi).

Mais la revue a raison sur un point que le plan masquait : un side-car **meurt
silencieusement**. L'entrypoint doit donc porter un `healthcheck` (le port
répond) et un redémarrage, sinon une source disparaît sans que personne ne le
voie. `os.dup2(2, 1)` au démarrage du serveur MCP est une seconde ceinture,
pas un remplacement : il protège aussi les bibliothèques tierces importées.

---

## 4. Modèle de données

```python
Listing = {
  "source": "autoscout24" | "autohero" | "lacentrale",
  "market": "fr" | "de",              # pays où se trouve la voiture
  "source_id": "…",                   # identifiant du site
  "url": "https://…",
  "make": "Renault", "group": "clio", "group_label": "Clio",
  "engine": "1.5 Blue dCi 85", "trim": "sport"|"confort"|"",
  "year": 2021, "km": 48000, "price": 11490, "fuel": "diesel", "gear": "auto",
  "vat_label": "", "seller": "pro"|"particulier"|"",
  "city": "", "postal_code": "", "country": "FR",
  "image": "https://…",
  "vin": None,                        # presque toujours absent
}
```

**Clé d'identité** : `(source, market, source_id)`. Le `market` est dans la clé
parce qu'AutoScout24 .fr et .de sont deux marketplaces et que rien ne garantit
que leurs identifiants numériques ne se recouvrent pas.

**Dédup — deux niveaux, et un seul fait autorité :**

- *intra-source* : sur `(source, market, source_id)`, silencieuse, c'est de la
  correction de lecture (le site se répète entre les pages).
- *inter-sources* : **VIN uniquement**. Sans VIN, on ne fusionne pas : on pose
  `possible_duplicate_of` et l'IA décide, en connaissance de cause.

Pourquoi cette prudence : c'est mesuré, LaCentrale ne donne **ni vendeur ni code
postal** (0/231) et Autohero **ni finition, ni vendeur, ni code postal** (0/10).
Deux Clio identiques chez deux concessionnaires différents sont deux annonces
distinctes ; les fusionner supprimerait la comparaison de prix qui est le but.

Test obligatoire, écrit **avant** le code de dédup : le couple qui ne doit pas
fusionner (même modèle, même année, même km, même prix, vendeurs distincts,
deux sources) reste deux lignes.

---

## 5. Surface d'outils

Quatre outils, par fonction. Chaque réponse porte un bloc `coverage`.

```jsonc
// 1. list_brands(source?, market="fr") -> {source, market, brands:[{value,label}], error?}
//      instantané. Pas de `models` en v1 : la page de taxonomie ne les porte pas,
//      et un champ toujours vide est une promesse creuse (revue du 16/11).
// 2. harvest(criteria, budget_s=60, sources?, resume?) -> {job_id, coverage,
//      cursor, warnings, resumes}
//      criteria = {make, model, year_min, year_max, km_max, price_min,
//                  price_max, fuel, gear, markets, pages_max, sort}
//      `make` et `model` sont des LIBELLÉS du site (« BMW », « Série 3 » ou le
//      `group` « serie 3 ») : la marque et la gamme se filtrent par le CHEMIN
//      (/lst/bmw/serie-3, §8) ; un `model` exige `make`, un 404 est un refus nommé.
//      `resumes` = [{source, market, token}] : les sources qui ne sont pas allées
//      au bout, chacune avec son jeton de reprise (`resume=<token>` reprend
//      cette source seule, à sa page suivante, dans le même `job_id`).
// 3. offers(job_id, cursor?, limit=25) -> {items:[Listing], next_cursor|null,
//      has_more, coverage}
// 4. analyze_models(criteria) -> {groups:[{key,label,n,price_median,
//      price_p25, price_p75,year_median,km_median,sources:{…}}], coverage,
//      warnings}
```

**Contrat de `coverage`** — obligatoire dans `harvest`, `offers` et
`analyze_models` ; **absent de `list_brands`** (aucune récolte) :

```jsonc
// dans harvest : une ligne par source, celle de CETTE récolte
{"source":"autoscout24","market":"fr","status":"ok|challenge|error|capped",
 "announced_total":465,"read":60,"unique":60,"dropped_duplicates":0,
 "truncated":true,"reason":"budget de 60 s atteint"}
// dans offers / analyze_models : le corpus, jamais la somme des récoltes
// (forme réelle, à la racine — il n'y a pas de clé `corpus`)
{"listings_distinct":812,"runs":3,
 "sources":[{"source":"autoscout24","market":"fr","listings_in_corpus":612,
             "status":"ok","announced_total":465,"read":60,"unique":60,
             "truncated":true}]}
```

Les entrées `sources` d'`offers` et d'`analyze_models` portent les **huit mêmes
clés**, `listings_in_corpus` comprise : les annonces distinctes de la paire
`(source, market)` dans tout le corpus, jamais un `job_id`. Ce qu'une récolte a
laissé est déjà porté par `unique`.

**On n'additionne jamais deux récoltes.** `announced_total` est un instantané du
site au moment de la requête et `read` un compteur de récolte ; deux récoltes
qui se recouvrent, additionnées, annoncent une couverture double. Le corpus se
décrit donc en **annonces distinctes**, et les récoltes restent listées à part,
chacune avec ses propres chiffres.

`announced_total` peut être `null` (Autohero et LaCentrale ne l'exposent pas
toujours) : l'absence est dite, pas comblée.

C'est le remède au défaut d'origine : un outil qui se déclare complet à tort est
pire qu'un outil qui échoue.

**`job_id` désigne une récolte, pas le corpus.** `offers(job_id)` lit la récolte
et ses suites ; `analyze_models` prend les **critères** et travaille sur tout le
corpus qui y correspond, récoltes confondues — sinon l'analyse d'un marché
dépendrait de l'ordre des appels.

**`harvest` est borné par source, pas globalement.** `budget_s` s'applique à
chaque source ; celles qui ne rentrent pas rendent `status:"capped"` et un
**jeton de reprise** dans `resumes`. Il ne dépasse jamais le budget. Ce n'est
pas un scheduler, c'est une reprise explicite : la couverture d'une reprise
décrit le total depuis le début du job, jamais le dernier appel.

**LaCentrale ne rentre pas dans un budget court.** Mesuré dans ICH : ~30 s de
lancement de navigateur puis 6–75 s de résolution DataDome avant la première
carte, et 60 s de délai mini entre deux lancements. Un `budget_s=60` la rend
donc **structurellement inatteignable**. Elle n'est demandée que par un job
explicite (`sources:["lacentrale"], budget_s >= 240`), et le défaut de `harvest`
ne l'inclut pas.

**`analyze_models` s'abstient sous 6 comparables** (seuil repris d'ICH) et rend
`price_median: null` + un `warning`, jamais une estimation.

---

## 6. Étapes, avec leur preuve

Chaque étape est indépendante et vérifiable seule. Preuve = commande exécutable
dans Docker, pas une affirmation.

**État au 28/11** — **E6 est livré** (commit `ec70ffd`) : quatrième outil
`analyze_models` (agrégats par `(make, group)`, prix crédibles seulement,
abstention sous 6 comparables), 278 tests, image reconstruite et branchée dans
`~/.pi/agent/mcp.json` avec les quatre outils. **Restent E3 + E4** (Autohero et
LaCentrale, un seul lot : ils partagent le même navigateur) **et E7** (dédup,
inutile avant deux sources).

**Méthode de travail convenue avec l'opérateur** : les décisions
d'implémentation passent par un council à trois conseillers, le superviseur
tranche, et l'opérateur ne voit que la décision. Critère de jugement : *simple
et efficace, pas d'usine à gaz.*

**`council-glm` est exclu du council** (décision de l'opérateur, 2026-11-16) :
sur trois passes de suite il a rendu « rapport prêt » sans rapport structuré,
même avec un budget de lectures explicite. Roster de remplacement :
`council-deepseek` + `reviewer`, et `oracle` (fork) quand il faut un troisième
avis. Tout brief de conseiller ou de vérificateur porte un **budget d'étapes** :
sans ça, l'agent meurt en essayant d'être exhaustif et ne rend rien.

**E1 mesuré le 16/11 — Autohero est niveau 3.** `curl` sur `/fr/search/` rend
**12 annonces** (blocs JSON-LD `Product`, avec prix, `productID`, image) sur
**3 085 annoncées**, et **aucun paramètre de pagination ne change la page**
(`page=2`, `p=2`, `start=12`, `offset=12` → HTML identique au octet près, mêmes
12 produits). Pas de `__NEXT_DATA__`, pas de RSC, aucun chemin `/api/` dans les
bundles, `/graphql` et `/api/search` en 404. Les 3 085 véhicules se chargent
côté client.

**Conséquence de plan** : E3 (Autohero) et E4 (LaCentrale) exigent désormais la
**même** infrastructure de navigateur. Faire Autohero seul n'achète plus de gain
incrémental — les deux sources se font dans le même lot, ou pas du tout.

| # | Étape | Preuve (dans Docker) |
|---|---|---|
| **E0** | Squelette : git, image, entrypoint, serveur MCP stdio qui répond à `list_brands([])` | un client MCP de test (`mcp` python) appelle `list_brands` et reçoit un JSON valide ; **stdout ne contient que du JSON-RPC** (test : zéro octet non-JSON) |
| **E1** | **Expérience Autohero** : `curl -L` sur `/fr/search/?priceMax=…` et compter les annonces réellement présentes dans le HTML | nombre d'annonces extraites. **Résultat ≥ 100 ⇒ niveau 2 (curl). Résultat ~20–24 ⇒ niveau 3 (navigateur)** et E3 bascule sur le side-car. Cette étape décide l'architecture de la source la moins connue |
| **E2** | Parseur AutoScout24 porté en Python, **tables de tests traduites avant le parseur** | (a) les 4 fixtures de `ICH/market/autoscout/testdata/` rejouées en pytest rendent le même nombre d'annonces et les mêmes champs critiques que les tests Go ; (b) **test de rupture** : un HTML sans `__NEXT_DATA__` lève une erreur nommée, il ne rend pas 0 annonce. (a) prouve le portage, (b) seul prouve qu'une casse du site sera vue |
| **E3** | Parseur Autohero (voie décidée en E1) | fixtures capturées en E1, test de non-régression sur les champs mesurés. **Réserve** : le 302 `&_abe=1` est un paramètre d'expérimentation A/B — une fixture achetée sur un bucket ne prouve rien sur l'autre. Le parseur doit donc échouer bruyamment si la structure change, et E3 se re-vérifie à chaque récolte réelle |
| **E4** | LaCentrale via le side-car Camoufox | (a) ≥ 20 cartes obtenues **par le chemin produit** (un job `budget_s=240`), pas par un appel direct au side-car — sinon la preuve ne dit rien du produit ; (b) un défi DataDome rend une erreur explicite (`status:"challenge"`), jamais 0 annonce silencieux ; (c) mesure du démarrage à froid, consignée : c'est elle qui décide si LaCentrale est utilisable ou décorative |
| **E5** | Corpus SQLite + `harvest` borné + `offers` à curseur + `coverage` | un `harvest(budget_s=10)` s'arrête en ≤ 11 s, rend un curseur ; deux `offers` successifs ne rendent **aucun doublon** et couvrent l'ensemble ; `read + dropped == unique + doublons expliqués` |
| **E6** | `analyze_models` | sur un corpus témoin : médiane et quartiles recalculés à la main dans le test ; sous 6 comparables → `null` + warning |
| **E7** | Dédup inter-sources — **marquage, pas fusion** | le test « ne doit pas fusionner » passe (deux Clio identiques, deux vendeurs, deux sources → deux lignes) **et** sur une fixture à VIN identique les deux lignes sont reliées par `possible_duplicate_of`. Le VIN étant quasi absent en pratique, cette étape ne fusionne rien : elle **marque**, et le plan le dit au lieu de le laisser croire |
| **E8** | Empaquetage : config MCP pour pi et Claude, README, `docker compose` | un `docker run` neuf, avec un volume vide, sert `list_brands` puis `harvest` puis `offers` sans configuration manuelle |

Ordre : E0, puis E2 et E5 (le chemin de la v1), puis E8. **E1 quand Autohero
entre au périmètre** — elle peut changer E3, donc elle doit précéder E3 et rien
d'autre. E4 seulement après avoir mesuré son démarrage à froid.

Correction de priorité après revue : le risque le plus cher n'est pas Autohero
(un parseur faux se replie sur un navigateur) mais **DataDome** (un durcissement
supprime la source), et le second est un changement de `__NEXT_DATA__` sur AS24,
qui est la source de la v1. Les deux sont traités par la même règle : échouer
bruyamment plutôt que rendre 0 annonce.

---

## 7. Risques

| Risque | Gravité | Réponse |
|---|---|---|
| Autohero plafonne à ~24 annonces en curl | moyenne | E1 tranche ; repli navigateur |
| DataDome durcit (défi systématique) | **haute** | statut `challenge` explicite, profil persistant, 1 navigateur à la fois ; pas de contournement de captcha |
| AS24 change `__NEXT_DATA__` | haute mais détectable | fixture + test : le parseur échoue bruyamment, il ne rend pas 0 annonce |
| Latence : 4 sources × plusieurs marques = minutes | haute | budget + curseur, jamais un appel long |
| Couverture présentée comme complète | **haute** | bloc `coverage` obligatoire ; test qui vérifie qu'il est présent et non vide |
| L'IA surinterprète (marge, « meilleure affaire ») | moyenne | les `warnings` remontés tels quels ; pas de frais calculés, vocabulaire interdit documenté |
| Autohero servi en A/B (`&_abe=1`) : deux buckets, deux DOM | moyenne | parseur qui échoue bruyamment ; re-capture de fixture à chaque changement |
| Side-car mort silencieusement | moyenne | healthcheck + redémarrage dans l'entrypoint, et `coverage.status="error"` côté produit |
| Profil navigateur persistant jamais éprouvé dans ICH | moyenne | R-4 : c'est un travail de E4, pas une reprise ; à défaut, un Camoufox neuf par job, et le coût est dit dans `coverage` |
| IP résidentielle = point unique de défaillance | basse | assumé, usage perso |

Hors périmètre, explicitement : pas de veille de prix, pas de scheduler, pas de
serveur HTTP public, pas de contournement de captcha, pas de calcul de frais
d'import/TVA/carte grise, pas de redistribution des données.

---

## 8. Décisions tranchées après revue

Trois ambiguïtés qu'un implémenteur aurait résolues de travers :

| Question | Décision |
|---|---|
| Unité des prix | **euros entiers, tel qu'affiché par le site**, jamais recalculé. `vat_label` recopié tel quel, vide si le site se tait. Aucun prix « débattu de TVA », aucun frais |
| Tri par défaut | `sort: "price_asc"` explicite dans les critères. **Changer de tri invalide le curseur** : un curseur porte son tri, et le serveur refuse un curseur repris avec un autre tri plutôt que de rendre des pages incohérentes |
| `budget_s` contre `pages_max` | `budget_s` **gagne** : `pages_max` est un plafond par source, jamais un objectif. `status:"capped"` dit lequel des deux a coupé |

Décisions prises pendant le premier lot, après mesure :

| Question | Décision |
|---|---|
| `truncated` quand le site n'annonce aucun total | `true` — on ne sait pas s'il reste des annonces, et le `reason` dit « plafond atteint, total annoncé inconnu » |
| Marqueur de défi anti-bot | cherché **seulement** sur un statut ≠ 200 ou en l'absence de `__NEXT_DATA__` : une annonce peut citer la chaîne dans sa description sans que la page soit un défi |
| Source hors périmètre dans `criteria.sources` | **refusée au parsage**, en nommant la source et celles servies. Un `status:"error"` se lirait « cette source a échoué », ce qui est faux : elle n'est pas implémentée |
| `list_brands.models` | **retiré** de la charge : jamais rempli en production (301 marques, 0 modèle) |
| Tri par défaut | **`age_desc`**, pas `price_asc` : mesuré le 16/11, AutoScout24 publie des annonces à **1 €** pour « prix non communiqué » (`priceRaw: 1`), et un tri par prix croissant commence donc par ces annonces-là. La première page d'un corpus sans critère était 100 % de la boue |
| Prix crédible | plancher **100 €**, repris d'ICH (mesuré : 228 annonces sous 800 €, prix triés `10, 280, 299, 300, …` — un seul aberrant). La valeur stockée reste celle du site ; `offers` ajoute `price_plausible` pour que l'appelant n'ait pas à le deviner |
| Filtre marque/gamme | **par le CHEMIN de l'adresse**, dérivé du libellé : `/lst/<slug(make)>/<slug(model)>`. Slug = minuscules, accents retirés (NFKD), `&` → `et`, le reste → `-`. Mesuré le 28/11 : `/lst/bmw/serie-3` rend 1 434 annonces toutes « Série 3 », `/lst/renault/clio` 2 501, `/lst/bmw/gamme-inexistante` **404**. `mmmv` (identifiants de MOTORISATION — 316, 320, M3 — et non de gamme) est abandonné : un `model` sans `make` est refusé, et un 404 est un refus nommé, jamais 0 annonce |

## 9. La décision qui reste ouverte

Les deux conseillers recommandent la même chose : **v1 = AutoScout24 seule**
(E0 + E2 + E5 + E8), parce qu'elle est la plus rapide (0,54 s, 20/page), la plus
riche en champs, et qu'elle suffit à « je cherche une Clio et je compare ».
Autohero, LaCentrale, `analyze_models` et la dédup inter-sources viendraient
ensuite, sans rien casser : `source` et `market` sont déjà dans le modèle.

C'est une question de périmètre, donc elle t'appartient : trois sources d'entrée
(un livrable plus long, une source bloquée bloque tout) ou AS24 d'abord (utile
plus tôt, les autres en incréments).

## 10. Ce que ce plan refuse de promettre

- **Une vue exhaustive du marché.** AS24 plafonne à 400 annonces par requête :
  une vue large se construit par requêtes étroites, et `coverage` dira toujours
  la part lue.
- **Un classement « meilleure affaire ».** `analyze_models` rend des
  distributions et des comptes, pas un verdict. Le choix reste à l'IA et à toi.
- **Que les quatre sources se valent.** AS24 est rapide et complet sur ses
  champs ; LaCentrale est lent et muet sur le vendeur ; Autohero est inconnu
  jusqu'à E1.
