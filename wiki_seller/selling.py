"""Passe de vente, dans les places libres aux enchères (5 à la fois sur le site) :
d'abord les cartes demandées (/sell, /buyandsell) au prix exact, remises en vente jusqu'à
ce qu'elles partent ; puis les cartes des raretés SELL_RARITIES, de la plus chère à la
moins chère, à PRICE_RATIO du prix moyen."""

import dataclasses
import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime

from .buying import WantedState, auction_title, my_user_id, rarity_matches
from .config import Config
from .lists import (AUTO, AUTO_SELL_RATIO, ListError, SellCard, buy_cards, group_matches, record_sale, remove_safe, sell_cards,
                    update_sell)
from . import collection, price_worker
from .pricing import PriceReader, fetch_averages, load_price_cache, reliable
from .settings import RARITIES
from .selection import (
    PROTECTED_COPIES_TO_KEEP,
    CardGroup,
    SaleItem,
    adjust_to_market,
    build_sale_plan,
    competitor_price,
    copies_available,
    copies_not_on_sale,
    count_listed,
    normalize_name,
    parse_collection,
)
from .site import ListingResult, ListingStatus, SessionExpired, WikiMasters, entry_rarity
from .state import now_utc, paris_now, parse_time, write_json
from .telegram import notify
from .unsold import UnsoldTracker, auction_outcome

log = logging.getLogger(__name__)

RETRY_LISTING_AFTER = 5  # s avant de réessayer une mise en vente refusée
LISTING_PAUSE = (2, 5)  # s entre deux mises en vente, pour garder un rythme humain


@dataclass
class RunResult:
    listed: int
    last_listing_at: datetime | None
    slots_full: bool
    earliest_auction_end: datetime | None


def sale_duration(config: Config) -> str:
    label = config.duration_label_at(paris_now().hour)
    if label != config.auction_duration_label:
        log.info("Durée de nuit : enchère de %s.", label)
    return label


def market_adjuster(site: WikiMasters, config: Config, title: str, my_auction_ids: set[str]):
    """Fonction (prix de base, prix moyen) → prix ajusté d'après les enchères en cours de
    la même carte (hors les vôtres), ou None si l'ajustement est désactivé."""
    if not config.market_adjust:
        return None

    def adjust(base: int, average: float) -> int:
        try:
            auctions = site.search_auctions(title)
        except SessionExpired:
            raise
        except Exception as exc:
            log.warning("%s : enchères en cours illisibles (%s), prix non ajusté.", title, exc)
            return base
        prices = [p for a in auctions
                  if normalize_name(auction_title(a)) == normalize_name(title)
                  and a.get("status", "active") == "active" and a.get("id") not in my_auction_ids
                  and (p := competitor_price(a))]
        result = adjust_to_market(base, average, prices, config.price_floor_ratio)
        if result.mean is None:
            log.info("%s : %d enchère(s) en cours, prix non ajusté (%d).", title, result.competitors, base)
        else:
            log.info("%s : prix %d → %d (%d enchère(s) en cours, moyenne %.0f, écart type %.0f).",
                     title, base, result.price, result.competitors, result.mean, result.stdev)
        return result.price

    return adjust


def list_item(site: WikiMasters, config: Config, item: SaleItem, dry_run: bool, copy_index: int,
              my_auction_ids: set[str]) -> ListingResult:
    """Le n-ième exemplaire vendu dans la passe part avec le n-ième identifiant
    d'exemplaire (le premier vient d'être mis en vente)."""
    k = copy_index % len(item.copy_ids) if item.copy_ids else 0
    copies = item.copy_ids[k:] + item.copy_ids[:k]
    if item.exact:  # durée demandée, sinon la durée habituelle (nuit comprise)
        return site.list_card(item.card_id, copies, item.title, None, dry_run, None,
                              item.duration or sale_duration(config), price=item.price)
    return site.list_card(item.card_id, copies, item.title, item.average, dry_run,
                          market_adjuster(site, config, item.title, my_auction_ids), sale_duration(config))


