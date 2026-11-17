"""Adaptateur LaCentrale, par le side-car navigateur. France uniquement.

LaCentrale est protégée par DataDome : `curl` rend 403 et un HTTP simple ne lit
aucune carte (PLAN.md §2). Cet adaptateur ne parle donc jamais au site. Il poste
une adresse de recherche à un side-car Camoufox local — `POST
http://127.0.0.1:8899/scrape`, `{"url": …, "max_pages": n, "start_page": p}` —
qui rend les cartes déjà mises au contrat, et il les convertit en `Listing`.

Quatre règles, chacune payée par un défaut mesuré ou par une règle du plan :

- **Un seul appel par récolte, tout le lot en une `Page`.** Le side-car enchaîne
  les pages du site dans UNE session Camoufox (lancement ~30 s, résolution
  DataDome 6–75 s, 60 s mini entre deux lancements — PLAN.md §2). `search`
  demande donc au side-car de lire de la page reçue jusqu'au plafond
  `criteria.pages_max`, et les pages suivantes rendent une page vide SANS
  requête : redemander relancerait un navigateur pour des annonces déjà lues.
- **Une reprise repart de sa page.** `search(criteria, page_number)` demande
  `start_page=page_number` : mesuré le 2026-11-28, une récolte LaCentrale
  profonde dépasse le délai du client MCP (~8 s/page dans le navigateur) et
  expirait sans rien écrire. Sans page de départ, reprendre voulait dire relire
  depuis la page 1 — repayer les pages déjà payées et expirer de nouveau, donc
  aucune récolte profonde possible.
- **Un défi DataDome est un statut, pas zéro annonce.** Un 409 du side-car devient
  `SourceError(status="challenge")`, ce qui rend `coverage.status="challenge"`
  atteignable de bout en bout (règle du projet, PLAN.md §6 E4b).
- **Une lecture partielle se déclare.** Le `partial` du side-car traverse jusqu'à
  `coverage.truncated` par `Page.has_more` : une récolte qui s'arrête avant la fin
  ne doit jamais se lire comme une source couverte.
- **Le site ne filtre ni le carburant ni la boîte**, et ne trie pas par prix de
  façon mesurée : ces critères sont refusés (au parsage pour les deux premiers,
  ici pour le tri), jamais acceptés puis ignorés.

La forme de l'adresse de recherche n'est pas inventée : elle vient du chemin
applicatif de référence, `ICH/scraper/lacentrale.go` (`LacentraleSearchURL`), qui
l'a validée au lab.
"""

from __future__ import annotations

import httpx

from ..models import Criteria, Listing, Page, SourceError, StructureChanged
from .labels import fuel_from, gear_from, strip_accents

SOURCE_NAME = "lacentrale"

# Le site est français : le marché du contrat, le pays, et le seul que cette
# source sache interroger. Écrits une fois, cités partout ci-dessous.
MARKET = "fr"
COUNTRY = "FR"

SITE_URL = "https://www.lacentrale.fr"

# SIDECAR_URL est l'adresse du side-car. Il n'écoute que sur la boucle locale
# (PLAN.md §3, règle 3) : un processus voisin dans le conteneur ne doit pas
# pouvoir le piloter depuis l'extérieur.
SIDECAR_URL = "http://127.0.0.1:8899"

# SCRAPE_TIMEOUT_S borne l'appel au side-car, et il est passé PAR REQUÊTE : le
# client de la récolte a un délai de 30 s (tools.HTTP_TIMEOUT_S), qui couperait
# un appel légitime — mesuré, un lancement de Camoufox coûte ~30 s puis la
# résolution DataDome 6–75 s, d'où la garde de 150 s du plan (PLAN.md §2).
SCRAPE_TIMEOUT_S = 150.0

# SORT_PARAMS ne contient QUE l'ordre mesuré. `firstOnlineDateDesc` a été
# découvert par l'interface du site et vérifié au lab : c'est le tri par date de
# mise en ligne. Sans lui, la page est classée par pertinence et une lecture
# automatique ne voit jamais que les 24 mêmes annonces — le défaut mesuré sur
# AutoScout24, en pire.
#
# Les tris par prix n'ont AUCUN code mesuré : les deviner (« priceAsc » ?) puis
# les présenter comme appliqués serait le critère accepté et jamais posé que ce
# serveur refuse ailleurs. `search_url` refuse donc un tri non mesuré.
SORT_PARAMS = {"age_desc": "firstOnlineDateDesc"}


