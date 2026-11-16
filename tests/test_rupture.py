"""Tests de rupture AutoScout24 : la casse doit être VUE, pas absorbée.

Le défaut que ces tests interdisent est « 0 annonce, aucune erreur » : une page
de défi anti-robot, une erreur du site ou une structure changée se liraient
comme un marché désert, et la couverture annoncerait zéro annonce sur un marché
qui en compte des milliers.

Un HTML sans `__NEXT_DATA__` est une casse (`StructureChanged`) ; un statut
non-200 est un refus (`SourceError`) ; une page qui n'est manifestement pas une
page de résultats (fiche, réponse d'API) est un refus aussi. Aucun de ces cas ne
rend une liste vide.

Aucun test ne touche le réseau : le transport `httpx` est factice.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from mcp_car_compare.models import Criteria, SourceError, StructureChanged
from mcp_car_compare.sources.autoscout24 import Autoscout24

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "autoscout24"


def fixture(name: str) -> str:
    """Corps d'une page capturée."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(awaitable):
    return asyncio.run(awaitable)


class Recorder:
    """Transport factice : rend un corps et un statut fixes."""

    def __init__(self, body: str, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)


def search(body: str, status: int = 200, market: str = "fr"):
    """Joue une recherche sur la page donnée et rend la `Page` obtenue."""
    recorder = Recorder(body, status)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            source = Autoscout24(client, market)
            return await source.search(Criteria(markets=(market,)), 1)

    return run(scenario())


def test_html_sans_next_data_leve_structure_changed():
    # Une page de blocage n'a pas la charge attendue. La lire comme « aucune
    # annonce » annoncerait un marché vide : c'est le défaut que ce test
    # interdit.
    with pytest.raises(StructureChanged, match="__NEXT_DATA__"):
        search("<html><body>Unauthorized</body></html>")


def test_page_de_defi_ne_rend_rien_de_silencieux():
    # Page de défi anti-robot : HTTP 200, HTML complet, aucun __NEXT_DATA__.
    # C'est exactement ce qu'un site durci rend, et le statut ne le dit pas.
    defi = (
        "<!DOCTYPE html><html><head><title>Vérification en cours</title>"
        "<script>window.__challenge = 'datadome';</script></head>"
        "<body><p>Merci de patienter, vérification de votre navigateur…</p>"
        "</body></html>"
    )
    with pytest.raises(StructureChanged):
        search(defi)


def next_data_page(page_props: dict, *, body_extra: str = "") -> str:
    """Page minimale autour d'un `pageProps` fabriqué, corps libre en plus.

    Sert aux cas qu'aucune capture ne porte (marqueur de défi présent dans une
    page valide), et le fragment est écrit ici plutôt que dans une fixture pour
    qu'il ne se fasse jamais passer pour une page du site.
    """
    payload = json.dumps({"props": {"pageProps": page_props}})
    return (
        '<!DOCTYPE html><html><head><title>AutoScout24</title>'
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{payload}</script></head><body>{body_extra}</body></html>"
    )


def test_une_page_200_valide_contenant_le_marqueur_reste_une_page_de_resultats():
    # Décision D2 : `CHALLENGE_MARKER` n'est cherché que sur un statut non-200 ou
    # sans `__NEXT_DATA__`. Une page 200 qui porte sa charge est une page de
    # résultats, même si son texte cite l'hôte du défi : une annonce peut le
    # mentionner (lien de suivi, description), et juger une 200 sur une chaîne
    # ferait perdre une page entière de 20 annonces sans qu'aucun chiffre ne le
    # dise.
    annonce = {
        "id": "a",
        "url": "/offres/a",
        "price": {"priceRaw": 10_000},
        "vehicle": {"make": "Renault", "modelGroup": "Clio", "mileageInKm": "10 000 km"},
    }
    body = next_data_page(
        {"numberOfResults": 1, "listings": [annonce]},
        body_extra='<a href="https://geo.captcha-delivery.com/track?x=1">suivi</a>',
    )
    resultat = search(body)
    assert [annonce_lue.source_id for annonce_lue in resultat.listings] == ["a"]
    assert resultat.announced_total == 1


