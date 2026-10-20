"""Listes de cartes, dans state/, modifiées par le bot Telegram :

- safe_cards.json : cartes protégées (jamais défaussées, aucun exemplaire vendu
  automatiquement) ;
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

from .config import parse_duration, short_duration
from .selection import normalize_name
from .settings import RARITIES
from .state import LISTS_LOCK, now_utc, read_json, write_json

log = logging.getLogger(__name__)

COPIES = re.compile(r"[x×](\d+)", re.I)
# Prix « auto » (prix omis dans /buy, /sell, /buyandsell) : calculé à chaque passe d'après
# le prix du marché (pricing.py) : achat jusqu'à 90 %, vente à 110 %.
AUTO = "auto"
AUTO_BUY_RATIO = 0.9
AUTO_SELL_RATIO = 1.1
SEPARATOR = ";"  # termine le nom d'une carte
SHINY = "L+"  # rareté demandée : légendaire shiny (sans « + », les shiny sont exclus)
SHINY_NO_AUTO = ("Prix obligatoire pour une carte shiny (L+) : le prix du marché ne distingue pas les "
                 "shiny des autres.")


def group_matches(group, rarity: str) -> bool:
    """Exemplaires de la collection visés par une rareté demandée : L+ = légendaires
    shiny ; une autre rareté, ou aucune, ne prend jamais d'exemplaire shiny."""
    if rarity == SHINY:
        return group.rarity == "L" and group.shiny
    return not group.shiny and (not rarity or group.rarity == rarity)


class ListError(Exception):
    pass


def line_id(name: str, rarity: str = "") -> str:
    """Identifiant d'une ligne d'achat ou de vente : nom, et rareté s'il y en a une."""
    return normalize_name(name) + (f"|{rarity}" if rarity else "")


def _find(data: list[dict], name: str, rarity: str | None, what: str) -> dict | None:
    """Ligne de cette carte : celle de la rareté demandée (« » : la ligne sans rareté) ;
    rareté non précisée (None) : la seule ligne de la carte, ListError s'il y en a plusieurs."""
    same = [e for e in data if normalize_name(e["name"]) == normalize_name(name)]
    if rarity is None:
        if len(same) > 1:
            raise ListError(f"« {same[0]['name']} » est {what} en plusieurs raretés "
                            f"({', '.join(e.get('rarity') or 'toutes' for e in same)}) : précisez laquelle.")
        return same[0] if same else None
    return next((e for e in same if e.get("rarity", "") == rarity), None)


def _remove(data: list[dict], name: str, rarity: str | None) -> list[dict]:
    """Liste sans les lignes de cette carte (toutes ses raretés si rarity est None)."""
    return [e for e in data if normalize_name(e["name"]) != normalize_name(name)
            or (rarity is not None and e.get("rarity", "") != rarity)]


@dataclass(frozen=True)
class BuyCard:
    name: str
    max_price: int | str  # AUTO : 90 % du prix du marché
    copies: int = 1  # exemplaires à acheter
    sell_price: int | str | None = None  # /buyandsell : revendue à ce prix (AUTO : 110 % du marché)
    seller: str = ""  # @Pseudo : n'acheter qu'à ce vendeur
    sell_duration: str = ""  # /buyandsell : durée de la revente (ex. 12h) ; vide = habituelle
    rarity: str = ""  # n'acheter que cette rareté (ex. SR) ; vide = toutes

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    @property
    def id(self) -> str:
        """Ligne de la liste : une carte peut y être une fois par rareté (ex. L et L+)."""
        return line_id(self.name, self.rarity)


@dataclass(frozen=True)
class SellCard:
    name: str
    price: int | str  # prix exact de mise en vente ; AUTO : 110 % du prix du marché
    copies: int = 1  # exemplaires à vendre
    added_at: str = ""  # les ventes conclues avant ne comptent pas
    sold: tuple[str, ...] = field(default=())  # enchères vendues déjà comptées
    missing_notified: bool = False  # « introuvable dans la collection » déjà signalé
    keep_one: bool | None = None  # carte protégée en plusieurs exemplaires : le dernier est gardé
                                  # (None : pas encore trouvée dans la collection)
    duration: str = ""  # durée des ventes (ex. 12h) ; vide = durée habituelle du compte
    rarity: str = ""  # ne vendre que cette rareté (ex. SR) ; vide = toutes

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    @property
    def id(self) -> str:
        return line_id(self.name, self.rarity)

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


