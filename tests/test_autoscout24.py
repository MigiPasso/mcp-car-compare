"""Rejoue les pages AutoScout24 réellement capturées le 2026-11-12.

Les quatre fixtures de `tests/fixtures/autoscout24/` sont des copies verbatim
des pages publiques de `ICH/market/autoscout/testdata/` (`search_fr.html`,
`search_de.html`, `detail_de.html`, `search_images.html`).

Aucun test ne touche le réseau : le client `httpx` est construit sur un
`MockTransport` qui rend le corps d'une fixture. Aucune assertion ne dépend de
l'horloge réelle : l'espacement entre deux pages est éprouvé avec une horloge et
un sommeil injectés.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from mcp_car_compare.models import (
    FUELS,
    GEARS,
    SELLERS,
    Criteria,
    SourceError,
    StructureChanged,
)
from mcp_car_compare.sources.autoscout24 import (
    PAGE_PAUSE_S,
    USER_AGENT,
    Autoscout24,
    image_url,
    normalize_group,
    search_url,
)
from mcp_car_compare.sources.labels import (
    FUEL_LABELS,
    GEAR_LABELS,
    SELLER_TYPES,
    fuel_from,
    gear_from,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "autoscout24"


def fixture(name: str) -> str:
    """Corps d'une page capturée."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(awaitable):
    """Exécute un scénario async sans plugin de test asynchrone."""
    return asyncio.run(awaitable)


def page_html(page_props: dict) -> str:
    """Page minimale autour d'un `pageProps` fabriqué.

    Sert aux cas qu'aucune capture ne porte (compteur muet, doublon, recherche
    vide) : le fragment est écrit ici, pas dans une fixture, pour qu'il ne se
    fasse jamais passer pour une page du site.
    """
    payload = json.dumps({"props": {"pageProps": page_props}})
    return (
        '<!DOCTYPE html><html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{payload}</script></head><body></body></html>"
    )


def raw_listing(source_id: str = "annonce-1", **overrides) -> dict:
    """Annonce brute d'une page de résultats, champs au format du site."""
    raw = {
        "id": source_id,
        "url": f"/offres/essai-{source_id}",
        "price": {"priceRaw": 10000, "vatLabel": ""},
        "vehicle": {
            "make": "Renault",
            "modelGroup": "Clio",
            "motorTypeName": "1.5 dCi",
            "transmission": "Boîte automatique",
            "fuel": "Diesel",
            "mileageInKm": "10 000 km",
        },
        "location": {"countryCode": "FR", "zip": "59000", "city": "Lille"},
        "seller": {"type": "Dealer"},
        "tracking": {"firstRegistration": "01-2020"},
    }
    raw.update(overrides)
    return raw


class Recorder:
    """Transport factice : rend un corps fixe et retient les requêtes reçues."""

    def __init__(self, body: str, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)


def search_html(body: str, market: str = "fr", status: int = 200):
    """Joue une recherche sur une page donnée, sans réseau."""
    recorder = Recorder(body, status)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            source = Autoscout24(client, market)
            return await source.search(Criteria(markets=(market,)), 1)

    return run(scenario()), recorder


# --- Les quatre pages capturées ---------------------------------------------


def test_page_fr_rend_les_deux_annonces_capturees():
    page, recorder = search_html(fixture("search_fr.html"), "fr")

    # 3110 annonces annoncées pour la requête, 2 sur cette page : le compteur
    # n'est pas la longueur de la liste.
    assert page.announced_total == 3110
    assert len(page.listings) == 2
    assert page.page_number == 1

    premier, second = page.listings
    assert premier.source == "autoscout24"
    assert premier.market == "fr"
    assert premier.source_id == "cf83ddf8-fa18-4400-95b4-839c2c457037"
    assert premier.url.startswith("https://www.autoscout24.fr/offres/")
    assert premier.make == "BMW"
    assert premier.group == "serie 3"
    assert premier.group_label == "Série 3"
    assert premier.engine == "320d"
    assert (premier.year, premier.km, premier.price) == (2014, 202000, 6590)
    assert premier.fuel == "diesel"
    assert premier.gear == "manuelle"
    assert premier.seller == "pro"
    assert (premier.city, premier.postal_code, premier.country) == (
        "Rosselange",
        "57780",
        "FR",
    )
    assert premier.vat_label == ""
    assert premier.image == ""

    assert second.source_id == "2edfb7de-0e13-4ddc-bef5-771669e3d90c"
    assert (second.year, second.km, second.price) == (2015, 140882, 19889)
    assert second.gear == "auto"

    # La requête vise le domaine du marché et parle sa langue : c'est l'en-tête
    # de langue qui décide des libellés rendus par le site.
    assert recorder.requests[0].url.host == "www.autoscout24.fr"
    assert recorder.requests[0].headers["accept-language"] == "fr-FR,fr;q=0.9"
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


