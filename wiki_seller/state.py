"""Fichiers d'état (dossier state/) et horloge : lecture tolérante, écriture atomique."""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SITE_TIMEZONE = "Europe/Paris"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def paris_now() -> datetime:
    """Heure du site (Paris) : plages de nuit, durée des ventes."""
    try:
        return datetime.now(ZoneInfo(SITE_TIMEZONE))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def parse_time(value) -> datetime | None:
    """Heure ISO du site (« …Z » accepté) ; None si absente ou illisible."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, indent: int | None = 2) -> None:
    """Écriture atomique : un fichier à moitié écrit n'est jamais relu."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
