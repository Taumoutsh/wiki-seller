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
# Surveillance de la fin d'enchère : à partir de WATCH_START avant la fin, l'enchère est
# relue toutes les WATCH_POLL secondes ; on surenchérit à SNIPE_LEAD de la fin.
WATCH_START = timedelta(seconds=60)


class WantedCardsError(Exception):
    pass


@dataclass(frozen=True)
class WantedCard:
    name: str
    max_price: int
    copies: int = 1  # exemplaires à acheter

    @property
    def key(self) -> str:
        return normalize_name(self.name)


def load_wanted_cards(path: Path) -> list[WantedCard]:
    """Liste JSON [{"name": "Tour Eiffel", "max_price": 500, "copies": 2}, ...] (copies :
    1 par défaut). Fichier absent = rien à acheter."""
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
        copies = item.get("copies", 1)
        if isinstance(copies, bool) or not isinstance(copies, int) or copies < 1:
            raise WantedCardsError(f"{path} : « copies » doit être un entier positif ({item!r}).")
        cards.append(WantedCard(name.strip(), price, copies))
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
                   my_sales: set[str] = frozenset(), exclude: set[str] = frozenset()) -> dict | None:
    """Enchère active de cette carte, pas à nous, dans la limite : la moins chère, puis
    celle qui finit le plus tôt."""
    candidates = []
    for auction in auctions:
        end = parse_time(auction.get("end_at"))
        if (normalize_name(auction_title(auction)) != card.key or auction.get("status", "active") != "active"
                or (my_id and auction.get("seller_id") == my_id) or auction.get("id") in my_sales
                or auction.get("id") in exclude
                or not end or end - now < TOO_LATE or min_next_bid(auction) > card.max_price):
            continue
        candidates.append((min_next_bid(auction), end, auction))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2] if candidates else None


