"""Parcours complet avec un vrai Chromium contre le faux site local (tests/fake_site.py)."""

import json
import os

import pytest

from wiki_seller.config import Config
from wiki_seller.scheduler import run_once, run_snipe
from wiki_seller.site import SiteError

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


def make_config(tmp_path, url, safe, wanted=None, **extra):
    safe_file = tmp_path / "safed_cards.json"
    safe_file.write_text(json.dumps(safe), encoding="utf-8")
    wanted_file = tmp_path / "wanted_cards.json"
    if wanted is not None:
        wanted_file.write_text(json.dumps(wanted), encoding="utf-8")
    extra = {"wanted_cards_file": wanted_file, **extra}
    return Config(
        mail="me@example.com", password="secret", base_url=url, safe_cards_file=safe_file,
        state_dir=tmp_path / "state", price_ratio=0.7, auction_duration_label="1 h",
        headless=True, chromium_executable=os.getenv("CHROMIUM_EXECUTABLE") or None, **extra,
    )


def test_full_run_lists_most_expensive_first(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=5) as site:
        config = make_config(tmp_path, site.url, ["tour eiffel", "CHATEAU DE VERSAILLES"])
        result = run_once(config)

    assert result.listed == 5 and result.last_listing_at is not None
    assert [(l["title"], l["price"], l["duration"]) for l in site.listings] == [
        ("Château de Versailles", "560", "1 h"),  # 2 exemplaires, protégée => 1 vendu ; 800 * 0.7
        ("Victor Hugo", "210", "1 h"),
        ("Victor Hugo", "210", "1 h"),
        ("Loire", "105", "1 h"),
        ("Rhône", "70", "1 h"),
    ]
    assert site.logins == 1


def test_login_blocked_by_antibot_check_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr("wiki_seller.session.LOGIN_WAIT_HEADLESS", 2)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, challenge="manual") as site:
        config = make_config(tmp_path, site.url, [])
        with pytest.raises(SiteError, match="anti-bot"):
            run_once(config)
    assert site.logins == 0 and site.listings == []


def test_second_run_reuses_session_and_respects_slots(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=5) as site:
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        result = run_once(config, dry_run=True)
    assert result.listed == 5 and result.last_listing_at is None
    assert site.listings == []


def test_cards_without_readable_price_wait_for_the_next_pass(tmp_path, caplog):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=2, sales_forbidden=True) as site:
        with caplog.at_level("INFO"):
            result = run_once(make_config(tmp_path, site.url, []))
    assert result.listed == 0 and site.listings == []
    assert "prix illisible pour l'instant" in caplog.text

def test_only_selected_rarities_are_loaded_and_sold(tmp_path):
    collection = [entry("a1", "A", "Pomme", "SR"), entry("b1", "B", "Château de Versailles", "C"),
                  entry("c1", "C", "Tour Eiffel", "L"), entry("f1", "F", "Loire", "UR")]
    with FakeWikiMasters(collection, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [], sell_rarities=("L", "SR"))
        result = run_once(config, dry_run=True)
    assert result.listed == 2
    # Le script demande la collection au site rareté par rareté, sans parcourir le reste
    # (les autres requêtes viennent de la page /collection elle-même).
    rarities = {q.split("rarity=")[1] for q in site.collection_requests if "rarity=" in q}
    assert rarities == {"L", "SR"}


def test_session_cookies_skip_the_login_page(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, challenge="manual") as site:
        config = make_config(tmp_path, site.url, [], session_cookies="other=1; session=ok")
        result = run_once(config, dry_run=True)
    assert result.listed == 5 and site.logins == 0


def eiffel_auctions():
    return [market_auction("e1", "Gustave Eiffel", 200), market_auction("e2", "Gustave Eiffel", 150),
            market_auction("e3", "Gustave Eiffel (homonymie)", 10),
            market_auction("e4", "Gustave Eiffel", 100, seller=ME)]


