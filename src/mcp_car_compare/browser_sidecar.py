"""Side-car navigateur : Camoufox lit les pages de résultats de LaCentrale.

**Pourquoi un processus séparé.** stdout est le canal JSON-RPC du serveur MCP
(`server.py`), et la bibliothèque Camoufox écrit sa barre de progression sur
stdout (PLAN.md §2). L'important dans un import direct serait donc une session
corrompue chez le client ; un processus voisin ne peut pas la corrompre, et
`entrypoint.sh` envoie en plus son stdout sur stderr par ceinture.

**Une seule route.** `POST /scrape` est la seule chose que le produit demande au
navigateur (PLAN.md §3) ; `GET /health` existe pour que l'entrypoint puisse
attendre que le port réponde avant de lancer le serveur MCP.

La réponse est un contrat figé, écrit une fois ici et nulle part ailleurs :

    {"vehicles": [{source_id, url, make, group, group_label, engine, trim,
                   year, km, price, fuel, gear, seller, city, postal_code,
                   country, image}],
     "announced_total": <int|null>, "pages_read": <int>, "partial": <bool>}

C'est la forme `Listing` du contrat privée de `source` et `market` : l'adaptateur
(`sources/lacentrale.py`) ajoute ces deux-là, il n'a pas à deviner un libellé de
carte. `announced_total` vaut `None` quand le site se tait — l'absence est dite,
jamais comblée par `len(vehicles)`.

**Ce module n'est pas le contrat de données.** Il rend ce que la carte affiche,
et rien de plus. `seller`, `city` et `postal_code` restent vides, et c'est une
mesure, pas une reprise du plan : le pied de carte porte bien un NOM de vendeur
et un emplacement (« AUTOMOBILES ROBESPIERRE » + « POISSY », ou « ADVANCE
ORGEVAL » + « 78 »), mais `seller` est une énumération du contrat
(`pro`/`particulier`), `city` reçoit tantôt une ville tantôt un numéro de
département dans le même emplacement, et aucun code postal n'y figure (sonde du
2026-11-16). Un champ vide est une absence déclarée ; le remplir d'une
supposition mettrait une adresse inventée dans une comparaison de prix.

**Ce que la mesure a appris et que le code respecte :**

- un Camoufox par DÉFAUT (headless="virtual", humanize, locale fr-FR, geoip)
  prend un 403 sur le document, la page se recharge d'elle-même, et 24 cartes
  sont rendues en ~6 s sans captcha (`docs/recon/lacentrale-firefox-vs-camoufox.md`
  du dépôt ICH). Un 403 de navigation n'est donc PAS une erreur ici : seule
  l'absence de carte conclut quelque chose ;
- une page NEUVE par navigation, dans la même session. Réutiliser la même page
  fait mourir le processus de contenu à la quatrième page (« Target crashed »),
  mesuré dans ICH ;
- 60 s au moins entre deux LANCEMENTS de navigateur (`LAUNCH_INTERVAL_S`) : le
  site le supporte mal autrement, et deux navigateurs simultanés sur la même IP
  ne vont pas plus vite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .sources.autoscout24 import normalize_group
from .sources.labels import fuel_from, gear_from

LOGGER = logging.getLogger(__name__)

# --- Écoute ------------------------------------------------------------------
# 127.0.0.1 et rien d'autre : le side-car conduit un navigateur furtif pour
# n'importe quel appelant, donc il ne doit pas être joignable d'un autre
# conteneur (PLAN.md §3, règle 3).
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8899

# --- Cadence et délais -------------------------------------------------------
# Le lancement d'un Camoufox coûte ~30 s et se paie UNE fois par requête : une
# session, plusieurs pages.
LAUNCH_INTERVAL_S = 60.0

# Délai d'attente des cartes de la page 1. Il couvre la résolution de
# l'interstitiel DataDome, qui se résout seul en 6 s sur une IP fraîche et
# couramment 21 à 38 s sur une IP « chaude » (mesuré dans ICH, PLAN.md §2 :
# « résolution DataDome 6–75 s »). Détecter le défi AVANT cette attente donne un
# faux positif systématique ; on attend donc les cartes d'abord.
FIRST_PAGE_TIMEOUT_S = 75.0

# Délai des pages suivantes : la session est déjà ouverte et le défi déjà
# résolu, une page qui ne rend rien en 15 s ne rendra rien.
NEXT_PAGE_TIMEOUT_S = 15.0

# Délai interne d'une requête entière, lancement du navigateur compris. Il est
# testé ENTRE deux pages et jamais pendant une attente de cartes : une page qui
# arrive à la 74ᵉ seconde de son délai a encore le droit d'être lue.
SCRAPE_DEADLINE_S = 120.0

# Garde EXTERNE d'une requête, strictement au-dessus du budget interne plus le
# lancement du navigateur (~30 s). Le budget interne s'arrête entre deux pages ;
# ce plafond-ci couvre ce qu'il ne voit pas — un navigateur qui ne rend jamais
# la main (lancement bloqué, fermeture qui pend). Sans lui, une requête pendue
# tient le verrou et le side-car ne sert plus personne : la source meurt en
# silence, ce que PLAN.md §7 range parmi les risques à traiter.
REQUEST_TIMEOUT_S = 180.0

# Plafond de pages par requête. 20 pages ≈ 480 annonces à 24 par page : au-delà,
# c'est un balayage de marché, pas une récolte bornée.
MAX_PAGES_CAP = 20
PAGINATION_PARAM = "page"

# Page de départ quand l'appelant ne s'en prononce pas : le début.
FIRST_PAGE = 1

# --- Extraction DOM (sélecteurs éprouvés, repris d'ICH) ----------------------
# La carte EST l'ancre de l'annonce : `a[href*='auto-occasion-annonce']` englobe
# le titre, le sous-titre, le prix, les puces et l'image.
CARD_SELECTOR = "a[href*='auto-occasion-annonce']"
TITLE_SELECTOR = "[class*='vehiclecardV2_title']"
SUBTITLE_SELECTOR = "[class*='vehiclecardV2_subTitle']"
PRICE_SELECTOR = "[class*='vehiclePrice']"
CHARACTERISTICS_SELECTOR = "[class*='vehicleCharacteristicsItem']"

BASE_URL = "https://www.lacentrale.fr"
ALLOWED_HOST = "lacentrale.fr"

# LaCentrale est un marché national : le pays n'est pas lu sur la carte, il est
# une propriété du site (comme le `market: "fr"` que l'adaptateur pose). Il n'est
# donc pas une devinette — mais il est le SEUL champ de localisation que le site
# permet, d'où les trois autres laissés vides.
SITE_COUNTRY = "FR"

# --- Défi anti-robot (DataDome et assimilés) ---------------------------------
# Cherché seulement quand aucune carte n'a été rendue, jamais sur une page qui
# porte ses annonces : une annonce peut citer « datadome » sans que la page soit
# un défi (même règle qu'AutoScout24, PLAN.md §8).
CHALLENGE_SELECTORS = (
    "iframe[src*='captcha-delivery.com']",
    "div[id*='datadome']",
    "div[id*='captcha__human']",
    "form[id*='captcha']",
    "div.g-recaptcha",
    "div.h-captcha",
)
CHALLENGE_TITLE_MARKERS = ("captcha", "datadome", "just a moment", "verification")
CHALLENGE_TEXT_MARKERS = (
    "captcha-delivery",
    "datadome",
    "pardon for the interruption",
    "nous avons remarqué",
    "vérification en cours",
    "verification in progress",
    "un instant",
)

# Compteur d'annonces de la page de résultats : « Annonces 1 à 23 sur 6 913 »
# (insécables tolérées — `clean` les ramène à des espaces).
_ANNOUNCED_TOTAL = re.compile(
    r"annonces?\s+[\d\s.]+\s*(?:à|a|-|–)\s*[\d\s.]+\s+sur\s+"
    r"(?P<total>\d+(?:[\s.,]\d{3})*)",
    re.IGNORECASE,
)

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_KM = re.compile(r"([\d][\d\s.]*)\s*km\b", re.IGNORECASE)


class Challenge(Exception):
    """La page affichée est un défi anti-robot : la route rend 409."""


class ScrapeFailed(Exception):
    """La page n'a pas rendu ce qu'on sait lire : la route rend 502."""


class BadRequest(Exception):
    """La requête n'a pas la forme du contrat : la route rend 400."""


