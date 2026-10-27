"""Achat des cartes de state/buy_cards.json (/buy, /buyandsell) : on mise sur une
enchère dans la limite de prix, on surenchérit si quelqu'un passe devant, et on revient
SNIPE_LEAD avant la fin. Le suivi (enchères choisies, heure de fin, exemplaires obtenus)
est enregistré dans state/wanted_state.json.

Une carte voulue en plusieurs exemplaires (x3) est suivie sur autant d'enchères à la fois
qu'il reste d'exemplaires à obtenir : la ligne (clé card.id) porte les exemplaires
obtenus, les enchères perdues et sa première enchère ; les autres ont leur propre entrée
(« card.id#2 », « card.id#3 »…).

Une fois tous les exemplaires obtenus, la carte quitte la liste d'achat et devient
protégée, ou part dans les ventes au prix demandé (/buyandsell)."""

import dataclasses
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from . import collection
from .config import Config
from .lists import AUTO, AUTO_BUY_RATIO, AUTO_SELL_RATIO, SHINY, BuyCard, buy_cards, complete_purchase, price_text
from .selection import is_shiny
from .pricing import PriceReader, reliable
from .selection import normalize_name
from .site import HUMAN_CHECK_CODE, SiteError, entry_rarity, min_bid
from .state import parse_time, read_json, write_json
from .telegram import notify

log = logging.getLogger(__name__)

# Enchère qu'on laisse passer : trop proche de la fin pour miser à temps.
TOO_LATE = timedelta(seconds=5)
# Réveil avant l'heure de mise : vérifier la session et relire les enchères prend un peu de temps.
SNIPE_STARTUP = timedelta(seconds=15)
# Surveillance de la fin d'enchère : à partir de watch_start() avant la fin, l'enchère est
# relue toutes les WATCH_POLL secondes ; on surenchérit à SNIPE_LEAD de la fin, puis à
# chaque fois qu'on est dépassé, jusqu'à la vraie fin.
WATCH_START = timedelta(seconds=60)
WATCH_POLL = 1  # secondes entre deux vérifications pendant la fin d'enchère
WATCH_GUARD = timedelta(minutes=60)  # guerre d'enchères : chaque mise ajoute une minute
# Le site ne répond pas toujours : une mise est renvoyée jusqu'à la fin de l'enchère, et
# au plus BID_RETRY_LIMIT pendant une passe.
BID_RETRY_LIMIT = timedelta(minutes=2)
# Fin d'une enchère surveillée : le site met parfois un moment à désigner le gagnant ;
# l'enchère est relue toutes les OUTCOME_POLL s, au plus OUTCOME_WAIT.
OUTCOME_WAIT = timedelta(minutes=3)
OUTCOME_POLL = 3
# Après la fin, la mise est jouée : si le site ne répond plus, l'enchère n'est relue que
# toutes les SILENT_POLL s, au plus SILENT_WAIT après la fin ; la passe suivante lira le
# résultat (plus de requêtes ne ferait qu'encombrer un site déjà en peine).
SILENT_POLL = 15
SILENT_WAIT = timedelta(minutes=1)
# Une mise des 10 dernières secondes prolonge l'enchère de 60 s : tant que le site la dit
# active, elle reste surveillée, même après l'heure de fin (prolongation vue en retard).
BID_GRACE = timedelta(seconds=3)  # mise encore tentée jusqu'à ce délai après la fin lue
BID_REREAD = 0.3  # s avant de relire une enchère dont la mise vient d'être refusée
BID_TOO_LOW_TRIES = 3  # mises refaites aussitôt au minimum indiqué par le site
LOST_KEPT = 50  # enchères perdues retenues par carte, pour ne les annoncer qu'une fois


def watch_start(lead: timedelta) -> timedelta:
    """Début de la surveillance : au moins 30 s avant l'heure de la surenchère."""
    return max(WATCH_START, lead + timedelta(seconds=30))


def auction_title(auction: dict) -> str:
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    return card.get("wikipedia_title") or card.get("title") or ""


def min_next_bid(auction: dict) -> int:
    """Mise minimale exacte, calculée comme la page de l'enchère."""
    return min_bid(auction)


def choose_auction(auctions: list[dict], card: BuyCard, my_id: str | None, now: datetime,
                   my_sales: set[str] = frozenset(), exclude: set[str] = frozenset(),
                   rivals: set[str] = frozenset()) -> dict | None:
    """Enchère active de cette carte, pas à nous, dans la limite, que ne mène aucun autre
    compte du bot (rivals) : la moins chère, puis celle qui finit le plus tôt."""
    candidates = []
    for auction in auctions:
        end = parse_time(auction.get("end_at"))
        if (normalize_name(auction_title(auction)) != card.key or auction.get("status", "active") != "active"
                or (my_id and auction.get("seller_id") == my_id) or auction.get("id") in my_sales
                or auction.get("id") in exclude
                or (auction.get("current_bidder_id") and auction.get("current_bidder_id") in rivals)
                or (card.seller and not seller_matches(auction, card.seller))
                or not rarity_matches(auction, card)
                or not end or end - now < TOO_LATE or min_next_bid(auction) > card.max_price):
            continue
        candidates.append((min_next_bid(auction), end, auction))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2] if candidates else None


SELLER_FIELDS = ("username", "pseudo", "display_name", "name", "id")


def seller_names(auction: dict) -> set[str]:
    """Noms (et identifiant) du vendeur d'une enchère, tels que le site les donne."""
    seller = auction.get("seller") if isinstance(auction.get("seller"), dict) else {}
    profile = auction.get("seller_profile") if isinstance(auction.get("seller_profile"), dict) else {}
    values = [auction.get(k) for k in ("seller_username", "seller_name", "seller_pseudo", "seller_id")]
    values += [d.get(k) for d in (seller, profile) for k in SELLER_FIELDS]
    return {normalize_name(str(v)).lstrip("@") for v in values if isinstance(v, (str, int)) and str(v).strip()}


