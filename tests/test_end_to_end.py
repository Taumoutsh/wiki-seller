"""Parcours complet avec un vrai Chromium contre le faux site local (tests/fake_site.py)."""

import json
import os

import pytest

from wiki_seller.config import Config
from wiki_seller.main import run_once
from wiki_seller.site import ListingStatus

from .fake_site import FakeWikiMasters


def entry(copy_id, card_id, title, rarity="R"):
    return {"id": copy_id, "card_id": card_id, "card": {"id": card_id, "wikipedia_title": title, "rarity": rarity}}


COLLECTION = [
    entry("a1", "A", "Pomme"),
    entry("b1", "B", "Château de Versailles"), entry("b2", "B", "Château de Versailles"),
    entry("c1", "C", "Tour Eiffel"),
    entry("d1", "D", "Victor Hugo"), entry("d2", "D", "Victor Hugo"),
    entry("e1", "E", "Sans Prix"),
    entry("f1", "F", "Loire"),
    entry("g1", "G", "Rhône"),
]
API_AVERAGES = {"A": 20, "B": 800, "C": 5000, "D": 300, "F": 150, "G": 100}
# Valeurs affichées dans la fenêtre (le script doit utiliser celles-ci pour le prix).
UI_AVERAGES = {"Pomme": 21, "Château de Versailles": 1234, "Tour Eiffel": 5000, "Victor Hugo": 301,
               "Loire": 150, "Rhône": 100}


def make_config(tmp_path, url, safe):
    safe_file = tmp_path / "safed_cards.json"
    safe_file.write_text(json.dumps(safe), encoding="utf-8")
    return Config(
        mail="me@example.com", password="secret", base_url=url, safe_cards_file=safe_file,
        state_dir=tmp_path / "state", price_ratio=0.7, auction_duration_label="1 h",
        headless=True, chromium_executable=os.getenv("CHROMIUM_EXECUTABLE") or None,
    )


def test_full_run_lists_most_expensive_first(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=5) as site:
        config = make_config(tmp_path, site.url, ["tour eiffel", "CHATEAU DE VERSAILLES"])
        result = run_once(config)

    assert result.listed == 5 and result.last_listing_at is not None
    assert [(l["title"], l["price"], l["duration"]) for l in site.listings] == [
        ("Château de Versailles", "863", "1 h"),  # 2 exemplaires, protégée => 1 vendu ; 1234 * 0.7
        ("Victor Hugo", "210", "1 h"),            # 301 * 0.7 = 210.7
        ("Victor Hugo", "210", "1 h"),
        ("Loire", "105", "1 h"),
        ("Rhône", "70", "1 h"),
    ]
    assert site.logins == 1


def test_second_run_reuses_session_and_respects_slots(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=5) as site:
        config = make_config(tmp_path, site.url, [])
        site.selling = [{"id": f"x{i}", "card_id": "zz", "status": "active", "end_at": "2030-01-01T10:00:00Z"}
                        for i in range(3)]
        first = run_once(config)
        assert first.listed == 2
        second = run_once(config)
    assert second.listed == 0 and second.slots_full
    assert site.logins == 1  # la session sauvegardée a été réutilisée
    assert [l["title"] for l in site.listings] == ["Tour Eiffel", "Château de Versailles"]


def test_dry_run_does_not_launch(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        result = run_once(config, dry_run=True)
    assert result.listed == 5 and result.last_listing_at is None
    assert site.listings == []


def test_card_without_ui_average_is_skipped(tmp_path):
    ui = dict(UI_AVERAGES)
    del ui["Tour Eiffel"]  # la fenêtre affiche « Indisponible »
    with FakeWikiMasters(COLLECTION, API_AVERAGES, ui, max_auctions=2) as site:
        config = make_config(tmp_path, site.url, [])
        run_once(config)
    assert [l["title"] for l in site.listings] == ["Château de Versailles", "Château de Versailles"]


def test_price_api_refused_falls_back_to_window_price(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=2, sales_forbidden=True) as site:
        config = make_config(tmp_path, site.url, [])
        result = run_once(config)
    assert result.listed == 2
    # Sans classement possible : ordre alphabétique, prix lu dans la fenêtre.
    assert [(l["title"], l["price"]) for l in site.listings] == [
        ("Château de Versailles", "863"), ("Château de Versailles", "863")]
