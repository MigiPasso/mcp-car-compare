"""Corpus SQLite : les annonces d'un côté, les récoltes de l'autre.

Deux natures de chiffres, jamais mélangées (PLAN.md §5) :

- les **annonces** (`listings`) ont une identité `(source, market, source_id)` ;
  la même annonce vue par dix récoltes reste une ligne, `INSERT OR REPLACE` ;
- les **récoltes** (`runs`) portent les compteurs de `SourceCoverage`, une ligne
  par récolte et par `(source, market)`. Deux récoltes qui se recouvrent,
  additionnées, annoncent une couverture double : elles ne sont jamais sommées.

`run_listings` relie les deux : une annonce vue dans deux récoltes appartient
aux deux. Sans cette table, la seconde récolte effacerait l'annonce de la
première et la couverture d'un job plus ancien rétrécirait en silence.

Le chemin de la base est un paramètre, jamais une constante : les tests ouvrent
une base dans `tmp_path`.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import CorpusAge, CorpusCoverage, Listing, SourceCoverage, price_is_plausible

# BUSY_TIMEOUT_MS est le temps qu'une écriture attend un verrou tenu par une
# autre connexion avant de rendre `database is locked`. Sans lui, un `offers`
# lancé pendant un `harvest` échoue au lieu d'attendre la fin d'une transaction
# de quelques millisecondes.
BUSY_TIMEOUT_MS = 5000

# Nombre de valeurs montrées par critère fautif dans le diagnostic : assez pour
# reconnaître un vocabulaire, assez peu pour ne pas noyer la réponse.
_SAMPLE_VALUES = 5


def _utc_now() -> datetime:
    """L'heure courante en UTC : la seule horloge du corpus."""
    return datetime.now(timezone.utc)


def _timestamp(moment: datetime) -> str:
    """Un horodatage ISO-8601 UTC à largeur FIXE (`2026-11-01T09:00:00.000000+00:00`).

    La largeur fixe n'est pas décorative : `MIN`/`MAX` de SQLite comparent des
    chaînes, et un `isoformat()` qui omettrait les microsecondes quand elles sont
    nulles (`timespec="auto"`) produirait deux largeurs pour une même date. Les
    bornes rendues par `job_corpus_age` se liraient alors dans un ordre qui
    n'est plus le temps.
    """
    # Un `datetime` naïf serait interprété en heure LOCALE de l'hôte par
    # `astimezone()` : le même test passerait en UTC (conteneur) et rougirait
    # sur une machine à `TZ=Europe/Paris`. La couture `now` est ouverte aux
    # tests ; on ne peut pas compter sur leur vigilance pour le fuseau.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


# Un tri par corpus doit être total, sinon deux pages successives peuvent
# répéter ou sauter une annonce : `(source, market, source_id)` départage
# toujours. Les prix et années inconnus passent en dernier (ils ne sont pas
# « les moins chers »).
_ORDER_BY = {
    "price_asc": 'l."price" IS NULL, l."price" ASC, l."source", l."market", l."source_id"',
    "price_desc": 'l."price" IS NULL, l."price" DESC, l."source", l."market", l."source_id"',
    "age_desc": 'l."year" IS NULL, l."year" DESC, l."source", l."market", l."source_id"',
}

# Les colonnes qui reconstruisent un `Listing` à la lecture. `last_seen_at` n'y
# est pas : le site ne la publie pas, c'est le corpus qui l'horodate, et
# l'ajouter ici ferait sortir un champ d'horodatage dans `offers.items` — un
# contrat que personne n'a demandé.
_LISTING_COLUMNS = (
    "source",
    "market",
    "source_id",
    "url",
    "make",
    "group",
    "group_label",
    "engine",
    "trim",
    "year",
    "km",
    "price",
    "fuel",
    "gear",
    "vat_label",
    "seller",
    "city",
    "postal_code",
    "country",
    "image",
    "vin",
)

# Ce qu'un `add_page` écrit : l'annonce telle que le site la rend, plus la date
# de cette vue-ci. `INSERT OR REPLACE` réécrit la ligne entière, donc la date est
# bien RAFRAÎCHIE quand la même annonce est revue.
_LISTING_WRITE_COLUMNS = (*_LISTING_COLUMNS, "last_seen_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS "listings" (
    "source"        TEXT NOT NULL,
    "market"        TEXT NOT NULL,
    "source_id"     TEXT NOT NULL,
    "url"           TEXT NOT NULL,
    "make"          TEXT NOT NULL,
    "group"         TEXT NOT NULL,
    "group_label"   TEXT NOT NULL,
    "engine"        TEXT NOT NULL DEFAULT '',
    "trim"          TEXT NOT NULL DEFAULT '',
    "year"          INTEGER,
    "km"            INTEGER,
    "price"         INTEGER,
    "fuel"          TEXT NOT NULL DEFAULT '',
    "gear"          TEXT NOT NULL DEFAULT '',
    "vat_label"     TEXT NOT NULL DEFAULT '',
    "seller"        TEXT NOT NULL DEFAULT '',
    "city"          TEXT NOT NULL DEFAULT '',
    "postal_code"   TEXT NOT NULL DEFAULT '',
    "country"       TEXT NOT NULL DEFAULT '',
    "image"         TEXT NOT NULL DEFAULT '',
    "vin"           TEXT,
    -- Date de la dernière VUE de cette annonce, en ISO-8601 UTC. Elle ne vient
    -- pas du site : `Store.add_page` l'écrit à chaque passage, donc une annonce
    -- revue est rafraîchie et une annonce jamais revue garde sa dernière vue.
    -- NULL sur une base migrée (annonce écrite avant que la colonne existe) :
    -- une date inventée se lirait comme une mesure.
    "last_seen_at"  TEXT,
    PRIMARY KEY ("source", "market", "source_id")
);

CREATE TABLE IF NOT EXISTS "runs" (
    "job_id"             TEXT NOT NULL,
    "source"             TEXT NOT NULL,
    "market"             TEXT NOT NULL,
    "status"             TEXT NOT NULL,
    "announced_total"    INTEGER,
    "read"               INTEGER NOT NULL DEFAULT 0,
    "unique"             INTEGER NOT NULL DEFAULT 0,
    "dropped_duplicates" INTEGER NOT NULL DEFAULT 0,
    "dropped_invalid"    INTEGER NOT NULL DEFAULT 0,
    "truncated"          INTEGER NOT NULL DEFAULT 0,
    "reason"             TEXT NOT NULL DEFAULT '',
    "criteria_hash"      TEXT NOT NULL DEFAULT '',
    "sort"               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY ("job_id", "source", "market")
);

CREATE TABLE IF NOT EXISTS "run_listings" (
    "job_id"    TEXT NOT NULL,
    "source"    TEXT NOT NULL,
    "market"    TEXT NOT NULL,
    "source_id" TEXT NOT NULL,
    PRIMARY KEY ("job_id", "source", "market", "source_id")
);

