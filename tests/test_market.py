import json
from datetime import datetime, timedelta, timezone

import pytest

from wiki_seller import accounts
from wiki_seller.commands import run_market_command
from wiki_seller.lists import AUTO, set_buy, set_sell
from wiki_seller.market import MarketPrice, Sale, clean_price, configured, parse_sales
from wiki_seller.scheduler import run_once
from wiki_seller.session import open_site

from .fake_site import FakeWikiMasters, market_auction
from .test_end_to_end import API_AVERAGES, COLLECTION, make_config

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def sales(*prices, start=NOW - timedelta(days=10)):
    return [Sale(start + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_isolated_money_transfer_is_removed():
    found = clean_price(sales(100, 105, 95, 2000, 100, 98, 102), NOW)
    assert found == MarketPrice(100, 6, 1, False)


def test_last_sales_out_of_line_mean_a_new_value():
    assert clean_price(sales(100, 100, 102, 98, 100, 101, 300, 320), NOW) == MarketPrice(310, 2, 6, True)
    # Une seule dernière vente aberrante : peut-être un échange d'argent, écartée en attendant.
    assert clean_price(sales(100, 100, 102, 98, 100, 500), NOW) == MarketPrice(100, 5, 1, False)
    assert clean_price(sales(100, 100, 102, 98, 100, 30, 32), NOW) == MarketPrice(31, 2, 5, True)  # baisse
    # Une vente haute puis une basse : pas de sens commun, les deux sont écartées.
    assert clean_price(sales(100, 100, 102, 98, 100, 500, 20, 101), NOW) == MarketPrice(pytest.approx(601 / 6), 6, 2, False)


def test_regular_prices_and_window():
    assert clean_price(sales(10, 10, 10, 10, 12), NOW).price == pytest.approx(10.4)  # écart minimal : 10 %
    assert clean_price(sales(100, 120), NOW) == MarketPrice(110, 2, 0, False)  # trop peu de ventes : médiane…
    assert clean_price(sales(22050, 10), NOW).low == 10  # … et la plus basse
    old = sales(5, 5, 5, start=NOW - timedelta(days=40))  # hors des 30 jours
    assert clean_price(old + sales(100, 100), NOW).sales == 2  # ventes de plus de 30 jours ignorées
    assert clean_price(old, NOW) is None
    assert clean_price(sales(*([1] * 10 + [100] * 50)), NOW).price == 100  # 50 dernières ventes


# Ventes réelles d'une carte en rareté C (Marché, octobre 2026) : ~700, puis une hausse
# vers ~1600, et une dernière vente à 4835.
REAL_C = [("2026-10-01T00:24", 600), ("2026-10-01T02:12", 700), ("2026-10-01T23:11", 980),
          ("2026-10-02T20:42", 733), ("2026-10-07T19:29", 535), ("2026-10-08T08:15", 800),
          ("2026-10-08T12:26", 800), ("2026-10-11T01:04", 750), ("2026-10-11T06:20", 800),
          ("2026-10-12T07:00", 605), ("2026-10-12T08:49", 550), ("2026-10-13T09:14", 1651),
          ("2026-10-14T07:37", 1232), ("2026-10-15T09:01", 1706), ("2026-10-17T17:02", 4835)]


def test_real_rise_is_priced_by_the_median_of_the_new_level():
    data = {"wikipedia_title": "Carte", "recent": [],
            "sales": [{"id": str(i), "final_price": p, "settled_at": f"{t}:00.000000+00:00", "rarity": "C"}
                      for i, (t, p) in enumerate(REAL_C)] + [
                     {"id": "l", "final_price": 6000, "settled_at": "2026-10-16T10:00:00+00:00", "rarity": "L"}]}
    found = clean_price(parse_sales(data, "C"), NOW)
    assert found == MarketPrice(1678.5, 4, 11, True)  # 1651, 1232, 1706, 4835 : 4835 ne pèse pas
    assert clean_price(parse_sales(data, "L"), NOW) == MarketPrice(6000, 1, 0, False)  # une seule vente L


def test_parse_sales():
    data = {"sales": [
        {"final_price": 120, "settled_at": "2026-10-10T10:00:00Z", "rarity": "R"},
        {"price": "80", "sold_at": "2026-10-11T10:00:00+00:00", "card": {"rarity": "sr"}},
        {"amount": 90, "created_at": "2026-10-12T10:00:00"},  # rareté inconnue : gardée
        {"final_price": 0, "settled_at": "2026-10-12T10:00:00Z"}, {"final_price": 50}, "x"]}
    assert [s.price for s in parse_sales(data, "R")] == [120, 90]
    assert [s.price for s in parse_sales(data, None)] == [120, 80, 90]
    assert parse_sales(None, "R") == [] and parse_sales({"summary": {}}, "R") == []


def market_sales(*prices):
    start = datetime.now(timezone.utc) - timedelta(days=5)
    return [{"final_price": p, "settled_at": (start + timedelta(hours=i)).isoformat(), "rarity": "R"}
            for i, p in enumerate(prices)]


def market_accounts(tmp_path, site, **extra):
    from .fake_telegram import CHAT_ID

    base = make_config(tmp_path, site.url, ["Tour Eiffel"], telegram_chat_id=CHAT_ID, **extra)
    accounts.migrate(base)
    return base, accounts.account_config(base, CHAT_ID)


def test_market_session_prices_every_account(tmp_path):
    from .fake_telegram import CHAT_ID

    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=3) as site:
        base, config = market_accounts(tmp_path, site)
        (base.state_dir / "price_cache.json").write_text(json.dumps({"D:R": {"average": 1, "at": "2000-01-01"}}))
        site.market_sales = {"D": market_sales(400, 410, 390, 9000, 400), "B": market_sales(900)}
        assert "aucune session" in run_market_command(base, CHAT_ID, "")
        with open_site(config):  # session du compte
            pass
        own = config.session_file.read_text()
        assert "votre compte" in run_market_command(base, CHAT_ID, _cookies(config))
        site.has_market = False
        assert "pas accès au Marché" in run_market_command(base, CHAT_ID, site.session_cookies())
        assert not configured(base)
        site.has_market = True
        assert run_market_command(base, CHAT_ID, site.session_cookies()).startswith("✅ Session Marché")
        assert configured(base) and accounts.find(base, CHAT_ID).has_market
        assert config.session_file.read_text() == own  # la session du compte n'a pas bougé
        assert "session active, fournie par admin" in run_market_command(base, CHAT_ID, "")
        run_once(config)
    cache = json.loads((base.state_dir / "price_cache.json").read_text())
    assert cache["D:R"]["average"] == 400 and cache["D:R"]["source"] == "marche"  # 9000 écarté
    assert cache["B:R"]["average"] == 800 and cache["B:R"]["source"] == "moyenne"  # une seule vente
    assert [(l["title"], l["price"]) for l in site.listings] == [
        ("Château de Versailles", "560"), ("Château de Versailles", "560"), ("Victor Hugo", "280")]  # 70 % de 400


def _cookies(config) -> str:
    from wiki_seller.auth import encode_session

    data = json.loads(config.session_file.read_text())
    return "; ".join(f"{k}={v}" for k, v in encode_session(data["cookie"], data["session"]))


def test_expired_market_session_falls_back_to_the_site_average(tmp_path):
    from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram

    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1) as site, FakeTelegram() as tg:
        base, config = market_accounts(tmp_path, site, telegram_token=TOKEN, telegram_api_url=tg.url)
        site.market_sales = {"B": market_sales(1000, 1000, 1000)}
        assert run_market_command(base, CHAT_ID, site.session_cookies()).startswith("✅")
        market = json.loads((base.state_dir / "market" / "session.json").read_text())["session"]
        site.access_tokens.pop(market["access_token"])
        site.refresh_tokens.discard(market["refresh_token"])
        run_once(config)
        run_once(config)
    assert not configured(base)
    assert [(l["title"], l["price"]) for l in site.listings] == [("Château de Versailles", "560")]
    assert sum("La session Marché a expiré" in t for t in tg.sent) == 1
    cache = json.loads((base.state_dir / "price_cache.json").read_text())
    assert cache["B:R"]["fallback"] is True  # relue dans l'heure