# --- Requête -----------------------------------------------------------------

def clean(text: str | None) -> str:
    """Un texte du DOM ramené à une ligne : insécables et espaces réduits."""
    if not text:
        return ""
    return " ".join(text.replace("\xa0", " ").split())


def check_url(raw: object) -> str:
    """Valide l'URL demandée : une chaîne http(s) sur lacentrale.fr, ou un refus.

    Le side-car existe pour ce site et pour lui seul : accepter une URL
    arbitraire en ferait un navigateur furtif au service de l'appelant, capable
    d'aller chercher 169.254.169.254 ou un service du réseau interne.

    L'hôte est comparé APRÈS avoir refusé les caractères que le navigateur
    réinterprète. `urlsplit` ne découpe pas l'URL au même endroit que le WHATWG
    URL Standard : un antislash n'est pas un séparateur pour lui, mais il l'est
    pour tous les navigateurs, donc

        urlsplit("http://169.254.169.254\\\\www.lacentrale.fr/").hostname
            -> "169.254.169.254\\\\www.lacentrale.fr"   (accepté à tort)
        new URL("http://169.254.169.254\\\\www.lacentrale.fr/").host
            -> "169.254.169.254"                        (l'interne)

    d'où le refus de `\\`, des blancs et des caractères de contrôle avant
    l'analyse.
    """
    if not isinstance(raw, str):
        raise BadRequest("champ « url » : une chaîne est attendue")
    url = clean(raw)
    if not url:
        raise BadRequest("champ « url » manquant")
    if any(char in url for char in ("\\", "\n", "\r", "\t", "\x00", " ")):
        raise BadRequest("champ « url » : caractère interdit dans l'URL")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise BadRequest("champ « url » : URL malformée") from None
    if parsed.scheme.lower() not in ("http", "https"):
        raise BadRequest("champ « url » : une URL http(s) est attendue")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise BadRequest("champ « url » : identifiants dans l'URL refusés")
    host = (parsed.hostname or "").lower().rstrip(".")
    # Un nom d'hôte ne contient que des lettres, des chiffres, des points et des
    # tirets : tout le reste est le signe d'une URL déguisée.
    if not host or any(not (char.isalnum() or char in "-.") for char in host):
        raise BadRequest("champ « url » : nom d'hôte malformé")
    if host != ALLOWED_HOST and not host.endswith("." + ALLOWED_HOST):
        raise BadRequest(f"champ « url » : seul {ALLOWED_HOST} est autorisé")
    return url


