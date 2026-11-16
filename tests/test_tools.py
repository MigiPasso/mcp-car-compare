"""Les trois outils MCP : bornés, bavards sur ce qu'ils n'ont pas lu, hors réseau.

Aucun test n'ouvre de connexion : la source est factice et injectée, et
l'horloge est pilotée à la main — aucune assertion ne dépend d'une durée réelle
(à une exception près, signalée dans son test).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_car_compare import tools
from mcp_car_compare.cursor import Cursor, CursorError, decode, encode
from mcp_car_compare.models import Criteria, Listing, Page, SourceError, StructureChanged
from mcp_car_compare.store import UnknownJobError


# --- doublures -----------------------------------------------------------------


class FakeMcp:
    """Remplace FastMCP : retient les fonctions déclarées, sans protocole."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args: object, **_kwargs: object):
        def declare(function):
            self.tools[function.__name__] = function
            return function

        return declare


class FakeClock:
    """Horloge pilotée. `step` avance le temps à chaque lecture."""

    def __init__(self, now: float = 0.0, *, step: float = 0.0) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> float:
        instant = self.now
        self.now += self.step
        return instant

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeClient:
    """Client httpx factice : note sa construction, ses options et sa fermeture."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


class ChallengeDetected(Exception):
    """Un défi anti-bot : l'adaptateur le déclare par `status`."""

    status = "challenge"


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
        fuel="diesel",
        gear="auto",
    )


class FakeSource:
    """Source factice : rend des `Page` fabriquées, ne touche jamais au réseau."""

    source = "autoscout24"

    def __init__(self, pages: list[Page], *, error: BaseException | None = None) -> None:
        self.pages = pages
        self.error = error
        self.searches: list[tuple[Criteria, int]] = []
        self.brand_calls = 0
        self.on_search = None  # callback(criteria, page_number) avant de rendre

    @property
    def search_count(self) -> int:
        return len(self.searches)

    async def search(self, criteria: Criteria, page_number: int) -> Page:
        self.searches.append((criteria, page_number))
        if self.on_search is not None:
            self.on_search(criteria, page_number)
        if self.error is not None:
            raise self.error
        if page_number > len(self.pages):
            return Page(listings=[], page_number=page_number)
        return self.pages[page_number - 1]

    async def brands(self) -> list[dict]:
        self.brand_calls += 1
        if self.error is not None:
            raise self.error
        return [{"value": "renault", "label": "Renault", "models": [{"value": "clio", "label": "Clio"}]}]


def install(tmp_path, source: FakeSource, *, clock=None, db_path=None) -> FakeMcp:
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=db_path or (tmp_path / "corpus.db"),
        source_factory=lambda _name: source,
        clock=clock or FakeClock(),
    )
    return mcp


def harvest(mcp: FakeMcp, criteria: dict | None = None, budget_s: int | None = None) -> dict:
    # `budget_s=None` laisse l'outil prendre celui de la configuration, comme un
    # client qui n'en passe pas.
    return asyncio.run(mcp.tools["harvest"](criteria or {}, budget_s))


def brands(mcp: FakeMcp, source: str = "autoscout24") -> dict:
    return asyncio.run(mcp.tools["list_brands"](source))


# --- déclaration ---------------------------------------------------------------