def test_page_de_rend_les_deux_annonces_capturees():
    page, recorder = search_html(fixture("search_de.html"), "de")

    assert page.announced_total == 20167
    assert len(page.listings) == 2

    premier, second = page.listings
    assert premier.source_id == "1402ea47-6b36-44b9-a8aa-e8ed76d9f52b"
    assert premier.market == "de"
    assert premier.url.startswith("https://www.autoscout24.de/angebote/")
    assert (premier.year, premier.km, premier.price) == (2020, 80388, 18799)
    assert premier.gear == "manuelle"  # « Schaltgetriebe »
    assert premier.fuel == "diesel"  # « Diesel »
    assert premier.vat_label == "inkl. MwSt."
    assert (premier.city, premier.postal_code, premier.country) == (
        "Pfaffenhofen an der Ilm",
        "85276",
        "DE",
    )

    assert second.source_id == "10d4dd42-8e7f-4e9a-aab5-9142513e9861"
    assert (second.year, second.km, second.price) == (2014, 188000, 18999)
    assert second.gear == "auto"  # « Automatik »
    assert second.vat_label == ""

    assert recorder.requests[0].url.host == "www.autoscout24.de"
    assert recorder.requests[0].headers["accept-language"] == "de-DE,de;q=0.9"


def test_page_images_rend_la_vignette_de_chaque_annonce():
    page, _ = search_html(fixture("search_images.html"), "de")

    assert page.announced_total == 5085
    premier, second = page.listings
    assert premier.image.startswith(
        "https://prod.pictures.autoscout24.net/listing-images/"
        "c769c149-9bfb-44c5-9683-61f41fa51b6c_"
    )
    assert second.image.startswith(
        "https://prod.pictures.autoscout24.net/listing-images/"
        "38280ba1-61e6-40ea-9461-96ddbb951524_"
    )
    assert (premier.year, premier.km, premier.price) == (2009, 126500, 5990)
    assert (second.year, second.km, second.price) == (2018, 78400, 18500)
    assert premier.fuel == "essence"  # « Benzin »
    assert second.gear == "auto"  # « Automatik »


def test_deux_marches_ne_produisent_pas_la_meme_cle():
    # La MÊME page lue comme deux marchés : l'identifiant du site est identique,
    # la clé ne l'est pas. Sans le marché dans la clé, une annonce lue sur .de
    # et la même lue sur .fr se confondraient, et le corpus perdrait l'une des
    # deux — alors que rien ne garantit que les identifiants ne se recouvrent
    # pas d'une marketplace à l'autre.
    fr_page, _ = search_html(fixture("search_de.html"), "fr")
    de_page, _ = search_html(fixture("search_de.html"), "de")

    identifiant = "1402ea47-6b36-44b9-a8aa-e8ed76d9f52b"
    assert fr_page.listings[0].source_id == de_page.listings[0].source_id == identifiant
    assert fr_page.listings[0].key() == ("autoscout24", "fr", identifiant)
    assert de_page.listings[0].key() == ("autoscout24", "de", identifiant)
    assert fr_page.listings[0].key() != de_page.listings[0].key()
    assert fr_page.listings[0].url != de_page.listings[0].url


# --- Le contrat ---------------------------------------------------------------


