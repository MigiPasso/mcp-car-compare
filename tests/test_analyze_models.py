"""`analyze_models` : des distributions par modèle, et une abstention qui se dit.

Le corpus est écrit par le `Store` directement : ces tests portent sur ce que
l'analyse LIT, et une récolte ajouterait ses propres compteurs au corpus (un
adaptateur factice rend la même page pour chaque marché demandé). Aucun test ne
touche le réseau, et `analyze` échoue si l'outil construit une source.

Chaque test doit pouvoir rougir : le commentaire dit, quand ce n'est pas
évident, quelle ligne retirée le fait échouer.
"""

from __future__ import annotations

import pytest

from mcp_car_compare import tools
from mcp_car_compare.models import PRICE_FLOOR_EUR, Listing, SourceCoverage
from mcp_car_compare.store import ListingFilter, Store

from test_tools import FakeMcp


def car(
    source_id: str,
    *,
    make: str = "Renault",
    group: str = "clio",
    label: str = "Clio",
    price: int | None = 11_000,
    year: int | None = 2021,
    km: int | None = 48_000,
    fuel: str = "diesel",
    gear: str = "auto",
    market: str = "fr",
) -> Listing:
    return Listing(
        source="autoscout24",
        market=market,
        source_id=source_id,
        url=f"https://example.test/{market}/{source_id}",
        make=make,
        group=group,
        group_label=label,
        year=year,
        km=km,
        price=price,
        fuel=fuel,
        gear=gear,
    )


def harvest_run(
    market: str = "fr", *, read: int = 0, unique: int = 0, source: str = "autoscout24"
) -> SourceCoverage:
    return SourceCoverage(source=source, market=market, read=read, unique=unique)


def seed(
    tmp_path,
    cars: list[Listing],
    *,
    job_id: str = "job-1",
    runs: tuple[SourceCoverage, ...] = (),
) -> None:
    """Dépose `cars` dans le corpus, et une ligne de récolte par `runs`."""
    with Store(tmp_path / "corpus.db") as store:
        store.add_page(job_id, list(cars))
        for coverage in runs:
            store.write_run(job_id, coverage, criteria_hash="h", sort="age_desc")


def analyze(tmp_path, criteria: dict | None = None, limit: int = 30) -> dict:
    mcp = FakeMcp()
    tools.register(
        mcp,
        db_path=tmp_path / "corpus.db",
        # Une analyse ne récolte rien : construire une source serait déjà une
        # faute, donc la fabrique échoue au lieu de rendre une source factice.
        source_factory=lambda name: pytest.fail(f"source construite : {name}"),
        clock=lambda: 0.0,
    )
    return mcp.tools["analyze_models"](criteria or {}, limit)


def prices_to_cars(prices: list[int], *, prefix: str = "p", **kwargs: object) -> list[Listing]:
    # `prefix` compte : deux groupes qui partageraient « p0 » ne feraient qu'une
    # annonce, et le test mesurerait le contraire de ce qu'il écrit.
    return [
        car(f"{prefix}{index}", price=price, **kwargs)  # type: ignore[arg-type]
        for index, price in enumerate(prices)
    ]


# --- D3 : un prix de 1 € n'entre pas dans une médiane ---------------------------


def test_un_prix_a_un_euro_est_compte_dans_n_mais_pas_dans_la_mediane(tmp_path):
    # D3 : « prix non communiqué » chez AutoScout24. L'annonce existe (elle est
    # dans `n`), son prix n'est pas un prix (il n'est pas dans la série).
    # Retirer le plancher de la série de prix donnerait ici n_priced=7 et une
    # médiane de 11 000 au lieu de 11 500.
    seed(tmp_path, prices_to_cars([9_000, 10_000, 11_000, 12_000, 13_000, 14_000]) + [car("bidon", price=1)])
    (group,) = analyze(tmp_path)["groups"]
    assert (group["n"], group["n_priced"]) == (7, 6)
    # série crédible : rang (6-1)*0,5 = 2,5 → (11 000 + 12 000) / 2
    assert group["price_median"] == 11_500