def test_auto_prices_follow_the_market(tmp_path):
    auctions = [market_auction("v1", "Victor Hugo", 280), market_auction("v2", "Victor Hugo", 250)]
    for auction in auctions:
        auction["card"].update(id="D", rarity="R")
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=1, auctions=auctions) as site:
        config = make_config(tmp_path, site.url, [], wanted=[], selling=False)
        set_sell(config, "Loire", AUTO)
        set_buy(config, "Victor Hugo", AUTO)
        run_once(config)
    assert [(l["title"], l["price"]) for l in site.listings] == [("Loire", "165")]  # 110 % de 150
    assert site.bids == []  # suivie, mise à la fin seulement
    assert json.loads(config.wanted_state_file.read_text())["cards"]["victor hugo"]["auction_id"] == "v2"  # max 270
    state = json.loads(config.wanted_state_file.read_text())["cards"]["victor hugo"]
    assert state["auto_max"] == 270 and state["card_ref"] == {"id": "D", "rarity": "R"}


def test_market_command_through_telegram_deletes_the_cookies(tmp_path):
    from wiki_seller.telegram import CommandListener, TelegramBot

    from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram
    from .test_telegram import wait_for

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site, FakeTelegram() as fake:
        base, _ = market_accounts(tmp_path, site, telegram_token=TOKEN, telegram_api_url=fake.url)
        bot = TelegramBot(TOKEN, CHAT_ID, base.state_dir / "telegram.json", fake.url)
        listener = CommandListener(bot, lambda chat, kind: None, base).start()
        try:
            assert wait_for(lambda: (base.state_dir / "telegram.json").exists())  # messages anciens ignorés
            message_id = fake.say("/market " + site.session_cookies())
            assert wait_for(lambda: len(fake.sent) == 2)
        finally:
            listener.stop()
    assert fake.deleted == [message_id]
    assert fake.sent[0].startswith("⏳ Session Marché reçue") and fake.sent[1].startswith("✅ Session Marché")