def allowed_pages(raw: object) -> int:
    """Nombre de pages à lire, borné par le plafond du side-car.

    Absent vaut une page : le client qui ne se prononce pas ne fait pas balayer
    le marché. Un zéro ou un négatif vaut aussi une page — « aucune page » n'est
    pas une lecture, c'est une requête qui a l'air de tourner.
    """
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadRequest("champ « max_pages » : un entier est attendu")
    return max(1, min(raw, MAX_PAGES_CAP))


def allowed_start_page(raw: object) -> int:
    """Page où COMMENCER la lecture, `1` par défaut.

    C'est ce qui rend une reprise de récolte possible sans relire depuis le
    début : mesuré, un `harvest` LaCentrale profond dépasse le délai du client
    MCP (~8 s/page dans le navigateur), et sans page de départ la seule reprise
    possible était de tout relire — donc de repayer les pages déjà payées et de
    dépasser le délai de nouveau.

    Bornes : `1` au moins (une page 0 n'existe pas), `MAX_PAGES_CAP` au plus
    (au-delà, le site ne rend rien). Comme `allowed_pages`, une valeur hors
    bornes est ramenée dans les bornes plutôt que refusée : c'est un plafond,
    pas une faute de frappe.
    """
    if raw is None:
        return FIRST_PAGE
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadRequest("champ « start_page » : un entier est attendu")
    return max(FIRST_PAGE, min(raw, MAX_PAGES_CAP))


def page_url(url: str, page_number: int) -> str:
    """Adresse de la page N : celle de la recherche, plus `page=N`.

    La page 1 garde l'URL telle quelle (celle que l'adaptateur a construite, tri
    compris) ; les suivantes reçoivent le paramètre, vérifié sur le site dans
    ICH (10 pages lues, 237 annonces uniques le 2026-10-31).
    """
    if page_number <= 1:
        return url
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != PAGINATION_PARAM]
    query.append((PAGINATION_PARAM, str(page_number)))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def launch_wait(now: float, last_launch: float | None) -> float:
    """Secondes à attendre avant de relancer un navigateur (0 au premier)."""
    if last_launch is None:
        return 0.0
    return max(0.0, LAUNCH_INTERVAL_S - (now - last_launch))