def test_le_plancher_de_creditibilite_est_un_parametre_de_la_requete(tmp_path):
    # D2 : le plancher n'est écrit ni dans le SQL ni ailleurs, il est passé.
    # Une copie du nombre 100 dans la requête rendrait les deux comptes égaux.
    seed(tmp_path, prices_to_cars([500, 600, 700, 800, 900, 1_000]))
    with Store(tmp_path / "corpus.db") as store:
        bas, _ = store.model_groups(ListingFilter(), price_floor=100, limit=30)
        haut, _ = store.model_groups(ListingFilter(), price_floor=750, limit=30)
    assert (bas[0].n_priced, haut[0].n_priced) == (6, 3)


def test_n_priced_est_exactement_la_longueur_de_la_serie_rendue(tmp_path):
    # Deux chiffres qui doivent dire la même chose : le compte agrégé en SQL et
    # la série sur laquelle les quantiles sont calculés. S'ils divergent, une
    # médiane peut etre calculée sur six prix et annoncée pour cinq.
    seed(tmp_path, prices_to_cars([1, 9_000, 10_000, None, 12_000]))
    with Store(tmp_path / "corpus.db") as store:
        groups, _ = store.model_groups(ListingFilter(), price_floor=PRICE_FLOOR_EUR, limit=30)
        series = store.group_prices(
            ListingFilter(),
            [(group.make, group.group) for group in groups],
            price_floor=PRICE_FLOOR_EUR,
        )
    assert [(group.n, group.n_priced, len(series[(group.make, group.group)])) for group in groups] == [
        (5, 3, 3)
    ]


# --- D4 : abstention sous six comparables --------------------------------------


def test_cinq_prix_credibles_s_abstiennent_et_six_rendent_une_mediane(tmp_path):
    # Le seuil est lu dans models.MIN_COMPARABLES : le déplacer d'un cran
    # retourne exactement ces deux attentes, donc ce test le tient.
    cinq = prices_to_cars([10_000, 11_000, 12_000, 13_000, 14_000]) + [car("bidon", price=1)]
    six = prices_to_cars(
        [20_000, 21_000, 22_000, 23_000, 24_000, 25_000],
        prefix="m",
        make="BMW",
        group="serie 3",
        label="Série 3",
    )
    seed(tmp_path, cinq + six)
    result = analyze(tmp_path)
    by_group = {group["group"]: group for group in result["groups"]}
    clio, serie = by_group["clio"], by_group["serie 3"]
    # Le groupe sous le seuil apparaît quand même : n=6 annonces, 5 prix.
    assert (clio["n"], clio["n_priced"]) == (6, 5)
    assert (clio["price_median"], clio["price_p25"], clio["price_p75"]) == (None, None, None)
    assert serie["n_priced"] == 6
    assert serie["price_median"] == 22_500  # rang 2,5 sur [20 000 … 25 000]
    assert any(
        "Clio" in warning and "5" in warning and "aucune médiane" in warning
        for warning in result["warnings"]
    )


# --- D5 : quantiles par interpolation linéaire ---------------------------------


def test_les_quantiles_sont_interpoles_lineairement_sur_huit_prix(tmp_path):
    # D5, calculé à la main sur la série triée (n = 8, rang (n-1)*q) :
    #   p25     : rang 1,75 → 9 500 + 0,75 * 500        =  9 875
    #   médiane : rang 3,5  → (11 000 + 12 000) / 2     = 11 500
    #   p75     : rang 5,25 → 13 000 + 0,25 * 1 000     = 13 250
    # Prendre la valeur du rang bas (9 500 / 11 000 / 13 000) ou arrondir le
    # rang (10 000 / 12 000 / 13 000) rend ce test rouge.
    seed(tmp_path, prices_to_cars([9_000, 9_500, 10_000, 11_000, 12_000, 13_000, 14_000, 15_000]))
    (group,) = analyze(tmp_path)["groups"]
    assert (group["price_p25"], group["price_median"], group["price_p75"]) == (9_875, 11_500, 13_250)


