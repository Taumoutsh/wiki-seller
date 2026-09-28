"""Achat des cartes de wanted_cards.json : on mise sur une enchère dans la limite de
prix, on surenchérit si quelqu'un passe devant, et on revient SNIPE_LEAD avant la fin.

Le fichier wanted_cards.json n'est jamais modifié par le script : le suivi (enchère
choisie, heure de fin, carte obtenue) est enregistré dans state/wanted_state.json."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .selection import normalize_name

log = logging.getLogger(__name__)

# Enchère qu'on laisse passer : trop proche de la fin pour ouvrir la page et miser.
TOO_LATE = timedelta(seconds=5)
# Réveil avant l'heure de mise : lancer le navigateur et se connecter prend du temps.
SNIPE_STARTUP = timedelta(seconds=45)


class WantedCardsError(Exception):
    pass


@dataclass(frozen=True)
class WantedCard:
    name: str
    max_price: int

    @property
    def key(self) -> str:
        return normalize_name(self.name)


def load_wanted_cards(path: Path) -> list[WantedCard]:
    """Liste JSON [{"name": "Tour Eiffel", "max_price": 500}, ...]. Fichier absent = rien à acheter."""
    if not path.exists():
        return []
    if path.is_dir():  # Docker crée un dossier si le fichier monté n'existe pas
        raise WantedCardsError(f"{path} est un dossier : créez le fichier (voir wanted_cards.example.json).")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WantedCardsError(f"{path} n'est pas un JSON valide : {exc}") from exc
    if not isinstance(data, list):
        raise WantedCardsError(f'{path} doit contenir une liste, ex. [{{"name": "Tour Eiffel", "max_price": 500}}].')
    cards = []
    for item in data:
        name = item.get("name") if isinstance(item, dict) else None
        price = item.get("max_price") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip() or isinstance(price, bool) or not isinstance(price, int) or price < 1:
            raise WantedCardsError(f"{path} : entrée invalide {item!r} (attendu name + max_price entier positif).")
        cards.append(WantedCard(name.strip(), price))
    return cards


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def auction_title(auction: dict) -> str:
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    return card.get("wikipedia_title") or card.get("title") or ""


def min_next_bid(auction: dict) -> int:
    """Estimation basse de la prochaine mise (la vraie valeur est lue sur la page)."""
    current = auction.get("current_bid")
    if isinstance(current, (int, float)):
        return int(current) + 1
    return int(auction.get("effective_bid") or auction.get("base_amount") or 0)


def choose_auction(auctions: list[dict], card: WantedCard, my_id: str | None, now: datetime,
                   my_sales: set[str] = frozenset()) -> dict | None:
    """Enchère active de cette carte, pas à nous, dans la limite : la moins chère, puis
    celle qui finit le plus tôt."""
    candidates = []
    for auction in auctions:
        end = parse_time(auction.get("end_at"))
        if (normalize_name(auction_title(auction)) != card.key or auction.get("status", "active") != "active"
                or (my_id and auction.get("seller_id") == my_id) or auction.get("id") in my_sales
                or not end or end - now < TOO_LATE or min_next_bid(auction) > card.max_price):
            continue
        candidates.append((min_next_bid(auction), end, auction))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2] if candidates else None


def won_since(mine: dict, card: WantedCard, since: datetime | None) -> dict | None:
    """Enchère de cette carte dans « Gagnées », réglée depuis `since`."""
    for auction in mine.get("won") or []:
        if not isinstance(auction, dict) or normalize_name(auction_title(auction)) != card.key:
            continue
        settled = parse_time(auction.get("settled_at")) or parse_time(auction.get("end_at"))
        if since is None or (settled and settled >= since):
            return auction
    return None


def my_user_id(mine: dict, known: str | None) -> str | None:
    if known:
        return known
    for key, field in (("selling", "seller_id"), ("won", "winner_id"), ("history", "seller_id")):
        for auction in mine.get(key) or []:
            if isinstance(auction, dict) and isinstance(auction.get(field), str):
                return auction[field]
    return None


class WantedState:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("cards", {})

    @property
    def my_id(self) -> str | None:
        return self.data.get("my_id")

    @my_id.setter
    def my_id(self, value: str | None) -> None:
        if value:
            self.data["my_id"] = value

    def card(self, card: WantedCard) -> dict:
        return self.data["cards"].setdefault(card.key, {"name": card.name})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def next_snipe_at(self, lead: timedelta, offset: timedelta = timedelta(0)) -> datetime | None:
        """Heure (horloge locale) à laquelle se réveiller pour la prochaine fin d'enchère suivie."""
        ends = [parse_time(c.get("end_at")) for c in self.data["cards"].values()
                if c.get("status") == "bidding"]
        ends = [e for e in ends if e]
        if not ends:
            return None
        return min(ends) - offset - lead - SNIPE_STARTUP


