"""Ligne de commande : une passe, ou la boucle (--loop) utilisée par le conteneur."""

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import accounts
from .config import OBSOLETE_SETTINGS, Config, ConfigError, load_config, obsolete_settings
from .discard import DISCARDER
from .lists import ListError, buy_cards, legacy_files, safe_titles
from .scheduler import loop, request, run_once
from .site import SessionExpired
from .telegram import CommandListener, notify, telegram_bot

log = logging.getLogger("wiki_seller")


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
            admin = accounts.admin_id(base)
            loop(lambda: accounts.account_config(base, admin), args.dry_run, args.debug)
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
