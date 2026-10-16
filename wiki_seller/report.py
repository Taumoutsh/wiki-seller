"""Bilan envoyé par le bot Telegram : argent gagné par les ventes (détaillées) et les
défausses depuis le bilan automatique précédent, solde du compte, et enchères en cours
sur les cartes à acheter. Automatique toutes les REPORT_HOURS, ou à la demande
(/update), sans changer ce rythme.

Les ventes sont lues dans l'historique du site (final_price, settled_at) ; les
défausses sont comptées au fil de l'eau dans state/report.json (1 wikibidou chacune)."""

import logging
import threading
from datetime import datetime

from .buying import auction_title
from .lists import ListError, buy_cards
from .selection import normalize_name
from .state import now_utc, parse_time, read_json, write_json

log = logging.getLogger(__name__)

DISCARD_REWARD = 1  # wikibidou par exemplaire défaussé (fenêtre « Défausser cette carte ? »)
MAX_SALES_LISTED = 30  # ventes détaillées dans un bilan (Telegram limite la taille des messages)
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


def sales_between(history: list, my_id: str | None, start: datetime, end: datetime) -> list[tuple[str, int]]:
    """(carte, prix final) de vos ventes conclues entre start et end, dans l'ordre."""
    sales = []
    for auction in sorted((a for a in history or [] if isinstance(a, dict)),
                          key=lambda a: a.get("settled_at") or a.get("end_at") or ""):
        if my_id and auction.get("seller_id") != my_id:
            continue
        price = auction.get("final_price")
        settled = parse_time(auction.get("settled_at") or auction.get("end_at"))
        if isinstance(price, (int, float)) and price > 0 and settled and start <= settled < end:
            sales.append((auction_title(auction) or "carte inconnue", int(price)))
    return sales


def bid_lines(config, mine: dict, my_id: str | None) -> list[str]:
    """Enchères en cours sur les cartes à acheter : mise, et prix actuel si vous êtes
    dépassé (surenchère prévue juste avant la fin si elle reste dans la limite)."""
    try:
        cards = {c.key: c for c in buy_cards(config)}
    except ListError:
        return []
    tracked = read_json(config.wanted_state_file, {}).get("cards", {})
    lines = []
    for auction in mine.get("bidding") or []:
        if not isinstance(auction, dict) or auction.get("status", "active") != "active":
            continue
        card = cards.get(normalize_name(auction_title(auction)))
        if card is None:
            continue
        entry = tracked.get(card.key) or {}
        current = auction.get("current_bid")
        end = parse_time(auction.get("end_at"))
        ends = f", fin {end.astimezone():%d/%m %H:%M}" if end else ""
        if my_id and auction.get("current_bidder_id") == my_id:
            lines.append(f"• {card.name} : vous menez à {money(current)} (max {money(card.max_price)}){ends}")
            continue
        mine_bid = entry.get("my_bid") or {}
        placed = (f"votre mise {money(mine_bid['amount'])}" if mine_bid.get("auction") == auction.get("id")
                  else "votre mise inconnue")
        if isinstance(current, int):
            step = entry.get("step")  # pas de surenchère, appris à la fin d'une enchère
            following = current + int(step or 1)
            plan = (", au-dessus de votre max : abandonnée" if following > card.max_price
                    else f", surenchère à {money(following)} prévue avant la fin" if step
                    else ", surenchère prévue avant la fin")
            outbid = f"renchérie à {money(current)}{plan}"
        else:
            outbid = "prix actuel illisible"
        lines.append(f"• {card.name} : dépassé, {placed}, {outbid} (max {money(card.max_price)}){ends}")
    return lines


def money(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else str(n)


def report_text(start: datetime, end: datetime, sales: list[tuple[str, int]], discarded: int,
                balance: int | None, bids: list[str] = ()) -> str:
    earned = sum(price for _, price in sales)
    gained = discarded * DISCARD_REWARD
    lines = [f"📊 Bilan WikiMasters ({start.astimezone():%H:%M} → {end.astimezone():%H:%M})",
             f"💰 Ventes : {len(sales)} carte(s), +{money(earned)}" if sales else "💰 Ventes : aucune"]
    lines += [f"• {title} : {money(price)}" for title, price in sales[:MAX_SALES_LISTED]]
    if len(sales) > MAX_SALES_LISTED:
        lines.append(f"… et {len(sales) - MAX_SALES_LISTED} autre(s)")
    lines.append(f"🗑️ Défausses : {discarded} exemplaire(s), +{money(gained)}" if discarded
                 else "🗑️ Défausses : aucune")
    if sales or discarded:
        lines.append(f"Total gagné : +{money(earned + gained)}")
    lines.append(f"👛 Solde actuel : {money(balance)} wikibidous" if balance is not None
                 else "👛 Solde actuel : illisible")
    if bids:
        lines.append("🛒 Enchères en cours (achats) :")
        lines += bids
    return "\n".join(lines)


def send_report(site, config, bot, history: list, my_id: str | None, on_demand: bool = False,
                mine: dict | None = None) -> bool:
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
        text = report_text(since, now, sales_between(history, my_id, since, now), discarded, site.fetch_balance(),
                           bid_lines(config, mine or {}, my_id))
        if not on_demand:
            write_json(_file(config), {"since": now.isoformat(), "discarded": 0})
    bot.send(text)
    log.info("Bilan %s sur Telegram.", "demandé envoyé" if on_demand else "envoyé")
    return True