def model_value(label: str) -> str:
    """Un libellé dans la forme qu'emploie LaCentrale : majuscules, sans accent.

    Mesuré au lab : le site nomme ses marques et ses modèles en majuscules sans
    accent (« PEUGEOT », « SERIE 1 », « 208 »). Un « BMW::Série 1 » partait avec
    un accent encodé et une minuscule, et le site répondait « 0 annonce ».
    """
    return strip_accents(label.strip().lower()).upper()


def query_value(value: str) -> str:
    """Encode une valeur de requête LaCentrale en gardant « :: » lisible.

    `httpx.QueryParams` encoderait les deux-points (`PEUGEOT%3A%3A208`) ; la
    forme validée au lab les garde nus. L'encodage est donc écrit ici, à
    l'identique du chemin de référence : alphanumériques et `-_.~:` tels quels,
    tout le reste en `%XX` par octet UTF-8.
    """
    out: list[str] = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in "-_.~:"):
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out)


def search_url(criteria: Criteria) -> str:
    """L'adresse de recherche d'une page de résultats.

    `make` et `model` sont des LIBELLÉS, et le site les attend dans SON
    vocabulaire de requête : `makesModelsCommercialNames=PEUGEOT::208`, marque
    en majuscules puis modèle, séparés par « :: ». Ce n'est pas un chemin comme
    chez AutoScout24, donc `slug()` n'a rien à faire ici — un slug
    (`peugeot-208`) demande au site une marque qui n'existe pas.

    La pagination n'est pas dans l'adresse : le side-car pose lui-même le
    paramètre `page` du site en enchaînant jusqu'à `max_pages`.
    """
    if criteria.model and not criteria.make:
        raise SourceError(
            "choisis une marque avant le modèle : LaCentrale ne connaît pas de "
            "gamme hors d'une marque"
        )
    params: list[str] = []
    if criteria.make:
        value = model_value(criteria.make)
        if criteria.model:
            value = f"{value}::{model_value(criteria.model)}"
        params.append(f"makesModelsCommercialNames={query_value(value)}")
    # Une borne à zéro vaut « pas de filtre », donc paramètre absent : envoyer 0
    # demanderait « prix entre 0 et 0 € ».
    for key, bound in (
        ("priceMin", criteria.price_min),
        ("priceMax", criteria.price_max),
        ("mileageMax", criteria.km_max),
        ("yearMin", criteria.year_min),
        ("yearMax", criteria.year_max),
    ):
        if bound:
            params.append(f"{key}={bound}")
    sort = SORT_PARAMS.get(criteria.sort)
    if sort is None:
        raise SourceError(
            f"tri {criteria.sort!r} non mesuré chez LaCentrale : seul l'ordre "
            f"chronologique l'est ({', '.join(SORT_PARAMS)}) — retire ce tri ou "
            f"interroge AutoScout24 seul"
        )
    params.append(f"sortBy={sort}")
    return f"{SITE_URL}/listing?{'&'.join(params)}"


def _market_of(criteria: Criteria) -> str:
    """Le marché d'une page : celui des critères, et il ne peut valoir que `fr`.

    Deux refus distincts, parce qu'ils ne disent pas la même chose : plusieurs
    marchés dans une requête (une `Page` est celle d'un marché), et un marché
    non français — le site est français, le side-car ne lit que lui, et un `de`
    ne construirait pas une adresse qui répond.
    """
    markets = tuple(criteria.markets)
    if len(markets) != 1:
        raise SourceError(
            f"un seul marché par page : {len(markets)} demandé(s) {markets!r}"
        )
    if markets[0] != MARKET:
        raise SourceError(
            f"LaCentrale est française uniquement : marché {markets[0]!r} refusé "
            f"(attendu {MARKET!r}) — interroge AutoScout24 pour ce marché"
        )
    return markets[0]


def _text(value: object) -> str:
    """Champ texte du side-car, ou `""` : jamais la chaîne « None »."""
    return value if isinstance(value, str) else ""


