"""Lecture des prix en arrière-plan : un seul fil pour tous les comptes (les prix sont
communs, state/price_cache.json). Les passes ne lisent plus aucun prix : elles prennent
ceux du cache et déposent les manquants dans une file commune ; une carte demandée par
plusieurs comptes n'est lue qu'une fois.

- Ordre : les prix utiles aux ventes et aux prix auto (SALES) et ceux de la défausse
  (DISCARD) à tour de rôle, chacun dans l'ordre des demandes : la défausse avance même
  quand les ventes réclament des milliers de prix.
- Session : celle du Marché s'il y en a une, sinon celle d'un compte qui a demandé la
  carte (le prix moyen ne se lit qu'avec la session d'un compte qui la possède), jamais
  pendant une passe de ce compte.
- Le fil se met en pause dès qu'un compte approche d'une fin d'enchère suivie, et tant
  qu'une fin d'enchère est surveillée (prolongations comprises) : une mise de dernière
  seconde ne doit pas tomber sur un refus du site (trop de requêtes).
- Quand tous les prix de vente demandés par un compte sont lus, sa boucle est réveillée
  pour une passe de vente anticipée (on_ready)."""

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

from . import market
from .config import Config
from .state import now_utc

log = logging.getLogger(__name__)

SALES, DISCARD = 0, 1
SNIPE_GUARD = timedelta(seconds=30)  # pause du fil avant le réveil de fin d'enchère d'un compte
IDLE_WAIT = 2  # s entre deux vérifications quand il n'y a rien à lire
MAX_TRIES = 3  # lectures ratées d'une carte avant de la rendre « inconnue »
RETRY_AFTER = 30  # s avant de réessayer une carte dont la lecture a échoué
RESULTS_KEPT = 200_000  # une défausse demande d'avance toute sa collection

OK, UNKNOWN, STOPPED = "ok", "unknown", "stopped"


def _account(config: Config) -> str:
    return str(config.session_file)


@dataclass
class _Item:
    key: str
    card_id: str
    rarity: str | None
    title: str
    priority: int
    order: int
    requesters: dict[str, Config] = field(default_factory=dict)  # compte → réglages
    tries: int = 0
    not_before: float = 0.0


