"""Adaptateur AutoScout24 (.fr et .de), sans navigateur.

Porté de deux fichiers de référence, en lecture seule :
`ICH/market/autoscout/client.go` (la charge `__NEXT_DATA__` et son décodage, les
tables de libellés, la normalisation des gammes) et
`ICH/scraper/autoscout_fetch.go` (cadence entre pages, en-têtes, refus des
redirections).

Les deux marchés rendent leurs annonces entières dans un unique JSON logé dans
`<script id="__NEXT_DATA__">` du HTML, donc un GET suffit : pas de navigateur,
pas de rendu JS à attendre. Mesuré le 2026-11-12 : 200 en 0,3 s, 20 annonces par
page, 20 pages maximum par requête.

Trois règles de lecture, chacune payée par un défaut observé ailleurs :

- une page sans `__NEXT_DATA__` est une casse (défi, erreur du site) et lève
  `StructureChanged` : la confondre avec « aucune annonce » ferait conclure à un
  marché désert ;
- `Page.announced_total` est le compteur du site pour la requête entière, jamais
  `len(listings)` ; quand le site se tait il vaut `None`, l'absence est dite ;
- le dédoublonnage intra-page ne retouche pas `announced_total` : c'est le
  compteur du site, la seule mesure de couverture annoncée.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable

import httpx

from ..models import Criteria, Listing, Page, SourceError, StructureChanged
from .labels import fuel_from, gear_from, seller_from, strip_accents

SOURCE_NAME = "autoscout24"

# PAGE_PAUSE_S sépare deux pages lues. Mesuré : le site répond en 0,3 s, donc
# 800 ms laisse une marge confortable sans coûter cher (une page tient 20
# annonces, et le site plafonne à 20 pages par requête). La pause vaut entre
# deux pages et jamais avant la première : payer 800 ms pour ne rien espacer
# serait du temps perdu dans un budget de récolte.
PAGE_PAUSE_S = 0.8

# USER_AGENT est l'agent annoncé au site : celui du chemin applicatif de
# référence, pour qu'un seul robot se présente de la même façon.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)

# ACCEPT_LANGUAGE porte, par marché, l'en-tête qui décide de la langue des
# libellés rendus par le site (« Boîte automatique » contre « Automatik »,
# « Série 3 » contre « 3er »). Mesuré : sans lui le site choisit d'après
# l'adresse IP de l'appelant, donc la langue du serveur, pas celle du marché.
ACCEPT_LANGUAGE = {
    "fr": "fr-FR,fr;q=0.9",
    "de": "de-DE,de;q=0.9",
}

# IMAGE_HOST est le seul hôte d'où une vignette est acceptée. Mesuré le
# 2026-11-06 sur une page de chaque marché : 477 vignettes sur .fr, 420 sur .de,
# toutes sur cet hôte. Une adresse hors de là vient du corps de la réponse, pas
# du site : elle est ignorée plutôt que recopiée en base.
IMAGE_HOST = "prod.pictures.autoscout24.net"

# FUEL_PARAMS traduit le carburant du contrat en code du site. Les quatre codes
# sont MESURÉS le 2026-11-12, une requête à la fois, sur une requête de
# référence .fr : `fuel=B` rend 20 « Essence », `fuel=D` des diesel, `fuel=E`
# rend 20 « Electrique », `fuel=2%2C3` rend 20 « Électrique/Essence ».
# « hybride » recouvre deux codes — 2 (électrique/essence) et 3
# (électrique/diesel) — et c'est la valeur que le formulaire du site envoie.
# Un carburant absent de cette table n'ajoute AUCUN paramètre : aucun code n'est
# deviné, et la requête reste celle d'avant pour qui ne demande rien.
FUEL_PARAMS = {
    "essence": "B",
    "diesel": "D",
    "electrique": "E",
    "hybride": "2,3",
}

# GEAR_PARAMS traduit la boîte du contrat en code du site. Mesuré le
# 2026-11-12 : « gear=A » rend 103 annonces là où la même requête sans le
# paramètre en rend 464 (dont 27 % de manuelles), donc le filtre agit.
GEAR_PARAMS = {
    "auto": "A",
    "manuelle": "M",
}

# SORT_PARAMS traduit l'ordre demandé en paramètres du site : « price » + desc=0
# pour le prix croissant, « age » + desc=1 pour les plus récentes. Un tri inconnu
# retombe sur les plus récentes, qui est le défaut du site.
SORT_PARAMS = {
    "price_asc": ("price", "0"),
    "price_desc": ("price", "1"),
    "age_desc": ("age", "1"),
}

# _NEXT_DATA isole le JSON embarqué dans la page.
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Les gammes sont nommées dans la langue du domaine : mesuré le 2026-11-06, la
# même voiture s'appelle « Série 3 » en France et « 3er » en Allemagne,
# « Classe C » contre « C-Klasse ». Ancrés en début et non en fin, parce que le
# site accole la carrosserie au libellé (« Série 3 Touring », « 3er Touring »).
_SERIES_DE = re.compile(r"^(\d{1,2})er(?:\s+(.*))?$")
_SERIES_FR = re.compile(r"^serie\s+([a-z0-9]+)(?:\s+(.*))?$")
_CLASS_DE = re.compile(r"^([a-z]{1,4})-klasse(?:\s+(.*))?$")
_CLASS_FR = re.compile(r"^classe\s+([a-z0-9]+)(?:\s+(.*))?$")


def market_base(market: str) -> str:
    """Domaine du marché, ou `SourceError` : un marché inconnu construirait une
    adresse qui répond 404, donc un « aucune annonce » trompeur."""
    if market not in ACCEPT_LANGUAGE:
        raise SourceError(f"marché inconnu : {market!r} (attendu 'fr' ou 'de')")
    return f"https://www.autoscout24.{market}"


def normalize_group(label: str) -> str:
    """Ramène un libellé de gamme à sa forme comparable entre marchés.

    « Série 3 », « 3er », « Classe C » et « C-Klasse » se rejoignent sur
    « serie 3 » et « serie c » ; le reste est laissé tel quel, en minuscules sans
    accents (« a3 », « clio », « x1 »). La carrosserie qui suit est conservée,
    normalisée mais non traduite : deux breaks nommés pareil dans les deux
    langues se rejoignent, les autres restent séparés, ce qui vaut mieux qu'un
    rapprochement inventé.

    « Autres » et « Sonstige » désignent des annonces que le site n'a pas su
    classer : elles rendent la chaîne vide, donc aucun segment — comparer « le
    tas de tout ce qui n'est pas comparable » n'aurait aucun sens.
    """
    words = " ".join(strip_accents(label.lower()).split())
    if words in ("", "autres", "sonstige", "others"):
        return ""
    for pattern in (_SERIES_DE, _SERIES_FR, _CLASS_DE, _CLASS_FR):
        match = pattern.match(words)
        if match is not None:
            tail = " ".join((match.group(2) or "").split())
            return " ".join(part for part in ("serie", match.group(1), tail) if part)
    return words


def image_url(raw: str) -> str:
    """Valide une adresse de vignette : HTTPS, et hôte exactement `IMAGE_HOST`
    ou l'un de ses sous-domaines. Sinon `""`."""
    try:
        parsed = httpx.URL(raw.strip())
    except (httpx.InvalidURL, TypeError):
        return ""
    if parsed.scheme != "https" or parsed.userinfo or parsed.port is not None:
        return ""
    host = parsed.host.lower()
    if host == IMAGE_HOST or host.endswith("." + IMAGE_HOST):
        return str(parsed)
    return ""


