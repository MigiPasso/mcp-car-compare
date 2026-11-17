"""LaCentrale : conversion du side-car, statuts, marché français, lot en une page.

Aucun test n'ouvre de connexion et aucun ne lance de navigateur : le client
`httpx` est construit sur un `MockTransport` qui rejoue la réponse du side-car,
et le side-car lui-même n'est jamais joint. La forme de la réponse rejouée est
celle du contrat fixé pour le side-car (`POST /scrape` -> `vehicles`,
`announced_total`, `pages_read`, `partial`).

Le dernier test traverse le chemin PRODUIT de bout en bout (fabrique réelle des
adaptateurs, deux sources dans la même récolte, corpus mêlé) : c'est le seul qui
prouve que `lacentrale` est servie et que `offers` / `analyze_models` lisent les
deux sources sans additionner les récoltes.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mcp_car_compare import tools
from mcp_car_compare.models import (
    Criteria,
    Listing,
    SourceError,
    StructureChanged,
)
from mcp_car_compare.sources.lacentrale import (
    SIDECAR_URL,
    SITE_URL,
    Lacentrale,
    search_url,
)

from test_autoscout24 import page_html, raw_listing
from test_tools import FakeMcp, recording_client


def run(awaitable):
    """Exécute un scénario async sans plugin de test asynchrone."""
    return asyncio.run(awaitable)


def vehicle(source_id: str = "LC-1", **overrides) -> dict:
    """Un véhicule dans la forme du contrat du side-car, tous champs remplis.

    Tous les champs sont posés, y compris ceux qu'un site réel laisse vides :
    c'est ce qui permet au test de conversion de comparer le `Listing` entier.
    """
    raw = {
        "source_id": source_id,
        "url": "/auto-occasion-annonce-1234.html",
        "make": "Renault",
        "group": "clio",
        "group_label": "Clio",
        "engine": "1.5 Blue dCi 85",
        "trim": "sport",
        "year": 2021,
        "km": 48_000,
        "price": 11_490,
        "fuel": "diesel",
        "gear": "auto",
        "seller": "pro",
        "city": "Lille",
        "postal_code": "59000",
        "country": "FR",
        "image": "https://images.lacentrale.fr/annonce/1234.jpg",
    }
    raw.update(overrides)
    return raw


def payload(vehicles: list[dict] | None = None, **overrides) -> dict:
    """Une réponse 200 du side-car, dans la forme exacte du contrat."""
    body = {
        "vehicles": vehicles or [],
        "announced_total": None,
        "pages_read": 1,
        "partial": False,
    }
    body.update(overrides)
    return body


class Sidecar:
    """Transport factice : rejoue une réponse du side-car, retient les requêtes."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


def search(
    body: dict,
    *,
    status: int = 200,
    criteria: Criteria | None = None,
    page_number: int = 1,
    market: str = "fr",
):
    """Joue une recherche sur une réponse de side-car donnée, sans réseau."""
    sidecar = Sidecar(body, status)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(sidecar.handler)
        ) as client:
            source = Lacentrale(client, market)
            return await source.search(criteria or Criteria(markets=("fr",)), page_number)

    return run(scenario()), sidecar


# --- conversion d'un véhicule --------------------------------------------------


def test_un_vehicule_du_sidecar_devient_un_listing_du_contrat_sans_perte():
    # Comparaison du `Listing` ENTIER, pas de trois champs : une conversion qui
    # oublierait `trim`, `seller` ou `image` laisserait autrement passer la
    # perte, et c'est exactement ce qu'un corpus ne peut pas se permettre — un
    # champ vide se lit « le site se tait », pas « l'adaptateur l'a jeté ».
    raw = vehicle()
    page, _ = search(payload([raw]))
    assert page.listings == [
        Listing(
            source="lacentrale",
            market="fr",
            source_id="LC-1",
            url=f"{SITE_URL}/auto-occasion-annonce-1234.html",
            make="Renault",
            group="clio",
            group_label="Clio",
            engine="1.5 Blue dCi 85",
            trim="sport",
            year=2021,
            km=48_000,
            price=11_490,
            fuel="diesel",
            gear="auto",
            seller="pro",
            city="Lille",
            postal_code="59000",
            country="FR",
            image="https://images.lacentrale.fr/annonce/1234.jpg",
            vin=None,
        )
    ]
    # Aucun champ du side-car n'est laissé de côté : le nombre de clés de la
    # charge rejouée est celui du contrat, et chaque clé est relue ci-dessus.
    assert sorted(raw) == [
        "city",
        "country",
        "engine",
        "fuel",
        "gear",
        "group",
        "group_label",
        "image",
        "km",
        "make",
        "postal_code",
        "price",
        "seller",
        "source_id",
        "trim",
        "url",
        "year",
    ]


