import json

import pytest

from wiki_seller.buying import WantedCardsError, load_wanted_cards
from wiki_seller.lists import add_safe_card, parse_want, set_wanted_card
from wiki_seller.selection import SafeCardsError, load_safe_cards


def test_add_safe_card_keeps_the_format_and_ignores_duplicates(tmp_path):
    path = tmp_path / "safed_cards.json"
    path.write_text('["Tour Eiffel"]')
    assert add_safe_card(path, "  Victor   Hugo ") == (True, 2)
    assert add_safe_card(path, "TOUR EIFFEL") == (False, 2)  # majuscules, accents et espaces ignorés
    assert json.loads(path.read_text()) == ["Tour Eiffel", "Victor Hugo"]
    assert load_safe_cards(path) == {"tour eiffel", "victor hugo"}

    path.write_text('{"cards": ["Loire"], "note": "à garder"}')
    assert add_safe_card(path, "Rhône") == (True, 2)
    assert json.loads(path.read_text()) == {"cards": ["Loire", "Rhône"], "note": "à garder"}

    with pytest.raises(SafeCardsError):
        add_safe_card(path, "   ")
    path.write_text("[,")
    with pytest.raises(SafeCardsError):
        add_safe_card(path, "Pomme")


def test_parse_want():
    assert parse_want("Tour Eiffel 500") == ("Tour Eiffel", 500, None)
    assert parse_want("Victor Hugo 120 2") == ("Victor Hugo", 120, 2)
    assert parse_want("1984 (roman) 30") == ("1984 (roman)", 30, None)  # nombre dans le nom
    for bad in ("", "Tour Eiffel", "500", "Tour Eiffel 0", "Tour Eiffel 10 0"):
        with pytest.raises(WantedCardsError):
            parse_want(bad)


def test_set_wanted_card_adds_or_updates(tmp_path):
    path = tmp_path / "wanted_cards.json"
    assert set_wanted_card(path, "Tour Eiffel", 500)[0] == "ajoutée"  # fichier absent : créé
    assert set_wanted_card(path, "Victor Hugo", 120, 2)[0] == "ajoutée"
    assert set_wanted_card(path, "tour eiffel", 650) == ("mise à jour", {"name": "Tour Eiffel", "max_price": 650})
    assert set_wanted_card(path, "Tour Eiffel", 650)[0] == "inchangée"
    assert [(c.name, c.max_price, c.copies) for c in load_wanted_cards(path)] == [
        ("Tour Eiffel", 650, 1), ("Victor Hugo", 120, 2)]