def test_bids_once_then_rebids_only_at_the_end_or_moves_on(tmp_path):
    from datetime import datetime, timedelta, timezone

    wanted = [{"name": "gustave eiffel", "max_price": 300}]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=wanted, snipe_lead=timedelta(seconds=25))
        run_once(config)
        assert site.bids == [("e2", 150)]  # la moins chère, ni l'homonyme ni la nôtre

        site.outbid("e2", 250)
        run_once(config)  # dépassé mais dans la limite : pas de surenchère pendant une passe
        assert len(site.bids) == 1
        state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
        assert state["status"] == "bidding" and state["auction_id"] == "e2"

        # Réveil de fin d'enchère : surenchère, dans la limite.
        site.auctions["e2"]["end_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        run_snipe(config)
        assert site.bids[-1] == ("e2", 260)

        # Dépassé au-delà de la limite : abandon, et mise sur une autre enchère de la carte.
        site.auctions["e2"]["end_at"] = "2030-01-01T10:00:00+00:00"
        site.outbid("e2", 295)
        run_once(config)
        assert site.bids[-1] == ("e1", 200)
        state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
        assert state["auction_id"] == "e1"

        site.settle("e1")  # gagnée
        run_once(config)
    state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
    assert state["status"] == "won" and state["won"] == {"e1": 200}


def test_copies_buys_several_one_after_the_other(tmp_path):
    wanted = [{"name": "Gustave Eiffel", "max_price": 300, "copies": 2}]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=wanted)
        run_once(config)
        assert site.bids == [("e2", 150)]
        site.settle("e2")
        run_once(config)  # 1/2 : on passe à une autre enchère
        assert site.bids[-1] == ("e1", 200)
        state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
        assert state["status"] == "bidding" and list(state["won"]) == ["e2"]
        site.settle("e1")
        run_once(config)
    state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
    assert state["status"] == "won" and set(state["won"]) == {"e1", "e2"} and len(site.bids) == 2


def test_old_won_state_is_kept(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        config.wanted_state_file.parent.mkdir(parents=True, exist_ok=True)
        config.wanted_state_file.write_text(json.dumps({"my_id": ME, "cards": {"gustave eiffel": {
            "name": "Gustave Eiffel", "status": "won", "auction_id": "old", "price": 1}}}))
        run_once(config)
    assert site.bids == []


def test_buying_dry_run_places_no_bid(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        run_once(config, dry_run=True)
    assert site.bids == []


def test_snipe_bids_just_before_the_end(tmp_path):
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat()
    auction = market_auction("s1", "Victor Hugo", 50, end_at=end)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=[auction]) as site:
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=auctions) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Dewey Martin (acteur)", "max_price": 5}])
        run_once(config)
    assert site.bids == [("d1", 1)]


def test_listing_sends_copy_ids_and_api_average(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=3) as site:
        config = make_config(tmp_path, site.url, [])
        result = run_once(config)
    assert result.listed == 3
    # Prix moyen de l'API (et non celui de la fenêtre), par l'identifiant d'un exemplaire.
    assert site.api_listings == [
        {"card_id": "c1", "base_amount": 3500, "duration_minutes": 60},
        {"card_id": "b1", "base_amount": 560, "duration_minutes": 60},
        {"card_id": "b2", "base_amount": 560, "duration_minutes": 60},  # l'autre exemplaire
    ]


def test_listing_falls_back_to_card_id(tmp_path, caplog):
    # Si le site attendait l'identifiant de la carte (409 avec celui de l'exemplaire).
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1, listing_id="card") as site:
        with caplog.at_level("INFO"):
            run_once(make_config(tmp_path, site.url, []))
    assert [l["card_id"] for l in site.api_listings] == ["C"]
    assert "identifiant de la carte" in caplog.text


def test_no_second_bid_while_leading_another_auction_of_the_card(tmp_path):
    auctions = eiffel_auctions()
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=auctions) as site:
        # Vous menez déjà e1 (mise à la main, ou suivi perdu) ; le suivi pointe ailleurs.
        site.bids.append(("e1", 200))
        site.auctions["e1"].update(current_bid=200, current_bidder_id=ME)
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        config.wanted_state_file.parent.mkdir(parents=True, exist_ok=True)
        config.wanted_state_file.write_text(json.dumps(
            {"my_id": ME, "cards": {"gustave eiffel": {"name": "Gustave Eiffel", "status": "searching"}}}))
        run_once(config)
    assert site.bids == [("e1", 200)]  # aucune mise sur e2, pourtant moins chère
    state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
    assert state["auction_id"] == "e1" and state["status"] == "bidding"


def test_card_won_elsewhere_is_not_bought_again(tmp_path):
    from datetime import datetime, timedelta, timezone

    auctions = eiffel_auctions()
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=auctions) as site:
        site.auctions["e1"].update(status="settled_sold", winner_id=ME, final_price=210,
                                   settled_at=datetime.now(timezone.utc).isoformat())
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        config.wanted_state_file.parent.mkdir(parents=True, exist_ok=True)
        config.wanted_state_file.write_text(json.dumps(
            {"my_id": ME, "cards": {"gustave eiffel": {"name": "Gustave Eiffel", "since": since}}}))
        run_once(config)
    assert site.bids == []
    state = json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel"]
    assert state["status"] == "won" and state["won"] == {"e1": 210}


