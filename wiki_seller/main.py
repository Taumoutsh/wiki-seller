"""Ligne de commande : une passe, ou la boucle (--loop) utilisée par le conteneur."""

import argparse
import dataclasses
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import OBSOLETE_SETTINGS, Config, ConfigError, load_config, obsolete_settings
from .discard import DISCARDER
from .scheduler import loop, run_once
from .selection import SafeCardsError
from .site import SessionExpired

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
    log.info("Démarrage%s : raretés %s, enchères « %s », passe toutes les %s, achats %s, défausse %s.",
             " (simulation)" if dry_run else "", ",".join(config.sell_rarities) or "toutes",
             config.auction_duration_label, config.pass_interval, config.wanted_cards_file,
             f"des cartes à moins de {config.discard_below} au-delà de {config.discard_trigger} exemplaires"
             if config.discard_below else "désactivée")
    for name in obsolete_settings():
        log.warning("%s n'a plus d'effet (%s) : vous pouvez le retirer du .env.", name, OBSOLETE_SETTINGS[name])
    if config.telegram_token and not config.telegram_chat_id:
        log.warning("TELEGRAM_BOT_TOKEN sans TELEGRAM_CHAT_ID : bot Telegram désactivé (voir le README).")
    elif config.telegram_token:
        log.info("Bot Telegram actif : il demandera une nouvelle session si elle expire.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki_seller", description="Ventes, achats et défausse automatiques sur WikiMasters.")
    parser.add_argument("--dry-run", action="store_true", help="tout simuler : ni mise en vente, ni mise, ni défausse")
    parser.add_argument("--loop", action="store_true", help="tourner en continu (relance RUN_INTERVAL après la dernière vente)")
    parser.add_argument("--headed", action="store_true", help="afficher le navigateur (pour cocher la vérification Cloudflare)")
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
    log_startup(config, args.dry_run)

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