def _int_or_none(value: object) -> int | None:
    """Entier du side-car, ou `None` : un compteur absent ne devient pas zéro."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def absolute_url(href: str) -> str:
    """Complète une adresse relative par l'origine du site, garde l'absolue.

    Le side-car lit le lien de la carte, que le site rend parfois relatif ; une
    adresse d'un autre hôte est gardée telle quelle plutôt que recopiée sous
    l'origine du site, qui serait fausse.
    """
    href = href.strip()
    if not href or href.startswith(("http://", "https://")):
        return href
    return f"{SITE_URL}/{href.lstrip('/')}"


def _from_vehicle(raw: dict) -> Listing:
    """Convertit un véhicule du side-car en `Listing` du contrat, sans perte.

    Le side-car rend déjà la forme du contrat (identifiant, libellés, prix et
    kilométrage entiers) : deux traductions seulement restent ici.

    - le marché et le pays : ils n'existent pas côté side-car. LaCentrale ne lit
      que lacentrale.fr, donc `fr` / `FR` — les recopier d'un champ du side-car
      laisserait une annonce française se dire allemande le jour où ce champ
      changerait de sens ;
    - le carburant et la boîte, passés par les tables PARTAGÉES de `labels.py`,
      qui connaissent déjà les libellés français du site (« Diesel », « Boîte
      automatique », « Auto ») et rendent `""` sur un libellé inconnu. Une
      deuxième table rendrait « diesel » comparable dans une source et inconnu
      dans l'autre, en silence.
    """
    return Listing(
        source=SOURCE_NAME,
        market=MARKET,
        source_id=_text(raw.get("source_id")),
        url=absolute_url(_text(raw.get("url"))),
        make=_text(raw.get("make")),
        group=_text(raw.get("group")),
        group_label=_text(raw.get("group_label")),
        engine=_text(raw.get("engine")),
        trim=_text(raw.get("trim")),
        year=_int_or_none(raw.get("year")),
        km=_int_or_none(raw.get("km")),
        price=_int_or_none(raw.get("price")),
        fuel=fuel_from(_text(raw.get("fuel"))),
        gear=gear_from(_text(raw.get("gear"))),
        seller=_text(raw.get("seller")),
        city=_text(raw.get("city")),
        postal_code=_text(raw.get("postal_code")),
        country=COUNTRY,
        image=_text(raw.get("image")),
        vin=None,
    )


def parse_scrape(payload: object, page_number: int = 1) -> Page:
    """Lit la réponse du side-car : une `Page` avec son total et son aveu.

    Une charge sans liste `vehicles` est une `StructureChanged` : le contrat du
    side-car a changé, et la lire comme « aucune annonce » ferait conclure à un
    marché vide. C'est la même règle que pour `__NEXT_DATA__` chez AutoScout24.
    """
    if not isinstance(payload, dict):
        raise StructureChanged(
            f"réponse du side-car : {type(payload).__name__} au lieu d'un objet"
        )
    vehicles = payload.get("vehicles")
    if not isinstance(vehicles, list):
        raise StructureChanged(
            "réponse du side-car sans liste `vehicles` : contrat changé"
        )
    listings: list[Listing] = []
    for raw in vehicles:
        if not isinstance(raw, dict):
            raise StructureChanged(
                f"véhicule illisible : {type(raw).__name__} au lieu d'un objet"
            )
        listings.append(_from_vehicle(raw))
    return Page(
        listings=listings,
        # Le compteur du SITE pour la requête entière, jamais `len(listings)` :
        # `None` quand le side-car ne l'a pas lu, et l'absence est dite.
        announced_total=_int_or_none(payload.get("announced_total")),
        page_number=max(page_number, 1),
        # `rendered` n'est pas déclaré : le contrat du side-car ne porte aucun
        # compteur de cartes RENDUES, donc la page a rendu exactement les
        # véhicules qu'elle porte. Les répétitions qu'une autre page de la même
        # récolte rapporterait, elles, sont comptées par la récolte dans
        # `dropped_duplicates`.
        #
        # `partial` : le side-car a rendu les pages lues puis s'est arrêté avant
        # la fin (défi au-delà de la page 1, ou total annoncé non atteint). Il
        # traverse jusqu'à `coverage.truncated` au lieu d'être perdu ici.
        has_more=payload.get("partial") is True,
    )


def _error_detail(response: httpx.Response) -> str:
    """Le `error` du side-car, s'il en donne un, pour que le message le nomme."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    detail = payload.get("error") if isinstance(payload, dict) else None
    return f" ({detail})" if isinstance(detail, str) and detail else ""


