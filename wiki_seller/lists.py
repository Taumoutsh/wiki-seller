"""Listes de cartes, dans state/, modifiées par le bot Telegram :

- safe_cards.json : cartes protégées (jamais défaussées, un exemplaire toujours gardé) ;
- buy_cards.json : cartes à acheter, avec leur prix maximum (et un prix de revente pour
  /buyandsell) ; une fois obtenues, elles passent dans les cartes protégées (ou dans
  les ventes, pour /buyandsell) ;
- sell_cards.json : cartes à vendre à un prix exact, en priorité, remises en vente
  jusqu'à ce qu'elles partent.

Au premier démarrage, les anciens safed_cards.json et wanted_cards.json (SAFE_CARDS_FILE,
WANTED_CARDS_FILE) sont repris dans state/. Lectures et écritures passent par LISTS_LOCK :
le bot (thread) et les passes ne se marchent jamais dessus."""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .selection import normalize_name
from .state import LISTS_LOCK, now_utc, read_json, write_json

log = logging.getLogger(__name__)

COPIES = re.compile(r"[x×](\d+)", re.I)


class ListError(Exception):
    pass


@dataclass(frozen=True)
class BuyCard:
    name: str
    max_price: int
    copies: int = 1  # exemplaires à acheter
    sell_price: int | None = None  # /buyandsell : revendue à ce prix une fois obtenue

    @property
    def key(self) -> str:
        return normalize_name(self.name)


@dataclass(frozen=True)
class SellCard:
    name: str
    price: int  # prix exact de mise en vente
    copies: int = 1  # exemplaires à vendre
    added_at: str = ""  # les ventes conclues avant ne comptent pas
    sold: tuple[str, ...] = field(default=())  # enchères vendues déjà comptées
    missing_notified: bool = False  # « introuvable dans la collection » déjà signalé
    keep_one: bool | None = None  # carte protégée en plusieurs exemplaires : le dernier est gardé
                                  # (None : pas encore trouvée dans la collection)

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    @property
    def remaining(self) -> int:
        return self.copies - len(self.sold)


def safe_file(config) -> Path:
    return config.state_dir / "safe_cards.json"


def buy_file(config) -> Path:
    return config.state_dir / "buy_cards.json"


def sell_file(config) -> Path:
    return config.state_dir / "sell_cards.json"


def legacy_files(config) -> list[Path]:
    """Anciens fichiers déjà repris dans state/, qui ne servent plus (à supprimer)."""
    pairs = ((config.safe_cards_file, safe_file(config)), (config.wanted_cards_file, buy_file(config)))
    return [old for old, new in pairs if old.is_file() and new.exists()]


