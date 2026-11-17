"""Curseur de pagination : opaque par convention, refusé s'il ne s'applique plus.

Un curseur porte `{job_id, offset, sort, criteria_hash}` encodé en base64 URL.
Il n'est ni chiffré ni signé : n'importe qui peut le relire (`base64` n'est pas
une serrure) et en forger un autre. « Opaque » veut donc dire ceci, et pas
davantage : l'appelant doit le traiter comme un jeton dont la forme interne peut
changer sans préavis, pas comme une valeur qu'il peut fabriquer. Ce n'est pas
une garantie cryptographique, et le code qui s'appuie dessus ne doit rien en
affirmer de plus.

Ce que le jeton ne protège pas est borné là où il est relu : le serveur le
**refuse** plutôt que de rendre des pages incohérentes quand le tri ou les
critères du corpus ont changé (PLAN.md §8). Un `offset` négatif est refusé dès
le décodage (`decode`, plus bas) ; un `offset` plus grand que le corpus ne peut
l'être qu'ici, parce que seul `tools.offers` connaît la taille du corpus — il le
refuse en la nommant.

`criteria_hash` porte tous les champs qui changent le résultat d'une récolte,
`budget_s` compris : deux récoltes du même critère avec des budgets différents
ne lisent pas le même nombre d'annonces.

L'empreinte des critères SEULS (`criteria_fingerprint`) est une seconde forme,
avec un autre usage : c'est celle que porte un jeton de reprise (`resume.py`),
parce que le budget d'une reprise est celui de l'appel qui reprend et non une
propriété du job (voir `resume.check`). Les deux formes partagent le même
constructeur (`_canonical`), donc un champ ajouté au contrat entre dans les
deux au lieu d'en oublier une.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

from .models import SORTS, Criteria

# Clés exactes d'un curseur. Une clé en trop est un curseur d'une autre
# version : on le refuse au lieu d'ignorer ce qu'on ne comprend pas.
_KEYS = frozenset({"job_id", "offset", "sort", "criteria_hash"})


class CursorError(ValueError):
    """Le curseur est illisible, ou il ne s'applique pas à ce corpus."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """Position de lecture dans le corpus d'une récolte."""

    job_id: str
    offset: int
    sort: str
    criteria_hash: str

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "offset": self.offset,
            "sort": self.sort,
            "criteria_hash": self.criteria_hash,
        }


def encode(cursor: Cursor) -> str:
    """Encode un curseur, sans remplissage `=` (il passe dans une URL)."""
    payload = json.dumps(cursor.as_dict(), sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode(token: str) -> Cursor:
    """Relit un curseur. Lève `CursorError` sur tout ce qui n'en est pas un."""
    if not isinstance(token, str) or not token:
        raise CursorError("curseur vide")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise CursorError(f"curseur illisible : {exc}") from exc
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise CursorError(f"curseur illisible : clés attendues {sorted(_KEYS)}")
    job_id = data["job_id"]
    offset = data["offset"]
    sort = data["sort"]
    criteria_hash = data["criteria_hash"]
    if not isinstance(job_id, str) or not job_id:
        raise CursorError("curseur illisible : job_id manquant")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CursorError(f"curseur illisible : offset {offset!r}")
    if sort not in SORTS:
        raise CursorError(f"curseur illisible : tri {sort!r} inconnu")
    if not isinstance(criteria_hash, str) or not criteria_hash:
        raise CursorError("curseur illisible : criteria_hash manquant")
    return Cursor(job_id=job_id, offset=offset, sort=sort, criteria_hash=criteria_hash)


def check(cursor: Cursor, *, job_id: str, sort: str, criteria_hash: str) -> None:
    """Refuse un curseur qui ne décrit pas le corpus demandé.

    Les trois refus sont explicites et distincts : l'appelant doit pouvoir dire
    s'il s'est trompé de récolte, de tri, ou de critères.
    """
    if cursor.job_id != job_id:
        raise CursorError(
            f"curseur de la récolte {cursor.job_id!r}, demandé pour {job_id!r}"
        )
    if cursor.sort != sort:
        raise CursorError(
            f"curseur d'un tri {cursor.sort!r} alors que la récolte est triée en "
            f"{sort!r} : reprendre sans curseur"
        )
    if cursor.criteria_hash != criteria_hash:
        raise CursorError(
            "curseur d'autres critères que cette récolte : reprendre sans curseur"
        )


def _canonical(
    criteria: Criteria, budget_s: int | None, *, with_pages_max: bool = True
) -> str:
    """Les champs qui changent le résultat d'une récolte, en une chaîne stable.

    `markets` et `sources` sont triés avant hachage : l'ordre de parcours ne
    change pas le corpus, il ne doit donc pas changer l'empreinte. `budget_s`
    n'entre dans la charge que s'il est fourni, et `pages_max` que si
    `with_pages_max` — c'est toute la différence entre `hash_criteria` (identité
    d'une récolte) et `criteria_fingerprint` (identité d'une recherche), et elle
    est dite là-bas.
    """
    payload = {
        "make": criteria.make,
        "model": criteria.model,
        "year_min": criteria.year_min,
        "year_max": criteria.year_max,
        "km_max": criteria.km_max,
        "price_min": criteria.price_min,
        "price_max": criteria.price_max,
        "fuel": criteria.fuel,
        "gear": criteria.gear,
        "markets": sorted(criteria.markets),
        "sources": sorted(criteria.sources),
        "sort": criteria.sort,
    }
    if with_pages_max:
        payload["pages_max"] = criteria.pages_max
    if budget_s is not None:
        payload["budget_s"] = budget_s
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def hash_criteria(criteria: Criteria, budget_s: int) -> str:
    """Empreinte courte d'une RÉCOLTE : critères et budget de l'appel.

    C'est l'identité que portent `runs.criteria_hash` et le curseur d'`offers` :
    deux récoltes du même critère avec des budgets différents ne lisent pas le
    même nombre d'annonces, elles ne sont donc pas la même récolte.
    """
    return _canonical(criteria, budget_s)


def criteria_fingerprint(criteria: Criteria) -> str:
    """Empreinte des critères SEULS, sans le budget.

    C'est l'empreinte d'un jeton de reprise (`resume.py`) : elle identifie la
    RECHERCHE — ce qu'on cherche — et non la récolte. Le budget ET le plafond de
    pages sont ceux de l'appel qui reprend, décidés par l'appelant pour tenir
    dans le délai de son client : les inclure ici rendrait un jeton refusé dès
    que la reprise demande plus de pages, c'est-à-dire précisément le geste
    qu'un jeton de reprise existe pour permettre. Mesuré le 2026-11-28 : un
    harvest coupé par `pages_max=2` rendait un jeton inutilisable, puisque toute
    reprise aurait porté un autre plafond.

    Un job garde quand même son `hash_criteria` d'origine, budget compris : une
    reprise n'écrit jamais une nouvelle identité de récolte, elle continue la
    même.
    """
    return _canonical(criteria, None, with_pages_max=False)
