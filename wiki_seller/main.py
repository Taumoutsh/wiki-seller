"""Point d'entrée : une passe (paquets la nuit, achats, ventes), ou une boucle qui se
relance RUN_INTERVAL après la dernière mise en vente et se réveille aussi juste avant
la fin des enchères suivies pour surenchérir."""

import argparse
import dataclasses
import json
import logging
import random
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import sync_playwright

from .buying import WantedCardsError, WantedState, auction_title, load_wanted_cards, my_user_id, run_bids
from .config import Config, ConfigError, load_config
from .discard import DiscardItem, SessionGate, discard_candidates, should_discard, total_copies
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
from .site import (
    CollectionScan,
    DiscardStatus,
    HumanVerificationRequired,
    ListingStatus,
    PriceApiUnavailable,
    SessionExpired,
    WikiMasters,
)
from .trace import install_api_trace
from .unsold import UnsoldTracker
from .verification import notify

log = logging.getLogger("wiki_seller")

RESTART_MARGIN = timedelta(minutes=1)
RETRY_AFTER_ERROR = timedelta(minutes=10)
PRICE_SAVE_EVERY = 50
PRICE_RETRY_PAUSE = 30  # s de pause après un refus passager de l'API des prix
PRICE_FAILURES_BEFORE_GIVING_UP = 5  # refus passagers de suite avant d'abandonner l'API des prix
RETRY_LISTING_AFTER = 5  # secondes
PACKS_VERIFICATION_PAUSE = timedelta(hours=12)  # après une demande de vérification humaine
PASS_DURATION_GUARD = timedelta(minutes=10)  # durée maximale d'une passe, pour ne pas manquer un réveil
SITE_TIMEZONE = "Europe/Paris"
DISCARD_PAUSE = (1.0, 3.0)  # secondes entre deux défausses, pour garder un rythme humain
DISCARD_RECHECK = timedelta(hours=1)  # délai avant de relancer une défausse terminée
DISCARD_RECHECK_UNPRICED = timedelta(minutes=15)  # idem s'il restait des prix à lire
DISCARD_PROGRESS_MAX_AGE = timedelta(hours=24)  # au-delà, la collection lue est trop ancienne
DISCARD_SAVE_EVERY_PAGES = 20  # pages de collection lues entre deux sauvegardes
DISCARD_SAVE_EVERY_CARDS = 20  # cartes examinées entre deux sauvegardes
DISCARD_LOG_EVERY = 100  # cartes examinées entre deux lignes de progression

# Un seul navigateur à la fois sur la session (voir discard.SessionGate).
GATE = SessionGate()


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


