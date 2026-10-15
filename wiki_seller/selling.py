"""Passe de vente : les cartes des raretés SELL_RARITIES, de la plus chère à la moins
chère, dans les places libres aux enchères (5 à la fois sur le site)."""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime

from .buying import WantedState, auction_title, my_user_id
from .config import Config
from .pricing import fetch_averages
from .selection import (
    SaleItem,
    adjust_to_market,
    build_sale_plan,
    competitor_price,
    copies_available,
    normalize_name,
    parse_collection,
)
from .site import ListingResult, ListingStatus, SessionExpired, WikiMasters
from .state import now_utc, paris_now, write_json
from .unsold import UnsoldTracker

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
    return site.list_card(item.card_id, copies, item.title, item.average, dry_run,
                          market_adjuster(site, config, item.title, my_auction_ids), sale_duration(config))


def sell(site: WikiMasters, config: Config, safe_names: set[str], dry_run: bool) -> RunResult:
    market = site.fetch_market_state()
    log.info("Enchères en cours : %d/%d.", len(market.selling), market.max_auctions)
    if market.free_slots == 0:
        log.info("Aucune place libre aux enchères : rien à faire pour l'instant.")
        return RunResult(0, None, True, market.earliest_end())

    entries, in_trade = site.fetch_collection(config.sell_rarities)
    groups = parse_collection(entries, excluded_copy_ids=in_trade)
    sellable = [g for g in groups if copies_available(g, safe_names, market.selling) > 0]

    # Cartes qui ne se vendent pas : mises de côté quelques jours (state/unsold.json).
    unsold = UnsoldTracker(config.unsold_file, config.unsold_max_tries, config.unsold_cooldown)
    unsold.update(market.history, my_user_id(market.mine, WantedState(config.wanted_state_file).my_id), now_utc())
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
    plan = build_sale_plan(groups, safe_names, averages, market.selling, config.price_ratio, unknown)
    if plan.skipped:
        log.info("%d carte(s) ignorée(s), ex. : %s", len(plan.skipped),
                 ", ".join(f"{t} ({r})" for t, r in plan.skipped[:10]))
    if not plan.items:
        log.info("Aucune carte à mettre en vente.")
        return RunResult(0, None, False, market.earliest_end())

    free = market.free_slots
    log.info("%d place(s) libre(s). Ordre de vente : %s", free,
             ", ".join(f"{i.title} (~{i.average:g})" for i in plan.items[:free + 5]))

    my_ids = {a.get("id") for a in market.selling if isinstance(a, dict)}
    listed, last_listing_at, slots_full = 0, None, False
    for item in plan.items:
        if listed >= free or slots_full:
            break
        for copy_index in range(item.copies_to_sell):
            if listed >= free:
                break
            result = list_item(site, config, item, dry_run, copy_index, my_ids)
            if result.status is ListingStatus.FAILED:  # souvent passager
                log.info("%s : échec (%s), nouvel essai.", item.title, result.detail)
                time.sleep(RETRY_LISTING_AFTER)
                result = list_item(site, config, item, dry_run, copy_index, my_ids)
            if result.status in (ListingStatus.LISTED, ListingStatus.DRY_RUN):
                listed += 1
                if result.status is ListingStatus.LISTED:
                    last_listing_at = now_utc()
                log.info("%s : %s à %d (prix moyen %g, %d%%).", result.status.value.capitalize(),
                         item.title, result.price, result.average, round(config.price_ratio * 100))
            elif result.status is ListingStatus.SLOTS_FULL:
                log.info("Le site indique que les enchères sont pleines (%s).", result.detail)
                slots_full = True
                break
            else:
                log.warning("%s : %s%s", item.title, result.status.value, f" ({result.detail})" if result.detail else "")
                break  # carte suivante
            time.sleep(random.uniform(*LISTING_PAUSE))

    if last_listing_at:
        write_json(config.run_state_file, {"last_listing_at": last_listing_at.isoformat()})
    log.info("Passe terminée : %d carte(s) %s.", listed, "simulée(s)" if dry_run else "mise(s) en vente")
    earliest = None
    if slots_full or listed >= free:
        try:
            earliest = site.fetch_market_state().earliest_end()
        except Exception:
            pass
    return RunResult(listed, last_listing_at, slots_full or listed >= free, earliest)
