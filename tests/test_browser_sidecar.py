"""Le side-car navigateur, éprouvé sans navigateur.

Aucun test ne lance Camoufox : les pages sont les fixtures de
`tests/fixtures/lacentrale/`, et une page factice répond aux quatre appels que
le side-car fait à une vraie (`goto`, `wait_for_selector`, `query_selector*`,
`title`). Le faux DOM en bas de ce fichier n'interprète que les trois formes de
sélecteur que le side-car emploie — c'est ce qui permet d'éprouver les
SÉLECTEURS (la partie fragile) sans payer 30 s de lancement.

La route HTTP elle-même est éprouvée dans `test_browser_sidecar_route.py` :
elle a besoin de FastAPI, que l'image de test n'installe pas, alors que tout ce
fichier-ci tourne dans `Dockerfile.test`.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from html.parser import HTMLParser

import pytest

from mcp_car_compare import browser_sidecar as sidecar

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "lacentrale"

# Les dix-sept clés du contrat, écrites ici en dur : c'est le contrat que
# l'adaptateur lit, et une clé renommée doit faire échouer ce test, pas se
# propager en silence dans `sources/lacentrale.py`.
VEHICLE_KEYS = {
    "source_id", "url", "make", "group", "group_label", "engine", "trim",
    "year", "km", "price", "fuel", "gear", "seller", "city", "postal_code",
    "country", "image",
}


def fixture(name: str) -> str:
    """Corps d'une page de test."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(awaitable):
    """Exécute un scénario async sans plugin de test asynchrone."""
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _first_launch():
    """Aucun test n'attend les 60 s de cadence : le side-car croit n'avoir
    jamais lancé de navigateur. Sans cela, le premier test qui scrolle rendrait
    le suivant dépendant de l'ordre d'exécution, par un sommeil réel.
    """
    sidecar._last_launch = None
    yield
    sidecar._last_launch = None


def first_page(name: str) -> FakePage:
    """Une page factice déjà chargée avec la fixture `name` (sans navigation)."""
    page = FakePage([])
    page.node = tree(fixture(name))
    return page


# --- Contrat de la carte -----------------------------------------------------

def test_une_page_de_resultats_rend_les_dix_sept_champs_du_contrat():
    vehicles = run(sidecar.read_page(first_page("search_fr.html"), 1))

    # Quatre cartes dans la fixture, dont un doublon exact : trois annonces.
    assert len(vehicles) == 3
    for vehicle in vehicles:
        assert set(vehicle) == VEHICLE_KEYS
    assert vehicles[0] == {
        "source_id": "69119906827",
        "url": "https://www.lacentrale.fr/auto-occasion-annonce-69119906827.html",
        "make": "RENAULT",
        "group": "clio",
        "group_label": "CLIO",
        # Vide : la version (« 1.8 E-TECH 156 EVOLUTION + ») est dans `trim`,
        # entière, parce que le site ne sépare pas la motorisation de la
        # finition.
        "engine": "",
        "trim": "1.8 E-TECH 156 EVOLUTION +",
        "year": 2026,
        "km": 10857,
        "price": 23490,
        "fuel": "hybride",
        "gear": "auto",
        "seller": "",
        "city": "",
        "postal_code": "",
        "country": "FR",
        "image": "https://www.lacentrale.fr/medias/E119906827_STANDARD_0.jpg",
    }


def test_la_generation_du_titre_n_est_pas_prise_pour_une_motorisation():
    # Mesuré sur le site : le titre porte MARQUE + GAMME + GÉNÉRATION, le
    # sous-titre la version. Mettre la génération dans `engine` (le premier
    # défaut de ce module) ferait passer « IV phase 2 » pour une motorisation, et
    # la garder dans la gamme casserait la clé de rapprochement avec
    # AutoScout24, qui publie « clio ».
    assert sidecar.split_title("RENAULT CLIO IV phase 2", "1.5 DCI 90 BUSINESS") == (
        "RENAULT", "CLIO", "1.5 DCI 90 BUSINESS",
    )


def test_les_puces_donnent_l_annee_les_km_le_carburant_et_la_boite():
    vehicles = run(sidecar.read_page(first_page("search_fr.html"), 1))

    # Carte 2 : les libellés mesurés de LaCentrale (« Manuelle », « Diesel »),
    # dans l'ordre réel des puces (année, boîte, km, carburant), et une vignette
    # différée en `data-src`.
    assert (vehicles[1]["year"], vehicles[1]["km"]) == (2019, 49200)
    assert (vehicles[1]["fuel"], vehicles[1]["gear"]) == ("diesel", "manuelle")
    assert vehicles[1]["image"] == (
        "https://pictures.lacentrale.fr/classifieds/E120343648_STANDARD_0.jpg"
    )


