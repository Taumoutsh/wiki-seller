import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wiki_seller import price_worker
from wiki_seller.price_worker import DISCARD, SALES, PriceWorker
from wiki_seller.scheduler import run_once
from wiki_seller.session import gate, open_site

from .fake_site import FakeWikiMasters
from .test_end_to_end import API_AVERAGES, COLLECTION, make_config


@pytest.fixture
def worker(monkeypatch):
    def make(configs, on_ready=None):
        made = PriceWorker(lambda: configs, on_ready)
        made.manual = True  # servie par step(), sans fil
        monkeypatch.setattr(price_worker, "WORKER", made)
        return made
    return make


def drain(worker):
    while worker.step():
        pass


def card(card_id, title="Carte", rarity="R"):
    return SimpleNamespace(card_id=card_id, rarity=rarity, title=title)


def test_pass_reads_no_price_and_sells_once_the_worker_read_them(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES, max_auctions=5) as site:
        config = make_config(tmp_path, site.url, ["tour eiffel", "CHATEAU DE VERSAILLES"])
        ready = []
        prices = worker([config], ready.append)
        result = run_once(config)
        assert result.listed == 0 and not [e for e in site.events if e[0] == "prix"]  # la passe n'attend pas
        assert prices.pending() == 5  # cartes vendables sans prix (les protégées n'en font pas partie)
        drain(prices)
        assert ready == [config]  # tous les prix de vente lus : passe anticipée demandée
        run_once(config)
    assert [(l["title"], l["price"]) for l in site.listings] == [
        ("Victor Hugo", "210"), ("Victor Hugo", "210"), ("Loire", "105"), ("Rhône", "70"), ("Pomme", "14")]


def test_same_card_asked_by_two_accounts_is_read_once(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = make_config(tmp_path / "a", site.url, [])
        second = make_config(tmp_path / "b", site.url, [], shared_dir=first.state_dir)
        for config in (first, second):
            with open_site(config):
                pass
        prices = worker([first, second])
        prices.request(card("D", "Victor Hugo"), first, DISCARD)
        prices.request(card("D", "Victor Hugo"), second, SALES)
        drain(prices)
        assert [e for e in site.events if e[0] == "prix"] == [("prix", "D")]
        assert prices.wait("D:R") == ("ok", 300)


def test_account_session_is_not_used_during_its_pass(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        prices = worker([config])
        prices.request(card("D"), config, SALES)
        with gate(config).priority():  # passe en cours
            assert not prices.step()
        assert prices.step() and prices.wait("D:R") == ("ok", 300)


def test_worker_pauses_before_an_auction_end(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        end = datetime.now(timezone.utc) + timedelta(seconds=40)
        config.wanted_state_file.write_text(json.dumps({"cards": {"x": {"status": "bidding",
                                                                        "end_at": end.isoformat()}}}))
        prices = worker([config])
        prices.request(card("D"), config, SALES)
        assert not prices.step() and site.events == []  # fin d'enchère proche : rien n'est lu
        config.wanted_state_file.write_text(json.dumps({"cards": {}}))
        assert prices.step()


def test_market_session_reads_for_everyone(tmp_path, worker):
    from wiki_seller import market

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])  # pas de session de compte : seul le Marché lit
        ok, _ = market.set_market_session(config, "1", site.session_cookies(), None, None)
        assert ok
        prices = worker([config])
        prices.request(card("D"), config, SALES)
        drain(prices)
        assert prices.wait("D:R") == ("ok", 300)
        assert not config.session_file.exists()


def test_discard_reader_waits_for_the_worker(tmp_path, monkeypatch):
    from wiki_seller.pricing import PriceReader

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config, background=True) as wm:  # comme la défausse
            prices = PriceWorker(lambda: [config]).start()
            monkeypatch.setattr(price_worker, "WORKER", prices)
            try:
                reader = PriceReader(wm, config, priority=DISCARD, wait=True)
                assert reader.read(card("F", "Loire")) == ("ok", 150)
            finally:
                prices.stop()


def test_sales_and_discard_prices_are_read_in_turn(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        prices = worker([config])
        for card_id in ("A", "B", "C"):
            prices.request(card(card_id), config, SALES)
        for card_id in ("F", "G"):
            prices.request(card(card_id), config, DISCARD)
        drain(prices)
    assert [c for kind, c in site.events if kind == "prix"] == ["A", "F", "B", "G", "C"]


def test_prices_read_ahead_are_not_read_again_when_the_discard_gets_there(tmp_path, worker):
    import time

    from wiki_seller.pricing import PriceReader

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config, background=True) as wm:
            prices = worker([config])
            reader = PriceReader(wm, config, priority=DISCARD, wait=True)
            for card_id in ("A", "D", "F"):  # demandées d'avance, comme la défausse
                prices.request(card(card_id), config, DISCARD)
            drain(prices)
            reads = len(site.events)
            deadline = time.monotonic() + 2  # sans prix déjà lu, l'attente s'arrêterait là
            assert reader.read(card("D"), lambda: time.monotonic() > deadline) == ("ok", 300)
            assert len(site.events) == reads and prices.pending() == 0  # rien de relu


def test_card_asked_for_a_sale_keeps_its_place_in_the_queue(tmp_path, worker):
    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        prices = worker([config])
        for card_id in ("A", "B"):
            prices.request(card(card_id), config, DISCARD)
        prices.request(card("C"), config, SALES)
        prices.request(card("B"), config, SALES)  # déjà demandée par la défausse
        drain(prices)
    assert [c for kind, c in site.events if kind == "prix"] == ["B", "A", "C"]


def test_pass_requests_wait_for_a_watched_auction_end():
    import threading
    import time

    from wiki_seller.session import SessionGate

    lock, done = SessionGate(), []
    with lock.watching():
        waiting = threading.Thread(target=lambda: (lock.wait_watches(), done.append(True)))
        waiting.start()
        time.sleep(0.3)
        assert not done  # la requête de la passe attend la fin de la surveillance
    waiting.join(2)
    assert done


def test_worker_pauses_while_an_auction_end_is_watched(tmp_path, worker, monkeypatch):
    from wiki_seller import watcher

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [])
        with open_site(config):
            pass
        prices = worker([config])
        prices.request(card("D"), config, SALES)
        monkeypatch.setattr(watcher, "busy", lambda: True)  # prolongations : fin lue déjà passée
        assert not prices.step() and site.events == []
        monkeypatch.setattr(watcher, "busy", lambda: False)
        assert prices.step()


def test_discard_and_price_worker_give_way_during_an_auction_end(tmp_path):
    from wiki_seller.session import SessionGate

    lock = SessionGate()
    assert not lock.should_yield() and not lock.in_pass()
    with lock.watching():
        assert lock.should_yield() and lock.in_pass()  # défausse interrompue, session laissée par le fil des prix
    assert not lock.should_yield() and not lock.in_pass()
