"""Achat des cartes de state/buy_cards.json (/buy, /buyandsell) : on mise sur une
enchère dans la limite de prix, on surenchérit si quelqu'un passe devant, et on revient
SNIPE_LEAD avant la fin. Le suivi (enchère choisie, heure de fin, exemplaires obtenus)
est enregistré dans state/wanted_state.json.

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

from .config import Config
from .lists import AUTO, AUTO_BUY_RATIO, AUTO_SELL_RATIO, BuyCard, buy_cards, complete_purchase, price_text
from .pricing import PriceReader
from .selection import normalize_name
from .site import SiteError, entry_rarity, min_bid
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
    """Enchère de la rareté voulue (toujours vrai sans rareté demandée)."""
    rarity = entry_rarity(auction)
    return not card.rarity or rarity == card.rarity or (unknown_ok and not rarity)


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
        return self.data["cards"].setdefault(card.key, {"name": card.name})

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


def run_bids(site, config: Config, dry_run: bool, snipe_only: bool = False) -> None:
    """Une passe d'achat. snipe_only : ne traiter que les enchères suivies qui se
    terminent bientôt : attendre SNIPE_LEAD avant la fin, puis surveiller l'enchère
    jusqu'à sa vraie fin (le site la prolonge de 60 s après une mise tardive)."""
    wanted = buy_cards(config)
    state = WantedState(config.wanted_state_file)
    dropped = set(state.data["cards"]) - {c.key for c in wanted}  # /unbuy : suivi oublié
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
    my_sales = {a.get("id") for a in mine.get("selling") or [] if isinstance(a, dict)}
    taken, rivals = fellow_accounts(config)
    buyer.rivals = rivals

    bidding_ids = {a.get("id") for a in bidding}
    try:
        for card in wanted:
            entry = state.card(card)
            entry.setdefault("since", buyer.now().isoformat())
            if buyer.count_wins(card, entry, won_since(mine, card, parse_time(entry["since"]))):
                buyer.complete(card)
                continue
            card = buyer.resolve_max(card, entry, snipe_only)  # prix max « auto »
            if card is None:
                continue
            # Une seule enchère à la fois : si vous menez déjà une enchère de cette carte
            # dans « Mes enchères », c'est elle qu'on suit, et on ne mise nulle part ailleurs.
            # Le site laisse un moment les enchères finies dans « Mes enchères » : celles déjà
            # gagnées, perdues ou terminées ne sont pas reprises.
            done = set(entry.get("won") or {}) | set(entry.get("lost") or [])
            mine_for_card = [a for a in bidding if normalize_name(auction_title(a)) == card.key
                             and a.get("status", "active") == "active" and a.get("id") not in done
                             and rarity_matches(a, card, unknown_ok=True) and not ended(a, buyer.now())]
            leading_here = [a for a in mine_for_card if buyer.leading(a)]
            auction_id = entry.get("auction_id")
            if leading_here and auction_id not in {a["id"] for a in leading_here}:
                if auction_id:
                    log.info("Achat : « %s » — vous menez déjà une autre enchère de cette carte ; "
                             "elle seule est suivie.", card.name)
                auction_id = leading_here[0]["id"]
            elif not auction_id and mine_for_card:  # enchère misée à la main ?
                auction_id = mine_for_card[0]["id"]
            try:
                auction = site.fetch_auction(auction_id) if auction_id else None
            except SiteError as exc:  # le site ne répond pas pour cette enchère
                if not snipe_only:
                    log.warning("Achat : « %s » — enchère illisible (%s) ; nouvel essai plus tard.", card.name, exc)
                    continue
                # Fin d'enchère : on la surveille quand même, elle sera relue chaque seconde.
                auction = {"id": auction_id, "end_at": entry.get("end_at"), "status": "active"}

            if auction and auction.get("status") != "active":
                if state.my_id and auction.get("winner_id") == state.my_id:
                    if buyer.count_wins(card, entry, [auction]):
                        buyer.complete(card)
                        continue
                else:
                    log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
                    if state.my_id and auction.get("winner_id"):
                        buyer.lost(card, entry, auction, force=True)
                auction = None
            abandoned: set[str] = set()
            if auction is not None and not buyer.leading(auction) and (
                    auction["id"] in taken or auction.get("current_bidder_id") in rivals):
                log.info("Achat : « %s » — enchère suivie ou menée par un autre compte du bot ; elle lui est "
                         "laissée.", card.name)
                abandoned.add(auction["id"])
                auction = None
            if auction is None:
                _forget(entry)
            elif not snipe_only:
                claim(config, auction["id"])

            if snipe_only:
                if auction is not None:
                    buyer.watch_until_end(card, entry, auction)
                continue

            outbid = auction is not None and (auction["id"] in bidding_ids or entry.get("placed") == auction["id"])
            if auction is not None and not buyer.leading(auction) \
                    and (outbid or entry.get("auction_id") == auction["id"]):
                # Enchère déjà suivie (dépassé, ou pas encore misé) : pas de mise maintenant,
                # seulement à la fin (réveil SNIPE_LEAD avant), tant que la limite le permet.
                minimum = min_next_bid(auction)
                if minimum <= card.max_price:
                    buyer.track(card, entry, auction, outbid)
                    continue
                log.info("Achat : « %s » dépasse la limite (%d > %d) ; abandon de cette enchère.",
                         card.name, minimum, card.max_price)
                buyer.lost(card, entry, auction, force=outbid)  # perdue seulement si vous y aviez misé
                abandoned.add(auction["id"])
                _forget(entry)
                auction = None

            if auction is None:
                found = site.search_auctions(card.name)
                auction = choose_auction(found, card, state.my_id, buyer.now(), my_sales, abandoned | taken, rivals)
                if auction is not None and not claim(config, auction["id"]):
                    auction = None  # choisie au même instant par un autre compte
                if auction is None:
                    log.info("Achat : aucune enchère de « %s »%s à %d ou moins%s.", card.name,
                             f" {card.rarity}" if card.rarity else "", card.max_price,
                             f" chez @{card.seller}" if card.seller else "")
                    same_card = [a for a in found if normalize_name(auction_title(a)) == card.key]
                    if card.seller and same_card:  # pour vérifier comment le site nomme les vendeurs
                        log.info("Achat : « %s » — vendeurs des enchères en cours : %s", card.name,
                                 ", ".join(sorted(set().union(*(seller_names(a) for a in same_card))))[:300])
                    if card.rarity and same_card:
                        log.info("Achat : « %s » — raretés des enchères en cours : %s", card.name,
                                 ", ".join(sorted({entry_rarity(a) or "inconnue" for a in same_card})))
                    if choose_auction(found, card, state.my_id, buyer.now(), my_sales, abandoned) is not None:
                        buyer.blocked_by_fellow(card, entry)
                    continue
            entry.pop("blocked_notified", None)
            if buyer.leading(auction):  # misée à la main, ou déjà menée
                entry.update(auction_id=auction["id"], end_at=auction.get("end_at"), status="bidding")
                buyer.bid_if_needed(card, entry, auction)
            else:  # gardée et surveillée : la mise part juste avant la fin
                buyer.track(card, entry, auction, outbid=False)
    finally:
        state.save()
        release_claims(config)


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


