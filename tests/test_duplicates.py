"""Une même voiture annoncée plusieurs fois : marquée, jamais fusionnée.

Mesuré le 2026-11-28 : deux annonces AutoScout24 portaient deux identifiants
différents pour la même voiture (15 900 €, 45 109 km, même vendeur). Le corpus
garde les DEUX lignes — deux vendeurs qui annoncent deux voitures identiques
restent deux opportunités — et `offers` les relie par `duplicate_of`.

Le corpus est écrit par le `Store` directement, comme dans `test_market_gap` :
ces tests portent sur ce qu'`offers` rend, pas sur une récolte. Aucun test ne
touche le réseau.

Chaque test nomme la ligne dont le retrait le fait rougir.
"""

from __future__ import annotations

from test_market_gap import MEDIANE_SIX, SIX_PRIX, car, cohorte, offre, seed


# --- le marquage -----------------------------------------------------------------


def test_deux_annonces_identiques_se_citent_l_une_l_autre(tmp_path):
    # Le cas mesuré, réduit à l'essentiel : même modèle, même année, même
    # kilométrage, même prix, deux identifiants. Le marquage est MUTUEL — sans
    # le `NOT (...)` du seau, chaque ligne se trouverait elle-même et se
    # citerait ; sans la comparaison de clé, aucune ne verrait l'autre.
    seed(tmp_path, [car("a1", price=15_900), car("a2", price=15_900)])

    items = offre(tmp_path)

    assert items["a1"]["duplicate_of"] == "a2"
    assert items["a2"]["duplicate_of"] == "a1"


def test_deux_annonces_qui_ne_different_que_par_le_prix_ne_sont_pas_doublons(tmp_path):
    # Le prix est dans la clé : deux annonces du même modèle, même année, même
    # kilométrage, à 500 € d'écart, restent deux voitures — peut-être la même
    # d'ailleurs, mais le corpus ne le sait pas, et marquer l'une comme doublon
    # de l'autre ferait disparaître une comparaison de prix. Retirer `price` de
    # `_duplicate_key` fait rougir ce test.
    seed(tmp_path, [car("a1", price=15_900), car("a2", price=16_400)])

    items = offre(tmp_path)

    assert items["a1"]["duplicate_of"] is None
    assert items["a2"]["duplicate_of"] is None


def test_une_annonce_seule_n_a_pas_de_doublon(tmp_path):
    # `null`, pas `""` ni sa propre `source_id` : une annonce qui se citerait
    # elle-même dirait qu'elle existe ailleurs. C'est ce test qui rougit si
    # l'exclusion de l'annonce dans son propre seau disparaît.
    seed(tmp_path, [car("a1")])

    items = offre(tmp_path)

    assert items["a1"]["duplicate_of"] is None


def test_une_annonce_sans_kilometrage_n_est_pas_marquee_doublon(tmp_path):
    # Deux annonces du même modèle, même année, même prix, kilométrage INCONNU
    # des deux côtés : la clé est incomplète, donc aucune n'est marquée. `NULL`
    # n'est pas une valeur égale — les rabattre l'une sur l'autre écarterait un
    # prix d'une médiane sans aucune raison. Retirer la garde `None` de
    # `_duplicate_key` fait rougir ce test.
    def sans_km(source_id: str):
        annonce = car(source_id, price=15_900)
        annonce.km = None
        return annonce

    seed(tmp_path, [sans_km("a1"), sans_km("a2")])

    items = offre(tmp_path)

    assert items["a1"]["duplicate_of"] is None
    assert items["a2"]["duplicate_of"] is None


def test_trois_annonces_identiques_citent_la_premiere_des_autres(tmp_path):
    # Un champ ne porte qu'un identifiant : au-delà du couple, chacune cite la
    # première des autres dans l'ordre `(source, market, source_id)`, et ce
    # choix est stable d'un appel à l'autre. Rendre la DERNIÈRE autre (ou
    # inverser l'`ORDER BY` du seau) fait rougir les trois assertions.
    seed(
        tmp_path,
        [car("a1", price=15_900), car("a2", price=15_900), car("a3", price=15_900)],
    )

    items = offre(tmp_path)

    assert items["a1"]["duplicate_of"] == "a2"
    assert items["a2"]["duplicate_of"] == "a1"
    assert items["a3"]["duplicate_of"] == "a1"


# --- la cohorte ne compte pas deux fois la même voiture --------------------------


def test_une_voiture_annoncee_deux_fois_ne_pese_qu_une_fois_dans_la_mediane(tmp_path):
    # Le doublon est DANS la cohorte : six voitures distinctes à 10 000 … 15 000,
    # dont la première annoncée deux fois. L'annonce sondée est à 20 000, donc
    # hors de cette série. Sans la déduplication, la cohorte compterait sept prix
    # — [10 000, 10 000, 11 000, 12 000, 13 000, 14 000, 15 000], médiane
    # 12 000, n = 7 — au lieu de six, médiane 12 500 : le doublon déplacerait la
    # référence qui sert à juger toutes les annonces du modèle.
    seed(tmp_path, [car("sonde", price=20_000), *cohorte(SIX_PRIX), car("c0bis", price=10_000)])

    items = offre(tmp_path)

    assert (items["c0"]["duplicate_of"], items["c0bis"]["duplicate_of"]) == ("c0bis", "c0")
    assert (items["sonde"]["market_n"], items["sonde"]["market_median"]) == (6, MEDIANE_SIX)


def test_deux_marches_ne_font_pas_deux_doublons(tmp_path):
    # Une annonce .fr et une .de au même modèle/année/km/prix ne désignent pas la
    # même voiture : deux marchés ne publient pas des prix comparables (TVA,
    # frais), donc un prix identique est une coïncidence, pas une identité.
    # C'est la même règle que la cohorte applique déjà. Sans `market` dans la
    # clé, les deux se marquent mutuellement — mesuré par un vérificateur.
    fr = car("fr-1", price=15_900, market="fr")
    de = car("de-1", price=15_900, market="de")
    seed(tmp_path, [fr, de])

    offres = offre(tmp_path)

    assert offres["fr-1"]["duplicate_of"] is None
    assert offres["de-1"]["duplicate_of"] is None