def test_sale_price_follows_current_auctions_of_the_card(tmp_path, caplog):
    rivals = [market_auction("t1", "Tour Eiffel", 3000), market_auction("t2", "Tour Eiffel", 2000, current=4000),
              market_auction("t3", "Tour Eiffel (homonymie)", 1)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1, auctions=rivals) as site:
        config = make_config(tmp_path, site.url, [], market_adjust=True)
        with caplog.at_level("INFO"):
            run_once(config)
    # 70 % de 5000 = 3500, moyenne des enchères 3500 (3000 et 4000, pas l'homonyme) :
    # égal, donc inchangé. Avec une enchère de plus à 1000, voir le test suivant.
    assert site.api_listings[0]["base_amount"] == 3500
    assert "Tour Eiffel : prix 3500 → 3500 (2 enchère(s) en cours" in caplog.text


def test_sale_price_lowered_when_above_current_auctions(tmp_path):
    rivals = [market_auction("t1", "Tour Eiffel", 3000), market_auction("t2", "Tour Eiffel", 2000)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1, auctions=rivals) as site:
        run_once(make_config(tmp_path, site.url, [], market_adjust=True))
    # 3500 au-dessus de la moyenne 2500 : moins l'écart type 500.
    assert site.api_listings[0]["base_amount"] == 3000


def test_snipe_rebids_fast_with_learned_step(tmp_path, caplog):
    from datetime import datetime, timedelta, timezone

    auction = market_auction("s1", "Victor Hugo", 50)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=0, auctions=[auction]) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Victor Hugo", "max_price": 100}],
                             snipe_lead=timedelta(seconds=6))
        run_once(config)
        assert site.bids == [("s1", 50)]
        site.outbid("s1", 70)
        end = datetime.now(timezone.utc) + timedelta(seconds=14)
        site.auctions["s1"]["end_at"] = end.isoformat()
        with caplog.at_level("INFO"):
            run_snipe(config)
    # Surenchère au pas du site (70 + 10), partie dans les 6 dernières secondes.
    assert site.bids[-1] == ("s1", 80)
    assert "surenchère de 80 sur « Victor Hugo »" in caplog.text


def test_night_sale_duration(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1) as site:
        run_once(make_config(tmp_path, site.url, [], night_durations=((0, 24, "12 h"),)))
    assert site.api_listings[0]["duration_minutes"] == 720


def test_unsold_card_is_set_aside_then_retried(tmp_path):
    from datetime import datetime, timedelta, timezone

    unsold = [{"id": f"h{i}", "seller_id": ME, "status": "expired", "winner_id": None, "final_price": None,
               "card": {"id": "C", "wikipedia_title": "Tour Eiffel"}} for i in range(3)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1) as site:
        site.history = unsold
        config = make_config(tmp_path, site.url, [])
        run_once(config)
        # Tour Eiffel (la plus chère) est invendue 3 fois : c'est Château de Versailles qui part.
        assert site.api_listings[0]["card_id"] == "b1"
        state = json.loads(config.unsold_file.read_text())
        until = datetime.fromisoformat(state["cards"]["C"]["until"])
        assert timedelta(days=4, hours=23) < until - datetime.now(timezone.utc) <= timedelta(days=5)

        # Délai écoulé : elle revient dans l'ordre de vente.
        state["cards"]["C"]["until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        config.unsold_file.write_text(json.dumps(state))
        site.selling.clear()
        run_once(config)
    assert site.api_listings[-1]["card_id"] == "c1"


# ─────────────────────────── Défausse ───────────────────────────

DISCARD_COLLECTION = [
    entry("a1", "A", "Caillou", "C"), entry("a2", "A", "Caillou", "C"),
    entry("b1", "B", "Feuille", "PC"),
    entry("c1", "C", "Rare sans prix", "R"),
    entry("d1", "D", "Presque", "R"),
    entry("e1", "E", "Pile quinze", "R"),
    entry("f1", "F", "Tour Eiffel", "C"),
    entry("g1", "G", "Victor Hugo", "C"),
]
DISCARD_AVERAGES = {"A": 5, "D": 14, "E": 15, "F": 2, "G": 3}
FULL_SLOTS = [{"id": f"x{i}", "card_id": "zz", "status": "active", "end_at": "2030-01-01T10:00:00Z"}
              for i in range(5)]


def pages_read(site, page):
    """Lectures de /api/my-collection pour une page donnée."""
    return sum(f"page={page}&" in q for q in site.collection_requests)


def discard_site(**kwargs):
    site = FakeWikiMasters(list(DISCARD_COLLECTION), DISCARD_AVERAGES, max_auctions=5, **kwargs)
    site.selling = list(FULL_SLOTS)  # aucune vente : on ne teste que la défausse
    return site


def test_discard_runs_in_background_above_trigger(tmp_path):
    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, ["tour eiffel"], wanted=[{"name": "Victor Hugo", "max_price": 1}],
                             discard_below=15, discard_trigger=7)
        run_once(config)
        DISCARDER.join(120)
    assert not DISCARDER.running()
    # Les plus communes d'abord (C, PC, puis R), chacune dès son prix lu ; ni la carte
    # protégée, ni la carte voulue, ni « Pile quinze » (15), ni la rare sans prix.
    assert site.discarded == ["Caillou", "Caillou", "Feuille", "Presque"]
    assert sorted(c["card"]["wikipedia_title"] for c in site.collection) == [
        "Pile quinze", "Rare sans prix", "Tour Eiffel", "Victor Hugo"]