def won_since(mine: dict, card: WantedCard, since: datetime | None) -> list[dict]:
    """Enchères de cette carte dans « Gagnées », réglées depuis `since`."""
    found = []
    for auction in mine.get("won") or []:
        if not isinstance(auction, dict) or normalize_name(auction_title(auction)) != card.key:
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
        return max(min(ends) - max(lead, WATCH_START) - SNIPE_STARTUP, now)


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

    bidding_ids = {a.get("id") for a in bidding}
    try:
        for card in wanted:
            entry = state.card(card)
            entry.setdefault("since", buyer.now().isoformat())
            if buyer.count_wins(card, entry, won_since(mine, card, parse_time(entry["since"]))):
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
                    if buyer.count_wins(card, entry, [auction]):
                        continue
                else:
                    log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
                auction = None
            if auction is None:
                _forget(entry)

            if snipe_only:
                if auction is not None:
                    buyer.watch_until_end(card, entry, auction)
                continue

            abandoned: set[str] = set()
            if auction is not None and not buyer.leading(auction) \
                    and (auction["id"] in bidding_ids or entry.get("placed") == auction["id"]):
                # Déjà misé et dépassé : pas de surenchère maintenant, seulement à la fin
                # (réveil SNIPE_LEAD avant), tant que la limite le permet. La limite est
                # vérifiée avec la vraie mise minimale (champ prérempli de la page).
                minimum = min_next_bid(auction)
                if minimum <= card.max_price:
                    minimum = site.read_min_bid(auction["id"]) or minimum
                if minimum <= card.max_price:
                    entry.update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding")
                    log.info("Achat : « %s » — dépassé (%s), surenchère juste avant la fin (%s).",
                             card.name, auction.get("current_bid"), auction.get("end_at"))
                    continue
                log.info("Achat : « %s » dépasse la limite (%d > %d) ; abandon de cette enchère.",
                         card.name, minimum, card.max_price)
                abandoned.add(auction["id"])
                _forget(entry)
                auction = None

            if auction is None:
                auction = choose_auction(site.search_auctions(card.name), card, state.my_id,
                                         buyer.now(), my_sales, abandoned)
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

    def count_wins(self, card: WantedCard, entry: dict, wins: list[dict]) -> bool:
        """Note les enchères gagnées ; True si le nombre d'exemplaires voulu est atteint."""
        if entry.get("status") == "won" and "won" not in entry:  # ancien format : déjà obtenue
            entry["won"] = {entry.get("auction_id") or "avant": entry.get("price")}
        won = dict(entry.get("won") or {})
        for auction in wins:
            if auction.get("id") and auction["id"] not in won:
                won[auction["id"]] = auction.get("final_price")
                log.info("Achat : « %s » obtenue pour %s (%d/%d).", card.name, auction.get("final_price"),
                         len(won), card.copies)
        entry["won"] = won
        if len(won) >= card.copies:
            if entry.get("status") != "won":
                entry.update(status="won")
                entry.pop("auction_id", None)
                entry.pop("end_at", None)
            return True
        if entry.get("status") == "won":  # « copies » augmenté depuis
            _forget(entry)
        if entry.get("auction_id") in won:
            _forget(entry)
        return False

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
            entry["placed"] = auction["id"]
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
            log.info("Achat : la mise minimale de « %s » est %d, au-dessus de la limite %d ; "
                     "abandon de cette enchère.", card.name, amount, card.max_price)
            _forget(entry)
        elif result == "no_funds":
            log.warning("Achat : solde insuffisant pour miser %d sur « %s ».", amount, card.name)
        else:
            log.warning("Achat : la mise sur « %s » n'a pas été confirmée par le site.", card.name)

    def watch_until_end(self, card: WantedCard, entry: dict, auction: dict) -> None:
        """Surveille l'enchère de près à partir de WATCH_START avant la fin (relue chaque
        seconde) et surenchérit dès que vous n'êtes plus en tête à SNIPE_LEAD de la fin,
        dans la limite. Une mise des 10 dernières secondes prolonge l'enchère de 60 s :
        on continue jusqu'à la fin réelle."""
        lead = self.config.snipe_lead
        end = parse_time(auction.get("end_at"))
        if not end or end - self.now() > WATCH_START + SNIPE_STARTUP + timedelta(minutes=2):
            return  # pas encore l'heure : la boucle reviendra
        self._learn_step(entry, auction)
        deadline = time.monotonic() + WATCH_GUARD.total_seconds()
        announced = False
        while time.monotonic() < deadline:
            wait = (end - WATCH_START - self.now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            if not announced:
                log.info("Achat : surveillance de « %s » jusqu'à la fin (%s), surenchère à %d s de la fin.",
                         card.name, auction.get("end_at"), lead.total_seconds())
                announced = True
            auction = self.site.fetch_auction(auction["id"]) or auction
            if auction.get("status") != "active":
                return  # la passe suivante verra si elle est gagnée
            end = parse_time(auction.get("end_at")) or end
            entry["end_at"] = auction.get("end_at")
            remaining = end - self.now()
            if not self.leading(auction) and remaining <= lead:
                self.fast_bid(card, entry, auction)
                if entry.get("status") != "bidding" or self.dry_run:
                    return
                continue  # relire tout de suite
            if remaining < -timedelta(seconds=5):
                return
            time.sleep(WATCH_POLL)

    def _learn_step(self, entry: dict, auction: dict) -> None:
        """Pas de surenchère du site : mise minimale (champ prérempli) - mise actuelle."""
        if entry.get("step") or not isinstance(auction.get("current_bid"), int):
            return
        minimum = self.site.read_min_bid(auction["id"])
        if minimum and minimum > auction["current_bid"]:
            entry["step"] = minimum - auction["current_bid"]

    def fast_bid(self, card: WantedCard, entry: dict, auction: dict) -> None:
        """Surenchère de dernière seconde : montant calculé avec le pas appris, envoyé
        directement par l'API ; la page n'est relue que si le site refuse."""
        current = auction.get("current_bid")
        step = entry.get("step")
        if not self.config.actions_via_api or (isinstance(current, int) and not step):
            self.bid_if_needed(card, entry, auction)  # lecture de la page
            self._learn_step(entry, auction)
            return
        amount = int(current) + int(step) if isinstance(current, int) else int(
            auction.get("base_amount") or auction.get("effective_bid") or 0)
        if amount > card.max_price:
            log.info("Achat : « %s » — mise minimale %d au-dessus de la limite %d ; abandon.",
                     card.name, amount, card.max_price)
            _forget(entry)
            return
        if self.dry_run:
            log.info("Simulation : surenchère de %d sur « %s ».", amount, card.name)
            _forget(entry)
            return
        if self.site.post_bid(auction["id"], amount):
            entry["placed"] = auction["id"]
            log.info("Achat : surenchère de %d sur « %s » à %.0f s de la fin.", amount, card.name,
                     (parse_time(auction.get("end_at")) - self.now()).total_seconds())
            return
        entry.pop("step", None)  # pas erroné ? on repasse par la page
        self.bid_if_needed(card, entry, auction)
        self._learn_step(entry, auction)


WATCH_POLL = 1  # secondes entre deux vérifications pendant la fin d'enchère
WATCH_GUARD = timedelta(minutes=60)  # guerre d'enchères : chaque mise ajoute une minute