def test_register_declares_exactly_the_three_tools_of_this_lot(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    assert sorted(mcp.tools) == ["harvest", "list_brands", "offers"]


def test_importing_the_tools_loads_no_source_adapter():
    # Aucun site n'est connu au chargement : l'adaptateur n'est importé que
    # dans la fabrique par défaut, donc au premier appel qui en a besoin.
    code = (
        "import sys, mcp_car_compare.tools\n"
        "print(','.join(sorted(m for m in sys.modules "
        "if m.startswith('mcp_car_compare.sources'))))\n"
    )
    # `pythonpath = ["src"]` de pyproject ne touche que `sys.path`, jamais
    # `os.environ` : le sous-processus ne verrait donc pas le paquet. Le chemin
    # est posé ici, sur cet appel seulement, pour que le test dise ce que le
    # paquet sait importer au lieu d'en hériter d'un réglage global.
    src = Path(__file__).resolve().parents[1] / "src"
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(src)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_a_harvest_opens_one_http_client_for_the_whole_run_and_closes_it(tmp_path, monkeypatch):
    # Deux sources, un seul client : un pool par source ferait autant de
    # sockets à fuir dès qu'une source lève.
    created: list[FakeClient] = []

    def build(**kwargs: object) -> FakeClient:
        client = FakeClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(tools.httpx, "AsyncClient", build)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    result = harvest(mcp, {"sources": ["autohero", "lacentrale"]})
    assert [row["status"] for row in result["coverage"]] == ["error", "error"]
    assert len(created) == 1
    assert created[0].closed is True
    assert created[0].kwargs == {"follow_redirects": False, "timeout": tools.HTTP_TIMEOUT_S}
    mcp.tools["offers"](result["job_id"], None, 25)
    assert len(created) == 1  # `offers` lit la base, il n'ouvre rien


# --- list_brands ---------------------------------------------------------------


def test_list_brands_has_no_coverage_and_harvests_nothing(tmp_path):
    source = FakeSource([Page(listings=[listing(1)])])
    mcp = install(tmp_path, source)
    result = brands(mcp)
    assert sorted(result) == ["brands", "error", "source"]
    assert result["brands"][0]["value"] == "renault"
    assert result["error"] == ""  # pas d'erreur muette, pas de raison inventée
    assert source.brand_calls == 1
    assert source.search_count == 0  # aucune récolte


def test_list_brands_renders_a_site_failure_instead_of_raising(tmp_path):
    # Un outil de catalogue ne doit pas lever : « le site est tombé » et « ce
    # site ne vend aucune marque » ne peuvent pas se ressembler.
    mcp = install(tmp_path, FakeSource([], error=SourceError("503 Service Unavailable")))
    result = brands(mcp)
    assert (result["brands"], result["source"]) == ([], "autoscout24")
    assert "503" in result["error"]


def test_list_brands_renders_a_changed_structure_instead_of_raising(tmp_path):
    mcp = install(tmp_path, FakeSource([], error=StructureChanged("taxonomie absente")))
    assert "taxonomie absente" in brands(mcp)["error"]


def test_list_brands_renders_a_source_without_an_adapter_instead_of_raising(tmp_path):
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    result = brands(mcp, "autohero")
    assert result["brands"] == []
    assert "hors périmètre" in result["error"]


def test_list_brands_refuses_an_unknown_source(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    with pytest.raises(ValueError, match="source inconnue"):
        brands(mcp, "leboncoin")


# --- harvest : ce qu'il rend ---------------------------------------------------


def test_harvest_returns_a_job_its_coverage_and_an_entry_cursor(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(3)], announced_total=3)])
    mcp = install(tmp_path, source)
    result = harvest(mcp, {"pages_max": 2})
    assert set(result) == {"job_id", "coverage", "cursor"}
    assert len(result["coverage"]) == 1
    row = result["coverage"][0]
    assert set(row) == {
        "source",
        "market",
        "status",
        "announced_total",
        "read",
        "unique",
        "dropped_duplicates",
        "truncated",
        "reason",
    }
    assert (row["status"], row["read"], row["unique"], row["dropped_duplicates"]) == ("ok", 3, 3, 0)
    assert row["announced_total"] == 3
    assert row["truncated"] is False
    entry = decode(result["cursor"])
    assert (entry.job_id, entry.offset, entry.sort) == (result["job_id"], 0, "price_asc")


def test_harvest_stops_when_the_site_runs_out_of_pages(tmp_path):
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (row["status"], row["read"], row["truncated"]) == ("ok", 1, False)
    assert row["reason"] == ""


def test_harvest_says_when_pages_max_cut_it_short(tmp_path):
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[listing(2)])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 2})["coverage"][0]
    assert row["status"] == "ok"
    assert row["truncated"] is True
    assert "pages_max=2" in row["reason"]


