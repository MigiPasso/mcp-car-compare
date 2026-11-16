"""Le corpus : des annonces identifiées, et des récoltes qui ne se somment pas.

Chaque test ouvre sa propre base dans `tmp_path` : aucun état partagé, aucune
horloge, aucun réseau.
"""

from __future__ import annotations

import fnmatch
import sqlite3
from pathlib import Path

import pytest

from mcp_car_compare import store as store_module
from mcp_car_compare.models import Listing, SourceCoverage
from mcp_car_compare.store import BUSY_TIMEOUT_MS, Store, UnknownJobError


def listing(
    source_id: str = "1",
    *,
    market: str = "fr",
    price: int | None = 10_000,
    year: int | None = 2021,
    source: str = "autoscout24",
) -> Listing:
    return Listing(
        source=source,
        market=market,
        source_id=source_id,
        url=f"https://example.test/{source}/{market}/{source_id}",
        make="Renault",
        group="clio",
        group_label="Clio",
        year=year,
        km=48_000,
        price=price,
        fuel="diesel",
        gear="auto",
    )


def run(
    source: str = "autoscout24",
    market: str = "fr",
    *,
    read: int = 0,
    unique: int = 0,
    dropped: int = 0,
    invalid: int = 0,
    status: str = "ok",
    announced_total: int | None = None,
    truncated: bool = False,
    reason: str = "",
) -> SourceCoverage:
    return SourceCoverage(
        source=source,
        market=market,
        status=status,
        announced_total=announced_total,
        read=read,
        unique=unique,
        dropped_duplicates=dropped,
        dropped_invalid=invalid,
        truncated=truncated,
        reason=reason,
    )


def open_store(tmp_path) -> Store:
    return Store(tmp_path / "corpus.db")


# --- identité -----------------------------------------------------------------


def test_the_same_source_id_on_two_markets_is_two_listings(tmp_path):
    # Rien ne garantit que les identifiants d'autoscout24.fr et .de ne se
    # recouvrent pas : le marché est dans la clé d'identité.
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("42", market="fr"), listing("42", market="de")])
        items, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        assert sorted((item.source_id, item.market) for item in items) == [
            ("42", "de"),
            ("42", "fr"),
        ]


