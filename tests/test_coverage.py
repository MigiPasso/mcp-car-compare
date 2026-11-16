"""Le bloc `coverage` : présent partout, et jamais la somme des récoltes.

Ces tests portent sur l'invariant qui protège l'appelant d'une fausse
impression de complétude (PLAN.md §5) : `harvest` décrit ce que **cette**
récolte a lu, `offers` décrit ce que le **corpus** contient réellement.
"""

from __future__ import annotations

import asyncio

from mcp_car_compare import tools
from mcp_car_compare.models import SOURCE_STATUSES, Criteria, Listing, Page, SourceError


class FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args: object, **_kwargs: object):
        def declare(function):
            self.tools[function.__name__] = function
            return function

        return declare


class ScriptedSource:
    """Source factice : rejoue des pages d'indices, sans réseau ni horloge.

    Les annonces sont construites avec le marché demandé, comme le ferait un
    vrai adaptateur : une source qui ignorerait `criteria.markets` rendrait les
    identités des deux marchés identiques et masquerait la moitié du corpus.
    """

    source = "autoscout24"

    def __init__(self, pages: list[list[int]]) -> None:
        self.pages = pages
        self.searches: list[tuple[Criteria, int]] = []

    async def search(self, criteria: Criteria, page_number: int) -> Page:
        self.searches.append((criteria, page_number))
        if page_number > len(self.pages):
            return Page(listings=[])
        market = criteria.markets[0]
        return Page(listings=[listing(index, market=market) for index in self.pages[page_number - 1]])

    async def brands(self) -> list[dict]:
        return []


def listing(index: int, *, market: str = "fr") -> Listing:
    return Listing(
        source="autoscout24",
        market=market,
        source_id=f"{market}-{index:03d}",
        url=f"https://example.test/{market}/{index}",
        make="Renault",
        group="clio",
        group_label="Clio",
        year=2021,
        km=48_000,
        price=10_000 + index,
    )


def corpus_source(
    source: str,
    market: str,
    *,
    listings_in_corpus: int,
    status: str = "ok",
    announced_total: int | None = None,
    read: int = 0,
    unique: int = 0,
    truncated: bool = False,
) -> dict:
    """Une entrée de `coverage["sources"]` côté `offers` : les huit clés que
    `store.py` émet (PLAN.md §5, décision D5) — les mêmes que dans `harvest`,
    `listings_in_corpus` comprise.

    Les huit sont écrites ici, jamais un sous-ensemble : c'est ce qui fait
    échouer ce test si le bloc change de forme. Une entrée comparée à trois clés
    laisserait passer une cinquième sans que rien ne le dise.
    """
    return {
        "source": source,
        "market": market,
        "listings_in_corpus": listings_in_corpus,
        "status": status,
        "announced_total": announced_total,
        "read": read,
        "unique": unique,
        "truncated": truncated,
    }


def install(tmp_path, source: ScriptedSource) -> FakeMcp:
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=tmp_path / "corpus.db",
        source_factory=lambda _name: source,
        clock=lambda: 0.0,  # aucune récolte ne peut être coupée par le budget
    )
    return mcp


def harvest(mcp: FakeMcp, criteria: dict | None = None, budget_s: int | None = None) -> dict:
    # `budget_s=None` laisse l'outil prendre celui de la configuration, comme un
    # client qui n'en passe pas.
    return asyncio.run(mcp.tools["harvest"](criteria or {}, budget_s))


# --- côté harvest --------------------------------------------------------------


def test_harvest_coverage_is_never_absent_or_empty(tmp_path):
    mcp = install(tmp_path, ScriptedSource([[0, 1]]))
    coverage = harvest(mcp)["coverage"]
    assert coverage, "une récolte sans ligne de coverage ne dit rien de sa couverture"
    assert (coverage[0]["source"], coverage[0]["market"]) == ("autoscout24", "fr")


def test_read_is_unique_plus_dropped_duplicates_plus_dropped_invalid(tmp_path):
    # Une page qui se répète : le site rend deux fois les mêmes annonces. La
    # comptabilité est close à quatre termes (décision D4) : ce que les pages
    # ont rendu, c'est ce qui reste plus ce qui a été écarté, et rien d'autre.
    # Un cinquième terme non compté serait de la couverture perdue en silence.
    mcp = install(tmp_path, ScriptedSource([[0, 1, 2], [2, 3]]))
    row = harvest(mcp)["coverage"][0]
    assert row["read"] == row["unique"] + row["dropped_duplicates"] + row["dropped_invalid"]
    assert (row["read"], row["dropped_duplicates"], row["dropped_invalid"], row["unique"]) == (
        5,
        1,
        0,
        4,
    )