def run_bids(site, config: Config, dry_run: bool, snipe_only: bool = False) -> None:
    """Une passe d'achat. snipe_only : ne traiter que les enchères suivies qui se
    terminent bientôt : attendre SNIPE_LEAD avant la fin, puis surveiller l'enchère
    jusqu'à sa vraie fin (le site la prolonge de 60 s après une mise tardive)."""
    wanted = load_wanted_cards(config.wanted_cards_file)
    state = WantedState(config.wanted_state_file)
    if not wanted:
        return
    offset = site.server_clock_offset()
    buyer = _Buyer(site, config, state, dry_run, lambda: datetime.now(timezone.utc) + offset)
    mine = site.fetch_mine()
    state.my_id = my_user_id(mine, state.my_id)
    bidding = [a for a in mine.get("bidding") or [] if isinstance(a, dict)]
    buyer.budget_used = sum(int(a.get("current_bid") or 0) for a in bidding
                            if state.my_id and a.get("current_bidder_id") == state.my_id
                            and a.get("status") == "active")
    my_sales = {a.get("id") for a in mine.get("selling") or [] if isinstance(a, dict)}

    try:
        for card in wanted:
            entry = state.card(card)
            entry.setdefault("since", buyer.now().isoformat())
            if entry.get("status") == "won":
                continue
            # Un seul exemplaire : une enchère de cette carte gagnée depuis qu'elle est
            # dans la liste suffit, même si ce n'était pas celle que le script suivait.
            won = won_since(mine, card, parse_time(entry["since"]))
            if won:
                log.info("Achat : « %s » obtenue pour %s.", card.name, won.get("final_price"))
                entry.update(status="won", auction_id=won.get("id"), price=won.get("final_price"))
                entry.pop("end_at", None)
                continue
            # Une seule enchère à la fois : si vous menez déjà une enchère de cette carte
            # dans « Mes enchères », c'est elle qu'on suit, et on ne mise nulle part ailleurs.
            mine_for_card = [a for a in bidding if normalize_name(auction_title(a)) == card.key
                             and a.get("status", "active") == "active"]
            leading_here = [a for a in mine_for_card if buyer.leading(a)]
            auction_id = entry.get("auction_id")
            if leading_here and auction_id not in {a["id"] for a in leading_here}:
                if auction_id:
                    log.info("Achat : « %s » — vous menez déjà une autre enchère de cette carte ; "
                             "elle seule est suivie.", card.name)
                auction_id = leading_here[0]["id"]
            elif not auction_id and mine_for_card:  # enchère misée à la main ?
                auction_id = mine_for_card[0]["id"]
            auction = site.fetch_auction(auction_id) if auction_id else None

            if auction and auction.get("status") != "active":
                if state.my_id and auction.get("winner_id") == state.my_id:
                    log.info("Achat : « %s » obtenue pour %s.", card.name, auction.get("final_price"))
                    entry.update(status="won", auction_id=auction_id, price=auction.get("final_price"))
                    entry.pop("end_at", None)
                    continue
                log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
                auction = None
            if auction is None:
                _forget(entry)

            if snipe_only:
                if auction is not None:
                    buyer.watch_until_end(card, entry, auction)
                continue

            if auction is None:
                auction = choose_auction(site.search_auctions(card.name), card, state.my_id,
                                         buyer.now(), my_sales)
                if auction is None:
                    log.info("Achat : aucune enchère de « %s » à %d ou moins.", card.name, card.max_price)
                    continue
            entry.update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding")
            buyer.bid_if_needed(card, entry, auction)
    finally:
        state.save()