# --- harvest : budget ----------------------------------------------------------


def test_harvest_never_exceeds_its_budget(tmp_path):
    clock = FakeClock()
    pages = [Page(listings=[listing(page * 20 + index) for index in range(20)]) for page in range(3)]
    source = FakeSource(pages)
    source.on_search = lambda _criteria, _page: clock.advance(40)  # 40 s par page
    mcp = install(tmp_path, source, clock=clock)
    row = harvest(mcp, {"pages_max": 5}, 60)["coverage"][0]
    assert row["status"] == "capped"
    assert row["read"] == 40  # deux pages tiennent dans 60 s, pas trois
    assert row["truncated"] is True
    assert "budget de 60 s" in row["reason"]
    assert source.search_count == 2


def test_a_source_cut_before_its_first_page_reads_zero(tmp_path):
    source = FakeSource([Page(listings=[listing(1)])])
    mcp = install(tmp_path, source, clock=FakeClock(step=600))
    row = harvest(mcp, {"pages_max": 5}, 60)["coverage"][0]
    assert (row["status"], row["read"], row["unique"]) == ("capped", 0, 0)
    assert row["truncated"] is True
    assert source.search_count == 0


def test_the_budget_is_per_source_not_global(tmp_path):
    clock = FakeClock()
    pages = [Page(listings=[listing(page * 20 + index) for index in range(20)]) for page in range(3)]
    sources = {"autoscout24": FakeSource(pages), "autohero": FakeSource(pages)}
    for source in sources.values():
        source.on_search = lambda _criteria, _page: clock.advance(40)
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=tmp_path / "corpus.db",
        source_factory=lambda name: sources[name],
        clock=clock,
    )
    coverage = harvest(mcp, {"sources": ["autoscout24", "autohero"], "pages_max": 5}, 60)["coverage"]
    # Un budget global aurait laissé la deuxième source à zéro.
    assert [(row["source"], row["read"], row["status"]) for row in coverage] == [
        ("autoscout24", 40, "capped"),
        ("autohero", 40, "capped"),
    ]


def test_a_slow_page_does_not_push_the_harvest_past_its_budget(tmp_path):
    # Seul test qui attend vraiment : il prouve que l'attente en cours est
    # bornée, pas seulement le passage d'une page à l'autre. Aucune assertion
    # ne porte sur une durée mesurée.
    class SlowSource(FakeSource):
        async def search(self, criteria: Criteria, page_number: int) -> Page:
            await asyncio.sleep(5)
            return Page(listings=[listing(1)])

    mcp = install(tmp_path, SlowSource([]))
    row = harvest(mcp, {"pages_max": 5}, 1)["coverage"][0]
    assert row["status"] == "capped"
    assert row["read"] == 0
    assert "pendant la page 1" in row["reason"]


# --- harvest : échecs de source ------------------------------------------------


def test_a_source_failure_is_a_status_not_an_empty_result(tmp_path):
    source = FakeSource([], error=SourceError("503 Service Unavailable"))
    mcp = install(tmp_path, source)
    row = harvest(mcp)["coverage"][0]
    assert row["status"] == "error"
    assert "503" in row["reason"]
    assert row["read"] == 0


def test_a_challenge_declared_by_the_adapter_is_reported_as_challenge(tmp_path):
    source = FakeSource([], error=ChallengeDetected("défi DataDome"))
    mcp = install(tmp_path, source)
    row = harvest(mcp)["coverage"][0]
    assert row["status"] == "challenge"
    assert "DataDome" in row["reason"]


def test_a_source_without_an_adapter_is_reported_not_ignored(tmp_path):
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    row = harvest(mcp, {"sources": ["autohero"]})["coverage"][0]
    assert row["status"] == "error"
    assert "hors périmètre" in row["reason"]


