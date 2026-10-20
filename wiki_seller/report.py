"""Bilan envoyé par le bot Telegram : argent gagné par les ventes (détaillées) et les
défausses depuis le bilan automatique précédent, solde du compte, et enchères en cours
sur les cartes à acheter. Automatique toutes les REPORT_HOURS, ou à la demande
(/update), sans changer ce rythme.

Les ventes sont lues dans l'historique du site (final_price, settled_at) ; les
défausses sont comptées au fil de l'eau dans state/report.json (1 wikibidou chacune)."""

import logging
import threading
from datetime import datetime, timedelta
from html import escape

from .buying import auction_title, ended, line_of, rarity_matches
from .lists import AUTO, ListError, buy_cards
from .selection import normalize_name
from .site import min_bid
from .state import now_utc, parse_time, read_json, write_json
from .unsold import auction_outcome

log = logging.getLogger(__name__)

DISCARD_REWARD = 1  # wikibidou par exemplaire défaussé (fenêtre « Défausser cette carte ? »)
# Le bilan automatique attend qu'aucune de vos ventes ne se termine dans les REPORT_QUIET
# minutes, ni ne soit en cours de règlement (terminée depuis moins de SETTLING, mais pas
# encore dans l'historique : le solde la compte déjà, le bilan pas encore). Au-delà de
# REPORT_MAX_DELAY de retard, il part quand même.
REPORT_QUIET = timedelta(minutes=5)
SETTLING = timedelta(minutes=10)
REPORT_MAX_DELAY = timedelta(minutes=30)
# Les ventes sont comptées une fois chacune (identifiants gardés dans state/report.json) :
# une vente qui arrive en retard dans l'historique compte au bilan suivant.
LATE_SALES = timedelta(hours=6)
COUNTED_LIMIT = 500
MAX_SALES_LISTED = 30  # ventes détaillées dans un bilan (Telegram limite la taille des messages)
MAX_HISTORY_LISTED = 40  # enchères détaillées par /ventes
LOSSES_KEPT = 50  # enchères perdues gardées entre deux bilans
MAX_BIDS_LISTED = 15  # enchères en cours détaillées dans un bilan (les plus proches de la fin)
MAX_LOSSES_LISTED = 10  # enchères perdues détaillées dans un bilan
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


def record_loss(config, card: str, why: str, max_price) -> None:
    """Une enchère perdue depuis le dernier bilan (montrée au bilan, sans message à part)."""
    with _LOCK:
        data = read_json(_file(config), {})
        data["lost"] = [*data.get("lost", []), {"card": card, "why": why, "max": max_price,
                                                "at": now_utc().isoformat()}][-LOSSES_KEPT:]
        data.setdefault("since", now_utc().isoformat())
        write_json(_file(config), data)


def loss_lines(losses: list[dict]) -> list[str]:
    return [f"• {escape(str(l.get('card')))} · {escape(str(l.get('why')))} (max {money(l.get('max'))})"
            for l in losses if isinstance(l, dict)]


def _sales(history: list, my_id: str | None, start: datetime, end: datetime,
           counted: set[str] = frozenset(), start_without_id: datetime | None = None) -> list[dict]:
    """Vos ventes conclues entre start et end, pas encore comptées, dans l'ordre. Une vente
    sans identifiant (impossible à dédoublonner) compte à partir de start_without_id."""
    sales = []
    for auction in sorted((a for a in history or [] if isinstance(a, dict)),
                          key=lambda a: a.get("settled_at") or a.get("end_at") or ""):
        if (my_id and auction.get("seller_id") != my_id) or auction.get("id") in counted:
            continue
        price = auction.get("final_price")
        settled = parse_time(auction.get("settled_at") or auction.get("end_at"))
        first = start if auction.get("id") else max(start, start_without_id or start)
        if isinstance(price, (int, float)) and price > 0 and settled and first <= settled < end:
            sales.append(auction)
    return sales


def sales_between(history: list, my_id: str | None, start: datetime, end: datetime,
                  counted: set[str] = frozenset()) -> list[tuple[str, int]]:
    """(carte, prix final) de vos ventes conclues entre start et end, dans l'ordre."""
    return [(auction_title(a) or "carte inconnue", int(a["final_price"]))
            for a in _sales(history, my_id, start, end, counted)]


