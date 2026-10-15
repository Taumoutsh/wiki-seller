import json
from datetime import datetime, timedelta, timezone

from wiki_seller.report import maybe_send_report, record_discard, report_text, sales_between

from .test_end_to_end import make_config

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def sale(price, hours_ago, seller="me"):
    return {"seller_id": seller, "final_price": price, "status": "settled",
            "settled_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_sales_between_counts_my_sales_in_the_window():
    history = [sale(40, 1), sale(25, 1.5), sale(99, 3),       # la dernière : avant le bilan précédent
               sale(50, 1, seller="other"), sale(None, 1)]    # autre vendeur ; invendue
    assert sales_between(history, "me", NOW - timedelta(hours=2), NOW) == (2, 65)


def test_report_text():
    text = report_text(NOW - timedelta(hours=2), NOW, (2, 65), 120, 1234)
    assert "Ventes : 2 carte(s), +65" in text and "Défausses : 120 exemplaire(s), +120" in text
    assert "Total gagné : +185" in text and "Solde actuel : 1 234 wikibidous" in text
    assert "Ventes : aucune" in report_text(NOW, NOW, (0, 0), 0, None)


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
    assert not maybe_send_report(stub, config, stub, [], "me")  # premier lancement : point de départ
    record_discard(config)
    record_discard(config)
    assert not maybe_send_report(stub, config, stub, [], "me")  # moins de 2 h
    path = config.state_dir / "report.json"
    data = json.loads(path.read_text())
    data["since"] = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=1)).isoformat()
    path.write_text(json.dumps(data))
    assert maybe_send_report(stub, config, stub, [], "me")
    assert "Défausses : 2 exemplaire(s), +2" in stub.sent[0] and "Solde actuel : 500" in stub.sent[0]
    assert json.loads(path.read_text())["discarded"] == 0  # compteur remis à zéro
    assert not maybe_send_report(stub, config, stub, [], "me")


def test_report_on_demand_keeps_the_two_hour_counter(tmp_path):
    from wiki_seller.report import send_report_now

    config = make_config(tmp_path, "http://127.0.0.1:1", [], report_every=timedelta(hours=2))
    stub = Stub()
    record_discard(config)
    send_report_now(stub, config, stub, [], "me")
    assert "Défausses : 1 exemplaire(s), +1" in stub.sent[0]
    assert json.loads((config.state_dir / "report.json").read_text())["discarded"] == 1


def test_sleep_wakes_up_for_a_report_request(tmp_path, monkeypatch):
    import wiki_seller.main as main

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
