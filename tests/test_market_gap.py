"""`offers` chiffre chaque annonce face aux voitures comparables du corpus.

Quatre clés plates par annonce : `market_median` et `market_n` décrivent LE
MARCHÉ (les comparables du corpus), `gap_eur` et `gap_pct` décrivent CETTE
annonce face à lui. Les deux moitiés sont indépendantes, et chaque test le
vérifie sur un corpus fabriqué dont les valeurs sont calculées à la main.

Le corpus est écrit par le `Store` directement : ces tests portent sur ce
qu'`offers` lit, et une récolte ajouterait ses propres compteurs. Aucun test ne
touche le réseau, et `offers` échoue si l'outil construit une source.

Chaque test nomme, quand ce n'est pas évident, la ligne dont le retrait le fait
rougir.
"""

from __future__ import annotations

import pytest

from mcp_car_compare import store as store_module
from mcp_car_compare import tools
from mcp_car_compare.models import Listing, SourceCoverage
from mcp_car_compare.store import Store

from test_tools import FakeMcp

# Six prix de comparables dont la médiane est (12 000 + 13 000) / 2 = 12 500.
SIX_PRIX = [10_000, 11_000, 12_000, 13_000, 14_000, 15_000]
MEDIANE_SIX = 12_500


def car(
    source_id: str,
    *,
    price: int | None = 12_000,
    year: int | None = 2021,
    make: str = "Renault",
    group: str = "clio",
    label: str = "Clio",
    market: str = "fr",
) -> Listing:
    return Listing(
        source="autoscout24",
        market=market,
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        make=make,
        group=group,
        group_label=label,
        year=year,
        km=48_000,
        price=price,
        fuel="diesel",
        gear="auto",
    )


def cohorte(prix: list[int], *, prefix: str = "c", **kwargs: object) -> list[Listing]:
    # `prefix` compte : deux cohortes qui partageraient « c0 » ne feraient
    # qu'une annonce, et le test mesurerait le contraire de ce qu'il écrit.
    return [
        car(f"{prefix}{index}", price=price, **kwargs)  # type: ignore[arg-type]
        for index, price in enumerate(prix)
    ]


def seed(tmp_path, cars: list[Listing], *, job_id: str = "job-1") -> None:
    """Dépose `cars` dans le corpus avec la ligne de récolte qu'`offers` exige."""
    with Store(tmp_path / "corpus.db") as store:
        store.add_page(job_id, list(cars))
        store.write_run(
            job_id,
            SourceCoverage(source="autoscout24", market="fr", read=len(cars), unique=len(cars)),
            criteria_hash="h",
            sort="age_desc",
        )


def offre(tmp_path, job_id: str = "job-1") -> dict[str, dict]:
    """Les annonces d'`offers`, par `source_id`."""
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=tmp_path / "corpus.db",
        # `offers` lit un corpus : construire une source serait déjà une faute.
        source_factory=lambda name: pytest.fail(f"source construite : {name}"),
        clock=lambda: 0.0,
    )
    page = mcp.tools["offers"](job_id, None, 100)
    return {item["source_id"]: item for item in page["items"]}


# --- écart calculé ---------------------------------------------------------------


def test_l_ecart_est_le_prix_de_l_annonce_moins_la_mediane_de_ses_comparables(tmp_path):
    # Six comparables à 10 000 … 15 000 → médiane 12 500. L'annonce est à
    # 13 000 : écart 500 €, soit 500 / 12 500 = 4,0 %. Le dernier comparable est
    # écrit en capitales : la cohorte le compte, donc la normalisation
    # `unaccent` est tenue par ce test (sans elle, médiane 11 500 et n = 5).
    comparables = cohorte(SIX_PRIX)
    comparables[-1] = car("c5", price=15_000, make="RENAULT", group="CLIO", label="CLIO")
    seed(tmp_path, [car("annonce", price=13_000), *comparables])

    annonce = offre(tmp_path)["annonce"]

    assert annonce["market_n"] == 6
    assert annonce["market_median"] == MEDIANE_SIX
    assert annonce["gap_eur"] == 500
    assert annonce["gap_pct"] == 4.0


def test_un_ecart_negatif_est_rendu_avec_son_signe_et_arrondi_au_dixieme(tmp_path):
    # Deux gammes, chacune avec sa propre annonce sonde et les mêmes six
    # comparables : 10 000 - 12 500 = -2 500, soit -20,0 % ; et
    # 11 100 - 12 500 = -1 400, soit -11,2 % — un arrondi à l'unité ou une
    # valeur absolue rougit ici.
    bmw = {"make": "BMW", "group": "serie 3", "label": "Série 3"}
    seed(
        tmp_path,
        [
            car("bas", price=10_000),
            *cohorte(SIX_PRIX, prefix="a"),
            car("onze", price=11_100, **bmw),
            *cohorte(SIX_PRIX, prefix="b", **bmw),
        ],
    )

    items = offre(tmp_path)

    assert (items["bas"]["market_median"], items["bas"]["gap_eur"]) == (MEDIANE_SIX, -2_500)
    assert items["bas"]["gap_pct"] == -20.0
    assert (items["onze"]["market_median"], items["onze"]["gap_eur"]) == (MEDIANE_SIX, -1_400)
    assert items["onze"]["gap_pct"] == -11.2


