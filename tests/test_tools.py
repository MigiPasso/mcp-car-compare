"""Les quatre outils MCP : bornés, bavards sur ce qu'ils n'ont pas lu, hors réseau.

Aucun test n'ouvre de connexion : la source est factice et injectée, et
l'horloge est pilotée à la main — aucune assertion ne dépend d'une durée réelle
(à une exception près, signalée dans son test).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from mcp_car_compare import tools
from mcp_car_compare.cursor import Cursor, CursorError, decode, encode
from mcp_car_compare.models import (
    MAX_BUDGET_S,
    MAX_PAGES,
    MAX_PAGES_ANY,
    MAX_PAGES_BY_SOURCE,
    Criteria,
    Listing,
    Page,
    SourceError,
    StructureChanged,
)
from mcp_car_compare.store import Store, UnknownJobError


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
    """Client httpx factice : note sa construction, ses options et sa fermeture.

    `stream` échoue comme un site injoignable : c'est ce qui permet à un test
    d'exercer le chemin réseau réel de l'adaptateur sans ouvrir de connexion.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True

    def stream(self, *_args: object, **_kwargs: object):
        raise httpx.ConnectError("connexion refusée", request=httpx.Request("GET", "https://example.test"))


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
        # La forme réelle de l'adaptateur (D6) : une marque et son libellé, sans
        # `models` — la taxonomie ne les porte que sur une page filtrée sur elle.
        return [{"value": "13", "label": "BMW"}]


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


def brands(mcp: FakeMcp, source: str = "autoscout24", market: str = "fr") -> dict:
    return asyncio.run(mcp.tools["list_brands"](source, market))


# --- déclaration ---------------------------------------------------------------


def test_register_declares_exactly_the_four_tools_of_this_lot(tmp_path):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    assert sorted(mcp.tools) == ["analyze_models", "harvest", "list_brands", "offers"]


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
    # Deux marchés, un seul client : un pool par marché ferait autant de sockets
    # à fuir dès qu'une requête lève.
    created: list[FakeClient] = []

    def build(**kwargs: object) -> FakeClient:
        client = FakeClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(tools.httpx, "AsyncClient", build)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    result = harvest(mcp, {"markets": ["fr", "de"]})
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
    assert sorted(result) == ["brands", "error", "market", "source"]
    assert result["brands"][0]["value"] == "13"
    assert "models" not in result["brands"][0]  # D6 : jamais rempli en v1
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


def test_list_brands_refuses_an_unknown_market(tmp_path):
    # `market` est un argument de l'outil : un marché inconnu construirait une
    # adresse qui répond 404 et rendrait « aucune marque » sans erreur.
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    with pytest.raises(ValueError, match="marché inconnu"):
        brands(mcp, "autoscout24", market="it")


def brands_page() -> str:
    """Page minimale portant une taxonomie, dans la forme que lit l'adaptateur.

    Écrite ici plutôt que tirée d'une fixture : aucune capture de page
    `/lst?atype=C` n'existe dans le dépôt, et un fragment fabriqué ne doit pas se
    faire passer pour une page du site.
    """
    page_props = {
        "numberOfResults": 465,
        "listings": [],
        "taxonomy": {"makesSorted": [{"label": "BMW", "value": 13}]},
    }
    payload = json.dumps({"props": {"pageProps": page_props}})
    return (
        '<!DOCTYPE html><html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{payload}</script></head><body></body></html>"
    )


def recording_client(monkeypatch, handler) -> type[httpx.AsyncClient]:
    """Un VRAI client `httpx` (options retenues) sur un transport factice.

    C'est le seul moyen d'éprouver la fabrique réelle des sources — donc le
    constructeur d'adresse de l'adaptateur — sans toucher au réseau.
    """

    class RecordingClient(httpx.AsyncClient):
        """Vrai client httpx, options retenues, transport factice."""

        instances: list["RecordingClient"] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]
            RecordingClient.instances.append(self)

    monkeypatch.setattr(tools.httpx, "AsyncClient", RecordingClient)
    return RecordingClient