def test_harvest_counts_duplicates_as_dropped(tmp_path):
    source = FakeSource([Page(listings=[listing(1), listing(2)]), Page(listings=[listing(2), listing(3)])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (row["read"], row["unique"], row["dropped_duplicates"]) == (4, 3, 1)


# --- harvest : critères --------------------------------------------------------


def test_harvest_asks_one_market_at_a_time(tmp_path):
    class MarketSource(FakeSource):
        async def search(self, criteria: Criteria, page_number: int) -> Page:
            self.searches.append((criteria, page_number))
            if page_number > 1:
                return Page(listings=[])
            market = criteria.markets[0]
            return Page(listings=[listing(index, market=market) for index in range(2)])

    source = MarketSource([])
    mcp = install(tmp_path, source)
    coverage = harvest(mcp, {"markets": ["fr", "de"]})["coverage"]
    assert [(row["source"], row["market"], row["read"]) for row in coverage] == [
        ("autoscout24", "fr", 2),
        ("autoscout24", "de", 2),
    ]
    # Aucun appel ne mélange deux marchés : sans ça, les compteurs de
    # `coverage` ne pourraient plus être attribués à un marché.
    assert {criteria.markets for criteria, _page in source.searches} == {("fr",), ("de",)}
    assert {criteria.sources for criteria, _page in source.searches} == {("autoscout24",)}


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ({"kilometrage": 1000}, "inconnu"),
        ({"sort": "moins_cher"}, "tri inconnu"),
        ({"fuel": "gpl"}, "carburant inconnu"),
        ({"gear": "semi"}, "boîte inconnue"),
        ({"markets": []}, "marchés : liste non vide"),
        ({"markets": ["it"]}, "marché hors vocabulaire"),
        ({"sources": ["leboncoin"]}, "source hors vocabulaire"),
        ({"pages_max": 0}, "pages_max"),
        ({"year_min": "2020"}, "year_min"),
    ],
)
def test_harvest_refuses_criteria_it_would_have_to_guess(tmp_path, criteria, message):
    source = FakeSource([Page(listings=[])])
    mcp = install(tmp_path, source)
    with pytest.raises(ValueError, match=message):
        harvest(mcp, criteria)
    assert source.search_count == 0  # rien n'est parti sur le réseau