def bid_lines(config, mine: dict, my_id: str | None, now: datetime | None = None) -> list[str]:
    """Enchères en cours sur les cartes à acheter, une ligne chacune (HTML Telegram), les
    plus proches de la fin d'abord : ✅ vous menez, ⚠️ dépassé avec surenchère prévue
    avant la fin, ❌ abandonnée (au-dessus du max), 👀 suivie sans mise encore."""
    try:
        cards = {c.id: c for c in buy_cards(config)}
    except ListError:
        return []
    tracked = read_json(config.wanted_state_file, {}).get("cards", {})
    now = now or now_utc()
    rows: list[tuple[datetime | None, str]] = []
    for auction in mine.get("bidding") or []:
        if not isinstance(auction, dict) or auction.get("status", "active") != "active":
            continue
        card = next((c for c in cards.values() if c.key == normalize_name(auction_title(auction))
                     and rarity_matches(auction, c, unknown_ok=True)), None)
        if card is None or ended(auction, now):
            continue
        line = tracked.get(card.id) or {}
        entry = next((e for k, e in tracked.items() if line_of(k) == card.id and isinstance(e, dict)
                      and e.get("auction_id") == auction.get("id")), line)  # l'enchère suivie, parmi celles de la ligne
        current, end = auction.get("current_bid"), parse_time(auction.get("end_at"))
        max_price = line.get("auto_max") if card.max_price == AUTO else card.max_price
        if my_id and auction.get("current_bidder_id") == my_id:
            icon, state = "✅", f"en tête à {money(current)}"
        elif not isinstance(current, int):
            icon, state = "⚠️", "dépassé, prix illisible"
        else:
            mine_bid = entry.get("my_bid") or {}
            placed = mine_bid.get("amount") if mine_bid.get("auction") == auction.get("id") else None
            price = f"{money(placed)} → {money(current)}" if placed else f"dépassé à {money(current)}"
            following = min_bid(auction)
            if max_price and following > max_price:
                icon, state = "❌", f"{price}, abandonnée"
            else:
                icon, state = "⚠️", f"{price}, surenchère à {money(following)}"
        rows.append((end, _bid_row(icon, card, state, max_price, end, now)))
    mine_ids = {a.get("id") for a in mine.get("bidding") or [] if isinstance(a, dict)}
    for key, entry in tracked.items():  # gardées sans mise : la mise part juste avant la fin
        card, end = cards.get(line_of(key)), parse_time(entry.get("end_at"))
        if (card is None or entry.get("status") != "bidding" or not entry.get("auction_id")
                or entry["auction_id"] in mine_ids or not end or end < now):
            continue
        max_price = (tracked.get(card.id) or {}).get("auto_max") if card.max_price == AUTO else card.max_price
        state = f"mise à {money(entry['watch_price'])}" if entry.get("watch_price") else "suivie"
        rows.append((end, _bid_row("👀", card, state, max_price, end, now)))
    rows.sort(key=lambda r: (r[0] is None, r[0] or now))
    return [row for _, row in rows]


def _bid_row(icon: str, card, state: str, max_price, end: datetime | None, now: datetime) -> str:
    parts = [f"{icon} {escape(card.name)}", state, f"max {money(max_price)}" if max_price else "max auto"]
    return " · ".join(parts + ([f"fin {when(end, now)}"] if end else []))


def capped(lines: list[str], limit: int) -> list[str]:
    """Au plus `limit` lignes, puis « … et N autres » : un bilan reste lisible (et sous
    la taille maximale d'un message Telegram)."""
    if len(lines) <= limit:
        return list(lines)
    return [*lines[:limit], f"… et {plural(len(lines) - limit, 'autre')}"]


def when(moment: datetime, now: datetime) -> str:
    """Heure seule si c'est aujourd'hui (heure locale), sinon date et heure."""
    local = moment.astimezone()
    return f"{local:%H:%M}" if local.date() == now.astimezone().date() else f"{local:%d/%m %H:%M}"