# --- Lecture des champs ------------------------------------------------------

def source_id(url: str) -> str:
    """Identifiant de l'annonce, lu dans son adresse.

    Les cartes pointent vers des adresses du type
    `…/auto-occasion-annonce-<identifiant>.html` : la partie qui suit ce marqueur
    est l'identifiant que le site emploie lui-même. Une adresse sans ce marqueur
    rend son chemin entier plutôt que rien — l'annonce reste identifiable d'une
    page à l'autre, là où un identifiant vide la rendrait non dédoublonnable et
    incitable (voir `StoreCoverage.dropped_invalid`).
    """
    path = urlsplit(url).path
    before, marker, tail = path.rpartition("auto-occasion-annonce-")
    if not marker:
        return path.lstrip("/")
    identifier = tail[: -len(".html")] if tail.endswith(".html") else tail
    return identifier or before.lstrip("/") or path.lstrip("/")


def read_announced_total(text: str) -> int | None:
    """Nombre d'annonces annoncé par le site, ou `None` s'il est illisible.

    « Annonces 1 à 23 sur 13 481 » désigne la recherche entière, pas la page :
    c'est le seul chiffre qui permette de dire une lecture tronquée. Absent, il
    reste `None` — le combler par le nombre de cartes lues transformerait
    chaque page en marché complet.
    """
    match = _ANNOUNCED_TOTAL.search(clean(text))
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group("total"))
    return int(digits) if digits else None


def read_price(text: str) -> int | None:
    """Prix en euros entiers, tel qu'affiché ; `None` s'il n'y a pas de chiffre."""
    digits = re.sub(r"\D", "", clean(text))
    return int(digits) if digits else None


# Les mots qui introduisent une gamme en deux mots sur le site français :
# « CLASSE A », « SERIE 3 ». Comparés en minuscules et sans accent, sans
# traduction : c'est la forme que la page affiche.
_RANGE_WORDS = ("classe", "clase", "série", "serie", "class", "klasse")


def split_title(title: str, subtitle: str) -> tuple[str, str, str]:
    """`(make, group_label, trim)` lus dans le titre et le sous-titre.

    LaCentrale n'a pas de taxonomie par annonce : sa carte titre librement.
    Mesuré le 2026-11-16 sur une page de résultats réelle —
    `title="RENAULT CLIO IV phase 2"`, `subtitle="1.5 DCI 90 BUSINESS"` ;
    `title="RENAULT CLIO VI"`, `subtitle="1.8 E-TECH 156 EVOLUTION +"` — le
    titre porte MARQUE + GAMME + GÉNÉRATION, le sous-titre la version
    commerciale. La lecture est donc POSITIONNELLE :

    - `make` : le premier mot du titre ;
    - `group_label` : le mot suivant — un second quand il introduit une gamme en
      deux mots (« CLASSE A », « SÉRIE 3 »), la même forme qu'AutoScout24 ;
    - `trim` : le sous-titre entier, tel que le site l'affiche.

    La GÉNÉRATION qui suit la gamme (« IV phase 2 », « VI ») est écartée, et
    c'est une décision : aucun champ du contrat ne la porte, la mettre dans
    `engine` ferait passer une génération pour une motorisation (c'était le
    premier défaut de ce module), et l'ajouter à la gamme casserait la clé de
    rapprochement avec AutoScout24, qui publie « clio » et pas « clio iv phase 2 ».

    Deux limites, écrites plutôt que masquées :

    - une marque en deux mots (« ALFA ROMEO », « LAND ROVER ») donne
      `make="ALFA"` et `group_label="ROMEO"`. Rien dans la page ne permet de
      trancher sans un catalogue de marques, et un catalogue recopié ici
      divergerait de celui de l'adaptateur ;
    - la finition n'est pas séparée de la motorisation : le sous-titre les mêle
      (« 1.5 DCI 90 BUSINESS »), et les découper demanderait le vocabulaire des
      motorisations de chaque constructeur.

    Un titre vide rend trois chaînes vides : c'est une carte que le site n'a
    pas titrée, pas une marque inventée.
    """
    words = clean(title).split()
    if not words:
        return "", "", clean(subtitle)
    make = words[0]
    rest = words[1:]
    group_words = rest[:1]
    if rest[:2] and rest[0].lower() in _RANGE_WORDS:
        group_words = rest[:2]
    return make, " ".join(group_words), clean(subtitle)


