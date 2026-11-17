"""Preuves de `config.py` : défauts, surcharge par l'environnement, pas de gel.

Ces tests effacent d'abord toutes les `MCC_*` héritées du shell : sans ça, un
`MCC_DB` exporté par l'opérateur ferait passer le test des défauts pour une
régression, et les tests dépendraient de la machine qui les lance.
"""

from __future__ import annotations

import dataclasses

import pytest

from mcp_car_compare import config
from mcp_car_compare.models import MAX_BUDGET_S, MAX_PAGES, MAX_PAGES_ANY


@pytest.fixture(autouse=True)
def _environnement_propre(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MCC_DB", "MCC_BUDGET_S", "MCC_PAGES_MAX", "MCC_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_sans_variable_les_defauts_sont_ceux_de_l_image() -> None:
    """`/data` est le point de montage du volume, pas un répertoire de l'image."""
    loaded = config.load_config()

    assert loaded.db_path == config.DEFAULT_DB
    assert loaded.db_path == type(loaded.db_path)("/data/corpus.db")
    assert loaded.budget_s == 60
    # Dix par défaut : une valeur trop basse est ce qui a produit « 7 Série 3
    # sur 100 » en usage réel. Le plafond reste 20 (`MAX_PAGES`).
    assert loaded.pages_max == 10
    assert loaded.log_level == "INFO"


def test_mcc_db_present_est_rendu_tel_quel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCC_DB", "/srv/corpus/essai.db")

    assert config.load_config().db_path == type(config.DEFAULT_DB)("/srv/corpus/essai.db")


def test_budget_et_pages_sont_lus_comme_des_entiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCC_BUDGET_S", "12")
    monkeypatch.setenv("MCC_PAGES_MAX", "3")

    loaded = config.load_config()

    assert loaded.budget_s == 12
    assert loaded.pages_max == 3


def test_entier_illisible_nomme_la_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un `MCC_BUDGET_S=trente` doit échouer en le nommant.

    Rabattu sur 60 en silence, il ferait croire à un budget dix fois plus court
    alors que le serveur en utilise dix fois plus.
    """
    monkeypatch.setenv("MCC_BUDGET_S", "trente")

    with pytest.raises(ValueError, match="MCC_BUDGET_S"):
        config.load_config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCC_BUDGET_S", "0"),
        ("MCC_BUDGET_S", "-1"),
        ("MCC_PAGES_MAX", "0"),
        ("MCC_PAGES_MAX", "-5"),
    ],
)
def test_une_borne_nulle_ou_negative_est_refusee(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Zéro et négatif sont refusés, pas rabattus sur le défaut.

    `MCC_PAGES_MAX=0` n'est pas « aucune page » mais une récolte qui ne lit rien
    en ayant l'air de tourner ; `MCC_BUDGET_S=0`, une récolte toujours coupée.
    Un rabattement silencieux ferait croire à un réglage qui n'a pas été lu.
    """
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} doit être un entier >= 1"):
        config.load_config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCC_BUDGET_S", str(MAX_BUDGET_S + 1)),
        # `MAX_PAGES_ANY` (30), pas `MAX_PAGES` (20) : le plafond dépend des
        # sources demandées, et la configuration n'en connaît aucune. 21 est
        # une valeur parfaitement valide pour LaCentrale.
        ("MCC_PAGES_MAX", str(MAX_PAGES_ANY + 1)),
    ],
)
def test_une_borne_au_dessus_du_plafond_est_refusee(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Les mêmes plafonds qu'au parsage des critères, côté configuration.

    `MAX_PAGES` est le plafond mesuré du site, `MAX_BUDGET_S` la durée au-delà
    de laquelle un client MCP a abandonné : ce sont des bornes du serveur, pas
    des objectifs, et la variable d'environnement n'est pas un chemin plus
    permissif que l'argument d'outil.
    """
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} ne peut pas dépasser"):
        config.load_config()


def test_log_level_est_normalise_en_majuscules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCC_LOG_LEVEL", " debug ")

    assert config.load_config().log_level == "DEBUG"


def test_la_config_est_relue_a_chaque_appel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le piège que ce test tient : une config figée à l'import.

    Les tests qui posent une variable après l'import du module liraient alors la
    valeur d'un autre test, et la panne ne se verrait qu'en suite complète.
    """
    avant = config.load_config()
    monkeypatch.setenv("MCC_DB", "/tmp/apres.db")
    apres = config.load_config()

    assert avant.db_path != apres.db_path
    assert apres.db_path == type(config.DEFAULT_DB)("/tmp/apres.db")


def test_config_est_immuable() -> None:
    """Un appelant qui écrit dans la config partagée ne doit pas pouvoir le faire."""
    loaded = config.load_config()

    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.budget_s = 1  # type: ignore[misc]