def test_no_discard_at_or_below_trigger(tmp_path):
    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=8)
        run_once(config)
        DISCARDER.join(120)
    assert site.discarded == []


def test_discard_dry_run_sends_nothing(tmp_path):
    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        run_once(config, dry_run=True)
        DISCARDER.join(120)
    assert site.discarded == [] and len(site.collection) == len(DISCARD_COLLECTION)
    assert not (config.state_dir / "discard_progress.json").exists()  # simulation : rien d'enregistré


def test_pass_interrupts_discard_which_then_resumes(tmp_path, caplog):
    import logging
    import time

    from wiki_seller.discard import DISCARDER

    cheap = [entry(f"p{i}", f"P{i}", f"Galet {i}", "C") for i in range(6)]
    with FakeWikiMasters(list(cheap), {f"P{i}": 2 for i in range(6)}, max_auctions=5) as site:
        site.selling = list(FULL_SLOTS)
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        caplog.set_level(logging.INFO)
        DISCARDER.start(config, False, False)
        deadline = time.monotonic() + 120
        while not site.discarded and time.monotonic() < deadline:
            time.sleep(0.2)
        assert site.discarded, "la défausse n'a pas démarré"
        run_once(config)  # attend que la défausse cède la session, puis passe
        paused_at = len(site.discarded)
        DISCARDER.join(180)
    assert paused_at < 6
    assert sorted(site.discarded) == sorted(f"Galet {i}" for i in range(6))
    assert "Défausse en pause" in caplog.text
    # La reprise continue le plan : la collection n'est lue qu'une fois (la page vide qui
    # marque sa fin n'est demandée qu'une fois ; la page 0 l'est aussi par la page Collection).
    assert pages_read(site, 1) == 1


def test_collection_scan_pauses_between_pages_and_resumes(tmp_path):
    from wiki_seller.session import open_site
    from wiki_seller.site import CollectionScan

    cards = [entry(f"s{i}", f"S{i}", f"Sable {i}", "C") for i in range(5)]
    with FakeWikiMasters(list(cards), {}, page_size=2) as site:
        config = make_config(tmp_path, site.url, [])
        scan = CollectionScan()
        with open_site(config, False) as wm:
            calls = iter([False, True])  # une page, puis une passe attend
            assert not wm.scan_collection(scan, lambda: next(calls, True))
            assert len(scan.entries) == 2 and scan.next_page == 1
        with open_site(config, False) as wm:  # nouvelle session : reprise à la page 1
            assert wm.scan_collection(scan, lambda: False)
    assert [e["id"] for e in scan.entries] == [f"s{i}" for i in range(5)]
    assert [pages_read(site, p) for p in (1, 2, 3)] == [1, 1, 1]


def test_finished_discard_is_not_restarted_right_away(tmp_path):
    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=8)
        run_once(config)
        DISCARDER.join(120)
        assert pages_read(site, 1) == 1
        run_once(config)  # seuil non dépassé il y a moins d'une heure : pas de relecture
        DISCARDER.join(120)
    assert pages_read(site, 1) == 1


def test_discard_resumes_from_saved_progress_after_restart(tmp_path):
    from datetime import datetime, timezone

    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        # Redémarrage au milieu du plan : un « Caillou » sur deux déjà défaussé.
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "discard_progress.json").write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(), "position": 0, "copies_done": 1,
            "done": 1,
            "cards": [{"card_id": "A", "title": "Caillou", "rarity": "C", "average": 5, "copies": 2,
                       "copy_ids": ["a1", "a2"]},
                      {"card_id": "B", "title": "Feuille", "rarity": "PC", "average": None, "copies": 1,
                       "copy_ids": ["b1"]}],
        }), encoding="utf-8")
        run_once(config)
        DISCARDER.join(120)
    assert site.discarded == ["Caillou", "Feuille"]
    assert pages_read(site, 1) == 0  # examen repris tel quel, collection non relue
    saved = json.loads((config.state_dir / "discard_progress.json").read_text(encoding="utf-8"))
    assert saved["finished_at"] and saved["done"] == 3 and "cards" not in saved