def _price(value) -> bool:
    return value == AUTO or _positive(value)


def price_text(value, ratio: float) -> str:
    """« 500 », ou « auto (90 % du marché) »."""
    return f"auto ({round(ratio * 100)} % du marché)" if value == AUTO else str(value)


def _buy_raw(config) -> list[dict]:
    data = _load(buy_file(config), config.wanted_cards_file) or []
    if not isinstance(data, list):
        raise ListError(f"{buy_file(config)} doit contenir une liste.")
    for item in data:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip()
                or not _price(item.get("max_price")) or not _positive(item.get("copies", 1))
                or (item.get("sell_price") is not None and not _price(item["sell_price"]))
                or not isinstance(item.get("seller", ""), str) or not isinstance(item.get("sell_duration", ""), str)
                or item.get("rarity", "") not in ("", *RARITIES, SHINY)):
            raise ListError(f"{buy_file(config)} : entrée invalide {item!r}.")
    return data


def buy_cards(config) -> list[BuyCard]:
    with LISTS_LOCK:
        return [_buy_card(i) for i in _buy_raw(config)]


def _buy_card(entry: dict) -> BuyCard:
    return BuyCard(entry["name"].strip(), entry["max_price"], entry.get("copies", 1), entry.get("sell_price"),
                   entry.get("seller", ""), entry.get("sell_duration", ""), entry.get("rarity", ""))


def set_buy(config, name: str, max_price: int | str | None, copies: int | None = None,
            sell_price: int | str | None = None, seller: str | None = None,
            sell_duration: str | None = None, rarity: str | None = None,
            resale: bool | None = None) -> tuple[str, BuyCard]:
    """Ajoute une carte aux achats, ou met à jour ses prix (et options indiquées) ; sans
    prix max, celui de la liste est gardé (AUTO pour une nouvelle carte). Une carte a une
    ligne par rareté : rarity choisit la ligne (None : la seule ligne de la carte).
    resale=False (/buy) : achetée pour être gardée, la revente d'un ancien /buyandsell est
    retirée. Renvoie (« ajoutée » | « mise à jour » | « inchangée », carte)."""
    with LISTS_LOCK:
        data = _buy_raw(config)
        entry = _find(data, name, rarity, "dans les achats")
        before = dict(entry) if entry else None
        if entry is None:
            entry = {"name": name}
            data.append(entry)
            _forget_purchase(config, line_id(name, rarity or ""))  # une carte déjà obtenue peut être rachetée
        if max_price is not None or "max_price" not in entry:
            entry["max_price"] = AUTO if max_price is None else max_price
        if copies:
            entry["copies"] = copies
        if resale is False:  # /buy : protégée une fois obtenue, jamais revendue
            entry.pop("sell_price", None)
            entry.pop("sell_duration", None)
        if sell_price:
            entry["sell_price"] = sell_price
        if seller:
            entry["seller"] = seller
        if sell_duration:
            entry["sell_duration"] = sell_duration
        if rarity:
            entry["rarity"] = rarity
        if entry.get("rarity") == SHINY and AUTO in (entry.get("max_price"), entry.get("sell_price")):
            raise ListError(SHINY_NO_AUTO)
        status = "ajoutée" if before is None else "inchangée" if entry == before else "mise à jour"
        if status != "inchangée":
            write_json(buy_file(config), data)
        return status, _buy_card(entry)


def remove_buy(config, name: str, rarity: str | None = None) -> int:
    """Retire la carte des achats (toutes ses raretés si rarity est None) ; renvoie le
    nombre de lignes retirées."""
    with LISTS_LOCK:
        data = _buy_raw(config)
        kept = _remove(data, name, rarity)
        if len(kept) < len(data):
            write_json(buy_file(config), kept)
        return len(data) - len(kept)


def _forget_purchase(config, card_id: str) -> None:
    """Oublie le suivi d'achat d'une ligne (state/wanted_state.json) : sans cela, une
    carte déjà obtenue et redemandée serait considérée comme déjà achetée."""
    state = read_json(config.wanted_state_file, {})
    if card_id in state.get("cards", {}):
        del state["cards"][card_id]
        write_json(config.wanted_state_file, state)


