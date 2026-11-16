"""Contrat de données — figé avant tout code.

Ce module est la seule définition d'une annonce, d'une page de résultats, de
critères de recherche et d'un bloc de couverture. Les adaptateurs de source le
produisent, le corpus le stocke, les outils MCP le rendent.

Une divergence ici serait un champ que personne ne remplit et que personne ne
lit : deux moitiés du serveur d'accord sur un nom et pas sur son sens.

**Ce fichier ne se modifie pas pendant le développement des adaptateurs.** Si le
contrat est faux, on le corrige explicitement, avec le test qui le prouve.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

# --- Vocabulaires ------------------------------------------------------------
# Fermés, et vérifiés à la construction. Un libellé inconnu vaut "" (inconnu),
# jamais une devinette : une boîte « semi-automatique » n'est ni auto ni
# manuelle, et l'inventer fausserait toute comparaison de prix.

# SOURCES est le vocabulaire fermé des sources du plan : ce qu'un appelant a le
# droit de NOMMER.
SOURCES = ("autoscout24", "autohero", "lacentrale")

# SERVED_SOURCES est ce que ce lot sait INTERROGER : une source du vocabulaire
# sans adaptateur est un périmètre, pas une faute de frappe, et les deux refus
# ne disent pas la même chose à l'appelant. Le périmètre s'écrit ici une fois et
# nulle part ailleurs : `tools` le lit pour refuser au parsage des critères, et
# c'est la même liste que la fabrique d'adaptateurs cite.
SERVED_SOURCES = ("autoscout24",)

MARKETS = ("fr", "de")
FUELS = ("essence", "diesel", "electrique", "hybride", "")
GEARS = ("auto", "manuelle", "")
SELLERS = ("pro", "particulier", "")
SORTS = ("price_asc", "price_desc", "age_desc")
# Le tri par défaut ne peut exister qu'ici. Il était recopié dans le parsage des
# critères (`tools._parse_criteria`), et la copie gardait `price_asc` : changer
# le défaut dans le contrat ne changeait donc rien à ce que le site recevait.
# Mesuré : après correction du contrat, une récolte sans `sort` demandait
# toujours le prix croissant et servait les annonces à 1 € en premier.
DEFAULT_SORT = "age_desc"
SOURCE_STATUSES = ("ok", "challenge", "error", "capped")

# Plancher de crédibilité d'un prix affiché, en euros. Mesuré, pas deviné —
# repris de l'outil précédent : campagne du 2026-11-12, 228 annonces sous 800 €,
# prix relevés triés `10, 280, 299, 300, 390, 400, 499, 500, 500, 600, 650, 650,
# 680, 700, 700, 790, 800`. Un seul aberrant, deux ordres de grandeur sous le
# suivant (un Audi Q3 de 2022, 68 983 km, affiché à 10 €). À 100 € on n'écarte
# que lui ; à 500 € — la tentation naturelle — on écarterait six des dix-sept
# prix visibles sans aucune preuve qu'ils soient faux.
#
# Le seuil existe parce qu'AutoScout24 publie des annonces à **1 €** pour dire
# « prix non communiqué » (`priceRaw: 1`, `priceFormatted: "€ 1"`, mesuré sur
# la page de résultats le 2026-11-16). Ce ne sont pas des erreurs de parsing :
# c'est le site qui l'envoie. La conséquence pratique est qu'un tri par prix
# croissant commence par ces annonces-là.
PRICE_FLOOR_EUR = 100


def price_is_plausible(price: int | None) -> bool:
    """Un prix affiché peut-il être celui d'une voiture ?

    Un prix nul, négatif ou à 1 € n'en est pas un. Cette fonction est la règle
    UNIQUE : un appelant qui recopie `price > 0` recrée le défaut qu'elle
    corrige. Le test qui l'accompagne écrit 99 et 100 en dur, et il a raison de
    le faire : citer `PRICE_FLOOR_EUR` rendrait le test tautologique, puisque
    changer le seuil changerait l'attendu avec lui.
    """
    return price is not None and price >= PRICE_FLOOR_EUR


# Nombre de prix crédibles sous lequel une analyse de modèle s'abstient : sous
# ce seuil, une médiane décrit trois annonces et se lit comme un prix de
# marché. Repris d'ICH, et écrit ici UNE fois — `tools` le lit, il ne le
# recopie pas. Un groupe qui n'atteint pas le seuil apparaît quand même, avec
# des quantiles nuls et un avertissement qui cite son compte : cacher le groupe
# ferait disparaître des annonces du corpus, et une estimation inventée serait
# pire que les deux.
MIN_COMPARABLES = 6


# --- Bornes d'entrée ---------------------------------------------------------
# Refusées au-delà, par une erreur qui nomme la borne. Sans elles, un appelant
# qui demande `pages_max: 10**9` fait boucler la récolte jusqu'à ce que le site
# la coupe, et `budget_s: 10**9` l'autorise à durer.

# MAX_PAGES est le plafond réel d'AutoScout24 : mesuré le 2026-11-12, la requête
# `/lst` s'arrête à 20 pages, soit 400 annonces (PLAN.md §2). Demander plus ne
# lit rien de plus : c'est un plafond du site, pas un objectif à dépasser.
MAX_PAGES = 20

# MAX_BUDGET_S borne un appel MCP : au-delà de dix minutes, un client a
# abandonné la requête depuis longtemps et le corpus travaille pour personne.
MAX_BUDGET_S = 600


@dataclass(slots=True)
class Listing:
    """Une annonce. Montants en euros entiers, tels qu'affichés par le site.

    Aucun prix n'est recalculé : `price` est le chiffre affiché, `vat_label` le
    libellé TVA du site recopié tel quel (vide si le site se tait). Aucun frais
    n'entre ici.
    """

    source: str
    market: str
    source_id: str
    url: str
    make: str
    group: str
    group_label: str
    engine: str = ""
    trim: str = ""
    year: int | None = None
    km: int | None = None
    price: int | None = None
    fuel: str = ""
    gear: str = ""
    vat_label: str = ""
    seller: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""
    image: str = ""
    vin: str | None = None

    def key(self) -> tuple[str, str, str]:
        """Identité d'une annonce : `(source, market, source_id)`.

        Le marché est dans la clé : autoscout24.fr et .de sont deux
        marketplaces, et rien ne garantit que leurs identifiants se recouvrent.
        """
        return (self.source, self.market, self.source_id)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True, init=False)
class Page:
    """Une page de résultats telle que le site la rend.

    `announced_total` est le compteur du site pour la requête entière, pas pour
    la page. `None` quand le site ne le donne pas : l'absence est dite, pas
    comblée par `len(listings)`.

    `rendered` est le nombre d'annonces que la page a RENDUES, avant le
    dédoublonnage intra-page : c'est le chiffre que `coverage.read` compte. Il
    n'est PAS comparable au compteur du site — il compte des annonces rendues,
    doublons compris — et la complétude d'une récolte se juge donc sur les
    annonces distinctes (`unique`), pas sur `read`. Un adaptateur qui
    dédoublonne doit le renseigner, sinon `read` compterait les annonces déjà
    dédoublonnées et `read == unique + dropped_duplicates + dropped_invalid`
    deviendrait faux.

    Le champ est un `int` garanti, pas un `int | None` que `__post_init__`
    remplirait : `None` n'existe que dans les arguments de construction, et il y
    veut dire « rien à déclarer » (voir `__init__`). Tout ce qui n'est pas un
    entier est refusé ici, à la construction : un `rendered` textuel passait en
    silence, puis se comparait à `<= 0` dans la récolte, donc la panne surgissait
    loin de sa cause.
    """

    listings: list[Listing]
    announced_total: int | None
    page_number: int
    rendered: int

    def __init__(
        self,
        listings: list[Listing],
        announced_total: int | None = None,
        page_number: int = 1,
        rendered: int | None = None,
    ) -> None:
        """`rendered=None` : la page a rendu ce qu'elle porte, donc une source
        qui ne dédoublonne pas n'a rien à déclarer, et une doublure de test
        reste utilisable telle quelle.

        `rendered` non entier (chaîne, flottant, booléen) lève `TypeError` :
        c'est le seul chiffre auquel `coverage.read` se fie, et un texte se
        comparait à `<= 0` bien plus loin que l'erreur qui l'a produit.

        `rendered` inférieur à ce que la page porte lève `ValueError` : la
        récolte en déduit `dropped_duplicates = rendered - len(listings)`, donc
        un adaptateur qui sous-déclare rendrait ce compteur négatif et casserait
        en silence l'identité `read == unique + dropped_duplicates +
        dropped_invalid`, promue au rang de contrat par deux docstrings.
        """
        self.listings = listings
        self.announced_total = announced_total
        self.page_number = page_number
        if rendered is None:
            self.rendered = len(listings)
        elif isinstance(rendered, bool) or not isinstance(rendered, int):
            raise TypeError(f"rendered doit être un entier : {rendered!r}")
        else:
            if rendered < len(listings):
                raise ValueError(
                    f"rendered={rendered} annonce(s) rendues pour "
                    f"{len(listings)} portée(s) par la page : un adaptateur qui "
                    f"sous-déclare rendrait dropped_duplicates négatif"
                )
            self.rendered = rendered


@dataclass(slots=True)
class Criteria:
    """Critères de recherche. `pages_max` est un plafond par source, et
    `budget_s` gagne toujours sur lui (voir PLAN.md §8)."""

    make: str = ""
    model: str = ""
    year_min: int | None = None
    year_max: int | None = None
    km_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    fuel: str = ""
    gear: str = ""
    markets: tuple[str, ...] = ("fr",)
    sources: tuple[str, ...] = ("autoscout24",)
    pages_max: int = 5
    sort: str = DEFAULT_SORT


@dataclass(slots=True)
class SourceCoverage:
    """Ce qu'une récolte a réellement lu sur une source. Jamais vide.

    Comptabilité, dans cet ordre : `read` compte ce que les PAGES ont rendu
    (avant tout dédoublonnage), `unique` les annonces identifiables qui restent,
    `dropped_duplicates` celles qui répétaient une annonce déjà vue — y compris
    dans la même page — et `dropped_invalid` celles qui n'avaient pas
    d'identifiant, donc ni dédoublonnables ni citables. D'où l'invariant
    `read == unique + dropped_duplicates + dropped_invalid`.

    `truncated` dit qu'on s'est arrêté sur un plafond — `pages_max`, budget,
    plafond du site — et non sur une fin de marché. Il ne promet pas que le site
    annonçait davantage : quand `announced_total` est `None`, le site ne dit pas
    combien il en annonce, et un plafond atteint laisse alors ignorer s'il
    restait des annonces. `truncated=True` reste la bonne réponse dans ce cas —
    on ne peut pas présenter la source comme couverte — et le `reason` l'écrit
    en toutes lettres : « plafond atteint, total annoncé inconnu ». Un `status`
    autre que "ok" interdit de présenter la source comme couverte.
    """

    source: str
    market: str
    status: str = "ok"
    announced_total: int | None = None
    read: int = 0
    unique: int = 0
    dropped_duplicates: int = 0
    dropped_invalid: int = 0
    truncated: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CorpusCoverage:
    """Ce que le corpus contient, jamais la somme des récoltes.

    Deux récoltes qui se recouvrent, additionnées, annoncent une couverture
    double. On décrit donc des annonces distinctes, et les récoltes restent
    listées à part avec leurs propres chiffres.

    Chaque entrée de `sources` porte huit clés, les mêmes dans les deux
    chemins : `listings_in_corpus` compte les annonces de la paire
    `(source, market)` dans le corpus entier, `read` et `unique` celles de la
    récolte, `announced_total` ce que le site annonçait, avec `status` et
    `truncated` — un consommateur d'`offers` seul doit pouvoir dire « j'ai lu 400
    annonces sur 12 479 annoncées », pas seulement « le corpus en contient
    400 ».

    Une seule chose par clé : `store.corpus(job_id)` et `store.whole_corpus()`
    mesurent la même paire de la même façon, et aucune entrée ne porte de
    `job_id` — l'appelant qui a lancé une récolte l'a déjà, et deux formes pour
    un même type seraient un contrat à deux vitesses. `runs` et
    `listings_distinct` décrivent l'objet de la couverture — la récolte pour
    `store.corpus(job_id)` (c'est elle qu'`offers` pagine), le corpus entier
    pour `store.whole_corpus()` — jamais la somme des `read`.
    """

    listings_distinct: int = 0
    runs: int = 0
    sources: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Source(Protocol):
    """Interface que chaque adaptateur de source doit satisfaire.

    L'adaptateur reçoit son client HTTP à la construction, pour que les tests
    injectent un transport factice : **aucun test ne touche le réseau**.

    `search` lève :
      - `StructureChanged` quand la page ne contient plus la charge attendue
        (`__NEXT_DATA__`) : échouer bruyamment, jamais rendre 0 annonce ;
      - `SourceError` sur statut non-200, défi anti-bot, ou réponse illisible.
    """

    source: str

    async def search(self, criteria: Criteria, page_number: int) -> Page: ...

    async def brands(self) -> list[dict]: ...


class StructureChanged(Exception):
    """La page répond, mais sa structure n'est plus celle qu'on sait lire."""


class SourceError(Exception):
    """La source n'a pas répondu ce qu'il fallait : statut, défi, réseau.

    `status` est le statut de `coverage` que cette erreur mérite : "error" par
    défaut, "challenge" quand la page est un défi anti-bot. Le porter ici plutôt
    que de le deviner plus haut est ce qui rend `status="challenge"`
    atteignable : un vocabulaire que rien ne peut produire est un mensonge.
    """

    def __init__(self, message: str, *, status: str = "error") -> None:
        super().__init__(message)
        self.status = status