class PriceWorker:
    def __init__(self, accounts: Callable[[], list[Config]], on_ready: Callable[[Config], None] | None = None):
        self.accounts, self.on_ready = accounts, on_ready
        self._cond = threading.Condition()
        self._items: dict[str, _Item] = {}
        self._results: dict[str, tuple[str, float | None]] = {}
        self._order = itertools.count()
        self._sales: dict[str, set[str]] = {}  # compte → prix de vente encore attendus
        self._fresh: set[str] = set()  # comptes dont un prix de vente vient d'être lu
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.manual = False  # tests : la file est servie par step(), sans fil
        self._turn = SALES  # ventes et défausse servies à tour de rôle

    # ─────────────────────────── Demandes (fils des comptes) ───────────────────────────

    def request(self, group, config: Config, priority: int) -> str:
        """Ajoute la carte à la file (une seule fois, même demandée par plusieurs comptes).
        Un prix déjà lu n'est pas relu ; seul un prix illisible est redemandé."""
        key = f"{group.card_id}:{group.rarity}"
        with self._cond:
            if self._results.get(key, (None,))[0] == OK:
                return key
            self._results.pop(key, None)
            item = self._items.get(key)
            if item is None:
                item = self._items[key] = _Item(key, group.card_id, group.rarity, group.title, priority,
                                                next(self._order))
            elif priority < item.priority:  # demandée aussi pour une vente : sa place dans la file est gardée
                item.priority = priority
            item.requesters[_account(config)] = config
            if priority == SALES:
                self._sales.setdefault(_account(config), set()).add(key)
            self._cond.notify_all()
        return key

    def result(self, key: str) -> tuple[str, float | None] | None:
        """Prix déjà lu (ou illisible) pour cette carte, s'il y en a un."""
        with self._cond:
            return self._results.get(key)

    def wait(self, key: str, should_stop: Callable[[], bool] = lambda: False) -> tuple[str, float | None]:
        """Attend le prix demandé : (OK, prix), (UNKNOWN, None) s'il n'a pas pu être lu, ou
        (STOPPED, None) dès que should_stop() (une passe attend la session)."""
        with self._cond:
            while key not in self._results:
                if should_stop() or self._stop.is_set():
                    return STOPPED, None
                self._cond.wait(1)
            return self._results[key]

    def pending(self) -> int:
        with self._cond:
            return len(self._items)

    # ─────────────────────────── Fil ───────────────────────────

    def start(self) -> "PriceWorker":
        self._thread = threading.Thread(target=self._run, name="prix", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(10)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.step():
                    with self._cond:
                        self._cond.wait(IDLE_WAIT)
            except Exception:
                log.exception("Prix : lecture interrompue par une erreur ; nouvel essai dans 30 s.")
                self._stop.wait(RETRY_AFTER)

    def step(self) -> bool:
        """Lit un prix si possible ; False s'il n'y a rien à lire pour l'instant."""
        if self.snipe_soon():
            return False
        picked = self._pick()
        if picked is None:
            return False
        item, config, site = picked
        self._read(item, config, site)
        return True

    def snipe_soon(self) -> bool:
        """Un compte approche d'une fin d'enchère suivie (ou la surveille déjà, prolongations
        comprises)."""
        from . import watcher
        from .buying import WantedState

        if watcher.busy():
            return True
        limit = now_utc() + SNIPE_GUARD
        for config in self.accounts():
            if config.paused:
                continue
            wake = WantedState(config.wanted_state_file).next_snipe_at(config.snipe_lead)
            if wake is not None and wake <= limit:
                return True
        return False

    def _pick(self):
        """Prochaine carte à lire, avec la session pour la lire : (carte, réglages, site)."""
        from .session import gate

        now = time.monotonic()
        with self._cond:  # à tour de rôle : la classe dont c'est le tour d'abord, chacune dans l'ordre des demandes
            turn, self._turn = self._turn, DISCARD if self._turn == SALES else SALES
            items = sorted((i for i in self._items.values() if i.not_before <= now),
                           key=lambda i: (i.priority != turn, i.order))
        for item in items:
            requesters = list(item.requesters.values())
            if not requesters:
                self._finish(item, UNKNOWN, None)
                continue
            if market.configured(requesters[0]):
                return item, requesters[0], market.market_site(requesters[0])
            for config in requesters:  # sans Marché : la session d'un compte qui possède la carte
                if gate(config).in_pass():
                    continue
                site = _account_site(config)
                if site is not None:
                    return item, config, site
                self._drop(item, config)
        return None

    def _read(self, item: _Item, config: Config, site) -> None:
        from .pricing import PriceReader
        from .site import SessionExpired

        reader = PriceReader(site, config, inline=True)
        group = SimpleNamespace(card_id=item.card_id, rarity=item.rarity, title=item.title)
        try:
            status, average = reader.read(group, self.snipe_soon)
        except SessionExpired:
            log.info("Prix : session du compte %s refusée ; « %s » attend un autre compte.",
                     config.account_name, item.title)
            self._drop(item, config)
            return
        finally:
            reader.save()
        if status == reader.OK:
            self._finish(item, OK, average)
            return
        item.tries += 1
        if status == reader.STOPPED:
            item.tries -= 1  # pause pour une fin d'enchère : pas un échec
        if item.tries >= MAX_TRIES or not reader.api_ok:
            self._finish(item, UNKNOWN, None)
        else:
            item.not_before = time.monotonic() + (0 if status == reader.STOPPED else RETRY_AFTER)

    def _drop(self, item: _Item, config: Config) -> None:
        with self._cond:
            item.requesters.pop(_account(config), None)
            self._sales.get(_account(config), set()).discard(item.key)

    def _finish(self, item: _Item, status: str, average: float | None) -> None:
        ready = []
        with self._cond:
            self._items.pop(item.key, None)
            self._results[item.key] = (status, average)
            while len(self._results) > RESULTS_KEPT:
                self._results.pop(next(iter(self._results)))
            for account, config in item.requesters.items():
                waiting = self._sales.get(account)
                if waiting is None or item.key not in waiting:
                    continue
                waiting.discard(item.key)
                if status == OK:
                    self._fresh.add(account)
                if not waiting and account in self._fresh:  # tous les prix de vente demandés sont lus
                    self._fresh.discard(account)
                    ready.append(config)
            self._cond.notify_all()
        for config in ready:
            log.info("Prix : prix de vente lus pour le compte %s ; passe de vente anticipée.", config.account_name)
            if self.on_ready is not None:
                self.on_ready(config)


def _account_site(config: Config):
    """Site avec la session enregistrée du compte (jamais de demande de cookies ici : la
    passe du compte s'en charge)."""
    from .session import session_store
    from .site import SiteClient, WikiMasters

    store = session_store(config)
    if not store.has_session():
        return None
    return WikiMasters(SiteClient(config.base_url, store), config)


WORKER: PriceWorker | None = None


def start(accounts: Callable[[], list[Config]], on_ready: Callable[[Config], None] | None = None) -> PriceWorker:
    global WORKER
    WORKER = PriceWorker(accounts, on_ready).start()
    log.info("Prix : lecture en arrière-plan, un seul fil pour tous les comptes.")
    return WORKER


def active() -> PriceWorker | None:
    """Le fil des prix, s'il tourne (boucle --loop) ; sinon les prix sont lus sur place."""
    return WORKER if WORKER is not None and (WORKER.running or WORKER.manual) else None