def test_reading_a_listing_again_updates_it_instead_of_duplicating(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1", price=10_000), listing("2", price=11_000)])
        store.add_page("job-1", [listing("1", price=9_000)])
        items, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        assert [item.price for item in items] == [9_000, 11_000]


def test_a_listing_read_by_two_harvests_belongs_to_both(tmp_path):
    # La seconde récolte ne doit pas retirer l'annonce de la première : la
    # couverture d'un job ancien ne rétrécit jamais en silence.
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1"), listing("2")])
        store.add_page("job-2", [listing("2"), listing("3")])
        first, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        second, _ = store.page("job-2", sort="price_asc", offset=0, limit=10)
        assert sorted(item.source_id for item in first) == ["1", "2"]
        assert sorted(item.source_id for item in second) == ["2", "3"]
        assert store.corpus("job-1").listings_distinct == 2
        assert store.corpus("job-2").listings_distinct == 2


# --- lecture ------------------------------------------------------------------


def test_prices_sort_ascending_and_unknown_prices_come_last(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page(
            "job-1",
            [listing("a", price=3_000), listing("b", price=None), listing("c", price=1_000)],
        )
        ascending, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        descending, _ = store.page("job-1", sort="price_desc", offset=0, limit=10)
        assert [item.price for item in ascending] == [1_000, 3_000, None]
        assert [item.price for item in descending] == [3_000, 1_000, None]


def test_equal_prices_keep_a_stable_order(tmp_path):
    # Deux pages successives reposent sur un ordre total : à prix égal, la clé
    # d'identité départage.
    with open_store(tmp_path) as store:
        store.add_page(
            "job-1",
            [listing("b", price=5_000), listing("a", price=5_000), listing("c", price=5_000)],
        )
        first, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        second, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        assert [item.source_id for item in first] == ["a", "b", "c"]
        assert [item.source_id for item in second] == ["a", "b", "c"]


def test_age_desc_puts_the_newest_first_and_unknown_years_last(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page(
            "job-1",
            [listing("old", year=2015), listing("new", year=2023), listing("unknown", year=None)],
        )
        items, _ = store.page("job-1", sort="age_desc", offset=0, limit=10)
        assert [item.source_id for item in items] == ["new", "old", "unknown"]


def test_a_page_says_whether_more_remains(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1"), listing("2"), listing("3")])
        items, has_more = store.page("job-1", sort="price_asc", offset=0, limit=2)
        assert (len(items), has_more) == (2, True)
        items, has_more = store.page("job-1", sort="price_asc", offset=2, limit=2)
        assert (len(items), has_more) == (1, False)


def test_an_unknown_sort_is_refused(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1")])
        with pytest.raises(ValueError, match="tri inconnu"):
            store.page("job-1", sort="cheapest", offset=0, limit=10)


# --- récoltes -----------------------------------------------------------------


def test_an_unknown_harvest_is_refused(tmp_path):
    with open_store(tmp_path) as store:
        with pytest.raises(UnknownJobError):
            store.job("jamais-vu")


def test_a_run_is_written_once_per_source_and_market(tmp_path):
    with open_store(tmp_path) as store:
        store.write_run("job-1", run(read=20, unique=20), criteria_hash="h", sort="price_asc")
        store.write_run("job-1", run(read=40, unique=40), criteria_hash="h", sort="price_asc")
        assert store.corpus("job-1").runs == 1
        assert store.corpus("job-1").sources == [
            {
                "source": "autoscout24",
                "market": "fr",
                "listings": 0,
                "status": "ok",
                "announced_total": None,
                "read": 40,
                "unique": 40,
                "truncated": False,
            }
        ]


def test_a_harvest_keeps_its_criteria_and_sort(tmp_path):
    with open_store(tmp_path) as store:
        store.write_run(
            "job-1",
            run(market="de"),
            criteria_hash="abc",
            sort="price_desc",
        )
        job = store.job("job-1")
        assert (job.sort, job.criteria_hash) == ("price_desc", "abc")


def test_a_corpus_without_listings_reports_zeros_not_an_absent_block(tmp_path):
    with open_store(tmp_path) as store:
        store.write_run(
            "job-1",
            run(status="capped", reason="budget de 60 s atteint", truncated=True),
            criteria_hash="h",
            sort="price_asc",
        )
        corpus = store.corpus("job-1")
        assert corpus.listings_distinct == 0
        assert corpus.runs == 1
        assert corpus.sources == [
            {
                "source": "autoscout24",
                "market": "fr",
                "listings": 0,
                "status": "capped",
                "announced_total": None,
                "read": 0,
                "unique": 0,
                "truncated": True,
            }
        ]


def test_the_corpus_counts_distinct_listings_not_the_read_counter(tmp_path):
    # La récolte a lu 6 annonces dont 2 doublons : le corpus en contient 4. Les
    # deux chiffres coexistent, ils ne s'additionnent pas.
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1"), listing("2"), listing("3"), listing("4")])
        store.write_run(
            "job-1", run(read=6, unique=4, dropped=2), criteria_hash="h", sort="price_asc"
        )
        assert store.corpus("job-1").listings_distinct == 4


def test_a_corpus_entry_says_how_much_of_the_market_was_read(tmp_path):
    # Un appelant qui ne lit qu'`offers` doit pouvoir refuser de surinterpréter :
    # 400 annonces distinctes sur un marché qui en annonce 12 479 ne sont pas un
    # marché couvert, et le bloc `coverage` doit le dire sans qu'il ait à
    # rappeler `harvest`.
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1"), listing("2")])
        store.write_run(
            "job-1",
            run(
                read=20,
                unique=2,
                dropped=18,
                announced_total=12_479,
                truncated=True,
                reason="plafond du site atteint",
            ),
            criteria_hash="h",
            sort="price_asc",
        )
        entry = store.corpus("job-1").sources[0]
        assert entry["listings"] == 2  # ce que le corpus contient
        assert (entry["read"], entry["unique"]) == (20, 2)  # ce que la récolte a lu
        assert entry["announced_total"] == 12_479  # ce que le site annonçait
        assert (entry["truncated"], entry["status"]) == (True, "ok")


def test_a_run_keeps_its_dropped_invalid_count(tmp_path):
    # Les annonces sans identifiant écartées ne sont pas des doublons : le
    # chiffre est écrit avec la récolte, sinon la soustraction
    # `read == unique + doublons + invalides` ne se referme plus.
    with open_store(tmp_path) as store:
        store.write_run(
            "job-1", run(read=5, unique=3, dropped=1, invalid=1), criteria_hash="h", sort="price_asc"
        )
        row = store._conn.execute(
            'SELECT "read", "unique", "dropped_duplicates", "dropped_invalid" '
            'FROM "runs" WHERE "job_id" = ?',
            ("job-1",),
        ).fetchone()
        assert tuple(row) == (5, 3, 1, 1)


def test_each_source_and_market_appears_with_its_own_count(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("fr-1", market="fr"), listing("de-1", market="de")])
        store.add_page("job-1", [listing("de-2", market="de")])
        store.write_run("job-1", run(market="fr", read=1, unique=1), criteria_hash="h", sort="price_asc")
        store.write_run("job-1", run(market="de", read=2, unique=2), criteria_hash="h", sort="price_asc")
        corpus = store.corpus("job-1")
        assert corpus.runs == 2
        assert corpus.listings_distinct == 3
        assert [(row["source"], row["market"], row["listings"], row["read"]) for row in corpus.sources] == [
            ("autoscout24", "de", 2, 2),
            ("autoscout24", "fr", 1, 1),
        ]


# --- concurrence ----------------------------------------------------------------


def test_the_corpus_file_is_in_wal_mode(tmp_path):
    # Le mode est une propriété du FICHIER : une autre connexion le voit. En
    # journal par défaut, le COMMIT d'un écrivain exige un verrou exclusif et
    # échoue dès qu'un lecteur tient un instantané — c'est le « database is
    # locked » qu'un `offers` pendant un `harvest` produisait.
    path = tmp_path / "corpus.db"
    with Store(path) as store:
        store.add_page("job-1", [listing("1")])
    other = sqlite3.connect(path)
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        other.close()


def test_the_wal_sidecars_of_a_local_corpus_are_ignored_by_git_and_docker():
    # `Store` force `journal_mode=WAL`, donc un corpus local (`MCC_DB=./corpus.db`)
    # laisse `corpus.db-wal` et `corpus.db-shm` à côté de lui. Ces deux noms ne
    # matchent ni `*.db` ni `*.db-journal` : sans motifs dédiés, un `git add -A`
    # les emporterait dans un commit et un contexte de build les enverrait à
    # Docker. Un corpus d'annonces n'a rien à faire dans l'un ni dans l'autre.
    #
    # Le test lit les deux fichiers du dépôt ; `Dockerfile.test` ne les copie
    # pas dans l'image (elle ne porte que `src/`, `tests/`, `pyproject.toml`),
    # donc il se saute là-bas et ne vaut que par la commande qui monte le dépôt.
    root = Path(__file__).resolve().parents[1]
    names = [name for name in (".gitignore", ".dockerignore") if (root / name).exists()]
    if not names:
        pytest.skip("hors du dépôt (image de test sans fichier d'exclusion) : rien à lire")
    for name in names:
        patterns = [
            line.strip()
            for line in (root / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for sidecar in ("corpus.db-wal", "corpus.db-shm"):
            assert any(fnmatch.fnmatch(sidecar, pattern) for pattern in patterns), (
                f"{name} ne couvre pas {sidecar} : {patterns}"
            )


def test_a_reader_open_on_the_corpus_does_not_block_a_writer(tmp_path):
    path = tmp_path / "corpus.db"
    with Store(path) as store:
        store.add_page("job-1", [listing("1")])
    # Un lecteur qui tient son instantané, comme `offers` au milieu d'une page.
    reader = sqlite3.connect(path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        assert reader.execute('SELECT COUNT(*) FROM "listings"').fetchone()[0] == 1
        # En WAL, l'écriture aboutit pendant que l'instantané est ouvert ; en
        # journal par défaut, elle attend puis échoue en « database is locked ».
        with Store(path) as writer:
            writer.add_page("job-1", [listing("2")])
        assert reader.execute('SELECT COUNT(*) FROM "listings"').fetchone()[0] == 1
        reader.execute("COMMIT")
    finally:
        reader.close()
    assert Store(path).corpus("job-1").listings_distinct == 2


def test_the_busy_timeout_is_set_on_the_connection(tmp_path):
    # Le délai d'attente est une propriété de la CONNEXION (pas du fichier) :
    # il ne peut donc se lire que sur celle du `Store`.
    with Store(tmp_path / "corpus.db") as store:
        busy = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy == BUSY_TIMEOUT_MS


# --- migration ----------------------------------------------------------------

# Le schéma de la version d'avant `dropped_invalid`. Écrit ici en entier plutôt
# que dérivé de `store._SCHEMA` : une migration se prouve sur la forme réelle du
# fichier qu'on trouve chez l'utilisateur, pas sur une copie du code qui a déjà
# bougé avec elle.
PREVIOUS_SCHEMA = """
CREATE TABLE "listings" (
    "source"      TEXT NOT NULL,
    "market"      TEXT NOT NULL,
    "source_id"   TEXT NOT NULL,
    "url"         TEXT NOT NULL,
    "make"        TEXT NOT NULL,
    "group"       TEXT NOT NULL,
    "group_label" TEXT NOT NULL,
    "engine"      TEXT NOT NULL DEFAULT '',
    "trim"        TEXT NOT NULL DEFAULT '',
    "year"        INTEGER,
    "km"          INTEGER,
    "price"       INTEGER,
    "fuel"        TEXT NOT NULL DEFAULT '',
    "gear"        TEXT NOT NULL DEFAULT '',
    "vat_label"   TEXT NOT NULL DEFAULT '',
    "seller"      TEXT NOT NULL DEFAULT '',
    "city"        TEXT NOT NULL DEFAULT '',
    "postal_code" TEXT NOT NULL DEFAULT '',
    "country"     TEXT NOT NULL DEFAULT '',
    "image"       TEXT NOT NULL DEFAULT '',
    "vin"         TEXT,
    PRIMARY KEY ("source", "market", "source_id")
);

CREATE TABLE "runs" (
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

CREATE TABLE "run_listings" (
    "job_id"    TEXT NOT NULL,
    "source"    TEXT NOT NULL,
    "market"    TEXT NOT NULL,
    "source_id" TEXT NOT NULL,
    PRIMARY KEY ("job_id", "source", "market", "source_id")
);
"""


def previous_corpus(path) -> None:
    """Écrit un corpus dans le schéma d'avant, avec une récolte déjà dedans."""
    connection = sqlite3.connect(str(path))
    with connection:
        connection.executescript(PREVIOUS_SCHEMA)
        connection.execute(
            'INSERT INTO "runs" ("job_id", "source", "market", "status", "read", "unique", '
            '"dropped_duplicates", "criteria_hash", "sort") '
            "VALUES ('job-1', 'autoscout24', 'fr', 'ok', 20, 18, 2, 'h', 'price_asc')"
        )
    connection.close()


def test_une_base_de_la_version_precedente_est_migree_a_l_ouverture(tmp_path):
    # `CREATE TABLE IF NOT EXISTS` ne touche pas une table qui existe déjà :
    # sans migration, `write_run` échouerait sur « table runs has no column
    # named dropped_invalid », donc toute récolte sur un corpus existant — le
    # cas exact d'une mise à jour.
    path = tmp_path / "corpus.db"
    previous_corpus(path)

    with Store(path) as store:
        # la récolte écrite par la version d'avant est toujours lisible
        assert store.job("job-1").sort == "price_asc"
        assert store.corpus("job-1").sources[0]["read"] == 20
        # et la colonne nouvelle existe, à zéro plutôt qu'absente
        added = store._conn.execute(
            'SELECT "dropped_invalid" FROM "runs" WHERE "job_id" = ? AND "market" = ?',
            ("job-1", "fr"),
        ).fetchone()
        assert added[0] == 0
        # écrire après migration est le geste que la migration existe pour
        # rendre possible : sans elle, cette ligne lève.
        store.write_run(
            "job-1", run(market="de", read=1, unique=1), criteria_hash="h", sort="price_asc"
        )
        assert [row["market"] for row in store.corpus("job-1").sources] == ["de", "fr"]


def test_la_migration_est_idempotente(tmp_path):
    # `ALTER TABLE ... ADD COLUMN` échoue quand la colonne existe : chaque
    # ouverture d'un corpus déjà migré doit avaler cette erreur-là, sinon la
    # deuxième ouverture d'une base migrée lèverait — un serveur qui ne
    # redémarre plus sur son propre corpus.
    path = tmp_path / "corpus.db"
    previous_corpus(path)

    for _ in range(3):
        with Store(path) as store:
            assert store.job("job-1").sort == "price_asc"


def test_une_migration_qui_echoue_autrement_que_par_une_colonne_existante_leve(
    tmp_path, monkeypatch
):
    # Le seul cas avalé est « duplicate column name ». Une table absente ou une
    # base verrouillée doit continuer de lever : une migration ratée et avalée
    # passerait pour faite, et l'échec se déplacerait à la première récolte, là
    # où plus personne ne fait le lien avec l'ouverture du corpus.
    path = tmp_path / "corpus.db"
    monkeypatch.setattr(
        store_module, "_ADDED_COLUMNS", (("table_absente", "colonne", "INTEGER"),)
    )

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        Store(path)


# --- persistance --------------------------------------------------------------


def test_the_corpus_survives_a_reopening(tmp_path):
    path = tmp_path / "nested" / "corpus.db"
    with Store(path) as store:
        store.add_page("job-1", [listing("1")])
        store.write_run("job-1", run(read=1, unique=1), criteria_hash="h", sort="price_asc")
    with Store(path) as store:
        items, _ = store.page("job-1", sort="price_asc", offset=0, limit=10)
        assert [item.source_id for item in items] == ["1"]
        assert store.corpus("job-1").listings_distinct == 1