def test_harvest_takes_the_labels_offers_renders(tmp_path, monkeypatch):
    # Le seul vocabulaire de `harvest` est le libellé : la marque et la gamme se
    # filtrent par le CHEMIN de l'adresse, dérivé du libellé qu'`offers` écrit
    # dans `make` et `group_label`. Ce test traverse le constructeur d'adresse
    # réel (fabrique réelle, transport factice) : sans lui, un identifiant
    # numérique continuerait de traverser `harvest` sans erreur et ne filtrerait
    # rien, ce qui est exactement le défaut mesuré (8 320 annonces BMW pour 7
    # Série 3).
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=brands_page())

    recording_client(monkeypatch, handler)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    # Le libellé de `group_label` et le `group` normalisé désignent la MÊME
    # gamme : les deux formes mènent au même segment, donc au même filtre.
    harvest(mcp, {"make": "BMW", "model": "Série 3", "pages_max": 1})
    harvest(mcp, {"make": "BMW", "model": "serie 3", "pages_max": 1})
    assert [(request.url.host, request.url.path) for request in requests] == [
        ("www.autoscout24.fr", "/lst/bmw/serie-3")
    ] * 2
    assert requests[0].url.params["atype"] == "C"
    assert "mmmv" not in requests[0].url.params


def test_a_404_label_is_an_error_status_naming_the_slug(tmp_path, monkeypatch):
    # Une gamme inconnue du site ne doit jamais rendre zéro annonce : un 0 se
    # lirait comme un marché vide, alors que c'est le libellé qui est faux. Le
    # statut est `error`, et le message nomme le segment que le site a refusé.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>404</html>")

    recording_client(monkeypatch, handler)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    row = harvest(mcp, {"make": "BMW", "model": "Série 3", "pages_max": 1})["coverage"][0]
    assert row["status"] == "error"
    assert "« Série 3 »" in row["reason"] and "(404)" in row["reason"]
    assert (row["read"], row["unique"]) == (0, 0)