def complete_purchase(config, card: BuyCard, bought: int) -> str:
    """Achat terminé : la carte quitte les achats et devient protégée, ou part dans les
    ventes (/buyandsell). Renvoie le message à envoyer."""
    with LISTS_LOCK:
        remove_buy(config, card.name, card.rarity)
        if card.sell_price:
            set_sell(config, card.name, card.sell_price, bought, card.sell_duration or None, card.rarity)
            duration = f" pour {card.sell_duration}" if card.sell_duration else ""
            return (f"🔁 « {card.name} » : achat terminé, mise en vente à "
                    f"{price_text(card.sell_price, AUTO_SELL_RATIO)}{duration} "
                    f"({bought} exemplaire(s)).")
        add_safe(config, card.name)
        return f"🛡️ « {card.name} » : achat terminé ({bought} exemplaire(s)), ajoutée aux cartes protégées."


# ─────────────────────────── Ventes à prix exact ───────────────────────────


def _sell_raw(config) -> list[dict]:
    data = _load(sell_file(config)) or []
    if not isinstance(data, list) or not all(
            isinstance(i, dict) and isinstance(i.get("name"), str) and _price(i.get("price"))
            and _positive(i.get("copies", 1)) and i.get("rarity", "") in ("", *RARITIES, SHINY) for i in data):
        raise ListError(f"{sell_file(config)} : contenu invalide.")
    return data


def _sell_card(entry: dict) -> SellCard:
    return SellCard(entry["name"], entry["price"], entry.get("copies", 1), entry.get("added_at", ""),
                    tuple(entry.get("sold", ())), bool(entry.get("missing_notified")), entry.get("keep_one"),
                    entry.get("duration", ""), entry.get("rarity", ""))


def sell_cards(config) -> list[SellCard]:
    with LISTS_LOCK:
        return [_sell_card(e) for e in _sell_raw(config)]