def test_le_marche_et_le_pays_sont_francais_meme_si_le_sidecar_se_tait():
    # LaCentrale ne lit que lacentrale.fr : le marché et le pays viennent de la
    # SOURCE, pas d'un champ du side-car. Les laisser vides ou les recopier d'un
    # champ ferait qu'une annonce française pourrait se dire allemande.
    page, _ = search(payload([vehicle(country="")]))
    assert (page.listings[0].market, page.listings[0].country) == ("fr", "FR")


def test_un_libelle_de_carburant_ou_de_boite_inconnu_reste_inconnu():
    # Les tables partagées de `labels.py` traduisent les libellés du site ; un
    # libellé qu'elles ne connaissent pas vaut "" — inconnu, jamais une
    # devinette (« semi-automatique » n'est ni auto ni manuelle).
    page, _ = search(payload([vehicle(fuel="GPL", gear="Semi-automatique")]))
    assert (page.listings[0].fuel, page.listings[0].gear) == ("", "")


def test_une_annonce_sans_identifiant_garde_un_source_id_vide():
    # Une annonce sans identifiant n'est pas dédoublonnable : la récolte
    # l'écartera et la comptera (`dropped_invalid`). Ce qui ne doit pas arriver,
    # c'est que l'adaptateur invente un identifiant ou lève sur un champ absent.
    page, _ = search(payload([vehicle(source_id=None)]))
    assert page.listings[0].source_id == ""


# --- compteurs -----------------------------------------------------------------


def test_le_total_annonce_par_le_sidecar_est_transporte_tel_quel():
    # C'est le compteur du SITE pour la requête entière : il ne devient jamais
    # `len(listings)`, sinon une page de 2 cartes sur 465 annonces se lirait
    # comme un marché de 2 voitures.
    page, _ = search(payload([vehicle("LC-1"), vehicle("LC-2")], announced_total=465))
    assert (page.announced_total, page.rendered, len(page.listings)) == (465, 2, 2)


def test_un_total_absent_reste_absent_et_ne_devient_pas_le_nombre_de_cartes():
    # « Le side-car n'a pas lu le compteur » et « il annonce 2 annonces » ne
    # doivent pas se ressembler : l'absence est dite.
    page, _ = search(payload([vehicle("LC-1"), vehicle("LC-2")], announced_total=None))
    assert page.announced_total is None


def test_une_lecture_partielle_se_declare_par_has_more():
    # `partial` est le seul aveu de troncature que le side-car puisse faire, et
    # `Page.has_more` le seul canal qui le porte jusqu'à `coverage`. Sans cette
    # conversion, une lecture partielle sans total annoncé sortait
    # `truncated=False`, donc « source couverte ».
    page, _ = search(payload([vehicle("LC-1")], partial=True, pages_read=1))
    assert page.has_more is True
    assert page.announced_total is None  # le total manque : rien n'est inventé


# --- statuts du side-car -------------------------------------------------------


def test_un_409_du_sidecar_est_un_challenge():
    # DataDome n'est pas une panne : `coverage.status="challenge"` existe pour
    # le dire, et le vocabulaire du contrat ne vaut que si un adaptateur peut le
    # produire. Un 409 lu comme une erreur rendrait le statut inatteignable.
    with pytest.raises(SourceError) as caught:
        search(payload({"error": "challenge"}), status=409)
    assert caught.value.status == "challenge"
    assert "DataDome" in str(caught.value)


