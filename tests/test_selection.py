import json

import pytest

from wiki_seller.selection import (
    SafeCardsError,
    build_sale_plan,
    load_safe_cards,
    normalize_name,
    parse_collection,
    sale_price,
)


def entry(copy_id, card_id, title, rarity="R", **extra):
    return {"id": copy_id, "card_id": card_id, "card": {"id": card_id, "wikipedia_title": title, "rarity": rarity}, **extra}


def test_normalize_ignores_case_accents_and_spaces():
    assert normalize_name("  Élysée   Montmartre ") == normalize_name("elysee montmartre")
    assert normalize_name("L’Étranger") == normalize_name("l'etranger")


def test_load_safe_cards(tmp_path):
    path = tmp_path / "safed_cards.json"
    path.write_text(json.dumps(["Tour Eiffel", "Élysée"]), encoding="utf-8")
    assert load_safe_cards(path) == {"tour eiffel", "elysee"}
    path.write_text(json.dumps({"cards": ["Paris"]}), encoding="utf-8")
    assert load_safe_cards(path) == {"paris"}


def test_missing_or_invalid_safe_cards_file_is_an_error(tmp_path):
    with pytest.raises(SafeCardsError):
        load_safe_cards(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"cards": "Paris"}', encoding="utf-8")
    with pytest.raises(SafeCardsError):
        load_safe_cards(bad)


def test_sale_price_is_floor_of_ratio():
    assert sale_price(100, 0.7) == 70
    assert sale_price(117, 0.7) == 81  # 81.9
    assert sale_price(10, 0.7) == 7
    assert sale_price(1, 0.7) == 0


def test_parse_collection_groups_per_copy_entries():
    groups = parse_collection([entry("c1", "k1", "Paris"), entry("c2", "k1", "Paris"), entry("c1", "k1", "Paris")])
    assert len(groups) == 1 and groups[0].count == 2 and groups[0].copy_ids == ["c1", "c2"]


def test_parse_collection_uses_declared_count():
    groups = parse_collection([entry("c1", "k1", "Paris", count=3)])
    assert groups[0].count == 3


def test_parse_collection_excludes_copies_in_trade():
    groups = parse_collection([entry("c1", "k1", "Paris"), entry("c2", "k1", "Paris")], excluded_copy_ids={"c2"})
    assert groups[0].count == 1


def test_plan_sorts_by_price_and_keeps_one_protected_copy():
    groups = parse_collection([
        entry("a1", "A", "Cheap"),
        entry("b1", "B", "Château de Versailles"), entry("b2", "B", "Château de Versailles"),
        entry("c1", "C", "Tour Eiffel"),
        entry("d1", "D", "Expensive"),
        entry("e1", "E", "No price"),
    ])
    averages = {"A": 10, "B": 500, "C": 900, "D": 300, "E": None}
    plan = build_sale_plan(groups, {normalize_name("chateau de versailles"), normalize_name("TOUR EIFFEL")},
                           averages, [], 0.7)
    assert [(i.title, i.copies_to_sell, i.price) for i in plan.items] == [
        ("Château de Versailles", 1, 350),  # protégée : 2 exemplaires, on en garde 1
        ("Expensive", 1, 210),
        ("Cheap", 1, 7),
    ]
    assert plan.skipped == [("No price", "pas de prix moyen")]


def test_plan_counts_copies_already_on_auction():
    groups = parse_collection([entry("b1", "B", "Protégée"), entry("b2", "B", "Protégée"), entry("x1", "X", "Libre")])
    selling = [{"card_id": "b2", "status": "active"}, {"card_id": "x1", "status": "active"}]
    plan = build_sale_plan(groups, {"protegee"}, {"B": 100, "X": 100}, selling, 0.7)
    assert plan.items == []  # b2 déjà en vente => il ne reste que l'exemplaire à garder


def test_cards_with_unknown_api_price_come_last():
    groups = parse_collection([entry("a1", "A", "Zèbre"), entry("b1", "B", "Abeille"), entry("c1", "C", "Connue")])
    plan = build_sale_plan(groups, set(), {"C": 50}, [], 0.7, unknown_prices={"A", "B"})
    assert [(i.title, i.price) for i in plan.items] == [("Connue", 35), ("Abeille", None), ("Zèbre", None)]


def test_adjust_to_market():
    from wiki_seller.selection import adjust_to_market, competitor_price

    # Sous la moyenne des enchères en cours : + un écart type.
    up = adjust_to_market(228, 326, [300, 320, 340], 0.5)
    assert up.price == 244 and up.competitors == 3 and round(up.stdev) == 16
    # Au-dessus : - un écart type.
    assert adjust_to_market(228, 326, [100, 120], 0.5).price == 218
    # Jamais sous 50 % du prix moyen.
    assert adjust_to_market(228, 326, [100, 300], 0.5).price == 163
    # Moins de deux enchères : inchangé.
    assert adjust_to_market(228, 326, [5], 0.5).price == 228
    assert adjust_to_market(228, 326, [], 0.5).price == 228
    # Prix d'une enchère : mise actuelle, sinon mise de départ.
    assert competitor_price({"current_bid": 40, "base_amount": 10}) == 40
    assert competitor_price({"current_bid": None, "base_amount": 10}) == 10
    assert competitor_price({}) is None