def test_une_annee_a_plus_d_un_an_ne_fait_pas_un_comparable(tmp_path):
    # Fenêtre ±1 an autour de 2021 : 2020 et 2022 entrent, 2019 et 2023
    # sortent. Les douze proches valent deux fois 10 000 … 20 000 : les deux
    # valeurs du milieu sont 14 000 et 16 000, médiane 15 000. Les deux intrus
    # valent 15 500 ; s'ils entraient, la cohorte en compterait quatorze et la
    # médiane serait 15 500 — c'est ce que le retrait de `BETWEEN` fait rougir.
    proches = cohorte([10_000, 12_000, 14_000, 16_000, 18_000, 20_000], year=2020)
    proches += cohorte([10_000, 12_000, 14_000, 16_000, 18_000, 20_000], prefix="d", year=2022)
    intrus = [car("vieux", price=15_500, year=2019), car("neuf", price=15_500, year=2023)]
    seed(tmp_path, [car("annonce", price=13_000, year=2021), *proches, *intrus])

    annonce = offre(tmp_path)["annonce"]

    assert annonce["market_n"] == 12
    assert annonce["market_median"] == 15_000
    assert annonce["gap_eur"] == -2_000


# --- abstention sous six comparables ---------------------------------------------


def test_cinq_comparables_s_abstiennent_et_six_rendent_une_mediane(tmp_path):
    # Le seuil est lu dans `models.MIN_COMPARABLES` : le déplacer d'un cran
    # retourne exactement ces deux attentes, donc ce test le tient. Les deux
    # gammes sont distinctes pour qu'aucune cohorte ne voie l'autre.
    cinq = [car("cinq", price=12_000)] + cohorte(SIX_PRIX[:5], prefix="a")
    six = [car("six", price=20_000, make="BMW", group="serie 3", label="Série 3")]
    six += cohorte(
        [20_000, 21_000, 22_000, 23_000, 24_000, 25_000],
        prefix="b",
        make="BMW",
        group="serie 3",
        label="Série 3",
    )
    seed(tmp_path, [*cinq, *six])

    items = offre(tmp_path)

    assert items["cinq"]["market_n"] == 5
    assert (
        items["cinq"]["market_median"],
        items["cinq"]["gap_eur"],
        items["cinq"]["gap_pct"],
    ) == (None, None, None)
    assert items["six"]["market_n"] == 6
    assert items["six"]["market_median"] == 22_500  # (22 000 + 23 000) / 2
    assert items["six"]["gap_eur"] == -2_500


# --- l'annonce n'est pas dans sa propre cohorte ----------------------------------


def test_une_annonce_n_est_pas_dans_sa_propre_cohorte(tmp_path):
    # L'annonce vaut 100 000. Avec l'exclusion, la cohorte est les six
    # comparables : n = 6, médiane 12 500, écart 87 500. Sans l'exclusion, elle
    # compterait sept prix — dont son propre 100 000 — donc n = 7 et une
    # médiane de 13 000 : c'est ce que le retrait du `NOT (...)` fait rougir.
    seed(tmp_path, [car("annonce", price=100_000), *cohorte(SIX_PRIX)])

    annonce = offre(tmp_path)["annonce"]

    assert annonce["market_n"] == 6
    assert annonce["market_median"] == MEDIANE_SIX
    assert annonce["gap_eur"] == 87_500


# --- prix hors plancher -----------------------------------------------------------


def test_un_prix_sous_le_plancher_n_entre_pas_dans_la_cohorte(tmp_path):
    # « Prix non communiqué » chez AutoScout24 : l'annonce à 1 € reste une
    # annonce du corpus, mais pas un prix. La cohorte garde six valeurs et la
    # médiane 12 500 ; sans le filtre `price_is_plausible`, elle en compterait
    # sept (médiane 12 000) — c'est ce que son retrait fait rougir.
    seed(tmp_path, [car("annonce", price=13_000), *cohorte(SIX_PRIX), car("bidon", price=1)])

    annonce = offre(tmp_path)["annonce"]

    assert annonce["market_n"] == 6
    assert annonce["market_median"] == MEDIANE_SIX