def test_les_medianes_d_annee_et_de_km_ignorent_les_valeurs_absentes(tmp_path):
    # Un effectif pair prend la moyenne des deux valeurs du milieu — la même
    # définition que les prix. Un NULL compté comme zéro ferait 20 000 ici, et
    # une année manquante écraserait la médiane vers le bas.
    seed(
        tmp_path,
        [
            car("c0", year=2016, km=10_000),
            car("c1", year=2018, km=20_000),
            car("c2", year=2020, km=30_000),
            car("c3", year=2022, km=40_000),
            car("c4", year=2024, km=None),
        ],
    )
    (group,) = analyze(tmp_path)["groups"]
    assert group["year_median"] == 2020  # effectif impair : la valeur du milieu
    assert group["km_median"] == 25_000  # effectif pair : (20 000 + 30 000) / 2


# --- D1 : le groupe est (make, group), pas le libellé --------------------------


def test_deux_libelles_de_la_meme_gamme_font_un_groupe_et_deux_marques_en_font_deux(tmp_path):
    # `group` est la clé comparable entre marchés ; `group_label` ne l'est pas.
    # Grouper sur le libellé séparerait « Série 3 » et « 3er » — la paire qui
    # doit se rejoindre. À l'inverse, la même gamme chez deux marques doit
    # rester deux groupes : c'est la paire qui ne doit PAS se rejoindre.
    seed(
        tmp_path,
        [
            car("fr1", make="BMW", group="serie 3", label="Série 3", price=20_000),
            car("de1", make="BMW", group="serie 3", label="3er", price=22_000, market="de"),
            car("au1", make="Audi", group="serie 3", label="Série 3", price=30_000),
        ],
    )
    result = analyze(tmp_path)
    by_make = {group["make"]: group for group in result["groups"]}
    assert (by_make["BMW"]["group"], by_make["BMW"]["n"]) == ("serie 3", 2)
    assert by_make["Audi"]["n"] == 1
    assert result["groups_total"] == 2


# --- D6 : ordre neutre, aucun classement ---------------------------------------


def test_les_groupes_sortent_par_n_decroissant_puis_par_make_et_group(tmp_path):
    # Un ordre neutre et cité : `n` d'abord, puis `make` et `group`. Trier par
    # marque d'abord mettrait Audi en tête et rendrait ce test rouge ; c'est ce
    # qui prouve que l'ordre ne dépend pas d'un score.
    seed(
        tmp_path,
        [
            car("c1", make="Renault", group="clio", label="Clio"),
            car("c2", make="Renault", group="clio", label="Clio"),
            car("c3", make="Renault", group="clio", label="Clio"),
            car("b1", make="BMW", group="serie 1", label="Série 1"),
            car("b2", make="BMW", group="serie 1", label="Série 1"),
            car("a1", make="Audi", group="a1", label="A1"),
            car("a2", make="Audi", group="a1", label="A1"),
        ],
    )
    assert [
        (group["make"], group["group"]) for group in analyze(tmp_path)["groups"]
    ] == [("Renault", "clio"), ("Audi", "a1"), ("BMW", "serie 1")]


# --- `groups_total` et `limit` --------------------------------------------------


def test_groups_total_compte_les_groupes_que_limit_ne_rend_pas(tmp_path):
    # « 30 groupes » et « 30 sur 87 » ne disent pas la même chose. Rendre
    # len(groups) comme total ferait lire une coupure comme un marché vide.
    cars: list[Listing] = []
    for rank, name in enumerate(["a", "b", "c", "d", "e"]):
        cars.extend(
            car(f"{name}{index}", group=name, label=name.upper()) for index in range(rank + 1)
        )
    seed(tmp_path, cars)
    result = analyze(tmp_path, limit=2)
    assert [group["group"] for group in result["groups"]] == ["e", "d"]
    assert result["groups_total"] == 5


@pytest.mark.parametrize("limit", [0, -1, 101, True, "30"])
def test_limit_hors_bornes_est_refuse(tmp_path, limit):
    seed(tmp_path, [car("c1")])
    with pytest.raises(ValueError, match="limit"):
        analyze(tmp_path, {}, limit)