def test_une_puce_de_carburant_inconnue_reste_vide_au_lieu_d_etre_devinee():
    assert sidecar.read_specs(["Erdgas (CNG)", "2020"]) == (2020, None, "", "")


def test_une_gamme_en_deux_mots_est_lue_entiere():
    # « BMW SERIE 3 G20 » : sans le mot de gamme, la gamme serait « SERIE » et
    # ne rencontrerait jamais le « serie 3 » d'AutoScout24.
    vehicles = run(sidecar.read_page(first_page("search_fr.html"), 1))

    assert (vehicles[2]["make"], vehicles[2]["group_label"]) == ("BMW", "SERIE 3")
    assert vehicles[2]["group"] == "serie 3"
    assert vehicles[2]["engine"] == ""


def test_une_carte_sans_vignette_rend_une_image_vide():
    vehicles = run(sidecar.read_page(first_page("search_fr.html"), 1))

    assert vehicles[2]["image"] == ""


def test_un_prix_sans_chiffre_reste_none():
    assert sidecar.read_price("13 690 €") == 13690
    assert sidecar.read_price("Prix non communiqué") is None


def test_l_identifiant_est_lu_dans_les_deux_formes_d_adresse_du_site():
    assert sidecar.source_id("https://www.lacentrale.fr/auto-occasion-annonce-69116056.html") == "69116056"
    assert sidecar.source_id(
        "https://www.lacentrale.fr/listing/renault-clio-auto-occasion-annonce-69116057.html"
    ) == "69116057"
    # Une adresse qui ne porte pas le marqueur rend son chemin : l'annonce reste
    # dédoublonnable et citable, au lieu d'avoir un identifiant vide.
    assert sidecar.source_id("https://www.lacentrale.fr/annonce/abc") == "annonce/abc"


def test_le_compteur_annonce_est_celui_de_la_recherche_pas_de_la_page():
    text = run(sidecar.body_text(first_page("search_fr.html")))

    assert sidecar.read_announced_total(text) == 13481
    # Le site se taît : l'absence est dite, jamais comblée par le nombre de
    # cartes lues.
    assert sidecar.read_announced_total("Aucune annonce sur cette page") is None


# --- Requête -----------------------------------------------------------------

def test_le_side_car_ecoute_sur_le_port_que_l_entrypoint_sonde():
    # Deux fichiers portent ce port, et un désaccord ne se voit pas : l'entrypoint
    # attendrait 30 s un port que personne n'écoute, puis démarrerait le serveur
    # MCP avec LaCentrale en erreur sans qu'aucune ligne ne dise pourquoi.
    #
    # `entrypoint.sh` est dans l'image de production, pas dans l'image de test
    # (Dockerfile.test ne copie que src/ et tests/) : ce test s'y saute, et c'est
    # dit ici plutôt que découvert en croyant qu'il a tourné.
    entrypoint = pathlib.Path(__file__).resolve().parent.parent / "entrypoint.sh"
    if not entrypoint.is_file():
        pytest.skip("entrypoint.sh absent de cette image (image de test)")

    assert f"127.0.0.1:{sidecar.LISTEN_PORT}/health" in entrypoint.read_text(encoding="utf-8")
    assert sidecar.LISTEN_PORT == 8899


@pytest.mark.parametrize("url", [
    "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT::208",
    "http://lacentrale.fr/listing",
    "https://www.lacentrale.fr:443/listing",
])
def test_une_url_de_lacentrale_est_acceptee(url):
    assert sidecar.check_url(url) == url


@pytest.mark.parametrize("url", [
    "https://example.com/listing",
    # Sous-domaine déguisé : `www.lacentrale.fr.evil.com` n'est pas lacentrale.fr.
    "https://www.lacentrale.fr.evil.com/listing",
    # Antislash : `urlsplit` n'en fait pas un séparateur, tous les navigateurs
    # si — l'hôte lu serait alors 169.254.169.254.
    "https://169.254.169.254\\.www.lacentrale.fr/",
    "https://user:secret@www.lacentrale.fr/listing",
    "ftp://www.lacentrale.fr/listing",
    "https://www.lacentrale.fr/listing avec espace",
])
def test_toute_autre_adresse_est_refusee(url):
    with pytest.raises(sidecar.BadRequest):
        sidecar.check_url(url)