def write_json(path, data, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_price_cache(config: Config) -> dict:
    cache = read_json(config.state_dir / "price_cache.json", {})
    limit = (now_utc() - config.price_cache_ttl).isoformat()
    return {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("at", "") > limit}


def save_price_cache(config: Config, cache: dict) -> None:
    write_json(config.state_dir / "price_cache.json", cache)


# ─────────────────────────── Une passe ───────────────────────────


@contextmanager
def open_site(config: Config, debug: bool, background: bool = False):
    """Navigateur connecté au site ; la session est enregistrée à la sortie. Un seul à la
    fois : la défausse (background) attend et cède la place aux passes."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with GATE.background() if background else GATE.priority(), sync_playwright() as pw:
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
        result = _sell(site, config, safe_names, dry_run)
        check_discard(site, config, dry_run, debug)
        return result


def run_snipe(config: Config, dry_run: bool = False, debug: bool = False) -> None:
    """Réveil juste avant la fin d'une enchère suivie : surenchérir si besoin."""
    with open_site(config, debug) as site:
        run_bids(site, config, dry_run, snipe_only=True)
        check_discard(site, config, dry_run, debug)


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
    config = site.config
    pause_file = config.state_dir / "packs_pause.json"
    until = read_json(pause_file, {}).get("until")
    if until and datetime.fromisoformat(until) > now_utc():
        log.info("Paquets : ouverture automatique suspendue jusqu'au %s (vérification humaine demandée "
                 "par le site).", datetime.fromisoformat(until).astimezone().strftime("%d/%m %H:%M"))
        return
    try:
        opened = (site.open_packs_api if config.actions_via_api else site.open_packs)(dry_run)
        if opened:
            log.info("Paquets : %d ouvert(s).", opened)
    except HumanVerificationRequired as exc:
        # Le site exige un humain pour continuer : on ne contourne pas. Si PACKS_VERIFY_WAIT
        # est réglé, on prévient et on attend qu'une personne fasse la vérification dans ce
        # navigateur (écran distant) ; sinon, ou sans réponse, pause de PACKS_VERIFICATION_PAUSE.
        if config.packs_verify_wait and _human_verification(site, dry_run):
            return
        until = now_utc() + PACKS_VERIFICATION_PAUSE
        write_json(pause_file, {"until": until.isoformat(), "reason": str(exc)})
        log.warning("Paquets : %s Ouverture automatique suspendue jusqu'au %s ; ouvrez-les à la main "
                    "sur le site (la vérification s'y affiche).", exc,
                    until.astimezone().strftime("%d/%m %H:%M"))
    except SessionExpired:
        raise
    except Exception as exc:  # les ventes passent quand même
        log.warning("Ouverture des paquets interrompue : %s", str(exc).splitlines()[0])


def _human_verification(site: WikiMasters, dry_run: bool) -> bool:
    """Prévient, affiche la vérification et attend qu'une personne la fasse ; puis reprend
    l'ouverture. Renvoie False si personne ne l'a faite à temps (ou si elle revient aussitôt)."""
    config = site.config
    if config.headless:
        log.warning("Paquets : PACKS_VERIFY_WAIT demande HEADLESS=false (écran Xvfb/noVNC) "
                    "pour qu'une personne puisse faire la vérification.")
        return False
    wait = config.packs_verify_wait.total_seconds()
    notify(config.notify_url, f"WikiMasters : vérification humaine à faire pour les paquets "
                              f"(attente {wait / 60:.0f} min).")
    if not site.await_pack_verification(wait):
        return False
    try:
        opened = (site.open_packs_api if config.actions_via_api else site.open_packs)(dry_run)
    except HumanVerificationRequired:
        return False
    log.info("Paquets : %d ouvert(s) après vérification.", opened)
    return True


def _buy(site: WikiMasters, config: Config, dry_run: bool) -> None:
    try:
        run_bids(site, config, dry_run)
    except SessionExpired:
        raise
    except WantedCardsError as exc:
        log.error("Achats ignorés : %s", exc)
    except Exception as exc:  # les ventes passent quand même
        log.warning("Achats interrompus : %s", str(exc).splitlines()[0])


def _pause(seconds: float, should_stop) -> bool:
    """Attend `seconds` secondes ; False dès qu'une passe attend la session."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return True


class PriceReader:
    """Prix moyens carte par carte : cache state/price_cache.json, sinon l'API. Un refus
    passager (trop de requêtes, panne) ne fait laisser de côté que la carte concernée ;
    l'API n'est abandonnée qu'après PRICE_FAILURES_BEFORE_GIVING_UP refus de suite, ou
    dès un accès interdit."""

    OK, UNKNOWN, STOPPED = "ok", "unknown", "stopped"

    def __init__(self, site: WikiMasters, config: Config):
        self.site, self.config = site, config
        self.cache = load_price_cache(config)
        self.api_ok = True
        self.fetched = 0
        self.failures = 0  # refus passagers de suite

    def read(self, group, should_stop=lambda: False) -> tuple[str, float | None]:
        """(OK, prix ou None si aucune vente connue), (UNKNOWN, None) si le prix n'a pas
        pu être lu, ou (STOPPED, None) si une passe attend la session."""
        key = f"{group.card_id}:{group.rarity}"
        if key in self.cache:
            return self.OK, self.cache[key]["average"]
        if not self.api_ok:
            return self.UNKNOWN, None
        if should_stop():
            return self.STOPPED, None
        try:
            average = self.site.fetch_average(group.card_id, group.rarity)
        except PriceApiUnavailable as exc:  # pas mis en cache : redemandé la prochaine fois
            self.failures += 1
            if exc.transient and self.failures < PRICE_FAILURES_BEFORE_GIVING_UP:
                log.info("Prix de « %s » non lu (%s) : carte laissée de côté, pause de %d s.",
                         group.title, exc, PRICE_RETRY_PAUSE)
                if not _pause(PRICE_RETRY_PAUSE, should_stop):
                    return self.STOPPED, None
                return self.UNKNOWN, None
            log.warning("API des prix indisponible (%s) : prix lus dans la fenêtre d'enchère, "
                        "sans classement pour les cartes concernées.", exc)
            self.api_ok = False
            return self.UNKNOWN, None
        self.failures = 0
        self.cache[key] = {"average": average, "at": now_utc().isoformat()}
        self.fetched += 1
        if self.fetched % PRICE_SAVE_EVERY == 0:
            self.save()  # une passe interrompue ne perd pas les prix déjà lus
        time.sleep(0.3)
        return self.OK, average

    def save(self) -> None:
        save_price_cache(self.config, self.cache)


def fetch_averages(site: WikiMasters, config: Config, groups, should_stop=lambda: False):
    """Prix moyens (cache state/price_cache.json, sinon l'API). Renvoie (prix par carte,
    cartes dont l'API a refusé le prix, True si tout a été lu sans interruption)."""
    reader = PriceReader(site, config)
    averages: dict[str, float | None] = {}
    unknown: set[str] = set()
    try:
        for group in groups:
            status, average = reader.read(group, should_stop)
            if status == reader.STOPPED:
                return averages, unknown, False
            if status == reader.UNKNOWN:
                unknown.add(group.card_id)
            else:
                averages[group.card_id] = average
    finally:
        reader.save()
    return averages, unknown, True


def _sell(site: WikiMasters, config: Config, safe_names: set[str], dry_run: bool) -> RunResult:
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
    my_id = my_user_id(market.mine, WantedState(config.wanted_state_file).my_id)
    unsold.update(market.history, my_id, now_utc())
    unsold.save()
    paused = {g.card_id: unsold.paused(g.card_id, g.title, now_utc()) for g in sellable}
    paused = {k: v for k, v in paused.items() if v}
    if paused:
        examples = [f"{g.title} (jusqu'au {paused[g.card_id].astimezone().strftime('%d/%m')})"
                    for g in sellable if g.card_id in paused]
        log.info("%d carte(s) invendue(s) mise(s) de côté, ex. : %s", len(paused), ", ".join(examples[:10]))
        sellable = [g for g in sellable if g.card_id not in paused]
        groups = [g for g in groups if g.card_id not in paused]
    log.info("%d carte(s) distincte(s), dont %d avec des exemplaires à vendre.", len(groups), len(sellable))

    # Prix moyens via l'API pour classer les cartes de la plus chère à la moins chère.
    averages, unknown, _ = fetch_averages(site, config, sellable)

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


# ─────────────────────────── Défausse en arrière-plan ───────────────────────────


class DiscardWorker:
    """Thread de défausse : un seul à la fois, relancé au plus tôt DISCARD_RECHECK après
    la fin du précédent (noté dans state/discard_progress.json)."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def next_start(config: Config) -> datetime | None:
        """Heure à partir de laquelle la défausse peut être relancée (None : tout de suite)."""
        data = read_json(discard_progress_file(config), {})
        try:
            finished = datetime.fromisoformat(data["finished_at"])
        except (KeyError, TypeError, ValueError):
            return None
        delay = DISCARD_RECHECK_UNPRICED if data.get("unpriced") else DISCARD_RECHECK
        return finished + delay if now_utc() < finished + delay else None

    @classmethod
    def recently_finished(cls, config: Config) -> bool:
        return cls.next_start(config) is not None

    def start(self, config: Config, dry_run: bool, debug: bool) -> bool:
        with self._lock:
            if self.running():
                return False
            self._thread = threading.Thread(
                target=run_discard, args=(config, dry_run, debug), name="defausse", daemon=True)
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


DISCARDER = DiscardWorker()


def protected_names(config: Config) -> set[str]:
    """Cartes jamais défaussées : celles de safed_cards.json et de wanted_cards.json."""
    names = set(load_safe_cards(config.safe_cards_file))
    try:
        names |= {normalize_name(c.name) for c in load_wanted_cards(config.wanted_cards_file)}
    except WantedCardsError as exc:
        log.warning("Défausse : %s ; les cartes voulues ne sont pas protégées.", exc)
    return names


def check_discard(site: WikiMasters, config: Config, dry_run: bool, debug: bool) -> None:
    """À chaque réveil : lance la défausse en arrière-plan (sauf si elle tourne déjà ou
    vient de finir). Le thread lit lui-même la collection et vérifie DISCARD_TRIGGER :
    la passe ne garde pas la session pendant cette longue lecture."""
    if not config.discard_below:
        return
    if DISCARDER.running():
        log.info("Défausse déjà en cours en arrière-plan.")
        return
    next_start = DISCARDER.next_start(config)
    if next_start:
        log.info("Défausse terminée récemment : prochaine au plus tôt à %s.",
                 next_start.astimezone().strftime("%H:%M"))
        return
    log.info("Défausse lancée en arrière-plan (cartes à moins de %d au-delà de %d exemplaires).",
             config.discard_below, config.discard_trigger)
    DISCARDER.start(config, dry_run, debug)


def discard_progress_file(config: Config):
    return config.state_dir / "discard_progress.json"


@dataclass
class DiscardProgress:
    """Avancement d'une défausse, gardé dans state/discard_progress.json : la collection
    n'est lue qu'une fois, puis les cartes sont examinées une à une et reprises là où
    l'examen s'était arrêté, même après un redémarrage."""
    started_at: str = dataclasses.field(default_factory=lambda: now_utc().isoformat())
    scan: CollectionScan = dataclasses.field(default_factory=CollectionScan)
    cards: list[DiscardItem] | None = None  # cartes à examiner, les plus communes d'abord
    position: int = 0  # carte en cours d'examen
    copies_done: int = 0  # exemplaires déjà défaussés de cette carte
    done: int = 0  # exemplaires défaussés en tout
    unpriced: int = 0  # cartes dont le prix n'a pas pu être lu : relance plus tôt

    def save(self, config: Config, finished: bool = False) -> None:
        data = {"started_at": self.started_at, "position": self.position,
                "copies_done": self.copies_done, "done": self.done, "unpriced": self.unpriced}
        if finished:
            data["finished_at"] = now_utc().isoformat()
        elif self.cards is not None:  # la collection lue ne sert plus une fois les cartes listées
            data["cards"] = [dataclasses.asdict(i) for i in self.cards]
        else:
            scan = self.scan
            data["scan"] = {"rarity": scan.rarity, "entries": scan.entries, "seen": sorted(scan.seen),
                            "in_trade": sorted(scan.in_trade), "next_page": scan.next_page, "done": scan.done}
        write_json(discard_progress_file(config), data, indent=None)

    @classmethod
    def load(cls, config: Config) -> "DiscardProgress":
        """Reprend la défausse sauvegardée, ou en commence une nouvelle si elle est finie,
        trop ancienne ou illisible."""
        data = read_json(discard_progress_file(config), {})
        try:
            started = datetime.fromisoformat(data["started_at"])
            if data.get("finished_at") or now_utc() - started > DISCARD_PROGRESS_MAX_AGE:
                return cls()
            progress = cls(started_at=data["started_at"], position=data["position"],
                           copies_done=data["copies_done"], done=data["done"],
                           unpriced=data.get("unpriced", 0))
            if data.get("cards") is not None:
                progress.cards = [DiscardItem(**i) for i in data["cards"]]
            elif data.get("scan"):
                raw = data["scan"]
                progress.scan = CollectionScan(rarity=raw["rarity"], entries=raw["entries"], seen=set(raw["seen"]),
                                               in_trade=set(raw["in_trade"]), next_page=raw["next_page"],
                                               done=raw["done"])
            else:
                return cls()  # ancien format : on recommence (les prix restent en cache)
        except (KeyError, TypeError, ValueError):
            return cls()
        if progress.cards is not None:
            log.info("Défausse reprise : %d/%d carte(s) examinée(s), %d exemplaire(s) défaussé(s).",
                     progress.position, len(progress.cards), progress.done)
        else:
            log.info("Défausse reprise : %d entrée(s) de la collection déjà lues.", len(progress.scan.entries))
        return progress


def run_discard(config: Config, dry_run: bool, debug: bool) -> bool:
    """Corps du thread : défausse toutes les cartes à moins de DISCARD_BELOW, une à une.
    Cède la session à chaque passe qui attend, puis reprend où elle en était.
    Renvoie True si la défausse est allée au bout (False après une erreur)."""
    progress = DiscardProgress.load(config)
    try:
        try:
            while not _discard_round(config, dry_run, debug, progress):
                progress.save(config)
                log.info("Défausse en pause : une passe a besoin de la session ; reprise ensuite.")
        except BaseException:
            progress.save(config)  # reprise au prochain réveil, ou après un redémarrage
            raise
        progress.save(config, finished=True)
        return True
    except SessionExpired as exc:
        log.warning("Défausse interrompue : session expirée (%s).", exc)
    except Exception:
        log.exception("Défausse interrompue par une erreur ; nouvel essai au prochain réveil.")
    return False


def _discard_round(config: Config, dry_run: bool, debug: bool, progress: DiscardProgress) -> bool:
    """Une période avec la session. Renvoie True quand la défausse est finie, False si
    elle a cédé la place à une passe. Au fil de l'eau : le prix de chaque carte est lu,
    puis la carte est défaussée aussitôt si elle vaut moins de DISCARD_BELOW."""
    with open_site(config, debug, background=True) as site:
        if progress.cards is None and not _list_discard_cards(site, config, progress):
            return False
        if progress.cards is None:
            return True  # seuil non dépassé
        cards = progress.cards
        reader = PriceReader(site, config)
        try:
            while progress.position < len(cards):
                item = cards[progress.position]
                if progress.copies_done == 0:  # sinon : prix déjà lu, défausse en cours
                    if GATE.should_yield():
                        return False
                    status, average = reader.read(item, GATE.should_yield)
                    if status == reader.STOPPED:
                        return False
                    if status == reader.UNKNOWN:
                        progress.unpriced += 1
                        _next_discard_card(config, progress)
                        continue
                    item.average = average
                    if not should_discard(item.rarity, average, config.discard_below):
                        _next_discard_card(config, progress)
                        continue
                while progress.copies_done < item.copies:
                    if GATE.should_yield():
                        return False
                    status, detail = site.discard_card(item.title, dry_run)
                    price = f"prix moyen {item.average:g}" if item.average else "aucune vente connue"
                    if status is DiscardStatus.FAILED:
                        log.warning("Défausse : %s : %s (%s) ; carte suivante.", item.title, status.value, detail)
                        break
                    progress.copies_done += 1
                    progress.done += 1
                    progress.save(config)
                    log.info("Défausse : %s (%s, %s) : %s%s.", item.title, item.rarity or "?", price,
                             status.value, f" via {detail}" if detail else "")
                    time.sleep(random.uniform(*DISCARD_PAUSE))
                _next_discard_card(config, progress)
        finally:
            reader.save()
        log.info("Défausse terminée : %d exemplaire(s) %s, %d carte(s) examinée(s)%s.", progress.done,
                 "simulé(s)" if dry_run else "défaussé(s)", len(cards),
                 f", {progress.unpriced} sans prix lu (redemandées dans {DISCARD_RECHECK_UNPRICED.seconds // 60} min)"
                 if progress.unpriced else "")
        return True


def _next_discard_card(config: Config, progress: DiscardProgress) -> None:
    progress.position += 1
    progress.copies_done = 0
    if progress.position % DISCARD_LOG_EVERY == 0:
        log.info("Défausse : %d/%d carte(s) examinée(s), %d exemplaire(s) défaussé(s).",
                 progress.position, len(progress.cards), progress.done)
    if progress.position % DISCARD_SAVE_EVERY_CARDS == 0:
        progress.save(config)


def _list_discard_cards(site: WikiMasters, config: Config, progress: DiscardProgress) -> bool:
    """Lit la collection (reprise page par page) et liste les cartes à examiner.
    Renvoie False si une passe attend la session avant la fin ; laisse progress.cards
    à None si la collection ne dépasse pas DISCARD_TRIGGER."""
    def save_now_and_then(scan: CollectionScan) -> None:
        if scan.next_page % DISCARD_SAVE_EVERY_PAGES == 0:
            progress.save(config)

    if not site.scan_collection(progress.scan, GATE.should_yield, on_page=save_now_and_then):
        log.info("Défausse : %d entrée(s) de la collection lues avant la pause.", len(progress.scan.entries))
        return False
    count = total_copies(parse_collection(progress.scan.entries))
    if count <= config.discard_trigger:
        log.info("Collection : %d exemplaire(s), seuil de défausse %d non dépassé.", count, config.discard_trigger)
        return True
    groups = parse_collection(progress.scan.entries, excluded_copy_ids=progress.scan.in_trade)
    market = site.fetch_market_state()
    progress.cards = discard_candidates(groups, protected_names(config), market.selling)
    progress.save(config)
    log.info("Collection : %d exemplaire(s) > %d. Défausse au fil de l'eau de %d carte(s), les plus "
             "communes d'abord, à moins de %d.", count, config.discard_trigger, len(progress.cards),
             config.discard_below)
    return True


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
    log.info("Démarrage%s (actions par %s) : raretés %s, paquets %s, enchères « %s », passe toutes les %s, achats %s, "
             "défausse %s.",
             " (simulation)" if args.dry_run else "", "l'API" if config.actions_via_api else "la page",
             ",".join(config.sell_rarities) or "toutes",
             "-".join(map(str, config.packs_hours)) + " h" if config.packs_hours else "désactivés",
             config.auction_duration_label, config.pass_interval, config.wanted_cards_file,
             f"des cartes à moins de {config.discard_below} au-delà de {config.discard_trigger} exemplaires"
             if config.discard_below else "désactivée")

    try:
        if args.loop:
            loop(config, args.dry_run, args.debug)
        else:
            run_once(config, dry_run=args.dry_run, debug=args.debug)
            DISCARDER.join()  # une passe unique attend la fin de la défausse lancée
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