def test_l_adaptateur_porte_le_nom_de_sa_source():
    # `source` entre dans la clé d'identité de chaque annonce : deux sources ne
    # doivent pas se confondre dans le corpus.
    async def scenario():
        async with httpx.AsyncClient() as client:
            return Autoscout24(client)

    source = run(scenario())
    assert source.source == "autoscout24"
    assert callable(source.search) and callable(source.brands)


# --- Couverture annoncée ----------------------------------------------------


def test_annonce_total_est_le_compteur_du_site_pas_la_page():
    page, _ = search_html(fixture("search_fr.html"), "fr")
    assert page.announced_total == 3110
    assert page.announced_total != len(page.listings)


def test_annonce_total_absent_vaut_none_et_n_est_pas_comble():
    body = page_html({"listings": [raw_listing()]})
    page, _ = search_html(body, "fr")
    assert page.announced_total is None
    assert len(page.listings) == 1


def test_dedoublonnage_intra_page_garde_le_premier_et_ne_touche_pas_le_total():
    doublon = raw_listing("a", price={"priceRaw": 9999, "vatLabel": ""})
    body = page_html(
        {"numberOfResults": 465, "listings": [raw_listing("a"), raw_listing("b"), doublon]}
    )
    page, _ = search_html(body, "fr")

    assert [listing.source_id for listing in page.listings] == ["a", "b"]
    # Le premier exemplaire est gardé, à sa place : 9 999 n'est pas le prix lu.
    assert page.listings[0].price == 10000
    # Le compteur du site n'est pas retouché par le dédoublonnage : c'est la
    # seule mesure de couverture annoncée.
    assert page.announced_total == 465


def test_annonce_sans_identifiant_n_est_pas_dedoublonnee():
    # Rien ne dit que deux annonces sans identifiant sont la même : elles
    # passent toutes les deux, comme dans le chemin de référence.
    body = page_html({"numberOfResults": 3, "listings": [raw_listing(""), raw_listing(""), raw_listing("a")]})
    page, _ = search_html(body, "fr")
    assert [listing.source_id for listing in page.listings] == ["", "", "a"]


# --- Cadence ----------------------------------------------------------------


