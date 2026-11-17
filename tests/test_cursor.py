"""Curseur : opaque à l'extérieur, refusé dès qu'il ne décrit plus le corpus.

Aucun de ces tests ne touche au corpus : ils fixent le format et les refus.
"""

from __future__ import annotations

import base64
import json

import pytest

from mcp_car_compare.cursor import (
    Cursor,
    CursorError,
    check,
    criteria_fingerprint,
    decode,
    encode,
    hash_criteria,
)
from mcp_car_compare.models import Criteria


def token(payload: object) -> str:
    """Fabrique un curseur de forme arbitraire, comme le ferait un appelant."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def position(**overrides: object) -> Cursor:
    fields = {
        "job_id": "job-1",
        "offset": 25,
        "sort": "price_asc",
        "criteria_hash": "0123456789abcdef",
    }
    fields.update(overrides)
    return Cursor(**fields)  # type: ignore[arg-type]


# --- format -------------------------------------------------------------------


def test_a_cursor_survives_a_round_trip():
    assert decode(encode(position())) == position()


def test_a_cursor_is_not_readable_by_the_caller():
    encoded = encode(position())
    assert "job-1" not in encoded
    assert "price_asc" not in encoded
    assert "=" not in encoded  # il passe dans une URL sans échappement
    padded = encoded + "=" * (-len(encoded) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))["offset"] == 25


@pytest.mark.parametrize(
    "payload",
    [
        {"job_id": "job-1", "offset": 0, "sort": "price_asc"},  # clé manquante
        {
            "job_id": "job-1",
            "offset": 0,
            "sort": "price_asc",
            "criteria_hash": "x",
            "limit": 25,
        },  # clé en trop
        {"job_id": "", "offset": 0, "sort": "price_asc", "criteria_hash": "x"},
        {"job_id": "job-1", "offset": -1, "sort": "price_asc", "criteria_hash": "x"},
        {"job_id": "job-1", "offset": "0", "sort": "price_asc", "criteria_hash": "x"},
        {"job_id": "job-1", "offset": 0, "sort": "prix", "criteria_hash": "x"},
        {"job_id": "job-1", "offset": 0, "sort": "price_asc", "criteria_hash": ""},
        [1, 2, 3],
        "pas un objet",
    ],
)
def test_a_malformed_cursor_is_refused(payload: object):
    with pytest.raises(CursorError):
        decode(token(payload))


@pytest.mark.parametrize("garbage", ["", "pas-du-base64!", "eyJvZmZzZXQiOjB9"])
def test_a_cursor_that_is_not_base64_of_our_json_is_refused(garbage: str):
    with pytest.raises(CursorError):
        decode(garbage)


def test_a_boolean_offset_is_not_an_offset():
    # `True == 1` en Python : un booléen passerait pour l'offset 1.
    with pytest.raises(CursorError):
        decode(token({"job_id": "j", "offset": True, "sort": "price_asc", "criteria_hash": "x"}))


# --- refus --------------------------------------------------------------------


def test_a_cursor_from_another_harvest_is_refused():
    with pytest.raises(CursorError, match="récolte"):
        check(position(job_id="job-2"), job_id="job-1", sort="price_asc", criteria_hash="0123456789abcdef")


def test_a_cursor_taken_with_another_sort_is_refused():
    # PLAN.md §8 : un curseur porte son tri, et on refuse plutôt que de rendre
    # des pages incohérentes.
    with pytest.raises(CursorError, match="tri"):
        check(position(sort="price_desc"), job_id="job-1", sort="price_asc", criteria_hash="0123456789abcdef")


def test_a_cursor_of_other_criteria_is_refused():
    with pytest.raises(CursorError, match="critères"):
        check(position(criteria_hash="ffffffffffffffff"), job_id="job-1", sort="price_asc", criteria_hash="0123456789abcdef")


def test_a_matching_cursor_passes():
    check(position(), job_id="job-1", sort="price_asc", criteria_hash="0123456789abcdef")


# --- empreinte des critères ---------------------------------------------------


def test_the_same_criteria_hash_the_same_way():
    assert hash_criteria(Criteria(make="renault"), 60) == hash_criteria(Criteria(make="renault"), 60)


@pytest.mark.parametrize(
    "other",
    [
        Criteria(make="renault", km_max=100_000),
        Criteria(make="renault", sort="price_desc"),
        Criteria(make="renault", markets=("fr", "de")),
        Criteria(make="renault", pages_max=9),
    ],
)
def test_a_criterion_that_changes_the_result_changes_the_hash(other: Criteria):
    assert hash_criteria(Criteria(make="renault"), 60) != hash_criteria(other, 60)


def test_the_budget_changes_the_hash():
    # Deux budgets différents ne lisent pas le même nombre d'annonces : le
    # curseur de l'un ne décrit pas l'autre.
    assert hash_criteria(Criteria(make="renault"), 60) != hash_criteria(Criteria(make="renault"), 240)


def test_the_order_of_markets_does_not_change_the_hash():
    assert hash_criteria(Criteria(markets=("fr", "de")), 60) == hash_criteria(Criteria(markets=("de", "fr")), 60)


# --- empreinte des critères seuls (jeton de reprise) --------------------------


def test_l_empreinte_d_un_jeton_de_reprise_n_est_pas_celle_d_une_recolte():
    # Deux empreintes, deux usages, et la différence est le budget : un jeton de
    # reprise doit survivre à un changement de budget (c'est la ressource d'un
    # appel, pas une propriété du job), là où le curseur d'`offers` ne le doit
    # pas — deux budgets ne lisent pas le même nombre d'annonces. Mettre le
    # budget dans l'empreinte du jeton rendrait `ResumeError` sur une reprise
    # qui change de budget, donc refuserait le seul geste qu'un jeton sert à
    # permettre.
    criteres = Criteria(make="renault")
    assert criteria_fingerprint(criteres) != hash_criteria(criteres, 60)


def test_l_empreinte_des_criteres_seuls_ignore_la_profondeur_demandee():
    # L'INVERSE de ce que ce test affirmait avant le 2026-11-28, et c'est le
    # corriger, pas l'ajuster : `pages_max` est un plafond de LECTURE, pas un
    # critère de recherche. Imposer qu'il soit identique rendait inutilisable
    # tout jeton émis par une récolte coupée par son plafond — la reprise
    # demandait forcément plus de pages, donc portait un autre plafond, donc
    # était refusée. Mesuré : `pages_max=2` rendait un jeton que rien ne
    # pouvait reprendre.
    #
    # Ce qui doit rester vrai : le plafond change ce que la récolte LIT, donc il
    # reste dans l'identité de la récolte (`hash_criteria`).
    assert (
        criteria_fingerprint(Criteria(make="renault", pages_max=10))
        == criteria_fingerprint(Criteria(make="renault", pages_max=20))
    )
    assert hash_criteria(Criteria(make="renault", pages_max=10), 60) != hash_criteria(
        Criteria(make="renault", pages_max=20), 60
    )
