"""Vocabulaire du site vers le vocabulaire du contrat.

Porté fidèlement de `ICH/market/autoscout/client.go` (tables `fuelLabels`,
`gearLabels`, `sellerLabels` et la normalisation `normalize` de `trim.go`), avec
les fréquences mesurées là-bas en commentaire : ce sont elles qui prouvent
qu'une clé sert, et une clé oubliée rend « inconnu » sur des annonces réelles
sans que rien ne le signale.

Un libellé absent des tables vaut `""` (inconnu) : on n'invente pas une boîte.
Le site laisse deux libellés volontairement non traduits — « Semi-automatique »
et « Halbautomatik » — parce qu'il ne dit pas s'il s'agit d'une boîte robotisée
à embrayage (donc une manuelle pour l'usage visé) ou d'une automatique.
"""

from __future__ import annotations

# accents ramène les lettres accentuées du français et de l'allemand à leur
# forme nue. Le ß devient « ss », comme le veut la translittération usuelle :
# sans cela « weiss » et « weiß » seraient deux mots différents.
_ACCENTS = {
    "à": "a", "â": "a", "ä": "a", "á": "a",
    "ç": "c",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "î": "i", "ï": "i", "í": "i",
    "ô": "o", "ö": "o", "ó": "o",
    "ù": "u", "û": "u", "ü": "u", "ú": "u",
    "ß": "ss",
    "ñ": "n",
    "œ": "oe",
}


def strip_accents(text: str) -> str:
    """Supprime les accents d'un libellé, sans toucher au reste.

    C'est la seule table d'accents du paquet : la normalisation des libellés et
    celle des gammes s'en servent toutes les deux, pour qu'aucune ne compare des
    libellés différemment.
    """
    return "".join(_ACCENTS.get(char, char) for char in text)


def normalize_label(label: str) -> str:
    """Ramène un libellé à des mots nus : minuscules, sans accents, toute
    ponctuation ou symbole remplacé par une espace, espaces multiples réduits.

    Mesuré : c'est ce qui fait se rejoindre « Électrique/Essence » et
    « elektro benzin », deux écritures du même carburant selon le marché.
    """
    bare = strip_accents(label.lower())
    words = "".join(char if ("a" <= char <= "z" or "0" <= char <= "9") else " " for char in bare)
    return " ".join(words.split())


# FUEL_LABELS traduit le libellé carburant du site vers le vocabulaire du
# contrat. Les clés sont les libellés réellement rencontrés sur 1 200 annonces
# françaises et allemandes : « Diesel » 519, « Benzin » 333, « Essence » 280,
# « Elektro/Benzin » 28, « Électrique/Essence » 22, « Electrique » 6,
# « Elektro/Diesel » 4, « Elektro » 3, « Erdgas (CNG) » 2,
# « Électrique/Diesel » 1, « Autres » 1.
#
# « hybride » est le libellé des sites français (Autohero, La Centrale) là où
# AutoScout24 écrit « Elektro/Benzin » ; « hybrides », au pluriel, est la puce
# de La Centrale (8 cartes sur 24 au banc du 2026-11-12). Sans ces clés, des
# annonces réelles rendraient « inconnu » alors que le carburant est écrit.
FUEL_LABELS = {
    "diesel": "diesel",
    "essence": "essence",
    "benzin": "essence",
    "electrique": "electrique",
    "elektro": "electrique",
    "hybride": "hybride",
    "hybrides": "hybride",
    "electrique essence": "hybride",
    "elektro benzin": "hybride",
    "electrique diesel": "hybride",
    "elektro diesel": "hybride",
}

# GEAR_LABELS traduit la boîte. Mesuré : « Boîte automatique » 432,
# « Automatik » 394, « Schaltgetriebe » 203, « Boîte manuelle » 165, et une
# seule « Semi-automatique » / « Halbautomatik ».
#
# « automatique » seule est la puce des sites français (Autohero affiche
# « Automatique », La Centrale « Boîte automatique ») ; « Auto » est celle de La
# Centrale, qui tronque le mot (22 cartes sur 24). Ce libellé n'a sa place que
# dans cette table : « Auto » y désigne la boîte, pas le carburant.
#
# « semi-automatique » et « halbautomatik » ne sont PAS dans la table : le site
# ne dit pas si c'est une manuelle robotisée ou une automatique, donc `gear`
# vaut "" (inconnu) plutôt qu'une boîte devinée.
GEAR_LABELS = {
    "boite automatique": "auto",
    "automatik": "auto",
    "automatique": "auto",
    "auto": "auto",
    "boite manuelle": "manuelle",
    "schaltgetriebe": "manuelle",
    "manuelle": "manuelle",
}

# SELLER_TYPES traduit le type de vendeur. Ce n'est pas un libellé traduit mais
# une énumération du site, donc comparée telle quelle, sans normalisation.
# Mesuré : « Dealer » 1 125, « PrivateSeller » 75.
SELLER_TYPES = {
    "Dealer": "pro",
    "PrivateSeller": "particulier",
}


def fuel_from(label: str) -> str:
    """Carburant du contrat, ou `""` quand le libellé n'est pas connu.

    « Erdgas (CNG) » n'a PAS d'équivalent : le vocabulaire figé du contrat
    (`models.FUELS`) ne comporte pas de gaz, et le ranger sous « hybride » ou
    « essence » serait un carburant inventé. Un trou déclaré vaut mieux.
    """
    return FUEL_LABELS.get(normalize_label(label), "")


def gear_from(label: str) -> str:
    """Boîte du contrat, ou `""` quand le libellé n'est pas connu."""
    return GEAR_LABELS.get(normalize_label(label), "")


def seller_from(label: str) -> str:
    """Type de vendeur du contrat, ou `""` quand le site en donne un inconnu."""
    return SELLER_TYPES.get(label, "")