def _load(path: Path, legacy: Path | None = None) -> list | dict | None:
    """Contenu d'une liste ; reprend l'ancien fichier si la liste n'existe pas encore.
    None si ni l'une ni l'autre n'existe."""
    if not path.exists() and legacy is not None and legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ListError(f"{legacy} n'est pas un JSON valide : {exc}") from exc
        write_json(path, data)
        log.info("%s repris dans %s.", legacy, path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ListError(f"{path} n'est pas un JSON valide : {exc}") from exc


# ─────────────────────────── Cartes protégées ───────────────────────────


def _safe_list(config) -> list[str]:
    data = _load(safe_file(config), config.safe_cards_file)
    if data is None:
        raise ListError("Aucune liste de cartes protégées : envoyez /safe <carte> au bot (ou créez "
                        f"{safe_file(config)} avec [] pour tout vendre).")
    if isinstance(data, dict):
        data = data.get("cards")
    if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
        raise ListError(f"{safe_file(config)} doit contenir une liste de noms de cartes.")
    return [n for n in data if n.strip()]


def safe_titles(config) -> list[str]:
    with LISTS_LOCK:
        return _safe_list(config)


def safe_names(config) -> set[str]:
    return {normalize_name(n) for n in safe_titles(config)}


def add_safe(config, name: str) -> tuple[bool, int]:
    """Renvoie (ajoutée, nombre de cartes protégées) ; False si elle y était déjà."""
    name = " ".join(name.split())
    if not name:
        raise ListError("Usage : /safe <carte>, ex. /safe Tour Eiffel")
    with LISTS_LOCK:
        try:
            names = _safe_list(config)
        except ListError:
            if safe_file(config).exists():
                raise
            names = []  # première carte protégée
        if normalize_name(name) in {normalize_name(n) for n in names}:
            return False, len(names)
        write_json(safe_file(config), names + [name])
        return True, len(names) + 1


def remove_safe(config, name: str) -> bool:
    with LISTS_LOCK:
        names = _safe_list(config)
        kept = [n for n in names if normalize_name(n) != normalize_name(name)]
        if len(kept) == len(names):
            return False
        write_json(safe_file(config), kept)
        return True


# ─────────────────────────── Achats ───────────────────────────


def _positive(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _buy_raw(config) -> list[dict]:
    data = _load(buy_file(config), config.wanted_cards_file) or []
    if not isinstance(data, list):
        raise ListError(f"{buy_file(config)} doit contenir une liste.")
    for item in data:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip()
                or not _positive(item.get("max_price")) or not _positive(item.get("copies", 1))
                or (item.get("sell_price") is not None and not _positive(item["sell_price"]))):
            raise ListError(f"{buy_file(config)} : entrée invalide {item!r}.")
    return data


def buy_cards(config) -> list[BuyCard]:
    with LISTS_LOCK:
        return [BuyCard(i["name"].strip(), i["max_price"], i.get("copies", 1), i.get("sell_price"))
                for i in _buy_raw(config)]


def set_buy(config, name: str, max_price: int, copies: int | None = None,
            sell_price: int | None = None) -> tuple[str, BuyCard]:
    """Ajoute une carte aux achats, ou met à jour ses prix. Renvoie (« ajoutée » |
    « mise à jour » | « inchangée », carte)."""
    with LISTS_LOCK:
        data = _buy_raw(config)
        entry = next((e for e in data if normalize_name(e["name"]) == normalize_name(name)), None)
        before = dict(entry) if entry else None
        if entry is None:
            entry = {"name": name}
            data.append(entry)
            _forget_purchase(config, name)  # une carte déjà obtenue peut être rachetée
        entry["max_price"] = max_price
        if copies:
            entry["copies"] = copies
        if sell_price:
            entry["sell_price"] = sell_price
        status = "ajoutée" if before is None else "inchangée" if entry == before else "mise à jour"
        if status != "inchangée":
            write_json(buy_file(config), data)
        return status, BuyCard(entry["name"], entry["max_price"], entry.get("copies", 1), entry.get("sell_price"))


def remove_buy(config, name: str) -> bool:
    with LISTS_LOCK:
        data = _buy_raw(config)
        kept = [e for e in data if normalize_name(e["name"]) != normalize_name(name)]
        if len(kept) == len(data):
            return False
        write_json(buy_file(config), kept)
        return True


def _forget_purchase(config, name: str) -> None:
    """Oublie le suivi d'achat d'une carte (state/wanted_state.json) : sans cela, une
    carte déjà obtenue et redemandée serait considérée comme déjà achetée."""
    state = read_json(config.wanted_state_file, {})
    if normalize_name(name) in state.get("cards", {}):
        del state["cards"][normalize_name(name)]
        write_json(config.wanted_state_file, state)


def complete_purchase(config, card: BuyCard, bought: int) -> str:
    """Achat terminé : la carte quitte les achats et devient protégée, ou part dans les
    ventes (/buyandsell). Renvoie le message à envoyer."""
    with LISTS_LOCK:
        remove_buy(config, card.name)
        if card.sell_price:
            set_sell(config, card.name, card.sell_price, bought)
            return f"🔁 « {card.name} » : achat terminé, mise en vente à {card.sell_price} ({bought} exemplaire(s))."
        add_safe(config, card.name)
        return f"🛡️ « {card.name} » : achat terminé ({bought} exemplaire(s)), ajoutée aux cartes protégées."


# ─────────────────────────── Ventes à prix exact ───────────────────────────


def _sell_raw(config) -> list[dict]:
    data = _load(sell_file(config)) or []
    if not isinstance(data, list) or not all(
            isinstance(i, dict) and isinstance(i.get("name"), str) and _positive(i.get("price"))
            and _positive(i.get("copies", 1)) for i in data):
        raise ListError(f"{sell_file(config)} : contenu invalide.")
    return data


def _sell_card(entry: dict) -> SellCard:
    return SellCard(entry["name"], entry["price"], entry.get("copies", 1), entry.get("added_at", ""),
                    tuple(entry.get("sold", ())), bool(entry.get("missing_notified")), entry.get("keep_one"))


def sell_cards(config) -> list[SellCard]:
    with LISTS_LOCK:
        return [_sell_card(e) for e in _sell_raw(config)]


def set_sell(config, name: str, price: int, copies: int | None = None) -> tuple[str, SellCard]:
    """Ajoute une carte aux ventes, ou met à jour son prix (les exemplaires indiqués
    s'ajoutent alors à ceux restant à vendre). Renvoie (statut, carte)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        entry = next((e for e in data if normalize_name(e["name"]) == normalize_name(name)), None)
        if entry is None:
            entry = {"name": name, "price": price, "copies": copies or 1, "added_at": now_utc().isoformat()}
            data.append(entry)
            status = "ajoutée"
        else:
            entry["price"] = price
            if copies:
                entry["copies"] = entry.get("copies", 1) + copies
            status = "mise à jour"
        write_json(sell_file(config), data)
        return status, _sell_card(entry)


def remove_sell(config, name: str) -> bool:
    with LISTS_LOCK:
        data = _sell_raw(config)
        kept = [e for e in data if normalize_name(e["name"]) != normalize_name(name)]
        if len(kept) == len(data):
            return False
        write_json(sell_file(config), kept)
        return True


def record_sale(config, name: str, auction_id: str) -> SellCard | None:
    """Note une vente conclue ; la carte quitte la liste une fois tous ses exemplaires
    vendus. Renvoie la carte à jour (None si elle n'était plus dans la liste)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        entry = next((e for e in data if normalize_name(e["name"]) == normalize_name(name)), None)
        if entry is None:
            return None
        entry["sold"] = [*entry.get("sold", []), auction_id]
        card = _sell_card(entry)
        write_json(sell_file(config), [e for e in data if e is not entry] if card.remaining <= 0 else data)
        return card


def update_sell(config, name: str, /, **changes) -> None:
    """Modifie des champs d'une carte à vendre (missing_notified, keep_one, copies)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        for entry in data:
            if normalize_name(entry["name"]) == normalize_name(name):
                entry.update(changes)
        write_json(sell_file(config), data)


# ─────────────────────────── Tout ensemble ───────────────────────────


def protected_names(config) -> set[str]:
    """Cartes jamais défaussées : protégées, à acheter et à vendre."""
    with LISTS_LOCK:
        names = set(safe_names(config))
        names |= {c.key for c in buy_cards(config)}
        names |= {c.key for c in sell_cards(config)}
        return names


def parse_card_args(args: str, prices: int, usage: str) -> tuple[str, list[int], int | None]:
    """Nom de carte suivi de `prices` nombres (les derniers), et éventuellement « x2 »
    (exemplaires), avant ou après eux. Le nom peut lui-même finir par un nombre :
    « Apollo 11 500 » → ("Apollo 11", [500], None) ; « Victor Hugo 120 x2 » →
    ("Victor Hugo", [120], 2)."""
    words = args.split()
    copies = None
    if len(words) >= prices + 2:  # il reste un nom et les prix sans le « x2 »
        for i in range(1, prices + 2):
            if match := COPIES.fullmatch(words[-i]):
                copies = int(match.group(1))
                del words[-i]
                break
    if len(words) <= prices or copies == 0 or not all(w.isdigit() and int(w) >= 1 for w in words[-prices:]):
        raise ListError(usage)
    return " ".join(words[:-prices]), [int(w) for w in words[-prices:]], copies
