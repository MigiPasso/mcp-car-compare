"""Les quatre outils MCP du périmètre v1 : marques, récolte bornée, annonces,
analyse par modèle.

Chaque outil fait une chose et dit ce qu'elle coûte :

- `list_brands` coûte **un GET** (~0,5 s) et ne porte pas de `coverage` : il n'y
  a pas de récolte à couvrir (PLAN.md §5) ;
- `harvest` est borné **par source** et ne dépasse jamais son budget ; une
  source coupée est dite `capped`, jamais présentée comme couverte ;
- `offers` lit la base seule (aucun réseau) et décrit le **corpus** (annonces
  distinctes), jamais la somme des récoltes ;
- `analyze_models` lit la base seule, agrège par modèle et **s'abstient** sous
  six prix crédibles au lieu d'estimer un prix qu'aucune annonce ne soutient.

Ce module est le seul propriétaire du client HTTP : un `httpx.AsyncClient` par
récolte, partagé par toutes ses sources et fermé quoi qu'il arrive. Un client
par source ferait N pools pour un seul job, et fuirait dès qu'une source lève.
Rien n'est créé pour `offers` ni pour `analyze_models`, qui ne parlent qu'à
SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import cursor as cursor_module
from . import resume as resume_module
from .config import DEFAULT_DB, Config, load_config
from .cursor import Cursor, CursorError
from .models import (
    FUELS,
    GEARS,
    MARKETS,
    MAX_BUDGET_S,
    MAX_PAGES,
    MIN_COMPARABLES,
    PRICE_FLOOR_EUR,
    SERVED_SOURCES,
    DEFAULT_SORT,
    price_is_plausible,
    SOURCE_STATUSES,
    SOURCES,
    SORTS,
    Criteria,
    Listing,
    Source,
    SourceCoverage,
    SourceError,
    StructureChanged,
)
from .store import ListingFilter, MarketGap, Store

# Un site de petites annonces répond en ~0,5 s ; trente secondes est déjà une
# panne. Le budget de `harvest` borne de toute façon l'attente page par page :
# ce délai explicite évite seulement de dépendre du défaut de `httpx`.
HTTP_TIMEOUT_S = 30.0

# Une page plus grande ne sert personne : le canal MCP transporte du JSON, et
# 100 annonces font déjà ~30 Ko.
MAX_LIMIT = 100

# Le périmètre des sources n'est pas réécrit ici : `models.SERVED_SOURCES` en
# est la seule déclaration, et ce module la lit.
SourceFactory = Callable[[str], Source]

_CRITERIA_KEYS = frozenset(
    {
        "make",
        "model",
        "year_min",
        "year_max",
        "km_max",
        "price_min",
        "price_max",
        "fuel",
        "gear",
        "markets",
        "sources",
        "pages_max",
        "sort",
    }
)
_INT_KEYS = ("year_min", "year_max", "km_max", "price_min", "price_max")

# `sort` et `pages_max` gouvernent une RÉCOLTE : ils n'ont aucun effet sur une
# analyse du corpus déjà écrit. Les accepter en silence serait exactement ce que
# le refus des critères inconnus interdit — un critère qu'on croit avoir
# appliqué. La liste est dérivée du contrat plutôt que recopiée : un critère
# ajouté demain à `Criteria` est refusé ici tant que personne n'a décidé qu'il
# filtre une analyse.
_HARVEST_ONLY_KEYS = ("pages_max", "sort")
_ANALYSIS_KEYS = _CRITERIA_KEYS.difference(_HARVEST_ONLY_KEYS)


class UnsupportedSource(LookupError):
    """La source demandée n'a pas d'adaptateur dans ce lot."""


def register(
    mcp,
    *,
    db_path: str | Path | None = None,
    source_factory: SourceFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Déclare les quatre outils sur `mcp`.

    `db_path`, `source_factory` et `clock` existent pour les tests : la source
    est injectée (aucun test ne touche le réseau) et l'horloge aussi (aucune
    assertion ne dépend d'une durée réelle). Sans `source_factory`, c'est
    l'adaptateur réel qui est construit avec le client de la récolte.

    Ce qui n'est pas passé en argument vient de `load_config()`, relu **dans
    chaque corps d'outil** : une valeur figée à l'import rendrait les tests
    dépendants de l'ordre d'exécution. `offers` fait exception et ne lit que
    `MCC_DB` : il n'a ni page ni budget à régler, donc une variable de récolte
    hors bornes ne doit pas l'empêcher de lire un corpus déjà écrit.
    """
    injected = source_factory

    def make_source(
        client: httpx.AsyncClient | None, market: str = "fr"
    ) -> SourceFactory:
        """La fabrique de sources d'un appel.

        `market` ne sert qu'aux outils qui interrogent une source sans marché
        dans leurs arguments — `list_brands`. Les identifiants de marque sont
        propres à chaque marketplace, donc laisser `.fr` en dur rendait les
        marques de `.de` impossibles à demander.
        """
        if injected is not None:
            return injected
        # `client` n'est None que dans ce cas : la fabrique injectée n'en veut pas.
        return lambda name: _default_source(name, client, market)  # type: ignore[arg-type]

    @mcp.tool()
    async def list_brands(source: str = "autoscout24", market: str = "fr") -> dict:
        """Marques du site pour UN marché : un GET (~0,5 s), aucune récolte,
        aucun `coverage`. **Ne lève pas** : un site en échec rend `error` et
        `brands` vide — et une source hors périmètre du lot aussi, avec un
        message qui la nomme et nomme celles servies.

        Ce n'est pas la contradiction que ça paraît avec `harvest`, qui refuse
        une source hors périmètre au parsage : là-bas, une ligne de `coverage`
        en erreur ferait lire « cette source a échoué ». Ici il n'y a pas de
        `coverage`, et le message dit exactement ce qui manque.

        `market` n'est pas décoratif : les libellés de marque sont rendus dans la
        langue du marché, et c'est le libellé — pas l'identifiant `value` — que
        `harvest` attend dans `criteria.make`.
        """
        if source not in SOURCES:
            raise ValueError(f"source inconnue : {source!r} (attendu : {', '.join(SOURCES)})")
        if market not in MARKETS:
            raise ValueError(f"marché inconnu : {market!r} (attendu : {', '.join(MARKETS)})")
        async with _client(injected is None) as client:
            return await _brands(source, market, make_source(client, market))

    @mcp.tool()
    async def harvest(
        criteria: dict, budget_s: int | None = None, resume: str | None = None
    ) -> dict:
        """Récolte bornée : `budget_s` secondes **par source**, jamais dépassé
        (sans valeur : celui de la configuration, 60 s). Rend `{job_id,
        coverage, cursor, warnings, resumes}` ; `cursor` est le curseur d'entrée
        de `offers`, et `resumes` les jetons de reprise des sources qui ne sont
        pas allées au bout.

        **Une récolte profonde se découpe en appels courts.** Mesuré le
        2026-11-28 : `pages_max=20, budget_s=300` sur LaCentrale (~8 s/page,
        navigateur) expirait côté client sans rien écrire, et le `cursor` rendu
        ne reprenait pas la récolte — il pagine des annonces déjà lues. Chaque
        source arrêtée en cours de lecture rend donc un **jeton de reprise**
        dans `resumes` : `[{source, market, token}]`, vide quand tout est lu.
        Deux sources peuvent s'arrêter ensemble, d'où une LISTE et non un jeton
        unique.

        `resume=<jeton>` reprend **cette source seule**, à sa page suivante,
        dans le MÊME `job_id` : la couverture d'alors décrit le total depuis le
        début du job (les compteurs s'accumulent), et le budget de l'appel
        s'applique à la reprise — un jeton ne se refuse donc pas parce que le
        budget a changé. Les critères, eux, doivent être les mêmes que ceux de
        la récolte d'origine (un jeton d'autres critères est refusé, en le
        nommant), et une récolte inconnue est refusée aussi. `pages_max` reste
        le plafond de pages du job : une fois ce plafond atteint, il n'y a plus
        de jeton, et `warnings` le dit au lieu de conseiller un réglage qui ne
        lit rien de plus.

        **`criteria.make` et `criteria.model` sont des LIBELLÉS**, ceux que le
        site affiche et qu'`offers` écrit dans `make` et `group_label` :
        `model` accepte donc `"Série 3"` comme `"serie 3"` (le `group`
        normalisé). Le site filtre la marque et la gamme par le chemin de
        l'adresse (`/lst/bmw/serie-3`), et chaque segment se dérive du libellé :
        il n'y a **aucun identifiant à deviner**, et `list_brands` ne rend plus
        de vocabulaire à part.

        Deux refus à connaître avant d'appeler : un `model` sans `make` est
        refusé (l'adresse n'existerait pas), et un libellé que le site ne
        connaît pas rend `status="error"` avec un 404 nommant le segment
        demandé — jamais zéro annonce, qui se lirait comme un marché vide.
        """
        config = load_config()
        crit = _parse_criteria(criteria, pages_max=config.pages_max)
        budget = _parse_budget(budget_s, default=config.budget_s)
        # L'empreinte des critères SEULS : c'est elle que porte un jeton de
        # reprise, parce que le budget d'une reprise est celui de l'appel qui
        # reprend (voir `cursor.criteria_fingerprint`).
        fingerprint = cursor_module.criteria_fingerprint(crit)
        position: resume_module.ResumeToken | None = None
        if resume is None:
            job_id = uuid.uuid4().hex
            criteria_hash = cursor_module.hash_criteria(crit, budget)
        else:
            position = resume_module.decode(resume)
            resume_module.check(position, criteria=crit, criteria_hash=fingerprint)
            job_id = position.job_id
        with Store(_corpus_path(db_path, config)) as store:
            if position is not None:
                # `Store.job` lève `UnknownJobError` : un jeton forgé ne crée pas
                # une récolte fantôme. Et c'est l'empreinte DÉJÀ ÉCRITE qui fait
                # foi — une reprise continue le job, elle ne lui donne pas une
                # nouvelle identité, sans quoi deux curseurs d'`offers` du même
                # job ne s'accorderaient plus.
                criteria_hash = store.job(job_id).criteria_hash
            async with _client(injected is None) as client:
                harvested = await _harvest_sources(
                    store,
                    job_id,
                    crit,
                    budget=budget,
                    criteria_hash=criteria_hash,
                    make_source=make_source(client),
                    clock=clock,
                    resume=position,
                )
        coverage = [item.coverage for item in harvested]
        tokens = {
            (item.coverage.source, item.coverage.market): resume_module.encode(
                resume_module.ResumeToken(
                    job_id=job_id,
                    criteria_hash=fingerprint,
                    source=item.coverage.source,
                    market=item.coverage.market,
                    next_page=item.next_page,
                )
            )
            for item in harvested
            if item.next_page is not None
        }
        return {
            "job_id": job_id,
            "coverage": [row.as_dict() for row in coverage],
            # Une lecture incomplète doit SAUTER AUX YEUX, pas se découvrir en
            # lisant `coverage` ligne par ligne : un appelant qui ne voit que
            # `job_id` croit avoir tout le marché. Le message dit combien il
            # manque ET quel levier tourner — sans quoi « incomplet » s'entend
            # comme une fatalité.
            "warnings": _truncation_warnings(coverage, tokens, crit.pages_max),
            "cursor": cursor_module.encode(
                Cursor(
                    job_id=job_id,
                    offset=0,
                    sort=crit.sort,
                    criteria_hash=criteria_hash,
                )
            ),
            "resumes": [
                {"source": source, "market": market, "token": token}
                for (source, market), token in tokens.items()
            ],
        }

    @mcp.tool()
    def offers(job_id: str, cursor: str | None = None, limit: int = 25) -> dict:
        """Page d'annonces du corpus d'une récolte (25 par défaut, 100 au plus).
        `coverage` décrit le corpus (annonces distinctes, récoltes, et par source
        l'état de sa récolte), jamais la somme des récoltes.

        Limite résiduelle assumée : l'offset désigne une position dans un tri
        total, pas un instantané. Un `INSERT OR REPLACE` (une annonce revue entre
        deux pages, avec un prix à jour) peut déplacer une annonce d'un rang et
        faire sauter ou répéter une ligne à la frontière de page. Les doublons
        restent visibles à l'appelant, qui peut dédoublonner sur
        `(source, market, source_id)`.

        Chaque annonce porte aussi son écart au marché, en quatre clés plates :
        `market_median` (prix médian de ses comparables, `null` sans
        comparable), `market_n` (nombre de comparables réellement utilisés),
        `gap_eur` (`price - market_median`) et `gap_pct` (le même écart en pour
        cent, arrondi à 0,1). Les comparables sont les annonces du corpus de
        même `(make, group)` normalisé, de la même année à ±1 an, à prix
        crédible (`models.PRICE_FLOOR_EUR`), **l'annonce elle-même exclue** :
        sinon elle tirerait sa propre référence vers elle. `market_n` compte des
        ANNONCES COMPARABLES, une même voiture comptée une fois : une voiture
        annoncée deux fois n'entre qu'une fois dans la série, sinon un doublon
        pèserait double. Ce n'est pas « des voitures distinctes » — deux
        voitures réellement distinctes qui partagent modèle, année, kilométrage
        et prix se rabattent sur une, faute de donnée qui les sépare.

        Deux moitiés indépendantes, et c'est voulu : `market_median` et
        `market_n` décrivent LE MARCHÉ et existent que l'annonce soit chiffrable
        ou non ; `gap_eur` et `gap_pct` décrivent CETTE ANNONCE face au marché,
        et restent `null` quand son prix n'en est pas un (`price_plausible`) —
        un prix à 1 € donnerait « −99,9 % », un chiffre calculé et faux. Sous
        `models.MIN_COMPARABLES` comparables, les trois valeurs s'abstiennent et
        `market_n` dit le compte exact : jamais d'estimation.

        Un écart n'est pas une marge : aucun frais n'est calculé (TVA, import,
        carte grise), les montants restent ceux du site. Et rien n'est stocké —
        la référence est recalculée à chaque appel, parce qu'elle change dès que
        le corpus grandit ; un chiffre figé la veille mentirait le lendemain.

        Chaque annonce porte enfin `duplicate_of` : la `source_id` de l'autre
        annonce du corpus qui décrit la MÊME voiture, `null` quand elle est
        seule. Deux identifiants pour une même voiture, c'est mesuré (deux
        annonces AutoScout24 à 15 900 €, 45 109 km, même vendeur, le
        2026-11-28). **On ne fusionne pas** : les deux lignes restent, indexées
        par leur propre `source_id`, et `duplicate_of` les relie — un couple se
        cite mutuellement, donc une page qui n'en montre qu'une dit quand même
        qu'une autre annonce existe ailleurs.

        La clé est `(make, group)` normalisés + année + kilométrage + prix
        exacts (`store._duplicate_key`), et sa limite est assumée : deux vendeurs
        différents qui annoncent deux voitures réellement identiques ressortent
        marqués l'un vers l'autre, donc deux opportunités restent deux lignes et
        c'est à l'appelant de trancher. Une annonce dont l'année, le kilométrage
        ou le prix manque n'est jamais marquée : une absence n'est pas une
        valeur égale. Rien n'est stocké ici non plus — le marquage est recalculé
        à chaque appel, comme la référence de marché.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit doit être un entier de 1 à {MAX_LIMIT}")
        with Store(_read_only_corpus_path(db_path)) as store:
            job = store.job(job_id)
            corpus = store.corpus(job_id)
            offset = 0
            if cursor is not None:
                position = cursor_module.decode(cursor)
                cursor_module.check(
                    position,
                    job_id=job_id,
                    sort=job.sort,
                    criteria_hash=job.criteria_hash,
                )
                offset = position.offset
                # Le curseur est opaque, pas scellé : une valeur forgée n'est
                # arrêtée que par ce que le corpus peut porter. Un offset
                # négatif est déjà refusé au décodage (`cursor.decode`), donc
                # la garde est ici une ceinture, pas la barrière — la vraie
                # borne que seul cet endroit connaît est la taille du corpus :
                # au-delà, la page rendue serait vide et se lirait comme « la
                # fin de la récolte ».
                if offset < 0:  # ceinture : `decode` refuse déjà
                    raise CursorError(
                        f"curseur hors du corpus : offset {offset} négatif pour la "
                        f"récolte {job_id!r}"
                    )
                if offset > corpus.listings_distinct:
                    raise CursorError(
                        f"curseur hors du corpus : offset {offset} pour "
                        f"{corpus.listings_distinct} annonce(s) dans la récolte {job_id!r}"
                    )
            items, has_more = store.page(
                job_id, sort=job.sort, offset=offset, limit=limit
            )
            references = store.market_gaps(items)
            duplicates = store.duplicate_marks(items)
            following = None
            if has_more:
                following = cursor_module.encode(
                    Cursor(
                        job_id=job_id,
                        offset=offset + len(items),
                        sort=job.sort,
                        criteria_hash=job.criteria_hash,
                    )
                )
            return {
                "items": [
                    {
                        **item.as_dict(),
                        "price_plausible": price_is_plausible(item.price),
                        **_market_gap(item, references[item.key()]),
                        "duplicate_of": duplicates[item.key()],
                    }
                    for item in items
                ],
                "next_cursor": following,
                # `has_more` est dérivable (`following is not None`) et un
                # conseiller a proposé de le couper comme doublon. Il reste :
                # les deux valeurs sont calculées sur la même ligne, donc elles
                # ne peuvent pas diverger, et `has_more` est ce qu'un appelant
                # lit juste — un test de nullité est exactement ce qu'un modèle
                # rate. Ce n'est pas la classe de défaut des surfaces tenues à
                # la main en deux endroits.
                "has_more": has_more,
                "coverage": corpus.as_dict(),
            }

    @mcp.tool()
    def analyze_models(criteria: dict, limit: int = 30) -> dict:
        """Agrégats par modèle sur le corpus déjà récolté (30 groupes par défaut,
        100 au plus) : `{criteria, groups, groups_total, coverage,
        warnings}`.

        **Le groupe est `(make, group)`**, pas le libellé : `group` est la clé de
        gamme que l'adaptateur normalise entre marchés (« Série 3 » et « 3er » se
        rejoignent sur `serie 3`), donc deux libellés de la même gamme font un
        groupe. Aucune cohorte implicite par année ou par kilométrage : c'est à
        `criteria` de la définir, et le `criteria` renvoyé dit exactement ce qui
        a été filtré.

        **`criteria.make` et `criteria.model` sont des LIBELLÉS ici**, comme
        partout dans ce serveur : `model` vaut pour `group_label` comme pour le
        `group` normalisé, sans tenir compte de la casse ni des accents. L'analyse peut
        donc reprendre tels quels les critères d'une récolte. Un filtre qui ne
        retient aucune annonce est NOMMÉ (dans `warnings`) avec les valeurs que
        le corpus porte pour le même champ, plutôt que de rendre un zéro muet
        qu'on lirait comme « ce modèle n'existe pas ». `sort` et `pages_max`
        sont refusés : ils gouvernent une récolte, pas une analyse.

        **Seuls les prix crédibles portent les quantiles** (`models.PRICE_FLOOR_EUR`) :
        une annonce à 1 € dit « prix non communiqué » chez AutoScout24, elle
        reste une annonce. `n` compte toutes les annonces du groupe, `n_priced`
        celles qui portent un prix crédible, et l'écart est visible.

        **Sous six prix crédibles, l'outil s'abstient**
        (`models.MIN_COMPARABLES`) : le groupe apparaît quand même, avec
        `price_median`, `price_p25` et `price_p75` à `null`, et un `warning` qui
        cite son nombre de comparables. Jamais d'estimation, jamais un groupe
        caché.

        **Quantiles par interpolation linéaire** sur la série triée, au rang
        `(n - 1) * q` : pour huit prix triés, `p25` est au rang 1,75 —
        `x[1] + 0,75 * (x[2] - x[1])` — et la médiane est la moyenne des 4e et 5e.
        C'est la définition unique de « médiane » dans ce serveur, et elle vaut
        aussi pour `year_median` et `km_median` (moyenne des deux valeurs du
        milieu quand l'effectif est pair).

        **Aucun classement.** Les groupes sortent par `n` décroissant, puis
        `make` et `group` alphabétiques : un ordre neutre, pas un score. Aucun
        « deal », aucune marge, aucun frais — l'outil rend des distributions, et
        le choix reste à l'appelant (PLAN.md §10).

        **Périmètre : tout le corpus correspondant aux critères**, pas une
        récolte : sinon l'analyse d'un marché dépendrait de l'ordre des appels.
        `coverage` décrit le corpus ENTIER, non filtré (annonces distinctes,
        récoltes, et par `(source, market)` l'état de sa dernière récolte
        écrite) ; `criteria` dit à part ce qui a filtré. `groups_total` compte
        tous les groupes correspondants, pas seulement ceux que `limit` rend :
        c'est la différence entre « 30 groupes » et « 30 sur 87 ».
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit doit être un entier de 1 à {MAX_LIMIT}")
        listing_filter = _parse_analysis_criteria(criteria)
        with Store(_read_only_corpus_path(db_path)) as store:
            groups, groups_total = store.model_groups(
                listing_filter, price_floor=PRICE_FLOOR_EUR, limit=limit
            )
            series = store.group_prices(
                listing_filter,
                [(group.make_key, group.group_key) for group in groups],
                price_floor=PRICE_FLOOR_EUR,
            )
            unmatched = store.unmatched_filters(listing_filter)
            coverage = store.whole_corpus()
        rendered: list[dict] = []
        warnings: list[str] = []
        for miss in unmatched:
            present = (
                ", ".join(miss.present) if miss.present else "aucune valeur pour ce champ"
            )
            warnings.append(
                f"{miss.key}={miss.value!r} : aucune annonce ne correspond ; "
                f"valeurs présentes dans le corpus : {present}"
            )
        for group in groups:
            prices = series[(group.make_key, group.group_key)]
            median = quartile_low = quartile_high = None
            if group.n_priced < MIN_COMPARABLES:
                warnings.append(
                    f"{group.group_label} : {group.n_priced} prix crédible(s) "
                    f"(< {MIN_COMPARABLES}) — aucune médiane"
                )
            else:
                median = _quantile(prices, 0.5)
                quartile_low = _quantile(prices, 0.25)
                quartile_high = _quantile(prices, 0.75)
            rendered.append(
                {
                    "make": group.make,
                    "group": group.group,
                    "group_label": group.group_label,
                    "n": group.n,
                    "n_priced": group.n_priced,
                    "price_median": median,
                    "price_p25": quartile_low,
                    "price_p75": quartile_high,
                    "year_median": group.year_median,
                    "km_median": group.km_median,
                }
            )
        if not groups_total and not unmatched:
            # Aucun filtre fautif n'explique le vide : c'est le corpus, ou une
            # combinaison sans intersection. Un zéro sans phrase se lirait comme
            # « aucun modèle n'existe ».
            warnings.append(
                "aucun groupe ne correspond à ces critères : le corpus est vide, "
                "ou les critères ne se recoupent pas"
            )
        return {
            "criteria": listing_filter.as_dict(),
            "groups": rendered,
            "groups_total": groups_total,
            "coverage": coverage.as_dict(),
            "warnings": warnings,
        }


def _truncation_warnings(
    coverage: list[SourceCoverage],
    tokens: dict[tuple[str, str], str],
    pages_max: int,
) -> list[str]:
    """Une phrase par source qui s'est arrêtée avant la fin, avec le levier.

    Rendue vide quand tout a été lu : c'est ce qui permet à un test de dire
    « pas d'avertissement sur une récolte complète » et donc de réfuter un
    message qui crierait au loup à chaque appel.

    Le levier est le **jeton de reprise** de cette source, jamais un
    `pages_max` plus grand : mesuré le 2026-11-28, « relance avec `pages_max=20` »
    conseillait un appel qui expirait côté client (`Request timed out`), sans
    rien écrire en base. Une consigne qui échoue est pire que pas de consigne —
    c'est le défaut que ce lot corrige. Quand il n'y a plus de page à demander
    (plafond `pages_max` du job, ou stock épuisé), le message le dit au lieu de
    proposer un jeton qui ne mènerait à rien.
    """
    warnings: list[str] = []
    for row in coverage:
        if not row.truncated:
            continue
        total = row.announced_total
        if total is not None and total <= row.unique:
            continue
        lues = f"{row.unique} annonce(s) lue(s)"
        annonce = f" sur {total} annoncée(s)" if total is not None else " (total annoncé inconnu)"
        token = tokens.get((row.source, row.market))
        if token is not None:
            suite = f'relance avec resume="{token}" pour élargir'
        else:
            suite = (
                f"aucune page de plus à demander dans cette récolte "
                f"(plafond pages_max={pages_max} atteint, ou arrêt de la source) "
                f": une lecture plus large demande des critères plus étroits"
            )
        warnings.append(f"{row.source} ({row.market}) : {lues}{annonce} — {suite}")
    return warnings


# --- réseau -------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(needed: bool) -> AsyncIterator[httpx.AsyncClient | None]:
    """Le client d'un appel : redirections refusées, délai explicite.

    `httpx` suit les redirections par défaut ; ici un 302 vers un autre marché
    serait analysé avec le marché demandé, donc on ne le suit pas. Aucun client
    n'est créé quand une fabrique est injectée : les tests n'en veulent pas.
    """
    if not needed:
        yield None
        return
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=HTTP_TIMEOUT_S
    ) as client:
        yield client