def test_rarity_option_buys_and_sells_only_that_rarity(tmp_path):
    from wiki_seller.commands import run_list_command
    from wiki_seller.lists import buy_cards, sell_cards

    from .test_end_to_end import entry

    collection = COLLECTION + [entry("h1", "D2", "Victor Hugo", "SR")]
    auctions = [market_auction("c1", "Gustave Eiffel", 10), market_auction("s1", "Gustave Eiffel", 50)]
    auctions[0]["card"].update(id="GC", rarity="C")
    auctions[1]["card"].update(id="GS", rarity="SR")
    with FakeWikiMasters(collection, {**API_AVERAGES, "D2": 1000}, max_auctions=1, auctions=auctions) as site:
        config = make_config(tmp_path, site.url, [], wanted=[], selling=False)
        assert "« Gustave Eiffel » (SR) ajoutée" in run_list_command(config, "/buy", "Gustave Eiffel 100 sr")
        assert "« Victor Hugo » (SR) ajoutée" in run_list_command(config, "/sell", "Victor Hugo 900 SR")
        assert "• Gustave Eiffel (SR) : 100 max" in run_list_command(config, "/list", "")
        run_once(config)
    assert json.loads(config.wanted_state_file.read_text())["cards"]["gustave eiffel|SR"]["auction_id"] == "s1"  # pas C
    assert [(l["card_id"], l["price"]) for l in site.listings] == [("h1", "900")]  # l'exemplaire SR
    assert buy_cards(config)[0].rarity == "SR" and sell_cards(config)[0].rarity == "SR"


