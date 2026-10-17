import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from wiki_seller.lists import (
    BuyCard,
    ListError,
    add_safe,
    buy_cards,
    buy_file,
    complete_purchase,
    legacy_files,
    parse_card_args,
    protected_names,
    record_sale,
    remove_buy,
    remove_safe,
    remove_sell,
    safe_file,
    safe_names,
    sell_cards,
    sell_file,
    set_buy,
    set_sell,
)


@dataclass
class Cfg:
    state_dir: Path
    safe_cards_file: Path
    wanted_cards_file: Path

    @property
    def wanted_state_file(self) -> Path:
        return self.state_dir / "wanted_state.json"


@pytest.fixture
def config(tmp_path):
    return Cfg(tmp_path / "state", tmp_path / "safed_cards.json", tmp_path / "wanted_cards.json")


def test_old_files_are_taken_over_once(config):
    config.safe_cards_file.write_text('{"cards": ["Tour Eiffel", "Élysée"]}')
    config.wanted_cards_file.write_text('[{"name": " Pomme ", "max_price": 5, "copies": 3}]')
    assert legacy_files(config) == []  # pas encore repris
    assert safe_names(config) == {"tour eiffel", "elysee"}
    assert [(c.name, c.max_price, c.copies, c.key) for c in buy_cards(config)] == [("Pomme", 5, 3, "pomme")]
    assert legacy_files(config) == [config.safe_cards_file, config.wanted_cards_file]  # à supprimer
    config.safe_cards_file.write_text('["Autre"]')  # l'ancien fichier ne sert plus
    assert safe_names(config) == {"tour eiffel", "elysee"}


def test_missing_or_invalid_safe_list_is_an_error(config):
    with pytest.raises(ListError, match="/safe"):
        safe_names(config)
    safe_file(config).parent.mkdir()
    safe_file(config).write_text('{"cards": "Paris"}')
    with pytest.raises(ListError):
        safe_names(config)
    safe_file(config).write_text("[,")
    with pytest.raises(ListError):
        add_safe(config, "Pomme")


def test_add_and_remove_safe(config):
    assert add_safe(config, "  Victor   Hugo ") == (True, 1)  # première carte : liste créée
    assert add_safe(config, "Tour Eiffel") == (True, 2)
    assert add_safe(config, "VICTOR HUGO") == (False, 2)  # majuscules, accents et espaces ignorés
    assert json.loads(safe_file(config).read_text()) == ["Victor Hugo", "Tour Eiffel"]
    assert remove_safe(config, "tour eiffel") and not remove_safe(config, "Pomme")
    assert safe_names(config) == {"victor hugo"}
    with pytest.raises(ListError):
        add_safe(config, "   ")


def test_buy_list_validation(config):
    assert buy_cards(config) == []  # rien à acheter
    buy_file(config).parent.mkdir()
    for bad in ('{"name": "x"}', '[{"name": "x", "max_price": "10"}]', '[{"max_price": 10}]', "[,",
                '[{"name": "x", "max_price": 10, "copies": 0}]', '[{"name": "x", "max_price": 1, "sell_price": 0}]'):
        buy_file(config).write_text(bad)
        with pytest.raises(ListError):
            buy_cards(config)


def test_set_buy_adds_or_updates(config):
    assert set_buy(config, "Tour Eiffel", 500)[0] == "ajoutée"
    assert set_buy(config, "Victor Hugo", 120, 2, sell_price=300)[0] == "ajoutée"
    assert set_buy(config, "tour eiffel", 650) == ("mise à jour", BuyCard("Tour Eiffel", 650))
    assert set_buy(config, "Tour Eiffel", 650)[0] == "inchangée"
    assert buy_cards(config) == [BuyCard("Tour Eiffel", 650), BuyCard("Victor Hugo", 120, 2, 300)]
    assert remove_buy(config, "TOUR EIFFEL") and not remove_buy(config, "Tour Eiffel")


def test_card_bought_again_starts_from_scratch(config):
    config.wanted_state_file.parent.mkdir()
    config.wanted_state_file.write_text('{"cards": {"pomme": {"status": "won", "won": {"a": 3}}, "x": {}}}')
    set_buy(config, "Pomme", 5)
    assert json.loads(config.wanted_state_file.read_text()) == {"cards": {"x": {}}}


def test_complete_purchase_protects_or_sells(config):
    set_buy(config, "Tour Eiffel", 500)
    set_buy(config, "Victor Hugo", 120, 2, sell_price=300)
    assert "protégées" in complete_purchase(config, BuyCard("Tour Eiffel", 500), 1)
    assert "mise en vente à 300" in complete_purchase(config, BuyCard("Victor Hugo", 120, 2, 300), 2)
    assert buy_cards(config) == []
    assert safe_names(config) == {"tour eiffel"}
    assert [(c.name, c.price, c.copies) for c in sell_cards(config)] == [("Victor Hugo", 300, 2)]


