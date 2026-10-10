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


def test_packs_are_opened_during_the_night_window(tmp_path, caplog):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3) as site:
        config = make_config(tmp_path, site.url, [], packs_hours=(0, 24))
        with caplog.at_level("INFO"):
            run_once(config, dry_run=False)
    assert site.packs_opened == 3 and site.packs == 0
    # Contenu du paquet : résumé, et détail des UR et L seulement.
    assert "5 cartes : 1 L, 1 UR, 1 PC, 2 C" in caplog.text
    assert "★ UR — Tirage 1-3" in caplog.text and "★ L — Tirage 1-4 (brillante)" in caplog.text
    assert "Tirage 1-2" not in caplog.text


def test_packs_are_not_opened_in_dry_run_or_outside_the_window(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=2) as site:
        run_once(make_config(tmp_path, site.url, [], packs_hours=(0, 24)), dry_run=True)
        run_once(make_config(tmp_path, site.url, [], packs_hours=None), dry_run=True)
    assert site.packs_opened == 0


def eiffel_auctions():
    return [market_auction("e1", "Gustave Eiffel", 200), market_auction("e2", "Gustave Eiffel", 150),
            market_auction("e3", "Gustave Eiffel (homonymie)", 10),
            market_auction("e4", "Gustave Eiffel", 100, seller=ME)]


def test_bids_once_then_rebids_only_at_the_end_or_moves_on(tmp_path):
    from datetime import datetime, timedelta, timezone

    wanted = [{"name": "gustave eiffel", "max_price": 300}]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}])
        config.wanted_state_file.parent.mkdir(parents=True, exist_ok=True)
        config.wanted_state_file.write_text(json.dumps({"my_id": ME, "cards": {"gustave eiffel": {
            "name": "Gustave Eiffel", "status": "won", "auction_id": "old", "price": 1}}}))
        run_once(config)
    assert site.bids == []


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


def test_pack_waits_for_each_card_to_be_counted(tmp_path):
    # Comme sur le vrai site : « Encore N cartes » ne baisse qu'une fois la carte affichée
    # un moment (ici 1 s). Le script attend ce décompte avant la carte suivante.
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=2, seen_after_ms=1000) as site:
        from wiki_seller.main import open_site
        with open_site(make_config(tmp_path, site.url, []), debug=False) as wm:
            assert wm.open_packs(dry_run=False) == 2
    assert site.packs_opened == 2


def test_api_trace_logs_calls_without_secrets(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}],
                             api_trace=True)
        run_once(config)
    lines = [json.loads(l) for l in config.api_trace_file.read_text().splitlines()]
    bids = [l for l in lines if l["method"] == "POST" and l["path"].endswith("/bid")]
    assert bids and bids[0]["sent"] == '{"amount": 150}' and bids[0]["status"] == 200
    assert any(l["path"].startswith("/api/marketplace?") and l["received"] for l in lines)
    assert "session=ok" not in config.api_trace_file.read_text()


def test_redact_masks_secret_fields():
    from wiki_seller.trace import redact

    assert redact({"amount": 5, "access_token": "x", "nested": [{"refresh_token": "y", "id": 1}]}) == \
        {"amount": 5, "access_token": "***", "nested": [{"refresh_token": "***", "id": 1}]}


def test_api_mode_lists_by_request_with_api_average(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=3) as site:
        config = make_config(tmp_path, site.url, [], actions_via_api=True)
        result = run_once(config)
    assert result.listed == 3
    # Prix moyen de l'API (et non celui de la fenêtre), par l'identifiant d'un exemplaire.
    assert site.api_listings == [
        {"card_id": "c1", "base_amount": 3500, "duration_minutes": 60},
        {"card_id": "b1", "base_amount": 560, "duration_minutes": 60},
        {"card_id": "b2", "base_amount": 560, "duration_minutes": 60},  # l'autre exemplaire
    ]


def test_api_mode_falls_back_to_card_id(tmp_path, caplog):
    # Si le site attendait l'identifiant de la carte (409 avec celui de l'exemplaire).
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=1, listing_id="card") as site:
        with caplog.at_level("INFO"):
            run_once(make_config(tmp_path, site.url, [], actions_via_api=True))
    assert [l["card_id"] for l in site.api_listings] == ["C"]
    assert "identifiant de la carte" in caplog.text


def test_api_mode_opens_packs_and_bids_by_request(tmp_path, caplog):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, packs=2,
                         auctions=eiffel_auctions()) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Gustave Eiffel", "max_price": 300}],
                             actions_via_api=True, packs_hours=(0, 24), api_trace=True)
        with caplog.at_level("INFO"):
            run_once(config)
    assert site.packs_opened == 2 and "★ UR — Tirage 1-3" in caplog.text
    assert site.bids == [("e2", 150)]
    posts = [json.loads(l) for l in config.api_trace_file.read_text().splitlines() if '"POST"' in l]
    # Paquets, puis mise ; la tentative de mise en vente qui suit est refusée faute de place.
    assert [p["path"] for p in posts][:3] == ["/api/packs/open", "/api/packs/open", "/api/marketplace/e2/bid"]


def test_no_second_bid_while_leading_another_auction_of_the_card(tmp_path):
    auctions = eiffel_auctions()
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=auctions) as site:
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=auctions) as site:
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


