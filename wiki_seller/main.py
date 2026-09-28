"""Point d'entrée : une passe (paquets la nuit, achats, ventes), ou une boucle qui se
relance RUN_INTERVAL après la dernière mise en vente et se réveille aussi juste avant
la fin des enchères suivies pour surenchérir."""

import argparse
import dataclasses
import json
import logging
import random
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import sync_playwright

from .buying import WantedCardsError, WantedState, auction_title, run_bids
from .config import Config, ConfigError, load_config
from .selection import (
    SafeCardsError,
    adjust_to_market,
    build_sale_plan,
    competitor_price,
    copies_available,
    load_safe_cards,
    normalize_name,
    parse_collection,
)
from .site import ListingStatus, PriceApiUnavailable, SessionExpired, WikiMasters
from .trace import install_api_trace

log = logging.getLogger("wiki_seller")

RESTART_MARGIN = timedelta(minutes=1)
RETRY_AFTER_ERROR = timedelta(minutes=10)
PRICE_CACHE_TTL = timedelta(hours=3)
PRICE_SAVE_EVERY = 50
RETRY_LISTING_AFTER = 5  # secondes
PASS_DURATION_GUARD = timedelta(minutes=10)  # durée maximale d'une passe, pour ne pas manquer un réveil
SITE_TIMEZONE = "Europe/Paris"


@dataclass
class RunResult:
    listed: int
    last_listing_at: datetime | None
    slots_full: bool
    earliest_auction_end: datetime | None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(SITE_TIMEZONE))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def in_packs_window(config: Config, now: datetime) -> bool:
    if not config.packs_hours:
        return False
    start, end = config.packs_hours
    return start <= now.hour < end if start < end else (now.hour >= start or now.hour < end)


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


@contextmanager
def open_site(config: Config, debug: bool):
    """Navigateur connecté au site ; la session est enregistrée à la sortie."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless, executable_path=config.chromium_executable)
        storage = str(config.storage_state_file) if config.storage_state_file.exists() else None
        context = browser.new_context(
            storage_state=storage, locale="fr-FR", timezone_id=SITE_TIMEZONE,
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(20000)
        if config.api_trace:
            install_api_trace(context, config.base_url, config.api_trace_file)
        site = WikiMasters(context.new_page(), config, debug=debug)
        try:
            site.ensure_logged_in()
            context.storage_state(path=str(config.storage_state_file))
            yield site
        finally:
            try:
                context.storage_state(path=str(config.storage_state_file))
            except Exception:
                pass
            browser.close()


def run_once(config: Config, dry_run: bool = False, debug: bool = False) -> RunResult:
    safe_names = load_safe_cards(config.safe_cards_file)
    log.info("%d carte(s) protégée(s) dans %s.", len(safe_names), config.safe_cards_file)
    with open_site(config, debug) as site:
        site.refresh_market()
        now = local_now()
        if in_packs_window(config, now):
            log.info("Paquets : il est %s, dans la plage %s h : ouverture.", now.strftime("%H:%M"),
                     "-".join(map(str, config.packs_hours)))
            _open_packs(site, dry_run)
        elif config.packs_hours:
            log.info("Paquets : il est %s, hors de la plage %s h.", now.strftime("%H:%M"),
                     "-".join(map(str, config.packs_hours)))
        _buy(site, config, dry_run)
        return _sell(site, config, safe_names, dry_run)


def run_snipe(config: Config, dry_run: bool = False, debug: bool = False) -> None:
    """Réveil juste avant la fin d'une enchère suivie : surenchérir si besoin."""
    with open_site(config, debug) as site:
        run_bids(site, config, dry_run, snipe_only=True)


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


def _list(site: WikiMasters, config: Config, item, dry_run: bool, copy_index: int = 0,
          my_auction_ids: set[str] = frozenset()):
    """Par l'API quand le prix moyen est connu ; sinon par la fenêtre d'enchère, qui
    affiche la « Moyenne ». Le n-ième exemplaire vendu dans la passe part avec le n-ième
    identifiant d'exemplaire (le premier vient d'être mis en vente)."""
    if config.actions_via_api and item.average:
        k = copy_index % len(item.copy_ids) if item.copy_ids else 0
        copies = item.copy_ids[k:] + item.copy_ids[:k]
        adjust = market_adjuster(site, config, item.title, my_auction_ids)
        return site.list_card_api(item.card_id, copies, item.title, item.average, dry_run, adjust,
                                  sale_duration(config))
    adjust = market_adjuster(site, config, item.title, my_auction_ids)
    return site.list_card(item.title, item.average, dry_run, adjust, sale_duration(config))


def sale_duration(config: Config) -> str:
    label = config.duration_label_at(local_now().hour)
    if label != config.auction_duration_label:
        log.info("Durée de nuit : enchère de %s.", label)
    return label