def test_un_200_sans_next_data_portant_le_marqueur_est_un_challenge():
    # Le cas que le marqueur existe pour reconnaître : un 200 sans charge
    # utile. La distinction est portée par `SourceError.status`, pas par le
    # message, pour que `coverage.status="challenge"` soit atteignable.
    defi = (
        '<!DOCTYPE html><html><body><iframe '
        'src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></iframe>'
        "</body></html>"
    )
    with pytest.raises(SourceError) as caught:
        search(defi)
    assert caught.value.status == "challenge"


def test_un_403_portant_le_marqueur_est_un_challenge_pas_une_erreur():
    with pytest.raises(SourceError) as caught:
        search("<html><body>captcha-delivery.com</body></html>", status=403)
    assert caught.value.status == "challenge"


def test_un_403_sans_marqueur_reste_une_erreur_de_source():
    # Sans cette assertion, `status="challenge"` pourrait être rendu pour tout
    # refus et ne plus rien distinguer.
    with pytest.raises(SourceError) as caught:
        search("<html><body>Forbidden</body></html>", status=403)
    assert caught.value.status == "error"


def test_next_data_illisible_leve_structure_changed():
    casse = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"listings": [ , ]}}}</script></head></html>'
    )
    with pytest.raises(StructureChanged, match="illisible"):
        search(casse)


def test_pageprops_absent_leve_source_error():
    # `__NEXT_DATA__` présent mais sans `props.pageProps` : réponse d'API ou
    # page d'erreur du site, pas une recherche.
    page = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {}}</script></head></html>'
    )
    with pytest.raises(SourceError):
        search(page)


def test_fiche_lue_comme_une_recherche_leve_source_error():
    # `detail_de.html` est une fiche réellement capturée : elle porte un
    # `__NEXT_DATA__` et un `pageProps`, mais aucune liste d'annonces. La lire
    # comme une page de résultats rendrait 0 annonce sans erreur.
    with pytest.raises(SourceError, match="sans liste d'annonces"):
        search(fixture("detail_de.html"), market="de")


def test_statut_403_leve_source_error():
    with pytest.raises(SourceError, match="403"):
        search("<html><body>Forbidden</body></html>", status=403)


def test_statut_500_leve_source_error():
    with pytest.raises(SourceError, match="500"):
        search("<html><body>Erreur du site</body></html>", status=500)


def test_redirection_jamais_suivie():
    # Une .fr redirigée vers .de serait analysée avec le marché .fr et ses
    # annonces porteraient le mauvais marché : la 3xx échoue au lieu d'être
    # suivie, quel que soit le client injecté.
    with pytest.raises(SourceError, match="302"):
        search("", status=302)


def test_erreur_reseau_leve_source_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Autoscout24(client, "fr").search(Criteria(markets=("fr",)), 1)

    with pytest.raises(SourceError, match="connexion refusée"):
        run(scenario())


def test_listings_pas_une_liste_leve_structure_changed():
    page = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"numberOfResults": 10, "listings": "oops"}}}'
        "</script></head></html>"
    )
    with pytest.raises(StructureChanged, match="listings"):
        search(page)


def test_recherche_vide_annoncee_par_le_site_n_est_pas_une_casse():
    # Frontière : le site DIT que la recherche est vide (0 annoncé, liste
    # vide). Ce n'est pas une casse — c'est une réponse. Une page muette sur
    # les deux points serait, elle, refusée (voir le test de la fiche).
    page = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"numberOfResults": 0, "listings": []}}}'
        "</script></head></html>"
    )
    resultat = search(page)
    assert resultat.listings == []
    assert resultat.announced_total == 0


def test_une_gamme_inconnue_du_site_est_un_refus_nomme_pas_zero_annonce():
    # Frontière : le site répond 404 sur un segment qu'il ne connaît pas. C'est
    # un refus explicite qui nomme le libellé dérivé, jamais « 0 annonce » — un
    # zéro se lirait comme un marché vide, et le libellé fautif ne serait
    # jamais corrigé.
    recorder = Recorder("<html>404</html>", status=404)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler)
        ) as client:
            await Autoscout24(client, "fr").search(
                Criteria(make="BMW", model="Gamme Inexistante", markets=("fr",)), 1
            )

    with pytest.raises(SourceError, match="libellé inconnu du site \\(404\\) : « BMW » / « Gamme Inexistante »"):
        run(scenario())
    assert recorder.requests[0].url.path == "/lst/bmw/gamme-inexistante"
