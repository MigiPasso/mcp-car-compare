"""Configuration du serveur, entièrement par variables d'environnement.

Préfixe `MCC_` : dans un `docker run -e`, une variable sans préfixe peut
heurter celle d'une autre image ou du shell de l'utilisateur, et le serveur
lirait alors une valeur qui ne lui était pas destinée.

Rien n'est figé au moment de l'import. `load_config()` est appelé à chaque
usage, parce qu'une constante de module gelée à l'import rend les tests
dépendants de l'ordre d'exécution : le premier test qui importe le module gèle
l'environnement pour tous les suivants, et la panne ne se voit qu'en suite
complète.

Aucun secret ici, et il ne doit jamais y en avoir : le corpus est un fichier
SQLite local, la configuration décrit des chemins et des bornes, pas des
identifiants. Un fichier de credentials n'a aucune raison d'exister dans ce
dépôt (voir PLAN.md §3 sur la garde).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import MAX_BUDGET_S, MAX_PAGES_ANY

# Le défaut vise le volume Docker : dans l'image, `/data` est le point de
# montage du corpus. Hors conteneur ce chemin n'est pas inscriptible, ce qui
# est voulu — un serveur lancé sur l'hôte doit dire où il écrit (`MCC_DB`)
# plutôt que de déposer une base dans le répertoire courant.
DEFAULT_DB = Path("/data/corpus.db")
DEFAULT_BUDGET_S = 60
DEFAULT_PAGES_MAX = 10
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class Config:
    """La configuration effective d'un appel.

    `budget_s` et `pages_max` sont des valeurs par défaut pour `harvest` : le
    budget gagne toujours sur `pages_max` (PLAN.md §8), ce module ne fait que
    les lire.
    """

    db_path: Path
    budget_s: int
    pages_max: int
    log_level: str


def _env_int(name: str, default: int, *, maximum: int) -> int:
    """Lit un entier, et refuse bruyamment une valeur illisible ou hors bornes.

    Un `MCC_BUDGET_S=trente` rabattu silencieusement sur 60 ferait croire à un
    budget de 30 s alors que le serveur en utilise 60 : l'erreur doit nommer la
    variable plutôt que produire un chiffre faux. Le zéro et le négatif sont
    refusés pour la même raison : `MCC_PAGES_MAX=0` n'est pas « aucune page »,
    c'est une récolte qui ne lit rien en ayant l'air de tourner, et
    `MCC_BUDGET_S=0` une récolte toujours coupée.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} doit être un entier, reçu {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} doit être un entier >= 1, reçu {value}")
    if value > maximum:
        raise ValueError(
            f"{name} ne peut pas dépasser {maximum}, reçu {value} "
            "(c'est une borne du serveur, pas un objectif)"
        )
    return value


def load_config() -> Config:
    """La configuration courante, relue depuis l'environnement à chaque appel."""
    return Config(
        db_path=Path(os.environ.get("MCC_DB", str(DEFAULT_DB))),
        budget_s=_env_int("MCC_BUDGET_S", DEFAULT_BUDGET_S, maximum=MAX_BUDGET_S),
        pages_max=_env_int("MCC_PAGES_MAX", DEFAULT_PAGES_MAX, maximum=MAX_PAGES_ANY),
        log_level=os.environ.get("MCC_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper(),
    )
