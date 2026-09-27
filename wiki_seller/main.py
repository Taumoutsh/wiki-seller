"""Point d'entrée : une passe de vente, ou une boucle qui se relance 1h après la
dernière mise en vente."""

import argparse
import dataclasses
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from playwright.sync_api import sync_playwright

from .config import Config, ConfigError, load_config
from .selection import (
    SafeCardsError,
    build_sale_plan,
    copies_available,
    load_safe_cards,
    parse_collection,
)
from .site import ListingStatus, PriceApiUnavailable, SessionExpired, WikiMasters

log = logging.getLogger("wiki_seller")

RESTART_MARGIN = timedelta(minutes=1)
RETRY_AFTER_ERROR = timedelta(minutes=10)
PRICE_CACHE_TTL = timedelta(hours=3)
PRICE_SAVE_EVERY = 50


@dataclass
class RunResult:
    listed: int
    last_listing_at: datetime | None
    slots_full: bool
    earliest_auction_end: datetime | None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────── État persistant ───────────────────────────


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_price_cache(config: Config) -> dict:
    cache = read_json(config.state_dir / "price_cache.json", {})
    limit = (now_utc() - PRICE_CACHE_TTL).isoformat()
    return {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("at", "") > limit}


def save_price_cache(config: Config, cache: dict) -> None:
    write_json(config.state_dir / "price_cache.json", cache)


# ─────────────────────────── Une passe ───────────────────────────