-- Index de la COHORTE de `Store.market_gaps` : `offers` cherche, pour chaque
-- annonce rendue, les comparables d'une gamme et d'une année. Sans lui, chaque
-- annonce coûtait un balayage complet de `listings` — mesuré sur 40 000
-- annonces, page de 100 : ≈2 450 ms, contre 2 ms avec cet index.
--
-- PIÈGE, et il ne fait aucun bruit : l'index porte sur une EXPRESSION qui
-- appelle une fonction PYTHON (`unaccent`). Le jour où `_unaccent` change
-- (`ß`, `ø`, une marque combinante de plus), les entrées déjà écrites
-- correspondent à l'ANCIENNE fonction, et SQLite ne peut pas le savoir : les
-- lignes concernées cessent simplement de se joindre, sans erreur. Qui touche
-- à `_unaccent` doit donc reconstruire l'index (`REINDEX` ou `DROP INDEX` puis
-- réouverture de la base).
--
-- `IF NOT EXISTS` le rend idempotent : `executescript` le rejoue à chaque
-- ouverture, donc une base écrite par la version d'avant le reçoit aussi.
CREATE INDEX IF NOT EXISTS "idx_listings_cohort" ON "listings"(
    unaccent("make"), unaccent("group"), "year"
);
"""


# Les colonnes ajoutées APRÈS la première version du schéma, avec leur
# définition SQL. `CREATE TABLE IF NOT EXISTS` ne touche pas une table qui existe
# déjà : `executescript` ci-dessus n'ajoute donc rien à une base écrite par la
# version d'avant, et `write_run` échouerait sur l'INSERT (« table runs has no
# column named dropped_invalid »). Une mise à jour casserait alors toute
# récolte sur un corpus existant, sans que rien ne le signale avant l'appel.
_ADDED_COLUMNS = (
    ("runs", "dropped_invalid", "INTEGER NOT NULL DEFAULT 0"),
    # Sans date, une médiane présentait des récoltes d'âges inconnus comme « le
    # marché », voitures vendues comprises. La colonne est NULL sur les annonces
    # déjà écrites : l'absence de mesure se dit, elle ne s'invente pas.
    ("listings", "last_seen_at", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Ajoute à une base existante les colonnes que le schéma courant suppose.

    Un `ALTER TABLE` n'est idempotent qu'au prix de l'erreur : SQLite refuse
    une colonne qui existe déjà (`duplicate column name`). C'est cette
    erreur-là, et elle seule, qui est avalée — une table absente ou une base
    verrouillée doit continuer de lever, sinon une migration ratée passerait
    pour une migration faite.
    """
    for table, column, definition in _ADDED_COLUMNS:
        try:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


class UnknownJobError(LookupError):
    """Aucune récolte ne porte ce `job_id`."""


@dataclass(frozen=True, slots=True)
class JobCriteria:
    """Ce qui gouverne la lecture du corpus d'une récolte : son tri, et
    l'empreinte des critères qui l'ont produite."""

    sort: str
    criteria_hash: str


# --- analyse par modèle -------------------------------------------------------
# L'analyse répond à « quels modèles acheter » sur le corpus déjà récolté. Elle
# ne récolte rien et ne classe rien (PLAN.md §10) : elle rend des distributions
# et des comptes. Le groupe comparable est `(make, group)`, pas `(make,
# group_label)` : `group` est la clé de gamme que l'adaptateur normalise déjà
# entre marchés (« Série 3 » et « 3er » se rejoignent sur `serie 3`), donc deux
# libellés de la même gamme font un seul groupe.


@dataclass(frozen=True, slots=True)
class ListingFilter:
    """Ce qu'une analyse retient du corpus : des critères déjà validés.

    `None` sur `markets`/`sources` veut dire « aucun filtre » : tout le corpus,
    quel que soit son marché ou sa source. Cette distinction compte, et c'est
    pour cela que le défaut n'est pas celui de `harvest` : une récolte ne peut
    interroger qu'une marketplace à la fois, donc elle en choisit une par
    défaut ; une analyse lit un corpus déjà constitué, et se restreindre en
    silence à `.fr` cacherait la moitié des annonces d'un corpus qui en porte
    deux (PLAN.md §5 : l'analyse ne dépend pas de l'ordre des appels).

    `make` et `model` sont des LIBELLÉS, comparés sans tenir compte de la casse
    (sans casse ni accents) : `model` vaut pour `group_label` ou pour le `group`
    normalisé.
    Les deux outils de lecture (`harvest` par ses critères, `analyze_models` par
    ce même filtre) prennent désormais les **mêmes libellés** : ceux que le site
    affiche. Il n'y a plus deux vocabulaires pour un nom de clé — c'était le cas
    tant que `harvest` attendait les identifiants numériques du site, et c'est ce
    décalage qui a produit « 8 320 BMW annoncées, 7 Série 3 lues ».
    La comparaison ignore la casse **et les accents** (`unaccent`, mesuré :
    `CITROËN` trouve `Citroën`) ; `model` vaut pour `group_label` ou pour le
    `group` normalisé. La réponse nomme les filtres qui ne retiennent aucune
    annonce (`unmatched_filters`) plutôt que de rendre un zéro muet.
    """

    make: str = ""
    model: str = ""
    year_min: int | None = None
    year_max: int | None = None
    km_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    fuel: str = ""
    gear: str = ""
    markets: tuple[str, ...] | None = None
    sources: tuple[str, ...] | None = None

    def as_dict(self) -> dict:
        """Les critères réellement appliqués, tels que la réponse les annonce.

        Un champ vide n'est pas un filtre, donc il est absent — et c'est ce
        `dict` que l'appelant relit pour savoir ce qui a été filtré. Un critère
        appliqué mais absent d'ici serait un filtre invisible.
        """
        applied: dict = {}
        if self.make:
            applied["make"] = self.make
        if self.model:
            applied["model"] = self.model
        for key in ("year_min", "year_max", "km_max", "price_min", "price_max"):
            value = getattr(self, key)
            if value is not None:
                applied[key] = value
        if self.fuel:
            applied["fuel"] = self.fuel
        if self.gear:
            applied["gear"] = self.gear
        if self.markets is not None:
            applied["markets"] = list(self.markets)
        if self.sources is not None:
            applied["sources"] = list(self.sources)
        return applied


@dataclass(frozen=True, slots=True)
class ModelGroup:
    """Les agrégats d'un groupe `(make, group)`.

    `n` compte TOUTES les annonces du groupe, y compris celles qui ne portent
    pas de prix crédible : une annonce à 1 € (« prix non communiqué » chez
    AutoScout24) reste une annonce. `n_priced` compte celles qui portent un prix
    crédible, et c'est cette série-là, seule, qui porte les quantiles de prix.
    L'écart entre les deux chiffres est donc visible sans avoir à le recalculer.

    Le nombre d'annonces comparables de cette série — une même voiture comptée
    une fois, la clé de `_duplicate_key` — n'est délibérément PAS un champ ici :
    c'est la longueur de la série que rend `group_prices`, qui dédoublonne. Un
    champ de plus serait une seconde définition du même chiffre, et deux
    chiffres pour une seule série peuvent diverger ; `n_priced` reste donc un
    compte d'ANNONCES, jamais un compte de voitures.

    `year_median` et `km_median` sont les médianes classiques — la moyenne des
    deux valeurs du milieu quand l'effectif est pair — calculées en SQL sur les
    annonces du groupe qui portent l'année, respectivement le kilométrage.
    `None` quand aucune ne le porte : l'absence est dite, jamais comblée.
    """

    make_key: str
    group_key: str
    make: str
    group: str
    group_label: str
    n: int
    n_priced: int
    year_median: int | float | None
    km_median: int | float | None


@dataclass(frozen=True, slots=True)
class UnmatchedFilter:
    """Un critère appliqué qui ne retient aucune annonce du corpus, et ce que
    le corpus porte réellement pour le même champ.

    Sans lui, un filtre qui ne ramène rien se lirait comme « aucun modèle
    n'existe » au lieu de « ton libellé ne correspond à rien ».
    un libellé) rendrait zéro groupe sans que rien ne distingue « ce filtre ne
    correspond à rien » de « le corpus est vide ». `present` porte les valeurs
    trouvées, pas une valeur suggérée : l'outil ne devine aucun vocabulaire.
    """

    key: str
    value: object
    present: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FilterPart:
    """Un critère : sa clause SQL, ses paramètres, et la colonne où lire une
    valeur réellement présente (pour dire ce que le corpus porte).

    La colonne reste citée en SQL (`"group"` est un mot réservé) et la clause
    est toujours paramétrée : aucune valeur d'appelant n'entre dans le texte de
    la requête.
    """

    key: str
    value: object
    sql: str
    params: tuple[object, ...]
    column: str