def test_un_502_du_sidecar_est_une_erreur_pas_un_challenge():
    # L'inverse doit être vrai aussi : une panne du side-car n'est pas un défi,
    # et la confondre ferait réessayer un service mort en croyant à un blocage.
    with pytest.raises(SourceError) as caught:
        search(payload({"error": "sidecar down"}), status=502)
    assert caught.value.status == "error"
    assert "502" in str(caught.value)


def test_un_sidecar_injoignable_est_une_erreur():
    # Le side-car meurt silencieusement (PLAN.md §3) : le refus de connexion doit
    # devenir un statut, pas remonter en `ConnectError` au milieu de la récolte.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Lacentrale(client).search(Criteria(markets=("fr",)), 1)

    with pytest.raises(SourceError) as caught:
        run(scenario())
    assert caught.value.status == "error"
    assert SIDECAR_URL in str(caught.value)


def test_une_reponse_sans_liste_de_vehicules_est_une_structure_changee():
    # Un contrat de side-car qui change doit être bruyant : lire une charge sans
    # `vehicles` comme « aucune annonce » ferait conclure à un marché vide.
    with pytest.raises(StructureChanged, match="vehicles"):
        search({"pages_read": 1, "partial": False})


# --- marché français uniquement ------------------------------------------------


def test_un_marche_allemand_est_refuse_par_la_recherche():
    # LaCentrale est française : un `de` ne construirait pas une adresse qui
    # répond, et le refus doit le dire au lieu de rendre zéro annonce.
    sidecar = Sidecar(payload([vehicle()]))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(sidecar.handler)
        ) as client:
            return await Lacentrale(client).search(Criteria(markets=("de",)), 1)

    with pytest.raises(SourceError, match="française uniquement"):
        run(scenario())
    assert sidecar.requests == []  # rien n'a été demandé au side-car


def test_un_marche_allemand_est_refuse_a_la_construction():
    # `brands()` n'a pas de marché dans ses arguments : c'est le marché de
    # l'instance qui décide, et il ne peut pas être autre que `fr`.
    async def scenario():
        async with httpx.AsyncClient() as client:
            Lacentrale(client, "de")

    with pytest.raises(SourceError, match="française uniquement"):
        run(scenario())


# --- adresse de recherche ------------------------------------------------------


def test_l_adresse_est_celle_du_site_avec_les_libelles_en_vocabulaire_de_requete():
    # Mesuré dans le chemin de référence : `makesModelsCommercialNames=PEUGEOT::208`
    # — marque en majuscules sans accent, modèle en majuscules, séparés par « :: ».
    # Un slug (`peugeot-208`) demande au site une marque qu'il ne connaît pas.
    criteria = Criteria(
        make="Peugeot",
        model="Série 208",  # accentué et minuscule : la forme du site est nue
        year_min=2020,
        year_max=2022,
        km_max=75_000,
        price_min=8_000,
        price_max=15_000,
    )
    url = search_url(criteria)
    assert url.startswith(f"{SITE_URL}/listing?")
    assert (
        "makesModelsCommercialNames=PEUGEOT::SERIE%20208" in url
    )  # « :: » reste lisible, l'espace est échappé
    assert "priceMin=8000" in url and "priceMax=15000" in url
    assert "mileageMax=75000" in url
    assert "yearMin=2020" in url and "yearMax=2022" in url
    # Sans ce tri, la page est classée par pertinence et une lecture automatique
    # ne voit jamais que les 24 mêmes annonces : il n'est pas optionnel.
    assert "sortBy=firstOnlineDateDesc" in url
    # La pagination n'est pas dans l'adresse : le side-car l'enchaîne lui-même.
    assert "page=" not in url


def test_un_tri_par_prix_est_refuse_plutot_qu_ignore():
    # Aucun code de tri par prix n'est mesuré chez LaCentrale : l'accepter puis
    # rendre des annonces classées autrement serait le critère accepté et jamais
    # posé que ce serveur refuse partout ailleurs.
    with pytest.raises(SourceError, match="tri"):
        search_url(Criteria(markets=("fr",), sort="price_asc"))


