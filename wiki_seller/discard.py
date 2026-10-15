"""Défausse automatique des cartes de faible valeur, en arrière-plan.

Dès que la collection dépasse DISCARD_TRIGGER exemplaires, un thread à part défausse,
un exemplaire à la fois, toutes les cartes qui valent moins de DISCARD_BELOW sur le
marché (bouton « Défausser » de la fiche carte, depuis « Collection »).

Les passes de vente et la défausse partagent la même session du site : deux
navigateurs qui la renouvellent en même temps peuvent la faire annuler. Le
SessionGate garantit qu'un seul navigateur est ouvert à la fois, la défausse cédant
la place dès qu'une passe (ou une surenchère) attend."""

import threading
from contextlib import contextmanager
from dataclasses import dataclass

from .selection import CardGroup, count_listed, normalize_name

# Raretés défaussées même sans prix moyen connu (aucune vente sur le marché).
DISCARD_WITHOUT_PRICE = ("C", "PC")


class SessionGate:
    """Un seul navigateur à la fois sur la session. Les passes (prioritaires) passent
    avant la défausse, qui vérifie should_yield() entre deux cartes."""

    def __init__(self):
        self._cond = threading.Condition()
        self._busy = False
        self._priority_waiting = 0

    @contextmanager
    def priority(self):
        with self._cond:
            self._priority_waiting += 1
            try:
                while self._busy:
                    self._cond.wait()
            finally:
                self._priority_waiting -= 1
            self._busy = True
        try:
            yield
        finally:
            self._release()

    @contextmanager
    def background(self):
        with self._cond:
            while self._busy or self._priority_waiting:
                self._cond.wait()
            self._busy = True
        try:
            yield
        finally:
            self._release()

    def should_yield(self) -> bool:
        """Vrai si une passe attend la session : la défausse doit s'interrompre."""
        return self._priority_waiting > 0

    def _release(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()


def total_copies(groups: list[CardGroup]) -> int:
    return sum(g.count for g in groups)


@dataclass
class DiscardItem:
    card_id: str
    title: str
    rarity: str | None
    average: float | None  # None : aucune vente connue (C/PC)
    copies: int


# Ordre d'examen : les plus communes d'abord (les plus susceptibles de valoir peu).
RARITY_ORDER = ("C", "PC", "R", "SR", "UR", "L")


def discard_candidates(groups: list[CardGroup], protected_names: set[str], selling: list[dict]) -> list[DiscardItem]:
    """Cartes à examiner pour la défausse, les plus communes d'abord. Jamais les cartes
    protégées (`protected_names` : cartes à garder et cartes voulues), ni les
    exemplaires en vente. Le prix (`average`) est lu ensuite, carte par carte."""
    items = []
    for group in groups:
        if normalize_name(group.title) in protected_names:
            continue
        copies = group.count - count_listed(group, selling)
        if copies > 0:
            items.append(DiscardItem(group.card_id, group.title, group.rarity, None, copies))
    rank = {r: i for i, r in enumerate(RARITY_ORDER)}
    items.sort(key=lambda i: (rank.get(i.rarity or "", len(rank)), normalize_name(i.title)))
    return items


def should_discard(rarity: str | None, average: float | None, below: int) -> bool:
    """Prix moyen connu inférieur à `below`, ou aucune vente connue pour une C/PC.
    (Une carte dont le prix n'a pas pu être lu n'arrive pas jusqu'ici.)"""
    if average:
        return average < below
    return (rarity or "") in DISCARD_WITHOUT_PRICE