class FakeClock:
    """Horloge qui n'avance que pendant les sommeils : aucune attente réelle."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_pause_entre_deux_pages_et_jamais_avant_la_premiere():
    clock = FakeClock()
    recorder = Recorder(fixture("search_fr.html"))
    criteria = Criteria(markets=("fr",))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            source = Autoscout24(client, "fr", clock=clock.clock, sleep=clock.sleep)
            await source.search(criteria, 1)
            # La première page ne paie pas la pause : elle n'espace rien.
            assert clock.sleeps == []
            await source.search(criteria, 2)
            assert clock.sleeps == [PAGE_PAUSE_S]
            # Si le temps a passé autrement, la pause n'est pas repayée.
            clock.now += 5.0
            await source.search(criteria, 3)
            assert clock.sleeps == [PAGE_PAUSE_S]

    run(scenario())
    assert len(recorder.requests) == 3


# --- Adresse de recherche ---------------------------------------------------


def test_url_porte_les_filtres_du_contrat():
    criteria = Criteria(
        make="9",
        model="1624",
        year_min=2020,
        year_max=2022,
        km_max=75000,
        price_min=10000,
        price_max=25000,
        fuel="diesel",
        gear="auto",
        markets=("de",),
        sort="price_asc",
    )
    url = httpx.URL(search_url(criteria, 2, "de"))

    assert (url.scheme, url.host, url.path) == ("https", "www.autoscout24.de", "/lst")
    params = url.params
    assert params["atype"] == "C"  # voitures seulement
    assert params["page"] == "2"
    assert params["mmmv"] == "9|1624||"  # marque | modèle | gamme | variante
    assert (params["sort"], params["desc"]) == ("price", "0")
    assert params["fregfrom"] == "2020"
    assert params["fregto"] == "2022"
    assert params["kmto"] == "75000"
    assert params["pricefrom"] == "10000"
    assert params["priceto"] == "25000"
    assert params["fuel"] == "D"
    assert params["gear"] == "A"


def test_url_sans_filtres_n_ajoute_ni_borne_ni_code():
    url = httpx.URL(search_url(Criteria(markets=("fr",)), 1, "fr"))
    params = url.params

    for absent in ("pricefrom", "priceto", "kmto", "fregfrom", "fregto", "fuel", "gear"):
        assert absent not in params
    assert params["mmmv"] == "|||"
    assert params["page"] == "1"


def test_url_hybride_demande_les_deux_codes_du_site():
    url = httpx.URL(search_url(Criteria(fuel="hybride", markets=("fr",)), 1, "fr"))
    # 2 = électrique/essence, 3 = électrique/diesel ; le site les encode
    # ensemble, et c'est la valeur que son propre formulaire envoie.
    assert url.params["fuel"] == "2,3"


def test_url_carburant_inconnu_ne_devine_aucun_code():
    url = httpx.URL(search_url(Criteria(fuel="gaz", markets=("fr",)), 1, "fr"))
    assert "fuel" not in url.params


def test_url_page_hors_bornes_revient_a_la_premiere():
    url = httpx.URL(search_url(Criteria(markets=("fr",)), 0, "fr"))
    assert url.params["page"] == "1"


def test_url_tri_decroissant_et_par_age():
    assert httpx.URL(search_url(Criteria(sort="price_desc"), 1, "fr")).params["desc"] == "1"
    age = httpx.URL(search_url(Criteria(sort="age_desc"), 1, "fr")).params
    assert (age["sort"], age["desc"]) == ("age", "1")


def test_marque_non_numerique_refusee_sans_aucune_requete():
    recorder = Recorder(fixture("search_fr.html"))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            source = Autoscout24(client, "fr")
            with pytest.raises(SourceError, match="identifiant numérique"):
                await source.search(Criteria(make="Renault", markets=("fr",)), 1)

    run(scenario())
    assert recorder.requests == []


def test_modele_sans_marque_refuse():
    with pytest.raises(SourceError, match="marque"):
        search_url(Criteria(model="1624", markets=("fr",)), 1, "fr")


def test_marche_inconnu_refuse():
    with pytest.raises(SourceError, match="marché inconnu"):
        search_url(Criteria(markets=("it",)), 1, "it")


def test_plusieurs_marches_refuses_au_lieu_d_en_perdre_un():
    recorder = Recorder(fixture("search_fr.html"))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            source = Autoscout24(client, "fr")
            with pytest.raises(SourceError, match="un seul marché"):
                await source.search(Criteria(markets=("fr", "de")), 1)
            with pytest.raises(SourceError, match="un seul marché"):
                await source.search(Criteria(markets=()), 1)

    run(scenario())
    assert recorder.requests == []


# --- Marques ----------------------------------------------------------------

# Taxonomie de la forme lue par ICH (scraper/autoscout_search.go) : elle vient
# d'une page `/lst?atype=C` réelle, mais AUCUNE capture de cette page n'existe
# dans les fixtures. C'est donc un fragment écrit ici, pas une page du site.
TAXONOMY = {
    "numberOfResults": 465,
    "listings": [],
    "taxonomy": {
        "makesSorted": [{"label": "BMW", "value": 13}, {"label": "Audi", "value": 9}],
        "models": {"13": [{"label": "116", "value": 18480}]},
        "modelLines": {"13": [{"label": "1 Series (all)", "id": 9}]},
    },
}


def test_brands_lit_la_taxonomie_du_marche():
    recorder = Recorder(page_html(TAXONOMY))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            return await Autoscout24(client, "de").brands()

    brands = run(scenario())
    assert brands == [
        {"value": "13", "label": "BMW", "models": [{"value": "18480", "label": "116"}]},
        {"value": "9", "label": "Audi", "models": []},
    ]
    request = recorder.requests[0]
    assert (request.url.host, request.url.path) == ("www.autoscout24.de", "/lst")
    assert request.url.params["atype"] == "C"
    assert request.headers["accept-language"] == "de-DE,de;q=0.9"


def test_brands_sans_taxonomie_leve_une_casse():
    recorder = Recorder(page_html({"numberOfResults": 465, "listings": []}))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            await Autoscout24(client, "fr").brands()

    with pytest.raises(StructureChanged):
        run(scenario())


# --- Vocabulaire du site vers le contrat ------------------------------------


@pytest.mark.parametrize(
    ("label", "attendu"),
    [
        ("Diesel", "diesel"),  # 519 annonces mesurées
        ("Benzin", "essence"),  # 333
        ("Essence", "essence"),  # 280
        ("Elektro/Benzin", "hybride"),  # 28
        ("Électrique/Essence", "hybride"),  # 22
        ("Electrique", "electrique"),  # 6
        ("Elektro/Diesel", "hybride"),  # 4
        ("Elektro", "electrique"),  # 3
        ("Électrique/Diesel", "hybride"),  # 1
        ("Hybrides", "hybride"),  # puce La Centrale, pluriel
        ("hybride", "hybride"),
        ("Erdgas (CNG)", ""),  # pas de gaz au vocabulaire du contrat
        ("Autres", ""),
        ("", ""),
    ],
)
def test_libelles_carburant_du_site(label, attendu):
    assert fuel_from(label) == attendu


@pytest.mark.parametrize(
    ("label", "attendu"),
    [
        ("Boîte automatique", "auto"),  # 432 annonces mesurées
        ("Automatik", "auto"),  # 394
        ("Schaltgetriebe", "manuelle"),  # 203
        ("Boîte manuelle", "manuelle"),  # 165
        ("Automatique", "auto"),
        ("Auto", "auto"),
        ("Manuelle", "manuelle"),
        # Non traduits à dessein : le site ne dit pas si c'est une manuelle
        # robotisée ou une automatique, donc ni l'un ni l'autre.
        ("Semi-automatique", ""),
        ("Halbautomatik", ""),
        ("", ""),
    ],
)
def test_libelles_boite_du_site(label, attendu):
    assert gear_from(label) == attendu


def test_les_tables_ne_rendent_que_le_vocabulaire_du_contrat():
    # Le vocabulaire est fermé et vérifié à la construction : une valeur hors
    # table ferait échouer le corpus, pas la comparaison.
    assert set(FUEL_LABELS.values()) <= set(FUELS)
    assert set(GEAR_LABELS.values()) <= set(GEARS)
    assert set(SELLER_TYPES.values()) <= set(SELLERS)


@pytest.mark.parametrize(
    ("label", "attendu"),
    [
        ("Série 3", "serie 3"),
        ("3er", "serie 3"),
        ("3er Touring", "serie 3 touring"),
        ("Série 3 Touring", "serie 3 touring"),
        ("Classe C", "serie c"),
        ("C-Klasse", "serie c"),
        ("SLK-Klasse", "serie slk"),
        # La carrosserie suit, normalisée mais non traduite : le tiret du
        # libellé d'origine reste, il n'est pas une ponctuation de mot.
        ("C-Klasse T-Modell", "serie c t-modell"),
        ("1er", "serie 1"),
        ("X1", "x1"),
        ("A4", "a4"),
        ("Clio", "clio"),
        # Le tas de tout ce qui n'est pas comparable ne forme aucun segment.
        ("Autres", ""),
        ("Sonstige", ""),
        ("", ""),
    ],
)
def test_gammes_rejointes_entre_marches(label, attendu):
    assert normalize_group(label) == attendu


@pytest.mark.parametrize(
    ("raw", "attendu"),
    [
        ("https://prod.pictures.autoscout24.net/listing-images/a.jpg/250x188.webp", True),
        ("https://cdn.prod.pictures.autoscout24.net/a.jpg", True),  # sous-domaine
        ("http://prod.pictures.autoscout24.net/a.jpg", False),  # HTTPS seulement
        ("https://evil.example/a.jpg", False),
        ("https://prod.pictures.autoscout24.net.evil.example/a.jpg", False),
        ("", False),
    ],
)
def test_vignette_acceptee_seulement_depuis_l_hote_mesure(raw, attendu):
    assert bool(image_url(raw)) is attendu