def search_url(criteria: Criteria, page_number: int, market: str) -> str:
    """Assemble l'adresse d'une page de résultats pour un marché.

    Le marché est passé séparément plutôt que lu dans `criteria.markets` : une
    adresse vise un seul domaine, et `search()` refuse d'en recevoir plusieurs
    (voir `_market_of`).
    """
    base = market_base(market)
    # Le site attend des identifiants NUMÉRIQUES dans mmmv (13 pour BMW, 9|1624
    # pour l'Audi A3). Un slug ou un libellé envoyé à leur place ne filtre rien
    # et rendrait la marque entière en se présentant comme une recherche de
    # modèle : il est refusé ici, jamais envoyé.
    make = _site_id(criteria.make, "marque")
    model = _site_id(criteria.model, "modèle")
    if model and not make:
        # « mmmv=|1624|| » est malformé : le site ne saurait pas le lire.
        raise SourceError("choisis une marque avant le modèle")

    params = {
        "atype": "C",  # voitures seulement
        "ustate": "N,U",  # neuf et occasion
        "sort": SORT_PARAMS.get(criteria.sort, SORT_PARAMS["age_desc"])[0],
        "desc": SORT_PARAMS.get(criteria.sort, SORT_PARAMS["age_desc"])[1],
        # Une page part de 1 chez le site : une valeur plus petite demanderait
        # une page inexistante et lirait un résultat vide.
        "page": str(max(page_number, 1)),
        # mmmv = marque|modèle|gamme|variante. Le modèle n'occupe que la
        # deuxième case ; sans modèle, les trois dernières restent vides.
        "mmmv": f"{make}|{model}||" if model else f"{make}|||",
    }
    # Une borne à zéro vaut « pas de filtre », donc paramètre absent : envoyer
    # 0 demanderait « prix entre 0 et 0 € ».
    for key, bound in (
        ("pricefrom", criteria.price_min),
        ("priceto", criteria.price_max),
        ("kmto", criteria.km_max),
        ("fregfrom", criteria.year_min),
        ("fregto", criteria.year_max),
    ):
        if bound:
            params[key] = str(bound)
    fuel_code = FUEL_PARAMS.get(criteria.fuel)
    if fuel_code:
        params["fuel"] = fuel_code
    gear_code = GEAR_PARAMS.get(criteria.gear)
    if gear_code:
        params["gear"] = gear_code
    return f"{base}/lst?{httpx.QueryParams(params)}"


