"""Bilan envoyé par le bot Telegram : argent gagné par les ventes (détaillées) et les
défausses depuis le bilan automatique précédent, solde du compte, et enchères en cours
sur les cartes à acheter. Automatique toutes les REPORT_HOURS, ou à la demande
(/update), sans changer ce rythme.

Les ventes sont lues dans l'historique du site (final_price, settled_at) ; les
défausses sont comptées au fil de l'eau dans state/report.json (1 wikibidou chacune)."""

import logging
import threading
from datetime import datetime
from html import escape

from .buying import auction_title
from .lists import ListError, buy_cards
from .selection import normalize_name
from .state import now_utc, parse_time, read_json, write_json
from .unsold import auction_outcome

log = logging.getLogger(__name__)

DISCARD_REWARD = 1  # wikibidou par exemplaire défaussé (fenêtre « Défausser cette carte ? »)
INDENT = "   "
MAX_SALES_LISTED = 30
MAX_HISTORY_LISTED = 40  # enchères détaillées par /ventes  # ventes détaillées dans un bilan (Telegram limite la taille des messages)
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


def bid_lines(config, mine: dict, my_id: str | None, now: datetime | None = None) -> list[str]:
    """Enchères en cours sur les cartes à acheter, une par bloc (HTML Telegram) : ✅ vous
    menez, ⚠️ dépassé avec surenchère prévue avant la fin, ❌ abandonnée (au-dessus du max)."""
    try:
        cards = {c.key: c for c in buy_cards(config)}
    except ListError:
        return []
    tracked = read_json(config.wanted_state_file, {}).get("cards", {})
    now = now or now_utc()
    blocks = []
    for auction in mine.get("bidding") or []:
        if not isinstance(auction, dict) or auction.get("status", "active") != "active":
            continue
        card = cards.get(normalize_name(auction_title(auction)))
        if card is None:
            continue
        entry = tracked.get(card.key) or {}
        current = auction.get("current_bid")
        end = parse_time(auction.get("end_at"))
        details = [f"max {money(card.max_price)}"] + ([f"fin {when(end, now)}"] if end else [])
        name = escape(card.name)
        if my_id and auction.get("current_bidder_id") == my_id:
            blocks.append(f"✅ {name}\n{INDENT}" + " · ".join([f"en tête à {money(current)}", *details]))
            continue
        mine_bid = entry.get("my_bid") or {}
        placed = mine_bid.get("amount") if mine_bid.get("auction") == auction.get("id") else None
        if not isinstance(current, int):
            blocks.append(f"⚠️ {name}\n{INDENT}" + " · ".join(["dépassé (prix actuel illisible)", *details]))
            continue
        outbid = f"dépassé : {money(placed)} → {money(current)}" if placed else f"dépassé à {money(current)}"
        step = entry.get("step")  # pas de surenchère, appris à la fin d'une enchère
        following = current + int(step or 1)
        if following > card.max_price:
            icon, plan = "❌", "abandonnée (au-dessus du max)"
        else:
            icon, plan = "⚠️", (f"surenchère à {money(following)} juste avant la fin" if step
                                else "surenchère juste avant la fin")
        blocks.append(f"{icon} {name}\n{INDENT}" + " · ".join([outbid, *details]) + f"\n{INDENT}↳ {plan}")
    return blocks


def when(moment: datetime, now: datetime) -> str:
    """Heure seule si c'est aujourd'hui (heure locale), sinon date et heure."""
    local = moment.astimezone()
    return f"{local:%H:%M}" if local.date() == now.astimezone().date() else f"{local:%d/%m %H:%M}"


