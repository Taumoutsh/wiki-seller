"""Réglages d'un compte, modifiables par le bot (/settings, /set, /pause, /resume,
/simulation) et gardés dans state/users/<chat_id>/settings.json, tels qu'ils ont été
écrits. Un réglage absent prend la valeur du .env.

Un nouveau compte démarre en pause, sans vente automatique ni défausse : il les active
lui-même une fois ses cookies envoyés."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from .config import MIN_RUN_INTERVAL, Config, ConfigError, _night_durations, parse_duration
from .state import read_json, write_json

log = logging.getLogger(__name__)

RARITIES = ("L", "UR", "SR", "R", "PC", "C")
DURATIONS = {10: "10 min", 30: "30 min", 60: "1 h", 180: "3 h", 360: "6 h", 720: "12 h"}
YES, NO = ("on", "oui", "1", "true", "yes", "actif", "active"), ("off", "non", "0", "false", "no", "inactif")
NEW_ACCOUNT = {"raretes": "aucune", "defausse": "0", "pause": "on"}


class SettingError(ValueError):
    pass


def _switch(text: str) -> bool:
    value = text.strip().lower()
    if value in YES:
        return True
    if value in NO:
        return False
    raise SettingError("écrivez on ou off")


def _percent(text: str) -> float:
    try:
        value = float(text.strip().rstrip("%").replace(",", ".").strip())
    except ValueError as exc:
        raise SettingError("un pourcentage, ex. 70") from exc
    ratio = value / 100 if value > 1 else value
    if not 0 < ratio <= 1:
        raise SettingError("entre 1 et 100 %")
    return ratio


def _integer(low: int, high: int | None = None) -> Callable[[str], int]:
    def parse(text: str) -> int:
        if not text.strip().isdigit() or int(text) < low or (high is not None and int(text) > high):
            raise SettingError(f"un nombre entier{f' entre {low} et {high}' if high else f' ≥ {low}'}")
        return int(text)
    return parse


def _rarities(text: str) -> dict:
    value = text.strip().lower()
    if value in ("aucune", "non", "off", "0"):
        return {"selling": False}
    if value in ("toutes", "tout", "all", "*"):
        return {"selling": True, "sell_rarities": ()}
    codes = tuple(c.strip().upper() for c in text.replace(";", ",").replace(" ", ",").split(",") if c.strip())
    unknown = [c for c in codes if c not in RARITIES]
    if not codes or unknown:
        raise SettingError(f"des raretés parmi {', '.join(RARITIES)} (ex. L,SR), « toutes » ou « aucune »")
    return {"selling": True, "sell_rarities": codes}


def _duration_label(text: str) -> str:
    duration = parse_duration(text)
    label = DURATIONS.get(int(duration.total_seconds() // 60)) if duration else None
    if not label:
        raise SettingError(f"une durée du site : {', '.join(DURATIONS.values())}")
    return label


def _night(text: str):
    if text.strip().lower() in ("aucune", "non", "off", ""):
        return ()
    try:
        return _night_durations(text)
    except ConfigError as exc:
        raise SettingError("ex. 1-3=12 h;3-6=6 h (heures de Paris), ou « aucune »") from exc


def _interval(text: str):
    if text.strip().lower() in ("auto", "aucun", ""):
        return None
    duration = parse_duration(text)
    if duration is None or duration < MIN_RUN_INTERVAL:
        raise SettingError(f"une durée d'au moins {MIN_RUN_INTERVAL.seconds // 60} min (ex. 1 h), ou « auto »")
    return duration


def _budget(text: str):
    if text.strip().lower() in ("aucun", "0", "non", "off"):
        return None
    return _integer(1)(text)


def _minutes(d) -> str:
    total = int(d.total_seconds() // 60)
    return f"{total // 60} h" + (f"{total % 60:02d}" if total % 60 else "") if total >= 60 else f"{total} min"


def _on(value: bool) -> str:
    return "on" if value else "off"


@dataclass(frozen=True)
class Setting:
    key: str
    help: str
    parse: Callable[[str], dict]  # texte → changements de Config
    show: Callable[[Config], str]


SETTINGS = (
    Setting("raretes", "raretés mises en vente automatiquement (ex. L,SR), toutes ou aucune",
            _rarities, lambda c: (",".join(c.sell_rarities) or "toutes") if c.selling else "aucune"),
    Setting("prix", "mise de départ, en % du prix moyen",
            lambda t: {"price_ratio": _percent(t)}, lambda c: f"{round(c.price_ratio * 100)} %"),
    Setting("ajustement", "prix ajusté d'après les enchères en cours de la carte (on/off)",
            lambda t: {"market_adjust": _switch(t)}, lambda c: _on(c.market_adjust)),
    Setting("plancher", "ajustement jamais sous ce % du prix moyen",
            lambda t: {"price_floor_ratio": _percent(t)}, lambda c: f"{round(c.price_floor_ratio * 100)} %"),
    Setting("duree", "durée des ventes (10 min, 30 min, 1 h, 3 h, 6 h, 12 h)",
            lambda t: {"auction_duration_label": _duration_label(t)}, lambda c: c.auction_duration_label),
    Setting("nuit", "durées de nuit, heures de Paris (ex. 1-3=12 h;3-6=6 h) ou aucune",
            lambda t: {"night_durations": _night(t)},
            lambda c: ";".join(f"{a}-{b}={label}" for a, b, label in c.night_durations) or "aucune"),
    Setting("intervalle", "délai entre deux passes (ex. 1 h), auto = durée des ventes",
            lambda t: {"run_interval": _interval(t)}, lambda c: _minutes(c.run_interval) if c.run_interval else "auto"),
    Setting("invendues", "ventes ratées avant de mettre une carte de côté",
            lambda t: {"unsold_max_tries": _integer(1)(t)}, lambda c: str(c.unsold_max_tries)),
    Setting("pause_invendues", "jours de mise de côté d'une carte invendue",
            lambda t: {"unsold_cooldown": timedelta(days=_integer(1)(t))}, lambda c: str(c.unsold_cooldown.days)),
    Setting("defausse", "défausse des cartes valant moins que ce prix (0 = coupée)",
            lambda t: {"discard_below": _integer(0)(t)}, lambda c: str(c.discard_below) if c.discard_below else "0 (coupée)"),
    Setting("seuil_defausse", "exemplaires au-delà desquels la défausse démarre",
            lambda t: {"discard_trigger": _integer(0)(t)}, lambda c: str(c.discard_trigger)),
    Setting("budget", "somme maximale des mises en cours, ou aucun",
            lambda t: {"max_total_bids": _budget(t)}, lambda c: str(c.max_total_bids or "aucun")),
    Setting("surenchere", "secondes avant la fin d'une enchère pour surenchérir (2 à 300)",
            lambda t: {"snipe_lead": timedelta(seconds=_integer(2, 300)(t))},
            lambda c: f"{int(c.snipe_lead.total_seconds())} s"),
    Setting("bilan", "bilan automatique toutes les N heures (0 = jamais)",
            lambda t: {"report_every": timedelta(hours=n) if (n := _integer(0, 168)(t)) else None},
            lambda c: f"{int(c.report_every.total_seconds() // 3600)} h" if c.report_every else "jamais"),
    Setting("simulation", "tout simuler, rien n'est envoyé au site (on/off)",
            lambda t: {"simulation": _switch(t)}, lambda c: _on(c.simulation)),
    Setting("pause", "compte en pause : ni vente, ni achat, ni défausse (on/off)",
            lambda t: {"paused": _switch(t)}, lambda c: _on(c.paused)),
)
BY_KEY = {s.key: s for s in SETTINGS}
ALIASES = {"rarete": "raretes", "rarities": "raretes", "duree_nuit": "nuit", "défausse": "defausse",
           "durée": "duree", "surenchère": "surenchere", "raretés": "raretes"}


def _file(config: Config):
    return config.state_dir / "settings.json"


def stored(config: Config) -> dict[str, str]:
    return read_json(_file(config), {})


def apply(config: Config) -> dict:
    """Changements de Config tirés de settings.json (un réglage illisible est ignoré)."""
    changes = {}
    for key, text in stored(config).items():
        setting = BY_KEY.get(key)
        if setting is None:
            continue
        try:
            changes.update(setting.parse(str(text)))
        except SettingError as exc:
            log.warning("Réglage « %s » ignoré (%s) : %s", key, text, exc)
    return changes


def set_value(config: Config, key: str, text: str) -> Setting:
    """Vérifie puis enregistre un réglage ; SettingError si la valeur est invalide."""
    key = ALIASES.get(key.lower(), key.lower())
    setting = BY_KEY.get(key)
    if setting is None:
        raise SettingError(f"réglage inconnu « {key} » (voir /settings)")
    setting.parse(text)  # valide
    data = stored(config)
    data[key] = " ".join(text.split())
    write_json(_file(config), data)
    return setting


def init_new_account(config: Config) -> None:
    if not _file(config).exists():
        write_json(_file(config), dict(NEW_ACCOUNT))
