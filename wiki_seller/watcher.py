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
        # (compte, ligne) → gains et verrou partagés par les fins d'enchère d'une même ligne
        # (x2 : deux fins simultanées comptées l'une après l'autre, l'achat terminé une fois).
        self._lines: dict[tuple[str, str], tuple[dict, threading.RLock]] = {}
        # (compte, enchère suivie) → heure de fin déjà surveillée jusqu'au bout : pas relancée
        # (site muet après la fin : la passe suivante lira le résultat), sauf fin repoussée.
        self._finished: dict[tuple[str, str], tuple[str | None, str | None]] = {}
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
                    if self._finished.get((str(config.session_file), key)) == (entry.get("auction_id"),
                                                                                entry.get("end_at")):
                        continue
                    thread = threading.Thread(target=self._watch, args=(config, key), daemon=True,
                                              name=f"fin-{config.account_name or 'compte'}")
                    self._active[(str(config.session_file), key)] = thread
                thread.start()

    def _watch(self, config: Config, key: str) -> None:
        from .buying import WantedState
        from .session import gate
        from .state import now_utc, parse_time

        start = WantedState(config.wanted_state_file).data["cards"].get(key) or {}
        auction_id, end_at = start.get("auction_id"), start.get("end_at")
        with gate(config).watching():  # les passes et la défausse du compte attendent
            state = self._watch_end(config, key)
        if state is not None:  # fin repoussée pendant la surveillance : c'est la dernière vue qui compte
            end_at = (state.data["cards"].get(key) or {}).get("end_at") or end_at
        end = parse_time(end_at)
        if end is not None and now_utc() >= end:
            with self._lock:
                self._finished[(str(config.session_file), key)] = (auction_id, end_at)
        if state is not None:  # verrou du suivi pris hors surveillance : une passe qui le tient attend celle-ci
            self._merge(config, key, state)

    def _watch_end(self, config: Config, key: str):
        from .buying import AUTO, Budget, WantedState, _Buyer, buy_cards, fellow_accounts, line_of, my_user_id
        from .session import session_store
        from .site import SessionExpired, SiteClient, SiteError, WikiMasters

        account = str(config.session_file)
        state = WantedState(config.wanted_state_file)  # écriture atomique : relu sans verrou
        entry = state.data["cards"].get(key)
        card = next((c for c in buy_cards(config) if c.id == line_of(key)), None)
        if entry is None or card is None or entry.get("status") != "bidding":
            return None
        if card.max_price == AUTO:  # prix max auto : noté sur la ligne
            auto_max = (state.data["cards"].get(card.id) or {}).get("auto_max")
            if not auto_max:
                return None
            card = dataclasses.replace(card, max_price=auto_max)
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
            # Une autre fin d'enchère de la même ligne a pu être gagnée entre-temps : gains du
            # fichier (relu sans verrou : écriture atomique) et des autres surveillances.
            with self._lock:
                shared, buyer.lock = self._lines.setdefault((account, card.id), ({}, threading.RLock()))

            def reload_line(c) -> dict:
                saved = WantedState(config.wanted_state_file).data["cards"].get(c.id) or {}
                return {"won": {**(saved.get("won") or {}), **shared}}

            buyer.reload_line = reload_line
            buyer.publish_won = lambda c, won: shared.update(won)
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
        """Résultat de la surveillance reporté dans le suivi (relu : une passe, ou une autre
        fin d'enchère de la même ligne, a pu l'enregistrer entre-temps), sans toucher aux
        autres enchères. Gains et pertes de la ligne s'additionnent."""
        from .buying import LOST_KEPT, WantedState, buy_cards, line_of, wanted_lock

        line_key = line_of(key)
        with wanted_lock(config):
            fresh = WantedState(config.wanted_state_file)
            cards, mine = fresh.data["cards"], state.data["cards"]
            if line_key not in mine or line_key not in {c.id for c in buy_cards(config)}:
                # achat terminé (par cette fin d'enchère ou une autre) : la ligne et ses enchères quittent le suivi
                fresh.forget_line(line_key)
            else:
                before = cards.get(line_key) or {}
                if key in mine:
                    cards[key] = mine[key]
                else:
                    cards.pop(key, None)
                line = cards.get(line_key)
                if line is not None:  # gains et pertes : ceux déjà enregistrés et ceux de cette surveillance
                    won = {**(before.get("won") or {}), **(mine[line_key].get("won") or {})}
                    lost = list(dict.fromkeys([*(before.get("lost") or []), *(mine[line_key].get("lost") or [])]))
                    if won:
                        line["won"] = won
                    if lost:
                        line["lost"] = lost[-LOST_KEPT:]
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
