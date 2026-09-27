"""Parcours complet avec un vrai Chromium contre le faux site local (tests/fake_site.py)."""

import json
import os

import pytest

from wiki_seller.config import Config
from wiki_seller.main import run_once, run_snipe
from wiki_seller.site import ListingStatus, SiteError

from .fake_site import ME, FakeWikiMasters, market_auction


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


def make_config(tmp_path, url, safe, wanted=None, **extra):
    safe_file = tmp_path / "safed_cards.json"
    safe_file.write_text(json.dumps(safe), encoding="utf-8")
    wanted_file = tmp_path / "wanted_cards.json"
    if wanted is not None:
        wanted_file.write_text(json.dumps(wanted), encoding="utf-8")
    extra = {"packs_hours": None, "wanted_cards_file": wanted_file, **extra}
    return Config(
        mail="me@example.com", password="secret", base_url=url, safe_cards_file=safe_file,
        state_dir=tmp_path / "state", price_ratio=0.7, auction_duration_label="1 h",
        headless=True, chromium_executable=os.getenv("CHROMIUM_EXECUTABLE") or None, **extra,
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
    assert site.logins == 1 and site.signups == 0


def test_login_blocked_by_antibot_check_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr("wiki_seller.site.LOGIN_WAIT_HEADLESS", 2)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, challenge="manual") as site:
        config = make_config(tmp_path, site.url, [])
        with pytest.raises(SiteError, match="anti-bot"):
            run_once(config)
    assert site.logins == 0 and site.signups == 0 and site.listings == []


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


def test_only_selected_rarities_are_loaded_and_sold(tmp_path):
    collection = [entry("a1", "A", "Pomme", "SR"), entry("b1", "B", "Château de Versailles", "C"),
                  entry("c1", "C", "Tour Eiffel", "L"), entry("f1", "F", "Loire", "UR")]
    with FakeWikiMasters(collection, API_AVERAGES, UI_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [], sell_rarities=("L", "SR"))
        result = run_once(config, dry_run=True)
    assert result.listed == 2
    # Le script demande la collection au site rareté par rareté, sans parcourir le reste
    # (les autres requêtes viennent de la page /collection elle-même).
    rarities = {q.split("rarity=")[1] for q in site.collection_requests if "rarity=" in q}
    assert rarities == {"L", "SR"}


def test_session_cookies_skip_the_login_page(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, challenge="manual") as site:
        config = make_config(tmp_path, site.url, [], session_cookies="other=1; session=ok")
        result = run_once(config, dry_run=True)
    assert result.listed == 5 and site.logins == 0 and site.signups == 0


def test_packs_are_opened_during_the_night_window(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3) as site:
        config = make_config(tmp_path, site.url, [], packs_hours=(0, 24))
        run_once(config, dry_run=False)
    assert site.packs_opened == 3 and site.packs == 0


def test_packs_are_not_opened_in_dry_run_or_outside_the_window(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=2) as site:
        run_once(make_config(tmp_path, site.url, [], packs_hours=(0, 24)), dry_run=True)
        run_once(make_config(tmp_path, site.url, [], packs_hours=None), dry_run=True)
    assert site.packs_opened == 0


def eiffel_auctions():
    return [market_auction("e1", "Gustave Eiffel", 200), market_auction("e2", "Gustave Eiffel", 150),
            market_auction("e3", "Gustave Eiffel (homonymie)", 10),
            market_auction("e4", "Gustave Eiffel", 100, seller=ME)]


def test_buys_the_cheapest_matching_auction_and_rebids_within_limit(tmp_path):
    wanted = [{"name": "gustave eiffel", "max_price": 300}]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=wanted)
        run_once(config)
        assert site.bids == [("e2", 150)]  # la moins chère, ni l'homonyme ni la nôtre
        state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
        assert state["status"] == "bidding" and state["auction_id"] == "e2" and state["end_at"]

        run_once(config)  # on mène déjà : pas de nouvelle mise
        assert len(site.bids) == 1

        site.outbid("e2", 250)
        run_snipe(config)  # la fin est loin : la surenchère attend la passe normale
        assert len(site.bids) == 1
        run_once(config)
        assert site.bids[-1] == ("e2", 260)

        site.outbid("e2", 295)  # prochaine mise 305 > 300 : on s'arrête là
        run_once(config)
        assert site.bids[-1] == ("e2", 260)

        # Le rival se retire (enchère annulée de son côté) : on remporte à 260.
        site.auctions["e2"].update(current_bid=260, current_bidder_id=ME)
        site.settle("e2")
        run_once(config)
    state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
    assert state["status"] == "won"


def test_buying_dry_run_places_no_bid(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        run_once(config, dry_run=True)
    assert site.bids == []


def test_snipe_bids_just_before_the_end(tmp_path):
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat()
    auction = market_auction("s1", "Victor Hugo", 50, end_at=end)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=[auction]) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Victor Hugo", "max_price": 100}],
                             snipe_lead=timedelta(seconds=25))
        run_once(config)
        assert site.bids == [("s1", 50)]
        site.outbid("s1", 70)
        run_snipe(config)
        # La surenchère n'est partie qu'une fois arrivé à 25 s de la fin.
        assert site.bids[-1] == ("s1", 80)
        assert datetime.now(timezone.utc) >= datetime.fromisoformat(end) - timedelta(seconds=26)


def test_card_with_parentheses_is_found_without_them(tmp_path):
    auctions = [market_auction("d1", "Dewey Martin (acteur)", 1), market_auction("d2", "Dewey Martin (homonymie)", 1)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=auctions) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Dewey Martin (acteur)", "max_price": 5}])
        run_once(config)
    assert site.bids == [("d1", 1)]