def _default_source(
    source: str, client: httpx.AsyncClient, market: str = "fr"
) -> Source:
    """Adaptateur réel, importé ici seulement : les tests injectent une source
    factice et n'ont pas besoin du module d'adaptateur.

    `market` est celui de `list_brands` : `search()` prend le sien dans les
    critères, et une même instance sert toutes les paires `(source, market)`.
    Sauf `lacentrale`, qui est française uniquement : son constructeur refuse un
    marché autre que `fr` (`sources/lacentrale.py`).

    Le périmètre n'est pas une liste écrite ici : `models.SERVED_SOURCES` le
    porte une seule fois, et c'est la même que ce module cite quand il refuse un
    job (`_parse_criteria`).
    """
    if source == "autoscout24":
        from .sources.autoscout24 import Autoscout24

        return Autoscout24(client, market)
    if source == "lacentrale":
        from .sources.lacentrale import Lacentrale

        # Le client de la récolte porte AUSSI la requête au side-car navigateur :
        # une seule fabrique, un seul pool, et les tests n'ont qu'un transport
        # factice à fournir pour les deux transports.
        return Lacentrale(client, market)
    raise UnsupportedSource(
        f"source {source!r} hors périmètre v1 : aucun adaptateur dans ce lot "
        f"(servie(s) : {', '.join(SERVED_SOURCES)})"
    )