def money(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else str(n)


def plural(n: int, word: str) -> str:
    return f"{money(n)} {word}{'s' if n > 1 else ''}"


def report_text(start: datetime, end: datetime, sales: list[tuple[str, int]], discarded: int,
                balance: int | None, bids: list[str] = (), lost: list[str] = ()) -> str:
    """Bilan au format HTML de Telegram : l'essentiel en haut, une section par sujet,
    les sections vides omises."""
    earned = sum(price for _, price in sales)
    gained = discarded * DISCARD_REWARD
    head = f"👛 {money(balance)} wikibidous" if balance is not None else "👛 Solde illisible"
    if earned + gained:
        head += f"  (+{money(earned + gained)} depuis le dernier bilan)"
    sections = [f"<b>📊 Bilan · {start.astimezone():%H:%M} → {end.astimezone():%H:%M}</b>\n{head}"
                + ("" if sales or discarded or bids or lost else "\nRien de neuf")]
    if sales:
        rows = [f"• {escape(title)} · {money(price)}" for title, price in sales[:MAX_SALES_LISTED]]
        if len(sales) > MAX_SALES_LISTED:
            rows.append(f"… et {plural(len(sales) - MAX_SALES_LISTED, 'autre')}")
        sections.append(f"<b>💰 Ventes · {plural(len(sales), 'carte')} · +{money(earned)}</b>\n" + "\n".join(rows))
    if discarded:
        sections.append(f"<b>🗑️ Défausses · {plural(discarded, 'exemplaire')} · +{money(gained)}</b>")
    if bids:
        counts = " ".join(f"{icon} {n}" for icon in ("👀", "✅", "⚠️", "❌")
                          if (n := sum(line.startswith(icon) for line in bids)))
        sections.append(f"<b>🛒 Achats en cours · {len(bids)}</b> ({counts})\n"
                        + "\n".join(capped(bids, MAX_BIDS_LISTED)))
    if lost:
        sections.append(f"<b>❌ Enchères perdues · {len(lost)}</b>\n" + "\n".join(capped(lost, MAX_LOSSES_LISTED)))
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


def report_due(config) -> bool:
    """Le bilan automatique est-il à envoyer (REPORT_HOURS passées, ou premier lancement) ?"""
    if not config.report_every:
        return False
    since = parse_time(read_json(_file(config), {}).get("since"))
    return since is None or now_utc() - since >= config.report_every


def report_overdue(config, now: datetime) -> bool:
    """Bilan automatique en retard de plus de REPORT_MAX_DELAY : il part quoi qu'il arrive."""
    since = parse_time(read_json(_file(config), {}).get("since"))
    return since is None or now - since - config.report_every > REPORT_MAX_DELAY


def settling(config, selling: list[dict], history: list, now: datetime) -> datetime | None:
    """Fin d'une vente terminée depuis moins de SETTLING qui a quitté vos ventes en cours
    (relevées en fin de passe précédente) sans être encore dans l'historique, sinon None."""
    previous = read_json(_file(config), {}).get("selling") or {}
    known = {a.get("id") for a in [*selling, *(history or [])] if isinstance(a, dict)}
    ends = [e for auction_id, end in previous.items()
            if auction_id not in known and (e := parse_time(end)) and now - SETTLING <= e <= now + REPORT_QUIET]
    return max(ends) if ends else None


def remember_selling(config, selling: list[dict]) -> None:
    """Vos ventes en cours, pour repérer au réveil suivant celles en cours de règlement."""
    with _LOCK:
        data = read_json(_file(config), {})
        data["selling"] = {a["id"]: a.get("end_at") for a in selling if isinstance(a, dict) and a.get("id")}
        write_json(_file(config), data)


def ending_soon(selling: list[dict], now: datetime) -> datetime | None:
    """Fin de la première de vos ventes qui se termine avant now + REPORT_QUIET (ou qui
    est terminée mais pas encore réglée), sinon None."""
    ends = [e for a in selling if isinstance(a, dict) and (e := parse_time(a.get("end_at")))]
    soon = [e for e in ends if e < now + REPORT_QUIET]
    return min(soon) if soon else None


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
        counted = data.get("counted")
        if counted is None:  # avant le suivi par identifiant : les ventes d'avant `since` sont comptées
            counted = [a.get("id") for a in _sales(history, my_id, since - LATE_SALES, since)]
        sold = _sales(history, my_id, since - LATE_SALES, now, set(counted), start_without_id=since)
        log.info("Bilan : l'historique du site contient %d enchère(s) terminée(s), %d vente(s) à compter.",
                 len(history or []), len(sold))
        sales = [(auction_title(a) or "carte inconnue", int(a["final_price"])) for a in sold]
        text = report_text(since, now, sales, discarded, site.fetch_balance(), bid_lines(config, mine or {}, my_id),
                           loss_lines(data.get("lost", [])))
        if not on_demand:
            counted = [*counted, *(a.get("id") for a in sold if a.get("id"))][-COUNTED_LIMIT:]
            write_json(_file(config), {**data, "since": now.isoformat(), "discarded": 0, "counted": counted,
                                       "lost": []})
    bot.send(text, html=True)
    log.info("Bilan %s sur Telegram.", "demandé envoyé" if on_demand else "envoyé")
    return True
