"""Adaptateurs de source : chacun lit un site et rend des `Listing` du contrat.

Un adaptateur ne crée jamais son client HTTP : il le reçoit à la construction,
pour que les tests injectent un transport factice et qu'aucun d'eux ne touche
le réseau. Il lève `StructureChanged` quand la structure de la page n'est plus
celle qu'il sait lire, et `SourceError` quand la source n'a pas répondu ce
qu'il fallait — jamais de « 0 annonce » silencieux, qui se lirait comme un
marché désert.
"""

from .autoscout24 import Autoscout24

__all__ = ["Autoscout24"]
