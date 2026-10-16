import json
from datetime import datetime, timedelta, timezone

from wiki_seller.report import bid_lines, history_text, record_discard, report_text, sales_between, send_report

from .test_end_to_end import make_config

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def sale(price, hours_ago, seller="me", title="Tour Eiffel"):
    return {"seller_id": seller, "final_price": price, "status": "settled", "card": {"wikipedia_title": title},
            "settled_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_sales_between_lists_my_sales_in_the_window():
    history = [sale(40, 1, title="Loire"), sale(25, 1.5), sale(99, 3),  # la dernière : avant le bilan précédent
               sale(50, 1, seller="other"), sale(None, 1)]               # autre vendeur ; invendue
    assert sales_between(history, "me", NOW - timedelta(hours=2), NOW) == [("Tour Eiffel", 25), ("Loire", 40)]


def test_report_text():
    text = report_text(NOW - timedelta(hours=2), NOW, [("Tour Eiffel", 25), ("Loire & Cher", 1040)], 120, 1234)
    start, end = (NOW - timedelta(hours=2)).astimezone(), NOW.astimezone()
    assert text == (f"<b>📊 Bilan · {start:%H:%M} → {end:%H:%M}</b>\n"
                    "👛 1 234 wikibidous  (+1 185 depuis le dernier bilan)\n\n"
                    "<b>💰 Ventes · 2 cartes · +1 065</b>\n"
                    "• Tour Eiffel · 25\n"
                    "• Loire &amp; Cher · 1 040\n\n"
                    "<b>🗑️ Défausses · 120 exemplaires · +120</b>")
    quiet = report_text(NOW, NOW, [], 0, None)
    assert quiet.endswith("👛 Solde illisible\nRien de neuf") and "Ventes" not in quiet and "Défausses" not in quiet
    assert "1 exemplaire ·" in report_text(NOW, NOW, [], 1, 5)


def test_report_details_auctions_of_the_cards_to_buy(tmp_path):
    from wiki_seller.lists import set_buy

    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    set_buy(config, "Tour Eiffel", 500)
    set_buy(config, "Victor Hugo", 120)
    set_buy(config, "Loire", 100)
    config.wanted_state_file.write_text(json.dumps({"cards": {
        "victor hugo": {"my_bid": {"auction": "v1", "amount": 80}, "step": 10},
        "loire": {"my_bid": {"auction": "l1", "amount": 90}}}}))
    end = "2026-10-15T18:42:00+00:00"
    mine = {"bidding": [
        {"id": "t1", "card": {"wikipedia_title": "Tour Eiffel"}, "current_bid": 300, "current_bidder_id": "me",
         "status": "active", "end_at": end},
        {"id": "v1", "card": {"wikipedia_title": "Victor Hugo"}, "current_bid": 100, "current_bidder_id": "rival",
         "status": "active", "end_at": end},
        {"id": "l1", "card": {"wikipedia_title": "Loire"}, "current_bid": 100, "current_bidder_id": "rival",
         "status": "active", "end_at": end},
        {"id": "x1", "card": {"wikipedia_title": "Pas voulue"}, "current_bid": 5, "status": "active"}]}
    hm = datetime.fromisoformat(end).astimezone().strftime("%H:%M")
    text = "\n".join(bid_lines(config, mine, "me", now=datetime.fromisoformat(end)))
    assert text == (f"✅ Tour Eiffel\n   en tête à 300 · max 500 · fin {hm}\n"
                    f"⚠️ Victor Hugo\n   dépassé : 80 → 100 · max 120 · fin {hm}\n"
                    "   ↳ surenchère à 110 juste avant la fin\n"
                    f"❌ Loire\n   dépassé : 90 → 100 · max 100 · fin {hm}\n"
                    "   ↳ abandonnée (au-dessus du max)")
    later = datetime.fromisoformat(end) + timedelta(days=2)
    assert "fin " + datetime.fromisoformat(end).astimezone().strftime("%d/%m %H:%M") in bid_lines(
        config, mine, "me", now=later)[0]
    stub = Stub()
    send_report(stub, config, stub, [], "me", on_demand=True, mine=mine)
    assert "\n\n<b>🛒 Achats en cours</b>\n✅ Tour Eiffel\n" in stub.sent[0] and "Pas voulue" not in stub.sent[0]


class Stub:
    def __init__(self):
        self.sent = []

    def send(self, text, html=False):
        self.sent.append(text)

    def fetch_balance(self):
        return 500


def test_report_every_two_hours_with_discards_counted(tmp_path):
    config = make_config(tmp_path, "http://127.0.0.1:1", [], report_every=timedelta(hours=2))
    stub = Stub()
    assert not send_report(stub, config, stub, [], "me")  # premier lancement : point de départ
    record_discard(config)
    record_discard(config)
    assert not send_report(stub, config, stub, [], "me")  # moins de 2 h
    path = config.state_dir / "report.json"
    data = json.loads(path.read_text())
    data["since"] = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=1)).isoformat()
    path.write_text(json.dumps(data))
    assert send_report(stub, config, stub, [], "me")
    assert "Défausses · 2 exemplaires · +2" in stub.sent[0] and "👛 500 wikibidous" in stub.sent[0]
    assert json.loads(path.read_text())["discarded"] == 0  # compteur remis à zéro
    assert not send_report(stub, config, stub, [], "me")


