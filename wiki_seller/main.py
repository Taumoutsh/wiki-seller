"""Ligne de commande : une passe, ou la boucle (--loop) utilisée par le conteneur."""

import argparse
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import accounts
from .config import OBSOLETE_SETTINGS, Config, ConfigError, load_config, obsolete_settings
from .discard import DISCARDER
from .lists import ListError, buy_cards, legacy_files, safe_titles
from .scheduler import loop, request, run_once
from .site import SessionExpired
from .telegram import CommandListener, notify, telegram_bot

log = logging.getLogger("wiki_seller")


SUPERVISE_EVERY = 30  # s : comptes ajoutés ou retirés, boucles arrêtées par une erreur


def setup_logging(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    # [compte] : chaque compte tourne dans son fil, nommé comme lui.
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(config.state_dir / "seller.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]


def log_startup(config: Config, dry_run: bool) -> None:
    log.info("Démarrage%s : raretés %s, enchères « %s », passe toutes les %s, défausse %s.",
             " (simulation)" if dry_run else "", ",".join(config.sell_rarities) or "toutes",
             config.auction_duration_label, config.pass_interval,
             f"des cartes à moins de {config.discard_below} au-delà de {config.discard_trigger} exemplaires"
             if config.discard_below else "désactivée")
    for name in obsolete_settings():
        log.warning("%s n'a plus d'effet (%s) : vous pouvez le retirer du .env.", name, OBSOLETE_SETTINGS[name])
    if config.telegram_token and not config.telegram_chat_id:
        log.warning("TELEGRAM_BOT_TOKEN sans TELEGRAM_CHAT_ID : bot Telegram désactivé (voir le README).")
    elif config.telegram_token:
        log.info("Bot Telegram actif : il demandera une nouvelle session si elle expire.")
    remind_legacy_files(config, dry_run)


def remind_legacy_files(config: Config, dry_run: bool) -> None:
    """Les listes sont dans state/ : les anciens safed_cards.json et wanted_cards.json y
    sont repris au premier démarrage, puis ne servent plus. Rappel de les supprimer."""
    try:
        safe_titles(config)  # reprise des anciens fichiers
        buy_cards(config)
    except ListError:
        pass  # signalé par la passe
    legacy = legacy_files(config)
    if not legacy:
        return
    names = " et ".join(p.name for p in legacy)
    text = (f"🧹 {names} : repris dans state/ et modifiés désormais par le bot (/list). Ces anciens fichiers "
            "ne servent plus : vous pouvez les supprimer.")
    log.warning("%s", text)
    if not dry_run:
        notify(config, text)


def run_accounts(base: Config, dry_run: bool, debug: bool) -> None:
    """Une boucle par compte, chacune dans son fil : démarrée pour un compte ajouté,
    arrêtée pour un compte retiré, relancée après une erreur imprévue."""
    threads: dict[str, threading.Thread] = {}
    while True:
        for account in accounts.accounts(base):
            thread = threads.get(account.chat_id)
            if thread is not None and thread.is_alive():
                continue
            if thread is not None:
                log.warning("Compte %s : boucle arrêtée, relancée.", account.name or account.chat_id)
            thread = threading.Thread(target=_account_loop, args=(base, account.chat_id, dry_run, debug),
                                      name=account.name or account.chat_id, daemon=True)
            threads[account.chat_id] = thread
            thread.start()
        time.sleep(SUPERVISE_EVERY)


def _account_loop(base: Config, chat_id: str, dry_run: bool, debug: bool) -> None:
    def current():
        return accounts.account_config(base, chat_id) if accounts.find(base, chat_id) else None

    try:
        loop(current, dry_run, debug)
    except Exception:
        log.exception("Boucle du compte arrêtée par une erreur.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki_seller", description="Ventes, achats et défausse automatiques sur WikiMasters.")
    parser.add_argument("--dry-run", action="store_true", help="tout simuler : ni mise en vente, ni mise, ni défausse")
    parser.add_argument("--loop", action="store_true", help="tourner en continu (relance RUN_INTERVAL après la dernière vente)")
    parser.add_argument("--debug", action="store_true", help="réponses du site enregistrées dans state/debug/ en cas d'échec")
    args = parser.parse_args(argv)

    try:
        base = load_config()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    setup_logging(base)
    accounts.migrate(base)
    config = accounts.account_config(base, accounts.admin_id(base))  # compte de l'administrateur
    log_startup(config, args.dry_run)

    try:
        if args.loop:
            bot = telegram_bot(base)
            if bot is not None:  # une seule écoute pour toutes les conversations
                CommandListener(bot, request, base).start()
            run_accounts(base, args.dry_run, args.debug)
        else:
            run_once(config, dry_run=args.dry_run or config.simulation, debug=args.debug)
            DISCARDER.join()  # une passe unique attend la fin de la défausse lancée
    except ListError as exc:
        log.error("%s", exc)
        return 2
    except SessionExpired as exc:
        log.error("Session expirée (%s) : relancez le script pour vous reconnecter.", exc)
        return 1
    except Exception:
        log.exception("Échec de la passe.")
        return 1
    return 0
