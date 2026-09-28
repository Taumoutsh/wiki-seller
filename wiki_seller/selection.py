"""Logique pure (sans navigateur) : lecture de la liste protégée, regroupement de la
collection, calcul des prix et ordre de mise en vente."""

import json
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

# Nombre d'exemplaires conservés pour une carte présente dans safed_cards.json.
PROTECTED_COPIES_TO_KEEP = 1


class SafeCardsError(Exception):
    pass


def normalize_name(name: str) -> str:
    """Nom comparable : sans accents, sans casse, espaces et apostrophes uniformisés."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    text = re.sub(r"\s+", " ", text)
    return text.casefold().strip()


def load_safe_cards(path: Path) -> set[str]:
    """Charge safed_cards.json : une liste JSON de noms, ou {"cards": [...]}.

    L'absence du fichier est une erreur : sans lui, le script mettrait toute la
    collection en vente."""
    if not path.exists():
        raise SafeCardsError(
            f"{path} introuvable. Copiez safed_cards.example.json en {path} "
            "(une liste vide [] vend toutes les cartes)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SafeCardsError(f"{path} n'est pas un JSON valide : {exc}") from exc
    if isinstance(data, dict):
        data = data.get("cards")
    if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
        raise SafeCardsError(f"{path} doit contenir une liste de noms de cartes, ex. [\"Tour Eiffel\"].")
    return {normalize_name(n) for n in data if n.strip()}


@dataclass
class CardGroup:
    """Tous les exemplaires possédés d'une même carte."""

    card_id: str
    title: str
    rarity: str | None
    copy_ids: list[str] = field(default_factory=list)
    count: int = 0


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def parse_collection(entries: list[dict], excluded_copy_ids: set[str] = frozenset()) -> list[CardGroup]:
    """Regroupe les entrées de /api/my-collection par carte.

    Selon les versions du site, une entrée représente soit un exemplaire, soit une
    carte avec un champ `count` : on gère les deux."""
    groups: dict[str, CardGroup] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        card = entry.get("card") if isinstance(entry.get("card"), dict) else entry
        card_id = _str(card.get("id")) or _str(entry.get("card_id"))
        title = _str(card.get("wikipedia_title")) or _str(card.get("title")) or _str(card.get("name"))
        copy_id = _str(entry.get("id")) if entry is not card else ""
        if not card_id or not title:
            continue
        if copy_id and copy_id in excluded_copy_ids:
            continue
        rarity = _str(card.get("rarity")).upper() or _str(entry.get("rarity")).upper() or None

        group = groups.get(card_id)
        if group is None:
            group = groups[card_id] = CardGroup(card_id=card_id, title=title, rarity=rarity)
        try:
            declared = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            declared = 0
        if copy_id:
            if copy_id in group.copy_ids:  # même exemplaire vu deux fois (pagination)
                continue
            group.copy_ids.append(copy_id)
            group.count = max(group.count, declared, len(group.copy_ids))
        else:
            group.count += declared or 1
    return list(groups.values())


def count_listed(group: CardGroup, selling: list[dict]) -> int:
    """Nombre d'enchères en cours portant sur un exemplaire de cette carte."""
    ids = {group.card_id, *group.copy_ids}
    total = 0
    for auction in selling:
        if not isinstance(auction, dict):
            continue
        card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
        refs = {_str(auction.get("card_id")), _str(auction.get("user_card_id")), _str(card.get("id"))}
        if refs & ids:
            total += 1
    return total


def sale_price(average: float, ratio: float) -> int:
    """Prix de mise en vente : ratio du prix moyen, arrondi à l'entier inférieur."""
    return math.floor(Decimal(str(average)) * Decimal(str(ratio)))


@dataclass
class SaleItem:
    card_id: str
    title: str
    rarity: str | None
    average: float | None  # None : prix inconnu côté API, lu dans la fenêtre d'enchère
    price: int | None
    copies_to_sell: int
    copy_ids: tuple[str, ...] = ()  # exemplaires possédés (secours si le site attend leur id)


@dataclass
class SalePlan:
    items: list[SaleItem]
    skipped: list[tuple[str, str]]  # (titre, raison)


def copies_available(group: CardGroup, safe_names: set[str], selling: list[dict]) -> int:
    keep = PROTECTED_COPIES_TO_KEEP if normalize_name(group.title) in safe_names else 0
    return max(0, group.count - count_listed(group, selling) - keep)


def build_sale_plan(
    groups: list[CardGroup],
    safe_names: set[str],
    averages: dict[str, float | None],
    selling: list[dict],
    ratio: float,
    unknown_prices: set[str] = frozenset(),
) -> SalePlan:
    """Liste des cartes à vendre, de la plus chère à la moins chère.

    Les cartes dont l'API n'a pas donné le prix (`unknown_prices`) passent en dernier :
    leur prix sera lu dans la fenêtre d'enchère."""
    items: list[SaleItem] = []
    unknown: list[SaleItem] = []
    skipped: list[tuple[str, str]] = []
    for group in groups:
        copies = copies_available(group, safe_names, selling)
        if copies <= 0:
            continue
        if group.card_id in unknown_prices:
            unknown.append(SaleItem(group.card_id, group.title, group.rarity, None, None, copies,
                                    tuple(group.copy_ids)))
            continue
        average = averages.get(group.card_id)
        if not average or average <= 0:
            skipped.append((group.title, "pas de prix moyen"))
            continue
        price = sale_price(average, ratio)
        if price < 1:
            skipped.append((group.title, f"prix calculé nul (moyenne {average})"))
            continue
        items.append(SaleItem(group.card_id, group.title, group.rarity, average, price, copies,
                              tuple(group.copy_ids)))
    items.sort(key=lambda i: (-i.average, normalize_name(i.title)))
    unknown.sort(key=lambda i: normalize_name(i.title))
    return SalePlan(items=items + unknown, skipped=skipped)


@dataclass
class PriceAdjustment:
    price: int
    base: int
    competitors: int
    mean: float | None = None
    stdev: float | None = None


def competitor_price(auction: dict) -> int | None:
    """Prix d'une enchère en cours : la mise actuelle, sinon la mise de départ."""
    for key in ("current_bid", "base_amount", "effective_bid"):
        value = auction.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


def adjust_to_market(base: int, average: float, prices: list[int], floor_ratio: float) -> PriceAdjustment:
    """Ajuste le prix de mise en vente d'après les enchères en cours de la même carte :
    sous leur moyenne, on ajoute un écart type ; au-dessus, on en retire un. Moins de
    deux enchères : prix inchangé. Jamais sous floor_ratio × prix moyen."""
    if len(prices) < 2:
        return PriceAdjustment(base, base, len(prices))
    mean = statistics.fmean(prices)
    stdev = statistics.pstdev(prices)
    if base < mean:
        price = base + stdev
    elif base > mean:
        price = base - stdev
    else:
        price = base
    floor = math.floor(Decimal(str(average)) * Decimal(str(floor_ratio)))
    price = max(math.floor(price), floor, 1)
    return PriceAdjustment(price, base, len(prices), mean, stdev)
