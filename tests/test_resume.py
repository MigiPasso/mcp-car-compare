"""Le jeton de reprise d'une RÉCOLTE : une lecture profonde en appels courts.

Mesuré le 2026-11-28 (`TODO.md`) :
`harvest(sources=["lacentrale"], pages_max=20, budget_s=300)` puis
`pages_max=10, budget_s=100` expiraient **tous les deux** côté client
(`Request timed out`) sans rien écrire en base, et le seul jeton rendu était le
`cursor` d'`offers` — qui pagine des annonces déjà lues et ne reprend pas la
récolte. Le message de couverture conseillait en plus « relance avec
pages_max=20 », c'est-à-dire une action qui échoue.

Ce que ces tests certifient, un par un :

- un appel coupé par son budget rend un jeton, et ce jeton reprend la suite ;
- la couverture CUMULE le total depuis le début du job (elle ne repart pas de
  zéro à chaque appel) ;
- jusqu'à épuisement, `resumes` devient vide ;
- un jeton d'autres critères, ou d'une récolte inconnue, est refusé en le nommant ;
- deux sources qui s'arrêtent ne rendent que les jetons des sources arrêtées ;
- l'avertissement cite le jeton, jamais `pages_max=20` ;
- un jeton de récolte et un curseur d'`offers` ne se confondent pas.

Aucun test ne touche le réseau : la source est factice et injectée, l'horloge
est pilotée à la main.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_car_compare import cursor as cursor_module
from mcp_car_compare import resume as resume_module
from mcp_car_compare import tools
from mcp_car_compare.cursor import CursorError
from mcp_car_compare.models import Criteria, Page
from mcp_car_compare.resume import ResumeError
from mcp_car_compare.store import UnknownJobError
from test_tools import FakeClock, FakeMcp, FakeSource, install, listing


def harvest(
    mcp: FakeMcp,
    criteria: dict,
    budget_s: int | None = None,
    resume: str | None = None,
) -> dict:
    # Une source explicite : le défaut de l'outil est désormais toutes les
    # sources servies, et ces tests portent sur la reprise d'UNE source.
    return asyncio.run(
        mcp.tools["harvest"]({"sources": ["autoscout24"], **criteria}, budget_s, resume)
    )


def slow_install(tmp_path, pages: int, *, seconds_per_page: float = 40.0):
    """Une récolte de `pages` pages à `seconds_per_page`, horloge pilotée.

    C'est le rythme mesuré de LaCentrale par le navigateur (~8 s/page, et le
    client MCP abandonne bien avant 20 pages) : la coupure vient donc du BUDGET,
    jamais d'un plafond de pages ou d'un total annoncé. Les annonces n'annoncent
    aucun total, comme LaCentrale quand elle se tait.
    """
    clock = FakeClock()
    source = FakeSource(
        [
            Page(listings=[listing(page * 2 + index) for index in range(2)])
            for page in range(pages)
        ]
    )
    source.on_search = lambda _criteria, _page: clock.advance(seconds_per_page)
    return install(tmp_path, source, clock=clock), source


# --- les quatre points du contrat, un par test ----------------------------------


def test_une_recolte_coupee_par_son_budget_rend_un_jeton_de_reprise(tmp_path):
    mcp, source = slow_install(tmp_path, pages=5)
    result = harvest(mcp, {"pages_max": 3}, 20)  # une page tient, la deuxième non

    (row,) = result["coverage"]
    assert (row["status"], row["truncated"]) == ("capped", True)
    assert (row["read"], row["unique"]) == (2, 2)
    # UN jeton, pour LA source arrêtée, et il désigne la page non lue.
    (reprise,) = result["resumes"]
    assert (reprise["source"], reprise["market"]) == ("autoscout24", "fr")
    token = resume_module.decode(reprise["token"])
    assert (token.job_id, token.source, token.market) == (result["job_id"], "autoscout24", "fr")
    assert token.next_page == 2
    # La page 2 n'a jamais été DEMANDÉE : le budget a coupé avant, et c'est le
    # rôle du jeton que de la reprendre sans repayer la page 1.
    assert [number for _criteria, number in source.searches] == [1]


def test_la_reprise_lit_la_suite_et_la_couverture_cumule(tmp_path):
    mcp, source = slow_install(tmp_path, pages=5)
    first = harvest(mcp, {"pages_max": 3}, 20)
    (reprise,) = first["resumes"]

    second = harvest(mcp, {"pages_max": 3}, 20, resume=reprise["token"])

    (row,) = second["coverage"]
    assert second["job_id"] == first["job_id"]  # le MÊME job, pas un nouveau
    # La page 2 a bien été demandée : la reprise lit la SUITE, pas le début.
    assert source.searches[-1][1] == 2
    # LE point qui fait qu'une couverture ne peut pas être lue source par
    # source : le total du job est de 4 annonces, pas de 2. Un compteur remis à
    # zéro aurait annoncé 2 annonces lues pour un marché de 10.
    assert (row["read"], row["unique"]) == (4, 4)
    # Et le curseur d'`offers` rendu par le PREMIER appel reste valable : une
    # reprise continue le job, elle ne lui donne pas une nouvelle empreinte de
    # critères — sinon le curseur du premier appel serait refusé par le second.
    assert len(mcp.tools["offers"](second["job_id"], first["cursor"], 25)["items"]) == 4


def test_des_reprises_jusqu_a_epuisement_vident_la_liste_des_jetons(tmp_path):
    mcp, _source = slow_install(tmp_path, pages=5)
    result = harvest(mcp, {"pages_max": 3}, 20)
    assert len(result["resumes"]) == 1

    # Deux reprises : la page 3 après la page 2, puis le plafond `pages_max=3`
    # du job — il n'y a plus de page À DEMANDER pour cette récolte, donc plus de
    # jeton. Un jeton qui mènerait à la page 4 serait la consigne qui échoue que
    # ce lot corrige : `pages_max` est le plafond du JOB, pas celui de l'appel.
    for _ in range(2):
        (reprise,) = result["resumes"]
        result = harvest(mcp, {"pages_max": 3}, 20, resume=reprise["token"])
    assert result["resumes"] == []
    (row,) = result["coverage"]
    assert (row["read"], row["unique"]) == (6, 6)  # 3 pages cumulées


def test_une_reprise_avec_d_autres_criteres_est_refusee_en_le_nommant(tmp_path):
    mcp, _source = slow_install(tmp_path, pages=5)
    (reprise,) = harvest(mcp, {"pages_max": 3}, 20)["resumes"]

    with pytest.raises(ResumeError, match="autres critères"):
        harvest(mcp, {"pages_max": 3, "make": "Renault"}, 20, resume=reprise["token"])


def test_une_reprise_d_une_recolte_inconnue_est_refusee(tmp_path):
    mcp, _source = slow_install(tmp_path, pages=5)
    # Mêmes critères que l'appel : le helper ajoute `sources`, et l'empreinte
    # porte les critères (pas les bornes de lecture).
    fingerprint = cursor_module.criteria_fingerprint(
        Criteria(pages_max=3, sources=("autoscout24",))
    )
    forgeron = resume_module.encode(
        resume_module.ResumeToken(
            job_id="jamais-ecrit",
            criteria_hash=fingerprint,
            source="autoscout24",
            market="fr",
            next_page=2,
        )
    )
    with pytest.raises(UnknownJobError, match="jamais-ecrit"):
        harvest(mcp, {"pages_max": 3}, 20, resume=forgeron)


def test_la_reprise_applique_le_budget_de_l_appel_pas_celui_d_origine(tmp_path):
    # Le budget est une ressource d'un APPEL (tenir dans le délai du client), pas
    # une propriété du job : une reprise avec un autre budget doit être acceptée,
    # sinon le seul geste qu'un jeton sert à permettre serait refusé.
    mcp, _source = slow_install(tmp_path, pages=5)
    (reprise,) = harvest(mcp, {"pages_max": 3}, 20)["resumes"]

    result = harvest(mcp, {"pages_max": 3}, 100, resume=reprise["token"])

    (row,) = result["coverage"]
    assert row["reason"].startswith("plafond atteint")  # 100 s : trois pages
    assert (row["read"], row["unique"]) == (6, 6)


# --- deux sources, une seule qui s'arrête --------------------------------------


def test_une_seule_source_s_arrete_un_seul_jeton(tmp_path):
    # Deux sources du périmètre, la seconde lente (navigateur) : la rapide a fini
    # son stock, la lente est coupée par le budget. Le jeton rendu doit être
    # celui de la LENTE, et d'elle seule — un jeton pour la source finie
    # proposerait une reprise qui ne lit rien.
    clock = FakeClock()
    rapide = FakeSource([Page(listings=[listing(1), listing(2)]), Page(listings=[])])
    lente = FakeSource([Page(listings=[listing(index + 10) for index in range(2)])] * 4)
    lente.on_search = lambda _criteria, _page: clock.advance(40)
    sources = {"autoscout24": rapide, "lacentrale": lente}
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=tmp_path / "corpus.db",
        source_factory=lambda name: sources[name],
        clock=clock,
    )

    result = harvest(mcp, {"sources": ["autoscout24", "lacentrale"], "pages_max": 4}, 60)

    assert [(row["source"], row["truncated"]) for row in result["coverage"]] == [
        ("autoscout24", False),
        ("lacentrale", True),
    ]
    (reprise,) = result["resumes"]
    assert (reprise["source"], reprise["market"]) == ("lacentrale", "fr")
    (warning,) = result["warnings"]
    assert warning.startswith("lacentrale (fr)")


def test_un_jeton_de_recolte_n_est_pas_un_curseur_d_offers(tmp_path):
    # Les deux types se ressemblent (base64 URL, un `job_id`, une empreinte) et
    # ne désignent pas la même chose : un offset de lecture contre une page à
    # récolter. Les confondre rendrait un `offers` qui pagine la mauvaise chose.
    mcp, _source = slow_install(tmp_path, pages=5)
    result = harvest(mcp, {"pages_max": 3}, 20)
    (reprise,) = result["resumes"]

    with pytest.raises(ResumeError, match="n'est pas un jeton de récolte"):
        resume_module.decode(result["cursor"])
    with pytest.raises(CursorError, match="curseur illisible"):
        cursor_module.decode(reprise["token"])
    # Et le curseur reste utilisable comme curseur d'`offers`.
    page = mcp.tools["offers"](result["job_id"], result["cursor"], 25)
    assert len(page["items"]) == 2


# --- ce que l'avertissement doit dire ------------------------------------------


def test_l_avertissement_cite_le_jeton_jamais_un_pages_max_qui_echoue(tmp_path):
    mcp, _source = slow_install(tmp_path, pages=5)
    result = harvest(mcp, {"pages_max": 3}, 20)

    (warning,) = result["warnings"]
    (reprise,) = result["resumes"]
    assert f'resume="{reprise["token"]}"' in warning
    # Le défaut mesuré : ce message conseillait `pages_max=20`, un appel qui
    # expirait côté client sans rien écrire.
    assert "pages_max=20" not in warning


def test_l_avertissement_d_une_source_epuisee_ne_propose_aucune_reprise(tmp_path):
    # Le pendant : quand il n'y a plus de page à demander, proposer un jeton
    # serait la même faute sous une autre forme. Le message le dit.
    source = FakeSource([Page(listings=[listing(index) for index in range(20)], announced_total=500)])
    mcp = install(tmp_path, source)
    result = harvest(mcp, {"pages_max": 2})

    assert result["resumes"] == []
    (warning,) = result["warnings"]
    assert "resume=" not in warning
    assert "aucune page de plus à demander" in warning
    assert "pages_max=2" in warning  # le plafond du job, nommé comme tel