def seller_matches(auction: dict, seller: str) -> bool:
    return normalize_name(seller).lstrip("@") in seller_names(auction)


def rarity_matches(auction: dict, card: BuyCard, unknown_ok: bool = False) -> bool:
    """Enchère de la rareté voulue (toujours vrai sans rareté demandée) : L+ = légendaire
    shiny ; une autre rareté exclut les shiny."""
    if not card.rarity:
        return True
    rarity = entry_rarity(auction)
    wanted = "L" if card.rarity == SHINY else card.rarity
    if card.rarity == SHINY and not is_shiny(auction):
        return False
    if card.rarity != SHINY and is_shiny(auction):
        return False
    return rarity == wanted or (unknown_ok and not rarity)


def won_since(mine: dict, card: BuyCard, since: datetime | None) -> list[dict]:
    """Enchères de cette carte dans « Gagnées », réglées depuis `since`."""
    found = []
    for auction in mine.get("won") or []:
        if (not isinstance(auction, dict) or normalize_name(auction_title(auction)) != card.key
                or not rarity_matches(auction, card, unknown_ok=True)):
            continue
        settled = parse_time(auction.get("settled_at")) or parse_time(auction.get("end_at"))
        if since is None or (settled and settled >= since):
            found.append(auction)
    return found


def my_user_id(mine: dict, known: str | None) -> str | None:
    if known:
        return known
    for key, field in (("selling", "seller_id"), ("won", "winner_id"), ("history", "seller_id")):
        for auction in mine.get(key) or []:
            if isinstance(auction, dict) and isinstance(auction.get(field), str):
                return auction[field]
    return None


# ─────────────────────────── Plusieurs comptes ───────────────────────────
# Deux comptes du bot ne misent jamais l'un contre l'autre : une enchère suivie par un
# compte (enregistrée dans son wanted_state.json, ou choisie pendant sa passe en cours)
# est laissée aux autres, de même que celles qu'un autre compte du bot mène.

_CLAIMS: dict[str, str] = {}  # enchère → dossier du compte qui l'a choisie (passe en cours)
_CLAIMS_LOCK = threading.Lock()


def fellow_accounts(config: Config) -> tuple[set[str], set[str]]:
    """(enchères suivies par les autres comptes du bot, leurs identifiants sur le site)."""
    if config.shared_dir is None:
        return set(), set()
    me = str(config.state_dir)
    users = read_json(config.shared_state_dir / "users.json", {}).get("users") or {}
    auctions, ids = set(), set()
    for chat_id in users:
        folder = config.shared_state_dir / "users" / chat_id
        if str(folder) == me:
            continue
        data = read_json(folder / "wanted_state.json", {})
        if data.get("my_id"):
            ids.add(data["my_id"])
        auctions |= {e["auction_id"] for e in (data.get("cards") or {}).values()
                     if isinstance(e, dict) and e.get("auction_id") and e.get("status") == "bidding"}
    with _CLAIMS_LOCK:
        auctions |= {a for a, owner in _CLAIMS.items() if owner != me}
    return auctions, ids


def claim(config: Config, auction_id: str) -> bool:
    """Réserve l'enchère pour ce compte ; False si un autre compte l'a choisie avant."""
    with _CLAIMS_LOCK:
        return _CLAIMS.setdefault(auction_id, str(config.state_dir)) == str(config.state_dir)


def release_claims(config: Config) -> None:
    """Fin de passe : le wanted_state.json enregistré prend le relais."""
    with _CLAIMS_LOCK:
        for auction_id in [a for a, owner in _CLAIMS.items() if owner == str(config.state_dir)]:
            del _CLAIMS[auction_id]


SLOT = "#"  # « tour eiffel|L#2 » : deuxième enchère suivie de la ligne « tour eiffel|L »
SLOT_FIELDS = ("auction_id", "end_at", "status", "my_bid", "placed", "watch_price")


def line_of(key: str) -> str:
    """Ligne d'achat (card.id) d'une entrée du suivi."""
    return key.split(SLOT, 1)[0]


def _slot_number(key: str) -> int:
    _, sep, number = key.partition(SLOT)
    return int(number) if sep and number.isdigit() else 1


class WantedState:
    def __init__(self, path: Path):
        self.path = path
        self.data = read_json(path, {})
        self.data.setdefault("cards", {})

    @property
    def my_id(self) -> str | None:
        return self.data.get("my_id")

    @my_id.setter
    def my_id(self, value: str | None) -> None:
        if value:
            self.data["my_id"] = value

    def card(self, card: BuyCard) -> dict:
        return self.data["cards"].setdefault(card.id, {"name": card.name})

    def slots(self, card: BuyCard) -> list[tuple[str, dict]]:
        """Enchères suivies de la ligne : (clé, entrée), la ligne elle-même d'abord."""
        found = [(k, e) for k, e in self.data["cards"].items() if line_of(k) == card.id and isinstance(e, dict)]
        return sorted(found, key=lambda item: _slot_number(item[0]))

    def free_slot(self, card: BuyCard, busy: set[str] = frozenset()) -> tuple[str, dict]:
        """Une entrée sans enchère de la ligne (hors `busy`), créée s'il le faut."""
        for key, entry in self.slots(card):
            if key not in busy and not entry.get("auction_id"):
                return key, entry
        number = 2
        while f"{card.id}{SLOT}{number}" in self.data["cards"]:
            number += 1
        key = f"{card.id}{SLOT}{number}"
        self.data["cards"][key] = {"name": card.name, "status": "searching"}
        return key, self.data["cards"][key]

    def drop_empty_slots(self, card: BuyCard) -> None:
        for key, entry in self.slots(card):
            if key != card.id and not entry.get("auction_id"):
                del self.data["cards"][key]

    def forget_line(self, card_id: str) -> None:
        for key in [k for k in self.data["cards"] if line_of(k) == card_id]:
            del self.data["cards"][key]

    def save(self) -> None:
        write_json(self.path, self.data)

    def next_snipe_at(self, lead: timedelta, now: datetime | None = None,
                      offset: timedelta = timedelta(0)) -> datetime | None:
        """Heure (horloge locale) à laquelle se réveiller pour la prochaine fin d'enchère
        suivie encore à venir. Si ce moment est déjà passé (fin repoussée d'une minute à
        chaque surenchère), c'est maintenant."""
        now = now or datetime.now(timezone.utc)
        ends = [parse_time(c.get("end_at")) for c in self.data["cards"].values()
                if c.get("status") == "bidding"]
        ends = [e - offset for e in ends if e and e - offset > now]
        if not ends:
            return None
        return max(min(ends) - watch_start(lead) - SNIPE_STARTUP, now)


