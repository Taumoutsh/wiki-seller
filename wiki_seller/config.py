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
# La nuit, moins d'activité : ventes plus longues pour laisser les enchères monter.
DEFAULT_NIGHT_DURATIONS = "1-3=12 h;3-6=6 h"


def parse_duration(text: str) -> timedelta | None:
    """« 10m », « 10 min », « 1h », « 2 heures », « 1h30 » → durée ; None si illisible."""
    match = re.fullmatch(
        r"\s*(?:(\d+)\s*h(?:eures?)?)?\s*(?:(\d+)\s*(?:m(?:in(?:utes?)?)?)?)?\s*", text.lower())
    if not match or not any(match.groups()):
        return None
    hours, minutes = (int(g) if g else 0 for g in match.groups())
    if not match.group(1) and not re.search(r"m", text.lower()):
        return None  # « 10 » seul : unité ambiguë
    duration = timedelta(hours=hours, minutes=minutes)
    return duration if duration > timedelta(0) else None


def short_duration(value) -> str:
    """Durée (timedelta ou libellé « 12 h ») au format compact : 12h, 30m, 1h30."""
    duration = value if isinstance(value, timedelta) else parse_duration(str(value))
    if duration is None:
        return str(value)
    hours, minutes = divmod(int(duration.total_seconds() // 60), 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h{minutes:02d}" if minutes else f"{hours}h"


def _hours(value: str) -> tuple[int, int] | None:
    """« 1-3 » → (1, 3) ; illisible → None."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*[-–àa]\s*(\d{1,2})\s*h?\s*", value)
    if not match or not (0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 24):
        return None
    return int(match.group(1)), int(match.group(2))


def _ratio(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default) or default)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre, ex. {default}") from exc
    if not 0 < value <= 1:
        raise ConfigError(f"{name} doit être compris entre 0 et 1.")
    return value


def _night_durations(value: str) -> tuple[tuple[int, int, str], ...]:
    """« 1-3=12 h;3-6=6 h » → ((1, 3, "12 h"), (3, 6, "6 h")) ; vide → ()."""
    ranges = []
    for part in filter(None, (p.strip() for p in value.split(";"))):
        hours, sep, label = part.partition("=")
        span = _hours(hours) if sep else None
        label = label.strip()
        if not span or not parse_duration(label):
            raise ConfigError(f"NIGHT_DURATIONS illisible ({part!r}) : écrivez par ex. « 1-3=12 h;3-6=6 h ».")
        ranges.append((span[0], span[1], label))
    return tuple(ranges)


def _positive_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigError(f"{name} doit être un nombre entier positif.")
    return int(raw)


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ConfigError(f"{name} doit être un nombre entier (0 pour désactiver).")
    return int(raw)


# Options des anciennes versions, sans effet aujourd'hui : signalées au démarrage.
OBSOLETE_SETTINGS = {
    "ACTIONS_VIA": "toutes les actions passent par l'API",
    "OPEN_PACKS_HOURS": "l'ouverture des paquets a été retirée",
    "PACKS_VERIFY_WAIT": "l'ouverture des paquets a été retirée",
    "NOTIFY_URL": "les notifications passent par le bot Telegram",
    "API_TRACE": "le journal des appels à l'API a été retiré",
    "MAIL": "la connexion par mot de passe n'est plus utilisée (session par cookies)",
    "PASSWORD": "la connexion par mot de passe n'est plus utilisée (session par cookies)",
    "HEADLESS": "le script n'utilise plus de navigateur",
    "CHROMIUM_EXECUTABLE": "le script n'utilise plus de navigateur",
}


def obsolete_settings() -> list[str]:
    """Options obsolètes encore définies (après load_config, qui lit le .env)."""
    return [name for name in OBSOLETE_SETTINGS if os.getenv(name, "").strip()]


def _rarities(value: str) -> tuple[str, ...]:
    return tuple(r.strip().upper() for r in value.replace(";", ",").split(",") if r.strip())


@dataclass(frozen=True)
class Config:
    base_url: str
    safe_cards_file: Path  # ancienne liste, reprise dans state/ (lists.py) ; à retirer
    state_dir: Path
    price_ratio: float
    auction_duration_label: str
    # Raretés mises en vente (codes du site : L, UR, SR, R, PC, C) ; vide = toutes.
    sell_rarities: tuple[str, ...] = ()
    # Cookies d'une session connectée (« nom=valeur; nom2=valeur2 »), voir README.
    session_cookies: str = ""
    # Session du site (Supabase) : adresse et clé publique, déduites des cookies et du
    # code du site si vides.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Plusieurs comptes : state_dir est le dossier du compte (state/users/<chat_id>),
    # shared_dir celui des fichiers communs (cache des prix, bot Telegram, journal).
    shared_dir: Path | None = None
    # Réglages d'un compte (settings.py) : ventes automatiques des raretés ci-dessus,
    # compte en pause, tout simulé.
    selling: bool = True
    paused: bool = False
    simulation: bool = False
    account_name: str = ""  # nom du compte, pour le journal
    # Délai avant la passe suivante ; None = durée des enchères (AUCTION_DURATION_LABEL).
    run_interval: timedelta | None = None
    # Achats : ancienne liste (reprise dans state/, à retirer), budget total optionnel,
    # avance avant la fin.
    wanted_cards_file: Path = Path("wanted_cards.json")
    max_total_bids: int | None = None
    snipe_lead: timedelta = timedelta(seconds=30)
    # Cartes invendues : mises de côté après unsold_max_tries échecs, pendant unsold_cooldown.
    unsold_max_tries: int = 3
    unsold_cooldown: timedelta = timedelta(days=5)
    # Durée de validité des prix moyens en cache (state/price_cache.json).
    price_cache_ttl: timedelta = timedelta(hours=24)
    # Vente automatique : ventes connues minimales pour se fier au prix d'une carte (sinon mise de côté).
    min_sales_to_sell: int = 4
    # Durées de vente la nuit : ((début, fin, libellé), ...), heures de Paris [début, fin[.
    night_durations: tuple[tuple[int, int, str], ...] = ()
    # Bot Telegram qui demande une nouvelle session quand elle expire (vide = désactivé).
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_wait: timedelta = timedelta(minutes=60)
    telegram_api_url: str = "https://api.telegram.org"
    # Bilan Telegram (ventes, défausses, solde) tous les report_every ; None = jamais.
    report_every: timedelta | None = None
    # Prix de vente ajusté d'un écart type d'après les enchères en cours de la même carte,
    # sans descendre sous price_floor_ratio × prix moyen.
    market_adjust: bool = False
    price_floor_ratio: float = 0.5
    # Achats : prix max misé dès qu'une enchère est choisie (dépassé : abandonnée pour une
    # autre), ou mise minimale seulement à snipe_lead de la fin (enchère suivie sans miser).
    bid_now: bool = True
    # Défausse en arrière-plan des cartes valant moins de discard_below (0 = désactivée),
    # lancée quand la collection dépasse discard_trigger exemplaires.
    discard_below: int = 0
    discard_trigger: int = 150
    # Raretés que la défausse peut toucher (les légendaires jamais, par défaut).
    discard_rarities: tuple[str, ...] = ("UR", "SR", "R", "PC", "C")

    def duration_label_at(self, hour: int) -> str:
        """Libellé de durée des ventes à cette heure : plage de NIGHT_DURATIONS, sinon
        AUCTION_DURATION_LABEL."""
        for start, end, label in self.night_durations:
            if (start <= hour < end) if start < end else (hour >= start or hour < end):
                return label
        return self.auction_duration_label

    @property
    def pass_interval(self) -> timedelta:
        return self.run_interval or parse_duration(self.auction_duration_label) or DEFAULT_RUN_INTERVAL

    @property
    def shared_state_dir(self) -> Path:
        return self.shared_dir or self.state_dir

    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def storage_state_file(self) -> Path:  # session de l'ancien navigateur, reprise une fois
        return self.state_dir / "storage_state.json"

    @property
    def run_state_file(self) -> Path:
        return self.state_dir / "run_state.json"

    @property
    def wanted_state_file(self) -> Path:
        return self.state_dir / "wanted_state.json"

    @property
    def unsold_file(self) -> Path:
        return self.state_dir / "unsold.json"

    @property
    def debug_dir(self) -> Path:
        return self.state_dir / "debug"


def load_config() -> Config:
    # Les variables déjà présentes dans l'environnement (ex. Docker) priment sur le .env.
    load_dotenv(override=False)

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
        if snipe_lead is None or not timedelta(seconds=2) <= snipe_lead <= timedelta(minutes=5):
            raise ConfigError("SNIPE_LEAD doit être un nombre de secondes entre 2 et 300.")

    return Config(
        base_url=os.getenv("BASE_URL", "https://www.wiki-masters.com").rstrip("/"),
        safe_cards_file=Path(os.getenv("SAFE_CARDS_FILE", "safed_cards.json")),
        state_dir=Path(os.getenv("STATE_DIR", "state")),
        price_ratio=price_ratio,
        auction_duration_label=os.getenv("AUCTION_DURATION_LABEL", "1 h"),
        sell_rarities=_rarities(os.getenv("SELL_RARITIES", DEFAULT_SELL_RARITIES)),
        session_cookies=os.getenv("SESSION_COOKIES", "").strip(),
        supabase_url=os.getenv("SUPABASE_URL", "").strip(),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", "").strip(),
        run_interval=run_interval,
        wanted_cards_file=Path(os.getenv("WANTED_CARDS_FILE", "wanted_cards.json")),
        max_total_bids=_positive_int("MAX_TOTAL_BIDS"),
        snipe_lead=snipe_lead or timedelta(seconds=30),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_wait=timedelta(minutes=_positive_int("TELEGRAM_WAIT") or 60),
        report_every=(timedelta(hours=hours) if (hours := _non_negative_int("REPORT_HOURS", 2)) else None),
        unsold_max_tries=_positive_int("UNSOLD_MAX_TRIES") or 3,
        unsold_cooldown=timedelta(days=_positive_int("UNSOLD_COOLDOWN_DAYS") or 5),
        price_cache_ttl=timedelta(hours=_positive_int("PRICE_CACHE_HOURS") or 24),
        min_sales_to_sell=_positive_int("MIN_SALES_TO_SELL") or 4,
        night_durations=_night_durations(os.getenv("NIGHT_DURATIONS", DEFAULT_NIGHT_DURATIONS)),
        market_adjust=_bool(os.getenv("MARKET_ADJUST"), True),
        bid_now=_bool(os.getenv("BID_NOW"), True),
        price_floor_ratio=_ratio("PRICE_FLOOR_RATIO", "0.5"),
        discard_below=_non_negative_int("DISCARD_BELOW", 15),
        discard_trigger=_non_negative_int("DISCARD_TRIGGER", 150),
        discard_rarities=_rarities(os.getenv("DISCARD_RARITIES") or "UR,SR,R,PC,C"),
    )
