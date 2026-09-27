"""Chargement de la configuration depuis l'environnement (et le fichier .env)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "oui", "on")


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
    )
