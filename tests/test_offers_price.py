"""`offers` doit dire à l'appelant si le prix affiché est un prix.

AutoScout24 publie des annonces à 1 € pour « prix non communiqué ». Un modèle
qui lit `price: 1` sur une BMW de 2024 doit pouvoir le constater sans le
deviner : le champ `price_plausible` est calculé à la sérialisation, la valeur
stockée reste celle du site — on ne réécrit pas la donnée, on la qualifie.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_car_compare.models import Page
from mcp_car_compare.store import Store
from mcp_car_compare.tools import register

from test_tools import FakeClock, FakeSource, harvest, install, listing


def test_offers_qualifie_un_prix_derisoire_et_un_prix_credible(tmp_path):
    derisoire = listing(0)
    derisoire.price = 1
    credible = listing(1)
    credible.price = 15_000
    mcp = install(tmp_path, FakeSource([Page(listings=[derisoire, credible])]))

    result = harvest(mcp)
    page = mcp.tools["offers"](result["job_id"], None, 25)

    par_id = {item["source_id"]: item for item in page["items"]}
    assert par_id[derisoire.source_id]["price"] == 1  # la donnée du site est gardée
    assert par_id[derisoire.source_id]["price_plausible"] is False
    assert par_id[credible.source_id]["price_plausible"] is True


def test_le_tri_par_defaut_du_parsage_est_celui_du_contrat():
    # Deux copies du défaut ont déjà divergé une fois : le contrat disait
    # `age_desc`, le parsage des critères disait `price_asc`, donc le site
    # recevait toujours le prix croissant et servait les annonces à 1 € en
    # premier. Ce test tient la jointure.
    from mcp_car_compare.models import Criteria, DEFAULT_SORT
    from mcp_car_compare.tools import _parse_criteria

    assert _parse_criteria({}, pages_max=5).sort == DEFAULT_SORT == Criteria().sort