# Fins d'enchère surveillées à part (watcher.py, mode --loop) : les passes ne les
# surveillent plus elles-mêmes et laissent tranquilles les cartes dont la fin approche.
WATCH_APART = threading.Event()
_WANTED_LOCKS: dict[str, threading.RLock] = {}
_WANTED_LOCKS_GUARD = threading.Lock()


def wanted_lock(config: Config) -> threading.RLock:
    """Verrou du suivi des achats d'un compte (wanted_state.json) : la passe et les
    surveillances de fin d'enchère ne s'écrasent pas l'une l'autre."""
    with _WANTED_LOCKS_GUARD:
        return _WANTED_LOCKS.setdefault(str(config.wanted_state_file), threading.RLock())


def in_watch_window(entry: dict, lead: timedelta, now: datetime) -> bool:
    """Fin d'enchère suivie assez proche pour être (ou bientôt être) surveillée à part."""
    end = parse_time(entry.get("end_at"))
    return (entry.get("status") == "bidding" and end is not None
            and now - timedelta(minutes=3) < end <= now + watch_start(lead) + SNIPE_STARTUP + timedelta(minutes=2))


def run_bids(site, config: Config, dry_run: bool, snipe_only: bool = False) -> None:
    """Une passe d'achat. snipe_only : ne traiter que les enchères suivies qui se
    terminent bientôt : attendre SNIPE_LEAD avant la fin, puis surveiller l'enchère
    jusqu'à sa vraie fin (le site la prolonge de 60 s après une mise tardive)."""
    with wanted_lock(config):
        _run_bids(site, config, dry_run, snipe_only)


def _run_bids(site, config: Config, dry_run: bool, snipe_only: bool) -> None:
    wanted = buy_cards(config)
    state = WantedState(config.wanted_state_file)
    for card in wanted:  # ancien suivi, au nom seul, d'une carte achetée dans une seule rareté
        if card.id != card.key and card.id not in state.data["cards"] and card.key in state.data["cards"] \
                and not any(c.id == card.key for c in wanted):
            state.data["cards"][card.id] = state.data["cards"].pop(card.key)
    ids = {c.id for c in wanted}
    dropped = [k for k in state.data["cards"] if line_of(k) not in ids]  # /unbuy : suivi oublié
    if dropped and not dry_run:
        for key in dropped:
            del state.data["cards"][key]
        state.save()
    if not wanted:
        return
    offset = site.server_clock_offset()
    buyer = _Buyer(site, config, state, dry_run, lambda: datetime.now(timezone.utc) + offset)
    mine = site.fetch_mine()
    # Sans vente ni gain, l'identifiant vient de la session : sans lui, le bot ne se saurait pas en tête.
    state.my_id = my_user_id(mine, state.my_id) or site.user_id()
    bidding = [a for a in mine.get("bidding") or [] if isinstance(a, dict)]
    buyer.budget_used = sum(int(a.get("current_bid") or 0) for a in bidding
                            if state.my_id and a.get("current_bidder_id") == state.my_id
                            and a.get("status") == "active")
    pass_info = SimpleNamespace(mine=mine, bidding=bidding, bidding_ids={a.get("id") for a in bidding},
                                my_sales={a.get("id") for a in mine.get("selling") or [] if isinstance(a, dict)})
    pass_info.taken, buyer.rivals = fellow_accounts(config)
    try:
        for card in wanted:
            _buy_line(buyer, card, pass_info, snipe_only)
        buyer.run_watches()  # fins d'enchère proches, toutes en même temps
    finally:
        state.save()
        release_claims(config)