# --- le corpus entier, et ses critères -----------------------------------------


def test_l_analyse_porte_sur_tout_le_corpus_pas_sur_une_recolte(tmp_path):
    # D7 : sinon l'analyse dépendrait de l'ordre des appels. Se restreindre à
    # la dernière récolte perdrait Renault, récolté avant.
    seed(tmp_path, [car("r1", make="Renault")], job_id="job-1", runs=(harvest_run(read=1, unique=1),))
    seed(
        tmp_path,
        [car("b1", make="BMW", group="serie 3", label="Série 3")],
        job_id="job-2",
        runs=(harvest_run(read=1, unique=1),),
    )
    result = analyze(tmp_path)
    assert sorted(group["make"] for group in result["groups"]) == ["BMW", "Renault"]
    assert result["coverage"]["runs"] == 2


def test_les_criteres_rendus_sont_exactement_ceux_qui_ont_filtre(tmp_path):
    # Un critère appliqué mais absent de `criteria` serait un filtre invisible ;
    # un critère annoncé mais non appliqué serait un mensonge. Le premier
    # contrôle porte aussi sur l'absence de défaut : `harvest` prend `fr` par
    # défaut, une analyse ne prend RIEN par défaut.
    seed(
        tmp_path,
        [
            car("r1", make="Renault"),
            car("b1", make="BMW", group="serie 3", label="Série 3", market="de", price=30_000),
        ],
        runs=(harvest_run("fr", read=1, unique=1), harvest_run("de", read=1, unique=1)),
    )
    libre = analyze(tmp_path)
    assert libre["criteria"] == {}
    assert libre["groups_total"] == 2  # les deux marchés, sans filtre par défaut
    restreint = analyze(tmp_path, {"make": "renault", "price_max": 12_000})
    assert restreint["criteria"] == {"make": "renault", "price_max": 12_000}
    assert [group["make"] for group in restreint["groups"]] == ["Renault"]


@pytest.mark.parametrize(
    ("criteria", "attendu"),
    [
        ({"year_min": 2022}, ["serie 3"]),
        ({"year_max": 2016}, ["clio"]),
        ({"km_max": 20_000}, ["clio"]),
        ({"price_min": 30_000}, ["serie 3"]),
        ({"price_max": 12_000}, ["clio"]),
        ({"fuel": "essence"}, ["serie 3"]),
        ({"gear": "manuelle"}, ["clio"]),
        ({"markets": ["de"]}, ["serie 3"]),
        ({"sources": ["autoscout24"]}, ["serie 3", "clio"]),
        ({"model": "Clio"}, ["clio"]),
        ({"model": "clio"}, ["clio"]),  # la clé normalisée, comme le libellé
        ({"make": "BMW"}, ["serie 3"]),
        ({"make": "bmw"}, ["serie 3"]),  # la casse ne compte pas
    ],
)
def test_chaque_critere_restreint_le_corpus_et_est_annonce(tmp_path, criteria, attendu):
    # Un critère accepté mais silencieusement inappliqué passerait inaperçu
    # partout ailleurs : ce test le rend visible, un critère à la fois.
    seed(
        tmp_path,
        [
            car(
                "r1",
                make="Renault",
                group="clio",
                label="Clio",
                year=2015,
                km=12_000,
                price=9_000,
                fuel="diesel",
                gear="manuelle",
            ),
            car(
                "b1",
                make="BMW",
                group="serie 3",
                label="Série 3",
                year=2023,
                km=60_000,
                price=35_000,
                fuel="essence",
                gear="auto",
                market="de",
            ),
        ],
        runs=(harvest_run("fr", read=1, unique=1), harvest_run("de", read=1, unique=1)),
    )
    result = analyze(tmp_path, criteria)
    assert [group["group"] for group in result["groups"]] == attendu
    assert result["criteria"] == criteria
    assert not [w for w in result["warnings"] if "aucune annonce ne correspond" in w]