def read_specs(bullets: list[str]) -> tuple[int | None, int | None, str, str]:
    """`(year, km, fuel, gear)` lus dans les puces de la carte.

    Les puces sont les seules à porter ces quatre champs. Mesuré le 2026-11-16
    sur une carte réelle, dans cet ordre : « 2026 », « Auto », « 10 857 km »,
    « Hybrides » — les libellés que `labels.py` traduit déjà (il documente « Auto »
    sur 22 cartes sur 24 et « Hybrides » sur 8). Le PREMIER libellé reconnu
    gagne ; un libellé inconnu laisse le champ vide, il ne l'invente pas.
    """
    year: int | None = None
    km: int | None = None
    fuel = ""
    gear = ""
    for bullet in bullets:
        text = clean(bullet)
        if not text:
            continue
        if year is None:
            match = _YEAR.search(text)
            if match is not None:
                year = int(match.group(0))
        if km is None:
            match = _KM.search(text)
            if match is not None:
                digits = re.sub(r"\D", "", match.group(1))
                if digits:
                    km = int(digits)
        if not fuel:
            fuel = fuel_from(text)
        if not gear:
            gear = gear_from(text)
    return year, km, fuel, gear


# --- Lecture du DOM ----------------------------------------------------------

async def _text(container: Any, selector: str) -> str:
    """Texte du premier élément qui correspond, ou ""."""
    element = await container.query_selector(selector)
    if element is None:
        return ""
    return clean(await element.inner_text())


async def _texts(container: Any, selector: str) -> list[str]:
    """Textes de tous les éléments qui correspondent."""
    return [clean(await element.inner_text())
            for element in await container.query_selector_all(selector)]


async def _image(card: Any) -> str:
    """Adresse de la vignette de la carte, ou "" s'il n'y en a pas.

    `data-src` est là pour les images différées : une carte en bas de page n'a
    pas encore de `src`, et la ignorer perdrait la vignette des dernières
    annonces de chaque page.
    """
    image = await card.query_selector("img")
    if image is None:
        return ""
    for attribute in ("src", "data-src", "data-lazy-src"):
        value = clean(await image.get_attribute(attribute))
        if value:
            return urljoin(BASE_URL + "/", value)
    return ""


async def read_card(card: Any) -> dict | None:
    """Une carte de résultat → les champs du contrat, `None` si elle n'a pas d'URL.

    Une carte sans adresse n'est pas citable ni dédoublonnable : elle est
    écartée ici plutôt que rendue avec un `source_id` vide.
    """
    href = clean(await card.get_attribute("href"))
    if not href:
        return None
    url = urljoin(BASE_URL + "/", href)
    make, group_label, trim = split_title(
        await _text(card, TITLE_SELECTOR), await _text(card, SUBTITLE_SELECTOR)
    )
    year, km, fuel, gear = read_specs(await _texts(card, CHARACTERISTICS_SELECTOR))
    return {
        "source_id": source_id(url),
        "url": url,
        "make": make,
        "group": normalize_group(group_label),
        "group_label": group_label,
        # Vide parce que le site ne sépare pas la motorisation de la finition :
        # les deux sont dans le sous-titre, et c'est `trim` qui le porte en
        # entier. Vide vaut « pas mesuré », pas « pas de motorisation ».
        "engine": "",
        "trim": trim,
        "year": year,
        "km": km,
        "price": read_price(await _text(card, PRICE_SELECTOR)),
        "fuel": fuel,
        "gear": gear,
        # Vides parce que la carte ne les publie pas (PLAN.md §4 : 0/231).
        "seller": "",
        "city": "",
        "postal_code": "",
        "country": SITE_COUNTRY,
        "image": await _image(card),
    }


async def body_text(page: Any) -> str:
    """Texte du corps de la page, "" s'il n'y en a pas.

    Lu par un sélecteur plutôt que par `page.evaluate` : c'est la même chose,
    sans injecter de JavaScript dans un navigateur dont toute la valeur est de
    ne pas ressembler à un robot.
    """
    return await _text(page, "body")