class _Listings:
    """Mises en vente de la passe, dans la limite des places libres."""

    def __init__(self, site: WikiMasters, config: Config, dry_run: bool, free: int, my_ids: set[str]):
        self.site, self.config, self.dry_run, self.free, self.my_ids = site, config, dry_run, free, my_ids
        self.listed, self.last_listing_at, self.slots_full = 0, None, False

    @property
    def room(self) -> bool:
        return self.listed < self.free and not self.slots_full

    def list(self, item: SaleItem, copy_index: int) -> bool:
        """Met en vente un exemplaire ; False s'il faut passer à la carte suivante."""
        result = list_item(self.site, self.config, item, self.dry_run, copy_index, self.my_ids)
        if result.status is ListingStatus.FAILED and "pas de réponse" not in result.detail:  # souvent passager
            log.info("%s : échec (%s), nouvel essai.", item.title, result.detail)
            time.sleep(RETRY_LISTING_AFTER)
            result = list_item(self.site, self.config, item, self.dry_run, copy_index, self.my_ids)
        if result.status in (ListingStatus.LISTED, ListingStatus.DRY_RUN):
            self.listed += 1
            if result.status is ListingStatus.LISTED:
                self.last_listing_at = now_utc()
            if item.exact:
                log.info("%s : %s à %d (prix demandé).", result.status.value.capitalize(), item.title, result.price)
            else:
                log.info("%s : %s à %d (%s %g, %d%%).", result.status.value.capitalize(), item.title, result.price,
                         "dernier recours, vente la plus basse connue" if item.last_resort else "prix moyen",
                         result.average, round(self.config.price_ratio * 100))
            time.sleep(random.uniform(*LISTING_PAUSE))
            return True
        if result.status is ListingStatus.NOT_OWNED:  # ni nouvel essai, ni remise en vente avant relecture
            removed = collection.drop_card(self.config, item.card_id, item.rarity)
            log.info("%s : le site dit que vous ne la possédez plus (%s) ; retirée de la collection gardée "
                     "(%d entrée(s)), pas de nouvel essai.", item.title, result.detail, removed)
            return False
        if result.status is ListingStatus.SLOTS_FULL:
            log.info("Le site indique que les enchères sont pleines (%s).", result.detail)
            self.slots_full = True
        else:
            log.warning("%s : %s%s", item.title, result.status.value, f" ({result.detail})" if result.detail else "")
        return False


# ─────────────────────────── Ventes à prix exact (/sell) ───────────────────────────


def priority_cards(config: Config) -> list[SellCard]:
    try:
        return sell_cards(config)
    except ListError as exc:
        log.error("Ventes demandées ignorées : %s", exc)
        return []


def record_sales(config: Config, cards: list[SellCard], history: list, my_id: str | None,
                 dry_run: bool) -> None:
    """Ventes conclues des cartes à vendre (historique du site) : notées et annoncées ; la
    carte quitte la liste une fois tous ses exemplaires vendus. Seules comptent les ventes
    conclues après la demande."""
    for card in cards:
        added = parse_time(card.added_at)
        sold = set(card.sold)
        for auction in sorted((a for a in history or [] if isinstance(a, dict)),
                              key=lambda a: a.get("settled_at") or a.get("end_at") or ""):
            settled = parse_time(auction.get("settled_at") or auction.get("end_at"))
            if (auction.get("id") in sold or auction_outcome(auction) != "sold"
                    or (my_id and auction.get("seller_id") != my_id)
                    or normalize_name(auction_title(auction)) != card.key or not rarity_matches(auction, card)
                    or (added and (not settled or settled < added))):
                continue
            sold.add(auction["id"])
            price = auction.get("final_price")
            if dry_run:
                log.info("Simulation : vente de « %s » à %s (non notée).", card.name, price)
                continue
            updated = record_sale(config, card.name, auction["id"], card.rarity)
            left = updated.remaining if updated else 0
            log.info("Vente demandée conclue : « %s » à %s%s.", card.name, price,
                     f" ; encore {left} exemplaire(s) à vendre" if left > 0 else " ; retirée de la liste")
            notify(config, f"💰 Vendue : {card.name} à {price}"
                           + (f" (encore {left} à vendre)" if left > 0 else ""))
            if left <= 0:
                break


