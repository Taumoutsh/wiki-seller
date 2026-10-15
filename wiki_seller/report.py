"""Bilan périodique envoyé par le bot Telegram (REPORT_HOURS, 2 h par défaut) : argent
gagné par les ventes et par les défausses depuis le bilan précédent, et solde actuel.

Les ventes sont lues dans l'historique du site (final_price, settled_at) ; les
défausses sont comptées au fil de l'eau dans state/report.json (1 wikibidou chacune)."""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("wiki_seller")

DISCARD_REWARD = 1  # wikibidou par exemplaire défaussé (fenêtre « Défausser cette carte ? »)
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file(config) -> Path:
    return config.state_dir / "report.json"


def _read(config) -> dict:
    try:
        return json.loads(_file(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(config, data: dict) -> None:
    path = _file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def record_discard(config) -> None:
    """Un exemplaire défaussé (réellement) depuis le dernier bilan."""
    with _LOCK:
        data = _read(config)
        data["discarded"] = data.get("discarded", 0) + 1
        data.setdefault("since", _now().isoformat())
        _write(config, data)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def sales_between(history: list, my_id: str | None, start: datetime, end: datetime) -> tuple[int, int]:
    """(nombre, total des prix finaux) de vos ventes conclues entre start et end."""
    count = total = 0
    for auction in history or []:
        if not isinstance(auction, dict) or (my_id and auction.get("seller_id") != my_id):
            continue
        price = auction.get("final_price")
        settled = _parse_time(auction.get("settled_at") or auction.get("end_at"))
        if not isinstance(price, (int, float)) or price <= 0 or not settled or not start <= settled < end:
            continue
        count += 1
        total += int(price)
    return count, total


def report_text(start: datetime, end: datetime, sales: tuple[int, int], discarded: int,
                balance: int | None) -> str:
    def money(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    span = f"{start.astimezone().strftime('%H:%M')} → {end.astimezone().strftime('%H:%M')}"
    sold, earned = sales
    lines = [f"📊 Bilan WikiMasters ({span})",
             f"💰 Ventes : {sold} carte(s), +{money(earned)}" if sold else "💰 Ventes : aucune",
             f"🗑️ Défausses : {discarded} exemplaire(s), +{money(discarded * DISCARD_REWARD)}"
             if discarded else "🗑️ Défausses : aucune"]
    if sold or discarded:
        lines.append(f"Total gagné : +{money(earned + discarded * DISCARD_REWARD)}")
    lines.append(f"👛 Solde actuel : {money(balance)} wikibidous" if balance is not None
                 else "👛 Solde actuel : illisible")
    return "\n".join(lines)


def maybe_send_report(site, config, bot, history: list, my_id: str | None) -> bool:
    """Envoie le bilan si REPORT_HOURS sont passées depuis le précédent. Renvoie True
    s'il a été envoyé."""
    if bot is None or not config.report_every:
        return False
    with _LOCK:
        data = _read(config)
        now = _now()
        since = _parse_time(data.get("since"))
        if since is None:  # premier lancement : le premier bilan couvrira les heures à venir
            data["since"] = now.isoformat()
            _write(config, data)
            return False
        if now - since < config.report_every:
            return False
        discarded = data.get("discarded", 0)
        text = report_text(since, now, sales_between(history, my_id, since, now), discarded,
                           site.fetch_balance())
        bot.send(text)
        _write(config, {"since": now.isoformat(), "discarded": 0})
    log.info("Bilan envoyé sur Telegram.")
    return True


def send_report_now(site, config, bot, history: list, my_id: str | None) -> None:
    """Bilan demandé par /update : depuis le dernier bilan automatique, sans remettre
    le compteur à zéro (le rythme de REPORT_HOURS ne change pas)."""
    with _LOCK:
        data = _read(config)
        now = _now()
        since = _parse_time(data.get("since"))
        if since is None:
            data["since"] = now.isoformat()
            _write(config, data)
            since = now
        text = report_text(since, now, sales_between(history, my_id, since, now), data.get("discarded", 0),
                           site.fetch_balance())
    bot.send(text)
    log.info("Bilan demandé envoyé sur Telegram.")
