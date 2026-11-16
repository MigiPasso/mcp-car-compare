"""Corpus SQLite : les annonces d'un côté, les récoltes de l'autre.

Deux natures de chiffres, jamais mélangées (PLAN.md §5) :

- les **annonces** (`listings`) ont une identité `(source, market, source_id)` ;
  la même annonce vue par dix récoltes reste une ligne, `INSERT OR REPLACE` ;
- les **récoltes** (`runs`) portent les compteurs de `SourceCoverage`, une ligne
  par récolte et par `(source, market)`. Deux récoltes qui se recouvrent,
  additionnées, annoncent une couverture double : elles ne sont jamais sommées.

`run_listings` relie les deux : une annonce vue dans deux récoltes appartient
aux deux. Sans cette table, la seconde récolte effacerait l'annonce de la
première et la couverture d'un job plus ancien rétrécirait en silence.

Le chemin de la base est un paramètre, jamais une constante : les tests ouvrent
une base dans `tmp_path`.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import CorpusCoverage, Listing, SourceCoverage

# BUSY_TIMEOUT_MS est le temps qu'une écriture attend un verrou tenu par une
# autre connexion avant de rendre `database is locked`. Sans lui, un `offers`
# lancé pendant un `harvest` échoue au lieu d'attendre la fin d'une transaction
# de quelques millisecondes.
BUSY_TIMEOUT_MS = 5000

# Nombre de valeurs montrées par critère fautif dans le diagnostic : assez pour
# reconnaître un vocabulaire, assez peu pour ne pas noyer la réponse.
_SAMPLE_VALUES = 5

# Un tri par corpus doit être total, sinon deux pages successives peuvent
# répéter ou sauter une annonce : `(source, market, source_id)` départage
# toujours. Les prix et années inconnus passent en dernier (ils ne sont pas
# « les moins chers »).
_ORDER_BY = {
    "price_asc": 'l."price" IS NULL, l."price" ASC, l."source", l."market", l."source_id"',
    "price_desc": 'l."price" IS NULL, l."price" DESC, l."source", l."market", l."source_id"',
    "age_desc": 'l."year" IS NULL, l."year" DESC, l."source", l."market", l."source_id"',
}

_LISTING_COLUMNS = (
    "source",
    "market",
    "source_id",
    "url",
    "make",
    "group",
    "group_label",
    "engine",
    "trim",
    "year",
    "km",
    "price",
    "fuel",
    "gear",
    "vat_label",
    "seller",
    "city",
    "postal_code",
    "country",
    "image",
    "vin",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS "listings" (
    "source"        TEXT NOT NULL,
    "market"        TEXT NOT NULL,
    "source_id"     TEXT NOT NULL,
    "url"           TEXT NOT NULL,
    "make"          TEXT NOT NULL,
    "group"         TEXT NOT NULL,
    "group_label"   TEXT NOT NULL,
    "engine"        TEXT NOT NULL DEFAULT '',
    "trim"          TEXT NOT NULL DEFAULT '',
    "year"          INTEGER,
    "km"            INTEGER,
    "price"         INTEGER,
    "fuel"          TEXT NOT NULL DEFAULT '',
    "gear"          TEXT NOT NULL DEFAULT '',
    "vat_label"     TEXT NOT NULL DEFAULT '',
    "seller"        TEXT NOT NULL DEFAULT '',
    "city"          TEXT NOT NULL DEFAULT '',
    "postal_code"   TEXT NOT NULL DEFAULT '',
    "country"       TEXT NOT NULL DEFAULT '',
    "image"         TEXT NOT NULL DEFAULT '',
    "vin"           TEXT,
    PRIMARY KEY ("source", "market", "source_id")
);

CREATE TABLE IF NOT EXISTS "runs" (
    "job_id"             TEXT NOT NULL,
    "source"             TEXT NOT NULL,
    "market"             TEXT NOT NULL,
    "status"             TEXT NOT NULL,
    "announced_total"    INTEGER,
    "read"               INTEGER NOT NULL DEFAULT 0,
    "unique"             INTEGER NOT NULL DEFAULT 0,
    "dropped_duplicates" INTEGER NOT NULL DEFAULT 0,
    "dropped_invalid"    INTEGER NOT NULL DEFAULT 0,
    "truncated"          INTEGER NOT NULL DEFAULT 0,
    "reason"             TEXT NOT NULL DEFAULT '',
    "criteria_hash"      TEXT NOT NULL DEFAULT '',
    "sort"               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY ("job_id", "source", "market")
);

CREATE TABLE IF NOT EXISTS "run_listings" (
    "job_id"    TEXT NOT NULL,
    "source"    TEXT NOT NULL,
    "market"    TEXT NOT NULL,
    "source_id" TEXT NOT NULL,
    PRIMARY KEY ("job_id", "source", "market", "source_id")
);
"""