def priority_items(config: Config, cards: list[SellCard], groups: list[CardGroup], selling: list[dict],
                   safe_names: set[str], dry_run: bool, auto_price=None) -> list[SaleItem]:
    """Exemplaires des cartes à vendre à mettre en vente, au prix demandé (prix « auto » :
    auto_price(carte de la collection), None tant que le prix du marché est inconnu). Une carte
    protégée en un seul exemplaire quitte les cartes protégées ; en plusieurs, elle le
    reste et le dernier exemplaire est gardé. Une carte absente de la collection est
    signalée une fois et reste dans la liste."""
    items = []
    for card in cards:
        if card.remaining <= 0:
            continue
        matching = [g for g in groups if normalize_name(g.title) == card.key and group_matches(g, card.rarity)]
        matching.sort(key=lambda g: RARITIES.index(g.rarity) if g.rarity in RARITIES else len(RARITIES))
        if card.price == AUTO and matching:  # prix d'une rareté : la plus rare possédée d'abord
            matching = [g for g in matching if g.rarity == matching[0].rarity]
        owned = sum(g.count for g in matching)
        if not owned:
            log.info("Vente demandée : « %s » introuvable dans la collection.", card.name)
            if not card.missing_notified and not dry_run:
                update_sell(config, card.name, card.rarity, missing_notified=True)
                notify(config, f"❓ « {card.name} »{f' {card.rarity}' if card.rarity else ''} est introuvable dans "
                               "votre collection : elle reste dans "
                               "les ventes et partira dès qu'elle y sera (/unsell pour l'annuler).")
            continue
        if not dry_run and (card.missing_notified or card.name != matching[0].title):
            update_sell(config, card.name, card.rarity, missing_notified=False, name=matching[0].title)  # nom exact
        card = dataclasses.replace(card, name=matching[0].title)
        keep_one = card.keep_one
        if keep_one is None:  # première fois que la carte est trouvée
            keep_one = card.key in safe_names and owned > 1
            card = _settle_protection(config, card, keep_one, owned, safe_names, dry_run)
        listed = sum(count_listed(g, selling) for g in matching)
        keep = PROTECTED_COPIES_TO_KEEP if keep_one and card.key in safe_names else 0  # /unsafe : tout part
        count = min(card.remaining - listed, owned - listed - keep)
        if count <= 0:
            log.info("Vente demandée : « %s » — %d exemplaire(s) déjà en vente.", card.name, listed)
            continue
        copy_ids = tuple(c for g in matching for c in copies_not_on_sale(g, selling))
        price = card.price
        if price == AUTO:
            price = auto_price(matching[0]) if auto_price else None
            if not price:
                log.info("Vente demandée : « %s » — prix du marché inconnu pour l'instant ; vente reportée.",
                         card.name)
                continue
            log.info("Vente demandée : « %s » à %d (%d %% du prix du marché).", card.name, price,
                     round(AUTO_SELL_RATIO * 100))
        items.append(SaleItem(matching[0].card_id, matching[0].title, matching[0].rarity, 0, price,
                              count, copy_ids, exact=True, duration=card.duration))
    return items


def auto_sell_price(reader: PriceReader, group: CardGroup, min_sales: int = 1) -> int | None:
    """Prix de vente « auto » : AUTO_SELL_RATIO du prix du marché, s'il repose sur assez de
    ventes (ventes_min) pour s'y fier."""
    _, price = reader.read(group)
    if price and not reliable(reader.entry(group), min_sales):
        log.info("Vente demandée : « %s » — %s vente(s) connue(s) seulement (il en faut %d) pour un prix auto ; "
                 "vente reportée.", group.title, reader.entry(group).get("count"), min_sales)
        return None
    return max(1, math.floor(price * AUTO_SELL_RATIO)) if price else None