def _open_packs(site: WikiMasters, dry_run: bool) -> None:
    try:
        opened = (site.open_packs_api if site.config.actions_via_api else site.open_packs)(dry_run)
        if opened:
            log.info("Paquets : %d ouvert(s).", opened)
    except SessionExpired:
        raise
    except Exception as exc:  # les ventes passent quand même
        log.warning("Ouverture des paquets interrompue : %s", str(exc).splitlines()[0])


def _buy(site: WikiMasters, config: Config, dry_run: bool) -> None:
    try:
        run_bids(site, config, dry_run)
    except SessionExpired:
        raise
    except WantedCardsError as exc:
        log.error("Achats ignorés : %s", exc)
    except Exception as exc:  # les ventes passent quand même
        log.warning("Achats interrompus : %s", str(exc).splitlines()[0])


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

    my_ids = {a.get("id") for a in market.selling if isinstance(a, dict)}
    listed = 0
    last_listing_at = None
    slots_full = False
    for item in plan.items:
        if listed >= free or slots_full:
            break
        for copy_index in range(item.copies_to_sell):
            if listed >= free:
                break
            result = _list(site, config, item, dry_run, copy_index, my_ids)
            if result.status is ListingStatus.FAILED:
                # Souvent passager (« Le chargement de la collection a échoué », fenêtre lente).
                log.info("%s : échec (%s), nouvel essai.", item.title, result.detail)
                time.sleep(RETRY_LISTING_AFTER)
                result = _list(site, config, item, dry_run, copy_index, my_ids)
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
        # Au plus RUN_INTERVAL : avec des ventes de nuit de 12 h, on ne dort pas jusqu'à
        # leur fin (paquets, achats).
        target = min(result.earliest_auction_end + RESTART_MARGIN, now + interval)
    elif result.slots_full:
        stored = read_json(config.run_state_file, {}).get("last_listing_at")
        target = (datetime.fromisoformat(stored) + interval + RESTART_MARGIN
                  if stored else now + interval)
    else:
        target = now + interval
    return max(target, now + timedelta(minutes=1))


def next_wake(next_pass: datetime, config: Config, now: datetime) -> tuple[datetime, bool]:
    """Prochain réveil : la passe normale, ou avant, la fin d'une enchère suivie.
    Renvoie (heure, True si c'est un réveil de surenchère)."""
    snipe = WantedState(config.wanted_state_file).next_snipe_at(config.snipe_lead, now)
    # Une passe dure plusieurs minutes : un réveil qui tomberait pendant la passe passe avant.
    if snipe and snipe < next_pass + PASS_DURATION_GUARD:
        return snipe, True
    return next_pass, False


def loop(config: Config, dry_run: bool, debug: bool) -> None:
    next_pass = now_utc()
    snipe = False
    while True:
        try:
            if snipe:
                run_snipe(config, dry_run=dry_run, debug=debug)
            else:
                result = run_once(config, dry_run=dry_run, debug=debug)
                next_pass = next_run_at(result, config, now_utc())
        except (SafeCardsError, ConfigError):
            raise
        except SessionExpired as exc:
            log.warning("Session expirée (%s) : nouvelle connexion à la prochaine passe.", exc)
            config.storage_state_file.unlink(missing_ok=True)
            next_pass = now_utc() + timedelta(minutes=1)
        except Exception:
            log.exception("Erreur pendant la passe ; nouvel essai dans 10 minutes.")
            if not snipe:
                next_pass = now_utc() + RETRY_AFTER_ERROR
        wake, snipe = next_wake(next_pass, config, now_utc())
        log.info("Prochain réveil à %s%s.", wake.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                 " (surenchère avant la fin d'une enchère)" if snipe else "")
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
    parser.add_argument("--dry-run", action="store_true", help="tout simuler : ni « Lancer l'enchère », ni « Miser », ni ouverture de paquets")
    parser.add_argument("--loop", action="store_true", help="tourner en continu (relance RUN_INTERVAL après la dernière vente)")
    parser.add_argument("--headed", action="store_true", help="afficher le navigateur")
    parser.add_argument("--trace-api", action="store_true",
                        help="noter les appels à l'API du site dans state/api.log (sans cookies)")
    parser.add_argument("--debug", action="store_true", help="captures d'écran à chaque étape dans state/debug/")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    if args.headed:
        config = dataclasses.replace(config, headless=False)
    if args.trace_api:
        config = dataclasses.replace(config, api_trace=True)
    setup_logging(config)
    log.info("Démarrage%s (actions par %s) : raretés %s, paquets %s, enchères « %s », passe toutes les %s, achats %s.",
             " (simulation)" if args.dry_run else "", "l'API" if config.actions_via_api else "la page",
             ",".join(config.sell_rarities) or "toutes",
             "-".join(map(str, config.packs_hours)) + " h" if config.packs_hours else "désactivés",
             config.auction_duration_label, config.pass_interval, config.wanted_cards_file)

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