def test_shiny_legendaries(tmp_path):
    from wiki_seller.commands import run_list_command
    from wiki_seller.lists import sell_cards

    from .test_end_to_end import entry

    collection = [entry("h1", "LL", "Victor Hugo", "L"), dict(entry("h2", "LL", "Victor Hugo", "L"), is_shiny=True),
                  entry("r1", "RR", "Rhône", "L"), dict(entry("r2", "RR", "Rhône", "L"), is_shiny=True)]
    auctions = [market_auction("n1", "Gustave Eiffel", 300), market_auction("s1", "Gustave Eiffel", 400),
                market_auction("s2", "Loire", 10), market_auction("n2", "Loire", 50)]
    for auction, shiny_auction in zip(auctions, (False, True, True, False)):
        auction["card"].update(id=auction["id"], rarity="L", is_shiny=shiny_auction)
        auction["is_shiny"] = shiny_auction
    with FakeWikiMasters(collection, {"LL": 1000, "RR": 500}, max_auctions=5, auctions=auctions) as site:
        config = make_config(tmp_path, site.url, [], wanted=[], sell_rarities=("L",))
        assert "(L+ shiny) ajoutée" in run_list_command(config, "/buy", "Gustave Eiffel 500 L+")
        assert "(L) ajoutée" in run_list_command(config, "/buy", "Loire 100 L")
        assert run_list_command(config, "/buy", "Rhône L+").startswith("❌ Prix obligatoire")  # pas d'auto
        assert run_list_command(config, "/sell", "Victor Hugo auto L+").startswith("❌ Prix obligatoire")
        assert "(L+ shiny) ajoutée" in run_list_command(config, "/sell", "Victor Hugo 5000 L+")
        run_once(config)
    state = json.loads(config.wanted_state_file.read_text())["cards"]
    assert state["gustave eiffel|L+"]["auction_id"] == "s1"  # la shiny, pas la normale moins chère
    assert state["loire|L"]["auction_id"] == "n2"  # L sans « + » : pas la shiny, pourtant moins chère
    # /sell … L+ vend l'exemplaire shiny ; la vente automatique (raretés L) vend les normaux, jamais un shiny.
    assert sorted((l["card_id"], l["price"]) for l in site.listings) == [
        ("h1", "700"), ("h2", "5000"), ("r1", "350")]
    assert sell_cards(config)[0].rarity == "L+"


def test_shiny_copies_are_never_discarded(tmp_path):
    from wiki_seller.discard import DISCARDER

    from .test_end_to_end import FULL_SLOTS, entry

    collection = [entry("c1", "CC", "Commune", "C"), dict(entry("c2", "CC", "Commune", "C"), is_shiny=True)]
    with FakeWikiMasters(collection, {"CC": 2}, max_auctions=5) as site:
        site.selling = list(FULL_SLOTS)
        config = make_config(tmp_path, site.url, [], discard_below=15, discard_trigger=1)
        run_once(config)
        DISCARDER.join(120)
    assert site.discarded == ["Commune"] and [c["id"] for c in site.collection] == ["c2"]  # la shiny reste



def test_same_card_in_two_rarities_has_two_lines(tmp_path):
    from wiki_seller.commands import run_list_command
    from wiki_seller.lists import buy_cards, sell_cards

    config = make_config(tmp_path, "http://127.0.0.1:1", [], wanted=[])
    assert "(L) ajoutée" in run_list_command(config, "/buy", "Tour Eiffel 500 L")
    assert "(L+ shiny) ajoutée" in run_list_command(config, "/buy", "Tour Eiffel 5000 L+")
    assert "(L) mise à jour" in run_list_command(config, "/buy", "Tour Eiffel 600 L")
    assert [(c.rarity, c.max_price) for c in buy_cards(config)] == [("L", 600), ("L+", 5000)]
    assert "précisez laquelle" in run_list_command(config, "/buy", "Tour Eiffel x2")  # quelle ligne ?
    assert run_list_command(config, "/unbuy", "Tour Eiffel L+") == "« Tour Eiffel » (L+) retirée des achats."
    assert [c.rarity for c in buy_cards(config)] == ["L"]
    run_list_command(config, "/sell", "Loire 80 L")
    run_list_command(config, "/sell", "Loire 900 L+")
    assert run_list_command(config, "/unsell", "Loire").startswith("« Loire » retirée des ventes (2 raretés)")
    assert sell_cards(config) == []


def test_price_is_trusted_only_with_enough_sales():
    from wiki_seller.pricing import decision_price, reliable

    rosehill = {"average": 11030, "count": 2, "low": 10}
    assert not reliable(rosehill, 4) and decision_price(rosehill, 11030, 4) == 10  # la plus basse
    assert reliable({"count": 4}, 4) and decision_price({"count": 4, "low": 10}, 300, 4) == 300
    assert reliable({"average": 50}, 4)  # prix en cache d'avant le comptage des ventes : gardé