def test_un_modele_sans_marque_est_refuse():
    # Le site ne connaît pas de gamme hors d'une marque : le libellé seul
    # deviendrait une marque inexistante, donc « 0 annonce ».
    with pytest.raises(SourceError, match="marque avant le modèle"):
        search_url(Criteria(model="Clio"))


# --- un lot, une requête -------------------------------------------------------


def test_la_page_un_demande_tout_le_lot_et_la_page_deux_n_emet_aucune_requete():
    # Le side-car enchaîne les pages dans UNE session Camoufox (lancement ~30 s,
    # DataDome 6–75 s). Redemander page par page relancerait un navigateur pour
    # des annonces déjà lues : la page 2 rend une page vide, sans requête.
    sidecar = Sidecar(payload([vehicle("LC-1")], announced_total=465))
    criteria = Criteria(markets=("fr",), pages_max=7)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(sidecar.handler)
        ) as client:
            source = Lacentrale(client)
            first = await source.search(criteria, 1)
            second = await source.search(criteria, 2)
            return first, second

    first, second = run(scenario())
    assert len(sidecar.requests) == 1  # une seule requête pour les deux pages
    request = sidecar.requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL(f"{SIDECAR_URL}/scrape")
    assert (first.rendered, second.rendered) == (1, 0)
    assert second.listings == [] and second.has_more is False
    # `max_pages` porte le plafond de la RÉCOLTE : c'est lui qui borne la lecture
    # du side-car, et il vient de `criteria.pages_max`.
    assert json.loads(request.content)["max_pages"] == 7
    assert json.loads(request.content)["url"] == search_url(criteria)


def test_une_reprise_demande_sa_page_de_depart_au_side_car():
    # C'est le raccord du point 7 : sans `start_page`, une reprise relisait la
    # page 1 — repayer les pages déjà payées (~8 s/page dans le navigateur) est
    # exactement ce qui faisait expirer l'appel côté client. Le `pages_max`
    # transmis est celui qui RESTE dans le job, pas celui de l'appel.
    sidecar = Sidecar(payload([vehicle("LC-9")], announced_total=465))
    criteria = Criteria(markets=("fr",), pages_max=7)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(sidecar.handler)
        ) as client:
            source = Lacentrale(client)
            resumed = await source.search(criteria, 5)
            # Le lot a déjà été demandé : cet appel-ci ne redemande rien.
            after = await source.search(criteria, 6)
            return resumed, after

    resumed, after = run(scenario())
    assert len(sidecar.requests) == 1
    body = json.loads(sidecar.requests[0].content)
    assert body["start_page"] == 5
    assert body["max_pages"] == 3  # pages 5, 6 et 7 : le plafond du job
    assert resumed.rendered == 1 and after.rendered == 0


# --- catalogue de marques ------------------------------------------------------


def test_brands_refuse_au_lieu_de_rendre_une_liste_vide():
    # « Aucun catalogue par ce chemin » et « ce site ne vend aucune marque » ne
    # peuvent pas se ressembler : `list_brands` affiche l'erreur, et le message
    # dit où lire les libellés que `search` attend.
    async def scenario():
        async with httpx.AsyncClient() as client:
            return await Lacentrale(client).brands()

    with pytest.raises(SourceError, match="aucun catalogue de marques"):
        run(scenario())


def test_list_brands_rend_l_absence_de_catalogue_au_lieu_de_lever(tmp_path, monkeypatch):
    # Par la fabrique RÉELLE : une source servie dont le catalogue n'existe pas
    # est un résultat (`error` + `brands` vide), pas une exception qui ferait
    # tomber l'outil — et aucune requête ne part, ni vers le site ni vers le
    # side-car, puisque le side-car ne connaît que `/scrape`.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload([vehicle()]))

    recording_client(monkeypatch, handler)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    result = asyncio.run(mcp.tools["list_brands"]("lacentrale", "fr"))
    assert (result["source"], result["market"], result["brands"]) == (
        "lacentrale",
        "fr",
        [],
    )
    assert "aucun catalogue de marques" in result["error"]
    assert requests == []