def test_sell_list(config):
    status, card = set_sell(config, "Tour Eiffel", 800)
    assert status == "ajoutée" and (card.price, card.copies, card.remaining) == (800, 1, 1) and card.added_at
    status, card = set_sell(config, "tour eiffel", 900, 2)  # nouveau prix, 2 exemplaires de plus
    assert status == "mise à jour" and (card.name, card.price, card.copies) == ("Tour Eiffel", 900, 3)
    assert record_sale(config, "Tour Eiffel", "a1").remaining == 2
    assert record_sale(config, "Pomme", "a2") is None
    assert record_sale(config, "Tour Eiffel", "a3").remaining == 1
    assert sell_cards(config)[0].sold == ("a1", "a3")
    assert record_sale(config, "Tour Eiffel", "a4").remaining == 0
    assert sell_cards(config) == []  # tout est vendu : retirée de la liste
    set_sell(config, "Pomme", 3)
    assert remove_sell(config, "pomme") and sell_cards(config) == []
    sell_file(config).write_text('[{"name": "x", "price": 0}]')
    with pytest.raises(ListError):
        sell_cards(config)


def test_protected_names_gathers_the_three_lists(config):
    add_safe(config, "Tour Eiffel")
    set_buy(config, "Pomme", 5)
    set_sell(config, "Loire", 50)
    assert protected_names(config) == {"tour eiffel", "pomme", "loire"}


def test_parse_card_args():
    usage = "usage"
    assert parse_card_args("Tour Eiffel 500", 1, usage) == ("Tour Eiffel", [500], None)
    assert parse_card_args("Victor Hugo 120 x2", 1, usage) == ("Victor Hugo", [120], 2)
    assert parse_card_args("Victor Hugo x2 120", 1, usage) == ("Victor Hugo", [120], 2)
    assert parse_card_args("Apollo 11 500", 1, usage) == ("Apollo 11", [500], None)  # le nom finit par un nombre
    assert parse_card_args("Apollo 11 500 X3", 1, usage) == ("Apollo 11", [500], 3)
    assert parse_card_args("1984 (roman) 30", 1, usage) == ("1984 (roman)", [30], None)
    assert parse_card_args("X2 500", 1, usage) == ("X2", [500], None)  # le nom ressemble à « x2 »
    assert parse_card_args("Apollo 11 500 800", 2, usage) == ("Apollo 11", [500, 800], None)
    assert parse_card_args("Tour Eiffel 500 800 x2", 2, usage) == ("Tour Eiffel", [500, 800], 2)
    for bad in ("", "Tour Eiffel", "500", "Tour Eiffel 0", "Tour Eiffel 10 x0", "Tour Eiffel x2"):
        with pytest.raises(ListError, match="usage"):
            parse_card_args(bad, 1, usage)
    with pytest.raises(ListError):
        parse_card_args("Tour Eiffel 500", 2, usage)


def test_startup_reminds_to_delete_the_old_files(tmp_path, caplog):
    from wiki_seller.main import remind_legacy_files

    from .test_end_to_end import make_config

    config = make_config(tmp_path, "http://127.0.0.1:1", ["Tour Eiffel"], wanted=[])
    remind_legacy_files(config, dry_run=True)
    assert "safed_cards.json et wanted_cards.json : repris dans state/" in caplog.text
    assert safe_file(config).exists() and buy_file(config).exists()


def test_parse_card_command_options():
    from wiki_seller.lists import parse_card_command

    all_options = ("copies", "duree", "vendeur")
    parsed = parse_card_command("Tour Eiffel 500 800 x2 12h @Paul", 2, "usage", all_options)
    assert (parsed.name, parsed.prices, parsed.copies, parsed.duration, parsed.seller) == (
        "Tour Eiffel", [500, 800], 2, "12h", "Paul")
    parsed = parse_card_command("Victor Hugo 30m 120", 1, "usage", all_options)  # option avant le prix
    assert (parsed.name, parsed.prices, parsed.duration) == ("Victor Hugo", [120], "30m")
    assert parse_card_command("Apollo 11 500 1h", 1, "usage", all_options).duration == "1h"
    assert parse_card_command("12h 500", 1, "usage", all_options).name == "12h"  # le nom ressemble à une durée
    with pytest.raises(ListError, match="10m, 30m, 1h"):
        parse_card_command("Tour Eiffel 800 2h", 1, "usage", all_options)
    with pytest.raises(ListError, match="usage"):  # option non permise pour cette commande
        parse_card_command("Tour Eiffel 800 @Paul", 1, "usage", ("copies", "duree"))


def test_sale_duration_and_seller_are_kept(config):
    set_buy(config, "Tour Eiffel", 500, sell_price=800, seller="Paul", sell_duration="12h")
    assert buy_cards(config) == [BuyCard("Tour Eiffel", 500, 1, 800, "Paul", "12h")]
    message = complete_purchase(config, buy_cards(config)[0], 1)
    assert "mise en vente à 800 pour 12h" in message
    assert [(c.name, c.price, c.duration) for c in sell_cards(config)] == [("Tour Eiffel", 800, "12h")]
    set_sell(config, "Tour Eiffel", 900, duration="30m")
    assert sell_cards(config)[0].duration == "30m"
