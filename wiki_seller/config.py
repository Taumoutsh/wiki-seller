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


def _hours(value: str) -> tuple[int, int] | None:
    """« 0-6 » → (0, 6) ; vide → None (désactivé)."""
    if not value.strip():
        return None
    match = re.fullmatch(r"\s*(\d{1,2})\s*[-–àa]\s*(\d{1,2})\s*h?\s*", value)
    if not match or not (0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 24):
        raise ConfigError(f"OPEN_PACKS_HOURS illisible ({value!r}) : écrivez par ex. « 0-6 ».")
    return int(match.group(1)), int(match.group(2))


def _positive_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigError(f"{name} doit être un nombre entier positif.")
    return int(raw)


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
    # Achats : liste des cartes voulues, budget total optionnel, avance avant la fin.
    wanted_cards_file: Path = Path("wanted_cards.json")
    max_total_bids: int | None = None
    snipe_lead: timedelta = timedelta(seconds=20)
    # Heures (locales, Europe/Paris) où les paquets sont ouverts : [début, fin[ ; None = jamais.
    packs_hours: tuple[int, int] | None = (0, 6)

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
    def wanted_state_file(self) -> Path:
        return self.state_dir / "wanted_state.json"

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

    snipe_lead = None
    raw_lead = os.getenv("SNIPE_LEAD", "").strip()
    if raw_lead:
        snipe_lead = timedelta(seconds=int(raw_lead)) if raw_lead.isdigit() else None
        if snipe_lead is None or not timedelta(seconds=5) <= snipe_lead <= timedelta(minutes=5):
            raise ConfigError("SNIPE_LEAD doit être un nombre de secondes entre 5 et 300.")

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
        wanted_cards_file=Path(os.getenv("WANTED_CARDS_FILE", "wanted_cards.json")),
        max_total_bids=_positive_int("MAX_TOTAL_BIDS"),
        snipe_lead=snipe_lead or timedelta(seconds=20),
        packs_hours=_hours(os.getenv("OPEN_PACKS_HOURS", "0-6")),
    )