def _buy_line(buyer: "_Buyer", card: BuyCard, info: SimpleNamespace, snipe_only: bool) -> None:
    """Une ligne d'achat : autant d'enchères suivies à la fois qu'il reste d'exemplaires à
    obtenir. Vos propres enchères sur la carte (misées à la main) sont reprises d'abord,
    celles que vous menez en priorité ; puis les moins chères du Marché dans la limite."""
    state, site, config = buyer.state, buyer.site, buyer.config
    line = state.card(card)
    line.setdefault("since", buyer.now().isoformat())
    now = buyer.now()
    near = {k for k, e in state.slots(card)
            if WATCH_APART.is_set() and not snipe_only and in_watch_window(e, config.snipe_lead, now)}
    if near:  # ces fins d'enchère comptent elles-mêmes leurs gains (veilleur)
        log.info("Achat : « %s » — %d fin(s) d'enchère proche(s), surveillée(s) à part.", card.name, len(near))
    elif buyer.count_wins(card, line, won_since(info.mine, card, parse_time(line["since"]))):
        buyer.complete(card)
        return
    card = buyer.resolve_max(card, line, snipe_only)  # prix max « auto »
    if card is None:
        return
    remaining = card.copies - len(line.get("won") or {})

    def active() -> list[tuple[str, dict]]:
        return [(k, e) for k, e in state.slots(card) if e.get("status") == "bidding" and e.get("auction_id")]

    # Le site laisse un moment les enchères finies dans « Mes enchères » : celles déjà
    # gagnées, perdues ou terminées ne sont pas reprises.
    done = set(line.get("won") or {}) | set(line.get("lost") or [])
    mine_for_card = [a for a in info.bidding if normalize_name(auction_title(a)) == card.key
                     and a.get("status", "active") == "active" and a.get("id") not in done
                     and rarity_matches(a, card, unknown_ok=True) and not ended(a, now)]
    leading_ids = {a["id"] for a in mine_for_card if buyer.leading(a)}
    tracked = {e.get("auction_id") for _, e in state.slots(card)}
    for auction in sorted((a for a in mine_for_card if a["id"] not in tracked), key=lambda a: a["id"] not in leading_ids):
        target = None
        if len(active()) < remaining:
            target = state.free_slot(card, near)
        elif auction["id"] in leading_ids:  # à la place d'une enchère suivie que vous ne menez pas
            target = next(((k, e) for k, e in reversed(active())
                           if k not in near and e["auction_id"] not in leading_ids), None)
            if target is not None:
                log.info("Achat : « %s » — vous menez déjà une autre enchère de cette carte ; elle est suivie "
                         "à la place.", card.name)
        if target is not None:
            target[1].update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding")

    abandoned: set[str] = set()
    for key, slot in state.slots(card):
        if key in near or not slot.get("auction_id"):
            continue
        if buyer.follow(card, line, slot, info, abandoned, snipe_only) == "complete":
            return

    extra = len(active()) - remaining  # exemplaires voulus réduits depuis : enchères en trop laissées
    for key, slot in reversed(active()):
        if extra <= 0:
            break
        if key not in near and slot["auction_id"] not in leading_ids:
            log.info("Achat : « %s » — enchère en trop pour les exemplaires voulus ; elle est laissée.", card.name)
            _forget(slot)
            extra -= 1

    needed = remaining - len(active())
    if not snipe_only and needed > 0:
        found = site.search_auctions(card.name)
        picked: set[str] = set()
        while needed > 0:  # une enchère par exemplaire manquant (même abandonnée ou simulée aussitôt)
            own = {e.get("auction_id") for e in state.data["cards"].values() if e.get("auction_id")}
            auction = choose_auction(found, card, state.my_id, now, info.my_sales,
                                     abandoned | info.taken | own | picked, buyer.rivals)
            if auction is None:
                buyer.none_found(card, line, found, abandoned, len(active()), remaining, info.my_sales)
                break
            picked.add(auction["id"])
            if not claim(config, auction["id"]):
                continue  # choisie au même instant par un autre compte
            needed -= 1
            line.pop("blocked_notified", None)
            _, slot = state.free_slot(card, near)
            if buyer.leading(auction) or config.bid_now:  # mise tout de suite (mise_directe)
                buyer.bid_now(card, slot, auction)
            else:  # gardée et surveillée : la mise part juste avant la fin
                buyer.track(card, slot, auction, outbid=False)
    state.drop_empty_slots(card)


def card_ref(auction: dict) -> dict | None:
    """{"id", "rarity"} de la carte d'une enchère, pour lire son prix."""
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    card_id = auction.get("card_id") or card.get("id")
    return {"id": str(card_id), "rarity": entry_rarity(auction)} if card_id else None


def ended(auction: dict, now: datetime) -> bool:
    end = parse_time(auction.get("end_at"))
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end is not None and end < now


def _forget(entry: dict) -> None:
    entry.pop("my_bid", None)
    entry.pop("auction_id", None)
    entry.pop("end_at", None)
    entry["status"] = "searching"


class Budget:
    """Somme des mises en cours d'un compte (budget MAX_TOTAL_BIDS), réservée avant
    chaque mise de fin d'enchère."""

    def __init__(self, used: int = 0):
        self.used, self.lock = used, threading.Lock()


