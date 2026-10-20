"""Fins d'enchère surveillées à part (mode --loop) : un fil « veilleur » regarde les
enchères suivies de tous les comptes (wanted_state.json) et lance, à l'heure, une
surveillance par enchère dans son propre fil. Une passe longue, une demande de cookies
sur Telegram ou une autre fin d'enchère ne peuvent donc plus faire manquer une fin.

La session du compte est la même que celle des passes (session_store : un seul
renouvellement à la fois) ; la surveillance passe avant tout le reste : les requêtes des
passes et de la défausse du compte attendent qu'elle soit terminée, et le fil des prix
s'arrête (SessionGate.watching). Elle relit l'enchère chaque seconde (mise actuelle, temps
restant, qui mène) et surenchérit tant que la limite le permet, prolongations comprises.
Elle ne demande jamais de cookies : si la session ne marche plus, la passe suivante s'en
charge."""

import dataclasses
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

POLL = 2  # s entre deux regards sur les enchères suivies
EARLY = timedelta(seconds=5)  # surveillance lancée un peu avant l'heure prévue


class EndWatcher:
    def __init__(self, accounts: Callable[[], list[Config]], dry_run: bool = False):
        self.accounts, self.dry_run = accounts, dry_run
        self._active: dict[tuple[str, str], threading.Thread] = {}  # (compte, carte) → surveillance
        self._budgets: dict[str, object] = {}  # compte → budget partagé par ses surveillances
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "EndWatcher":
        from .buying import WATCH_APART

        WATCH_APART.set()
        self._thread = threading.Thread(target=self._run, name="veilleur", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        from .buying import WATCH_APART

        self._stop.set()
        WATCH_APART.clear()
        if self._thread is not None:
            self._thread.join(10)

    def busy(self) -> bool:
        with self._lock:
            return any(thread.is_alive() for thread in self._active.values())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                log.exception("Veilleur : erreur ; nouvel essai dans quelques secondes.")
            self._stop.wait(POLL)

    def check(self) -> None:
        """Lance la surveillance des fins d'enchère dont l'heure est venue."""
        from .buying import SNIPE_STARTUP, WantedState, watch_start
        from .state import parse_time

        now = datetime.now(timezone.utc)
        for config in self.accounts():
            if config.paused:
                continue
            for key, entry in WantedState(config.wanted_state_file).data["cards"].items():
                end = parse_time(entry.get("end_at"))
                if entry.get("status") != "bidding" or not entry.get("auction_id") or end is None:
                    continue
                if not now - timedelta(minutes=2) < end <= now + watch_start(config.snipe_lead) + SNIPE_STARTUP + EARLY:
                    continue
                with self._lock:
                    running = self._active.get((str(config.session_file), key))
                    if running is not None and running.is_alive():
                        continue
                    thread = threading.Thread(target=self._watch, args=(config, key), daemon=True,
                                              name=f"fin-{config.account_name or 'compte'}")
                    self._active[(str(config.session_file), key)] = thread
                thread.start()

    def _watch(self, config: Config, key: str) -> None:
        from .session import gate

        with gate(config).watching():  # les passes et la défausse du compte attendent
            state = self._watch_end(config, key)
        if state is not None:  # verrou du suivi pris hors surveillance : une passe qui le tient attend celle-ci
            self._merge(config, key, state)

    def _watch_end(self, config: Config, key: str):
        from .buying import AUTO, Budget, WantedState, _Buyer, buy_cards, fellow_accounts, my_user_id
        from .session import session_store
        from .site import SessionExpired, SiteClient, SiteError, WikiMasters

        account = str(config.session_file)
        state = WantedState(config.wanted_state_file)  # écriture atomique : relu sans verrou
        entry = state.data["cards"].get(key)
        card = next((c for c in buy_cards(config) if c.id == key), None)
        if entry is None or card is None or entry.get("status") != "bidding":
            return None
        if card.max_price == AUTO:
            if not entry.get("auto_max"):
                return None
            card = dataclasses.replace(card, max_price=entry["auto_max"])
        dry_run = self.dry_run or config.simulation
        site = WikiMasters(SiteClient(config.base_url, session_store(config)), config)
        try:
            offset = site.server_clock_offset()
            mine = site.fetch_mine()
            state.my_id = my_user_id(mine, state.my_id) or site.user_id()
            with self._lock:  # budget commun aux surveillances simultanées du compte
                if not any(t.is_alive() and t is not threading.current_thread()
                           for (a, _), t in self._active.items() if a == account):
                    self._budgets[account] = Budget(sum(
                        int(a.get("current_bid") or 0) for a in mine.get("bidding") or []
                        if isinstance(a, dict) and state.my_id and a.get("current_bidder_id") == state.my_id))
                budget = self._budgets.setdefault(account, Budget())
            buyer = _Buyer(site, config, state, dry_run, lambda: datetime.now(timezone.utc) + offset, budget)
            _, buyer.rivals = fellow_accounts(config)
            auction = site.read_auction(entry["auction_id"]) or {
                "id": entry["auction_id"], "end_at": entry.get("end_at"), "status": "active"}
            buyer.watch_until_end(card, entry, auction)
        except SessionExpired as exc:
            log.warning("Fin d'enchère de « %s » non surveillée : session refusée (%s) ; la passe suivante "
                        "demandera de nouveaux cookies si besoin.", card.name, exc)
        except (SiteError, OSError) as exc:
            log.warning("Fin d'enchère de « %s » : site injoignable (%s).", card.name, exc)
        return state

    @staticmethod
    def _merge(config: Config, key: str, state) -> None:
        """Résultat de la surveillance reporté dans le suivi (relu : une passe a pu
        l'enregistrer entre-temps), sans toucher aux autres cartes."""
        from .buying import WantedState, wanted_lock

        with wanted_lock(config):
            fresh = WantedState(config.wanted_state_file)
            if key in state.data["cards"]:
                fresh.data["cards"][key] = state.data["cards"][key]
            else:  # achat terminé : la carte a quitté les achats
                fresh.data["cards"].pop(key, None)
            fresh.my_id = fresh.my_id or state.my_id
            fresh.save()


WATCHER: EndWatcher | None = None


def start(accounts: Callable[[], list[Config]], dry_run: bool = False) -> EndWatcher:
    global WATCHER
    WATCHER = EndWatcher(accounts, dry_run).start()
    log.info("Fins d'enchère surveillées à part, chacune dans son fil.")
    return WATCHER


def busy() -> bool:
    """Une fin d'enchère est en cours de surveillance (tous comptes confondus)."""
    return WATCHER is not None and WATCHER.busy()


def active() -> bool:
    return WATCHER is not None and WATCHER._thread is not None and WATCHER._thread.is_alive()