def _forget(entry: dict) -> None:
    entry.pop("auction_id", None)
    entry.pop("end_at", None)
    entry["status"] = "searching"


class _Buyer:
    def __init__(self, site, config: Config, state: WantedState, dry_run: bool, now):
        self.site, self.config, self.state, self.dry_run, self.now = site, config, state, dry_run, now
        self.budget_used = 0

    def leading(self, auction: dict) -> bool:
        return bool(self.state.my_id) and auction.get("current_bidder_id") == self.state.my_id

    def bid_if_needed(self, card: WantedCard, entry: dict, auction: dict) -> None:
        if self.leading(auction):
            log.info("Achat : « %s » — vous menez à %s (fin %s).", card.name, auction.get("current_bid"),
                     auction.get("end_at"))
            return
        if min_next_bid(auction) > card.max_price:
            log.info("Achat : « %s » dépasse la limite (%d > %d) ; abandon de cette enchère.",
                     card.name, min_next_bid(auction), card.max_price)
            _forget(entry)
            return
        if self.config.max_total_bids and self.budget_used + min_next_bid(auction) > self.config.max_total_bids:
            log.info("Achat : budget MAX_TOTAL_BIDS atteint (%d engagés) ; « %s » attendra.",
                     self.budget_used, card.name)
            return

        result, amount = self.site.place_bid(auction["id"], card.max_price, self.dry_run)
        if result == "bid":
            self.budget_used += amount
            refreshed = self.site.fetch_auction(auction["id"]) or {}
            if not self.state.my_id and refreshed.get("current_bid") == amount:
                self.state.my_id = refreshed.get("current_bidder_id")
            entry["end_at"] = refreshed.get("end_at") or entry.get("end_at")  # fin repoussée ?
            log.info("Achat : mise de %d sur « %s » (limite %d, fin %s).", amount, card.name,
                     card.max_price, entry["end_at"])
        elif result == "dry_run":
            log.info("Simulation : mise de %d sur « %s » (limite %d).", amount, card.name, card.max_price)
            _forget(entry)  # rien n'est misé : rien à surveiller
        elif result == "too_high":
            log.info("Achat : la mise minimale de « %s » est %d, au-dessus de la limite %d.",
                     card.name, amount, card.max_price)
        elif result == "no_funds":
            log.warning("Achat : solde insuffisant pour miser %d sur « %s ».", amount, card.name)
        else:
            log.warning("Achat : la mise sur « %s » n'a pas été confirmée par le site.", card.name)

    def watch_until_end(self, card: WantedCard, entry: dict, auction: dict) -> None:
        """Attend SNIPE_LEAD avant la fin, puis surenchérit si besoin jusqu'à la fin
        réelle de l'enchère (prolongée de 60 s par toute mise des 10 dernières secondes)."""
        lead = self.config.snipe_lead
        end = parse_time(auction.get("end_at"))
        if not end or end - self.now() > lead + SNIPE_STARTUP + timedelta(minutes=2):
            return  # pas encore l'heure : la boucle reviendra
        deadline = time.monotonic() + WATCH_GUARD.total_seconds()
        while time.monotonic() < deadline:
            wait = (end - lead - self.now()).total_seconds()
            if wait > 0:
                log.info("Achat : « %s » se termine dans %.0f s ; retour %d s avant la fin.", card.name,
                         (end - self.now()).total_seconds(), lead.total_seconds())
                time.sleep(wait)
            auction = self.site.fetch_auction(auction["id"]) or auction
            if auction.get("status") != "active":
                return  # la passe suivante verra si elle est gagnée
            end = parse_time(auction.get("end_at")) or end
            entry["end_at"] = auction.get("end_at")
            if not self.leading(auction):
                self.bid_if_needed(card, entry, auction)
                if entry.get("status") != "bidding" or self.dry_run:
                    return
            if end - self.now() < -timedelta(seconds=5):
                return
            time.sleep(WATCH_POLL)


WATCH_POLL = 2  # secondes entre deux vérifications pendant la fin d'enchère
WATCH_GUARD = timedelta(minutes=15)  # jamais plus longtemps sur une même enchère