async def _brands(source: str, market: str, make_source: SourceFactory) -> dict:
    """Catalogue des marques d'un marché. Un site en échec est un résultat, pas
    une exception.

    `brands` vide sans `error` voudrait dire « ce site ne vend aucune marque » :
    les deux cas ne doivent pas se ressembler. `market` est rendu parce que les
    libellés qu'on vient de lire sont ceux de cette marketplace : `harvest` les
    attend tels quels, et c'est d'eux que se dérive le segment d'adresse.
    """
    try:
        adapter = make_source(source)
        brands = list(await adapter.brands())
    except (StructureChanged, SourceError, UnsupportedSource) as exc:
        return {"source": source, "market": market, "brands": [], "error": _failure(exc)}
    return {"source": source, "market": market, "brands": brands, "error": ""}


# --- récolte ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Harvested:
    """Ce qu'un appel a rendu pour une paire `(source, market)`, et la page où
    reprendre.

    `next_page` vaut `None` quand il n'y a plus de page À DEMANDER pour cette
    source dans ce job : la boucle s'est arrêtée sur le plafond `pages_max`, le
    site n'annonçait plus rien, ou une page vide a montré que le stock est
    épuisé. C'est cette valeur — et elle seule — qui décide s'il y a un jeton de
    reprise à rendre : un jeton qui ne mènerait à aucune page serait la consigne
    qui échoue que ce lot corrige (`RESTE-A-FAIRE.md` point 0).

    Une source qui s'arrête sur le budget, sur une attente trop longue ou sur un
    échec de page laisse au contraire la page qu'elle n'a pas lue : c'est là
    qu'une reprise repart.
    """

    coverage: SourceCoverage
    next_page: int | None