def _filter_parts(listing_filter: ListingFilter) -> list[_FilterPart]:
    """Les critères effectivement appliqués, un par un, dans l'ordre du contrat.

    Un seul endroit décide de ce qui filtre une ANNONCE : `_filter_clause` en
    fait la clause SQL et `unmatched_filters` les sonde un par un. Les deux
    lisent cette liste, donc ils ne peuvent pas en diverger.

    `as_dict` n'est PAS la même liste, et c'est voulu : il annonce ce que
    l'appelant a demandé, y compris `markets` et `sources`, qui ne filtrent pas
    des annonces mais choisissent les sources à interroger. Les fusionner ferait
    disparaître ces deux critères de la réponse — un filtre appliqué deviendrait
    invisible, ce que `as_dict` existe précisément pour empêcher.
    """
    parts: list[_FilterPart] = []
    if listing_filter.make:
        parts.append(
            _FilterPart(
                "make",
                listing_filter.make,
                'unaccent(l."make") = ?',
                (_unaccent(listing_filter.make),),
                '"make"',
            )
        )
    if listing_filter.model:
        parts.append(
            _FilterPart(
                "model",
                listing_filter.model,
                '(unaccent(l."group_label") = ? OR unaccent(l."group") = ?)',
                (
                    _unaccent(listing_filter.model),
                    _unaccent(listing_filter.model),
                ),
                '"group_label"',
            )
        )
    for key, column, operator in (
        ("year_min", '"year"', ">="),
        ("year_max", '"year"', "<="),
        ("km_max", '"km"', "<="),
        ("price_min", '"price"', ">="),
        ("price_max", '"price"', "<="),
    ):
        bound = getattr(listing_filter, key)
        if bound is not None:
            parts.append(_FilterPart(key, bound, f"l.{column} {operator} ?", (bound,), column))
    for key, column in (("fuel", '"fuel"'), ("gear", '"gear"')):
        value = getattr(listing_filter, key)
        if value:
            parts.append(_FilterPart(key, value, f"l.{column} = ?", (value,), column))
    for key, column in (("markets", '"market"'), ("sources", '"source"')):
        values = getattr(listing_filter, key)
        if values:
            placeholders = ", ".join("?" for _ in values)
            parts.append(
                _FilterPart(
                    key, list(values), f"l.{column} IN ({placeholders})", tuple(values), column
                )
            )
    return parts


def _filter_clause(parts: Sequence[_FilterPart]) -> tuple[str, tuple[object, ...]]:
    """La conjonction d'un filtre, sans le mot `WHERE` : un filtre vide n'est
    pas une condition, et l'appelant décide où la placer."""
    if not parts:
        return "", ()
    return " AND ".join(part.sql for part in parts), tuple(
        value for part in parts for value in part.params
    )


# --- écart au marché d'une annonce --------------------------------------------
# `offers` chiffre chaque annonce face aux annonces comparables du corpus. Deux
# natures de chiffres, encore : la référence (`market_median`, `market_n`)
# décrit le marché, l'écart (`gap_eur`, `gap_pct`) décrit CETTE annonce.

# Les prix de la cohorte d'une annonce, dans l'ordre où `_comparable_prices`
# passe ses paramètres : `make`, `group`, année basse, année haute, puis
# l'identité de l'annonce exclue. Constante nommée pour que le test de plan
# d'exécution interroge le VRAI texte de la requête, pas une copie qui
# dériverait.
# La cohorte est restreinte à `(source, market)` de l'annonce : ce sont deux
# dimensions de son identité, et les oublier rendait le chiffre faux. Mesuré le
# 2026-11-28 : une Golf .fr à 20 000 € s'affichait « +37,9 % » au-dessus du
# marché parce que quatre Golf .de à 30 000 € étaient dans sa médiane — et une
# annonce .de était chiffrée contre des prix français, +106,9 %. Deux marchés ne
# portent ni la même TVA ni les mêmes frais : leurs prix ne se comparent pas. Le
# `source` est là pour la même raison, un site n'étant pas l'autre (AutoScout24
# et LaCentrale n'affichent pas les mêmes prix pour la même voiture).
# Le SELECT porte les cinq pièces de la clé de doublon : la cohorte écarte les
# annonces qui répètent une voiture déjà comptée, et c'est `_duplicate_key` qui
# décide de cette identité — pas le SQL, qui ne saurait pas dire qu'un
# kilométrage inconnu n'est pas un kilométrage égal.
_COHORT_SQL = (
    'SELECT l."make" AS make, l."group" AS grp, l."year" AS year, '
    'l."km" AS km, l."price" AS price FROM "listings" AS l '
    'WHERE unaccent(l."make") = unaccent(?) '
    'AND unaccent(l."group") = unaccent(?) '
    'AND l."year" BETWEEN ? AND ? '
    'AND l."source" = ? '
    'AND l."market" = ? '
    'AND NOT (l."source" = ? AND l."market" = ? AND l."source_id" = ?) '
    'ORDER BY l."price"'
)


# --- doublons : la même voiture annoncée plusieurs fois -----------------------
# Mesuré le 2026-11-28 : deux annonces AutoScout24 portaient deux identifiants
# différents pour la même voiture (15 900 €, 45 109 km, même vendeur). Le corpus
# garde les DEUX lignes — on ne fusionne pas (PLAN.md §4) — et `offers` les
# marque l'une vers l'autre.
#
# Ce SQL ne fait que choisir le SEAU que l'index de cohorte sait lire :
# `(make, group)` normalisés et l'année exacte. Toute la décision est dans
# `_duplicate_key`, en Python : les deux lecteurs de la clé (le marquage
# ci-dessous et la cohorte de `market_gaps`) la calculent donc par le même code,
# et une annonce marquée ne peut pas être comptée deux fois dans une médiane.
# Recopier `km` ou `price` en SQL ici rendrait invisible une clé qui les
# oublierait : le SQL filtrerait encore, et le test ne verrait rien.
_DUPLICATE_BUCKET_SQL = (
    'SELECT l."source" AS source, l."market" AS market, '
    'l."source_id" AS source_id, l."make" AS make, l."group" AS grp, '
    'l."year" AS year, l."km" AS km, l."price" AS price '
    'FROM "listings" AS l '
    'WHERE unaccent(l."make") = unaccent(?) '
    'AND unaccent(l."group") = unaccent(?) '
    'AND l."year" = ? '
    'AND l."market" = ? '
    'AND NOT (l."source" = ? AND l."market" = ? AND l."source_id" = ?) '
    'ORDER BY l."source", l."market", l."source_id"'
)


