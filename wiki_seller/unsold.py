"""Cartes qui ne se vendent pas : après UNSOLD_MAX_TRIES enchères terminées sans
acheteur, la carte est mise de côté UNSOLD_COOLDOWN_DAYS jours, puis retentée.

Les échecs sont lus dans l'historique de vos ventes (/api/marketplace?mine=1, clé
« history ») et notés dans state/unsold.json."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .selection import normalize_name

log = logging.getLogger(__name__)

SEEN_LIMIT = 500  # enchères déjà comptées, gardées pour ne pas les compter deux fois


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}
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
            if entry["fails"] >= self.max_tries:
                until = now + self.cooldown
                entry.update(fails=0, until=until.isoformat())
                log.info("Invendue %d fois : « %s » mise de côté jusqu'au %s.", self.max_tries,
                         title or card_id, until.astimezone().strftime("%d/%m %H:%M"))
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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