async def _harvest_sources(
    store: Store,
    job_id: str,
    criteria: Criteria,
    *,
    budget: int,
    criteria_hash: str,
    make_source: SourceFactory,
    clock: Callable[[], float],
    resume: resume_module.ResumeToken | None = None,
) -> list[_Harvested]:
    """Récolte chaque `(source, market)`, avec un budget **par source**.

    `resume` restreint la récolte à la paire que le jeton désigne, à partir de
    sa page : une reprise lit la SUITE d'une source, jamais le début des autres,
    et c'est ce qui rend un jeton attribuable à une seule d'entre elles.
    """
    harvested: list[_Harvested] = []
    start_page = resume.next_page if resume is not None else 1
    for source_name in criteria.sources:
        started = clock()
        try:
            adapter: Source | None = make_source(source_name)
            refusal = ""
            refusal_status = "error"
        except Exception as exc:  # source sans adaptateur : dite, pas ignorée
            adapter = None
            refusal = _failure(exc)
            refusal_status = _status_of(exc)
        for market in criteria.markets:
            if resume is not None and (source_name, market) != (resume.source, resume.market):
                continue
            if adapter is None:
                harvested.append(
                    _record_failed_run(
                        store,
                        job_id,
                        source_name,
                        market,
                        status=refusal_status,
                        reason=refusal,
                        criteria_hash=criteria_hash,
                        sort=criteria.sort,
                    )
                )
                continue
            harvested.append(
                await _harvest_one(
                    store,
                    job_id,
                    adapter,
                    _for_market(criteria, source_name, market),
                    source_name=source_name,
                    market=market,
                    budget_s=budget,
                    started=started,
                    clock=clock,
                    criteria_hash=criteria_hash,
                    start_page=start_page,
                )
            )
    return harvested


