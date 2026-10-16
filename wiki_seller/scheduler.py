"""Passes et boucle : une passe (achats, ventes, défausse en arrière-plan, bilan), puis
la suivante RUN_INTERVAL après la dernière mise en vente, avec des réveils juste avant
la fin des enchères suivies pour surenchérir, et le bilan à la demande (/update)."""

import logging
import threading
import time
from datetime import datetime, timedelta

from .buying import WantedState, my_user_id, run_bids
from .config import Config, ConfigError
from .discard import check_discard
from .lists import ListError, safe_names
from .report import history_text, send_report
from .selling import RunResult, sell
from .session import open_site
from .site import SessionExpired, WikiMasters
from .state import now_utc, read_json
from .telegram import CommandListener, telegram_bot

log = logging.getLogger(__name__)

RESTART_MARGIN = timedelta(minutes=1)
RETRY_AFTER_ERROR = timedelta(minutes=10)
PASS_DURATION_GUARD = timedelta(minutes=10)  # durée maximale d'une passe, pour ne pas manquer un réveil
REPORT_GUARD_SECONDS = 90  # pas de bilan demandé juste avant un réveil

# Demandes du bot Telegram (/update : "report", /ventes : "history") : réveillent la boucle.
REPORT_REQUEST = threading.Event()
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()


def request(kind: str) -> None:
    with _PENDING_LOCK:
        _PENDING.add(kind)
    REPORT_REQUEST.set()


def run_once(config: Config, dry_run: bool = False, debug: bool = False) -> RunResult:
    protected = safe_names(config)  # liste absente ou illisible : rien n'est vendu
    log.info("%d carte(s) protégée(s).", len(protected))
    with open_site(config, debug) as site:
        site.refresh_market()
        _buy(site, config, dry_run)
        result = sell(site, config, safe_names(config), dry_run)  # /safe pendant les achats
        check_discard(config, dry_run, debug)
        _report(site, config, dry_run)
        return result


def run_snipe(config: Config, dry_run: bool = False, debug: bool = False) -> None:
    """Réveil juste avant la fin d'une enchère suivie : surenchérir si besoin."""
    with open_site(config, debug) as site:
        run_bids(site, config, dry_run, snipe_only=True)
        check_discard(config, dry_run, debug)
        _report(site, config, dry_run)


def _buy(site: WikiMasters, config: Config, dry_run: bool) -> None:
    try:
        run_bids(site, config, dry_run)
    except SessionExpired:
        raise
    except ListError as exc:
        log.error("Achats ignorés : %s", exc)
    except Exception as exc:  # les ventes passent quand même
        log.warning("Achats interrompus : %s", str(exc).splitlines()[0])


def _send_report(site: WikiMasters, config: Config, bot, on_demand: bool) -> None:
    market = site.fetch_market_state()
    my_id = my_user_id(market.mine, WantedState(config.wanted_state_file).my_id)
    send_report(site, config, bot, market.history, my_id, on_demand, market.mine)


def _report(site: WikiMasters, config: Config, dry_run: bool) -> None:
    """Bilan automatique toutes les REPORT_HOURS (au premier réveil après l'échéance)."""
    bot = telegram_bot(config)
    if dry_run or bot is None or not config.report_every:
        return
    try:
        _send_report(site, config, bot, on_demand=False)
    except SessionExpired:
        raise
    except Exception as exc:
        log.warning("Bilan Telegram non envoyé : %s", str(exc).splitlines()[0])


def report_now(config: Config, debug: bool, kinds: set[str] = frozenset({"report"})) -> None:
    """Bilan (/update) ou historique des ventes (/ventes) demandé : ouvre la session (une
    passe en cours passe avant)."""
    bot = telegram_bot(config)
    if bot is None:
        return
    try:
        with open_site(config, debug) as site:
            if "report" in kinds:
                _send_report(site, config, bot, on_demand=True)
            if "history" in kinds:
                market = site.fetch_market_state()
                my_id = my_user_id(market.mine, WantedState(config.wanted_state_file).my_id)
                bot.send(history_text(market.history, my_id), html=True)
    except Exception as exc:
        log.warning("Demande Telegram non traitée : %s", str(exc).splitlines()[0])
        bot.send(f"❌ Impossible pour l'instant : {str(exc).splitlines()[0][:200]}")


def sleep_until(wake: datetime, config: Config, debug: bool) -> None:
    """Attend le prochain réveil ; un /update reçu entre-temps envoie le bilan aussitôt,
    sauf juste avant le réveil (une surenchère ne doit pas attendre) : il part après."""
    while True:
        remaining = (wake - now_utc()).total_seconds()
        if remaining <= 0:
            return
        if remaining <= REPORT_GUARD_SECONDS:
            time.sleep(remaining)
            return
        if REPORT_REQUEST.wait(remaining - REPORT_GUARD_SECONDS):
            REPORT_REQUEST.clear()
            with _PENDING_LOCK:
                kinds = set(_PENDING) or {"report"}
                _PENDING.clear()
            report_now(config, debug, kinds)


def next_run_at(result: RunResult, config: Config, now: datetime) -> datetime:
    """RUN_INTERVAL (par défaut la durée des enchères) après la dernière mise en vente ;
    si les places sont déjà prises, dès la fin de la première enchère (au plus
    RUN_INTERVAL) ; sinon dans RUN_INTERVAL."""
    interval = config.pass_interval
    if result.last_listing_at:
        target = result.last_listing_at + interval + RESTART_MARGIN
    elif result.slots_full and result.earliest_auction_end:
        target = min(result.earliest_auction_end + RESTART_MARGIN, now + interval)
    elif result.slots_full:
        stored = read_json(config.run_state_file, {}).get("last_listing_at")
        target = datetime.fromisoformat(stored) + interval + RESTART_MARGIN if stored else now + interval
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
    bot = telegram_bot(config)
    if bot is not None:
        CommandListener(bot, request, config).start()
    list_error = None
    while True:
        try:
            if snipe:
                run_snipe(config, dry_run=dry_run, debug=debug)
            else:
                next_pass = next_run_at(run_once(config, dry_run=dry_run, debug=debug), config, now_utc())
            list_error = None
        except ConfigError:
            raise
        except ListError as exc:  # le bot reste à l'écoute : /safe corrige la liste
            log.error("%s Nouvel essai dans 10 minutes.", exc)
            if bot is not None and str(exc) != list_error:
                bot.send(f"❌ Passe impossible : {exc}")
            list_error = str(exc)
            next_pass = now_utc() + RETRY_AFTER_ERROR
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
        sleep_until(wake, config, debug)