def _site_id(value: str, what: str) -> str:
    """Identifiant numérique du site, ou `SourceError` (règle de `core.IsSiteID`)."""
    if value and not all("0" <= char <= "9" for char in value):
        raise SourceError(
            f"{what} {value!r} : identifiant numérique attendu (par exemple 9 pour Audi)"
        )
    return value


def _market_of(criteria: Criteria) -> str:
    """Le marché d'une page : celui des critères, et un seul.

    `Page` décrit une page d'UN marché, donc une requête qui en demande deux ne
    peut pas être rendue sans en perdre un en silence. Le refus est explicite,
    et l'appelant découpe ses marchés — sinon les annonces d'un marché
    porteraient le `market` de l'autre, et deux clés d'identité se
    confondraient.
    """
    markets = tuple(criteria.markets)
    if len(markets) != 1:
        raise SourceError(f"un seul marché par page : {len(markets)} demandé(s) {markets!r}")
    return markets[0]


def _int_or_none(value: object) -> int | None:
    """Entier du site, ou `None` : un compteur absent ne devient pas zéro."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _digits(label: str) -> int | None:
    """Ne garde que les chiffres d'un libellé, ou `None` s'il n'y en a pas.

    Nécessaire parce que le site écrit le kilométrage dans la convention du
    marché : « 202 000 km » en France (espace insécable étroite), « 80.388 km »
    en Allemagne (point). Extraire les chiffres donne 202000 et 80388.
    """
    digits = "".join(char for char in label if "0" <= char <= "9")
    return int(digits) if digits else None


def _year_from(first_registration: str) -> int | None:
    """Année de « MM-YYYY » (`tracking.firstRegistration`), ou `None`."""
    _, _, year = first_registration.partition("-")
    if not year.isdigit():
        return None
    value = int(year)
    return value if value >= 1900 else None


def _from_json(raw: dict, market: str, base: str) -> Listing:
    """Normalise une annonce de page de résultats vers le contrat."""
    vehicle = raw.get("vehicle") or {}
    price = raw.get("price") or {}
    location = raw.get("location") or {}
    seller = raw.get("seller") or {}
    tracking = raw.get("tracking") or {}
    # Le site donne une adresse relative (« /offres/… »). Une valeur d'une autre
    # forme laisse l'URL vide plutôt que de composer une adresse fausse.
    path = raw.get("url") or ""
    group_label = vehicle.get("modelGroup") or ""
    raw_price = price.get("priceRaw")
    images = raw.get("images") or []
    return Listing(
        source=SOURCE_NAME,
        market=market,
        # Une annonce sans identifiant n'est pas dédoublonnable, mais elle reste
        # lisible : elle passe avec `source_id` vide.
        source_id=str(raw.get("id") or ""),
        url=f"{base}{path}" if path.startswith("/") else "",
        make=vehicle.get("make") or "",
        group=normalize_group(group_label),
        group_label=group_label,
        engine=vehicle.get("motorTypeName") or "",
        # `trim` reste vide : la finition demanderait le portage de
        # `ICH/market/autoscout/trim.go`, hors de ce lot. Vide vaut « pas
        # mesuré », pas « pas de finition ».
        trim="",
        year=_year_from(tracking.get("firstRegistration") or ""),
        km=_digits(vehicle.get("mileageInKm") or ""),
        price=raw_price if _int_or_none(raw_price) is not None else None,
        fuel=fuel_from(vehicle.get("fuel") or ""),
        gear=gear_from(vehicle.get("transmission") or ""),
        vat_label=price.get("vatLabel") or "",
        seller=seller_from(seller.get("type") or ""),
        city=location.get("city") or "",
        postal_code=location.get("zip") or "",
        country=location.get("countryCode") or "",
        # Première vignette seulement : les autres sont la même photo en plus
        # grand, et le contrat ne porte que ce que la liste affiche.
        image=image_url(images[0]) if images else "",
        vin=None,
    )


def _dedupe(listings: list[Listing]) -> list[Listing]:
    """Retire les annonces de même clé DANS la page rendue.

    Mesuré le 2026-11-12 sur la page 1 d'AutoScout24 .de : 20 annonces rendues
    par le site, 19 identifiants uniques. Le PREMIER exemplaire d'une clé est
    gardé, à sa place : l'ordre du site est préservé et deux lectures de la même
    page rendent la même chose.

    Ce dédoublonnage ne rend pas `announced_total` plus juste : il ne le touche
    pas du tout, c'est le compteur du site. Et il ne voit pas deux pages qui se
    recouvrent — cela reste le travail de la récolte.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[Listing] = []
    for listing in listings:
        if listing.source_id:
            key = listing.key()
            if key in seen:
                continue
            seen.add(key)
        unique.append(listing)
    return unique