async def is_challenge(page: Any, text: str) -> bool:
    """La page affichée est-elle un défi anti-robot ?

    Appelée UNIQUEMENT quand aucune carte n'a été rendue : sur une page qui
    porte ses annonces, la chaîne « datadome » peut venir d'une description
    légitime, et un statut `challenge` qui ment rendrait inutilisable celui qui
    ne ment pas.
    """
    for selector in CHALLENGE_SELECTORS:
        if await page.query_selector(selector) is not None:
            return True
    title = clean(await page.title()).lower()
    if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
        return True
    lowered = clean(text).lower()
    return any(marker in lowered for marker in CHALLENGE_TEXT_MARKERS)


# --- Session navigateur ------------------------------------------------------

@asynccontextmanager
async def open_browser() -> AsyncIterator[Any]:
    """Une session Camoufox pour toute la requête (~30 s de lancement, payés une fois).

    Les options sont celles du bras de référence mesuré dans ICH : headless
    « virtual » (Xvfb), humanize, locale fr-FR, GeoIP, et surtout **aucun
    `block_images`** — le rendu complet compte dans la furtivité DataDome.
    """
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(
        headless="virtual",
        humanize=True,
        os="linux",
        locale="fr-FR",
        geoip=True,
    ) as browser:
        yield browser


_browser_turn = asyncio.Lock()
_last_launch: float | None = None
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@asynccontextmanager
async def _launch_turn() -> AsyncIterator[None]:
    """Sérialise les lancements et les espace d'au moins `LAUNCH_INTERVAL_S`.

    Le verrou est tenu pendant TOUTE la requête, pas seulement autour du
    lancement : c'est ce qui garantit un seul navigateur vivant à la fois
    (PLAN.md §3, règle 2). L'attente est prise sous le verrou, donc un second
    appelant attend son tour au lieu de partir en même temps.
    """
    global _last_launch
    async with _browser_turn:
        wait = launch_wait(_clock(), _last_launch)
        if wait > 0:
            LOGGER.info("attente de %.0f s avant le prochain lancement", wait)
            await _sleep(wait)
        _last_launch = _clock()
        yield


async def _wait_cards(page: Any, timeout_s: float) -> None:
    """Attend les premières cartes, sans conclure si elles ne viennent pas.

    Le dépassement de délai n'est pas une erreur ici : la page a peut-être rendu
    un défi, et c'est l'appelant qui décide ce qu'une page sans carte signifie.
    Laisser filer l'exception transformerait un défi en 500 illisible.
    """
    with contextlib.suppress(Exception):
        await page.wait_for_selector(CARD_SELECTOR, timeout=timeout_s * 1000)


async def read_page(page: Any, number: int) -> list[dict]:
    """Lit les cartes de la page affichée, dédoublonnées dans la page.

    Une annonce peut apparaître deux fois dans une même page ; c'est le `seen`
    de `_paginate` qui absorbe, lui, le recouvrement entre deux pages.
    """
    vehicles: list[dict] = []
    seen: set[str] = set()
    for card in await page.query_selector_all(CARD_SELECTOR):
        vehicle = await read_card(card)
        if vehicle is None or vehicle["url"] in seen:
            continue
        seen.add(vehicle["url"])
        vehicles.append(vehicle)
    LOGGER.info("page %d : %d carte(s) lue(s)", number, len(vehicles))
    return vehicles


async def scrape_pages(
    url: str,
    max_pages: int,
    *,
    start_page: int = FIRST_PAGE,
    browser_factory: Callable[[], Any] = open_browser,
) -> dict:
    """Lit `max_pages` pages de résultats à partir de `start_page`, dans UNE
    session de navigateur.

    `start_page` est la page du SITE où commencer : une reprise de récolte
    reprend à la page suivante sans relire (ni repayer) celles déjà lues, et
    c'est le seul moyen pour LaCentrale de rentrer dans le délai d'un client
    MCP (~8 s/page dans le navigateur).

    Une page NEUVE par navigation : le lancement et la résolution du défi sont
    payés une fois, mais chaque document est libéré avec sa page (réutiliser la
    même page tue le processus de contenu à la 4ᵉ page, mesuré dans ICH).

    Une première page LUE sans carte ne rend JAMAIS un résultat vide quand c'est
    aussi la page 1 du site : c'est un défi (409) ou une page qu'on ne sait plus
    lire (502). Un `{"vehicles": []}` en 200 se lirait comme un marché désert,
    et c'est exactement ce que ce projet refuse. À partir d'une page de reprise,
    une page vide veut dire « il n'y a plus rien à lire » — c'est le cas normal
    d'une reprise qui repart au-delà du stock, et le refuser transformerait une
    fin de stock en panne.
    """
    async with _launch_turn():
        async with browser_factory() as browser:
            return await _paginate(browser, url, max_pages, start_page)


