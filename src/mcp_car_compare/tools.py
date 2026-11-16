"""Les trois outils MCP du périmètre v1 : marques, récolte bornée, annonces.

Chaque outil fait une chose et dit ce qu'elle coûte :

- `list_brands` coûte **un GET** (~0,5 s) et ne porte pas de `coverage` : il n'y
  a pas de récolte à couvrir (PLAN.md §5) ;
- `harvest` est borné **par source** et ne dépasse jamais son budget ; une
  source coupée est dite `capped`, jamais présentée comme couverte ;
- `offers` lit la base seule (aucun réseau) et décrit le **corpus** (annonces
  distinctes), jamais la somme des récoltes.

Ce module est le seul propriétaire du client HTTP : un `httpx.AsyncClient` par
récolte, partagé par toutes ses sources et fermé quoi qu'il arrive. Un client
par source ferait N pools pour un seul job, et fuirait dès qu'une source lève.
Rien n'est créé pour `offers`, qui ne parle qu'à SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from . import cursor as cursor_module
from .config import Config, load_config
from .cursor import Cursor
from .models import (
    FUELS,
    GEARS,
    MARKETS,
    SOURCE_STATUSES,
    SOURCES,
    SORTS,
    Criteria,
    Source,
    SourceCoverage,
    SourceError,
    StructureChanged,
)
from .store import Store

# Un site de petites annonces répond en ~0,5 s ; trente secondes est déjà une
# panne. Le budget de `harvest` borne de toute façon l'attente page par page :
# ce délai explicite évite seulement de dépendre du défaut de `httpx`.
HTTP_TIMEOUT_S = 30.0

# Une page plus grande ne sert personne : le canal MCP transporte du JSON, et
# 100 annonces font déjà ~30 Ko.
MAX_LIMIT = 100

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


class UnsupportedSource(LookupError):
    """La source demandée n'a pas d'adaptateur dans ce lot."""


def register(
    mcp,
    *,
    db_path: str | Path | None = None,
    source_factory: SourceFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Déclare les trois outils sur `mcp`.

    `db_path`, `source_factory` et `clock` existent pour les tests : la source
    est injectée (aucun test ne touche le réseau) et l'horloge aussi (aucune
    assertion ne dépend d'une durée réelle). Sans `source_factory`, c'est
    l'adaptateur réel qui est construit avec le client de la récolte.

    Ce qui n'est pas passé en argument vient de `load_config()`, relu **dans
    chaque corps d'outil** : une valeur figée à l'import rendrait les tests
    dépendants de l'ordre d'exécution.
    """
    injected = source_factory

    def make_source(client: httpx.AsyncClient | None) -> SourceFactory:
        if injected is not None:
            return injected
        # `client` n'est None que dans ce cas : la fabrique injectée n'en veut pas.
        return lambda name: _default_source(name, client)  # type: ignore[arg-type]

    @mcp.tool()
    async def list_brands(source: str = "autoscout24") -> dict:
        """Marques et modèles du site : un GET (~0,5 s), aucune récolte, aucun
        `coverage`. Ne lève pas : un site en échec rend `error` et `brands` vide."""
        if source not in SOURCES:
            raise ValueError(f"source inconnue : {source!r} (attendu : {', '.join(SOURCES)})")
        async with _client(injected is None) as client:
            return await _brands(source, make_source(client))

    @mcp.tool()
    async def harvest(criteria: dict, budget_s: int | None = None) -> dict:
        """Récolte bornée : `budget_s` secondes **par source**, jamais dépassé
        (sans valeur : celui de la configuration, 60 s). Rend `{job_id,
        coverage, cursor}` ; `cursor` est le curseur d'entrée de `offers`. Une
        source coupée porte `status="capped"` et `read=0` si rien n'a été lu."""
        config = load_config()
        crit = _parse_criteria(criteria, pages_max=config.pages_max)
        budget = _parse_budget(budget_s, default=config.budget_s)
        job_id = uuid.uuid4().hex
        criteria_hash = cursor_module.hash_criteria(crit, budget)
        with Store(_corpus_path(db_path, config)) as store:
            async with _client(injected is None) as client:
                coverage = await _harvest_sources(
                    store,
                    job_id,
                    crit,
                    budget=budget,
                    criteria_hash=criteria_hash,
                    make_source=make_source(client),
                    clock=clock,
                )
        return {
            "job_id": job_id,
            "coverage": [row.as_dict() for row in coverage],
            "cursor": cursor_module.encode(
                Cursor(
                    job_id=job_id,
                    offset=0,
                    sort=crit.sort,
                    criteria_hash=criteria_hash,
                )
            ),
        }

    @mcp.tool()
    def offers(job_id: str, cursor: str | None = None, limit: int = 25) -> dict:
        """Page d'annonces du corpus d'une récolte (25 par défaut, 100 au plus).
        `coverage` décrit le corpus (annonces distinctes, récoltes, par source),
        jamais la somme des récoltes."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit doit être un entier de 1 à {MAX_LIMIT}")
        config = load_config()
        with Store(_corpus_path(db_path, config)) as store:
            job = store.job(job_id)
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
            items, has_more = store.page(
                job_id, sort=job.sort, offset=offset, limit=limit
            )
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
                "items": [item.as_dict() for item in items],
                "next_cursor": following,
                "has_more": has_more,
                "coverage": store.corpus(job_id).as_dict(),
            }


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


def _default_source(source: str, client: httpx.AsyncClient) -> Source:
    """Adaptateur réel, importé ici seulement : les tests injectent une source
    factice et n'ont pas besoin du module d'adaptateur."""
    if source == "autoscout24":
        from .sources.autoscout24 import Autoscout24

        return Autoscout24(client)
    raise UnsupportedSource(
        f"source {source!r} hors périmètre v1 : seule autoscout24 est servie"
    )


async def _brands(source: str, make_source: SourceFactory) -> dict:
    """Catalogue des marques. Un site en échec est un résultat, pas une exception.

    `brands` vide sans `error` voudrait dire « ce site ne vend aucune marque » :
    les deux cas ne doivent pas se ressembler.
    """
    try:
        adapter = make_source(source)
        brands = list(await adapter.brands())
    except (StructureChanged, SourceError, UnsupportedSource) as exc:
        return {"source": source, "brands": [], "error": _failure(exc)}
    return {"source": source, "brands": brands, "error": ""}


# --- récolte ------------------------------------------------------------------


async def _harvest_sources(
    store: Store,
    job_id: str,
    criteria: Criteria,
    *,
    budget: int,
    criteria_hash: str,
    make_source: SourceFactory,
    clock: Callable[[], float],
) -> list[SourceCoverage]:
    """Récolte chaque `(source, market)`, avec un budget **par source**."""
    coverage: list[SourceCoverage] = []
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
            if adapter is None:
                coverage.append(
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
            coverage.append(
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
                )
            )
    return coverage


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
) -> SourceCoverage:
    """Récolte une paire `(source, market)` jusqu'à épuisement, `pages_max` ou
    budget. Ce qui a été lu est persisté page par page : une coupure ne perd
    pas les pages déjà payées."""
    seen: set[tuple[str, str, str]] = set()
    read = 0
    dropped = 0
    announced_total: int | None = None
    status = "ok"
    truncated = False
    reason = ""
    for page_number in range(1, criteria.pages_max + 1):
        remaining = budget_s - (clock() - started)
        if remaining <= 0:
            status = "capped"
            truncated = True
            reason = f"budget de {budget_s} s atteint après {read} annonce(s)"
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
                f"après {read} annonce(s)"
            )
            break
        except Exception as exc:  # échec de source : dit, jamais 0 annonce muet
            status = _status_of(exc)
            truncated = True
            reason = _failure(exc)
            break
        if announced_total is None:
            announced_total = page.announced_total
        if not page.listings:
            break  # page vide : la source est épuisée
        read += len(page.listings)
        for listing in page.listings:
            if listing.key() in seen:
                dropped += 1
            else:
                seen.add(listing.key())
        store.add_page(job_id, page.listings)
        if announced_total is not None and read >= announced_total:
            break
    else:
        truncated = True
        reason = f"pages_max={criteria.pages_max} atteint après {read} annonce(s)"
    coverage = SourceCoverage(
        source=source_name,
        market=market,
        status=status,
        announced_total=announced_total,
        read=read,
        unique=len(seen),
        dropped_duplicates=dropped,
        truncated=truncated,
        reason=reason,
    )
    store.write_run(job_id, coverage, criteria_hash=criteria_hash, sort=criteria.sort)
    return coverage


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
) -> SourceCoverage:
    """Une récolte qui n'a pas pu démarrer laisse quand même sa ligne : une
    source absente du corpus doit s'y voir, avec zéro."""
    coverage = SourceCoverage(
        source=source_name,
        market=market,
        status=status,
        read=0,
        unique=0,
        dropped_duplicates=0,
        truncated=True,
        reason=reason,
    )
    store.write_run(job_id, coverage, criteria_hash=criteria_hash, sort=sort)
    return coverage