def set_sell(config, name: str, price: int | str | None, copies: int | None = None,
             duration: str | None = None, rarity: str | None = None) -> tuple[str, SellCard]:
    """Ajoute une carte aux ventes, ou met à jour son prix (les exemplaires indiqués
    s'ajoutent alors à ceux restant à vendre, la durée indiquée remplace l'ancienne) ;
    sans prix, celui de la liste est gardé (AUTO pour une nouvelle carte). Une ligne par
    rareté, comme les achats. Renvoie (statut, carte)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        entry = _find(data, name, rarity, "dans les ventes")
        if entry is None:
            entry = {"name": name, "price": AUTO if price is None else price, "copies": copies or 1,
                     "added_at": now_utc().isoformat()}
            data.append(entry)
            status = "ajoutée"
        else:
            if price is not None:
                entry["price"] = price
            if copies:
                entry["copies"] = entry.get("copies", 1) + copies
            status = "mise à jour"
        if duration:
            entry["duration"] = duration
        if rarity:
            entry["rarity"] = rarity
        if entry.get("rarity") == SHINY and entry.get("price") == AUTO:
            raise ListError(SHINY_NO_AUTO)
        write_json(sell_file(config), data)
        return status, _sell_card(entry)


def remove_sell(config, name: str, rarity: str | None = None) -> int:
    with LISTS_LOCK:
        data = _sell_raw(config)
        kept = _remove(data, name, rarity)
        if len(kept) < len(data):
            write_json(sell_file(config), kept)
        return len(data) - len(kept)


def record_sale(config, name: str, auction_id: str, rarity: str = "") -> SellCard | None:
    """Note une vente conclue ; la carte quitte la liste une fois tous ses exemplaires
    vendus. Renvoie la carte à jour (None si elle n'était plus dans la liste)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        entry = _find(data, name, rarity, "dans les ventes")
        if entry is None:
            return None
        entry["sold"] = [*entry.get("sold", []), auction_id]
        card = _sell_card(entry)
        write_json(sell_file(config), [e for e in data if e is not entry] if card.remaining <= 0 else data)
        return card


def update_sell(config, name: str, rarity: str, /, **changes) -> None:
    """Modifie des champs d'une ligne à vendre (missing_notified, keep_one, copies, name)."""
    with LISTS_LOCK:
        data = _sell_raw(config)
        entry = _find(data, name, rarity, "dans les ventes")
        if entry is not None:
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


SALE_MINUTES = (10, 30, 60, 180, 360, 720)  # durées de vente du site
SELLER = re.compile(r"@(\S+)")


@dataclass(frozen=True)
class CardArgs:
    name: str
    prices: list[int | str]  # AUTO pour un prix omis (optional_prices)
    copies: int | None = None
    duration: str = ""  # durée de vente, ex. 12h
    seller: str = ""  # pseudo du vendeur, sans @
    rarity: str = ""  # rareté voulue (L, UR, SR, R, PC, C)


def _option(word: str, allowed: tuple[str, ...]) -> tuple[str, object] | None:
    """(« copies » | « duree » | « vendeur » | « rarete », valeur) si le mot est une option
    permise."""
    if "copies" in allowed and (match := COPIES.fullmatch(word)):
        return "copies", int(match.group(1))
    if "vendeur" in allowed and (match := SELLER.fullmatch(word)):
        return "vendeur", match.group(1)
    if "duree" in allowed and not word.isdigit() and (duration := parse_duration(word)) is not None:
        return "duree", duration
    if "rarete" in allowed and (word.upper() in RARITIES or word.upper() == SHINY):
        return "rarete", word.upper()
    return None


def parse_card_command(args: str, prices: int, usage: str, allowed: tuple[str, ...] = ("copies",),
                       optional_prices: bool = False) -> CardArgs:
    """Nom de carte suivi de `prices` nombres (les derniers), avec des options avant ou
    après eux : « x2 » (exemplaires), « 12h » ou « 30m » (durée de vente), « @Pseudo »
    (vendeur). Le nom peut lui-même finir par un nombre : « Apollo 11 500 » →
    Apollo 11 à 500 ; « Victor Hugo 120 x2 12h » → 2 exemplaires, vendus 12h.

    optional_prices : les prix peuvent manquer (None) ou s'écrire « auto » (AUTO), les
    premiers étant donnés d'abord : « Tour Eiffel 500 » pour deux prix → [500, None]. Le
    séparateur « ; » termine le nom : « Apollo 11; x2 » → Apollo 11, sans prix."""
    if optional_prices:
        return _parse_optional(args, prices, usage, allowed)
    words = args.split()
    options: dict[str, object] = {}

    def take_options() -> None:  # options en fin de liste, sans jamais vider le nom
        while len(words) > 1 and (option := _option(words[-1], allowed)) and option[0] not in options:
            options[option[0]] = option[1]
            words.pop()

    take_options()
    if len(words) <= prices or not all(w.isdigit() and int(w) >= 1 for w in words[-prices:]):
        raise ListError(usage)
    numbers = [int(w) for w in words[-prices:]]
    del words[-prices:]
    take_options()
    if options.get("copies") == 0:
        raise ListError(usage)
    return CardArgs(" ".join(words), numbers, options.get("copies"), _duration(options.get("duree")),
                    options.get("vendeur", ""))


def _parse_optional(args: str, prices: int, usage: str, allowed: tuple[str, ...]) -> CardArgs:
    name, separated, rest = args.partition(SEPARATOR)
    words = name.split()
    tail = rest.split() if separated else []
    numbers: list[int | str] = []
    options: dict[str, object] = {}

    def take(word: str) -> bool:  # prix ou option, lus depuis la fin
        if len(numbers) < prices and (word.lower() == AUTO or (word.isdigit() and int(word) >= 1)):
            numbers.insert(0, AUTO if word.lower() == AUTO else int(word))
            return True
        if (option := _option(word, allowed)) and option[0] not in options:
            options[option[0]] = option[1]
            return True
        return False

    while tail:  # après le séparateur : seulement des prix et des options
        if not take(tail.pop()):
            raise ListError(usage)
    while not separated and len(words) > 1 and take(words[-1]):  # sans jamais vider le nom
        words.pop()
    if not words or options.get("copies") == 0 or (words[-1].isdigit() and int(words[-1]) == 0):
        raise ListError(usage)
    return CardArgs(" ".join(words), numbers + [None] * (prices - len(numbers)), options.get("copies"),
                    _duration(options.get("duree")), options.get("vendeur", ""), options.get("rarete", ""))


def _duration(duration) -> str:
    if duration is None:
        return ""
    if int(duration.total_seconds() // 60) not in SALE_MINUTES:
        raise ListError("Durées de vente possibles : 10m, 30m, 1h, 3h, 6h, 12h.")
    return short_duration(duration)


def parse_card_args(args: str, prices: int, usage: str) -> tuple[str, list[int], int | None]:
    """Nom, prix et exemplaires (« x2 ») seulement : voir parse_card_command."""
    parsed = parse_card_command(args, prices, usage)
    return parsed.name, parsed.prices, parsed.copies