def test_un_filtre_qui_ne_correspond_a_rien_est_nomme_avec_les_valeurs_du_corpus(tmp_path):
    # `make` et `model` sont des libellés, ici comme à la récolte : un
    # identifiant numérique n'est plus un vocabulaire d'appel, c'est un filtre
    # qui ne correspond à rien, et il est nommé comme tel plutôt que rendu en
    # zéro muet. Aucune heuristique ne devine le vocabulaire : `model="500"`
    # reste un libellé légitime (une Fiat 500).
    seed(tmp_path, [car("r1", make="Renault"), car("b1", make="BMW", group="serie 3", label="Série 3")])
    result = analyze(tmp_path, {"make": "13"})
    assert (result["groups"], result["groups_total"]) == ([], 0)
    (warning,) = [warning for warning in result["warnings"] if "make=" in warning]
    assert "make='13'" in warning
    assert "BMW" in warning and "Renault" in warning


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ({"sort": "price_asc"}, "récolte"),
        ({"pages_max": 2}, "récolte"),
        ({"kilometrage": 1_000}, "inconnu"),
        ({"fuel": "gpl"}, "carburant inconnu"),
        ({"gear": "semi"}, "boîte inconnue"),
        ({"markets": ["it"]}, "marché hors vocabulaire"),
        ({"sources": ["leboncoin"]}, "source hors vocabulaire"),
        ({"year_min": "2020"}, "year_min"),
    ],
)
def test_analyze_refuse_les_criteres_qui_ne_filtrent_pas_une_analyse(tmp_path, criteria, message):
    # `sort` et `pages_max` gouvernent une récolte : les accepter sans les
    # appliquer serait un critère que l'appelant croit avoir posé.
    seed(tmp_path, [car("r1")])
    with pytest.raises(ValueError, match=message):
        analyze(tmp_path, criteria)


# --- couverture ----------------------------------------------------------------


def test_la_couverture_decrit_le_corpus_entier_meme_quand_un_filtre_est_applique(tmp_path):
    # Un corpus filtré sur `.fr` garde une couverture qui montre `.de` : les
    # compteurs de récolte n'ont rien à voir avec les critères, et les filtrer
    # laisserait croire que la couverture a changé parce qu'on a filtré.
    seed(
        tmp_path,
        [car("r1", make="Renault"), car("b1", make="BMW", group="serie 3", label="Série 3", market="de")],
        runs=(harvest_run("fr", read=1, unique=1), harvest_run("de", read=1, unique=1)),
    )
    result = analyze(tmp_path, {"markets": ["fr"]})
    assert [group["make"] for group in result["groups"]] == ["Renault"]
    assert result["coverage"]["listings_distinct"] == 2
    assert [
        (row["market"], row["listings_in_corpus"]) for row in result["coverage"]["sources"]
    ] == [
        ("de", 1),
        ("fr", 1),
    ]


def test_la_couverture_par_paire_porte_la_derniere_recolte_ecrite(tmp_path):
    # Deux récoltes de la même paire : les compteurs de récolte sont ceux de la
    # dernière écrite (jamais la somme : 4 serait une couverture double). Les
    # annonces, elles, se comptent dans le corpus ENTIER de la paire — les huit
    # clés, sans `job_id`, sont les mêmes que celles de `offers`.
    seed(tmp_path, [car("r1"), car("r2")], job_id="job-1", runs=(harvest_run(read=2, unique=2),))
    seed(tmp_path, [car("r1"), car("r3")], job_id="job-2", runs=(harvest_run(read=2, unique=2),))
    coverage = analyze(tmp_path)["coverage"]
    assert coverage["runs"] == 2
    assert coverage["listings_distinct"] == 3
    (entry,) = coverage["sources"]
    assert set(entry) == {
        "source",
        "market",
        "listings_in_corpus",
        "status",
        "announced_total",
        "read",
        "unique",
        "truncated",
    }
    assert (entry["read"], entry["unique"], entry["listings_in_corpus"]) == (2, 2, 3)


# --- un corpus vide --------------------------------------------------------------