@pytest.mark.parametrize("raw,attendu", [
    (None, 1),
    (1, 1),
    (5, 5),
    # « Aucune page » n'est pas une lecture : cela vaudrait une requête qui a
    # l'air de tourner sans rien lire.
    (0, 1),
    (-3, 1),
    (10**6, sidecar.MAX_PAGES_CAP),
])
def test_le_nombre_de_pages_est_borne(raw, attendu):
    assert sidecar.allowed_pages(raw) == attendu


def test_un_nombre_de_pages_non_entier_est_refuse():
    with pytest.raises(sidecar.BadRequest):
        sidecar.allowed_pages("3")


def test_l_adresse_de_la_page_suivante_ajoute_le_parametre_de_page():
    url = "https://www.lacentrale.fr/listing?makesModelsCommercialNames=BMW::SERIE 3&page=1"

    assert sidecar.page_url(url, 1) == url
    # Le paramètre est REMPLACÉ, pas dupliqué : `page=1&page=2` se lirait mal.
    assert sidecar.page_url(url, 2).endswith("&page=2")
    assert sidecar.page_url(url, 2).count("page=") == 1


def test_deux_lancements_de_navigateur_sont_espaces_de_soixante_secondes(monkeypatch):
    waits: list[float] = []
    now = [1000.0]

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(sidecar, "_clock", lambda: now[0])
    monkeypatch.setattr(sidecar, "_sleep", fake_sleep)
    monkeypatch.setattr(sidecar, "_last_launch", None)
    async def two_launches() -> None:
        for _ in range(2):
            async with sidecar._launch_turn():
                now[0] += 5.0  # un scrape de cinq secondes

    run(two_launches())

    # Le PREMIER lancement n'attend pas, le second attend le reste des 60 s.
    assert waits == [sidecar.LAUNCH_INTERVAL_S - 5.0]
    assert sidecar.LAUNCH_INTERVAL_S == 60.0


# --- Défi et pages -----------------------------------------------------------------

def test_une_page_de_defi_est_reconnue_et_une_page_de_resultats_non():
    challenge = first_page("challenge.html")
    search = first_page("search_fr.html")

    assert run(sidecar.is_challenge(challenge, run(sidecar.body_text(challenge))))
    assert not run(sidecar.is_challenge(search, run(sidecar.body_text(search))))


def test_un_defi_sur_la_page_1_leve_challenge_et_ne_rend_pas_zero_annonce():
    browser = FakeBrowser(fixture("challenge.html"))

    with pytest.raises(sidecar.Challenge):
        run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 3,
                                 browser_factory=session(browser)))


def test_une_page_1_sans_carte_et_sans_defi_echoue_au_lieu_de_rendre_zero():
    browser = FakeBrowser(EMPTY_PAGE)

    with pytest.raises(sidecar.ScrapeFailed):
        run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 3,
                                 browser_factory=session(browser)))


def test_chaque_navigation_ouvre_une_page_neuve_dans_la_meme_session():
    browser = FakeBrowser(fixture("search_fr.html"), fixture("search_fr.html"))

    result = run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 2,
                                      browser_factory=session(browser)))

    assert len(browser.opened) == 2
    assert browser.opened[0] is not browser.opened[1]
    # La page 2 est demandée par son paramètre, pas par la même adresse.
    assert browser.opened[1].visited[0].endswith("&page=2") or "page=2" in browser.opened[1].visited[0]
    assert result["pages_read"] == 2


def test_une_annonce_vue_a_la_page_1_n_est_pas_rendue_une_seconde_fois():
    # La même page deux fois : c'est le recouvrement mesuré d'une carte
    # sponsorisée qui revient en tête de chaque page.
    browser = FakeBrowser(fixture("search_fr.html"), fixture("search_fr.html"))

    result = run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 2,
                                      browser_factory=session(browser)))

    assert len(result["vehicles"]) == 3
    assert result["partial"] is True  # 3 lues sur 13 481 annoncées


def test_un_plafond_de_pages_atteint_est_declare_partiel():
    browser = FakeBrowser(fixture("search_fr.html"))

    result = run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 1,
                                      browser_factory=session(browser)))

    assert result["announced_total"] == 13481
    assert result["pages_read"] == 1
    assert result["partial"] is True


def test_une_fin_de_stock_n_est_pas_declaree_partielle():
    # Une carte rendue, une annonce annoncée, une page suivante vide : tout a
    # été lu. Un `partial: true` ici rendrait toute récolte LaCentrale tronquée.
    browser = FakeBrowser(fixture("search_end_of_stock.html"), EMPTY_PAGE)

    result = run(sidecar.scrape_pages("https://www.lacentrale.fr/listing", 2,
                                      browser_factory=session(browser)))

    assert result["announced_total"] == 1
    assert len(result["vehicles"]) == 1
    assert result["pages_read"] == 1
    assert result["partial"] is False


