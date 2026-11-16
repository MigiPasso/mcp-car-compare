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
        self._conn.executescript(_SCHEMA)

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
                '"read", "unique", "dropped_duplicates", "truncated", "reason", '
                '"criteria_hash", "sort") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    job_id,
                    coverage.source,
                    coverage.market,
                    coverage.status,
                    coverage.announced_total,
                    coverage.read,
                    coverage.unique,
                    coverage.dropped_duplicates,
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
        de récoltes, et le détail par source. Jamais la somme des `read`."""
        run_count = self._conn.execute(
            'SELECT COUNT(*) FROM "runs" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        distinct = self._conn.execute(
            'SELECT COUNT(*) FROM "run_listings" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        rows = self._conn.execute(
            'SELECT r."source" AS source, r."market" AS market, '
            'COUNT(rl."source_id") AS listings FROM "runs" AS r '
            'LEFT JOIN "run_listings" AS rl ON rl."job_id" = r."job_id" '
            'AND rl."source" = r."source" AND rl."market" = r."market" '
            'WHERE r."job_id" = ? GROUP BY r."source", r."market" '
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
                }
                for row in rows
            ],
        )