def _settle_protection(config: Config, card: SellCard, keep_one: bool, owned: int, safe_names: set[str],
                       dry_run: bool) -> SellCard:
    """Carte protégée mise en vente : décidé une fois pour toutes, à la première passe
    qui la trouve (sinon, une fois les doublons vendus, le dernier exemplaire partirait)."""
    copies = card.copies
    if keep_one and card.remaining > owned - PROTECTED_COPIES_TO_KEEP:
        copies = len(card.sold) + owned - PROTECTED_COPIES_TO_KEEP
        message = (f"🛡️ « {card.name} » est protégée : le dernier exemplaire est gardé, "
                   f"{owned - PROTECTED_COPIES_TO_KEEP} exemplaire(s) mis en vente.")
        log.info("Vente demandée : %s", message)
        if not dry_run:
            notify(config, message)
    elif card.key in safe_names and not keep_one:
        log.info("Vente demandée : « %s » (un seul exemplaire) n'est plus protégée.", card.name)
        if not dry_run:
            remove_safe(config, card.name)
            notify(config, f"🛡️ « {card.name} » (un seul exemplaire) n'est plus protégée : elle est mise en vente.")
    if not dry_run:
        update_sell(config, card.name, card.rarity, keep_one=keep_one, copies=copies)
    return dataclasses.replace(card, keep_one=keep_one, copies=copies)


# ─────────────────────────── Passe ───────────────────────────


def sell(site: WikiMasters, config: Config, safe_names: set[str], dry_run: bool) -> RunResult:
    market = site.fetch_market_state()
    my_id = my_user_id(market.mine, WantedState(config.wanted_state_file).my_id)
    priority = priority_cards(config)
    if priority:
        record_sales(config, priority, market.history, my_id, dry_run)
        priority = [c for c in priority_cards(config) if c.remaining > 0]
    log.info("Enchères en cours : %d/%d.", len(market.selling), market.max_auctions)
    if market.free_slots == 0:
        log.info("Aucune place libre aux enchères : rien à faire pour l'instant.")
        return RunResult(0, None, True, market.earliest_end())
    if not priority and not config.selling:
        log.info("Ventes automatiques coupées (réglage raretes : aucune) et aucune vente demandée.")
        return RunResult(0, None, False, market.earliest_end())

    # Les cartes à vendre (/sell) sont de toutes raretés : la collection est alors lue en entier.
    entries, in_trade = collection.fetch(site, config, () if priority else config.sell_rarities)
    my_ids = {a.get("id") for a in market.selling if isinstance(a, dict)}
    listings = _Listings(site, config, dry_run, market.free_slots, my_ids)
    if priority:
        groups = parse_collection(entries, excluded_copy_ids=in_trade)
        reader = PriceReader(site, config)
        try:
            items = priority_items(config, priority, groups, market.selling, safe_names, dry_run,
                                   lambda group: auto_sell_price(reader, group, config.min_sales_to_sell))
        finally:
            reader.save()
        if items:
            log.info("Ventes demandées : %s", ", ".join(f"{i.title} ×{i.copies_to_sell} à {i.price}" for i in items))
        _list_all(listings, items)
        if config.sell_rarities:
            entries = [e for e in entries if entry_rarity(e) in config.sell_rarities]
    # Exemplaires visés par une vente demandée : laissés à celle-ci (les autres raretés de la
    # carte restent). Carte à acheter : jamais vendue automatiquement, même déjà obtenue en
    # partie ou gagnée sans que l'achat soit encore enregistré (protégée ensuite, ou revendue
    # au prix de /buyandsell).
    lines = [*priority, *buy_cards(config)]
    groups = [g for g in parse_collection(entries, excluded_copy_ids=in_trade)
              if not any(normalize_name(g.title) == c.key and group_matches(g, c.rarity) for c in lines)]
    if not config.selling:
        log.info("Ventes automatiques coupées (réglage raretes : aucune).")
    elif listings.room:
        _sell_plan(site, config, listings, groups, safe_names, market, my_id)
    else:
        log.info("Plus de place libre après les ventes demandées.")

    if listings.last_listing_at:
        write_json(config.run_state_file, {"last_listing_at": listings.last_listing_at.isoformat()})
    log.info("Passe terminée : %d carte(s) %s.", listings.listed, "simulée(s)" if dry_run else "mise(s) en vente")
    full = listings.slots_full or listings.listed >= listings.free
    earliest = None
    if full:
        try:
            earliest = site.fetch_market_state().earliest_end()
        except Exception:
            pass
    elif not listings.listed:
        earliest = market.earliest_end()
    return RunResult(listings.listed, listings.last_listing_at, full, earliest)