def test_list_brands_traverses_the_real_factory_for_the_requested_market(tmp_path, monkeypatch):
    """`list_brands` est le seul outil dont les arguments ne portent pas le
    marché de la récolte : c'est `_default_source` qui décide du domaine.

    Ce test le traverse pour de vrai (adaptateur réel, transport factice). Sans
    lui, `market="de"` pourrait lire `.fr` sans que rien ne le dise, et les
    libellés rendus seraient ceux de l'autre marché.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=brands_page())

    RecordingClient = recording_client(monkeypatch, handler)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    result = brands(mcp, "autoscout24", market="de")
    assert result["error"] == ""
    assert result["market"] == "de"
    assert result["brands"] == [{"value": "13", "label": "BMW"}]
    # Les deux options du client d'un appel : aucune redirection suivie (une
    # .de redirigée serait lue comme .de, et ses annonces porteraient le mauvais
    # marché), et un délai borné.
    assert RecordingClient.instances[0].kwargs == {
        "follow_redirects": False,
        "timeout": tools.HTTP_TIMEOUT_S,
    }
    assert (requests[0].url.host, requests[0].url.path) == ("www.autoscout24.de", "/lst")
    assert requests[0].url.params["atype"] == "C"
    assert requests[0].headers["accept-language"] == "de-DE,de;q=0.9"


# --- harvest : ce qu'il rend ---------------------------------------------------


def test_harvest_returns_a_job_its_coverage_and_an_entry_cursor(tmp_path):
    source = FakeSource([Page(listings=[listing(index) for index in range(3)], announced_total=3)])
    mcp = install(tmp_path, source)
    result = harvest(mcp, {"pages_max": 2})
    assert set(result) == {"job_id", "coverage", "cursor", "warnings", "resumes"}
    # Tout a été lu : aucune source n'a de jeton de reprise à rendre.
    assert result["resumes"] == []
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
        "dropped_invalid",
        "truncated",
        "reason",
    }
    assert (row["status"], row["read"], row["unique"], row["dropped_duplicates"]) == ("ok", 3, 3, 0)
    assert row["announced_total"] == 3
    assert row["truncated"] is False
    entry = decode(result["cursor"])
    # Le tri du curseur est celui du contrat : `age_desc` par défaut, pas le
    # prix croissant qui sert les annonces à 1 € en premier chez AutoScout24.
    assert (entry.job_id, entry.offset, entry.sort) == (result["job_id"], 0, "age_desc")


def test_harvest_stops_when_the_site_runs_out_of_pages(tmp_path):
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (row["status"], row["read"], row["truncated"]) == ("ok", 1, False)
    assert row["reason"] == ""


def test_the_stop_condition_compares_distinct_listings_not_the_read_counter(tmp_path):
    # Le banc d'AutoScout24 rend 20 annonces pour 19 identifiants uniques :
    # `read` atteint donc le total annoncé alors qu'une annonce annoncée n'a
    # jamais été lue. Une requête qui annonce 20 résultats s'arrêtait là,
    # `status="ok"` et `truncated=False` : une annonce manquait, et rien ne le
    # disait. La complétude se juge sur les annonces DISTINCTES.
    page_one = Page(
        listings=[listing(index) for index in range(19)], rendered=20, announced_total=20
    )
    page_two = Page(
        listings=[listing(index) for index in range(19, 38)], rendered=20, announced_total=20
    )
    mcp = install(tmp_path, FakeSource([page_one, page_two]))
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    # La page 2 a bien été demandée : 38 annonces distinctes, 40 lues.
    assert (row["read"], row["unique"], row["dropped_duplicates"]) == (40, 38, 2)
    assert row["truncated"] is False


def test_an_empty_page_after_duplicates_still_reports_truncation(tmp_path):
    # Même comptabilité, autre sortie de boucle : le site s'arrête de rendre des
    # annonces après une page dont `read` a déjà atteint le total annoncé, mais
    # dont les annonces distinctes, elles, ne l'atteignent pas. Comparer `read`
    # au total annoncé laissait `truncated=False` : une lecture incomplète se
    # lisait comme une fin de marché.
    page_one = Page(
        listings=[listing(index) for index in range(19)], rendered=20, announced_total=20
    )
    mcp = install(tmp_path, FakeSource([page_one, Page(listings=[])]))
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (row["read"], row["unique"]) == (20, 19)
    assert row["truncated"] is True
    # Le message nomme l'écart et laisse la cause ouverte : « plafond du site »
    # était faux quand l'arrêt venait de `pages_max` (mesuré : 71/100 avec
    # pages_max=3, 100/100 avec pages_max=10). Ce sont les deux nombres qui
    # disent qu'il manque quelque chose.
    assert "lecture incomplète" in row["reason"]
    assert "19 annonce(s) distincte(s)" in row["reason"]
    assert "20" in row["reason"]


def test_harvest_says_when_pages_max_cut_it_short(tmp_path):
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[listing(2)])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 2})["coverage"][0]
    assert row["status"] == "ok"
    assert row["truncated"] is True
    assert "pages_max=2" in row["reason"]


def test_un_arret_sur_pages_max_sans_total_annonce_dit_que_le_total_manque(tmp_path):
    # Décision D1 : `truncated=true` reste juste quand le site n'annonce aucun
    # total (on ne sait pas s'il restait des annonces), mais le `reason` doit
    # dire lequel des deux chiffres manque. Sinon « plafond atteint après 2
    # annonce(s) » se lirait comme une fin de marché.
    source = FakeSource([Page(listings=[listing(1)]), Page(listings=[listing(2)])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 2})["coverage"][0]
    assert row["announced_total"] is None
    assert row["truncated"] is True
    assert "inconnu" in row["reason"]  # « total annoncé inconnu »


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


def test_the_budget_reason_counts_distinct_listings_not_the_read_counter(tmp_path):
    # Le `reason` est le seul chiffre qu'un client peut comparer au total annoncé
    # par le site. S'il interpole `read` (doublons compris) au lieu des annonces
    # distinctes, il annonce plus que le marché : revenir à `{read}` ici laissait
    # les 227 tests verts, donc ce cas est le seul à certifier l'interpolation.
    # Mesuré sur une page qui rend 20 annonces dont une en double.
    clock = FakeClock()
    page = Page(listings=[listing(index) for index in range(19)], rendered=20)
    source = FakeSource([page, Page(listings=[listing(100)])])
    source.on_search = lambda _criteria, _page: clock.advance(40)
    mcp = install(tmp_path, source, clock=clock)
    # Budget 35 s : la page 1 coûte 40 s, donc la boucle s'arrête AVANT la page 2
    # sur la branche du budget, avec une seule page lue et un doublon dedans.
    row = harvest(mcp, {"pages_max": 5}, 35)["coverage"][0]
    assert row["status"] == "capped"
    assert (row["read"], row["unique"], row["dropped_duplicates"]) == (20, 19, 1)
    assert "19 annonce(s) distincte(s) (20 lue(s))" in row["reason"]


def test_a_source_cut_before_its_first_page_reads_zero(tmp_path):
    source = FakeSource([Page(listings=[listing(1)])])
    mcp = install(tmp_path, source, clock=FakeClock(step=600))
    row = harvest(mcp, {"pages_max": 5}, 60)["coverage"][0]
    assert (row["status"], row["read"], row["unique"]) == ("capped", 0, 0)
    assert row["truncated"] is True
    assert source.search_count == 0


def test_the_budget_is_per_source_not_global(tmp_path):
    # Deux sources, une horloge : 40 s par page, budget de 60 s. Un budget
    # global aurait laissé la deuxième source à zéro, puisque la première a
    # déjà consommé 60 s à elle seule.
    #
    # `_harvest_sources` est appelé directement : ce lot ne sert qu'une source,
    # donc le parsage des critères ne laisse plus passer un `Criteria` à deux
    # sources. La propriété testée, elle, reste dans le code et c'est elle qui
    # garde la porte ouverte au jour où une deuxième source entre.
    clock = FakeClock()
    pages = [Page(listings=[listing(page * 20 + index) for index in range(20)]) for page in range(3)]
    sources = {"autoscout24": FakeSource(pages), "autohero": FakeSource(pages)}
    for source in sources.values():
        source.on_search = lambda _criteria, _page: clock.advance(40)
    with Store(tmp_path / "corpus.db") as store:
        coverage = asyncio.run(
            tools._harvest_sources(
                store,
                "job-budget",
                Criteria(sources=("autoscout24", "autohero"), pages_max=5),
                budget=60,
                criteria_hash="0123456789abcdef",
                make_source=lambda name: sources[name],
                clock=clock,
            )
        )
    assert [(item.coverage.source, item.coverage.read, item.coverage.status) for item in coverage] == [
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


def test_a_source_outside_this_lot_is_refused_at_parse_time(tmp_path):
    # `autohero` est au contrat mais hors de ce lot : le refus tombe avant la
    # première requête et nomme la source. L'accepter pour rendre
    # `status="error"` une source plus loin ferait passer un périmètre pour une
    # panne du site, et l'appelant chercherait la panne du mauvais côté.
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")  # fabrique réelle
    with pytest.raises(ValueError, match="hors périmètre"):
        harvest(mcp, {"sources": ["autohero"]})
    assert not (tmp_path / "corpus.db").exists()  # rien n'a même ouvert le corpus


def test_harvest_counts_duplicates_as_dropped(tmp_path):
    source = FakeSource([Page(listings=[listing(1), listing(2)]), Page(listings=[listing(2), listing(3)])])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (row["read"], row["unique"], row["dropped_duplicates"]) == (4, 3, 1)


def test_an_intra_page_duplicate_counts_as_dropped(tmp_path):
    # Une page qui REND une annonce en double : l'adaptateur n'en garde qu'une
    # (`page.listings`), mais `page.rendered` en compte deux. L'écart entre les
    # deux est le doublon intra-page, et il doit entrer dans
    # `dropped_duplicates` — sans quoi l'identité `read == unique +
    # dropped_duplicates + dropped_invalid` ne se referme plus, et l'annonce
    # comptée deux fois disparaîtrait des compteurs sans que rien ne le dise.
    source = FakeSource([Page(listings=[listing(1), listing(2)], rendered=3, announced_total=2)])
    mcp = install(tmp_path, source)
    row = harvest(mcp, {"pages_max": 5})["coverage"][0]
    assert (
        row["read"],
        row["unique"],
        row["dropped_duplicates"],
        row["dropped_invalid"],
    ) == (3, 2, 1, 0)
    assert row["read"] == row["unique"] + row["dropped_duplicates"] + row["dropped_invalid"]
    assert row["truncated"] is False


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


def test_harvest_refuses_a_budget_above_the_server_ceiling(tmp_path):
    # La borne est une propriété du serveur, pas un objectif : au-delà de dix
    # minutes le client a abandonné depuis longtemps. Sans ce refus, un appel
    # `budget_s: 10**9` autoriserait une récolte à durer indéfiniment.
    source = FakeSource([Page(listings=[])])
    mcp = install(tmp_path, source)
    with pytest.raises(ValueError, match=str(MAX_BUDGET_S)):
        harvest(mcp, {}, MAX_BUDGET_S + 1)
    assert source.search_count == 0  # rien n'est parti sur le réseau


def test_harvest_refuses_a_pages_max_above_the_site_ceiling(tmp_path):
    # MAX_PAGES est le plafond mesuré d'AutoScout24 (20 pages, 400 annonces) :
    # demander plus boucle pour rien. Le message nomme la borne pour que
    # l'appelant sache quoi corriger.
    source = FakeSource([Page(listings=[])])
    mcp = install(tmp_path, source)
    with pytest.raises(ValueError, match=str(MAX_PAGES)):
        harvest(mcp, {"pages_max": MAX_PAGES + 1})
    assert source.search_count == 0


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


def test_offers_refuses_a_negative_offset(tmp_path):
    # Le curseur n'est pas scellé : un offset forgé négatif serait lu par SQLite
    # comme 0, donc il relirait la première page en la faisant passer pour la
    # suivante. Il est refusé — au décodage du curseur, avant même la garde
    # d'`offers` ; mesuré, le message est « curseur illisible : offset -1 ».
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    entry = decode(result["cursor"])
    forged = encode(
        Cursor(
            job_id=entry.job_id,
            offset=-1,
            sort=entry.sort,
            criteria_hash=entry.criteria_hash,
        )
    )
    with pytest.raises(CursorError, match="illisible.*offset -1"):
        mcp.tools["offers"](result["job_id"], forged, 2)


def test_offers_refuses_an_offset_past_the_corpus(tmp_path):
    # Un offset au-delà du corpus rendrait une page vide, qui se lirait comme
    # « la fin de la récolte ». La borne est nommée dans le message.
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    entry = decode(result["cursor"])
    forged = encode(
        Cursor(
            job_id=entry.job_id,
            offset=5,
            sort=entry.sort,
            criteria_hash=entry.criteria_hash,
        )
    )
    with pytest.raises(CursorError, match="4 annonce"):
        mcp.tools["offers"](result["job_id"], forged, 2)


def test_offers_at_the_corpus_bound_renders_an_empty_last_page(tmp_path):
    # Frontière exacte : un offset égal au nombre d'annonces n'est pas une
    # erreur, c'est la page qui suit la dernière. Il doit rendre vide, avec
    # `has_more` faux — sinon un client qui pagine jusqu'au bout reçoit une
    # erreur au lieu d'une fin.
    source = FakeSource([Page(listings=[listing(index) for index in range(4)])])
    mcp = install(tmp_path, source)
    result = harvest(mcp)
    entry = decode(result["cursor"])
    forged = encode(
        Cursor(
            job_id=entry.job_id,
            offset=4,
            sort=entry.sort,
            criteria_hash=entry.criteria_hash,
        )
    )
    page = mcp.tools["offers"](result["job_id"], forged, 2)
    assert page["items"] == []
    assert page["has_more"] is False


@pytest.mark.parametrize("limit", [0, -1, 101, True, "25"])
def test_offers_refuses_a_limit_out_of_bounds(tmp_path, limit):
    mcp = install(tmp_path, FakeSource([Page(listings=[])]))
    result = harvest(mcp)
    with pytest.raises(ValueError, match="limit"):
        mcp.tools["offers"](result["job_id"], None, limit)


def test_offers_reads_only_the_db_variable(tmp_path, monkeypatch):
    # `offers` n'a ni page ni budget à régler : une `MCC_PAGES_MAX` hors bornes
    # fait lever `load_config()`, et c'est voulu pour `harvest` (c'est un
    # plafond de récolte). Mais `offers` ne récolte rien : hériter de ce refus
    # rendrait la lecture d'un corpus déjà écrit impossible à cause d'une
    # variable qui ne concerne aucune page lue.
    monkeypatch.setenv("MCC_DB", str(tmp_path / "from-env.db"))
    mcp = FakeMcp()
    tools.register(
        mcp,
        source_factory=lambda _name: FakeSource(
            [Page(listings=[listing(1), listing(2)])]
        ),
        clock=lambda: 0.0,
    )
    result = harvest(mcp, {})
    # `MAX_PAGES_ANY` : 21 reste valide (LaCentrale monte à 30), donc la
    # variable doit dépasser la borne la plus large pour être refusée.
    monkeypatch.setenv("MCC_PAGES_MAX", str(MAX_PAGES_ANY + 1))
    page = mcp.tools["offers"](result["job_id"], None, 25)
    assert [item["source_id"] for item in page["items"]] == ["fr-001", "fr-002"]
    assert page["coverage"]["listings_distinct"] == 2
    # Le refus existe toujours, et il reste celui de `harvest` : les deux
    # outils ne doivent pas être confondus sur ce point. Le message nomme la
    # SOURCE et son plafond (20 pour AutoScout24, 30 pour LaCentrale), plus la
    # variable d'environnement fautive : deux leviers différents, deux noms.
    with pytest.raises(ValueError, match="MCC_PAGES_MAX"):
        harvest(mcp, {})
    # La variable refusée tombe AVANT le parsage des critères : on la retire
    # pour éprouver le refus par source, qui est l'autre levier.
    monkeypatch.delenv("MCC_PAGES_MAX")
    with pytest.raises(ValueError, match="autoscout24 : 20"):
        harvest(mcp, {
            "sources": ["autoscout24"],
            "pages_max": MAX_PAGES_ANY + 1,
        })


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


def test_une_recolte_tronquee_previent_avec_le_jeton_de_reprise(tmp_path):
    # Le propriétaire l'a demandé explicitement : si la source annonce plus
    # d'annonces qu'on n'en a lu, l'appelant doit être prévenu — sinon il croit
    # avoir tout le marché — et le message doit dire QUEL levier tourner.
    #
    # Mesuré le 2026-11-28 : « relance avec pages_max=20 » conseillait un appel
    # qui expirait côté client (`Request timed out`) sans rien écrire en base.
    # Le levier est donc le jeton de reprise, et `pages_max=20` ne doit plus
    # apparaître — c'est l'action qui échoue que ce lot corrige.
    clock = FakeClock()
    source = FakeSource(
        [
            Page(listings=[listing(index) for index in range(20)], announced_total=500),
            Page(listings=[listing(index) for index in range(20, 40)], announced_total=500),
        ]
    )
    source.on_search = lambda _criteria, _page: clock.advance(40)  # 40 s par page
    mcp = install(tmp_path, source, clock=clock)
    result = harvest(mcp, {"pages_max": 5}, 60)
    (warning,) = result["warnings"]
    (reprise,) = result["resumes"]
    assert "autoscout24" in warning and "40 annonce(s) lue(s) sur 500" in warning
    assert f'resume="{reprise["token"]}"' in warning
    assert "pages_max=20" not in warning


def test_une_recolte_complete_ne_previent_de_rien(tmp_path):
    # Le pendant indispensable : un avertissement qui crie toujours ne prévient
    # plus de rien. Tout est lu, donc rien à dire.
    source = FakeSource([Page(listings=[listing(index) for index in range(3)], announced_total=3)])
    mcp = install(tmp_path, source)
    assert harvest(mcp, {"pages_max": 2})["warnings"] == []


def test_le_plafond_de_pages_depend_de_la_source_demandee(tmp_path):
    # Mesuré le 2026-11-28 : 21 pages étaient refusées pour TOUTES les sources,
    # avec le message « plafond mesuré du site » — alors que 20 est le plafond
    # d'AutoScout24, pas celui de LaCentrale, qui annonce 524 résultats (~22
    # pages). Une recherche Yaris s'est arrêtée à 441/524 pour cette raison.
    # Sans table par source, ce test rougit sur la première assertion.
    assert MAX_PAGES_BY_SOURCE["lacentrale"] > MAX_PAGES_BY_SOURCE["autoscout24"]
    assert MAX_PAGES_BY_SOURCE["autoscout24"] == MAX_PAGES

    # LaCentrale accepte ce qu'AutoScout24 refuse, et le message nomme la source.
    assert tools._parse_criteria({"sources": ["lacentrale"], "pages_max": MAX_PAGES + 1}, pages_max=5)
    with pytest.raises(ValueError, match="autoscout24 : 20"):
        tools._parse_criteria({"sources": ["autoscout24"], "pages_max": MAX_PAGES + 1}, pages_max=5)