def run_once(config: Config, dry_run: bool = False, debug: bool = False) -> RunResult:
    safe_names = load_safe_cards(config.safe_cards_file)
    log.info("%d carte(s) protégée(s) dans %s.", len(safe_names), config.safe_cards_file)
    config.state_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless, executable_path=config.chromium_executable)
        storage = str(config.storage_state_file) if config.storage_state_file.exists() else None
        context = browser.new_context(
            storage_state=storage, locale="fr-FR", timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(20000)
        page = context.new_page()
        site = WikiMasters(page, config, debug=debug)
        try:
            site.ensure_logged_in()
            context.storage_state(path=str(config.storage_state_file))
            return _sell(site, config, safe_names, dry_run)
        finally:
            try:
                context.storage_state(path=str(config.storage_state_file))
            except Exception:
                pass
            browser.close()


def _sell(site: WikiMasters, config: Config, safe_names: set[str], dry_run: bool) -> RunResult:
    market = site.fetch_market_state()
    log.info("Enchères en cours : %d/%d.", len(market.selling), market.max_auctions)
    if market.free_slots == 0:
        log.info("Aucune place libre aux enchères : rien à faire pour l'instant.")
        return RunResult(0, None, True, market.earliest_end())

    entries, in_trade = site.fetch_collection(config.sell_rarities)
    groups = parse_collection(entries, excluded_copy_ids=in_trade)
    sellable = [g for g in groups if copies_available(g, safe_names, market.selling) > 0]
    log.info("%d carte(s) distincte(s), dont %d avec des exemplaires à vendre.", len(groups), len(sellable))

    # Prix moyens via l'API pour classer les cartes de la plus chère à la moins chère.
    cache = load_price_cache(config)
    averages: dict[str, float | None] = {}
    unknown: set[str] = set()
    price_api_ok = True
    fetched = 0
    for group in sellable:
        key = f"{group.card_id}:{group.rarity}"
        if key in cache:
            averages[group.card_id] = cache[key]["average"]
            continue
        if not price_api_ok:
            unknown.add(group.card_id)
            continue
        try:
            averages[group.card_id] = site.fetch_average(group.card_id, group.rarity)
        except PriceApiUnavailable as exc:
            log.warning("API des prix indisponible (%s) : prix lus dans la fenêtre d'enchère, "
                        "sans classement pour les cartes concernées.", exc)
            price_api_ok = False
            unknown.add(group.card_id)
            continue
        cache[key] = {"average": averages[group.card_id], "at": now_utc().isoformat()}
        fetched += 1
        if fetched % PRICE_SAVE_EVERY == 0:
            save_price_cache(config, cache)  # une passe interrompue ne perd pas les prix déjà lus
        time.sleep(0.3)
    save_price_cache(config, cache)

    plan = build_sale_plan(groups, safe_names, averages, market.selling, config.price_ratio, unknown)
    if plan.skipped:
        log.info("%d carte(s) ignorée(s) faute de prix moyen, ex. : %s", len(plan.skipped),
                 ", ".join(f"{t} ({r})" for t, r in plan.skipped[:10]))
    if not plan.items:
        log.info("Aucune carte à mettre en vente.")
        return RunResult(0, None, False, market.earliest_end())

    free = market.free_slots
    log.info("%d place(s) libre(s). Ordre de vente : %s", free,
             ", ".join(f"{i.title} (~{i.average:g})" if i.average else f"{i.title} (prix ?)"
                       for i in plan.items[:free + 5]))

    listed = 0
    last_listing_at = None
    slots_full = False
    for item in plan.items:
        if listed >= free or slots_full:
            break
        for _ in range(item.copies_to_sell):
            if listed >= free:
                break
            result = site.list_card(item.title, item.average, dry_run)
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
                log.warning("%s : %s%s", item.title, result.status.value,
                            f" ({result.detail})" if result.detail else "")
                break  # carte suivante
            time.sleep(random.uniform(2, 5))

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


# ─────────────────────────── Boucle ───────────────────────────


def next_run_at(result: RunResult, config: Config, now: datetime) -> datetime:
    """RUN_INTERVAL (par défaut la durée des enchères) après la dernière mise en vente ;
    si les places sont déjà prises, dès la fin de la première enchère ; sinon dans
    RUN_INTERVAL."""
    interval = config.pass_interval
    if result.last_listing_at:
        target = result.last_listing_at + interval + RESTART_MARGIN
    elif result.slots_full and result.earliest_auction_end:
        target = result.earliest_auction_end + RESTART_MARGIN
    elif result.slots_full:
        stored = read_json(config.run_state_file, {}).get("last_listing_at")
        target = (datetime.fromisoformat(stored) + interval + RESTART_MARGIN
                  if stored else now + interval)
    else:
        target = now + interval
    return max(target, now + timedelta(minutes=1))


def loop(config: Config, dry_run: bool, debug: bool) -> None:
    while True:
        try:
            result = run_once(config, dry_run=dry_run, debug=debug)
            wake = next_run_at(result, config, now_utc())
        except (SafeCardsError, ConfigError):
            raise
        except SessionExpired as exc:
            log.warning("Session expirée (%s) : nouvelle connexion à la prochaine passe.", exc)
            config.storage_state_file.unlink(missing_ok=True)
            wake = now_utc() + timedelta(minutes=1)
        except Exception:
            log.exception("Erreur pendant la passe ; nouvel essai dans 10 minutes.")
            wake = now_utc() + RETRY_AFTER_ERROR
        log.info("Prochaine passe à %s.", wake.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
        time.sleep(max(0.0, (wake - now_utc()).total_seconds()))


def setup_logging(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(config.state_dir / "seller.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki_seller", description="Mise aux enchères automatique sur WikiMasters.")
    parser.add_argument("--dry-run", action="store_true", help="tout faire sauf cliquer sur « Lancer l'enchère »")
    parser.add_argument("--loop", action="store_true", help="tourner en continu (relance RUN_INTERVAL après la dernière vente)")
    parser.add_argument("--headed", action="store_true", help="afficher le navigateur")
    parser.add_argument("--debug", action="store_true", help="captures d'écran à chaque étape dans state/debug/")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    if args.headed:
        config = dataclasses.replace(config, headless=False)
    setup_logging(config)

    try:
        if args.loop:
            loop(config, args.dry_run, args.debug)
        else:
            run_once(config, dry_run=args.dry_run, debug=args.debug)
    except SafeCardsError as exc:
        log.error("%s", exc)
        return 2
    except SessionExpired as exc:
        config.storage_state_file.unlink(missing_ok=True)
        log.error("Session expirée (%s) : relancez le script pour vous reconnecter.", exc)
        return 1
    except Exception:
        log.exception("Échec de la passe.")
        return 1
    return 0