async def _harvest_one(
    store: Store,
    job_id: str,
    adapter: Source,
    criteria: Criteria,
    *,
    source_name: str,
    market: str,
    budget_s: int,
    started: float,
    clock: Callable[[], float],
    criteria_hash: str,
    start_page: int = 1,
) -> _Harvested:
    """Récolte une paire `(source, market)` de `start_page` jusqu'à épuisement,
    `pages_max` ou budget. Ce qui a été lu est persisté page par page : une
    coupure ne perd pas les pages déjà payées.

    **La couverture d'un job s'accumule.** `write_run` REMPLACE la ligne de la
    paire — c'est ce qui garde une seule ligne par `(job_id, source, market)`, et
    donc une seule identité par paire dans `corpus()` et `whole_corpus()` — donc
    les compteurs déjà écrits sont relus ici et repris, jamais remis à zéro :
    une reprise qui relit deux pages d'un job qui en a déjà lu six annonce
    huit pages de couverture, pas deux. Le choix est délibéré : ajouter une ligne
    par appel aurait changé la clé primaire de `runs`, et avec elle le sens de
    `corpus()` (« une ligne par paire, celle de CETTE récolte »), pour un
    historique dont personne n'a besoin — le total depuis le début du job est
    exactement ce qu'un appelant qui reprend veut lire.

    `read`, `dropped_duplicates`, `dropped_invalid` et `unique` sont ces
    compteurs cumulés ; `status`, `truncated` et `reason` décrivent l'état APRÈS
    cet appel (une reprise qui finit la lecture rend `truncated=False`, sans quoi
    la source ne se déclarerait jamais couverte). `announced_total` est un
    instantané du site, pas un compteur : il n'est pas additionné, et la valeur
    déjà connue est gardée quand le site se tait dans l'appel qui reprend.

    Comptabilité tenue : `read` additionne ce que les PAGES ont rendu, doublons
    internes compris (`page.rendered`) — c'est un chiffre de LECTURE, pas de
    couverture ; `unique` ne compte que les annonces identifiables qui restent.
    D'où `read == unique + dropped_duplicates + dropped_invalid`. La complétude,
    elle, se juge sur `unique` : seul un compteur d'annonces distinctes est
    comparable au total annoncé par le site (voir la sortie de boucle).

    `truncated` est posé dès qu'on s'arrête sur un plafond — budget, `pages_max`,
    ou plafond du site (AutoScout24 rend une page vide après 400 annonces). Ce
    plafond ne coïncide pas toujours avec un total annoncé plus grand : quand le
    site ne dit pas combien il en annonce (`announced_total is None`), on ne sait
    pas s'il restait des annonces, et la récolte se déclare `truncated` quand
    même — « je me suis arrêté sans savoir » ne doit jamais se lire comme « j'ai
    tout lu ». Une récolte de 400 annonces sur un marché qui en annonce 12 479 ne
    doit jamais se lire comme complète.

    `has_more` d'une page est la seconde façon de s'arrêter avant la fin, et la
    seule qu'une source puisse déclarer elle-même : un adaptateur qui lit
    plusieurs pages du site en une requête sait qu'il a coupé, et le dit par
    `Page.has_more` (voir `models.Page`).

    La page où reprendre est rendue avec la couverture (`_Harvested`) : c'est
    elle qui devient le jeton de reprise, et elle vaut `None` quand il n'y a plus
    de page à demander.
    """
    # Ce que le job a DÉJÀ écrit pour cette paire : les compteurs se cumulent et
    # les annonces déjà vues ne sont pas des nouveautés. Sur un job neuf, les
    # deux lectures sont vides — ce n'est pas une branche réservée aux reprises,
    # c'est le même code qui répond aux deux cas.
    prior = store.run(job_id, source_name, market)
    seen: set[tuple[str, str, str]] = {
        (source_name, market, source_id)
        for source_id in store.run_listing_ids(job_id, source_name, market)
    }
    read = prior.read if prior is not None else 0
    dropped = prior.dropped_duplicates if prior is not None else 0
    dropped_invalid = prior.dropped_invalid if prior is not None else 0
    announced_total: int | None = prior.announced_total if prior is not None else None
    # `has_more` s'ACCUMULE sur les pages déjà lues : la page 2 d'un lot rend
    # une page vide et sans suite (`has_more=False`), et l'écraser effacerait
    # l'aveu de troncature fait par la page 1 — le seul qui compte.
    has_more = False
    status = "ok"
    truncated = False
    reason = ""
    # La page où une reprise repartirait, ou `None` s'il n'y a plus rien à
    # demander. Renseignée par CHAQUE sortie de boucle, faute de quoi la
    # dernière branche gagnante laisserait la valeur d'une autre.
    next_page: int | None = None
    for page_number in range(max(1, start_page), criteria.pages_max + 1):
        remaining = budget_s - (clock() - started)
        if remaining <= 0:
            status = "capped"
            truncated = True
            reason = f"budget de {budget_s} s atteint après {len(seen)} annonce(s) distincte(s) ({read} lue(s))"
            # La page n'a pas été lue : c'est elle que la reprise reprend.
            next_page = page_number
            break
        try:
            # Le budget borne aussi l'attente en cours : une page qui traîne ne
            # fait pas déborder la récolte.
            async with asyncio.timeout(remaining):
                page = await adapter.search(criteria, page_number)
        except TimeoutError:
            status = "capped"
            truncated = True
            reason = (
                f"budget de {budget_s} s atteint pendant la page {page_number} "
                f"après {len(seen)} annonce(s) distincte(s) ({read} lue(s))"
            )
            next_page = page_number
            break
        except Exception as exc:  # échec de source : dit, jamais 0 annonce muet
            status = _status_of(exc)
            truncated = True
            reason = _failure(exc)
            # Un échec de page est rejouable : la reprise repart de celle qui a
            # échoué. C'est ce qui rend un `challenge` DataDome reprenable plus
            # tard sans reconstruire un job.
            next_page = page_number
            break
        if announced_total is None:
            announced_total = page.announced_total
        has_more = has_more or page.has_more
        if page.rendered <= 0:
            # Page vide : la source est épuisée, OU elle a atteint son propre
            # plafond (AutoScout24 s'arrête à 20 pages, soit 400 annonces, et
            # rend une page vide au-delà). Les annonces DISTINCTES tranchent :
            # si le site en annonce plus qu'on n'en a identifié, l'arrêt est un
            # plafond, pas une fin de marché. `read` ne peut pas trancher ici,
            # il compte aussi les doublons (20 rendues pour 19 uniques sur le
            # banc d'AutoScout24).
            if announced_total is not None and len(seen) < announced_total:
                truncated = True
                # La phrase ne dit plus « plafond du site » : cette branche sert
                # aussi une source qui rend tout son lot d'un coup, et là l'arrêt
                # vient de `pages_max`, pas du site. Mesuré le 2026-11-28 : avec
                # pages_max=3, LaCentrale rendait 71 annonces sur 100 et l'outil
                # annonçait un plafond du site ; avec pages_max=10, elle rendait
                # 100/100. Le chiffre qui manque est nommé, la cause reste
                # inconnue du serveur : le dire vaut mieux que la deviner.
                reason = (
                    f"lecture incomplète : arrêt après {len(seen)} annonce(s) "
                    f"distincte(s) ({read} lue(s)) alors que la source en annonce "
                    f"{announced_total} — plafond de lecture (pages_max="
                    f"{criteria.pages_max}) ou de la source"
                )
            # Une page vide ne dit pas la même chose selon la source. Pour une
            # source PAGE PAR PAGE (AutoScout24), elle signifie « le site
            # s'arrête » : une reprise à la suivante redemanderait une page vide
            # et proposerait le même jeton, une consigne qui ne mène à rien.
            # Pour une source en LOT (LaCentrale), elle signifie seulement « le
            # lot demandé est épuisé » — et `has_more` le dit. Là, il reste des
            # pages à demander : la reprise repart au-delà du plafond, avec un
            # plafond plus grand (que `criteria_fingerprint` n'invalide plus).
            if has_more:
                next_page = criteria.pages_max + 1
            break
        read += page.rendered
        # Les doublons DANS la page ont déjà été retirés par l'adaptateur : ils
        # ne sont donc plus dans `page.listings` mais ils sont dans `rendered`.
        # Les compter ici est ce qui rend `read` honnête.
        dropped += page.rendered - len(page.listings)
        keep: list[Listing] = []
        for listing in page.listings:
            if not listing.source_id:
                # Sans identifiant, une annonce n'est ni dédoublonnable ni
                # citable : elle ne peut pas entrer dans le corpus sous la clé
                # `(source, market, '')` où elle en écraserait une autre. Elle
                # est écartée, et comptée — un écartement muet serait une
                # perte de couverture invisible.
                dropped_invalid += 1
                continue
            if listing.key() in seen:
                dropped += 1
            else:
                seen.add(listing.key())
                keep.append(listing)
        store.add_page(job_id, keep)
        # La complétude se juge sur les annonces DISTINCTES. `read` compte ce
        # que les pages ont rendu, doublons compris : il n'est donc pas
        # comparable au total annoncé par le site, qui compte des annonces. Le
        # banc d'AutoScout24 rend 20 annonces pour 19 identifiants uniques, donc
        # `read == announced_total` avec une annonce jamais lue : comparer
        # `read` cassait la boucle une annonce trop tôt, en déclarant
        # `truncated=False`.
        if announced_total is not None and len(seen) >= announced_total:
            break
    else:
        truncated = True
        reason = (
            f"plafond atteint : pages_max={criteria.pages_max} après "
            f"{len(seen)} annonce(s) distincte(s) ({read} lue(s))"
        )
        if announced_total is None:
            # Plafond atteint sans total annoncé : le `reason` dit lequel des
            # deux chiffres manque, sinon il laisserait croire à une fin de
            # marché.
            reason += ", total annoncé inconnu"
        else:
            reason += f" alors que le site en annonce {announced_total}"
    # Une source qui déclare elle-même s'être arrêtée avant la fin ne peut pas se
    # lire comme couverte. Sans cette ligne, un lot partiel rendu par LaCentrale
    # (`partial` du side-car, porté par `Page.has_more`) sortait `truncated=False`
    # quand le site n'annonçait aucun total : `status="ok"` sur une lecture
    # partielle, soit exactement la fausse complétude que `coverage` existe pour
    # empêcher. Le `reason` est composé, pas écrasé : un plafond déjà nommé
    # (budget, `pages_max`) reste dit.
    if has_more:
        stopped = (
            f"la source s'est arrêtée avant la fin après {len(seen)} annonce(s) "
            f"distincte(s) ({read} lue(s))"
        )
        truncated = True
        # `reason` porte déjà le plafond quand l'un a coupé : le répéter sous une
        # autre forme donnait « plafond atteint … ; la source s'est arrêtée avant
        # la fin … », deux fois le même fait dans un champ que l'appelant cite.
        reason = reason or stopped
    coverage = SourceCoverage(
        source=source_name,
        market=market,
        status=status,
        announced_total=announced_total,
        read=read,
        unique=len(seen),
        dropped_duplicates=dropped,
        dropped_invalid=dropped_invalid,
        truncated=truncated,
        reason=reason,
    )
    store.write_run(job_id, coverage, criteria_hash=criteria_hash, sort=criteria.sort)
    return _Harvested(coverage=coverage, next_page=next_page)