def test_harvest_refuses_a_budget_below_one_second(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    with pytest.raises(ValueError, match="budget_s"):
        harvest(mcp, {}, 0)


# --- offers -------------------------------------------------------------------


def test_offers_pages_without_duplicates_over_the_whole_corpus(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(7)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    seen: list[str] = []
    page = mcp.tools["offers"](result["job_id"], None, 3)
    for _ in range(5):  # plus de pages que nécessaire : on doit s'arrêter
        seen.extend(item["source_id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        page = mcp.tools["offers"](result["job_id"], page["next_cursor"], 3)
    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert page["coverage"]["listings_distinct"] == 7


def test_offers_starts_at_the_first_page_with_an_empty_result(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    result = harvest(mcp)
    page = mcp.tools["offers"](result["job_id"], result["cursor"], 25)
    assert (page["items"], page["next_cursor"], page["has_more"]) == ([], None, False)


def test_offers_only_reads_the_listings_of_its_own_harvest(tmp_path):
    # `job_id` désigne une récolte, pas le corpus (PLAN.md §5) : deux récoltes
    # qui se recouvrent ne se mélangent pas dans une page.
    mcp = install(tmp_path, FakeSource([Page(listings=[listing(index) for index in range(3)])]))
    first = harvest(mcp)
    mcp = install(tmp_path, FakeSource([Page(listings=[listing(index) for index in range(2, 5)])]))
    second = harvest(mcp)
    ids = {
        job: {item["source_id"] for item in mcp.tools["offers"](job, None, 25)["items"]}
        for job in (first["job_id"], second["job_id"])
    }
    assert ids[first["job_id"]] == {"fr-000", "fr-001", "fr-002"}
    assert ids[second["job_id"]] == {"fr-002", "fr-003", "fr-004"}


def test_offers_refuses_a_harvest_it_does_not_know(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    with pytest.raises(UnknownJobError):
        mcp.tools["offers"]("jamais-vu", None, 25)


def test_offers_refuses_a_cursor_from_another_harvest(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    first = harvest(mcp)
    second = harvest(mcp)
    with pytest.raises(CursorError, match="récolte"):
        mcp.tools["offers"](first["job_id"], second["cursor"], 2)


def test_offers_refuses_a_cursor_taken_with_another_sort(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    entry = decode(result["cursor"])
    forged = encode(
        Cursor(
            job_id=entry.job_id,
            offset=2,
            sort="price_desc",
            criteria_hash=entry.criteria_hash,
        )
    )
    with pytest.raises(CursorError, match="tri"):
        mcp.tools["offers"](result["job_id"], forged, 2)


def test_offers_refuses_a_cursor_of_other_criteria(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    entry = decode(result["cursor"])
    forged = encode(
        Cursor(job_id=entry.job_id, offset=0, sort=entry.sort, criteria_hash="0000000000000000")
    )
    with pytest.raises(CursorError, match="critères"):
        mcp.tools["offers"](result["job_id"], forged, 2)


def test_offers_refuses_a_garbled_cursor(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    result = harvest(mcp)
    with pytest.raises(CursorError, match="illisible"):
        mcp.tools["offers"](result["job_id"], "pas-un-curseur", 25)


@pytest.mark.parametrize("limit", [0, -1, 101, True, "25"])
def test_offers_refuses_a_limit_out_of_bounds(tmp_path, limit):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    result = harvest(mcp)
    with pytest.raises(ValueError, match="limit"):
        mcp.tools["offers"](result["job_id"], None, limit)


def test_offers_never_builds_a_source(tmp_path):
    factory_calls: list[str] = []

    def factory(name: str):
        factory_calls.append(name)
        return FakeSource([Page(listings=[listing(1)])])

    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db", source_factory=factory, clock=FakeClock())
    result = harvest(mcp)
    factory_calls.clear()
    mcp.tools["offers"](result["job_id"], None, 25)
    assert factory_calls == []  # `offers` lit la base, il ne récolte pas


# --- configuration -------------------------------------------------------------


def test_the_default_budget_comes_from_the_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_BUDGET_S", "7")
    mcp = install(tmp_path, FakeSource([Page(listings=[listing(1)])]), clock=FakeClock(step=600))
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert row["status"] == "capped"
    assert row["read"] == 0
    assert "budget de 7 s" in row["reason"]  # 7 vient de la configuration, pas 60


def test_the_default_pages_max_comes_from_the_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_PAGES_MAX", "1")
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[listing(2)])])
    mcp = install(tmp_path, source, clock=lambda: 0.0)
    row = harvest(mcp, {})["coverage"][0]
    assert row["read"] == 1
    assert "pages_max=1" in row["reason"]


def test_an_explicit_budget_beats_the_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_BUDGET_S", "7")
    mcp = install(tmp_path, FakeSource([Page(listings=[listing(1)])]), clock=FakeClock(step=600))
    row = harvest(mcp, {"pages_max": 5}, 99)["coverage"][0]
    assert "budget de 99 s" in row["reason"]


def test_a_pages_max_in_the_criteria_beats_the_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_PAGES_MAX", "1")
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[listing(2)])])
    mcp = install(tmp_path, source, clock=lambda: 0.0)
    row = harvest(mcp, {"pages_max": 2})["coverage"][0]
    assert row["read"] == 2
    assert "pages_max=2" in row["reason"]


def test_the_corpus_path_comes_from_the_configuration_when_not_passed(tmp_path, monkeypatch):
    # C'est le câblage de production : `server.py` appelle `register(server)`
    # sans chemin, donc la base doit être celle que dit `load_config()`.
    monkeypatch.setenv("MCC_DB", str(tmp_path / "from-config.db"))
    mcp = FakeMcp()
    tools.register(
        mcp,
        source_factory=lambda _name: FakeSource([Page(listings=[listing(1)])]),
        clock=lambda: 0.0,
    )
    result = harvest(mcp, {})
    assert (tmp_path / "from-config.db").exists()
    assert len(mcp.tools["offers"](result["job_id"], None, 25)["items"]) == 1