class _Buyer:
    def __init__(self, site, config: Config, state: WantedState, dry_run: bool, now):
        self.site, self.config, self.state, self.dry_run, self.now = site, config, state, dry_run, now
        self.budget_used = 0
        self.rivals: set[str] = set()  # identifiants des autres comptes du bot

    def count_wins(self, card: BuyCard, entry: dict, wins: list[dict]) -> bool:
        """Note les enchères gagnées ; True si le nombre d'exemplaires voulu est atteint."""
        if entry.get("status") == "won" and "won" not in entry:  # ancien format : déjà obtenue
            entry["won"] = {entry.get("auction_id") or "avant": entry.get("price")}
        won = dict(entry.get("won") or {})
        for auction in wins:
            if auction.get("id") and auction["id"] not in won:
                price = auction.get("final_price") or auction.get("current_bid")
                won[auction["id"]] = price
                log.info("Achat : « %s » obtenue pour %s (%d/%d).", card.name, price, len(won), card.copies)
                if not self.dry_run:
                    resale = f" · revente à {price_text(card.sell_price, AUTO_SELL_RATIO)}" if card.sell_price else ""
                    notify(self.config, f"🛒 Obtenue : {card.name} pour {price} ({len(won)}/{card.copies}){resale}")
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

    def complete(self, card: BuyCard) -> None:
        """Tous les exemplaires obtenus : la carte quitte les achats (et leur suivi) pour
        les cartes protégées, ou pour les ventes (/buyandsell)."""
        bought = len(self.state.card(card).get("won") or {})
        if self.dry_run:
            log.info("Simulation : achat de « %s » terminé ; elle passerait %s.", card.name,
                     f"dans les ventes à {card.sell_price}" if card.sell_price else "dans les cartes protégées")
            return
        message = complete_purchase(self.config, card, bought)
        del self.state.data["cards"][card.key]  # redemandée plus tard, elle repartira de zéro
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
        try:
            _, price = reader.read(SimpleNamespace(card_id=ref["id"], rarity=ref.get("rarity") or None,
                                                   title=card.name))
        finally:
            reader.save()
        if not price:
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
        lost = list(entry.get("lost") or [])
        if auction.get("id") in lost:  # déjà annoncée
            return
        entry["lost"] = [*lost, auction.get("id")][-LOST_KEPT:]
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
        while True:
            status = str(auction.get("status") or "active").lower()
            winner = auction.get("winner_id")
            if status != "active" and "cancel" in status:
                log.info("Achat : enchère de « %s » annulée ; recherche d'une autre.", card.name)
                _forget(entry)
                return
            if status != "active" and winner and self.state.my_id:
                if winner == self.state.my_id:
                    if self.count_wins(card, entry, [auction]):
                        self.complete(card)
                else:
                    log.info("Achat : enchère perdue pour « %s » ; recherche d'une autre.", card.name)
                    self.lost(card, entry, auction, force=True)
                    _forget(entry)
                return
            if time.monotonic() + OUTCOME_POLL > deadline:
                log.info("Achat : résultat de « %s » pas encore connu ; vu à la prochaine passe.", card.name)
                return
            time.sleep(OUTCOME_POLL)
            auction = self.site.read_auction(auction["id"]) or auction

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
        self.watch_until_end(card, entry, auction)

    def bid_if_needed(self, card: BuyCard, entry: dict, auction: dict) -> None:
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
        if self.config.max_total_bids and self.budget_used + min_next_bid(auction) > self.config.max_total_bids:
            log.info("Achat : budget MAX_TOTAL_BIDS atteint (%d engagés) ; « %s » attendra.",
                     self.budget_used, card.name)
            return

        result, amount = self.site.place_bid(auction["id"], card.max_price, self.dry_run,
                                             self.deadline(auction), self.state.my_id)
        if result == "bid":
            self.budget_used += amount
            entry["placed"] = auction["id"]
            entry["my_bid"] = {"auction": auction["id"], "amount": amount}  # pour le bilan (/update)
            refreshed = self.site.read_auction(auction["id"]) or {}
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
            self.lost(card, entry, auction)
            _forget(entry)
        elif result == "no_funds":
            log.warning("Achat : solde insuffisant pour miser %d sur « %s ».", amount, card.name)
        else:
            log.warning("Achat : la mise sur « %s » n'a pas été confirmée par le site.", card.name)

    def watch_until_end(self, card: BuyCard, entry: dict, auction: dict) -> None:
        """Surveille l'enchère de près à partir de watch_start() avant la fin (relue chaque
        seconde) et surenchérit dès que vous n'êtes plus en tête à SNIPE_LEAD de la fin,
        dans la limite, puis à chaque fois que vous êtes dépassé. Une mise des 10
        dernières secondes prolonge l'enchère de 60 s : on continue jusqu'à la fin réelle.
        Une lecture sans réponse est simplement refaite la seconde suivante."""
        lead = self.config.snipe_lead
        start = watch_start(lead)
        end = parse_time(auction.get("end_at"))
        if not end or end - self.now() > start + SNIPE_STARTUP + timedelta(minutes=2):
            return  # pas encore l'heure : la boucle reviendra
        deadline = time.monotonic() + WATCH_GUARD.total_seconds()
        announced = False
        while time.monotonic() < deadline:
            wait = (end - start - self.now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            if not announced:
                log.info("Achat : surveillance de « %s » jusqu'à la fin (%s), surenchère à %d s de la fin.",
                         card.name, auction.get("end_at"), lead.total_seconds())
                announced = True
            fresh = self.site.read_auction(auction["id"])
            if fresh is None:  # le site ne répond pas : on relit dans une seconde
                if end - self.now() < -timedelta(minutes=2):
                    return  # la passe suivante verra si elle est gagnée
                time.sleep(WATCH_POLL)
                continue
            auction = fresh
            if auction.get("status") != "active":
                self.settle(card, entry, auction)
                return
            end = parse_time(auction.get("end_at")) or end
            entry["end_at"] = auction.get("end_at")
            if auction.get("current_bidder_id") in self.rivals:
                log.info("Achat : « %s » — un autre compte du bot mène cette enchère ; elle lui est laissée.",
                         card.name)
                _forget(entry)
                return
            remaining = end - self.now()
            if not self.leading(auction) and remaining <= lead:
                self.fast_bid(card, entry, auction)
                if entry.get("status") != "bidding" or self.dry_run:
                    return
                continue  # relire tout de suite
            if remaining < -timedelta(seconds=5):
                self.settle(card, entry, auction)
                return
            time.sleep(WATCH_POLL)

    def fast_bid(self, card: BuyCard, entry: dict, auction: dict) -> None:
        """Surenchère de dernière seconde : la mise minimale, calculée sur l'enchère qu'on
        vient de relire, envoyée directement par l'API ; si elle est refusée (l'enchère a
        bougé), l'enchère est relue et la mise refaite."""
        amount = min_next_bid(auction)
        if amount > card.max_price:
            log.info("Achat : « %s » — mise minimale %d au-dessus de la limite %d ; abandon.",
                     card.name, amount, card.max_price)
            self.lost(card, entry, auction)
            _forget(entry)
            return
        if self.config.max_total_bids and self.budget_used + amount > self.config.max_total_bids:
            log.info("Achat : budget MAX_TOTAL_BIDS atteint (%d engagés) ; pas de mise sur « %s ».",
                     self.budget_used, card.name)
            _forget(entry)
            return
        if self.dry_run:
            log.info("Simulation : surenchère de %d sur « %s ».", amount, card.name)
            _forget(entry)
            return
        if self.site.post_bid(auction["id"], amount, self.deadline(auction), self.state.my_id):
            self.budget_used += amount
            entry["placed"] = auction["id"]
            entry["my_bid"] = {"auction": auction["id"], "amount": amount}
            log.info("Achat : surenchère de %d sur « %s » à %.0f s de la fin.", amount, card.name,
                     (parse_time(auction.get("end_at")) - self.now()).total_seconds())
            return
        self.bid_if_needed(card, entry, auction)

