"""Modification de safed_cards.json et wanted_cards.json (commandes Telegram /safe et
/want). Les fichiers sont réécrits sur place (ils sont montés un par un dans le
conteneur, ce qui empêche de les remplacer), sous le verrou LISTS_LOCK que prennent
aussi leurs lecteurs : une passe ne lit jamais un fichier à moitié écrit."""

import json
import re
from pathlib import Path

from .buying import WantedCardsError, load_wanted_cards
from .selection import SafeCardsError, normalize_name
from .state import LISTS_LOCK

WANT_USAGE = ("Usage : /want <nom de la carte> <prix max> [x<exemplaires>], ex. /want Tour Eiffel 500 "
              "ou /want Victor Hugo 120 x2")
COPIES = re.compile(r"[x×](\d+)", re.I)


def _read_list(path: Path, error):
    if path.is_dir():  # Docker crée un dossier si le fichier monté n'existe pas
        raise error(f"{path} est un dossier : créez le fichier.")
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise error(f"{path} n'est pas un JSON valide : {exc}") from exc


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def add_safe_card(path: Path, name: str) -> tuple[bool, int]:
    """Ajoute une carte à safed_cards.json. Renvoie (ajoutée, nombre de cartes) ;
    False si elle y était déjà (majuscules, accents et espaces ignorés)."""
    name = " ".join(name.split())
    if not name:
        raise SafeCardsError("Usage : /safe <nom de la carte>, ex. /safe Tour Eiffel")
    with LISTS_LOCK:
        data = _read_list(path, SafeCardsError)
        names = data.get("cards") if isinstance(data, dict) else data
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise SafeCardsError(f"{path} doit contenir une liste de noms de cartes.")
        if normalize_name(name) in {normalize_name(n) for n in names}:
            return False, len(names)
        names.append(name)
        _write(path, {**data, "cards": names} if isinstance(data, dict) else names)
        return True, len(names)


def parse_want(args: str) -> tuple[str, int, int | None]:
    """Le dernier nombre est le prix maximum, « x2 » le nombre d'exemplaires (avant ou
    après le prix) ; le reste est le nom, qui peut lui-même finir par un nombre :
    « Apollo 11 500 » → ("Apollo 11", 500, None) ; « Victor Hugo 120 x2 » →
    ("Victor Hugo", 120, 2)."""
    words = args.split()
    copies = None
    for i in (-1, -2):
        if len(words) >= 3 and (match := COPIES.fullmatch(words[i])):  # il reste un nom et un prix
            copies = int(match.group(1))
            del words[i]
            break
    if len(words) < 2 or not words[-1].isdigit() or int(words[-1]) < 1 or copies == 0:
        raise WantedCardsError(WANT_USAGE)
    return " ".join(words[:-1]), int(words[-1]), copies


def set_wanted_card(path: Path, name: str, max_price: int, copies: int | None = None) -> tuple[str, dict]:
    """Ajoute une carte à wanted_cards.json, ou met à jour son prix (et ses exemplaires)
    si elle y est déjà. Renvoie ("ajoutée" | "mise à jour" | "inchangée", entrée)."""
    with LISTS_LOCK:
        load_wanted_cards(path)  # vérifie le fichier avant d'y toucher
        data = _read_list(path, WantedCardsError)
        entry = next((e for e in data if normalize_name(e["name"]) == normalize_name(name)), None)
        if entry is None:
            entry = {"name": name, "max_price": max_price, **({"copies": copies} if copies else {})}
            data.append(entry)
            status = "ajoutée"
        else:
            before = dict(entry)
            entry["max_price"] = max_price
            if copies:
                entry["copies"] = copies
            status = "inchangée" if entry == before else "mise à jour"
        if status != "inchangée":
            _write(path, data)
        return status, entry
