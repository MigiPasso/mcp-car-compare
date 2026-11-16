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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import CorpusCoverage, Listing, SourceCoverage

# BUSY_TIMEOUT_MS est le temps qu'une écriture attend un verrou tenu par une
# autre connexion avant de rendre `database is locked`. Sans lui, un `offers`
# lancé pendant un `harvest` échoue au lieu d'attendre la fin d'une transaction
# de quelques millisecondes.
BUSY_TIMEOUT_MS = 5000

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
            '(SELECT COUNT(*) FROM "run_listings" AS rl '
            'WHERE rl."job_id" = r."job_id" AND rl."source" = r."source" '
            'AND rl."market" = r."market") AS listings '
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
                    "listings": row["listings"],
                    "status": row["status"],
                    "announced_total": row["announced_total"],
                    "read": row["read"],
                    "unique": row["unique"],
                    "truncated": bool(row["truncated"]),
                }
                for row in rows
            ],
        )