async def _paginate(
    browser: Any, url: str, max_pages: int, start_page: int = FIRST_PAGE
) -> dict:
    """Boucle sur les pages et accumule les annonces. Voir `scrape_pages`.

    `first_read` est la première page LUE dans cette session : c'est elle qui
    paie la résolution du défi et porte les conclusions de la page 1 — le
    délai long, et le refus d'un résultat vide. La comparer à `1` en dur
    rendrait une reprise à la page 5 sans attente de cartes et sans refus, donc
    silencieusement vide.
    """
    started = _clock()
    vehicles: list[dict] = []
    seen: set[str] = set()
    announced_total: int | None = None
    pages_read = 0
    partial = False
    first_read = max(FIRST_PAGE, start_page)
    # Le plafond du side-car borne la lecture ABSOLUE, pas le nombre de pages de
    # la requête : sans lui, `start_page=19&max_pages=20` demanderait 39 pages
    # à un site qui n'en rend que 20.
    last_page = min(first_read + max_pages - 1, MAX_PAGES_CAP)

    for number in range(first_read, last_page + 1):
        if _clock() - started > SCRAPE_DEADLINE_S:
            LOGGER.warning("délai de %.0f s atteint avant la page %d",
                           SCRAPE_DEADLINE_S, number)
            partial = True
            break
        page = await browser.new_page()
        try:
            # wait_until="domcontentloaded" et non "load" : le document prend un
            # 403 que la page recharge d'elle-même, et attendre "load" ferait
            # payer le chargement complet des vignettes avant chaque lecture.
            await page.goto(page_url(url, number), wait_until="domcontentloaded")
            await _wait_cards(
                page, FIRST_PAGE_TIMEOUT_S if number == first_read else NEXT_PAGE_TIMEOUT_S
            )
            text = await body_text(page)
            if announced_total is None:
                announced_total = read_announced_total(text)
            page_vehicles = await read_page(page, number)
            if not page_vehicles:
                if await is_challenge(page, text):
                    if number == first_read:
                        raise Challenge(f"défi anti-robot sur la page {number}")
                    partial = True
                    LOGGER.warning("défi anti-robot à la page %d : lecture partielle", number)
                    break
                # La page 1 du SITE, à condition que ce soit aussi la première
                # page lue : une page 3 vide d'une lecture commencée à la page 1
                # est une fin de stock, pas une structure cassée.
                if number == first_read == FIRST_PAGE:
                    raise ScrapeFailed(
                        "la page 1 n'a rendu aucune carte et n'est pas un défi : "
                        "structure changée ou adresse sans résultat"
                    )
                # Une page sans carte n'est la fin du stock que si le site
                # n'annonçait pas davantage. Sinon (page trop lente, paramètre
                # de pagination ignoré) la lecture est courte, et le dire évite
                # de faire passer un marché tronqué pour un marché complet.
                #
                # À partir d'une page de reprise, une page vide est un cas
                # NORMAL : le stock peut s'arrêter avant la page demandée.
                if announced_total is None or len(vehicles) < announced_total:
                    partial = True
                LOGGER.warning("page %d sans carte : lecture partielle", number)
                break
            # Recouvrement entre deux pages : la carte mise en avant revient en
            # tête de chaque page, et une annonce peut chevaucher deux pages.
            # La PREMIÈRE lecture gagne, donc l'ordre du site est préservé.
            fresh = [vehicle for vehicle in page_vehicles if vehicle["url"] not in seen]
            seen.update(vehicle["url"] for vehicle in fresh)
            vehicles.extend(fresh)
            pages_read = number
        finally:
            # Une page qui ne se ferme pas ne doit pas emporter la requête : les
            # annonces déjà lues sont acquises.
            with contextlib.suppress(Exception):
                await page.close()
    else:
        # La boucle s'est épuisée sur le plafond de pages : le plafond a coupé,
        # ce n'est pas une fin de marché. Un total annoncé inconnu ne permet pas
        # d'affirmer que tout a été lu, donc il compte comme une lecture
        # partielle (PLAN.md §8 : « plafond atteint, total annoncé inconnu »).
        if announced_total is None or len(vehicles) < announced_total:
            partial = True

    return {
        "vehicles": vehicles,
        "announced_total": announced_total,
        "pages_read": pages_read,
        "partial": partial,
    }