def test_un_prix_non_credible_chiffre_le_marche_mais_pas_l_ecart(tmp_path):
    # Les deux moitiés de la réponse ne dépendent pas l'une de l'autre : le
    # marché de cette annonce existe (six comparables, médiane 12 500), mais
    # son propre prix n'en est pas un, donc aucun écart — « -99,9 % » serait un
    # chiffre calculé et faux.
    seed(tmp_path, [car("bidon", price=1), *cohorte(SIX_PRIX)])

    item = offre(tmp_path)["bidon"]

    assert item["price_plausible"] is False
    assert (item["market_n"], item["market_median"]) == (6, MEDIANE_SIX)
    assert (item["gap_eur"], item["gap_pct"]) == (None, None)


# --- coût de la cohorte : l'index du schéma --------------------------------------


def index_names(store: Store) -> set[str]:
    return {row["name"] for row in store._conn.execute('PRAGMA index_list("listings")')}


def test_une_base_ecrite_sans_l_index_le_recoit_a_l_ouverture(tmp_path):
    # Le corpus d'un utilisateur existe déjà avant ce lot. `CREATE INDEX IF NOT
    # EXISTS` est rejoué par `executescript` à chaque ouverture, donc la base
    # ANCIENNE le reçoit aussi : sans ça, la première vraie base serait la
    # seule à payer le balayage complet de `listings` à chaque annonce.
    with Store(tmp_path / "corpus.db") as store:
        store.add_page("job-1", [car("ancienne"), *cohorte(SIX_PRIX)])
        store._conn.execute('DROP INDEX "idx_listings_cohort"')
        assert "idx_listings_cohort" not in index_names(store)

    with Store(tmp_path / "corpus.db") as store:
        assert "idx_listings_cohort" in index_names(store)
        (gap,) = store.market_gaps([car("ancienne")]).values()
        assert (gap.n, gap.median) == (6, MEDIANE_SIX)


def test_la_requete_de_cohorte_utilise_l_index(tmp_path):
    # Un chronomètre dépend de la machine, un plan d'exécution non. Ce test
    # tient la jointure entre l'index du schéma et le texte SQL réellement
    # exécuté : retirer l'index de `_SCHEMA`, changer son expression ou
    # renommer l'index fait retomber le plan sur `SCAN l` et rougit ici.
    with Store(tmp_path / "corpus.db") as store:
        store.add_page("job-1", [car("a")])
        plan = [
            row["detail"]
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN " + store_module._COHORT_SQL,
                (
                    "Renault",
                    "clio",
                    2020,
                    2022,
                    "autoscout24",
                    "fr",
                    "autoscout24",
                    "fr",
                    "absente",
                ),
            )
        ]

    assert any("USING INDEX idx_listings_cohort" in detail for detail in plan), plan



# --- la cohorte reste sur le marché de l'annonce ---------------------------------


def test_la_cohorte_ne_melange_pas_les_marches(tmp_path):
    # Mesuré le 2026-11-28 : une Golf française à 20 000 € s'affichait « +37,9 % »
    # au-dessus du marché parce que quatre Golf ALLEMANDES à 30 000 € entraient
    # dans sa médiane. Deux marchés ne portent ni la même TVA ni les mêmes frais.
    # Sans `AND l."market" = ?` dans `_COHORT_SQL`, ce test rougit : n=10 et
    # médiane 14 500 au lieu de n=6 et 12 500.
    francaises = cohorte(SIX_PRIX, prefix="fr")
    allemandes = [
        car(f"de{index}", price=prix, market="de")
        for index, prix in enumerate([30_000, 31_000, 32_000, 33_000])
    ]
    seed(tmp_path, [car("cible", price=20_000), *francaises, *allemandes])

    annonce = offre(tmp_path)["cible"]

    assert (annonce["market_n"], annonce["market_median"]) == (6, MEDIANE_SIX)
    assert (annonce["gap_eur"], annonce["gap_pct"]) == (7_500, 60.0)


def test_une_annonce_allemande_ne_se_chiffre_pas_contre_la_france(tmp_path):
    # Le symétrique, et c'est lui qui prouve que la dimension est bien portée par
    # l'annonce et pas par une constante : une annonce .de n'a que deux
    # comparables allemands, donc pas de référence — même si six comparables
    # français sont à côté. Sans le filtre, elle aurait une médiane française.
    # Les deux allemandes diffèrent de 1 000 € : deux voitures, pas une annoncée
    # deux fois — à prix identique elles porteraient la même clé de doublon et la
    # cohorte n'en compterait qu'une (`store._duplicate_key`), donc ce test ne
    # mesurerait plus le filtre de marché qu'il existe pour mesurer.
    francaises = cohorte(SIX_PRIX, prefix="fre")
    allemandes = [
        car(f"de{index}", price=30_000 + 1_000 * index, market="de") for index in range(2)
    ]
    seed(tmp_path, [car("cible", price=20_000, market="de"), *francaises, *allemandes])

    annonce = offre(tmp_path)["cible"]

    assert annonce["market_n"] == 2
    assert annonce["market_median"] is None
    assert annonce["gap_eur"] is None