# Les colonnes ajoutées APRÈS la première version du schéma, avec leur
# définition SQL. `CREATE TABLE IF NOT EXISTS` ne touche pas une table qui existe
# déjà : `executescript` ci-dessus n'ajoute donc rien à une base écrite par la
# version d'avant, et `write_run` échouerait sur l'INSERT (« table runs has no
# column named dropped_invalid »). Une mise à jour casserait alors toute
# récolte sur un corpus existant, sans que rien ne le signale avant l'appel.
_ADDED_COLUMNS = (("runs", "dropped_invalid", "INTEGER NOT NULL DEFAULT 0"),)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Ajoute à une base existante les colonnes que le schéma courant suppose.

    Un `ALTER TABLE` n'est idempotent qu'au prix de l'erreur : SQLite refuse
    une colonne qui existe déjà (`duplicate column name`). C'est cette
    erreur-là, et elle seule, qui est avalée — une table absente ou une base
    verrouillée doit continuer de lever, sinon une migration ratée passerait
    pour une migration faite.
    """
    for table, column, definition in _ADDED_COLUMNS:
        try:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


class UnknownJobError(LookupError):
    """Aucune récolte ne porte ce `job_id`."""


@dataclass(frozen=True, slots=True)
class JobCriteria:
    """Ce qui gouverne la lecture du corpus d'une récolte : son tri, et
    l'empreinte des critères qui l'ont produite."""

    sort: str
    criteria_hash: str


# --- analyse par modèle -------------------------------------------------------
# L'analyse répond à « quels modèles acheter » sur le corpus déjà récolté. Elle
# ne récolte rien et ne classe rien (PLAN.md §10) : elle rend des distributions
# et des comptes. Le groupe comparable est `(make, group)`, pas `(make,
# group_label)` : `group` est la clé de gamme que l'adaptateur normalise déjà
# entre marchés (« Série 3 » et « 3er » se rejoignent sur `serie 3`), donc deux
# libellés de la même gamme font un seul groupe.


@dataclass(frozen=True, slots=True)
class ListingFilter:
    """Ce qu'une analyse retient du corpus : des critères déjà validés.

    `None` sur `markets`/`sources` veut dire « aucun filtre » : tout le corpus,
    quel que soit son marché ou sa source. Cette distinction compte, et c'est
    pour cela que le défaut n'est pas celui de `harvest` : une récolte ne peut
    interroger qu'une marketplace à la fois, donc elle en choisit une par
    défaut ; une analyse lit un corpus déjà constitué, et se restreindre en
    silence à `.fr` cacherait la moitié des annonces d'un corpus qui en porte
    deux (PLAN.md §5 : l'analyse ne dépend pas de l'ordre des appels).

    `make` et `model` sont des LIBELLÉS, comparés sans tenir compte de la casse
    (sans casse ni accents) : `model` vaut pour `group_label` ou pour le `group`
    normalisé.
    Les deux outils de lecture (`harvest` par ses critères, `analyze_models` par
    ce même filtre) prennent désormais les **mêmes libellés** : ceux que le site
    affiche. Il n'y a plus deux vocabulaires pour un nom de clé — c'était le cas
    tant que `harvest` attendait les identifiants numériques du site, et c'est ce
    décalage qui a produit « 8 320 BMW annoncées, 7 Série 3 lues ».
    La comparaison ignore la casse **et les accents** (`unaccent`, mesuré :
    `CITROËN` trouve `Citroën`) ; `model` vaut pour `group_label` ou pour le
    `group` normalisé. La réponse nomme les filtres qui ne retiennent aucune
    annonce (`unmatched_filters`) plutôt que de rendre un zéro muet.
    """

    make: str = ""
    model: str = ""
    year_min: int | None = None
    year_max: int | None = None
    km_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    fuel: str = ""
    gear: str = ""
    markets: tuple[str, ...] | None = None
    sources: tuple[str, ...] | None = None

    def as_dict(self) -> dict:
        """Les critères réellement appliqués, tels que la réponse les annonce.

        Un champ vide n'est pas un filtre, donc il est absent — et c'est ce
        `dict` que l'appelant relit pour savoir ce qui a été filtré. Un critère
        appliqué mais absent d'ici serait un filtre invisible.
        """
        applied: dict = {}
        if self.make:
            applied["make"] = self.make
        if self.model:
            applied["model"] = self.model
        for key in ("year_min", "year_max", "km_max", "price_min", "price_max"):
            value = getattr(self, key)
            if value is not None:
                applied[key] = value
        if self.fuel:
            applied["fuel"] = self.fuel
        if self.gear:
            applied["gear"] = self.gear
        if self.markets is not None:
            applied["markets"] = list(self.markets)
        if self.sources is not None:
            applied["sources"] = list(self.sources)
        return applied


@dataclass(frozen=True, slots=True)
class ModelGroup:
    """Les agrégats d'un groupe `(make, group)`.

    `n` compte TOUTES les annonces du groupe, y compris celles qui ne portent
    pas de prix crédible : une annonce à 1 € (« prix non communiqué » chez
    AutoScout24) reste une annonce. `n_priced` compte celles qui portent un prix
    crédible, et c'est cette série-là, seule, qui porte les quantiles de prix.
    L'écart entre les deux chiffres est donc visible sans avoir à le recalculer.

    `year_median` et `km_median` sont les médianes classiques — la moyenne des
    deux valeurs du milieu quand l'effectif est pair — calculées en SQL sur les
    annonces du groupe qui portent l'année, respectivement le kilométrage.
    `None` quand aucune ne le porte : l'absence est dite, jamais comblée.
    """

    make: str
    group: str
    group_label: str
    n: int
    n_priced: int
    year_median: int | float | None
    km_median: int | float | None


@dataclass(frozen=True, slots=True)
class UnmatchedFilter:
    """Un critère appliqué qui ne retient aucune annonce du corpus, et ce que
    le corpus porte réellement pour le même champ.

    Sans lui, un filtre qui ne ramène rien se lirait comme « aucun modèle
    n'existe » au lieu de « ton libellé ne correspond à rien ».
    un libellé) rendrait zéro groupe sans que rien ne distingue « ce filtre ne
    correspond à rien » de « le corpus est vide ». `present` porte les valeurs
    trouvées, pas une valeur suggérée : l'outil ne devine aucun vocabulaire.
    """

    key: str
    value: object
    present: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FilterPart:
    """Un critère : sa clause SQL, ses paramètres, et la colonne où lire une
    valeur réellement présente (pour dire ce que le corpus porte).

    La colonne reste citée en SQL (`"group"` est un mot réservé) et la clause
    est toujours paramétrée : aucune valeur d'appelant n'entre dans le texte de
    la requête.
    """

    key: str
    value: object
    sql: str
    params: tuple[object, ...]
    column: str


def _filter_parts(listing_filter: ListingFilter) -> list[_FilterPart]:
    """Les critères effectivement appliqués, un par un, dans l'ordre du contrat.

    Un seul endroit décide de ce qui filtre une ANNONCE : `_filter_clause` en
    fait la clause SQL et `unmatched_filters` les sonde un par un. Les deux
    lisent cette liste, donc ils ne peuvent pas en diverger.

    `as_dict` n'est PAS la même liste, et c'est voulu : il annonce ce que
    l'appelant a demandé, y compris `markets` et `sources`, qui ne filtrent pas
    des annonces mais choisissent les sources à interroger. Les fusionner ferait
    disparaître ces deux critères de la réponse — un filtre appliqué deviendrait
    invisible, ce que `as_dict` existe précisément pour empêcher.
    """
    parts: list[_FilterPart] = []
    if listing_filter.make:
        parts.append(
            _FilterPart(
                "make",
                listing_filter.make,
                'unaccent(l."make") = ?',
                (_unaccent(listing_filter.make),),
                '"make"',
            )
        )
    if listing_filter.model:
        parts.append(
            _FilterPart(
                "model",
                listing_filter.model,
                '(unaccent(l."group_label") = ? OR unaccent(l."group") = ?)',
                (
                    _unaccent(listing_filter.model),
                    _unaccent(listing_filter.model),
                ),
                '"group_label"',
            )
        )
    for key, column, operator in (
        ("year_min", '"year"', ">="),
        ("year_max", '"year"', "<="),
        ("km_max", '"km"', "<="),
        ("price_min", '"price"', ">="),
        ("price_max", '"price"', "<="),
    ):
        bound = getattr(listing_filter, key)
        if bound is not None:
            parts.append(_FilterPart(key, bound, f"l.{column} {operator} ?", (bound,), column))
    for key, column in (("fuel", '"fuel"'), ("gear", '"gear"')):
        value = getattr(listing_filter, key)
        if value:
            parts.append(_FilterPart(key, value, f"l.{column} = ?", (value,), column))
    for key, column in (("markets", '"market"'), ("sources", '"source"')):
        values = getattr(listing_filter, key)
        if values:
            placeholders = ", ".join("?" for _ in values)
            parts.append(
                _FilterPart(
                    key, list(values), f"l.{column} IN ({placeholders})", tuple(values), column
                )
            )
    return parts


def _filter_clause(parts: Sequence[_FilterPart]) -> tuple[str, tuple[object, ...]]:
    """La conjonction d'un filtre, sans le mot `WHERE` : un filtre vide n'est
    pas une condition, et l'appelant décide où la placer."""
    if not parts:
        return "", ()
    return " AND ".join(part.sql for part in parts), tuple(
        value for part in parts for value in part.params
    )