def test_discard_progress_saves_and_reloads_partial_scan(tmp_path):
    from wiki_seller.discard import DiscardProgress
    from wiki_seller.site import CollectionScan

    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    progress = DiscardProgress(scan=CollectionScan(entries=[{"id": "x"}], seen={"x"}, next_page=1))
    progress.save(config)
    again = DiscardProgress.load(config)
    assert again.scan.entries == [{"id": "x"}] and again.scan.seen == {"x"} and again.scan.next_page == 1
    assert again.cards is None and again.started_at == progress.started_at


def test_each_card_is_discarded_as_soon_as_its_price_is_read(tmp_path):
    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        run_once(config)
        DISCARDER.join(120)
    # Prix du « Caillou » (C), ses deux défausses aussitôt, avant de lire le prix suivant.
    assert site.events[:3] == [("prix", "A"), ("défausse", "Caillou"), ("défausse", "Caillou")]
    assert site.events.index(("défausse", "Caillou")) < site.events.index(("prix", "F"))


def test_expired_session_is_renewed_through_telegram(tmp_path, monkeypatch):
    import threading
    import time

    from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram

    monkeypatch.setattr("wiki_seller.session.LOGIN_WAIT_HEADLESS", 2)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, challenge="manual") as site, FakeTelegram() as tg:
        config = make_config(tmp_path, site.url, [], session_cookies="session=expired",
                             telegram_token=TOKEN, telegram_chat_id=CHAT_ID, telegram_api_url=tg.url)

        def answer():  # la personne répond une fois la demande reçue
            while not tg.sent:
                time.sleep(0.1)
            tg.say("sb-x-auth-token.0=base64-abc; session=ok")

        threading.Thread(target=answer, daemon=True).start()
        result = run_once(config, dry_run=True)
    assert result.listed == 5 and site.logins == 0  # jamais bloqué sur la page /login
    assert site.url in tg.sent[0] and tg.sent[-1].startswith("✅") and len(tg.deleted) == 1
    saved = (config.state_dir / "session_cookies.txt").read_text(encoding="utf-8")
    assert "auth-token.0=base64-abc" in saved  # gardée pour les redémarrages


def test_telegram_report_with_sales_discards_and_balance(tmp_path):
    from datetime import datetime, timedelta, timezone

    from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram

    now = datetime.now(timezone.utc)
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site, FakeTelegram() as tg:
        site.selling = list(FULL_SLOTS)  # aucune vente pendant la passe
        site.history = [{"id": "h1", "seller_id": ME, "status": "settled", "final_price": 40,
                         "settled_at": (now - timedelta(hours=1)).isoformat(), "card": {"id": "A"}}]
        config = make_config(tmp_path, site.url, [], telegram_token=TOKEN, telegram_chat_id=CHAT_ID,
                             telegram_api_url=tg.url, report_every=timedelta(hours=2))
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "report.json").write_text(json.dumps(
            {"since": (now - timedelta(hours=2, minutes=5)).isoformat(), "discarded": 3}))
        run_once(config)
    assert len(tg.sent) == 1
    assert "Ventes : 1 carte(s), +40" in tg.sent[0] and "Défausses : 3 exemplaire(s), +3" in tg.sent[0]
    assert "Total gagné : +43" in tg.sent[0] and "Solde actuel : 100 wikibidous" in tg.sent[0]


def test_progress_without_copy_ids_is_discarded_and_the_collection_reread(tmp_path):
    from datetime import datetime, timezone

    from wiki_seller.discard import DISCARDER

    with discard_site() as site:
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        # Avancement d'une ancienne version : identifiants d'exemplaire vides.
        (config.state_dir / "discard_progress.json").write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(), "position": 0, "copies_done": 0, "done": 0,
            "cards": [{"card_id": "A", "title": "Caillou", "rarity": "C", "average": 5, "copies": 2,
                       "copy_ids": []}],
        }), encoding="utf-8")
        run_once(config)
        DISCARDER.join(120)
    assert pages_read(site, 1) == 1  # collection relue
    assert site.discarded == ["Caillou", "Caillou", "Tour Eiffel", "Victor Hugo", "Feuille", "Presque"]