# --- le chemin produit : deux sources, un corpus -------------------------------


def harvest_transport(request: httpx.Request) -> httpx.Response:
    """Répond aux deux transports d'une récolte : le site et le side-car."""
    if request.url.host == "www.autoscout24.fr":
        return httpx.Response(
            200,
            text=page_html(
                {
                    "numberOfResults": 2,
                    "listings": [raw_listing("as-1"), raw_listing("as-2")],
                }
            ),
        )
    return httpx.Response(
        200,
        json=payload(
            [
                vehicle("lc-1"),
                vehicle("lc-2"),
                vehicle("lc-3", make="Peugeot", group="208", group_label="208"),
            ],
            announced_total=None,
        ),
    )


def mixed_harvest(tmp_path, monkeypatch):
    """Une récolte à deux sources, par la fabrique RÉELLE des adaptateurs.

    Le client de la récolte est un vrai `httpx.AsyncClient` sur un transport
    factice : la requête AutoScout24 part vers `www.autoscout24.fr`, celle de
    LaCentrale vers le side-car, et les deux adaptateurs sont ceux de production.
    """
    recording_client(monkeypatch, harvest_transport)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    criteria = {
        "make": "Renault",
        "sources": ["autoscout24", "lacentrale"],
        "pages_max": 2,
    }

    def once():
        return asyncio.run(mcp.tools["harvest"](criteria, None))

    return mcp, once


def test_une_recolte_a_deux_sources_remplit_la_couverture_des_deux(tmp_path, monkeypatch):
    # La couverture porte une ligne PAR SOURCE : une source servie mais muette
    # se lirait comme un marché vide, et c'est ce que ce test refuse.
    mcp, once = mixed_harvest(tmp_path, monkeypatch)
    result = once()
    rows = {(row["source"], row["market"]): row for row in result["coverage"]}
    assert sorted(rows) == [("autoscout24", "fr"), ("lacentrale", "fr")]
    assert [row["status"] for row in rows.values()] == ["ok", "ok"]
    assert rows[("autoscout24", "fr")]["unique"] == 2
    assert rows[("lacentrale", "fr")]["unique"] == 3
    # Le lot de LaCentrale a été lu d'un coup, et le site n'annonce aucun total :
    # la récolte s'arrête sur une page vide sans se dire tronquée, parce que le
    # side-car ne l'a pas déclarée partielle.
    assert rows[("lacentrale", "fr")]["truncated"] is False

    coverage = mcp.tools["offers"](result["job_id"], None, 25)["coverage"]
    # Le corpus décrit des annonces DISTINCTES, et chaque source avec ses
    # propres chiffres : jamais la somme des récoltes.
    assert coverage["listings_distinct"] == 5
    assert coverage["runs"] == 2
    assert [
        (row["source"], row["listings_in_corpus"], row["read"], row["unique"])
        for row in coverage["sources"]
    ] == [("autoscout24", 2, 2, 2), ("lacentrale", 3, 3, 3)]


def test_un_lot_partiel_traverse_jusqu_a_la_couverture(tmp_path, monkeypatch):
    # Le raccord COMPLET, par la fabrique réelle : `partial` du side-car ->
    # `Page.has_more` -> `coverage.truncated`. C'est ce champ qui décide si la
    # source se déclare couverte, et l'éprouver séparément sur l'adaptateur
    # laisserait le raccord avec `tools` non prouvé — une page partielle
    # remontée sans conséquence se lirait comme un marché entièrement lu.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=payload(
                [vehicle("lc-1"), vehicle("lc-2")],
                announced_total=None,  # le site se tait : rien ne rattrape l'aveu
                partial=True,
            ),
        )

    recording_client(monkeypatch, handler)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    result = asyncio.run(
        mcp.tools["harvest"]({"sources": ["lacentrale"], "pages_max": 3}, None)
    )
    row = result["coverage"][0]
    assert (row["source"], row["read"], row["unique"]) == ("lacentrale", 2, 2)
    assert row["truncated"] is True
    assert "s'est arrêtée avant la fin" in row["reason"]
    # Les cartes lues ne sont pas jetées : `partial` dit « il en reste », pas
    # « ce lot est inutilisable ».
    assert len(mcp.tools["offers"](result["job_id"], None, 25)["items"]) == 2
    # `max_pages` est la seule borne de lecture : il part tel quel, et les trois
    # pages demandées tiennent en UNE requête (le side-car enchaîne, l'adaptateur
    # ne redemande pas).
    assert len(requests) == 1
    assert json.loads(requests[0].content)["max_pages"] == 3