class Store:
    """Accès au corpus. S'ouvre en contexte : `with Store(path) as store: ...`."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        # WAL est ce qui permet à une lecture (`offers`) de se faire pendant une
        # écriture (`harvest`) : en journal par défaut, le COMMIT de l'écrivain
        # a besoin d'un verrou exclusif et échoue dès qu'un lecteur tient un
        # instantané. Le mode est une propriété du FICHIER, il est donc posé à
        # chaque ouverture (sans effet s'il l'est déjà).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # `unaccent` plutôt que `LOWER` pour les libellés : le `LOWER` de SQLite
        # n'abaisse que l'ASCII, donc `LOWER('CITROËN')` vaut `citroËn` et ne
        # matche jamais `Citroën` — mesuré. Sur un marché français, c'est
        # exactement les marques qu'on cherche qui échouent. La fonction est
        # déclarée `deterministic` pour que SQLite puisse la traiter comme telle.
        self._conn.create_function("unaccent", 1, _unaccent, deterministic=True)
        self._conn.executescript(_SCHEMA)
        _add_missing_columns(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # --- écriture -----------------------------------------------------------

    def add_page(self, job_id: str, listings: Iterable[Listing]) -> None:
        """Persiste une page lue et la rattache à la récolte `job_id`.

        Revoir une annonce déjà connue la met à jour sans créer de ligne : le
        corpus ne compte que des annonces distinctes.
        """
        columns = ", ".join(f'"{name}"' for name in _LISTING_COLUMNS)
        placeholders = ", ".join("?" for _ in _LISTING_COLUMNS)
        insert_listing = f'INSERT OR REPLACE INTO "listings" ({columns}) VALUES ({placeholders})'
        link_listing = (
            'INSERT OR IGNORE INTO "run_listings" '
            '("job_id", "source", "market", "source_id") VALUES (?, ?, ?, ?)'
        )
        with self._conn:
            for listing in listings:
                fields = listing.as_dict()
                self._conn.execute(
                    insert_listing, tuple(fields[name] for name in _LISTING_COLUMNS)
                )
                self._conn.execute(link_listing, (job_id,) + listing.key())

    def write_run(
        self,
        job_id: str,
        coverage: SourceCoverage,
        *,
        criteria_hash: str,
        sort: str,
    ) -> None:
        """Écrit la ligne de récolte d'un `(source, market)`. Une par récolte :
        deux appels sur la même paire remplacent, ils n'additionnent pas."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO "runs" ('
                '"job_id", "source", "market", "status", "announced_total", '
                '"read", "unique", "dropped_duplicates", "dropped_invalid", '
                '"truncated", "reason", "criteria_hash", "sort") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    job_id,
                    coverage.source,
                    coverage.market,
                    coverage.status,
                    coverage.announced_total,
                    coverage.read,
                    coverage.unique,
                    coverage.dropped_duplicates,
                    coverage.dropped_invalid,
                    int(coverage.truncated),
                    coverage.reason,
                    criteria_hash,
                    sort,
                ),
            )

    # --- lecture ------------------------------------------------------------

    def job(self, job_id: str) -> JobCriteria:
        """Tri et empreinte de critères d'une récolte. Lève `UnknownJobError`."""
        row = self._conn.execute(
            'SELECT "sort", "criteria_hash" FROM "runs" WHERE "job_id" = ? '
            'ORDER BY "source", "market" LIMIT 1',
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(f"récolte inconnue : {job_id!r}")
        return JobCriteria(sort=row["sort"], criteria_hash=row["criteria_hash"])

    def page(
        self, job_id: str, *, sort: str, offset: int, limit: int
    ) -> tuple[list[Listing], bool]:
        """`limit` annonces de la récolte à partir de `offset`, et s'il en reste.

        L'ordre vient du tri de la récolte et se termine toujours par la clé
        d'identité : `offset` tombe alors sur la même annonce d'un appel à
        l'autre.
        """
        order = _ORDER_BY.get(sort)
        if order is None:
            raise ValueError(f"tri inconnu : {sort!r}")
        columns = ", ".join(f'l."{name}"' for name in _LISTING_COLUMNS)
        rows = self._conn.execute(
            f'SELECT {columns} FROM "listings" AS l '
            'JOIN "run_listings" AS rl ON rl."source" = l."source" '
            'AND rl."market" = l."market" AND rl."source_id" = l."source_id" '
            'WHERE rl."job_id" = ? '
            f'ORDER BY {order} LIMIT ? OFFSET ?',
            (job_id, limit + 1, offset),
        ).fetchall()
        listings = [
            Listing(**{name: row[name] for name in _LISTING_COLUMNS})
            for row in rows[:limit]
        ]
        return listings, len(rows) > limit

    def corpus(self, job_id: str) -> CorpusCoverage:
        """Ce que la récolte contient réellement : annonces distinctes, nombre
        de récoltes, et le détail par source. Jamais la somme des `read`.

        Chaque source porte aussi l'état de sa récolte (`read`, `unique`,
        `announced_total`, `truncated`, `status`) : les annonces distinctes
        disent la taille du corpus, ces champs disent la part du marché lue.
        Un corpus de 400 annonces sur un marché qui en annonce 12 479 doit
        pouvoir le dire à un appelant qui ne lit qu'`offers`.

        `listings_in_corpus` compte les annonces de la PAIRE `(source, market)`
        dans le corpus entier, exactement comme `whole_corpus()` : deux chemins,
        une seule chose par clé. Ce que la récolte a laissé de neuf est déjà
        porté par `unique` ; compter ici les seules lignes de `run_listings`
        aurait donné deux chiffres différents sous le même nom selon l'outil.

        Une ligne de `runs` par `(source, market)`, sans agrégat ni `GROUP BY` :
        c'est la clé primaire de `runs`, `(job_id, source, market)`, qui
        garantit qu'une paire n'y a qu'une ligne (`write_run` remplace, il
        n'ajoute pas). Un `GROUP BY source, market` aurait rendu, pour deux
        lignes d'une même paire, un `status` et un `read` choisis arbitrairement
        parmi elles ; la sous-requête de comptage laisse au contraire chaque
        ligne de récolte visible au lieu de la fusionner en silence. Le jour où
        une deuxième récolte de la même paire devra cohabiter avec la première,
        c'est la clé primaire qu'il faudra changer, et cette requête le montrera.
        """
        run_count = self._conn.execute(
            'SELECT COUNT(*) FROM "runs" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        distinct = self._conn.execute(
            'SELECT COUNT(*) FROM "run_listings" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        rows = self._conn.execute(
            'SELECT r."source" AS source, r."market" AS market, '
            'r."status" AS status, r."announced_total" AS announced_total, '
            'r."read" AS read, r."unique" AS "unique", '
            'r."truncated" AS truncated, '
            '(SELECT COUNT(*) FROM "listings" AS l '
            'WHERE l."source" = r."source" AND l."market" = r."market") '
            'AS listings_in_corpus '
            'FROM "runs" AS r WHERE r."job_id" = ? '
            'ORDER BY r."source", r."market"',
            (job_id,),
        ).fetchall()
        return CorpusCoverage(
            listings_distinct=distinct,
            runs=run_count,
            sources=[
                {
                    "source": row["source"],
                    "market": row["market"],
                    "listings_in_corpus": row["listings_in_corpus"],
                    "status": row["status"],
                    "announced_total": row["announced_total"],
                    "read": row["read"],
                    "unique": row["unique"],
                    "truncated": bool(row["truncated"]),
                }
                for row in rows
            ],
        )

    def whole_corpus(self) -> CorpusCoverage:
        """Le corpus entier, toutes récoltes confondues, dans la forme de
        `corpus()` : annonces distinctes, nombre de récoltes, et une entrée par
        `(source, market)`.

        C'est le bloc dont une analyse a besoin : elle porte sur tout le corpus
        correspondant à ses critères, pas sur une récolte (PLAN.md §5), et le
        bloc décrit donc ce que le corpus contient — jamais le sous-ensemble
        qu'un filtre a retenu. Les compteurs de récolte n'ont rien à voir avec
        les critères : les filtrer laisserait croire que la couverture a changé
        parce qu'on a filtré.

        **Les compteurs d'une paire viennent de la dernière récolte écrite pour
        elle**, pas d'un agrégat : `runs` n'a pas d'horodatage, et additionner
        deux récoltes annoncerait une couverture double (PLAN.md §5). « Dernière
        écrite » s'appuie sur `MAX(rowid)` : `write_run` fait un
        `INSERT OR REPLACE`, qui supprime la ligne puis en insère une nouvelle,
        donc chaque écriture alloue un rowid neuf. C'est une propriété implicite
        de SQLite : un `ON CONFLICT DO UPDATE` la retirerait sans rien casser
        d'autre, et « la dernière récolte » deviendrait « une récolte
        arbitraire » — d'où ce commentaire. L'entrée ne porte pas le `job_id` de
        la récolte : les huit clés sont les mêmes que celles de `corpus()`, et
        deux formes de couverture pour un même type seraient un contrat à deux
        vitesses ; l'appelant qui veut le détail d'une récolte l'a déjà, c'est
        lui qui l'a lancée.

        Les annonces se comptent dans `listings` : la clé primaire
        `(source, market, source_id)` y garantit une ligne par annonce, donc
        `COUNT(*)` est déjà un compte d'annonces distinctes.
        """
        run_count = self._conn.execute('SELECT COUNT(*) FROM "runs"').fetchone()[0]
        distinct = self._conn.execute('SELECT COUNT(*) FROM "listings"').fetchone()[0]
        rows = self._conn.execute(
            'SELECT r."source" AS source, r."market" AS market, '
            'r."status" AS status, r."announced_total" AS announced_total, '
            'r."read" AS read, r."unique" AS "unique", '
            'r."truncated" AS truncated, '
            '(SELECT COUNT(*) FROM "listings" AS l '
            'WHERE l."source" = r."source" AND l."market" = r."market") '
            'AS listings_in_corpus '
            'FROM "runs" AS r WHERE r.rowid = ('
            'SELECT MAX(r2.rowid) FROM "runs" AS r2 '
            'WHERE r2."source" = r."source" AND r2."market" = r."market") '
            'ORDER BY r."source", r."market"'
        ).fetchall()
        return CorpusCoverage(
            listings_distinct=distinct,
            runs=run_count,
            sources=[
                {
                    "source": row["source"],
                    "market": row["market"],
                    "listings_in_corpus": row["listings_in_corpus"],
                    "status": row["status"],
                    "announced_total": row["announced_total"],
                    "read": row["read"],
                    "unique": row["unique"],
                    "truncated": bool(row["truncated"]),
                }
                for row in rows
            ],
        )

    # --- analyse par modèle -------------------------------------------------

    def model_groups(
        self, listing_filter: ListingFilter, *, price_floor: int, limit: int
    ) -> tuple[list[ModelGroup], int]:
        """Les `limit` premiers groupes `(make, group)` du corpus qui
        correspondent au filtre, et le nombre TOTAL de groupes correspondants.

        L'ordre est neutre et il est cité : `n` décroissant, puis `make` et
        `group` alphabétiques. Aucun classement de « meilleure affaire » —
        l'outil rend des distributions, le choix reste à l'appelant
        (PLAN.md §10). Les deux chiffres rendus ensemble sont ce qui distingue
        « 30 groupes » de « 30 sur 87 » : `limit` coupe la liste, jamais le
        compte.

        `price_floor` est un PARAMÈTRE, jamais un nombre écrit ici : le plancher
        de crédibilité d'un prix n'a qu'une déclaration (`models.PRICE_FLOOR_EUR`)
        et une seule règle (`models.price_is_plausible`). Une copie dans ce SQL
        se désynchroniserait de `offers.price_plausible` sans que rien ne le
        dise — la même annonce serait crédible ici et pas là.

        Les médianes d'année et de kilométrage sont calculées en SQL, par
        fenêtres : pour chaque groupe, le rang et l'effectif de chaque valeur,
        puis la moyenne des deux rangs du milieu — un effectif impair ne retient
        qu'un rang, donc la médiane reste la médiane. Les quantiles de PRIX, eux,
        sont calculés par `tools` sur les seuls groupes retenus : ils demandent
        une interpolation que SQLite n'exprime pas, et charger trente séries
        n'est pas charger le corpus.
        """
        clause, params = _filter_clause(_filter_parts(listing_filter))
        where = f"WHERE {clause}" if clause else ""
        rows = self._conn.execute(
            f'WITH f AS ('
            'SELECT l."make" AS make_, l."group" AS grp, l."group_label" AS label_, '
            'l."year" AS year_, l."km" AS km_, l."price" AS price_ '
            f'FROM "listings" AS l {where}), '
            'years AS ('
            'SELECT make_, grp, AVG(year_) AS median_ FROM ('
            'SELECT make_, grp, year_, '
            'ROW_NUMBER() OVER (PARTITION BY make_, grp ORDER BY year_) AS rn, '
            'COUNT(*) OVER (PARTITION BY make_, grp) AS cnt '
            'FROM f WHERE year_ IS NOT NULL) '
            'WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2) GROUP BY make_, grp), '
            'kms AS ('
            'SELECT make_, grp, AVG(km_) AS median_ FROM ('
            'SELECT make_, grp, km_, '
            'ROW_NUMBER() OVER (PARTITION BY make_, grp ORDER BY km_) AS rn, '
            'COUNT(*) OVER (PARTITION BY make_, grp) AS cnt '
            'FROM f WHERE km_ IS NOT NULL) '
            'WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2) GROUP BY make_, grp) '
            'SELECT g.make_ AS make, g.grp AS grp, g.label_ AS group_label, '
            'g.n AS n, g.n_priced AS n_priced, '
            'y.median_ AS year_median, k.median_ AS km_median '
            'FROM (SELECT make_, grp, MAX(label_) AS label_, COUNT(*) AS n, '
            'SUM(CASE WHEN price_ >= ? THEN 1 ELSE 0 END) AS n_priced '
            'FROM f GROUP BY make_, grp) AS g '
            'LEFT JOIN years AS y ON y.make_ = g.make_ AND y.grp = g.grp '
            'LEFT JOIN kms AS k ON k.make_ = g.make_ AND k.grp = g.grp '
            'ORDER BY g.n DESC, g.make_, g.grp LIMIT ?',
            (*params, price_floor, limit),
        ).fetchall()
        total = self._conn.execute(
            'SELECT COUNT(*) FROM ('
            f'SELECT 1 FROM "listings" AS l {where} GROUP BY l."make", l."group")',
            params,
        ).fetchone()[0]
        return (
            [
                ModelGroup(
                    make=row["make"],
                    group=row["grp"],
                    group_label=row["group_label"],
                    n=row["n"],
                    n_priced=row["n_priced"],
                    year_median=_number(row["year_median"]),
                    km_median=_number(row["km_median"]),
                )
                for row in rows
            ],
            total,
        )

    def group_prices(
        self,
        listing_filter: ListingFilter,
        groups: Sequence[tuple[str, str]],
        *,
        price_floor: int,
    ) -> dict[tuple[str, str], list[int]]:
        """Les prix crédibles de chaque groupe demandé, triés croissant.

        Une seule requête pour tous les groupes retenus, et pas une par groupe :
        chaque groupe coûterait une application du filtre. Les prix sous le
        plancher sont absents de la série — ils restent comptés dans `n` par
        `model_groups`, mais une médiane calculée sur un prix à 1 € ne serait pas
        un prix. Un groupe sans prix crédible rend une liste vide, jamais une
        clé absente : l'appelant doit pouvoir distinguer « zéro prix » d'un
        groupe qu'il aurait oublié de demander.
        """
        series: dict[tuple[str, str], list[int]] = {group: [] for group in groups}
        if not groups:
            return series
        clause, params = _filter_clause(_filter_parts(listing_filter))
        keys = " OR ".join('(l."make" = ? AND l."group" = ?)' for _ in groups)
        extra = f" AND {clause}" if clause else ""
        rows = self._conn.execute(
            'SELECT l."make" AS make, l."group" AS grp, l."price" AS price '
            f'FROM "listings" AS l WHERE ({keys}) AND l."price" >= ?{extra} '
            'ORDER BY l."make", l."group", l."price"',
            (
                *(value for group in groups for value in group),
                price_floor,
                *params,
            ),
        ).fetchall()
        for row in rows:
            series[(row["make"], row["grp"])].append(row["price"])
        return series

    def unmatched_filters(
        self, listing_filter: ListingFilter
    ) -> list[UnmatchedFilter]:
        """Les critères qui ne retiennent aucune annonce du corpus, avec ce que
        le corpus porte pour le même champ (au plus `sample` valeurs).

        Chaque critère est sondé SEUL, sur le corpus entier : c'est ce qui rend
        le diagnostic attribuable — « make='13' ne correspond à aucun libellé ;
        le corpus porte BMW, Renault » se lit sans deviner lequel des cinq
        critères est en cause. Une intersection vide, elle, n'est pas un
        vocabulaire faux mais une combinaison sans résultat, et `tools` la dit
        autrement.

        Le sondage ne devine rien : il ne teste ni forme numérique ni casse
        spéciale. `model="500"` est un libellé parfaitement légitime (une Fiat
        500) : une heuristique du genre « une valeur courte et chiffrée est donc
        un code » casserait ce cas réel pour rendre service à un autre.
        """
        misses: list[UnmatchedFilter] = []
        for part in _filter_parts(listing_filter):
            hits = self._conn.execute(
                f'SELECT COUNT(*) FROM "listings" AS l WHERE {part.sql}', part.params
            ).fetchone()[0]
            if hits:
                continue
            present = self._conn.execute(
                f'SELECT DISTINCT l.{part.column} AS present FROM "listings" AS l '
                f'WHERE CAST(l.{part.column} AS TEXT) <> \'\' '
                'ORDER BY present LIMIT ?',
                (_SAMPLE_VALUES,),
            ).fetchall()
            misses.append(
                UnmatchedFilter(
                    key=part.key,
                    value=part.value,
                    present=tuple(str(row["present"]) for row in present),
                )
            )
        return misses


def _unaccent(value: object) -> object:
    """Libellé comparé sans casse ni accents. `CITROËN` et `Citroën` se valent.

    Le `LOWER` de SQLite n'abaisse que l'ASCII : mesuré, `LOWER('CITROËN')` vaut
    `citroËn`, donc une recherche `Citroën` ne trouvait aucune annonce saisie en
    majuscules accentuées. `unicodedata` retire les marques combinantes (NFKD),
    puis `casefold` règle la casse — y compris `ß` → `ss`, que `lower` ne fait
    pas.

    Rend la valeur telle quelle si ce n'est pas une chaîne : la fonction est
    appelée dans du SQL, où `NULL` circule.
    """
    if not isinstance(value, str):
        return value
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def _number(value: object) -> int | float | None:
    """`AVG` rend toujours un flottant : une année médiane de 2021 s'écrirait
    `2021.0` alors que le contrat l'annonce entière, et une médiane de 2020,5
    (effectif pair) doit rester un demi — arrondir inventerait une précision
    que le corpus n'a pas.
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value  # type: ignore[return-value]