def test_report_on_demand_keeps_the_two_hour_counter(tmp_path):
    config = make_config(tmp_path, "http://127.0.0.1:1", [], report_every=timedelta(hours=2))
    stub = Stub()
    record_discard(config)
    send_report(stub, config, stub, [], "me", on_demand=True)
    assert "Défausses · 1 exemplaire · +1" in stub.sent[0]
    assert json.loads((config.state_dir / "report.json").read_text())["discarded"] == 1


def test_sleep_wakes_up_for_a_report_request(tmp_path, monkeypatch):
    import wiki_seller.scheduler as main

    calls = []
    monkeypatch.setattr(main, "report_now", lambda config, debug, kinds: calls.append(kinds))
    monkeypatch.setattr(main, "REPORT_GUARD_SECONDS", 0.5)
    main.request("report")
    main.request("history")
    main.sleep_until(main.now_utc() + timedelta(seconds=1.5), None, False)
    assert calls == [{"report", "history"}] and not main.REPORT_REQUEST.is_set()
    calls[:] = [1]
    # Juste avant un réveil : le bilan attend la fin du réveil.
    main.REPORT_REQUEST.set()
    main.sleep_until(main.now_utc() + timedelta(seconds=0.3), None, False)
    assert calls == [1] and main.REPORT_REQUEST.is_set()
    main.REPORT_REQUEST.clear()


def test_history_text_shows_what_the_site_returns():
    now = NOW
    history = [sale(14, 1, title="Trois-Pistoles"),
               {"seller_id": "me", "status": "settled", "final_price": None, "card": {"wikipedia_title": "Loire"},
                "settled_at": (NOW - timedelta(hours=2)).isoformat()},
               sale(30, 3, seller="other", title="Rhône"),
               {"seller_id": "me", "status": "cancelled", "card": {"wikipedia_title": "Pomme"},
                "end_at": (NOW - timedelta(days=2)).isoformat()}]
    text = history_text(history, "me", now)
    first, last = (NOW - timedelta(days=2)).astimezone(), (NOW - timedelta(hours=1)).astimezone()
    assert text.splitlines() == [
        "<b>📜 Historique des ventes · 1 vendue · +14</b>",
        f"Le site renvoie 4 enchères terminées, du {first:%d/%m %H:%M} au {last:%H:%M}.",
        f"✅ Trois-Pistoles · 14 · {last:%H:%M}",
        f"❌ Loire · invendue · {(NOW - timedelta(hours=2)).astimezone():%H:%M}",
        f"👤 Rhône · vendeur différent · {(NOW - timedelta(hours=3)).astimezone():%H:%M}",
        f"⏸️ Pomme · cancelled · {first:%d/%m %H:%M}"]
    assert "aucune enchère terminée" in history_text([], "me", now)