def _next_data(body: str) -> str:
    """JSON embarqué dans la page.

    Son absence est une casse et non un résultat vide : c'est la signature d'une
    page de blocage ou d'une erreur du site, et la confondre avec « aucune
    annonce » ferait conclure à un marché désert.
    """
    match = _NEXT_DATA.search(body)
    if match is None:
        raise StructureChanged("bloc __NEXT_DATA__ absent de la page")
    return match.group(1)


def _page_props(body: str) -> dict:
    """`props.pageProps` de la page, ou une erreur nommée.

    `pageProps` absent est ce qui distingue une page de blocage (ou une erreur
    d'API du site) d'une recherche : les deux rendraient sinon « 0 annonce »
    sans erreur.
    """
    try:
        document = json.loads(_next_data(body))
    except ValueError as exc:
        raise StructureChanged(f"__NEXT_DATA__ illisible : {exc}") from exc
    props = document.get("props") if isinstance(document, dict) else None
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    if not isinstance(page_props, dict):
        raise SourceError("page sans données : page de blocage ou réponse d'erreur du site")
    return page_props


def parse_search_page(body: str, market: str, page_number: int = 1) -> Page:
    """Lit une page de résultats depuis le corps de sa réponse HTTP."""
    base = market_base(market)
    page_props = _page_props(body)
    announced_total = _int_or_none(page_props.get("numberOfResults"))
    raw_listings = page_props.get("listings")
    if raw_listings is None:
        # Le site dit lui-même qu'il n'y a rien : c'est une recherche vide, et
        # elle est lisible comme telle.
        if announced_total == 0:
            return Page(listings=[], announced_total=0, page_number=max(page_number, 1))
        # Sinon la page n'est pas une page de résultats (une fiche, par
        # exemple) : la lire comme « aucune annonce » serait un zéro silencieux.
        raise SourceError(
            "page sans liste d'annonces : fiche ou page d'erreur lue comme une recherche"
        )
    if not isinstance(raw_listings, list):
        raise StructureChanged(f"listings n'est pas une liste : {type(raw_listings).__name__}")
    listings: list[Listing] = []
    for raw in raw_listings:
        if not isinstance(raw, dict):
            raise StructureChanged(
                f"annonce illisible : {type(raw).__name__} au lieu d'un objet"
            )
        listings.append(_from_json(raw, market, base))
    return Page(
        listings=_dedupe(listings),
        announced_total=announced_total,
        page_number=max(page_number, 1),
    )


