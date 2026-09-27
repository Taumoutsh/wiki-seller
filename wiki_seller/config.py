"""Chargement de la configuration depuis l'environnement (et le fichier .env)."""

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "oui", "on")


# Seules les cartes rares sont analysées : la collection grossit bien plus vite que le
# nombre de ventes possibles (5 à la fois), et chaque carte coûte une requête de prix.
DEFAULT_SELL_RARITIES = "L,SR"


# En dessous, une nouvelle passe démarrerait avant la fin de la précédente (~4 min).
MIN_RUN_INTERVAL = timedelta(minutes=5)
DEFAULT_RUN_INTERVAL = timedelta(hours=1)


def parse_duration(text: str) -> timedelta | None:
    """« 10 min », « 1 h », « 2 heures », « 1h30 » → durée ; None si illisible."""
    match = re.fullmatch(
        r"\s*(?:(\d+)\s*h(?:eures?)?)?\s*(?:(\d+)\s*(?:min(?:utes?)?)?)?\s*", text.lower())
    if not match or not any(match.groups()):
        return None
    hours, minutes = (int(g) if g else 0 for g in match.groups())
    if not match.group(1) and not re.search(r"min", text.lower()):
        return None  # « 10 » seul : unité ambiguë
    duration = timedelta(hours=hours, minutes=minutes)
    return duration if duration > timedelta(0) else None


def _rarities(value: str) -> tuple[str, ...]:
    return tuple(r.strip().upper() for r in value.replace(";", ",").split(",") if r.strip())


@dataclass(frozen=True)
class Config:
    mail: str
    password: str
    base_url: str
    safe_cards_file: Path
    state_dir: Path
    price_ratio: float
    auction_duration_label: str
    headless: bool
    chromium_executable: str | None
    # Raretés mises en vente (codes du site : L, UR, SR, R, PC, C) ; vide = toutes.
    sell_rarities: tuple[str, ...] = ()
    # Cookies d'une session connectée (« nom=valeur; nom2=valeur2 »), voir README.
    session_cookies: str = ""
    # Délai avant la passe suivante ; None = durée des enchères (AUCTION_DURATION_LABEL).
    run_interval: timedelta | None = None

    @property
    def pass_interval(self) -> timedelta:
        return self.run_interval or parse_duration(self.auction_duration_label) or DEFAULT_RUN_INTERVAL

    @property
    def storage_state_file(self) -> Path:
        return self.state_dir / "storage_state.json"

    @property
    def run_state_file(self) -> Path:
        return self.state_dir / "run_state.json"

    @property
    def debug_dir(self) -> Path:
        return self.state_dir / "debug"


def load_config() -> Config:
    # Les variables déjà présentes dans l'environnement (ex. Docker) priment sur le .env.
    load_dotenv(override=False)

    mail = os.getenv("MAIL", "").strip()
    password = os.getenv("PASSWORD", "")
    if not mail or not password:
        raise ConfigError("MAIL et PASSWORD doivent être définis (fichier .env ou environnement).")

    try:
        price_ratio = float(os.getenv("PRICE_RATIO", "0.70"))
    except ValueError as exc:
        raise ConfigError("PRICE_RATIO doit être un nombre, ex. 0.70") from exc
    if not 0 < price_ratio <= 1:
        raise ConfigError("PRICE_RATIO doit être compris entre 0 et 1.")

    run_interval = None
    raw_interval = os.getenv("RUN_INTERVAL", "").strip()
    if raw_interval:
        run_interval = parse_duration(raw_interval)
        if run_interval is None:
            raise ConfigError(f"RUN_INTERVAL illisible ({raw_interval!r}) : écrivez par ex. « 10 min » ou « 1 h ».")
        if run_interval < MIN_RUN_INTERVAL:
            raise ConfigError(f"RUN_INTERVAL doit être d'au moins {MIN_RUN_INTERVAL.seconds // 60} min.")

    return Config(
        mail=mail,
        password=password,
        base_url=os.getenv("BASE_URL", "https://www.wiki-masters.com").rstrip("/"),
        safe_cards_file=Path(os.getenv("SAFE_CARDS_FILE", "safed_cards.json")),
        state_dir=Path(os.getenv("STATE_DIR", "state")),
        price_ratio=price_ratio,
        auction_duration_label=os.getenv("AUCTION_DURATION_LABEL", "1 h"),
        headless=_bool(os.getenv("HEADLESS"), True),
        chromium_executable=os.getenv("CHROMIUM_EXECUTABLE") or None,
        sell_rarities=_rarities(os.getenv("SELL_RARITIES", DEFAULT_SELL_RARITIES)),
        session_cookies=os.getenv("SESSION_COOKIES", "").strip(),
        run_interval=run_interval,
    )
