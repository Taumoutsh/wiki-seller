"""Bilan envoyé par le bot Telegram : argent gagné par les ventes et les défausses depuis
le bilan automatique précédent, et solde du compte. Automatique toutes les REPORT_HOURS,
ou à la demande (/update), sans changer ce rythme.

Les ventes sont lues dans l'historique du site (final_price, settled_at) ; les
défausses sont comptées au fil de l'eau dans state/report.json (1 wikibidou chacune)."""

import logging
import threading
from datetime import datetime

from .state import now_utc, parse_time, read_json, write_json

log = logging.getLogger(__name__)

DISCARD_REWARD = 1  # wikibidou par exemplaire défaussé (fenêtre « Défausser cette carte ? »)
_LOCK = threading.Lock()  # la défausse (thread) et les passes écrivent le même fichier


def _file(config):
    return config.state_dir / "report.json"


def record_discard(config) -> None:
    """Un exemplaire réellement défaussé depuis le dernier bilan."""
    with _LOCK:
        data = read_json(_file(config), {})
        data["discarded"] = data.get("discarded", 0) + 1
        data.setdefault("since", now_utc().isoformat())
        write_json(_file(config), data)


def sales_between(history: list, my_id: str | None, start: datetime, end: datetime) -> tuple[int, int]:
    """(nombre, total des prix finaux) de vos ventes conclues entre start et end."""
    count = total = 0
    for auction in history or []:
        if not isinstance(auction, dict) or (my_id and auction.get("seller_id") != my_id):
            continue
        price = auction.get("final_price")
        settled = parse_time(auction.get("settled_at") or auction.get("end_at"))
        if isinstance(price, (int, float)) and price > 0 and settled and start <= settled < end:
            count += 1
            total += int(price)
    return count, total


def report_text(start: datetime, end: datetime, sales: tuple[int, int], discarded: int,
                balance: int | None) -> str:
    def money(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    sold, earned = sales
    gained = discarded * DISCARD_REWARD
    lines = [f"📊 Bilan WikiMasters ({start.astimezone():%H:%M} → {end.astimezone():%H:%M})",
             f"💰 Ventes : {sold} carte(s), +{money(earned)}" if sold else "💰 Ventes : aucune",
             f"🗑️ Défausses : {discarded} exemplaire(s), +{money(gained)}" if discarded else "🗑️ Défausses : aucune"]
    if sold or discarded:
        lines.append(f"Total gagné : +{money(earned + gained)}")
    lines.append(f"👛 Solde actuel : {money(balance)} wikibidous" if balance is not None
                 else "👛 Solde actuel : illisible")
    return "\n".join(lines)


def send_report(site, config, bot, history: list, my_id: str | None, on_demand: bool = False) -> bool:
    """Envoie le bilan. Automatique (on_demand=False) : seulement si REPORT_HOURS sont
    passées depuis le précédent, puis remise à zéro du compteur. À la demande : depuis le
    dernier bilan automatique, sans remise à zéro. Renvoie True s'il a été envoyé."""
    if bot is None or (not on_demand and not config.report_every):
        return False
    with _LOCK:
        data = read_json(_file(config), {})
        now = now_utc()
        since = parse_time(data.get("since"))
        if since is None:  # premier lancement : point de départ des bilans
            write_json(_file(config), {**data, "since": now.isoformat()})
            if not on_demand:
                return False
            since = now
        elif not on_demand and now - since < config.report_every:
            return False
        discarded = data.get("discarded", 0)
        text = report_text(since, now, sales_between(history, my_id, since, now), discarded, site.fetch_balance())
        if not on_demand:
            write_json(_file(config), {"since": now.isoformat(), "discarded": 0})
    bot.send(text)
    log.info("Bilan %s sur Telegram.", "demandé envoyé" if on_demand else "envoyé")
    return True