def test_api_packs_wait_for_rate_limit(tmp_path, caplog):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3, pack_cooldown=3) as site:
        from wiki_seller.main import open_site
        with open_site(make_config(tmp_path, site.url, [], actions_via_api=True), debug=False) as wm:
            with caplog.at_level("INFO"):
                assert wm.open_packs_api(dry_run=False) == 3
    assert site.packs_opened == 3 and site.rate_limited >= 1
    assert "ouverture trop rapide, nouvel essai" in caplog.text


def test_sale_price_follows_current_auctions_of_the_card(tmp_path, caplog):
    rivals = [market_auction("t1", "Tour Eiffel", 3000), market_auction("t2", "Tour Eiffel", 2000, current=4000),
              market_auction("t3", "Tour Eiffel (homonymie)", 1)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=1, auctions=rivals) as site:
        config = make_config(tmp_path, site.url, [], actions_via_api=True, market_adjust=True)
        with caplog.at_level("INFO"):
            run_once(config)
    # 70 % de 5000 = 3500, moyenne des enchères 3500 (3000 et 4000, pas l'homonyme) :
    # égal, donc inchangé. Avec une enchère de plus à 1000, voir le test suivant.
    assert site.api_listings[0]["base_amount"] == 3500
    assert "Tour Eiffel : prix 3500 → 3500 (2 enchère(s) en cours" in caplog.text


def test_sale_price_lowered_when_above_current_auctions(tmp_path):
    rivals = [market_auction("t1", "Tour Eiffel", 3000), market_auction("t2", "Tour Eiffel", 2000)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=1, auctions=rivals) as site:
        run_once(make_config(tmp_path, site.url, [], actions_via_api=True, market_adjust=True))
    # 3500 au-dessus de la moyenne 2500 : moins l'écart type 500.
    assert site.api_listings[0]["base_amount"] == 3000


def test_api_snipe_rebids_fast_with_learned_step(tmp_path, caplog):
    from datetime import datetime, timedelta, timezone

    auction = market_auction("s1", "Victor Hugo", 50)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=0, auctions=[auction]) as site:
        config = make_config(tmp_path, site.url, [], wanted=[{"name": "Victor Hugo", "max_price": 100}],
                             actions_via_api=True, snipe_lead=timedelta(seconds=6))
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
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=1) as site:
        run_once(make_config(tmp_path, site.url, [], actions_via_api=True, night_durations=((0, 24, "12 h"),)))
    assert site.api_listings[0]["duration_minutes"] == 720


def test_unsold_card_is_set_aside_then_retried(tmp_path):
    from datetime import datetime, timedelta, timezone

    unsold = [{"id": f"h{i}", "seller_id": ME, "status": "expired", "winner_id": None, "final_price": None,
               "card": {"id": "C", "wikipedia_title": "Tour Eiffel"}} for i in range(3)]
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, max_auctions=1) as site:
        site.history = unsold
        config = make_config(tmp_path, site.url, [], actions_via_api=True)
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


def test_packs_paused_when_site_requires_human_verification(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr("wiki_seller.site.VERIFICATION_RETRY_WAIT", 0)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3, max_auctions=0) as site:
        site.packs_need_human = True
        config = make_config(tmp_path, site.url, [], actions_via_api=True, packs_hours=(0, 24))
        with caplog.at_level("INFO"):
            run_once(config)
            run_once(config)
    assert site.packs_opened == 0
    # Un seul nouvel essai après rechargement, puis pas de nouvelle demande à la passe suivante.
    assert site.pack_refusals == 2
    assert "rechargement de la page" in caplog.text
    assert "Vérification anti-bot requise" in caplog.text
    assert "ouverture automatique suspendue" in caplog.text.lower()
    assert (config.state_dir / "packs_pause.json").exists()


def test_packs_retry_once_after_reload_when_verification_goes_away(tmp_path, monkeypatch):
    monkeypatch.setattr("wiki_seller.site.VERIFICATION_RETRY_WAIT", 0)
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=2, max_auctions=0) as site:
        site.packs_need_human = 1  # demandée une seule fois
        config = make_config(tmp_path, site.url, [], actions_via_api=True, packs_hours=(0, 24))
        run_once(config)
    assert site.pack_refusals == 1
    assert site.packs_opened == 2
    assert not (config.state_dir / "packs_pause.json").exists()


def test_await_pack_verification_waits_for_a_person(tmp_path):
    import threading

    from wiki_seller.main import open_site
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3, max_auctions=0) as site:
        site.packs_need_human = True
        with open_site(make_config(tmp_path, site.url, []), debug=False) as wm:
            assert wm.await_pack_verification(timeout_s=2) is False  # personne : délai dépassé
            # La « personne » fait la vérification au bout d'une seconde.
            threading.Timer(1, lambda: setattr(site, "packs_need_human", False)).start()
            assert wm.await_pack_verification(timeout_s=20) is True
    assert site.packs_opened == 1  # le paquet du clic, une fois la vérification faite


def test_human_verification_wait_needs_a_visible_browser(tmp_path, caplog):
    from datetime import timedelta
    with FakeWikiMasters(COLLECTION, API_AVERAGES, UI_AVERAGES, packs=3, max_auctions=0) as site:
        site.packs_need_human = True
        config = make_config(tmp_path, site.url, [], actions_via_api=True, packs_hours=(0, 24),
                             packs_verify_wait=timedelta(minutes=5))
        with caplog.at_level("INFO"):
            run_once(config)
    assert "HEADLESS=false" in caplog.text
    assert (config.state_dir / "packs_pause.json").exists()  # repli : pause habituelle