class Lacentrale:
    """Source LaCentrale, par le side-car navigateur. France uniquement.

    Le client HTTP est INJECTÉ et jamais créé ici : les tests passent un
    `httpx.MockTransport`, et aucun test ne touche ni le réseau ni un navigateur.
    Ce client porte la requête au side-car ; le site, lui, n'est jamais appelé
    directement (DataDome).

    `market` est celui des usages qui n'ont pas de marché dans leurs arguments —
    `brands()` — et il ne peut valoir que `fr`. `search()` prend le sien dans
    `criteria.markets` et refuse tout autre marché (voir `_market_of`).
    """

    source = SOURCE_NAME

    def __init__(
        self,
        client: httpx.AsyncClient,
        market: str = MARKET,
        *,
        sidecar_url: str = SIDECAR_URL,
        timeout_s: float = SCRAPE_TIMEOUT_S,
    ) -> None:
        if market != MARKET:
            raise SourceError(
                f"LaCentrale est française uniquement : marché {market!r} refusé "
                f"(attendu {MARKET!r})"
            )
        self.market = market
        self._client = client
        self._sidecar = sidecar_url.rstrip("/")
        self._timeout_s = timeout_s
        # Le lot déjà demandé DANS CET APPEL. Un adaptateur est construit par
        # récolte : la première page demandée fait lire au side-car toute la
        # suite du plafond, donc les appels suivants ne doivent plus rien lui
        # demander. C'est un drapeau et non un `page_number > 1` : une reprise
        # commence à la page 5 et doit demander son lot, là où une seconde page
        # à partir de 1 ne demande rien.
        self._lot_requested = False

    async def search(self, criteria: Criteria, page_number: int) -> Page:
        """Le lot de résultats d'une récolte, en une requête au side-car.

        `page_number` est la page du SITE où ce lot commence : 1 pour une
        récolte neuve, la page de reprise pour une reprise. Le side-car lit
        `pages_max - page_number + 1` pages à partir de là — la borne est celle
        du job, pas celle de l'appel, sinon une récolte reprise pourrait lire
        plus de pages que le plafond qu'elle s'est donné.

        Un second appel de la MÊME instance rend une page vide sans aucune
        requête : le premier a déjà demandé tout le lot, et redemander
        relancerait un navigateur pour des annonces déjà lues — l'aveu de
        troncature, lui, a été fait là-bas, par `has_more`.
        """
        _market_of(criteria)
        if self._lot_requested:
            return Page(listings=[], page_number=page_number)
        self._lot_requested = True
        payload = await self._scrape(
            search_url(criteria),
            max_pages=criteria.pages_max - page_number + 1,
            start_page=page_number,
        )
        return parse_scrape(payload, page_number)

    async def brands(self) -> list[dict]:
        """Aucun catalogue de marques par ce chemin, et c'est dit.

        Le side-car du lot ne lit que `/scrape`, une page de résultats. Rendre
        une liste vide serait pire qu'échouer : `list_brands` afficherait « ce
        site ne vend aucune marque ». Les libellés que `search` attend restent
        ceux du catalogue d'AutoScout24 — le chemin de référence envoie déjà ces
        libellés à LaCentrale, qui les met dans sa propre forme.
        """
        raise SourceError(
            "LaCentrale : aucun catalogue de marques par le side-car (il ne lit "
            'que /scrape) — lis les libellés avec list_brands("autoscout24") : '
            "ce sont ceux que search attend"
        )

    async def _scrape(self, url: str, *, max_pages: int, start_page: int) -> object:
        """Poste l'adresse au side-car et rend sa charge JSON.

        Les échecs sont nommés séparément parce qu'ils ne disent pas la même
        chose à l'appelant : un 409 est un défi DataDome (le site a refusé la
        lecture, réessayer plus tard peut marcher), un autre statut est une
        panne (`error`), et une réponse illisible est une `StructureChanged` —
        le contrat du side-car a changé, pas le site.
        """
        try:
            response = await self._client.post(
                f"{self._sidecar}/scrape",
                json={"url": url, "max_pages": max_pages, "start_page": start_page},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise SourceError(
                f"side-car LaCentrale injoignable ({self._sidecar}) : {exc}"
            ) from exc
        if response.status_code == 409:
            raise SourceError(
                f"défi anti-bot (DataDome) sur LaCentrale : le side-car n'a lu "
                f"aucune annonce ({url})",
                status="challenge",
            )
        if response.status_code != 200:
            raise SourceError(
                f"side-car LaCentrale : statut {response.status_code} pour {url}"
                f"{_error_detail(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise StructureChanged(f"réponse du side-car illisible : {exc}") from exc