def _duplicate_key(
    market: str,
    make: str,
    group: str,
    year: int | None,
    km: int | None,
    price: int | None,
) -> tuple[str, str, str, int, int, int] | None:
    """La clé d'une même voiture annoncée plusieurs fois, ou `None` s'il manque
    une pièce.

    `market`, `(make, group)` normalisés comme partout ailleurs (`unaccent`, donc
    « CLIO » et « Clio » sont la même gamme), plus l'année, le kilométrage et le
    prix EXACTS.

    Le marché est dans la clé, la source n'y est pas — et ce n'est pas une
    asymétrie gratuite. La source n'y est pas parce que le cas utile est la même
    voiture vue sur deux sites français : c'est là que la comparaison de prix a
    un sens. Le marché y est parce que deux marchés ne publient pas des prix
    comparables (TVA, frais) : une annonce .fr et une .de au même prix
    numérique ne désignent pas la même voiture, elles désignent deux voitures
    différentes qui se trouvent coûter le même chiffre. Les marquer doublons
    contredirait la règle que la cohorte applique déjà.

    `None` quand l'année, le kilométrage ou le prix manque : `NULL` n'est pas
    une valeur égale. Deux annonces du même modèle dont on ignore le kilométrage
    ne sont pas pour autant la même voiture, et les rapprocher marquerait comme
    doublons des annonces qui n'en sont pas — ou en écarterait une d'une médiane
    de prix.

    **La limite, dite plutôt que masquée** : cette clé ne distingue pas deux
    voitures réellement identiques (même modèle, même année, même kilométrage,
    même prix) chez deux vendeurs différents ; elles ressortent marquées l'une
    vers l'autre. C'est précisément pourquoi le corpus ne FUSIONNE rien : les
    deux lignes restent, avec leurs deux `source_id`, et `duplicate_of` est une
    observation que l'appelant peut ignorer. Le vendeur n'entre pas dans la clé
    parce que deux sources sur trois ne le donnent pas (LaCentrale 0/231,
    Autohero 0/10) : une clé qui en dépendrait ne s'appliquerait qu'à une
    source.
    """
    if year is None or km is None or price is None:
        return None
    return (market, str(_unaccent(make)), str(_unaccent(group)), year, km, price)


@dataclass(frozen=True, slots=True)
class MarketGap:
    """Les comparables d'une annonce, et leur médiane de prix.

    `n` compte les comparables réellement entrés dans la médiane — prix
    crédibles, année à ±1 an, annonce exclue, et une voiture annoncée deux fois
    comptée une seule fois (`Store.market_gaps`). `median` ne vaut `None` que
    quand `n` vaut 0 : les deux champs disent la même chose sous deux formes,
    donc aucun des deux ne peut mentir seul.

    Le seuil d'abstention (`models.MIN_COMPARABLES`) n'est PAS appliqué ici :
    décider qu'il manque des comparables est une règle de surface, et `tools`
    la porte une seule fois, pour `offers` comme pour `analyze_models`.
    """

    median: int | None
    n: int


