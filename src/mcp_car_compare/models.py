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

from dataclasses import asdict, dataclass
from typing import Protocol

# --- Vocabulaires ------------------------------------------------------------
# Fermés, et vérifiés à la construction. Un libellé inconnu vaut "" (inconnu),
# jamais une devinette : une boîte « semi-automatique » n'est ni auto ni
# manuelle, et l'inventer fausserait toute comparaison de prix.

SOURCES = ("autoscout24", "autohero", "lacentrale")
MARKETS = ("fr", "de")
FUELS = ("essence", "diesel", "electrique", "hybride", "")
GEARS = ("auto", "manuelle", "")
SELLERS = ("pro", "particulier", "")
SORTS = ("price_asc", "price_desc", "age_desc")
SOURCE_STATUSES = ("ok", "challenge", "error", "capped")


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


@dataclass(slots=True)
class Page:
    """Une page de résultats telle que le site la rend.

    `announced_total` est le compteur du site pour la requête entière, pas pour
    la page. `None` quand le site ne le donne pas : l'absence est dite, pas
    comblée par `len(listings)`.
    """

    listings: list[Listing]
    announced_total: int | None = None
    page_number: int = 1


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
    sort: str = "price_asc"


@dataclass(slots=True)
class SourceCoverage:
    """Ce qu'une récolte a réellement lu sur une source. Jamais vide.

    `read` compte les annonces lues, `unique` celles qui restent après
    dédoublonnage intra-source, `dropped_duplicates` la différence. Un
    `status` autre que "ok" interdit de présenter la source comme couverte.
    """

    source: str
    market: str
    status: str = "ok"
    announced_total: int | None = None
    read: int = 0
    unique: int = 0
    dropped_duplicates: int = 0
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
    """

    listings_distinct: int = 0
    runs: int = 0
    sources: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = []

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
    """La source n'a pas répondu ce qu'il fallait : statut, défi, réseau."""
