"""`corpus_age` : l'âge du corpus qui a SERVI la réponse, à côté de `coverage`.

La table `listings` ne portait aucune date : `analyze_models` présentait donc une
médiane comme « le marché », alors que le corpus mêle des récoltes d'âges
inconnus et peut compter des voitures déjà vendues. Rien n'est purgé (TODO.md
§3), aucune rétention n'est décidée : l'âge est rendu à côté des chiffres, et le
dire est le correctif.

Le corpus est écrit par le `Store` directement : ces tests portent sur ce que les
deux lecteurs LISENT, et une récolte ajouterait ses propres compteurs. Aucun test
ne touche le réseau, et les dates sont injectées — aucun `sleep`.

Chaque test doit pouvoir rougir : le commentaire dit quelle ligne retirée le fait
échouer quand ce n'est pas évident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_car_compare import tools
from mcp_car_compare.models import CorpusAge, Listing, SourceCoverage
from mcp_car_compare.store import Store

from test_tools import FakeMcp

# Deux dates de vue fixes et leurs formes stockées, écrites en clair : le format
# ISO-8601 UTC est le contrat de `last_seen_at`, et une forme qui bouge (celle
# des microsecondes, le suffixe) doit se voir ici plutôt qu'en production.
FIRST_SEEN = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
LATER_SEEN = datetime(2026, 11, 6, 18, 30, tzinfo=timezone.utc)
FIRST_ISO = "2026-11-01T09:00:00.000000+00:00"
LATER_ISO = "2026-11-06T18:30:00.000000+00:00"


def car(
    source_id: str,
    *,
    make: str = "Renault",
    group: str = "clio",
    label: str = "Clio",
) -> Listing:
    return Listing(
        source="autoscout24",
        market="fr",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        make=make,
        group=group,
        group_label=label,
        year=2021,
        km=48_000,
        price=11_000,
    )


def seed(tmp_path, pages: list[tuple[datetime, list[Listing]]], *, job_id: str = "job-1") -> Path:
    """Dépose le corpus, une page par date de vue, et une ligne de récolte.

    Chaque page ouvre son `Store` avec SA date : c'est l'horloge injectée, donc
    deux pages peuvent avoir deux âges sans qu'aucun test ne dorme.
    """
    path = tmp_path / "corpus.db"
    written = 0
    for seen_at, listings in pages:
        with Store(path, now=lambda moment=seen_at: moment) as store:
            store.add_page(job_id, list(listings))
        written += len(listings)
    with Store(path) as store:
        store.write_run(
            job_id,
            SourceCoverage(source="autoscout24", market="fr", read=written, unique=written),
            criteria_hash="h",
            sort="age_desc",
        )
    return path


def reader(path) -> FakeMcp:
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=path,
        # Les deux outils ne parlent qu'à SQLite : construire une source serait
        # déjà une faute, donc la fabrique échoue au lieu de rendre une doublure.
        source_factory=lambda source: pytest.fail(f"source construite : {source}"),
        clock=lambda: 0.0,
    )
    return mcp


# --- le même corpus, les mêmes bornes ------------------------------------------


def test_offers_et_analyze_models_donnent_les_memes_bornes_pour_le_meme_corpus(tmp_path):
    # Deux pages, deux dates : sans la colonne, les deux blocs n'auraient aucune
    # borne à rendre. Retirer `corpus_age` de l'un des deux retours fait rougir
    # la ligne correspondante.
    path = seed(
        tmp_path,
        [(FIRST_SEEN, [car("1"), car("2")]), (LATER_SEEN, [car("3")])],
    )
    mcp = reader(path)
    page = mcp.tools["offers"]("job-1", None, 25)
    analysis = mcp.tools["analyze_models"]({}, 30)
    assert page["corpus_age"] == {"oldest_seen": FIRST_ISO, "newest_seen": LATER_ISO}
    assert analysis["corpus_age"] == page["corpus_age"]
    # À côté, jamais dedans : la forme à huit clés de `coverage` est tenue par
    # `test_coverage.py` (une clé de plus y ferait échouer une comparaison
    # d'égalité), et ces deux lignes disent où le bloc vit.
    assert "corpus_age" not in page["coverage"]
    assert "corpus_age" not in analysis["coverage"]


def test_offers_date_la_recolte_qu_il_pagine_pas_la_page_rendue(tmp_path):
    # Une récolte de trois annonces vue en deux fois : la page de 2 annonces et
    # la page de 1 annonce doivent rendre les MÊMES bornes, celles de la récolte.
    # Des bornes par page changeraient d'une page à l'autre alors que le corpus
    # ne change pas — et `coverage`, juste à côté, décrit déjà la récolte.
    path = seed(
        tmp_path,
        [(FIRST_SEEN, [car("1"), car("2")]), (LATER_SEEN, [car("3")])],
    )
    mcp = reader(path)
    first = mcp.tools["offers"]("job-1", None, 2)
    second = mcp.tools["offers"]("job-1", first["next_cursor"], 2)
    assert [item["source_id"] for item in first["items"]] == ["1", "2"]
    assert [item["source_id"] for item in second["items"]] == ["3"]
    assert first["corpus_age"] == second["corpus_age"] == {
        "oldest_seen": FIRST_ISO,
        "newest_seen": LATER_ISO,
    }


def test_offers_ne_date_que_les_annonces_de_sa_propre_recolte(tmp_path):
    # Deux récoltes disjointes, deux dates : la vieille ne doit pas hériter de
    # l'âge de la nouvelle, ni l'inverse. Sans la jointure sur `run_listings`,
    # les deux jobs rendraient les bornes de la BASE — et `offers` sur une
    # récolte d'hier se lirait aussi frais que la récolte du jour.
    path = tmp_path / "corpus.db"
    with Store(path, now=lambda: FIRST_SEEN) as store:
        store.add_page("job-1", [car("1")])
    with Store(path, now=lambda: LATER_SEEN) as store:
        store.add_page("job-2", [car("2")])
    with Store(path) as store:
        for job in ("job-1", "job-2"):
            store.write_run(
                job,
                SourceCoverage(source="autoscout24", market="fr", read=1, unique=1),
                criteria_hash="h",
                sort="age_desc",
            )
    mcp = reader(path)
    assert mcp.tools["offers"]("job-1", None, 25)["corpus_age"] == {
        "oldest_seen": FIRST_ISO,
        "newest_seen": FIRST_ISO,
    }
    assert mcp.tools["offers"]("job-2", None, 25)["corpus_age"] == {
        "oldest_seen": LATER_ISO,
        "newest_seen": LATER_ISO,
    }


def test_analyze_models_date_le_corpus_filtre_et_pas_la_base_entiere(tmp_path):
    # La base porte une Clio du 1er et une 208 du 6. L'analyse des 208 ne doit
    # pas annoncer « vu il y a cinq jours » : la borne de la Clio ne concerne
    # aucune des annonces qu'elle a servies. `coverage`, lui, décrit la base
    # entière — les deux blocs ne répondent pas à la même question, et c'est
    # écrit dans chaque docstring.
    path = seed(
        tmp_path,
        [
            (FIRST_SEEN, [car("1")]),
            (LATER_SEEN, [car("2", make="Peugeot", group="208", label="208")]),
        ],
    )
    analysis = reader(path).tools["analyze_models"]({"make": "Peugeot"}, 30)
    assert analysis["corpus_age"] == {"oldest_seen": LATER_ISO, "newest_seen": LATER_ISO}
    assert analysis["coverage"]["listings_distinct"] == 2


def test_un_corpus_vide_rend_deux_null_jamais_un_bloc_absent(tmp_path):
    # Une récolte qui n'a rien lu : le bloc est là, avec deux `null`. Un bloc
    # absent se lirait comme une clé oubliée, et `null`/`null` dit exactement
    # ce que le corpus sait — rien.
    path = seed(tmp_path, [(FIRST_SEEN, [])])
    mcp = reader(path)
    assert mcp.tools["offers"]("job-1", None, 25)["corpus_age"] == {
        "oldest_seen": None,
        "newest_seen": None,
    }
    assert mcp.tools["analyze_models"]({}, 30)["corpus_age"] == {
        "oldest_seen": None,
        "newest_seen": None,
    }


# --- l'avertissement, des deux côtés du seuil ----------------------------------


def test_le_seuil_de_fraicheur_est_de_sept_jours_entiers():
    # Le cas borne, horloge fixée : la phrase apparaît au seuil et pas avant.
    # Passer la comparaison à `<=` (ou le seuil à 8) fait rougir la ligne des
    # sept jours ; la retirer entièrement fait rougir celle des six jours.
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def age(*, days: int, hours: int = 0) -> CorpusAge:
        oldest = (now - timedelta(days=days, hours=hours)).isoformat(timespec="microseconds")
        return CorpusAge(oldest_seen=oldest, newest_seen=oldest)

    assert tools.STALE_CORPUS_DAYS == 7
    # En deçà : silence. Six jours et vingt-trois heures n'est pas une semaine.
    assert tools._stale_corpus_warning(age(days=6, hours=23), now=now) is None
    # Au seuil : la phrase, qui cite l'âge et dit que des voitures peuvent être
    # vendues depuis.
    phrase = tools._stale_corpus_warning(age(days=7), now=now)
    assert phrase is not None
    assert "7 jour(s)" in phrase
    assert "vendues" in phrase
    # Un corpus sans date ne dit pas qu'il est frais : il ne dit rien.
    assert tools._stale_corpus_warning(CorpusAge(), now=now) is None


def test_un_corpus_de_plus_d_une_semaine_est_signale_dans_les_warnings(tmp_path):
    # Le défaut d'origine : une médiane présentée comme le marché alors que la
    # plus vieille annonce a un mois. Retirer l'appel à `_stale_corpus_warning`
    # dans `analyze_models` laisse `warnings` sans cette phrase.
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    path = seed(tmp_path, [(month_ago, [car("1"), car("2")])])
    warnings = reader(path).tools["analyze_models"]({}, 30)["warnings"]
    dites = [warning for warning in warnings if "jour(s)" in warning]
    assert len(dites) == 1
    assert "30 jour(s)" in dites[0]
    assert "vendues" in dites[0]


def test_un_corpus_frais_n_ajoute_aucun_avertissement(tmp_path):
    # L'autre côté du seuil, par le vrai chemin : une récolte d'aujourd'hui ne
    # gagne pas de phrase. Un avertissement qui parle toujours ne dirait rien.
    path = seed(tmp_path, [(datetime.now(timezone.utc), [car("1"), car("2")])])
    warnings = reader(path).tools["analyze_models"]({}, 30)["warnings"]
    assert [warning for warning in warnings if "jour(s)" in warning] == []