def test_every_reported_status_belongs_to_the_contract(tmp_path):
    # Deux marchés du même site : l'un répond, l'autre échoue. Les deux statuts
    # rendus doivent appartenir au vocabulaire fermé de `coverage` — sinon un
    # client qui teste `status == "challenge"` ne verrait jamais rien venir.
    class FailingOnOneMarket(ScriptedSource):
        async def search(self, criteria: Criteria, page_number: int) -> Page:
            if criteria.markets[0] == "de":
                raise SourceError("503 Service Unavailable")
            return await super().search(criteria, page_number)

    mcp = install(tmp_path, FailingOnOneMarket([[0]]))
    coverage = harvest(mcp, {"markets": ["fr", "de"]})["coverage"]
    assert [row["status"] for row in coverage] == ["ok", "error"]
    assert all(row["status"] in SOURCE_STATUSES for row in coverage)


# --- côté offers : le corpus, pas les récoltes ---------------------------------


def test_offers_coverage_describes_the_corpus_not_the_sum_of_the_harvests(tmp_path):
    # La récolte a lu 5 annonces, en a écarté 1 doublon : le corpus en contient
    # 4. Les deux chiffres coexistent et ne s'additionnent jamais.
    mcp = install(tmp_path, ScriptedSource([[0, 1, 2], [2, 3]]))
    result = harvest(mcp)
    assert result["coverage"][0]["read"] == 5
    coverage = mcp.tools["offers"](result["job_id"], None, 25)["coverage"]
    assert coverage["listings_distinct"] == 4
    assert coverage["runs"] == 1
    # 4 annonces distinctes dans le corpus, 5 lues par la récolte : les deux
    # chiffres coexistent dans la même entrée, sans jamais s'additionner.
    assert coverage["sources"] == [
        corpus_source("autoscout24", "fr", listings_in_corpus=4, read=5, unique=4)
    ]


def test_two_offers_cover_the_corpus_once_without_duplicates(tmp_path):
    mcp = install(tmp_path, ScriptedSource([list(range(9))]))
    result = harvest(mcp)
    seen: list[str] = []
    coverages: list[dict] = []
    page = mcp.tools["offers"](result["job_id"], None, 4)
    while True:
        seen.extend(item["source_id"] for item in page["items"])
        coverages.append(page["coverage"])
        if not page["has_more"]:
            break
        page = mcp.tools["offers"](result["job_id"], page["next_cursor"], 4)
    assert len(seen) == 9
    assert len(set(seen)) == 9, "deux pages ont rendu la même annonce"
    assert set(seen) == {listing(index).source_id for index in range(9)}
    # Le bloc décrit le corpus : il ne change pas d'une page à l'autre.
    corpus = {
        "listings_distinct": 9,
        "runs": 1,
        "sources": [corpus_source("autoscout24", "fr", listings_in_corpus=9, read=9, unique=9)],
    }
    assert coverages[0] == coverages[-1] == corpus


def test_an_empty_corpus_reports_zeros_not_a_missing_block(tmp_path):
    mcp = install(tmp_path, ScriptedSource([[]]))  # une page vide : rien à récolter
    result = harvest(mcp)
    page = mcp.tools["offers"](result["job_id"], None, 25)
    assert page["items"] == []
    assert page["coverage"] == {
        "listings_distinct": 0,
        "runs": 1,
        "sources": [corpus_source("autoscout24", "fr", listings_in_corpus=0)],
    }


def test_a_source_that_never_started_still_appears_with_zero(tmp_path):
    # Une source du périmètre dont l'adaptateur refuse de se construire doit se
    # voir dans le corpus avec zéro, pas disparaître du bloc. Une source HORS
    # périmètre ne peut plus arriver ici : elle est refusée au parsage des
    # critères (`test_tools.py`), pas récoltée puis comptée zéro.
    def factory(_name: str):
        raise SourceError("503 Service Unavailable")

    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db", source_factory=factory, clock=lambda: 0.0)
    result = harvest(mcp)
    assert result["coverage"][0]["status"] == "error"
    coverage = mcp.tools["offers"](result["job_id"], None, 25)["coverage"]
    assert coverage["listings_distinct"] == 0
    assert coverage["sources"] == [
        corpus_source("autoscout24", "fr", listings_in_corpus=0, status="error", truncated=True)
    ]


def test_the_corpus_counts_harvests_and_listings_separately(tmp_path):
    mcp = install(tmp_path, ScriptedSource([[0, 1]]))
    result = harvest(mcp, {"markets": ["fr", "de"]})
    assert len(result["coverage"]) == 2  # une ligne par (source, market)
    coverage = mcp.tools["offers"](result["job_id"], None, 25)["coverage"]
    assert coverage["runs"] == 2
    assert coverage["listings_distinct"] == 4
    assert coverage["sources"] == [
        corpus_source("autoscout24", "de", listings_in_corpus=2, read=2, unique=2),
        corpus_source("autoscout24", "fr", listings_in_corpus=2, read=2, unique=2),
    ]