def _status_of(exc: BaseException) -> str:
    """`challenge` quand l'adaptateur le déclare, `error` sinon (models.Source
    ne distingue que `StructureChanged` et `SourceError`)."""
    declared = getattr(exc, "status", "")
    return declared if declared in SOURCE_STATUSES and declared != "ok" else "error"


def _failure(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


# --- critères -----------------------------------------------------------------


def _corpus_path(configured: str | Path | None, config: Config) -> Path:
    """Le corpus d'un appel : le paramètre d'abord, la configuration ensuite."""
    return Path(configured) if configured is not None else config.db_path


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
    sort = raw.get("sort", "price_asc")
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
    # `pages_max` des critères prime sur celui de la configuration : c'est un
    # plafond par appel, pas un réglage global. Absent ou nul, c'est la
    # configuration qui décide.
    limit = raw.get("pages_max")
    if limit is None:
        limit = pages_max
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"pages_max doit être un entier >= 1 : {limit!r}")
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


def _parse_budget(budget_s: int | None, *, default: int) -> int:
    """Le budget de l'appelant s'il y en a un, celui de la configuration sinon."""
    if budget_s is None:
        return default
    if isinstance(budget_s, bool) or not isinstance(budget_s, int) or budget_s < 1:
        raise ValueError(f"budget_s doit être un entier >= 1 : {budget_s!r}")
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