def _record_failed_run(
    store: Store,
    job_id: str,
    source_name: str,
    market: str,
    *,
    status: str,
    reason: str,
    criteria_hash: str,
    sort: str,
) -> _Harvested:
    """Une récolte qui n'a pas pu démarrer laisse quand même sa ligne : une
    source absente du corpus doit s'y voir, avec zéro.

    Les compteurs DÉJÀ écrits par un appel précédent du même job sont repris, pas
    écrasés : une reprise qui ne peut pas construire son adaptateur ne doit pas
    effacer ce que le job a déjà lu. La page de reprise est la première de la
    source, puisque aucune n'a été demandée.
    """
    prior = store.run(job_id, source_name, market)
    coverage = SourceCoverage(
        source=source_name,
        market=market,
        status=status,
        announced_total=prior.announced_total if prior is not None else None,
        read=prior.read if prior is not None else 0,
        unique=prior.unique if prior is not None else 0,
        dropped_duplicates=prior.dropped_duplicates if prior is not None else 0,
        dropped_invalid=prior.dropped_invalid if prior is not None else 0,
        truncated=True,
        reason=reason,
    )
    store.write_run(job_id, coverage, criteria_hash=criteria_hash, sort=sort)
    return _Harvested(coverage=coverage, next_page=1)


