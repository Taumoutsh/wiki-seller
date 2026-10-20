from datetime import datetime, timedelta, timezone

import pytest

from wiki_seller import details
from wiki_seller.lists import ListError
from wiki_seller.session import open_site
from wiki_seller.telegram import TelegramBot

from .fake_site import FakeWikiMasters, market_auction
from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram
from .test_end_to_end import API_AVERAGES, COLLECTION, make_config

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def sale(sale_id, price, days_ago, rarity="R", **extra):
    at = (NOW - timedelta(days=days_ago)).isoformat()
    return {"id": sale_id, "final_price": price, "settled_at": at, "rarity": rarity, **extra}


SALES = [sale("s1", 100, 9), sale("s2", 110, 7), sale("s3", 95, 5), sale("s4", 5000, 4), sale("s5", 105, 2),
         sale("s6", 900, 3, "L"), sale("s7", 4000, 1, "L", is_shiny=True)]


def test_sales_are_grouped_by_rarity_with_the_bot_price():
    sales = details.parse_market_sales({"sales": SALES, "recent": SALES[-2:]})  # « recent » sans doublon
    groups = details.build_groups(sales, {"R": {"average": 1082, "count": 5}}, None, NOW)
    assert [(g.rarity, len(g.sales)) for g in groups] == [("L", 1), ("L+", 1), ("R", 5)]
    rare = groups[-1]
    assert rare.clean.price == pytest.approx(102.5) and rare.site_average == 1082
    assert [rare.sales[i].price for i in rare.outliers] == [5000]  # échange d'argent : écartée
    only = details.build_groups(sales, None, "L+", NOW)
    assert [(g.rarity, g.prices) for g in only] == [("L+", [4000])]


def test_page_shows_chart_stats_and_last_sales():
    sales = details.parse_market_sales({"sales": SALES})
    found = details.Details("D", "Victor Hugo", details.build_groups(sales, None, None, NOW),
                            [market_auction("a1", "Victor Hugo", 80)], "session Marché", NOW)
    page = details.render(found, "https://wikimasters.fr")
    assert page.startswith("<!doctype html>") and "<svg" in page and "<script" not in page
    assert "<h1>Victor Hugo</h1>" in page and "Prix retenu par le bot" in page
    assert "https://wikimasters.fr/marketplace/a1" in page  # enchère en cours
    assert page.count("<tr class='out'>") == 1  # la vente à 5000, écartée
    assert "R : 5 vente(s), moyenne 1 082, de 95 à 5 000, retenu 102" in details.caption(found)
    assert details.filename(found) == "fiche-victor-hugo.html"


def test_command_arguments():
    assert details.parse("Tour Eiffel") == ("Tour Eiffel", None)
    assert details.parse("Tour Eiffel L+") == ("Tour Eiffel", "L+")
    assert details.parse("Apollo 11; SR") == ("Apollo 11", "SR")
    link = "https://wikimasters.fr/marketplace/28f53a71-0000-4000-8000-000000000001"
    assert details.parse(link) == (link, None)
    with pytest.raises(ListError):
        details.parse("  ")


def test_details_page_is_sent_to_telegram(tmp_path):
    auction = market_auction("a1", "Victor Hugo", 80)
    auction["card"].update(id="D", rarity="R")
    with FakeWikiMasters(COLLECTION, API_AVERAGES, auctions=[auction]) as site, FakeTelegram() as tg:
        site.market_sales["D"] = [sale("s1", 280, 3), sale("s2", 300, 2), sale("s3", 320, 1)]
        config = make_config(tmp_path, site.url, [])
        with open_site(config):  # session du compte (pas de session Marché)
            pass
        before = {f for f in tmp_path.rglob("*") if f.is_file()}
        details.send_details(config, "victor hugo", TelegramBot(TOKEN, CHAT_ID, tmp_path / "tg.json", tg.url))
        assert {f for f in tmp_path.rglob("*") if f.is_file()} == before  # fiche jamais écrite sur disque
        assert not [e for e in site.events if e[0] in ("mise", "vente", "défausse")]  # lectures seulement
    assert tg.sent == [] and len(tg.documents) == 1
    document = tg.documents[0]
    assert document["name"] == "fiche-victor-hugo.html" and document["chat_id"] == CHAT_ID
    assert document["caption"].startswith("📊 Victor Hugo\nR : 3 vente(s), moyenne 300, de 280 à 320")
    assert b"<svg" in document["content"] and "Victor Hugo".encode() in document["content"]


def test_unknown_card_is_reported(tmp_path):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site, FakeTelegram() as tg:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        details.send_details(config, "Carte Inconnue", TelegramBot(TOKEN, CHAT_ID, tmp_path / "tg.json", tg.url))
    assert tg.documents == [] and tg.sent[0].startswith("❌ « Carte Inconnue » introuvable")


def test_details_command_answers_in_telegram(tmp_path):
    from wiki_seller import accounts
    from wiki_seller.telegram import CommandListener

    from .test_telegram import bot, wait_for

    base = make_config(tmp_path, "http://127.0.0.1:1", [], wanted=[], telegram_chat_id=CHAT_ID)
    accounts.migrate(base)
    with FakeTelegram() as fake:
        listener = CommandListener(bot(tmp_path, fake), lambda chat, kind: None, base).start()
        try:
            assert wait_for(lambda: (tmp_path / "telegram.json").exists())
            fake.say("/details Tour Eiffel")
            assert wait_for(lambda: len(fake.sent) == 2)
        finally:
            listener.stop()
    assert fake.sent[0] == "⏳ Fiche en préparation…"
    assert fake.sent[1].startswith("❌ Aucune session pour lire le Marché")  # compte sans session
