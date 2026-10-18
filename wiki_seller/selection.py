"""Logique pure (sans navigateur) : regroupement de la collection, calcul des prix et
ordre de mise en vente."""

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

# Nombre d'exemplaires conservés pour une carte protégée (state/safe_cards.json).
PROTECTED_COPIES_TO_KEEP = 1
# Vente automatique : une carte dont le prix repose sur moins de MIN_SALES_TO_SELL ventes
# (2 au plus : ventes isolées, peut-être des échanges d'argent) est mise de côté.
MIN_SALES_TO_SELL = 3


def normalize_name(name: str) -> str:
    """Nom comparable : sans accents, sans casse, espaces et apostrophes uniformisés."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    text = re.sub(r"\s+", " ", text)
    return text.casefold().strip()


@dataclass
class CardGroup:
    """Tous les exemplaires possédés d'une même carte, dans une même rareté (une carte
    peut exister en plusieurs raretés, à des prix très différents)."""

    card_id: str
    title: str
    rarity: str | None
    copy_ids: list[str] = field(default_factory=list)
    count: int = 0

    @property
    def key(self) -> str:
        """Carte et rareté : clé des prix (state/price_cache.json)."""
        return f"{self.card_id}:{self.rarity}"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def parse_collection(entries: list[dict], excluded_copy_ids: set[str] = frozenset()) -> list[CardGroup]:
    """Regroupe les entrées de /api/my-collection par carte.

    Selon les versions du site, une entrée représente soit un exemplaire, soit une
    carte avec un champ `count` : on gère les deux."""
    groups: dict[tuple[str, str | None], CardGroup] = {}
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

        group = groups.get((card_id, rarity))
        if group is None:
            group = groups[(card_id, rarity)] = CardGroup(card_id=card_id, title=title, rarity=rarity)
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


def auction_refs(auction: dict) -> set[str]:
    """Identifiants (carte ou exemplaire) auxquels une enchère en cours fait référence."""
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    return {r for r in (_str(auction.get("card_id")), _str(auction.get("user_card_id")), _str(card.get("id"))) if r}


def _rarity(auction: dict) -> str:
    card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
    return _str(auction.get("snapshot_rarity") or card.get("rarity")).upper()


def count_listed(group: CardGroup, selling: list[dict]) -> int:
    """Nombre d'enchères en cours portant sur un exemplaire de cette carte (de cette
    rareté, quand l'enchère la donne)."""
    ids = {group.card_id, *group.copy_ids}
    return sum(1 for a in selling if isinstance(a, dict) and auction_refs(a) & ids
               and (not group.rarity or _rarity(a) in ("", group.rarity)))


def copies_not_on_sale(group: CardGroup, selling: list[dict]) -> list[str]:
    """Exemplaires de la carte qui ne sont dans aucune enchère en cours."""
    listed = set().union(*(auction_refs(a) for a in selling if isinstance(a, dict)))
    return [c for c in group.copy_ids if c not in listed]


def sale_price(average: float, ratio: float) -> int:
    """Prix de mise en vente : ratio du prix moyen, arrondi à l'entier inférieur."""
    return math.floor(Decimal(str(average)) * Decimal(str(ratio)))


@dataclass
class SaleItem:
    card_id: str
    title: str
    rarity: str | None
    average: float
    price: int
    copies_to_sell: int
    copy_ids: tuple[str, ...] = ()  # exemplaires possédés (secours si le site attend leur id)
    exact: bool = False  # prix demandé (/sell) : ni ratio, ni ajustement au marché
    duration: str = ""  # durée demandée (/sell … 12h) ; vide = durée habituelle


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
    sales_counts: dict[str, int | None] | None = None,
) -> SalePlan:
    """Liste des cartes à vendre, de la plus chère à la moins chère. Prix par carte et
    rareté (CardGroup.key) ; les cartes dont le prix n'a pas pu être lu (`unknown_prices`)
    sont laissées pour la passe suivante."""
    items: list[SaleItem] = []
    skipped: list[tuple[str, str]] = []
    for group in groups:
        copies = copies_available(group, safe_names, selling)
        if copies <= 0:
            continue
        if group.key in unknown_prices:
            skipped.append((group.title, "prix illisible pour l'instant"))
            continue
        average = averages.get(group.key)
        if not average or average <= 0:
            skipped.append((group.title, "pas de prix moyen"))
            continue
        count = (sales_counts or {}).get(group.key)
        if count is not None and count < MIN_SALES_TO_SELL:
            sales = f"{count} vente{'s' if count > 1 else ''} connue{'s' if count > 1 else ''}"
            skipped.append((group.title, f"mise de côté : {sales}, prix {average:g} peu fiable"))
            continue
        price = sale_price(average, ratio)
        if price < 1:
            skipped.append((group.title, f"prix calculé nul (moyenne {average})"))
            continue
        items.append(SaleItem(group.card_id, group.title, group.rarity, average, price, copies,
                              tuple(group.copy_ids)))
    items.sort(key=lambda i: (-i.average, normalize_name(i.title)))
    return SalePlan(items=items, skipped=skipped)


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
