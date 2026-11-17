"""La route `/scrape` : les statuts et la forme, sans navigateur.

Ce fichier a besoin de FastAPI, que `Dockerfile.test` n'installe pas (les tests
d'une autre zone n'en ont pas besoin, et cette image est construite sans
`requirements-browser.txt`). Il est donc ignoré par la suite de l'image de test
et tourne dans l'image de production :

    docker build -t mcp-car-compare:test -f Dockerfile.test .
    docker run --rm mcp-car-compare:test                      # tout sauf ce fichier
    docker run --rm -v "$PWD":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \\
      mcp-car-compare:local sh -c "pip install -q pytest && python -m pytest -q"

`scrape_pages` est remplacé par une doublure : ce qu'on éprouve ici est la
traduction d'un résultat ou d'une panne en statut HTTP, pas la lecture du DOM
(qui a ses propres tests, sans navigateur, dans `test_browser_sidecar.py`).
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "fastapi",
    reason="la route n'est éprouvable qu'avec FastAPI (requirements-browser.txt)",
)

from fastapi.testclient import TestClient  # noqa: E402

from mcp_car_compare import browser_sidecar as sidecar  # noqa: E402

LISTING = "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT::208"

VEHICLE = {
    "source_id": "69116056",
    "url": "https://www.lacentrale.fr/auto-occasion-annonce-69116056.html",
    "make": "Peugeot",
    "group": "208",
    "group_label": "208",
    "engine": "1.2 PureTech 100ch",
    "trim": "Allure Pack",
    "year": 2020,
    "km": 68500,
    "price": 13690,
    "fuel": "essence",
    "gear": "auto",
    "seller": "",
    "city": "",
    "postal_code": "",
    "country": "FR",
    "image": "",
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(sidecar.create_app())


def test_la_route_rend_la_forme_annoncee(client, monkeypatch):
    async def fake_scrape(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        assert url == LISTING
        assert max_pages == 2
        return {
            "vehicles": [dict(VEHICLE)],
            "announced_total": 6913,
            "pages_read": 2,
            "partial": True,
        }

    monkeypatch.setattr(sidecar, "scrape_pages", fake_scrape)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 2})

    assert response.status_code == 200
    body = response.json()
    # Quatre clés à la racine, dix-sept par annonce : c'est le contrat que
    # l'adaptateur lit, clé par clé.
    assert set(body) == {"vehicles", "announced_total", "pages_read", "partial"}
    assert set(body["vehicles"][0]) == set(VEHICLE)
    assert body["announced_total"] == 6913
    assert body["pages_read"] == 2
    assert body["partial"] is True


def test_un_defi_rend_409_et_jamais_un_200_vide(client, monkeypatch):
    async def challenged(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        raise sidecar.Challenge("défi anti-robot sur la page 1")

    monkeypatch.setattr(sidecar, "scrape_pages", challenged)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 1})

    assert response.status_code == 409
    assert response.json() == {"error": "challenge"}


def test_une_page_illisible_rend_502_avec_un_message(client, monkeypatch):
    async def broken(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        raise sidecar.ScrapeFailed("la page 1 n'a rendu aucune carte")

    monkeypatch.setattr(sidecar, "scrape_pages", broken)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 1})

    assert response.status_code == 502
    assert "aucune carte" in response.json()["error"]


def test_une_panne_du_navigateur_rend_502_avec_le_nom_de_l_erreur(client, monkeypatch):
    async def crashed(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        raise RuntimeError("Target crashed")

    monkeypatch.setattr(sidecar, "scrape_pages", crashed)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 1})

    assert response.status_code == 502
    assert "Target crashed" in response.json()["error"]


def test_une_url_d_un_autre_hote_est_refusee_sans_aucune_requete(client, monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_scrape(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        calls.append((url, max_pages))
        return {"vehicles": [dict(VEHICLE)], "announced_total": None,
                "pages_read": 1, "partial": False}

    monkeypatch.setattr(sidecar, "scrape_pages", fake_scrape)

    response = client.post("/scrape", json={"url": "https://example.com/listing",
                                            "max_pages": 1})

    # Refusée AVANT tout lancement de navigateur : le side-car ne va pas
    # chercher un autre hôte pour le compte de son appelant.
    assert response.status_code == 400
    assert calls == []


def test_un_corps_qui_n_est_pas_du_json_est_refuse_avant_la_route(client, monkeypatch):
    # 422 vient de FastAPI, avant ce code : `payload: dict = Body(...)` fait
    # valider le corps par le cadre, et un corps illisible n'atteint jamais la
    # lecture. L'adaptateur envoie toujours un objet JSON ; ce test dit qui
    # refuse, pour qu'un 422 ne se lise pas comme un bug de la route.
    calls: list[tuple[str, int]] = []

    async def fake_scrape(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        calls.append((url, max_pages))
        return {"vehicles": [dict(VEHICLE)], "announced_total": None,
                "pages_read": 1, "partial": False}

    monkeypatch.setattr(sidecar, "scrape_pages", fake_scrape)

    response = client.post("/scrape", content=b"pas du json",
                           headers={"content-type": "application/json"})

    assert response.status_code == 422
    assert calls == []


def test_un_navigateur_qui_ne_rend_pas_la_main_rend_502(client, monkeypatch):
    # Sans plafond externe, une requête pendue tient le verrou du side-car et
    # plus aucune ne passe : la source mourrait en silence.
    import asyncio

    monkeypatch.setattr(sidecar, "REQUEST_TIMEOUT_S", 0.05)

    async def hangs(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(sidecar, "scrape_pages", hangs)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 1})

    assert response.status_code == 502
    assert "délai" in response.json()["error"]


def test_la_page_de_depart_traverse_jusqu_au_scrape(client, monkeypatch):
    # Le side-car accepte `start_page` : c'est ce qui permet à une reprise de
    # récolte de repartir à la page suivante sans relire (ni repayer) celles
    # déjà lues.
    calls: list[tuple[str, int, int]] = []

    async def fake_scrape(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        calls.append((url, max_pages, start_page))
        return {"vehicles": [dict(VEHICLE)], "announced_total": None,
                "pages_read": 5, "partial": True}

    monkeypatch.setattr(sidecar, "scrape_pages", fake_scrape)

    response = client.post("/scrape", json={"url": LISTING, "max_pages": 3,
                                            "start_page": 5})

    assert response.status_code == 200
    assert calls == [(LISTING, 3, 5)]


def test_une_page_de_depart_absente_vaut_le_debut(client, monkeypatch):
    calls: list[int] = []

    async def fake_scrape(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        calls.append(start_page)
        return {"vehicles": [dict(VEHICLE)], "announced_total": None,
                "pages_read": 1, "partial": False}

    monkeypatch.setattr(sidecar, "scrape_pages", fake_scrape)

    client.post("/scrape", json={"url": LISTING, "max_pages": 1})

    assert calls == [1]


def test_un_resultat_vide_est_refuse_au_debut_et_accepte_en_reprise(client, monkeypatch):
    # Un 200 vide au début serait le mensonge que ce side-car existe pour
    # empêcher (un marché désert au lieu d'une page illisible). À partir d'une
    # page de reprise, c'est la réponse honnête d'un stock épuisé avant cette
    # page : la refuser rendrait toute reprise au-delà du stock impossible.
    async def empty(url: str, max_pages: int, *, start_page: int = 1) -> dict:
        return {"vehicles": [], "announced_total": 40, "pages_read": 0, "partial": True}

    monkeypatch.setattr(sidecar, "scrape_pages", empty)

    debut = client.post("/scrape", json={"url": LISTING, "max_pages": 1})
    reprise = client.post("/scrape", json={"url": LISTING, "max_pages": 1,
                                          "start_page": 4})

    assert debut.status_code == 502
    assert "aucune annonce lue" in debut.json()["error"]
    assert reprise.status_code == 200
    assert reprise.json()["vehicles"] == []


def test_le_health_repond_avant_tout_lancement_de_navigateur(client):
    # L'entrypoint attend ce port : s'il ne répondait qu'après un scrape, le
    # serveur MCP ne démarrerait jamais.
    assert client.get("/health").json() == {"ok": True}
