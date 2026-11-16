"""Le corpus : des annonces identifiées, et des récoltes qui ne se somment pas.

Chaque test ouvre sa propre base dans `tmp_path` : aucun état partagé, aucune
horloge, aucun réseau.
"""

from __future__ import annotations

import pytest

from mcp_car_compare.models import Listing, SourceCoverage
from mcp_car_compare.store import Store, UnknownJobError


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
            {"source": "autoscout24", "market": "fr", "listings": 0}
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
        assert corpus.sources == [{"source": "autoscout24", "market": "fr", "listings": 0}]


def test_the_corpus_counts_distinct_listings_not_the_read_counter(tmp_path):
    # La récolte a lu 6 annonces dont 2 doublons : le corpus en contient 4. Les
    # deux chiffres coexistent, ils ne s'additionnent pas.
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("1"), listing("2"), listing("3"), listing("4")])
        store.write_run(
            "job-1", run(read=6, unique=4, dropped=2), criteria_hash="h", sort="price_asc"
        )
        assert store.corpus("job-1").listings_distinct == 4


def test_each_source_and_market_appears_with_its_own_count(tmp_path):
    with open_store(tmp_path) as store:
        store.add_page("job-1", [listing("fr-1", market="fr"), listing("de-1", market="de")])
        store.add_page("job-1", [listing("de-2", market="de")])
        store.write_run("job-1", run(market="fr"), criteria_hash="h", sort="price_asc")
        store.write_run("job-1", run(market="de"), criteria_hash="h", sort="price_asc")
        corpus = store.corpus("job-1")
        assert corpus.runs == 2
        assert corpus.listings_distinct == 3
        assert corpus.sources == [
            {"source": "autoscout24", "market": "de", "listings": 2},
            {"source": "autoscout24", "market": "fr", "listings": 1},
        ]


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
