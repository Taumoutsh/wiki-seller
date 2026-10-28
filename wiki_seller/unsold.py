"""Cartes qui ne se vendent pas : après UNSOLD_MAX_TRIES enchères terminées sans
acheteur, la carte est mise de côté UNSOLD_COOLDOWN_DAYS jours, puis retentée.

Quand plus rien d'autre ne peut être vendu, les cartes mises de côté repartent avant la
fin de leur pause, les plus anciennement mises de côté d'abord ; une telle remise en
vente sans acheteur la remet aussitôt de côté (nouvelle date : fin de la file).

Les échecs sont lus dans l'historique de vos ventes (/api/marketplace?mine=1, clé
« history ») et notés dans state/unsold.json."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from .selection import normalize_name
from .state import parse_time, read_json, write_json

log = logging.getLogger(__name__)

SEEN_LIMIT = 500  # enchères déjà comptées, gardées pour ne pas les compter deux fois


def card_key(auction: dict) -> tuple[str | None, str]:
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    title = card.get("wikipedia_title") or card.get("title") or ""
    return card.get("id"), normalize_name(title)


def auction_outcome(auction: dict) -> str | None:
    """« sold », « unsold », ou None (en cours, annulée, illisible)."""
    status = str(auction.get("status") or "").lower()
    if not status or status == "active" or "cancel" in status:
        return None
    if auction.get("winner_id") or auction.get("final_price"):
        return "sold"
    return "unsold"


class UnsoldTracker:
    def __init__(self, path: Path, max_tries: int, cooldown: timedelta):
        self.path, self.max_tries, self.cooldown = path, max_tries, cooldown
        self.data = read_json(path, {})
        self.data.setdefault("cards", {})
        self.data.setdefault("seen", [])

    def _entry(self, card_id: str | None, title: str) -> dict:
        key = card_id or f"title:{title}"
        entry = self.data["cards"].setdefault(key, {"fails": 0})
        entry["title"] = title
        return entry

    def update(self, history: list, my_id: str | None, now: datetime) -> None:
        """Compte les ventes terminées de l'historique qui n'ont pas encore été vues."""
        if not my_id:
            return
        seen = set(self.data["seen"])
        # L'historique arrive du plus récent au plus ancien : on le rejoue dans l'ordre,
        # pour qu'une vente récente efface les échecs plus anciens.
        ordered = sorted((a for a in history or [] if isinstance(a, dict)),
                         key=lambda a: a.get("settled_at") or a.get("end_at") or "")
        for auction in ordered:
            if auction.get("seller_id") != my_id or auction.get("id") in seen:
                continue
            outcome = auction_outcome(auction)
            if outcome is None:
                continue
            seen.add(auction["id"])
            self.data["seen"].append(auction["id"])
            card_id, title = card_key(auction)
            entry = self._entry(card_id, title)
            if outcome == "sold":
                entry.update(fails=0)
                entry.pop("until", None)
                continue
            entry["fails"] = entry.get("fails", 0) + 1
            in_pause = (parse_time(entry.get("until")) or now) > now  # remise en vente avant la fin de sa pause
            if entry["fails"] >= self.max_tries or in_pause:
                until = now + self.cooldown
                entry.update(fails=0, until=until.isoformat(), since=now.isoformat())
                log.info("%s : « %s » mise de côté jusqu'au %s.",
                         "Toujours invendue" if in_pause else f"Invendue {self.max_tries} fois", title or card_id,
                         until.astimezone().strftime("%d/%m %H:%M"))
        self.data["seen"] = self.data["seen"][-SEEN_LIMIT:]

    def paused(self, card_id: str, title: str, now: datetime) -> datetime | None:
        """Date de retour si la carte est mise de côté, sinon None."""
        title = normalize_name(title)
        for key, entry in self.data["cards"].items():
            if key != card_id and entry.get("title") != title:
                continue
            until = parse_time(entry.get("until"))
            if until and until > now:
                return until
        return None

    def set_aside_since(self, card_id: str, title: str) -> datetime | None:
        """Date à laquelle la carte a été mise de côté (estimée d'après la fin de sa pause
        pour une mise de côté d'avant cette date)."""
        title = normalize_name(title)
        for key, entry in self.data["cards"].items():
            if key != card_id and entry.get("title") != title:
                continue
            since = parse_time(entry.get("since"))
            until = parse_time(entry.get("until"))
            return since or (until - self.cooldown if until else None)
        return None

    def save(self) -> None:
        write_json(self.path, self.data)