class _Buyer:
    def __init__(self, site, config: Config, state: WantedState, dry_run: bool, now, budget=None):
        self.site, self.config, self.state, self.dry_run, self.now = site, config, state, dry_run, now
        self.budget = budget or Budget()  # partagé par les fins d'enchère surveillées en même temps
        self.rivals: set[str] = set()  # identifiants des autres comptes du bot
        self.watches: list[tuple[BuyCard, dict, dict]] = []  # fins d'enchère à surveiller
        self.lock = threading.RLock()  # plusieurs fins d'enchère d'une même ligne : gains comptés un à un
        self.reload_line = None  # veilleur : gains de la ligne vus par les autres fins d'enchère
        self.publish_won = None  # veilleur : gains de la ligne partagés avec les autres fins d'enchère

    @property
    def budget_used(self) -> int:
        return self.budget.used

    @budget_used.setter
    def budget_used(self, value: int) -> None:
        self.budget.used = value

    def watch_later(self, card: BuyCard, entry: dict, auction: dict) -> None:
        self.watches.append((card, entry, auction))

    def run_watches(self) -> None:
        """Surveille les fins d'enchère toutes en même temps, chacune dans son fil : deux
        fins rapprochées ne se gênent pas (watch_until_end rend la main tout de suite
        pour une fin encore loin)."""
        jobs, self.watches = self.watches, []
        if len(jobs) <= 1:
            for job in jobs:
                self.watch_until_end(*job)
            return
        errors: list[BaseException] = []

        def watch(job) -> None:
            try:
                self.watch_until_end(*job)
            except Exception as exc:  # session expirée… : relancée après les autres
                errors.append(exc)

        threads = [threading.Thread(target=watch, args=(job,), daemon=True,
                                    name=f"{threading.current_thread().name}·{job[0].name[:20]}") for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]

    def follow(self, card: BuyCard, line: dict, slot: dict, info, abandoned: set[str], snipe_only: bool) -> str:
        """Une enchère suivie de la ligne, relue : gagnée, perdue, laissée à un autre compte du
        bot, dépassant la limite (abandonnée) ou gardée jusqu'à la fin. « complete » si
        l'achat est terminé."""
        auction_id = slot["auction_id"]
        try:
            auction = self.site.fetch_auction(auction_id)
        except SiteError as exc:  # le site ne répond pas pour cette enchère
            if not snipe_only:
                log.warning("Achat : « %s » — enchère illisible (%s) ; nouvel essai plus tard.", card.name, exc)
                return ""
            # Fin d'enchère : on la surveille quand même, elle sera relue chaque seconde.
            auction = {"id": auction_id, "end_at": slot.get("end_at"), "status": "active"}
        if auction and auction.get("status") != "active":
            if self.state.my_id and auction.get("winner_id") == self.state.my_id:
                if self.count_wins(card, line, [auction]):
                    self.complete(card)
                    return "complete"
                return ""
            log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
            if self.state.my_id and auction.get("winner_id"):
                self.lost(card, slot, auction, force=True)
            auction = None
        if auction is not None and not self.leading(auction) and (
                auction["id"] in info.taken or auction.get("current_bidder_id") in self.rivals):
            log.info("Achat : « %s » — enchère suivie ou menée par un autre compte du bot ; elle lui est "
                     "laissée.", card.name)
            abandoned.add(auction["id"])
            auction = None
        if auction is None:
            _forget(slot)
            return ""
        if snipe_only:
            self.watch_later(card, slot, auction)
            return ""
        claim(self.config, auction["id"])
        if self.leading(auction):  # misée à la main, ou déjà menée
            self.bid_now(card, slot, auction)
            return ""
        # Enchère déjà suivie : dépassé, surenchère seulement à la fin (réveil SNIPE_LEAD
        # avant) ; pas encore misé, mise tout de suite avec mise_directe, sinon à la fin.
        outbid = auction["id"] in info.bidding_ids or slot.get("placed") == auction["id"]
        minimum = min_next_bid(auction)
        if minimum <= card.max_price:
            if self.config.bid_now and not outbid:
                self.bid_now(card, slot, auction)
            else:
                self.track(card, slot, auction, outbid)
            return ""
        log.info("Achat : « %s » dépasse la limite (%d > %d) ; abandon de cette enchère.",
                 card.name, minimum, card.max_price)
        self.lost(card, slot, auction, force=outbid)  # perdue seulement si vous y aviez misé
        abandoned.add(auction["id"])
        _forget(slot)
        return ""

    def none_found(self, card: BuyCard, line: dict, found: list[dict], abandoned: set[str], active: int,
                   remaining: int, my_sales: set[str]) -> None:
        """Pas (ou plus) d'enchère à prendre pour cette ligne : journal, et autre compte du
        bot prévenu s'il tient les seules possibles."""
        more = f" ({active} suivie(s) sur {remaining} voulue(s))" if remaining > 1 else ""
        log.info("Achat : aucune %senchère de « %s »%s à %d ou moins%s%s.", "autre " if active else "", card.name,
                 f" {card.rarity}" if card.rarity else "", card.max_price,
                 f" chez @{card.seller}" if card.seller else "", more)
        if active:
            return
        same_card = [a for a in found if normalize_name(auction_title(a)) == card.key]
        if card.seller and same_card:  # pour vérifier comment le site nomme les vendeurs
            log.info("Achat : « %s » — vendeurs des enchères en cours : %s", card.name,
                     ", ".join(sorted(set().union(*(seller_names(a) for a in same_card))))[:300])
        if card.rarity and same_card:
            log.info("Achat : « %s » — raretés des enchères en cours : %s", card.name,
                     ", ".join(sorted({entry_rarity(a) or "inconnue" for a in same_card})))
        if choose_auction(found, card, self.state.my_id, self.now(), my_sales, abandoned) is not None:
            self.blocked_by_fellow(card, line)

    def count_wins(self, card: BuyCard, entry: dict, wins: list[dict]) -> bool:
        """Note les enchères gagnées (entry : la ligne) ; True si le nombre d'exemplaires
        voulu est atteint."""
        if entry.get("status") == "won" and "won" not in entry:  # ancien format : déjà obtenue
            entry["won"] = {entry.get("auction_id") or "avant": entry.get("price")}
        saved = self.reload_line(card) if self.reload_line else None  # gains d'une autre fin d'enchère
        won = {**((saved or {}).get("won") or {}), **(entry.get("won") or {})}
        for auction in wins:
            if auction.get("id") and auction["id"] not in won:
                price = auction.get("final_price") or auction.get("current_bid")
                won[auction["id"]] = price
                log.info("Achat : « %s » obtenue pour %s (%d/%d).", card.name, price, len(won), card.copies)
                if not self.dry_run:
                    resale = f" · revente à {price_text(card.sell_price, AUTO_SELL_RATIO)}" if card.sell_price else ""
                    notify(self.config, f"🛒 Obtenue : {card.name} pour {price} ({len(won)}/{card.copies}){resale}")
        entry["won"] = won
        if self.publish_won:
            self.publish_won(card, won)
        if len(won) >= card.copies:
            if entry.get("status") != "won":
                entry.update(status="won")
                entry.pop("auction_id", None)
                entry.pop("end_at", None)
            return True
        if entry.get("status") == "won":  # « copies » augmenté depuis
            _forget(entry)
        for _, slot in self.state.slots(card):  # enchères gagnées : plus à suivre
            if slot.get("auction_id") in won:
                _forget(slot)
        return False

    def complete(self, card: BuyCard) -> None:
        """Tous les exemplaires obtenus : la carte quitte les achats (et leur suivi) pour
        les cartes protégées, ou pour les ventes (/buyandsell)."""
        bought = len(self.state.card(card).get("won") or {})
        if self.dry_run:
            log.info("Simulation : achat de « %s » terminé ; elle passerait %s.", card.name,
                     f"dans les ventes à {card.sell_price}" if card.sell_price else "dans les cartes protégées")
            return
        message = complete_purchase(self.config, card, bought)
        collection.forget(self.config)  # carte obtenue : collection relue à la passe suivante
        self.state.forget_line(card.id)  # redemandée plus tard, elle repartira de zéro
        if message is None:  # déjà terminé par une autre fin d'enchère de la ligne
            return
        log.info("Achat : %s", message)
        notify(self.config, message)

    def resolve_max(self, card: BuyCard, entry: dict, snipe_only: bool) -> BuyCard | None:
        """Prix max « auto » : AUTO_BUY_RATIO du prix du marché, recalculé à chaque passe
        (pas en fin d'enchère : la dernière valeur sert). None tant qu'il est inconnu."""
        if card.max_price != AUTO:
            return card
        if not snipe_only and (value := self._auto_max(card, entry)):
            entry["auto_max"] = value
        if not entry.get("auto_max"):
            log.info("Achat : « %s » — prix du marché inconnu pour l'instant ; carte laissée de côté.", card.name)
            return None
        return dataclasses.replace(card, max_price=entry["auto_max"])

    def _auto_max(self, card: BuyCard, entry: dict) -> int | None:
        ref = entry.get("card_ref")
        if ref and card.rarity and ref.get("rarity") != card.rarity:
            ref = None  # rareté changée depuis
        if not ref:  # carte jamais vue : son identifiant vient d'une enchère en cours
            try:
                found = self.site.search_auctions(card.name)
            except SiteError as exc:
                log.warning("Achat : « %s » — enchères illisibles (%s).", card.name, exc)
                return None
            ref = next((r for a in found if normalize_name(auction_title(a)) == card.key
                        and rarity_matches(a, card) and (r := card_ref(a))), None)
            if ref is None:
                return None
            entry["card_ref"] = ref
        reader = PriceReader(self.site, self.config)
        group = SimpleNamespace(card_id=ref["id"], rarity=ref.get("rarity") or None, title=card.name)
        try:
            _, price = reader.read(group)
        finally:
            reader.save()
        if not price:
            return None
        if not reliable(reader.entry(group), self.config.min_sales_to_sell):
            log.info("Achat : « %s » — %s vente(s) connue(s) seulement (il en faut %d) pour un prix max auto.",
                     card.name, reader.entry(group).get("count"), self.config.min_sales_to_sell)
            entry.pop("auto_max", None)  # un ancien prix max auto ne sert plus
            return None
        value = max(1, math.floor(price * AUTO_BUY_RATIO))
        if value != entry.get("auto_max"):
            log.info("Achat : « %s » — prix max auto %d (%d %% du prix du marché, %.0f).", card.name, value,
                     round(AUTO_BUY_RATIO * 100), price)
        return value

    def blocked_by_fellow(self, card: BuyCard, entry: dict) -> None:
        """Seules enchères possibles déjà suivies par un autre compte du bot : prévenu une fois."""
        log.info("Achat : « %s » — enchère(s) à votre prix suivie(s) par un autre compte du bot ; "
                 "le script attend une autre enchère.", card.name)
        if not entry.get("blocked_notified") and not self.dry_run:
            notify(self.config, f"⚠️ Achat de « {card.name} » : l'enchère à votre prix est déjà suivie par un "
                                "autre utilisateur du bot. Le script attend une autre enchère.")
        entry["blocked_notified"] = True

    def lost(self, card: BuyCard, entry: dict, auction: dict, force: bool = False) -> None:
        """Enchère perdue (remportée par un autre, ou montée au-dessus de votre max) : notée
        une fois pour le bilan, si vous y aviez misé (force : enchère suivie terminée)."""
        if not force and entry.get("placed") != auction.get("id"):
            return
        entry.pop("placed", None)
        line = self.state.data["cards"].get(card.id, entry)  # pertes notées sur la ligne
        lost = list(line.get("lost") or [])
        if auction.get("id") in lost:  # déjà annoncée
            return
        line["lost"] = [*lost, auction.get("id")][-LOST_KEPT:]
        if auction.get("winner_id"):
            price = auction.get("final_price") or auction.get("current_bid")
            why = f"remportée à {price} par un autre"
        else:
            why = f"montée à {auction.get('current_bid') or min_next_bid(auction)}, au-dessus de votre max"
        log.info("Achat : « %s » perdue, %s (max %s) ; recherche d'une autre enchère.", card.name, why, card.max_price)
        if not self.dry_run:
            from .report import record_loss  # report lit aussi les achats

            record_loss(self.config, card.name, why, card.max_price)

    def settle(self, card: BuyCard, entry: dict, auction: dict) -> None:
        """Fin de l'enchère surveillée : gagnée ou perdue, annoncée tout de suite. Si le
        gagnant n'est pas encore connu au bout d'OUTCOME_WAIT, la passe suivante s'en charge."""
        deadline = time.monotonic() + OUTCOME_WAIT.total_seconds()
        silent_since = 0.0  # le site ne répond plus depuis (time.monotonic())
        while True:
            status = str(auction.get("status") or "active").lower()
            winner = auction.get("winner_id")
            if status != "active" and "cancel" in status:
                log.info("Achat : enchère de « %s » annulée ; recherche d'une autre.", card.name)
                _forget(entry)
                return
            if status != "active" and winner and self.state.my_id:
                if winner == self.state.my_id:
                    with self.lock:  # deux fins d'enchère de la ligne en même temps : comptées l'une après l'autre
                        line = self.state.data["cards"].get(card.id)
                        if line is not None and self.count_wins(card, line, [auction]):
                            self.complete(card)
                else:
                    log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
                    self.lost(card, entry, auction, force=True)
                    _forget(entry)
                return
            if time.monotonic() + OUTCOME_POLL > deadline or (silent_since and time.monotonic() - silent_since
                                                              > SILENT_WAIT.total_seconds()):
                log.info("Achat : résultat de « %s » pas encore connu ; vu à la prochaine passe.", card.name)
                return
            time.sleep(SILENT_POLL if silent_since else OUTCOME_POLL)
            fresh = self.site.read_auction(auction["id"], quiet=True)
            if fresh is None:  # le site ne répond pas : relue plus rarement, puis laissée à la passe suivante
                silent_since = silent_since or time.monotonic()
                continue
            auction, silent_since = fresh, 0.0

    def deadline(self, auction: dict) -> float:
        """Jusqu'à quand renvoyer une mise sans réponse (time.monotonic()) : la fin de
        l'enchère, au plus BID_RETRY_LIMIT."""
        end = parse_time(auction.get("end_at"))
        left = (end - self.now()).total_seconds() if end else BID_RETRY_LIMIT.total_seconds()
        return time.monotonic() + max(0.0, min(left, BID_RETRY_LIMIT.total_seconds()))

    def leading(self, auction: dict) -> bool:
        return bool(self.state.my_id) and auction.get("current_bidder_id") == self.state.my_id

    def track(self, card: BuyCard, entry: dict, auction: dict, outbid: bool) -> None:
        """Enchère gardée sans miser : la mise part à SNIPE_LEAD de la fin
        (watch_until_end), si l'enchère est toujours dans la limite. Une fin toute proche
        est surveillée tout de suite, la passe suivante viendrait trop tard."""
        minimum = min_next_bid(auction)
        entry.update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding", watch_price=minimum)
        log.info("Achat : « %s » — %s, mise minimale %d (limite %d) ; mise à %d s de la fin (%s).", card.name,
                 f"dépassé ({auction.get('current_bid')})" if outbid else "enchère suivie sans miser", minimum,
                 card.max_price, self.config.snipe_lead.total_seconds(), auction.get("end_at"))
        if WATCH_APART.is_set():  # surveillée à part : enregistrée tout de suite si la fin approche
            if in_watch_window(entry, self.config.snipe_lead, self.now()):
                self.state.save()
            return
        self.watch_later(card, entry, auction)

    def bid_now(self, card: BuyCard, entry: dict, auction: dict) -> None:
        """Mise tout de suite de votre prix max (mise_directe), puis enchère surveillée
        jusqu'à la fin : dépassé, la mise minimale suivante dépasse votre max et l'enchère
        est abandonnée pour une autre. Une enchère déjà menée est seulement suivie."""
        entry.update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding",
                     watch_price=min_next_bid(auction))
        self.bid_if_needed(card, entry, auction, exact=self.config.bid_now)
        if entry.get("status") != "bidding" or entry.get("auction_id") != auction["id"]:
            return  # abandonnée (au-dessus du max), ou simulation
        if WATCH_APART.is_set():  # surveillée à part : enregistrée tout de suite si la fin approche
            if in_watch_window(entry, self.config.snipe_lead, self.now()):
                self.state.save()
            return
        self.watch_later(card, entry, auction)

    def bid_if_needed(self, card: BuyCard, entry: dict, auction: dict, exact: bool = False) -> None:
        """Mise sur l'enchère si vous n'y êtes pas en tête : la mise minimale, ou votre prix
        max lui-même si `exact` (mise_directe)."""
        if self.leading(auction):
            log.info("Achat : « %s » — vous menez à %s (fin %s).", card.name, auction.get("current_bid"),
                     auction.get("end_at"))
            return
        if min_next_bid(auction) > card.max_price:
            log.info("Achat : « %s » dépasse la limite (%d > %d) ; abandon de cette enchère.",
                     card.name, min_next_bid(auction), card.max_price)
            self.lost(card, entry, auction)
            _forget(entry)
            return
        wanted = card.max_price if exact else min_next_bid(auction)
        if self.config.max_total_bids and self.budget_used + wanted > self.config.max_total_bids:
            log.info("Achat : budget MAX_TOTAL_BIDS atteint (%d engagés) ; « %s » attendra.",
                     self.budget_used, card.name)
            return

        result, amount = self.site.place_bid(auction["id"], card.max_price, self.dry_run,
                                             self.deadline(auction), self.state.my_id, exact)
        if result == "bid":
            self.budget_used += amount
            entry["placed"] = auction["id"]
            entry["my_bid"] = {"auction": auction["id"], "amount": amount}  # pour le bilan (/update)
            refreshed = self.site.read_auction(auction["id"]) or {}
            if not self.state.my_id and refreshed.get("current_bid") == amount:
                self.state.my_id = refreshed.get("current_bidder_id")
            entry["end_at"] = refreshed.get("end_at") or entry.get("end_at")  # fin repoussée ?
            log.info("Achat : mise de %d sur « %s » (%s, fin %s).", amount, card.name,
                     "votre prix max, mise directe" if exact else f"limite {card.max_price}", entry["end_at"])
        elif result == "dry_run":
            log.info("Simulation : mise de %d sur « %s » (limite %d).", amount, card.name, card.max_price)
            _forget(entry)  # rien n'est misé : rien à surveiller
        elif result == "too_high":
            log.info("Achat : la mise minimale de « %s » est %d, au-dessus de la limite %d ; "
                     "abandon de cette enchère.", card.name, amount, card.max_price)
            self.lost(card, entry, auction)
            _forget(entry)
        elif result == "no_funds":
            log.warning("Achat : solde insuffisant pour miser %d sur « %s ».", amount, card.name)
        else:
            log.warning("Achat : la mise sur « %s » n'a pas été confirmée par le site.", card.name)

    def watch_until_end(self, card: BuyCard, entry: dict, auction: dict) -> None:
        """Surveille l'enchère de près à partir de watch_start() avant la fin (relue chaque
        seconde : mise actuelle, temps restant, qui mène) et surenchérit dès que vous n'êtes
        plus en tête à SNIPE_LEAD de la fin, dans la limite, puis à chaque fois que vous
        êtes dépassé. Une mise des 10 dernières secondes prolonge l'enchère de 60 s : on
        continue tant que le site la dit active, même passé l'heure de fin lue (prolongation
        vue en retard). Une lecture sans réponse est simplement refaite la seconde suivante."""
        lead = self.config.snipe_lead
        start = watch_start(lead)
        end = parse_time(auction.get("end_at"))
        if not end or end - self.now() > start + SNIPE_STARTUP + timedelta(minutes=2):
            return  # pas encore l'heure : la boucle reviendra
        deadline = time.monotonic() + WATCH_GUARD.total_seconds()
        announced, silent = False, 0  # silent : lectures sans réponse d'affilée
        while time.monotonic() < deadline:
            wait = (end - start - self.now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            if not announced:
                log.info("Achat : surveillance de « %s » jusqu'à la fin (%s), surenchère à %d s de la fin.",
                         card.name, auction.get("end_at"), lead.total_seconds())
                announced = True
            fresh = self.site.read_auction(auction["id"], quiet=silent > 0)
            if fresh is None:  # le site ne répond pas : relue dans une seconde avant la fin, plus rarement après
                silent += 1
                after_end = self.now() - end
                if after_end > SILENT_WAIT:
                    log.info("Achat : « %s » — le site ne répond plus depuis la fin de l'enchère (%d lecture(s) "
                             "sans réponse) ; résultat vu à la prochaine passe.", card.name, silent)
                    return
                time.sleep(WATCH_POLL if after_end < timedelta(0) else SILENT_POLL)
                continue
            if silent > 1:
                log.info("Achat : « %s » — le site répond de nouveau (%d lecture(s) sans réponse).", card.name, silent)
            silent = 0
            auction = fresh
            if str(auction.get("status") or "active").lower() != "active":
                self.settle(card, entry, auction)
                return
            new_end = parse_time(auction.get("end_at"))
            if new_end and new_end != end:
                if new_end > end:
                    log.info("Achat : « %s » — enchère prolongée jusqu'à %s (mise actuelle %s, %s).", card.name,
                             auction["end_at"], auction.get("current_bid"),
                             "vous menez" if self.leading(auction) else "vous n'êtes pas en tête")
                end = new_end
            entry["end_at"] = auction.get("end_at")
            if auction.get("current_bidder_id") in self.rivals:
                log.info("Achat : « %s » — un autre compte du bot mène cette enchère ; elle lui est laissée.",
                         card.name)
                _forget(entry)
                return
            remaining = end - self.now()
            if not self.leading(auction) and -BID_GRACE < remaining <= lead:
                if self.fast_bid(card, entry, auction):
                    if self.dry_run:
                        return
                    continue  # relire tout de suite : qui mène, fin repoussée ?
                if entry.get("status") != "bidding":
                    return
                time.sleep(BID_REREAD)  # mise refusée : l'enchère est relue et la mise refaite
                continue
            if remaining < -OUTCOME_WAIT:
                log.info("Achat : résultat de « %s » pas encore connu ; vu à la prochaine passe.", card.name)
                return
            time.sleep(WATCH_POLL if remaining > -BID_GRACE else OUTCOME_POLL)

    def fast_bid(self, card: BuyCard, entry: dict, auction: dict) -> bool:
        """Surenchère de dernière seconde : la mise minimale, calculée sur l'enchère qu'on
        vient de relire, envoyée directement par l'API. Si le site la refuse parce que
        l'enchère a monté entre-temps, la mise est refaite aussitôt au minimum qu'il
        indique, tant qu'il reste dans la limite. True si la mise est passée."""
        amount = min_next_bid(auction)
        for _ in range(BID_TOO_LOW_TRIES):
            if amount > card.max_price:
                log.info("Achat : « %s » — mise minimale %d au-dessus de la limite %d ; abandon.",
                         card.name, amount, card.max_price)
                self.lost(card, entry, auction)
                _forget(entry)
                return False
            with self.budget.lock:  # mise réservée avant l'envoi : deux fins simultanées ne dépassent pas le budget
                if self.config.max_total_bids and self.budget_used + amount > self.config.max_total_bids:
                    log.info("Achat : budget MAX_TOTAL_BIDS atteint (%d engagés) ; pas de mise sur « %s ».",
                             self.budget_used, card.name)
                    _forget(entry)
                    return False
                self.budget_used += amount
            if self.dry_run:
                log.info("Simulation : surenchère de %d sur « %s ».", amount, card.name)
                _forget(entry)
                return True
            refusal: dict = {}
            if self.site.post_bid(auction["id"], amount, self.deadline(auction), self.state.my_id, refusal):
                entry["placed"] = auction["id"]
                entry["my_bid"] = {"auction": auction["id"], "amount": amount}
                self.state.data["cards"].get(card.id, entry).pop("bid_blocked_notified", None)
                log.info("Achat : surenchère de %d sur « %s » à %.0f s de la fin.", amount, card.name,
                         (parse_time(auction.get("end_at")) - self.now()).total_seconds())
                return True
            with self.budget.lock:
                self.budget_used -= amount
            code, minimum = refusal.get("code"), refusal.get("min")
            if code == "bid_too_low" and isinstance(minimum, int) and minimum > amount:
                log.info("Achat : « %s » — l'enchère a monté entre-temps, mise minimale %d.", card.name, minimum)
                amount = minimum
                continue
            if code == HUMAN_CHECK_CODE:  # vérification anti-bot : prévenu (au plus toutes les 3 h)
                self.site.human_check(f"mise de {amount} sur « {card.name} »",
                                      str(refusal.get("error") or refusal.get("message") or code))
                _forget(entry)
                return False
            if code == "insufficient_balance":
                why = "solde insuffisant"
                log.warning("Achat : mise de %d sur « %s » impossible : %s.", amount, card.name, why)
                line = self.state.data["cards"].get(card.id, entry)  # prévenu une fois pour la ligne
                if not line.get("bid_blocked_notified"):
                    line["bid_blocked_notified"] = True
                    notify(self.config, f"⚠️ Mise de {amount} sur « {card.name} » impossible : {why}.")
                _forget(entry)
                return False
            return False
        return False