def test_un_corpus_vide_rend_des_zeros_et_le_dit(tmp_path):
    # Ni exception (une division par un effectif nul), ni bloc `coverage`
    # absent : des zéros, et une phrase qui les explique.
    result = analyze(tmp_path)
    assert (result["groups"], result["groups_total"]) == ([], 0)
    assert result["coverage"] == {"listings_distinct": 0, "runs": 0, "sources": []}
    assert result["criteria"] == {}
    assert not [w for w in result["warnings"] if "aucune annonce ne correspond" in w]
    assert any("aucun groupe" in warning for warning in result["warnings"])


def test_un_corpus_vide_dit_aussi_que_le_filtre_ne_correspond_a_rien(tmp_path):
    # Le corpus vide n'est pas la seule explication d'un zéro : ici les deux
    # sont dites, et la seconde nomme le champ sans valeur présente.
    result = analyze(tmp_path, {"make": "Renault"})
    (warning,) = [warning for warning in result["warnings"] if "make=" in warning]
    assert "aucune valeur" in warning


def test_une_variable_de_recolte_hors_bornes_n_empeche_pas_de_lire_le_corpus(tmp_path, monkeypatch):
    # `MCC_PAGES_MAX` hors bornes fait lever `load_config()`, et c'est voulu pour
    # `harvest` : c'est un plafond de récolte. Une analyse ne règle aucune page,
    # donc hériter de ce refus rendrait un corpus déjà écrit illisible à cause
    # d'une variable qui ne la concerne pas — le même piège que `offers`.
    monkeypatch.setenv("MCC_DB", str(tmp_path / "from-env.db"))
    with Store(tmp_path / "from-env.db") as store:
        store.add_page("job-1", [car("r1")])
    monkeypatch.setenv("MCC_PAGES_MAX", "999")
    mcp = FakeMcp()
    tools.register(
        mcp,
        source_factory=lambda name: pytest.fail(f"source construite : {name}"),
        clock=lambda: 0.0,
    )
    assert [group["group"] for group in mcp.tools["analyze_models"]({}, 30)["groups"]] == ["clio"]


def test_une_marque_accentuee_se_trouve_quelle_que_soit_la_casse(tmp_path):
    # Le `LOWER` de SQLite n'abaisse que l'ASCII : mesuré,
    # `LOWER('CITROËN')` vaut `citroËn` et ne matche jamais `Citroën`. Sur un
    # marché français, ce sont exactement les marques qu'on cherche qui
    # échouaient — et la docstring affirmait le contraire. La comparaison
    # passe donc par `unaccent`, qui retire en plus les accents.
    seed(tmp_path, [car("c1", make="Citroën", group="c3", label="C3")])
    for saisie in ("Citroën", "CITROËN", "citroen", "CITROEN"):
        result = analyze(tmp_path, {"make": saisie})
        assert [g["make"] for g in result["groups"]] == ["Citroën"], saisie
        assert not [w for w in result["warnings"] if "aucune annonce ne correspond" in w]


def test_les_deux_outils_de_lecture_rendent_la_meme_forme_de_couverture(tmp_path):
    # Le trou que le vérificateur a trouvé : `offers` et `analyze_models`
    # décrivent tous deux « le corpus », et leurs 8 clés étaient assertées par
    # deux listes écrites séparément. Une 9e clé ajoutée d'un seul côté serait
    # passée inaperçue — c'est exactement comme ça que la couverture à deux
    # vitesses est apparue. Ce test tient la jointure au lieu de la supposer.
    seed(
        tmp_path,
        [car("r1"), car("r2")],
        job_id="job-1",
        runs=(harvest_run(read=2, unique=2),),
    )
    mcp = FakeMcp()
    tools.register(mcp, db_path=tmp_path / "corpus.db", clock=lambda: 0.0)

    via_analyze = mcp.tools["analyze_models"]({}, 30)["coverage"]["sources"][0]
    via_offers = mcp.tools["offers"]("job-1", None, 25)["coverage"]["sources"][0]

    assert set(via_analyze) == set(via_offers), (
        "les deux outils de lecture doivent décrire la couverture avec les mêmes clés"
    )
    assert len(via_analyze) == 8