class Store:
    """Accès au corpus. S'ouvre en contexte : `with Store(path) as store: ...`.

    `now` est injectable pour les tests : `last_seen_at` est daté à l'écriture,
    et un test qui doit vieillir un corpus n'a pas à dormir.
    """

    def __init__(
        self, db_path: str | Path, *, now: Callable[[], datetime] = _utc_now
    ) -> None:
        self.path = Path(db_path)
        self._now = now
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        # WAL est ce qui permet à une lecture (`offers`) de se faire pendant une
        # écriture (`harvest`) : en journal par défaut, le COMMIT de l'écrivain
        # a besoin d'un verrou exclusif et échoue dès qu'un lecteur tient un
        # instantané. Le mode est une propriété du FICHIER, il est donc posé à
        # chaque ouverture (sans effet s'il l'est déjà).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # `unaccent` plutôt que `LOWER` pour les libellés : le `LOWER` de SQLite
        # n'abaisse que l'ASCII, donc `LOWER('CITROËN')` vaut `citroËn` et ne
        # matche jamais `Citroën` — mesuré. Sur un marché français, c'est
        # exactement les marques qu'on cherche qui échouent. La fonction est
        # déclarée `deterministic` pour que SQLite puisse la traiter comme telle.
        self._conn.create_function("unaccent", 1, _unaccent, deterministic=True)
        self._conn.executescript(_SCHEMA)
        _add_missing_columns(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # --- écriture -----------------------------------------------------------

    def add_page(self, job_id: str, listings: Iterable[Listing]) -> None:
        """Persiste une page lue et la rattache à la récolte `job_id`.

        Revoir une annonce déjà connue la met à jour sans créer de ligne : le
        corpus ne compte que des annonces distinctes.

        Chaque annonce écrite porte `last_seen_at`, la date de CETTE vue
        (`self._now`, ISO-8601 UTC), et `INSERT OR REPLACE` la réécrit : une
        annonce revue est donc rafraîchie, une annonce jamais revue garde la
        date de sa dernière vue. C'est exactement ce que `corpus_age` mesure.

        Une seule date par page : les annonces d'une même page ont été vues
        ensemble, et deux lectures d'horloge dans la même page ne feraient que
        suggérer une précision que la récolte n'a pas.
        """
        columns = ", ".join(f'"{name}"' for name in _LISTING_WRITE_COLUMNS)
        placeholders = ", ".join("?" for _ in _LISTING_WRITE_COLUMNS)
        insert_listing = f'INSERT OR REPLACE INTO "listings" ({columns}) VALUES ({placeholders})'
        link_listing = (
            'INSERT OR IGNORE INTO "run_listings" '
            '("job_id", "source", "market", "source_id") VALUES (?, ?, ?, ?)'
        )
        seen_at = _timestamp(self._now())
        with self._conn:
            for listing in listings:
                fields = listing.as_dict()
                self._conn.execute(
                    insert_listing,
                    tuple(fields[name] for name in _LISTING_COLUMNS) + (seen_at,),
                )
                self._conn.execute(link_listing, (job_id,) + listing.key())

    def write_run(
        self,
        job_id: str,
        coverage: SourceCoverage,
        *,
        criteria_hash: str,
        sort: str,
    ) -> None:
        """Écrit la ligne de récolte d'un `(source, market)`. Une par récolte :
        deux appels sur la même paire remplacent, ils n'additionnent pas."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO "runs" ('
                '"job_id", "source", "market", "status", "announced_total", '
                '"read", "unique", "dropped_duplicates", "dropped_invalid", '
                '"truncated", "reason", "criteria_hash", "sort") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    job_id,
                    coverage.source,
                    coverage.market,
                    coverage.status,
                    coverage.announced_total,
                    coverage.read,
                    coverage.unique,
                    coverage.dropped_duplicates,
                    coverage.dropped_invalid,
                    int(coverage.truncated),
                    coverage.reason,
                    criteria_hash,
                    sort,
                ),
            )

    # --- lecture ------------------------------------------------------------

    def job(self, job_id: str) -> JobCriteria:
        """Tri et empreinte de critères d'une récolte. Lève `UnknownJobError`."""
        row = self._conn.execute(
            'SELECT "sort", "criteria_hash" FROM "runs" WHERE "job_id" = ? '
            'ORDER BY "source", "market" LIMIT 1',
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(f"récolte inconnue : {job_id!r}")
        return JobCriteria(sort=row["sort"], criteria_hash=row["criteria_hash"])

    def run(self, job_id: str, source: str, market: str) -> SourceCoverage | None:
        """La ligne de récolte déjà écrite pour une paire, ou `None`.

        Lue par une REPRISE (`tools._harvest_one`) : la couverture d'un job
        décrit le total depuis son début, donc les compteurs d'une reprise
        s'ajoutent à ceux déjà écrits au lieu de les remplacer. `write_run`
        remplace la ligne — c'est ce qui garde une seule ligne par
        `(job_id, source, market)` — et c'est donc à l'appelant de lire avant
        d'écrire. Sans cette lecture, une reprise qui relit deux pages
        annoncerait deux pages de couverture pour en avoir lu vingt.
        """
        row = self._conn.execute(
            'SELECT "source", "market", "status", "announced_total", "read", '
            '"unique", "dropped_duplicates", "dropped_invalid", "truncated", '
            '"reason" FROM "runs" '
            'WHERE "job_id" = ? AND "source" = ? AND "market" = ?',
            (job_id, source, market),
        ).fetchone()
        if row is None:
            return None
        return SourceCoverage(
            source=row["source"],
            market=row["market"],
            status=row["status"],
            announced_total=row["announced_total"],
            read=row["read"],
            unique=row["unique"],
            dropped_duplicates=row["dropped_duplicates"],
            dropped_invalid=row["dropped_invalid"],
            truncated=bool(row["truncated"]),
            reason=row["reason"],
        )

    def run_listing_ids(self, job_id: str, source: str, market: str) -> set[str]:
        """Les `source_id` déjà rattachés à cette récolte pour cette paire.

        C'est le `seen` d'une reprise : une annonce lue par l'appel précédent et
        revue par celui-ci est un DOUBLON de la récolte, pas une annonce neuve.
        Sans cette lecture, `unique` compterait deux fois la carte mise en avant
        qui revient en tête de chaque page, et l'identité `read == unique +
        dropped_duplicates + dropped_invalid` ne se refermerait plus sur une
        récolte reprise.
        """
        rows = self._conn.execute(
            'SELECT "source_id" FROM "run_listings" '
            'WHERE "job_id" = ? AND "source" = ? AND "market" = ?',
            (job_id, source, market),
        ).fetchall()
        return {row["source_id"] for row in rows}

    def page(
        self, job_id: str, *, sort: str, offset: int, limit: int
    ) -> tuple[list[Listing], bool]:
        """`limit` annonces de la récolte à partir de `offset`, et s'il en reste.

        L'ordre vient du tri de la récolte et se termine toujours par la clé
        d'identité : `offset` tombe alors sur la même annonce d'un appel à
        l'autre.
        """
        order = _ORDER_BY.get(sort)
        if order is None:
            raise ValueError(f"tri inconnu : {sort!r}")
        columns = ", ".join(f'l."{name}"' for name in _LISTING_COLUMNS)
        rows = self._conn.execute(
            f'SELECT {columns} FROM "listings" AS l '
            'JOIN "run_listings" AS rl ON rl."source" = l."source" '
            'AND rl."market" = l."market" AND rl."source_id" = l."source_id" '
            'WHERE rl."job_id" = ? '
            f'ORDER BY {order} LIMIT ? OFFSET ?',
            (job_id, limit + 1, offset),
        ).fetchall()
        listings = [
            Listing(**{name: row[name] for name in _LISTING_COLUMNS})
            for row in rows[:limit]
        ]
        return listings, len(rows) > limit

    def corpus(self, job_id: str) -> CorpusCoverage:
        """Ce que la récolte contient réellement : annonces distinctes, nombre
        de récoltes, et le détail par source. Jamais la somme des `read`.

        Chaque source porte aussi l'état de sa récolte (`read`, `unique`,
        `announced_total`, `truncated`, `status`) : les annonces distinctes
        disent la taille du corpus, ces champs disent la part du marché lue.
        Un corpus de 400 annonces sur un marché qui en annonce 12 479 doit
        pouvoir le dire à un appelant qui ne lit qu'`offers`.

        `listings_in_corpus` compte les annonces de la PAIRE `(source, market)`
        dans le corpus entier, exactement comme `whole_corpus()` : deux chemins,
        une seule chose par clé. Ce que la récolte a laissé de neuf est déjà
        porté par `unique` ; compter ici les seules lignes de `run_listings`
        aurait donné deux chiffres différents sous le même nom selon l'outil.

        Une ligne de `runs` par `(source, market)`, sans agrégat ni `GROUP BY` :
        c'est la clé primaire de `runs`, `(job_id, source, market)`, qui
        garantit qu'une paire n'y a qu'une ligne (`write_run` remplace, il
        n'ajoute pas). Un `GROUP BY source, market` aurait rendu, pour deux
        lignes d'une même paire, un `status` et un `read` choisis arbitrairement
        parmi elles ; la sous-requête de comptage laisse au contraire chaque
        ligne de récolte visible au lieu de la fusionner en silence. Le jour où
        une deuxième récolte de la même paire devra cohabiter avec la première,
        c'est la clé primaire qu'il faudra changer, et cette requête le montrera.
        """
        run_count = self._conn.execute(
            'SELECT COUNT(*) FROM "runs" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        distinct = self._conn.execute(
            'SELECT COUNT(*) FROM "run_listings" WHERE "job_id" = ?', (job_id,)
        ).fetchone()[0]
        rows = self._conn.execute(
            'SELECT r."source" AS source, r."market" AS market, '
            'r."status" AS status, r."announced_total" AS announced_total, '
            'r."read" AS read, r."unique" AS "unique", '
            'r."truncated" AS truncated, '
            '(SELECT COUNT(*) FROM "listings" AS l '
            'WHERE l."source" = r."source" AND l."market" = r."market") '
            'AS listings_in_corpus '
            'FROM "runs" AS r WHERE r."job_id" = ? '
            'ORDER BY r."source", r."market"',
            (job_id,),
        ).fetchall()
        return CorpusCoverage(
            listings_distinct=distinct,
            runs=run_count,
            sources=[
                {
                    "source": row["source"],
                    "market": row["market"],
                    "listings_in_corpus": row["listings_in_corpus"],
                    "status": row["status"],
                    "announced_total": row["announced_total"],
                    "read": row["read"],
                    "unique": row["unique"],
                    "truncated": bool(row["truncated"]),
                }
                for row in rows
            ],
        )

    def whole_corpus(self) -> CorpusCoverage:
        """Le corpus entier, toutes récoltes confondues, dans la forme de
        `corpus()` : annonces distinctes, nombre de récoltes, et une entrée par
        `(source, market)`.

        C'est le bloc dont une analyse a besoin : elle porte sur tout le corpus
        correspondant à ses critères, pas sur une récolte (PLAN.md §5), et le
        bloc décrit donc ce que le corpus contient — jamais le sous-ensemble
        qu'un filtre a retenu. Les compteurs de récolte n'ont rien à voir avec
        les critères : les filtrer laisserait croire que la couverture a changé
        parce qu'on a filtré.

        **Les compteurs d'une paire viennent de la dernière récolte écrite pour
        elle**, pas d'un agrégat : `runs` n'a pas d'horodatage, et additionner
        deux récoltes annoncerait une couverture double (PLAN.md §5). « Dernière
        écrite » s'appuie sur `MAX(rowid)` : `write_run` fait un
        `INSERT OR REPLACE`, qui supprime la ligne puis en insère une nouvelle,
        donc chaque écriture alloue un rowid neuf. C'est une propriété implicite
        de SQLite : un `ON CONFLICT DO UPDATE` la retirerait sans rien casser
        d'autre, et « la dernière récolte » deviendrait « une récolte
        arbitraire » — d'où ce commentaire. L'entrée ne porte pas le `job_id` de
        la récolte : les huit clés sont les mêmes que celles de `corpus()`, et
        deux formes de couverture pour un même type seraient un contrat à deux
        vitesses ; l'appelant qui veut le détail d'une récolte l'a déjà, c'est
        lui qui l'a lancée.

        Les annonces se comptent dans `listings` : la clé primaire
        `(source, market, source_id)` y garantit une ligne par annonce, donc
        `COUNT(*)` est déjà un compte d'annonces distinctes.
        """
        run_count = self._conn.execute('SELECT COUNT(*) FROM "runs"').fetchone()[0]
        distinct = self._conn.execute('SELECT COUNT(*) FROM "listings"').fetchone()[0]
        rows = self._conn.execute(
            'SELECT r."source" AS source, r."market" AS market, '
            'r."status" AS status, r."announced_total" AS announced_total, '
            'r."read" AS read, r."unique" AS "unique", '
            'r."truncated" AS truncated, '
            '(SELECT COUNT(*) FROM "listings" AS l '
            'WHERE l."source" = r."source" AND l."market" = r."market") '
            'AS listings_in_corpus '
            'FROM "runs" AS r WHERE r.rowid = ('
            'SELECT MAX(r2.rowid) FROM "runs" AS r2 '
            'WHERE r2."source" = r."source" AND r2."market" = r."market") '
            'ORDER BY r."source", r."market"'
        ).fetchall()
        return CorpusCoverage(
            listings_distinct=distinct,
            runs=run_count,
            sources=[
                {
                    "source": row["source"],
                    "market": row["market"],
                    "listings_in_corpus": row["listings_in_corpus"],
                    "status": row["status"],
                    "announced_total": row["announced_total"],
                    "read": row["read"],
                    "unique": row["unique"],
                    "truncated": bool(row["truncated"]),
                }
                for row in rows
            ],
        )

    # --- âge du corpus ------------------------------------------------------

    def job_corpus_age(self, job_id: str) -> CorpusAge:
        """Les bornes de `last_seen_at` des annonces de CETTE récolte.

        Le corpus de `offers`, c'est la récolte qu'il pagine : les bornes
        décrivent donc l'ensemble des annonces de la récolte, pas la page
        rendue. Une date par page changerait à chaque page alors que le corpus
        ne change pas, et `coverage` décrit déjà le corpus de la récolte — les
        deux blocs doivent parler du même objet. L'appelant qui veut l'âge d'une
        annonce précise la retrouve dans `oldest_seen`/`newest_seen`.

        `MIN`/`MAX` ignorent les `NULL` : une annonce écrite avant que la
        colonne existe ne tire pas la borne vers une date inventée, elle
        n'apparaît pas. Un corpus vide (ou entièrement non daté) rend deux
        `None`.
        """
        row = self._conn.execute(
            'SELECT MIN(l."last_seen_at") AS oldest, MAX(l."last_seen_at") AS newest '
            'FROM "listings" AS l '
            'JOIN "run_listings" AS rl ON rl."source" = l."source" '
            'AND rl."market" = l."market" AND rl."source_id" = l."source_id" '
            'WHERE rl."job_id" = ?',
            (job_id,),
        ).fetchone()
        return CorpusAge(oldest_seen=row["oldest"], newest_seen=row["newest"])

    def filtered_corpus_age(self, listing_filter: ListingFilter) -> CorpusAge:
        """Les bornes de `last_seen_at` des annonces que ce filtre retient.

        C'est le corpus qui a SERVI une analyse, pas la base entière : une
        médiane de Clio porte sur les Clio du corpus, et dater la base
        répondrait à côté — un corpus de Clio toutes fraîches et une base de
        Golf de l'an dernier rendraient « un mois » sous une médiane qui n'a
        qu'un jour. Le filtre est celui de `model_groups`, appliqué par le même
        `_filter_parts` : les bornes et les comptes portent donc sur le même
        ensemble d'annonces.
        """
        clause, params = _filter_clause(_filter_parts(listing_filter))
        where = f"WHERE {clause}" if clause else ""
        row = self._conn.execute(
            'SELECT MIN(l."last_seen_at") AS oldest, MAX(l."last_seen_at") AS newest '
            f'FROM "listings" AS l {where}',
            params,
        ).fetchone()
        return CorpusAge(oldest_seen=row["oldest"], newest_seen=row["newest"])

    # --- analyse par modèle -------------------------------------------------

    def model_groups(
        self, listing_filter: ListingFilter, *, price_floor: int, limit: int
    ) -> tuple[list[ModelGroup], int]:
        """Les `limit` premiers groupes `(make, group)` du corpus qui
        correspondent au filtre, et le nombre TOTAL de groupes correspondants.

        L'ordre est neutre et il est cité : `n` décroissant, puis `make` et
        `group` alphabétiques. Aucun classement de « meilleure affaire » —
        l'outil rend des distributions, le choix reste à l'appelant
        (PLAN.md §10). Les deux chiffres rendus ensemble sont ce qui distingue
        « 30 groupes » de « 30 sur 87 » : `limit` coupe la liste, jamais le
        compte.

        `price_floor` est un PARAMÈTRE, jamais un nombre écrit ici : le plancher
        de crédibilité d'un prix n'a qu'une déclaration (`models.PRICE_FLOOR_EUR`)
        et une seule règle (`models.price_is_plausible`). Une copie dans ce SQL
        se désynchroniserait de `offers.price_plausible` sans que rien ne le
        dise — la même annonce serait crédible ici et pas là.

        Les médianes d'année et de kilométrage sont calculées en SQL, par
        fenêtres : pour chaque groupe, le rang et l'effectif de chaque valeur,
        puis la moyenne des deux rangs du milieu — un effectif impair ne retient
        qu'un rang, donc la médiane reste la médiane. Les quantiles de PRIX, eux,
        sont calculés par `tools` sur les seuls groupes retenus : ils demandent
        une interpolation que SQLite n'exprime pas, et charger trente séries
        n'est pas charger le corpus.
        """
        clause, params = _filter_clause(_filter_parts(listing_filter))
        where = f"WHERE {clause}" if clause else ""
        rows = self._conn.execute(
            f'WITH f AS ('
            # La clé de groupe est NORMALISÉE (casse et accents) : AutoScout24
            # écrit « Clio » et LaCentrale « CLIO », et sans ça le même modèle
            # sortait en deux lignes — mesuré le 2026-11-28 sur une récolte à
            # deux sources. Le libellé affiché reste celui du site.
            'SELECT unaccent(l."make") AS make_key, unaccent(l."group") AS grp_key, '
            'l."make" AS make_raw, l."group" AS grp_raw, l."group_label" AS label_, '
            'l."year" AS year_, l."km" AS km_, l."price" AS price_ '
            f'FROM "listings" AS l {where}), '
            'years AS ('
            'SELECT make_key, grp_key, AVG(year_) AS median_ FROM ('
            'SELECT make_key, grp_key, year_, '
            'ROW_NUMBER() OVER (PARTITION BY make_key, grp_key ORDER BY year_) AS rn, '
            'COUNT(*) OVER (PARTITION BY make_key, grp_key) AS cnt '
            'FROM f WHERE year_ IS NOT NULL) '
            'WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2) GROUP BY make_key, grp_key), '
            'kms AS ('
            'SELECT make_key, grp_key, AVG(km_) AS median_ FROM ('
            'SELECT make_key, grp_key, km_, '
            'ROW_NUMBER() OVER (PARTITION BY make_key, grp_key ORDER BY km_) AS rn, '
            'COUNT(*) OVER (PARTITION BY make_key, grp_key) AS cnt '
            'FROM f WHERE km_ IS NOT NULL) '
            'WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2) GROUP BY make_key, grp_key) '
            'SELECT g.make_key AS make_key, g.grp_key AS grp_key, '
            'g.make AS make, g.grp AS grp, g.label_ AS group_label, '
            'g.n AS n, g.n_priced AS n_priced, '
            'y.median_ AS year_median, k.median_ AS km_median '
            'FROM (SELECT make_key, grp_key, MAX(make_raw) AS make, MAX(grp_raw) AS grp, MAX(label_) AS label_, COUNT(*) AS n, '
            'SUM(CASE WHEN price_ >= ? THEN 1 ELSE 0 END) AS n_priced '
            'FROM f GROUP BY make_key, grp_key) AS g '
            'LEFT JOIN years AS y ON y.make_key = g.make_key AND y.grp_key = g.grp_key '
            'LEFT JOIN kms AS k ON k.make_key = g.make_key AND k.grp_key = g.grp_key '
            'ORDER BY g.n DESC, g.make_key, g.grp_key LIMIT ?',
            (*params, price_floor, limit),
        ).fetchall()
        total = self._conn.execute(
            'SELECT COUNT(*) FROM ('
            f'SELECT 1 FROM "listings" AS l {where} '
            'GROUP BY unaccent(l."make"), unaccent(l."group"))',
            params,
        ).fetchone()[0]
        return (
            [
                ModelGroup(
                    make_key=row["make_key"],
                    group_key=row["grp_key"],
                    make=row["make"],
                    group=row["grp"],
                    group_label=row["group_label"],
                    n=row["n"],
                    n_priced=row["n_priced"],
                    year_median=_number(row["year_median"]),
                    km_median=_number(row["km_median"]),
                )
                for row in rows
            ],
            total,
        )

    def group_prices(
        self,
        listing_filter: ListingFilter,
        groups: Sequence[tuple[str, str]],
        *,
        price_floor: int,
    ) -> dict[tuple[str, str], list[int]]:
        """Les prix crédibles de chaque groupe demandé, triés croissant, une
        même voiture comptée une seule fois.

        Une seule requête pour tous les groupes retenus, et pas une par groupe :
        chaque groupe coûterait une application du filtre. Les prix sous le
        plancher sont absents de la série — ils restent comptés dans `n` par
        `model_groups`, mais une médiane calculée sur un prix à 1 € ne serait pas
        un prix. Un groupe sans prix crédible rend une liste vide, jamais une
        clé absente : l'appelant doit pouvoir distinguer « zéro prix » d'un
        groupe qu'il aurait oublié de demander.

        **La série est dédoublonnée avec `_duplicate_key`**, la clé que `offers`
        applique déjà à sa cohorte (`market_n`, `duplicate_of`) : une voiture
        annoncée deux fois sous deux `source_id` n'entre qu'une fois, sinon elle
        pèserait double dans la médiane du groupe — c'est le défaut que ce
        dédoublonnage corrige, l'analyse comptant avant toutes les annonces là
        où `offers` comptait une voiture une fois. La conséquence à connaître :
        `len(série)` n'est PAS `n_priced`, qui compte des annonces.

        Le dédoublonnage se fait en Python, par groupe, et pas en SQL :
        `SELECT DISTINCT` tiendrait deux kilométrages inconnus pour égaux, ce que
        la clé refuse (`NULL` n'est pas une valeur égale). Le SQL projette donc
        les cinq pièces de la clé en plus du prix. Deux annonces de la même
        voiture portent le même prix — le prix est DANS la clé — donc l'ordre
        entre elles ne change pas le prix retenu.
        """
        series: dict[tuple[str, str], list[int]] = {group: [] for group in groups}
        if not groups:
            return series
        clause, params = _filter_clause(_filter_parts(listing_filter))
        # Même normalisation que `model_groups` : les groupes rendus portent la
        # clé, la jointure ne peut donc pas manquer une annonce que le
        # regroupement a comptée.
        keys = " OR ".join(
            '(unaccent(l."make") = unaccent(?) AND unaccent(l."group") = unaccent(?))'
            for _ in groups
        )
        extra = f" AND {clause}" if clause else ""
        rows = self._conn.execute(
            'SELECT unaccent(l."make") AS make_key, unaccent(l."group") AS grp_key, '
            'l."market" AS market, l."year" AS year, l."km" AS km, '
            'l."price" AS price '
            f'FROM "listings" AS l WHERE ({keys}) AND l."price" >= ?{extra} '
            'ORDER BY make_key, grp_key, l."price"',
            (
                *(value for group in groups for value in group),
                price_floor,
                *params,
            ),
        ).fetchall()
        # Un ensemble de clés par groupe : deux groupes peuvent porter la même
        # voiture au même prix, et ce sont deux séries distinctes.
        seen: dict[tuple[str, str], set[tuple[str, str, str, int, int, int]]] = {
            group: set() for group in groups
        }
        for row in rows:
            group_key = (row["make_key"], row["grp_key"])
            # `make_key` et `grp_key` sont déjà normalisés par le SQL ;
            # `_duplicate_key` les normalise une seconde fois, sans effet
            # (`unaccent` est idempotent).
            duplicate = _duplicate_key(
                row["market"],
                row["make_key"],
                row["grp_key"],
                row["year"],
                row["km"],
                row["price"],
            )
            if duplicate is not None:
                if duplicate in seen[group_key]:
                    continue
                seen[group_key].add(duplicate)
            series[group_key].append(row["price"])
        return series

    def unmatched_filters(
        self, listing_filter: ListingFilter
    ) -> list[UnmatchedFilter]:
        """Les critères qui ne retiennent aucune annonce du corpus, avec ce que
        le corpus porte pour le même champ (au plus `sample` valeurs).

        Chaque critère est sondé SEUL, sur le corpus entier : c'est ce qui rend
        le diagnostic attribuable — « make='13' ne correspond à aucun libellé ;
        le corpus porte BMW, Renault » se lit sans deviner lequel des cinq
        critères est en cause. Une intersection vide, elle, n'est pas un
        vocabulaire faux mais une combinaison sans résultat, et `tools` la dit
        autrement.

        Le sondage ne devine rien : il ne teste ni forme numérique ni casse
        spéciale. `model="500"` est un libellé parfaitement légitime (une Fiat
        500) : une heuristique du genre « une valeur courte et chiffrée est donc
        un code » casserait ce cas réel pour rendre service à un autre.
        """
        misses: list[UnmatchedFilter] = []
        for part in _filter_parts(listing_filter):
            hits = self._conn.execute(
                f'SELECT COUNT(*) FROM "listings" AS l WHERE {part.sql}', part.params
            ).fetchone()[0]
            if hits:
                continue
            present = self._conn.execute(
                f'SELECT DISTINCT l.{part.column} AS present FROM "listings" AS l '
                f'WHERE CAST(l.{part.column} AS TEXT) <> \'\' '
                'ORDER BY present LIMIT ?',
                (_SAMPLE_VALUES,),
            ).fetchall()
            misses.append(
                UnmatchedFilter(
                    key=part.key,
                    value=part.value,
                    present=tuple(str(row["present"]) for row in present),
                )
            )
        return misses

    # --- écart au marché ----------------------------------------------------

    def market_gaps(
        self, listings: Sequence[Listing]
    ) -> dict[tuple[str, str, str], MarketGap]:
        """La référence de marché de chaque annonce : médiane des comparables
        et effectif, sous la clé d'identité de l'annonce.

        Cohorte d'une annonce : mêmes `(make, group)` NORMALISÉS (`unaccent`,
        comme `model_groups`, donc « CLIO » et « Clio » font une seule gamme),
        année à ±1 an, prix crédibles seulement, et **l'annonce elle-même
        exclue** : sans cette exclusion elle tirerait sa propre référence vers
        elle, donc vers un écart nul. Une annonce sans année n'a pas de
        cohorte — la fenêtre ±1 n'existe pas — et rend `n = 0`, jamais une
        cohorte élargie.

        **`n` compte des ANNONCES COMPARABLES, une même voiture comptée une
        fois** (`_duplicate_key`, la même clé que `duplicate_marks`), sinon un
        doublon pèserait double dans la référence qui sert à juger les annonces.
        Ce n'est pas la même chose que « des voitures distinctes », et le
        vocabulaire compte : deux voitures réellement distinctes qui partagent
        modèle, année, kilométrage et prix se rabattent sur une seule, parce
        qu'aucune donnée du site ne permet de les séparer (le vendeur manque
        chez deux sources sur trois). Le chiffre est donc un minorant honnête du
        nombre de voitures, jamais une affirmation qu'elles sont distinctes.
        Deux annonces dont la clé est incomplète (kilométrage inconnu) ne sont
        pas rabattues l'une sur l'autre : elles restent deux prix, parce qu'on
        ne sait pas qu'elles sont la même voiture.

        Le plancher de crédibilité n'est PAS écrit dans le SQL :
        `models.price_is_plausible` est la seule règle qui décide ce qu'est un
        prix, et elle filtre la série une fois lue. Un `price >= 100` recopié
        ici se désynchroniserait du `price_plausible` que `offers` rend à côté,
        sans que rien ne le dise.

        Une requête par annonce, et c'est mesuré : sur un corpus de 40 000
        annonces avec l'index `idx_listings_cohort`, cent références coûtent
        2 ms ; sans cet index, ≈2 450 ms. Pré-calculer par lot d'années ne
        payerait pas cette complexité.

        La clé du dictionnaire est l'identité de l'annonce
        (`Listing.key()`), pas son rang dans la page : une référence ne peut
        donc pas glisser d'une annonce à l'autre.
        """
        gaps: dict[tuple[str, str, str], MarketGap] = {}
        for listing in listings:
            prices = self._comparable_prices(listing)
            gaps[listing.key()] = MarketGap(median=_median_of(prices), n=len(prices))
        return gaps

    def _comparable_prices(self, listing: Listing) -> list[int]:
        """Les prix de la cohorte d'une annonce, triés croissant, une voiture
        dupliquée comptée une seule fois.

        Appelée une fois par annonce par `market_gaps`, qui documente la
        cohorte. L'année est dans le `WHERE` : un comparable sans année ne peut
        pas être « de la même année à ±1 an », et `NULL BETWEEN ? AND ?` n'est
        jamais vrai — l'absence s'exclut d'elle-même au lieu de passer pour
        n'importe quelle année.

        La déduplication se fait ici, avec `_duplicate_key` : c'est la même
        fonction que le marquage d'`offers`, donc une annonce marquée dupliquée
        est exactement une annonce qui ne compte qu'une fois dans une médiane.
        Le SQL ne dédoublonne pas lui-même (`SELECT DISTINCT` aurait tenu deux
        kilométrages inconnus pour égaux, ce que la clé refuse).

        Le SQL est une constante nommée parce qu'un test le soumet à
        `EXPLAIN QUERY PLAN` : c'est ce qui prouve que l'index de cohorte est
        utilisé, sans dépendre d'un chronomètre.
        """
        if listing.year is None:
            return []
        rows = self._conn.execute(
            _COHORT_SQL,
            (
                listing.make,
                listing.group,
                listing.year - 1,
                listing.year + 1,
                listing.source,
                listing.market,
                listing.source,
                listing.market,
                listing.source_id,
            ),
        ).fetchall()
        prices: list[int] = []
        seen: set[tuple[str, str, int, int, int]] = set()
        for row in rows:
            # Toute la cohorte partage le marché de l'annonce (le SQL le filtre),
            # donc `listing.market` est la valeur des lignes : la lire sur la
            # ligne demanderait de la projeter pour rien.
            key = _duplicate_key(
                listing.market, row["make"], row["grp"], row["year"], row["km"], row["price"]
            )
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            if price_is_plausible(row["price"]):
                prices.append(row["price"])
        return prices

    # --- doublons -----------------------------------------------------------

    def duplicate_marks(
        self, listings: Sequence[Listing]
    ) -> dict[tuple[str, str, str], str | None]:
        """Pour chaque annonce, la `source_id` de l'autre annonce qui décrit la
        MÊME voiture — `None` quand elle est seule au monde.

        **On marque, on ne fusionne pas** (PLAN.md §4) : les deux lignes restent
        dans le corpus avec leurs deux identifiants et leurs deux adresses. Deux
        vendeurs différents qui annoncent deux voitures identiques restent donc
        deux opportunités — la clé ne sait pas les distinguer (voir
        `_duplicate_key`) — et c'est à l'appelant de trancher. Le corpus ne
        décide pas à sa place.

        Pour un couple, le marquage est MUTUEL : chaque annonce cite l'autre,
        donc une page qui n'en montre qu'une dit quand même qu'une autre annonce
        décrit la même voiture. Au-delà de deux annonces identiques, chacune cite
        la première des autres dans l'ordre `(source, market, source_id)` : un
        champ ne porte qu'un identifiant, et cet ordre est stable d'un appel à
        l'autre.

        La recherche porte sur TOUT le corpus, pas sur une récolte : « la même
        voiture ailleurs » traverse les récoltes (`listings` est unique par
        identité) et les sources (la clé n'en porte aucune). Une annonce lue par
        une récolte plus ancienne est donc trouvée aussi — sinon le marquage
        dépendrait de l'ordre des appels. La contrepartie, assumée : une
        `source_id` rendue peut désigner une annonce qu'un `offers` restreint à
        une récolte ne montre pas ; la ligne d'en face porte son propre
        `source`, donc l'appelant peut la retrouver.

        Une requête par annonce, comme `market_gaps` et pour la même raison : le
        seau `(make, group, année)` est servi par `idx_listings_cohort`.
        """
        return {listing.key(): self._duplicate_of(listing) for listing in listings}

    def _duplicate_of(self, listing: Listing) -> str | None:
        """La `source_id` de la première autre annonce de la même voiture, ou
        `None`. Appelée une fois par annonce par `duplicate_marks`, qui
        documente la clé, le coût et le sens du marquage.
        """
        key = _duplicate_key(
            listing.market,
            listing.make,
            listing.group,
            listing.year,
            listing.km,
            listing.price,
        )
        if key is None:
            return None
        for row in self._conn.execute(
            _DUPLICATE_BUCKET_SQL,
            (
                listing.make,
                listing.group,
                listing.year,
                listing.market,
                listing.source,
                listing.market,
                listing.source_id,
            ),
        ):
            if (
                _duplicate_key(
                    row["market"], row["make"], row["grp"], row["year"], row["km"], row["price"]
                )
                == key
            ):
                return row["source_id"]
        return None


def _median_of(sorted_prices: Sequence[int]) -> int | None:
    """Médiane d'une série TRIÉE de prix entiers, en euros entiers.

    Effectif impair : la valeur du milieu. Effectif pair : la moyenne des deux
    valeurs du milieu, arrondie à l'euro supérieur quand elle tombe sur un
    demi-euro — `market_median` est un montant entier, comme `price`
    (PLAN.md §8 : euros entiers, jamais recalculés). C'est la médiane de
    `tools._quantile` à q = 0,5, arrondie à l'unité d'affichage.

    `None` sur une série vide : aucune médiane ne décrit zéro annonce.
    """
    if not sorted_prices:
        return None
    middle = len(sorted_prices) // 2
    if len(sorted_prices) % 2:
        return sorted_prices[middle]
    return (sorted_prices[middle - 1] + sorted_prices[middle] + 1) // 2


def _unaccent(value: object) -> object:
    """Libellé comparé sans casse ni accents. `CITROËN` et `Citroën` se valent.

    Le `LOWER` de SQLite n'abaisse que l'ASCII : mesuré, `LOWER('CITROËN')` vaut
    `citroËn`, donc une recherche `Citroën` ne trouvait aucune annonce saisie en
    majuscules accentuées. `unicodedata` retire les marques combinantes (NFKD),
    puis `casefold` règle la casse — y compris `ß` → `ss`, que `lower` ne fait
    pas.

    **La modifier oblige à reconstruire l'index de cohorte.**
    `idx_listings_cohort` (voir `_SCHEMA`) indexe `unaccent(...)`, une
    expression qui appelle CETTE fonction ; SQLite ne sait pas qu'elle a
    changé, donc les entrées déjà écrites gardent le résultat de l'ancienne et
    les lignes concernées cessent de se joindre — sans erreur. Après toute
    modification ici : `REINDEX` sur la base (ou `DROP INDEX` puis réouverture,
    qui le recrée).

    Rend la valeur telle quelle si ce n'est pas une chaîne : la fonction est
    appelée dans du SQL, où `NULL` circule.
    """
    if not isinstance(value, str):
        return value
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def _number(value: object) -> int | float | None:
    """`AVG` rend toujours un flottant : une année médiane de 2021 s'écrirait
    `2021.0` alors que le contrat l'annonce entière, et une médiane de 2020,5
    (effectif pair) doit rester un demi — arrondir inventerait une précision
    que le corpus n'a pas.
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value  # type: ignore[return-value]