def money(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else str(n)


def plural(n: int, word: str) -> str:
    return f"{money(n)} {word}{'s' if n > 1 else ''}"


def report_text(start: datetime, end: datetime, sales: list[tuple[str, int]], discarded: int,
                balance: int | None, bids: list[str] = ()) -> str:
    """Bilan au format HTML de Telegram : l'essentiel en haut, une section par sujet,
    les sections vides omises."""
    earned = sum(price for _, price in sales)
    gained = discarded * DISCARD_REWARD
    head = f"👛 {money(balance)} wikibidous" if balance is not None else "👛 Solde illisible"
    if earned + gained:
        head += f"  (+{money(earned + gained)} depuis le dernier bilan)"
    sections = [f"<b>📊 Bilan · {start.astimezone():%H:%M} → {end.astimezone():%H:%M}</b>\n{head}"
                + ("" if sales or discarded or bids else "\nRien de neuf")]
    if sales:
        rows = [f"• {escape(title)} · {money(price)}" for title, price in sales[:MAX_SALES_LISTED]]
        if len(sales) > MAX_SALES_LISTED:
            rows.append(f"… et {plural(len(sales) - MAX_SALES_LISTED, 'autre')}")
        sections.append(f"<b>💰 Ventes · {plural(len(sales), 'carte')} · +{money(earned)}</b>\n" + "\n".join(rows))
    if discarded:
        sections.append(f"<b>🗑️ Défausses · {plural(discarded, 'exemplaire')} · +{money(gained)}</b>")
    if bids:
        sections.append("<b>🛒 Achats en cours</b>\n" + "\n".join(bids))
    return "\n\n".join(sections)


def history_text(history: list, my_id: str | None, now: datetime | None = None) -> str:
    """/ventes : vos enchères terminées telles que le site les renvoie (les plus récentes
    d'abord), pour vérifier ce que le bilan compte."""
    now = now or now_utc()
    ended = sorted((a for a in history or [] if isinstance(a, dict)),
                   key=lambda a: a.get("settled_at") or a.get("end_at") or "", reverse=True)
    if not ended:
        return "<b>📜 Historique des ventes</b>\nLe site ne renvoie aucune enchère terminée."
    times = [t for a in ended if (t := parse_time(a.get("settled_at") or a.get("end_at")))]
    sold = [a for a in ended if auction_outcome(a) == "sold" and (not my_id or a.get("seller_id") == my_id)]
    earned = sum(int(a["final_price"]) for a in sold if isinstance(a.get("final_price"), (int, float)))
    lines = [f"<b>📜 Historique des ventes · {plural(len(sold), 'vendue')} · +{money(earned)}</b>",
             f"Le site renvoie {plural(len(ended), 'enchère')} terminée{'s' if len(ended) > 1 else ''}"
             + (f", du {when(min(times), now)} au {when(max(times), now)}." if times else ".")]
    for auction in ended[:MAX_HISTORY_LISTED]:
        title = escape(auction_title(auction) or "carte inconnue")
        moment = parse_time(auction.get("settled_at") or auction.get("end_at"))
        at = f" · {when(moment, now)}" if moment else ""
        outcome = auction_outcome(auction)
        if my_id and auction.get("seller_id") != my_id:
            lines.append(f"👤 {title} · vendeur différent{at}")
        elif outcome == "sold":
            price = auction.get("final_price") or auction.get("current_bid")
            lines.append(f"✅ {title} · {money(price) if price else 'prix ?'}{at}")
        elif outcome == "unsold":
            lines.append(f"❌ {title} · invendue{at}")
        else:
            lines.append(f"⏸️ {title} · {escape(str(auction.get('status') or 'état inconnu'))}{at}")
    if len(ended) > MAX_HISTORY_LISTED:
        lines.append(f"… et {plural(len(ended) - MAX_HISTORY_LISTED, 'autre')}")
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
        log.info("Bilan : l'historique du site contient %d enchère(s) terminée(s).", len(history or []))
        text = report_text(since, now, sales_between(history, my_id, since, now), discarded, site.fetch_balance(),
                           bid_lines(config, mine or {}, my_id))
        if not on_demand:
            write_json(_file(config), {"since": now.isoformat(), "discarded": 0})
    bot.send(text, html=True)
    log.info("Bilan %s sur Telegram.", "demandé envoyé" if on_demand else "envoyé")
    return True