# --- Route -------------------------------------------------------------------

def create_app() -> Any:
    """L'application FastAPI : deux routes, dont une seule porte du métier.

    Les imports de FastAPI sont locaux. Ce module doit rester importable sans
    `requirements-browser.txt` — les tests unitaires lisent les fonctions de
    lecture et la forme de la réponse, et l'image de test (`Dockerfile.test`)
    n'installe pas FastAPI. Un import en tête ferait échouer la suite entière
    pour une dépendance qu'aucun de ces tests n'utilise.
    """
    from fastapi import Body, FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="mcp-car-compare sidecar",
        # Aucune documentation exposée : ce service n'est pas une API publique,
        # il est piloté par un seul appelant dans le même conteneur.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, bool]:
        """Sonde de démarrage : l'entrypoint attend que ce port réponde."""
        return {"ok": True}

    @app.post("/scrape")
    async def scrape(payload: dict = Body(...)):
        """Lit les pages demandées et rend les champs du contrat.

        Statuts : 200 avec les champs du contrat, 400 sur une requête hors
        contrat (URL d'un autre hôte comprise), 409 sur un défi anti-robot,
        502 sur une page illisible ou une panne du navigateur. Un 200 avec zéro
        véhicule n'existe pas POUR UNE LECTURE QUI COMMENCE À LA PAGE 1 : c'est
        la règle qui empêche un marché désert de ressembler à un site qu'on ne
        sait plus lire. À partir d'une page de reprise (`start_page` > 1), zéro
        véhicule est la réponse honnête d'un stock épuisé avant cette page — et
        la refuser transformerait une fin de stock en panne, ce qui rendrait
        toute reprise au-delà du stock impossible. Un corps qui n'est pas un
        objet JSON reçoit le 422 de FastAPI, avant que ce code le voie.

        `payload: dict` plutôt que `request: Request` : une annotation de type
        `Request` ne se résout pas quand l'import de FastAPI est local (le nom
        n'existe pas dans les globales du module), et FastAPI la prend alors
        pour un paramètre de requête — la route répondait 422 à TOUS les
        appels. Le corps est déjà un objet validé ici.
        """
        try:
            url = check_url(payload.get("url"))
            max_pages = allowed_pages(payload.get("max_pages"))
            start_page = allowed_start_page(payload.get("start_page"))
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                result = await scrape_pages(url, max_pages, start_page=start_page)
        except BadRequest as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Challenge as exc:
            LOGGER.warning("défi : %s", exc)
            return JSONResponse(status_code=409, content={"error": "challenge"})
        except TimeoutError:
            # Les pages déjà lues sont perdues : c'est le prix d'un plafond qui
            # ne peut pas lire l'accumulateur de `_paginate`. Il n'est atteint
            # que si le navigateur ne rend pas la main — le budget interne de
            # 120 s arrête la pagination bien avant, en rendant ce qu'elle a lu.
            return JSONResponse(
                status_code=502,
                content={"error": f"délai de {REQUEST_TIMEOUT_S:g} s dépassé : "
                                   "le navigateur n'a pas rendu la main"},
            )
        except Exception as exc:
            LOGGER.exception("lecture impossible")
            return JSONResponse(
                status_code=502,
                content={"error": "lecture impossible : " + f"{type(exc).__name__}: {exc}"},
            )
        if not result["vehicles"] and start_page == FIRST_PAGE:
            # Filet de sécurité : aucune branche ci-dessus ne produit ce cas, et
            # s'il apparaissait, un 200 vide serait le mensonge que ce side-car
            # existe pour empêcher. Il ne vaut que pour une lecture qui commence
            # au début : une reprise au-delà du stock n'a légitimement rien à
            # rendre.
            return JSONResponse(
                status_code=502,
                content={"error": "aucune annonce lue : résultat vide refusé"},
            )
        return JSONResponse(status_code=200, content=result)

    return app


def main() -> None:
    """Lance le side-car sur 127.0.0.1:8899, logs sur stderr.

    `uvicorn.run` configure ses logs sur stdout : l'entrypoint redirige déjà le
    stdout du side-car vers stderr, et cette redirection est la ceinture qui
    compte, parce que Camoufox écrit sa barre de progression sur stdout sans
    qu'on puisse l'en empêcher (PLAN.md §2).
    """
    import uvicorn

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    uvicorn.run(create_app(), host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