def _status_of(exc: BaseException) -> str:
    """`challenge` quand l'adaptateur le déclare, `error` sinon.

    `SourceError` porte ce statut (`models.SourceError`) : c'est lui qui rend
    `challenge` atteignable, au lieu d'un vocabulaire que rien ne produit.
    """
    declared = getattr(exc, "status", "")
    return declared if declared in SOURCE_STATUSES and declared != "ok" else "error"


def _failure(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


# --- critères -----------------------------------------------------------------


def _corpus_path(configured: str | Path | None, config: Config) -> Path:
    """Le corpus d'un appel : le paramètre d'abord, la configuration ensuite."""
    return Path(configured) if configured is not None else config.db_path


def _read_only_corpus_path(configured: str | Path | None) -> Path:
    """Le corpus d'un outil de LECTURE : `MCC_DB`, et rien d'autre.

    `offers` n'a ni page ni budget à régler, donc il n'a aucune raison de
    dépendre des variables qui les portent : une `MCC_PAGES_MAX` hors bornes
    fait lever `load_config()` — c'est voulu, c'est un plafond de récolte — et
    rendrait sinon un corpus déjà écrit illisible à cause d'une variable qui ne
    concerne aucune page lue. Le chemin de la base est tout ce que la lecture
    demande.
    """
    if configured is not None:
        return Path(configured)
    return Path(os.environ.get("MCC_DB", str(DEFAULT_DB)))


def _for_market(criteria: Criteria, source_name: str, market: str) -> Criteria:
    """Une requête par marché : `coverage` porte une ligne par `(source,
    market)`, donc chaque appel ne doit rendre qu'un marché — sinon les
    compteurs ne peuvent plus être attribués."""
    return Criteria(
        make=criteria.make,
        model=criteria.model,
        year_min=criteria.year_min,
        year_max=criteria.year_max,
        km_max=criteria.km_max,
        price_min=criteria.price_min,
        price_max=criteria.price_max,
        fuel=criteria.fuel,
        gear=criteria.gear,
        markets=(market,),
        sources=(source_name,),
        pages_max=criteria.pages_max,
        sort=criteria.sort,
    )


def _parse_criteria(raw: dict, *, pages_max: int) -> Criteria:
    """Traduit les critères reçus en `Criteria`, en refusant ce qui est faux.

    Un critère inconnu est une faute de frappe, pas un filtre à ignorer : il est
    refusé pour que l'appelant ne croie pas avoir filtré.
    """
    if not isinstance(raw, dict):
        raise ValueError("criteria doit être un objet")
    unknown = sorted(set(raw) - _CRITERIA_KEYS)
    if unknown:
        raise ValueError(f"critère(s) inconnu(s) : {', '.join(unknown)}")
    sort = raw.get("sort", DEFAULT_SORT)
    if sort not in SORTS:
        raise ValueError(f"tri inconnu : {sort!r} (attendu : {', '.join(SORTS)})")
    fuel = raw.get("fuel", "")
    if fuel not in FUELS:
        raise ValueError(f"carburant inconnu : {fuel!r} (attendu : {', '.join(FUELS)})")
    gear = raw.get("gear", "")
    if gear not in GEARS:
        raise ValueError(f"boîte inconnue : {gear!r} (attendu : {', '.join(GEARS)})")
    markets = _vocabulary(raw.get("markets", MARKETS[:1]), MARKETS, "marché")
    sources = _vocabulary(raw.get("sources", SOURCES[:1]), SOURCES, "source")
    # Une source du vocabulaire sans adaptateur est refusée ICI, avant la
    # première requête : l'accepter pour rendre un `status="error"` plus loin
    # ferait passer un périmètre pour une panne du site, et l'appelant
    # chercherait la panne du mauvais côté.
    unserved = [name for name in sources if name not in SERVED_SOURCES]
    if unserved:
        raise ValueError(
            f"source(s) hors périmètre de ce lot : {', '.join(unserved)} "
            f"(servie(s) : {', '.join(SERVED_SOURCES)})"
        )
    # LaCentrale ne filtre ni le carburant ni la boîte : ses paramètres de requête
    # ne sont pas validés (mesuré au lab, voir ICH). Les accepter puis ne rien
    # filtrer donnerait des compteurs de couverture faux — la récolte compterait
    # comme lues des annonces qui ne correspondent pas — et filtrer après lecture
    # serait pire : `read` compterait des annonces jetées. Le refus tombe donc au
    # parsage, en nommant la source et le critère fautif.
    if "lacentrale" in sources:
        for key, value, label in (
            ("fuel", fuel, "le carburant"),
            ("gear", gear, "la boîte"),
        ):
            if value:
                raise ValueError(
                    f"source lacentrale : elle ne filtre pas {label} "
                    f"({key}={value!r}) — retire ce critère ou interroge "
                    f"AutoScout24 seul"
                )
    # `pages_max` des critères prime sur celui de la configuration : c'est un
    # plafond par appel, pas un réglage global. Absent, c'est la configuration
    # qui décide ; zéro est refusé plus bas (un plafond nul ne récolte rien, et
    # le lire comme « prends celui de la configuration » serait un défaut
    # silencieux).
    limit = raw.get("pages_max")
    if limit is None:
        limit = pages_max
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"pages_max doit être un entier >= 1 : {limit!r}")
    if limit > MAX_PAGES:
        # Au-delà, AutoScout24 ne rend rien de plus : la récolte boucherait pour
        # rien. La borne est nommée pour que l'appelant sache quoi corriger.
        raise ValueError(
            f"pages_max ne peut pas dépasser {MAX_PAGES} : {limit!r} "
            "(plafond mesuré du site)"
        )
    bounds = {key: _optional_int(raw, key) for key in _INT_KEYS}
    return Criteria(
        make=_text(raw, "make"),
        model=_text(raw, "model"),
        pages_max=limit,
        fuel=fuel,
        gear=gear,
        markets=markets,
        sources=sources,
        sort=sort,
        **bounds,
    )