def test_une_seconde_recolte_ne_double_pas_les_annonces_distinctes(tmp_path, monkeypatch):
    # Deux récoltes qui se recouvrent, additionnées, annoncent une couverture
    # double : c'est le défaut que `coverage` existe pour empêcher. Le corpus
    # reste à 5 annonces distinctes — la seconde récolte n'ajoute rien — et
    # chaque récolte garde ses propres `read`, qui ne s'additionnent pas non
    # plus dans `listings_distinct`.
    mcp, once = mixed_harvest(tmp_path, monkeypatch)
    first = once()
    second = once()
    assert first["coverage"][0]["read"] == 2  # la première récolte a bien lu
    coverage = mcp.tools["offers"](second["job_id"], None, 25)["coverage"]
    assert coverage["listings_distinct"] == 5  # la seconde n'a rien doublé
    assert coverage["runs"] == 2  # une ligne par `(source, market)` de CETTE récolte
    assert [row["read"] for row in coverage["sources"]] == [2, 3]
    assert [row["listings_in_corpus"] for row in coverage["sources"]] == [2, 3]


def test_analyze_models_lit_un_corpus_mele_sans_changer_de_forme(tmp_path, monkeypatch):
    # `analyze_models` ne récolte rien et ne connaît pas les adaptateurs : il
    # groupe des `Listing` du contrat, quelle que soit leur source. Le test le
    # prouve sur le corpus réel de deux sources — et sans rien attendre des
    # quantiles, que le seuil de six comparables refuse ici.
    mcp, once = mixed_harvest(tmp_path, monkeypatch)
    once()
    analysis = mcp.tools["analyze_models"]({}, 30)
    groups = {(group["make"], group["group"]): group["n"] for group in analysis["groups"]}
    assert groups == {("Renault", "clio"): 4, ("Peugeot", "208"): 1}
    assert analysis["groups_total"] == 2
    assert [row["source"] for row in analysis["coverage"]["sources"]] == [
        "autoscout24",
        "lacentrale",
    ]


# --- le site ne filtre ni carburant ni boîte -----------------------------------


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ({"fuel": "diesel"}, "carburant"),
        ({"gear": "auto"}, "boîte"),
    ],
)
def test_un_filtre_que_lacentrale_ne_sait_pas_poser_est_refuse(tmp_path, criteria, message):
    # Accepter ces critères puis ne rien filtrer donnerait des compteurs faux ;
    # filtrer après lecture serait pire (`read` compterait des annonces jetées).
    # Le refus tombe au parsage, avant la première requête, en nommant la source.
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    with pytest.raises(ValueError, match=message):
        asyncio.run(
            mcp.tools["harvest"]({"sources": ["lacentrale"], **criteria}, None)
        )
    assert not (tmp_path / "corpus.db").exists()  # rien n'a même ouvert le corpus


def test_un_filtre_de_carburant_reste_accepte_pour_autoscout24(tmp_path, monkeypatch):
    # Le refus ne doit pas déborder sur la source qui sait filtrer : sinon la
    # correction d'un mensonge en créerait un autre, sur tout le monde.
    recording_client(monkeypatch, harvest_transport)
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db")
    result = asyncio.run(
        mcp.tools["harvest"](
            {"fuel": "diesel", "pages_max": 1, "sources": ["autoscout24"]}, None
        )
    )
    assert [row["source"] for row in result["coverage"]] == ["autoscout24"]