def _list_all(listings: _Listings, items: list[SaleItem]) -> None:
    for item in items:
        if not listings.room:
            return
        for copy_index in range(item.copies_to_sell):
            if not listings.room or not listings.list(item, copy_index):
                break


def _sell_plan(site: WikiMasters, config: Config, listings: _Listings, groups: list[CardGroup],
               safe_names: set[str], market, my_id: str | None) -> None:
    """Ventes habituelles (SELL_RARITIES) : PRICE_RATIO du prix moyen, des plus chères
    aux moins chères, dans les places restantes."""
    sellable = [g for g in groups if not g.shiny and copies_available(g, safe_names, market.selling) > 0]

    # Cartes qui ne se vendent pas : mises de côté quelques jours (state/unsold.json).
    unsold = UnsoldTracker(config.unsold_file, config.unsold_max_tries, config.unsold_cooldown)
    unsold.update(market.history, my_id, now_utc())
    unsold.save()
    paused = {g.card_id: until for g in sellable if (until := unsold.paused(g.card_id, g.title, now_utc()))}
    if paused:
        examples = [f"{g.title} (jusqu'au {paused[g.card_id].astimezone().strftime('%d/%m')})"
                    for g in sellable if g.card_id in paused]
        log.info("%d carte(s) invendue(s) mise(s) de côté, ex. : %s", len(paused), ", ".join(examples[:10]))
        sellable = [g for g in sellable if g.card_id not in paused]
        groups = [g for g in groups if g.card_id not in paused]
    log.info("%d carte(s) distincte(s), dont %d avec des exemplaires à vendre.", len(groups), len(sellable))

    averages, unknown, _ = fetch_averages(site, config, sellable)
    if unknown and price_worker.active():
        log.info("%d prix en cours de lecture en arrière-plan : ces cartes partiront à la passe suivante "
                 "(anticipée dès qu'ils sont lus).", len(unknown))
    cache = load_price_cache(config)
    plan = build_sale_plan(groups, safe_names, averages, market.selling, config.price_ratio, unknown,
                           {k: e.get("count") for k, e in cache.items()}, config.min_sales_to_sell,
                           {k: e.get("low") for k, e in cache.items()}, config.discard_below, config.discard_rarities)
    set_aside = [(t, r) for t, r in plan.skipped if r.startswith("mise de côté")]
    if set_aside:
        log.info("%d carte(s) mise(s) de côté, trop peu de ventes pour s'y fier : %s", len(set_aside),
                 ", ".join(f"{t} ({r.split(' : ', 1)[1]})" for t, r in set_aside[:20]))
    others = [(t, r) for t, r in plan.skipped if not r.startswith("mise de côté")]
    if others:
        log.info("%d carte(s) ignorée(s), ex. : %s", len(others), ", ".join(f"{t} ({r})" for t, r in others[:10]))
    if plan.items:
        free = listings.free - listings.listed
        log.info("%d place(s) libre(s). Ordre de vente : %s", free,
                 ", ".join(f"{i.title} (~{i.average:g})" for i in plan.items[:free + 5]))
        _list_all(listings, plan.items)
    if plan.set_aside and listings.room:  # plus rien d'autre à vendre : places libres quand même utilisées
        log.info("Plus d'autre carte à vendre : %d carte(s) mise(s) de côté vendue(s) en dernier recours, au "
                 "prix de leur vente la plus basse connue.", len(plan.set_aside))
        _list_all(listings, plan.set_aside)
    elif not plan.items:
        log.info("Aucune carte à mettre en vente.")