def _parse_analysis_criteria(raw: dict) -> ListingFilter:
    """Traduit les critères d'une analyse en filtre de corpus.

    Trois différences avec `_parse_criteria`, et chacune refuse de deviner :

    - `sort` et `pages_max` sont refusés : ils n'ont aucun effet sur un corpus
      déjà écrit, et un critère accepté puis inappliqué est un filtre que
      l'appelant croit avoir posé ;
    - `markets` et `sources` n'ont pas de défaut — absents, ils ne filtrent
      rien. Le défaut de `harvest` (`fr`, `autoscout24`) existe parce qu'une
      récolte ne peut interroger qu'une marketplace à la fois ; une analyse qui
      en hériterait cacherait en silence les annonces `.de` du même corpus
      (PLAN.md §5 : l'analyse ne dépend pas de l'ordre des appels) ;
    - une source est validée contre le VOCABULAIRE (`models.SOURCES`), pas
      contre celles que ce lot sait interroger : une analyse ne récolte rien,
      donc une source déjà présente dans le corpus est analysable même si ce
      lot ne sait pas (ou plus) l'interroger. Un corpus qui n'en porte aucune
      rend zéro groupe, et un `warning` nomme le filtre fautif.

    `make` et `model` ne sont pas validés davantage ici qu'à la récolte : ils
    sont des libellés comparés au corpus, et un libellé inconnu n'est pas une
    faute de frappe à refuser mais un filtre à nommer dans la réponse.
    """
    if not isinstance(raw, dict):
        raise ValueError("criteria doit être un objet")
    unknown = sorted(set(raw) - _ANALYSIS_KEYS)
    if unknown:
        harvest_only = [key for key in unknown if key in _HARVEST_ONLY_KEYS]
        if harvest_only:
            raise ValueError(
                f"critère(s) de récolte, sans effet sur une analyse : "
                f"{', '.join(harvest_only)} (analyze_models ne récolte rien)"
            )
        raise ValueError(f"critère(s) inconnu(s) : {', '.join(unknown)}")
    fuel = raw.get("fuel", "")
    if fuel not in FUELS:
        raise ValueError(f"carburant inconnu : {fuel!r} (attendu : {', '.join(FUELS)})")
    gear = raw.get("gear", "")
    if gear not in GEARS:
        raise ValueError(f"boîte inconnue : {gear!r} (attendu : {', '.join(GEARS)})")
    # `None` — et non une valeur par défaut — parce qu'un marché absent ne
    # filtre rien : c'est la différence avec `_parse_criteria`.
    markets = None if "markets" not in raw else _vocabulary(raw["markets"], MARKETS, "marché")
    sources = None if "sources" not in raw else _vocabulary(raw["sources"], SOURCES, "source")
    return ListingFilter(
        make=_text(raw, "make"),
        model=_text(raw, "model"),
        fuel=fuel,
        gear=gear,
        markets=markets,
        sources=sources,
        **{key: _optional_int(raw, key) for key in _INT_KEYS},
    )


def _parse_budget(budget_s: int | None, *, default: int) -> int:
    """Le budget de l'appelant s'il y en a un, celui de la configuration sinon."""
    if budget_s is None:
        return default
    if isinstance(budget_s, bool) or not isinstance(budget_s, int) or budget_s < 1:
        raise ValueError(f"budget_s doit être un entier >= 1 : {budget_s!r}")
    if budget_s > MAX_BUDGET_S:
        raise ValueError(
            f"budget_s ne peut pas dépasser {MAX_BUDGET_S} : {budget_s!r} "
            "(borne du serveur)"
        )
    return budget_s


def _vocabulary(value: object, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{label}s : liste non vide attendue, reçu {value!r}")
    values = tuple(value)
    for item in values:
        if item not in allowed:
            raise ValueError(
                f"{label} hors vocabulaire : {item!r} (attendu : {', '.join(allowed)})"
            )
    return tuple(dict.fromkeys(values))  # un doublon ferait deux récoltes


def _text(raw: dict, key: str) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} doit être une chaîne : {value!r}")
    return value.strip()


def _optional_int(raw: dict, key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} doit être un entier ou nul : {value!r}")
    return value


# --- écart au marché d'une offre ------------------------------------------------


def _market_gap(listing: Listing, reference: MarketGap) -> dict:
    """Les quatre clés d'écart au marché d'une annonce.

    `market_median` et `market_n` décrivent le marché : ils existent que
    l'annonce soit chiffrable ou non. `gap_eur` et `gap_pct` décrivent cette
    annonce face au marché, et il n'y a pas d'écart à partir d'un prix qui n'en
    est pas un — `price_is_plausible` en décide, jamais une copie du plancher
    écrite ici.

    Sous `MIN_COMPARABLES` comparables, les trois valeurs s'abstiennent et
    `market_n` reste exact : une médiane sur cinq annonces se lirait comme un
    prix de marché. Aucun frais n'est calculé : un écart n'est pas une marge.

    `gap_pct` est dérivé des deux montants rendus (`gap_eur` et
    `market_median`), pas d'un intermédiaire non arrondi : les deux chiffres ne
    peuvent donc pas se contredire.
    """
    if reference.n < MIN_COMPARABLES:
        return {
            "market_median": None,
            "market_n": reference.n,
            "gap_eur": None,
            "gap_pct": None,
        }
    median = reference.median
    price = listing.price
    gap_eur: int | None = None
    gap_pct: float | None = None
    # `median` n'est `None` que si la cohorte est vide, donc déjà écartée par
    # le seuil ci-dessus : la garde sert au typage, pas à un cas réel.
    if price is not None and median is not None and price_is_plausible(price):
        gap_eur = price - median
        gap_pct = round(gap_eur / median * 100, 1)
    return {
        "market_median": median,
        "market_n": reference.n,
        "gap_eur": gap_eur,
        "gap_pct": gap_pct,
    }


# --- quantiles d'une analyse ---------------------------------------------------


def _quantile(sorted_values: list[int], q: float) -> int | float | None:
    """Quantile d'une série TRIÉE, par interpolation linéaire au rang (n-1)*q.

    C'est la définition UNIQUE de « médiane » et de « quartile » dans ce serveur.
    Sans elle, un quartile veut dire deux choses selon qui le calcule : la valeur
    du rang bas, la moyenne des deux rangs voisins, ou un arrondi. Ici, sur huit
    prix triés (n = 8) :

    - `p25` est au rang 7 * 0,25 = 1,75, donc `x[1] + 0,75 * (x[2] - x[1])` ;
    - la médiane est au rang 7 * 0,5 = 3,5, donc la moyenne de `x[3]` et `x[4]` ;
    - `p75` est au rang 7 * 0,75 = 5,25, donc `x[5] + 0,25 * (x[6] - x[5])`.

    La valeur est rendue telle quelle, entière quand elle tombe juste : arrondir
    inventerait une précision que la série n'a pas, et une médiane d'année sur un
    effectif pair vaut légitimement 2020,5.
    """
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    value = sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)
    return int(value) if float(value).is_integer() else value