# --- Un DOM minuscule, juste assez pour les sélecteurs du side-car -----------
# Il n'interprète que trois formes — `tag[attr*='valeur']`, `[attr*='valeur']`
# et `tag.classe` — parce que ce sont les seules que le side-car emploie. Un
# sélecteur d'une autre forme lève : un sélecteur ajouté au side-car sans être
# couvert ici ne peut pas passer pour éprouvé.

_SELECTOR_ATTRIBUTE = re.compile(
    r"^(?P<tag>[a-z0-9]*)\[(?P<name>[a-z-]+)\*=['\"](?P<value>[^'\"]*)['\"]\]$"
)
_SELECTOR_CLASS = re.compile(r"^(?P<tag>[a-z0-9]*)\.(?P<name>[a-z0-9_-]+)$")
_SELECTOR_TAG = re.compile(r"^[a-z][a-z0-9]*$")
_VOID_TAGS = {"img", "br", "input", "meta", "link", "hr", "source"}
EMPTY_PAGE = (
    "<html><head><title>Voiture occasion - La Centrale</title></head>"
    "<body></body></html>"
)


class Node:
    """Un nœud du faux DOM : balise, attributs, enfants, morceaux de texte."""

    def __init__(self, tag: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parts: list[str] = []

    def text(self) -> str:
        return "".join(self.parts) + "".join(child.text() for child in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._open = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._open.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, 0, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data):
        self._open[-1].parts.append(data)


def tree(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    return builder.root


def _matches(node: Node, selector: str) -> bool:
    attribute = _SELECTOR_ATTRIBUTE.match(selector)
    if attribute:
        tag, name, value = attribute["tag"], attribute["name"], attribute["value"]
        return (not tag or node.tag == tag) and value in node.attrs.get(name, "")
    klass = _SELECTOR_CLASS.match(selector)
    if klass:
        tag, name = klass["tag"], klass["name"]
        return (not tag or node.tag == tag) and name in node.attrs.get("class", "").split()
    if _SELECTOR_TAG.match(selector):
        return node.tag == selector
    raise AssertionError(f"sélecteur non géré par le faux DOM : {selector!r}")


def query(node: Node, selector: str) -> list[Node]:
    found: list[Node] = []
    for child in node.children:
        if _matches(child, selector):
            found.append(child)
        found.extend(query(child, selector))
    return found


class FakeElement:
    """Ce qu'un `ElementHandle` de Playwright offre au side-car, et rien de plus."""

    def __init__(self, node: Node) -> None:
        self.node = node

    async def get_attribute(self, name: str) -> str | None:
        return self.node.attrs.get(name)

    async def inner_text(self) -> str:
        return self.node.text()

    async def query_selector(self, selector: str) -> FakeElement | None:
        found = query(self.node, selector)
        return FakeElement(found[0]) if found else None

    async def query_selector_all(self, selector: str) -> list[FakeElement]:
        return [FakeElement(node) for node in query(self.node, selector)]


class FakePage(FakeElement):
    """Une page factice qui rend un HTML par navigation, dans l'ordre fourni."""

    def __init__(self, pages_html: list[str]) -> None:
        super().__init__(Node("#document"))
        self._pages_html = list(pages_html)
        self.visited: list[str] = []

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.visited.append(url)
        self.node = tree(self._pages_html.pop(0) if self._pages_html else EMPTY_PAGE)

    async def wait_for_selector(self, selector: str, timeout: float | None = None):
        return await self.query_selector(selector)

    async def title(self) -> str:
        found = query(self.node, "title")
        return found[0].text().strip() if found else ""

    async def close(self) -> None:
        return None


class FakeBrowser:
    """Une session factice : `new_page` rend une page neuve à chaque appel."""

    def __init__(self, *pages_html: str) -> None:
        self._pages_html = list(pages_html)
        self.opened: list[FakePage] = []

    async def new_page(self) -> FakePage:
        html = self._pages_html.pop(0) if self._pages_html else EMPTY_PAGE
        page = FakePage([html])
        self.opened.append(page)
        return page


class Session:
    """Le contexte `async with` que `open_browser` rend sur le vrai Camoufox."""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    async def __aenter__(self) -> FakeBrowser:
        return self._browser

    async def __aexit__(self, *_exc) -> bool:
        return False


def session(browser: FakeBrowser):
    return lambda: Session(browser)