def parse_brands(body: str) -> list[dict]:
    """Lit la taxonomie embarquée dans une page de résultats.

    Les marques viennent de `pageProps.taxonomy.makesSorted`, et leurs modèles
    de `taxonomy.models`, indexé par la valeur de la marque. La taxonomie ne
    porte les modèles d'une marque que sur une page filtrée sur cette marque :
    une liste vide d'ici veut donc dire « cette page ne les portait pas », pas
    « cette marque n'a pas de modèles ».
    """
    page_props = _page_props(body)
    taxonomy = page_props.get("taxonomy")
    if not isinstance(taxonomy, dict):
        raise StructureChanged("taxonomie absente de la page : structure changée")
    makes = taxonomy.get("makesSorted")
    if not isinstance(makes, list) or not makes:
        raise StructureChanged("taxonomie sans makesSorted : structure changée")
    models = taxonomy.get("models")
    models = models if isinstance(models, dict) else {}
    brands: list[dict] = []
    for make in makes:
        if not isinstance(make, dict):
            raise StructureChanged(
                f"marque illisible : {type(make).__name__} au lieu d'un objet"
            )
        value = str(make.get("value") or "")
        listed = models.get(value)
        brands.append(
            {
                "value": value,
                "label": make.get("label") or "",
                "models": [
                    {"value": str(model.get("value") or ""), "label": model.get("label") or ""}
                    for model in listed or []
                    if isinstance(model, dict)
                ],
            }
        )
    return brands


class Autoscout24:
    """Source AutoScout24 pour un marché donné.

    Le client HTTP est INJECTÉ et jamais créé ici : les tests passent un
    `httpx.MockTransport`, et aucun test ne touche le réseau.

    `market` ne sert qu'à `brands()`, que le Protocol `Source` appelle sans
    argument ; `search()` prend son marché dans `criteria.markets` et refuse
    d'en recevoir plusieurs (une `Page` est celle d'un marché).

    La cadence est tenue par l'instance : `pause_s` entre deux requêtes, jamais
    avant la première. `clock` et `sleep` sont injectables pour que les tests
    éprouvent cet espacement sans horloge réelle.
    """

    source = SOURCE_NAME

    def __init__(
        self,
        client: httpx.AsyncClient,
        market: str = "fr",
        *,
        pause_s: float = PAGE_PAUSE_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        market_base(market)
        self.market = market
        self._client = client
        self._pause_s = pause_s
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        # Le verrou est tenu PENDANT l'attente, pas seulement autour de la date :
        # c'est ce qui sérialise deux recherches concurrentes et les espace
        # réellement, au lieu de les laisser partir ensemble.
        self._turn = asyncio.Lock()

    async def search(self, criteria: Criteria, page_number: int) -> Page:
        """Lit une page de résultats pour le marché des critères."""
        market = _market_of(criteria)
        url = search_url(criteria, page_number, market)
        body = await self._get(url, market)
        return parse_search_page(body, market, page_number)

    async def brands(self) -> list[dict]:
        """Marques du marché de l'instance, avec les modèles que la page porte."""
        url = f"{market_base(self.market)}/lst?atype=C"
        body = await self._get(url, self.market)
        return parse_brands(body)

    async def _get(self, url: str, market: str) -> str:
        await self._wait_turn()
        try:
            response = await self._client.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": ACCEPT_LANGUAGE[market],
                },
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"lecture de {url} : {exc}") from exc
        # Les redirections ne sont pas suivies : une 3xx arrive ici et échoue.
        # C'est le garde-fou du chemin de référence (une adresse .fr redirigée
        # vers .de serait analysée avec le marché .fr), sans dépendre de la
        # configuration du client injecté.
        if response.status_code != 200:
            raise SourceError(f"lecture de {url} : statut {response.status_code}")
        return response.text

    async def _wait_turn(self) -> None:
        """Espace deux requêtes d'au moins `pause_s`, sans retarder la première."""
        async with self._turn:
            now = self._clock()
            if self._last_request_at is not None:
                wait = self._pause_s - (now - self._last_request_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_request_at = self._clock()
