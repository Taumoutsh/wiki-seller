import json
from datetime import datetime, timedelta, timezone

from wiki_seller.report import record_discard, report_text, send_report, sales_between

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
    text = report_text(NOW - timedelta(hours=2), NOW, [("Tour Eiffel", 25), ("Loire", 40)], 120, 1234)
    assert "Ventes : 2 carte(s), +65\n• Tour Eiffel : 25\n• Loire : 40\n" in text
    assert "Défausses : 120 exemplaire(s), +120" in text
    assert "Total gagné : +185" in text and "Solde actuel : 1 234 wikibidous" in text
    assert "Enchères en cours" not in text
    assert "Ventes : aucune" in report_text(NOW, NOW, [], 0, None)


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
    stub = Stub()
    send_report(stub, config, stub, [], "me", on_demand=True, mine=mine)
    text = stub.sent[0]
    when = datetime.fromisoformat(end).astimezone().strftime("%d/%m %H:%M")
    assert f"🛒 Enchères en cours (achats) :\n• Tour Eiffel : vous menez à 300 (max 500), fin {when}" in text
    assert (f"• Victor Hugo : dépassé, votre mise 80, renchérie à 100, surenchère à 110 prévue avant la fin "
            f"(max 120), fin {when}") in text
    assert "• Loire : dépassé, votre mise 90, renchérie à 100, au-dessus de votre max : abandonnée" in text
    assert "Pas voulue" not in text


class Stub:
    def __init__(self):
        self.sent = []

    def send(self, text):
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
    assert "Défausses : 2 exemplaire(s), +2" in stub.sent[0] and "Solde actuel : 500" in stub.sent[0]
    assert json.loads(path.read_text())["discarded"] == 0  # compteur remis à zéro
    assert not send_report(stub, config, stub, [], "me")


def test_report_on_demand_keeps_the_two_hour_counter(tmp_path):
    config = make_config(tmp_path, "http://127.0.0.1:1", [], report_every=timedelta(hours=2))
    stub = Stub()
    record_discard(config)
    send_report(stub, config, stub, [], "me", on_demand=True)
    assert "Défausses : 1 exemplaire(s), +1" in stub.sent[0]
    assert json.loads((config.state_dir / "report.json").read_text())["discarded"] == 1


def test_sleep_wakes_up_for_a_report_request(tmp_path, monkeypatch):
    import wiki_seller.scheduler as main

    calls = []
    monkeypatch.setattr(main, "report_now", lambda config, debug: calls.append(1))
    monkeypatch.setattr(main, "REPORT_GUARD_SECONDS", 0.5)
    main.REPORT_REQUEST.set()
    main.sleep_until(main.now_utc() + timedelta(seconds=1.5), None, False)
    assert calls == [1] and not main.REPORT_REQUEST.is_set()
    # Juste avant un réveil : le bilan attend la fin du réveil.
    main.REPORT_REQUEST.set()
    main.sleep_until(main.now_utc() + timedelta(seconds=0.3), None, False)
    assert calls == [1] and main.REPORT_REQUEST.is_set()
    main.REPORT_REQUEST.clear()
