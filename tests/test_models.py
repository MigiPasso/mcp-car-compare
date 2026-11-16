"""Le contrat de `Page` : deux chiffres qui ne se confondent pas.

`rendered` est ce que la page a RENDU (doublons intra-page compris) et
`len(listings)` ce qu'elle porte après dédoublonnage. La récolte compare
`rendered` au compteur du site et lit `listings` pour remplir le corpus, donc un
`rendered` qui n'est pas un entier ne casse pas au bon endroit : il traverse la
construction, puis échoue sur une comparaison, loin de la source de l'erreur.
"""

from __future__ import annotations

import pytest

from mcp_car_compare.models import Criteria, Page, price_is_plausible


def test_rendered_vaut_par_defaut_ce_que_la_page_porte():
    # Une source qui ne dédoublonne pas n'a rien à déclarer : `rendered=None`
    # veut dire « la page a rendu ce qu'elle porte ».
    page = Page(listings=[])
    assert page.rendered == 0


def test_rendered_peut_depasser_les_annonces_portees():
    # Le cas qui justifie le champ : une page qui rend trois annonces dont une
    # en double porte deux annonces et un `rendered` de trois.
    page = Page(listings=[], rendered=3)
    assert (page.rendered, len(page.listings)) == (3, 0)


@pytest.mark.parametrize("rendered", ["x", "2", 2.5, True])
def test_un_rendered_non_entier_est_refuse_a_la_construction(rendered):
    # Un `rendered` textuel passait la construction en silence, puis se
    # comparait à `<= 0` dans la récolte : la panne surgissait loin de sa cause.
    # `True` est refusé aussi, comme partout où un entier est attendu dans ce
    # dépôt : un booléen compte pour 1 sans que personne ne l'ait voulu.
    with pytest.raises(TypeError, match="rendered"):
        Page(listings=[], rendered=rendered)


def test_un_rendered_inferieur_a_ce_que_la_page_porte_est_refuse():
    # La récolte en déduit `dropped_duplicates = rendered - len(listings)` : un
    # adaptateur qui sous-déclare rendrait ce compteur NÉGATIF et casserait en
    # silence l'identité `read == unique + dropped_duplicates + dropped_invalid`,
    # que deux docstrings présentent comme un contrat. Mieux vaut refuser ici.
    with pytest.raises(ValueError, match="sous-déclare"):
        Page(listings=[object(), object()], rendered=1)


# --- prix crédible -------------------------------------------------------------

def test_un_prix_a_un_euro_n_est_pas_un_prix():
    # AutoScout24 publie des annonces à 1 € pour dire « prix non communiqué »
    # (mesuré le 2026-11-16 : `priceRaw: 1`, `priceFormatted: "€ 1"`). Ce n'est
    # pas une erreur de parsing, c'est le site — mais ce n'est pas un prix.
    assert price_is_plausible(1) is False
    assert price_is_plausible(0) is False
    assert price_is_plausible(-500) is False
    assert price_is_plausible(None) is False


def test_le_seuil_de_credibilite_est_100_euros():
    # 99 et 100 sont écrits en dur À DESSEIN : citer `PRICE_FLOOR_EUR` ici
    # rendrait le test tautologique, puisque changer le seuil changerait
    # l'attendu avec lui.
    assert price_is_plausible(99) is False
    assert price_is_plausible(100) is True
    assert price_is_plausible(23000) is True


def test_le_tri_par_defaut_ne_sert_pas_la_boue_en_premier():
    # `price_asc` chez AutoScout24 commence par les annonces à 1 €, mesuré sur
    # la page réelle : le défaut ne doit donc pas être le prix croissant.
    assert Criteria().sort == "age_desc"
