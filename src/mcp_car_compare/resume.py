"""Jeton de reprise d'une RÉCOLTE : `{job_id, criteria_hash, source, market,
next_page}`, encodé en base64 URL.

Un jeton de récolte n'est PAS un curseur d'`offers`, et les deux types ne se
mélangent pas : le curseur (`cursor.py`) désigne une position de LECTURE dans le
corpus d'une récolte déjà écrite (`offset`), là où ce jeton-ci désigne la PAGE
SUIVANTE à récolter sur une source qui s'est arrêtée avant la fin. Un même
`job_id` porte les deux — `harvest` rend l'un et l'autre — mais un jeton passé à
`offers` est refusé comme illisible, et réciproquement, parce que les clés
attendues diffèrent (`cursor._KEYS` contre `_KEYS` ici).

C'est ce qui rend une récolte profonde possible : mesuré le 2026-11-28, un
`harvest(sources=["lacentrale"], pages_max=20, budget_s=300)` expirait côté
client (`Request timed out`) sans rien écrire en base, et le `cursor` rendu
jusque-là ne reprenait pas la récolte — il pagine des annonces déjà lues. Une
récolte de plusieurs minutes se découpe donc en appels courts, chacun reprenant
là où le précédent s'est arrêté.

Un jeton porte UNE source : `harvest` rend une LISTE `resumes`, parce que deux
sources peuvent s'arrêter dans le même appel sans reprendre au même endroit. Un
jeton unique portant « l'état de toutes les sources » aurait fait une structure
imbriquée dont le refus ne se nomme plus.

Opaque par convention, pas scellé : `base64` n'est pas une serrure, et n'importe
qui peut forger un jeton. « Opaque » veut donc dire que l'appelant le traite
comme un jeton dont la forme interne peut changer sans préavis, jamais qu'il le
fabrique. Ce qui borne un jeton forgé est là où il est relu : `harvest` refuse
un jeton dont l'empreinte ne correspond pas aux critères fournis, et une récolte
qui n'existe pas.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from .models import MARKETS, SOURCES, Criteria

# Clés exactes d'un jeton. Une clé en trop est un jeton d'une autre version —
# ou un curseur d'`offers`, qui n'a aucune de ces clés-là.
_KEYS = frozenset({"job_id", "criteria_hash", "source", "market", "next_page"})


class ResumeError(ValueError):
    """Le jeton est illisible, ou il ne reprend pas la récolte demandée."""


@dataclass(frozen=True, slots=True)
class ResumeToken:
    """Où reprendre une source d'une récolte, et de quelle récolte il s'agit.

    `next_page` est une page du site, comptée depuis 1 : `1` veut dire « rien
    n'a encore été lu sur cette source » (une source coupée avant sa première
    page), et une reprise à cette valeur relit depuis le début.
    """

    job_id: str
    criteria_hash: str
    source: str
    market: str
    next_page: int

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "criteria_hash": self.criteria_hash,
            "source": self.source,
            "market": self.market,
            "next_page": self.next_page,
        }


def encode(token: ResumeToken) -> str:
    """Encode un jeton, sans remplissage `=` (il passe dans une URL)."""
    payload = json.dumps(token.as_dict(), sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode(token: str) -> ResumeToken:
    """Relit un jeton. Lève `ResumeError` sur tout ce qui n'en est pas un."""
    if not isinstance(token, str) or not token:
        raise ResumeError("jeton de reprise vide")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ResumeError(f"jeton de reprise illisible : {exc}") from exc
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ResumeError(
            f"jeton de reprise illisible : clés attendues {sorted(_KEYS)} "
            "(un curseur d'`offers` n'est pas un jeton de récolte)"
        )
    job_id = data["job_id"]
    criteria_hash = data["criteria_hash"]
    source = data["source"]
    market = data["market"]
    next_page = data["next_page"]
    if not isinstance(job_id, str) or not job_id:
        raise ResumeError("jeton de reprise illisible : job_id manquant")
    if not isinstance(criteria_hash, str) or not criteria_hash:
        raise ResumeError("jeton de reprise illisible : criteria_hash manquant")
    if source not in SOURCES:
        raise ResumeError(
            f"jeton de reprise illisible : source {source!r} inconnue "
            f"(attendu : {', '.join(SOURCES)})"
        )
    if market not in MARKETS:
        raise ResumeError(
            f"jeton de reprise illisible : marché {market!r} inconnu "
            f"(attendu : {', '.join(MARKETS)})"
        )
    if isinstance(next_page, bool) or not isinstance(next_page, int) or next_page < 1:
        raise ResumeError(f"jeton de reprise illisible : next_page {next_page!r}")
    return ResumeToken(
        job_id=job_id,
        criteria_hash=criteria_hash,
        source=source,
        market=market,
        next_page=next_page,
    )


def check(token: ResumeToken, *, criteria: Criteria, criteria_hash: str) -> None:
    """Refuse un jeton qui ne reprend pas cette récolte-là.

    Deux refus distincts, comme pour le curseur d'`offers` : l'appelant doit
    pouvoir dire s'il s'est trompé de critères ou de source. Ils sont calculés
    sur les critères FOURNIS — c'est la seule façon de vérifier que la reprise
    relit bien la même recherche, et non les pages d'une autre.

    L'existence du `job_id` n'est pas vérifiée ici : c'est `Store.job` qui la
    connaît, et c'est `harvest` qui l'appelle avant toute écriture.
    """
    if token.criteria_hash != criteria_hash:
        raise ResumeError(
            "jeton de reprise d'autres critères : il ne vaut que pour ceux de la "
            "récolte d'origine — renvoie les mêmes critères, ou relance une "
            "récolte neuve sans `resume`"
        )
    if token.source not in criteria.sources or token.market not in criteria.markets:
        raise ResumeError(
            f"jeton de reprise de {token.source} ({token.market}), que ces "
            f"critères ne demandent pas (sources : {', '.join(criteria.sources)} ; "
            f"marchés : {', '.join(criteria.markets)})"
        )
