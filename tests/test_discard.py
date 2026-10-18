import threading
import time

from wiki_seller.discard import discard_candidates, should_discard, total_copies
from wiki_seller.session import SessionGate
from wiki_seller.selection import parse_collection


def entry(copy_id, card_id, title, rarity="R"):
    return {"id": copy_id, "card_id": card_id, "card": {"id": card_id, "wikipedia_title": title, "rarity": rarity}}


GROUPS = parse_collection([
    entry("a1", "A", "Caillou", "C"), entry("a2", "A", "Caillou", "C"),
    entry("b1", "B", "Feuille", "PC"),           # aucune vente connue, PC : défaussée
    entry("c1", "C", "Rare sans prix", "R"),     # aucune vente connue, R : gardée
    entry("d1", "D", "Presque", "R"),            # 14 : défaussée
    entry("e1", "E", "Pile quinze", "R"),        # 15 : gardée (strictement moins de 15)
    entry("f1", "F", "Tour Eiffel", "C"),        # protégée
    entry("g1", "G", "En vente", "C"), entry("g2", "G", "En vente", "C"),
    entry("h1", "H", "Prix refusé", "C"),        # API des prix refusée : on ne sait pas
])
AVERAGES = {"A": 5, "B": None, "C": None, "D": 14, "E": 15, "F": 2, "G": 3}


def test_total_copies():
    assert total_copies(GROUPS) == 10


def test_candidates_are_examined_commons_first():
    selling = [{"card_id": "g1", "status": "active"}]
    cards = discard_candidates(GROUPS, {"tour eiffel"}, selling)
    assert [(i.title, i.rarity, i.copies) for i in cards] == [
        ("Caillou", "C", 2),      # tous les exemplaires, y compris le dernier
        ("En vente", "C", 1),     # un exemplaire en vente : l'autre seulement
        ("Prix refusé", "C", 1),
        ("Feuille", "PC", 1),
        ("Pile quinze", "R", 1), ("Presque", "R", 1), ("Rare sans prix", "R", 1),
    ]                             # jamais la carte protégée


def test_discard_decision():
    assert should_discard("C", 5, 15) and should_discard("R", 14, 15)
    assert should_discard("R", 15, 15) and not should_discard("R", 15.5, 15)  # 15 ou moins
    assert should_discard("C", None, 15) and should_discard("PC", None, 15)  # aucune vente connue
    assert not should_discard("R", None, 15) and not should_discard("L", None, 15)


def test_gate_lets_priority_pass_before_background():
    gate = SessionGate()
    order = []
    background_holds = threading.Event()

    def background():
        with gate.background():
            background_holds.set()
            while not gate.should_yield():  # la défausse cède entre deux cartes
                time.sleep(0.01)
            order.append("background yields")

    def priority():
        background_holds.wait()
        with gate.priority():
            order.append("pass")

    threads = [threading.Thread(target=background), threading.Thread(target=priority)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert order == ["background yields", "pass"]


def test_gate_background_waits_for_running_pass():
    gate = SessionGate()
    order = []
    with gate.priority():
        t = threading.Thread(target=lambda: gate.background().__enter__() or order.append("background"))
        t.start()
        time.sleep(0.2)
        order.append("pass done")
    t.join(5)
    assert order == ["pass done", "background"]


class PriceStub:
    """Site minimal pour fetch_averages : refuse le prix des cartes de `refused`."""

    def __init__(self, refused=(), transient=True):
        self.refused, self.transient, self.asked = set(refused), transient, []
        self.retries = 0

    def fetch_average(self, card_id, rarity):
        from wiki_seller.site import PriceApiUnavailable

        self.asked.append(card_id)
        if card_id in self.refused:
            raise PriceApiUnavailable("trop de requêtes", transient=self.transient)
        return 5.0, 10, 5.0


def price_groups(n):
    return parse_collection([entry(f"x{i}", f"X{i}", f"Carte {i}", "C") for i in range(n)])


def test_transient_price_refusal_skips_only_that_card(tmp_path, monkeypatch):
    import wiki_seller.pricing as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main, "RETRY_PAUSE", 0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    site = PriceStub(refused={"X1"})
    averages, unknown, complete = main.fetch_averages(site, config, price_groups(4))
    assert complete and unknown == {"X1:C"}
    assert averages == {"X0:C": 5.0, "X2:C": 5.0, "X3:C": 5.0}
    # La carte refusée n'est pas mise en cache : elle sera redemandée la prochaine fois.
    site = PriceStub()
    main.fetch_averages(site, config, price_groups(4))
    assert site.asked == ["X1"]


def test_price_api_given_up_after_repeated_or_forbidden_refusals(tmp_path, monkeypatch):
    import wiki_seller.pricing as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main, "RETRY_PAUSE", 0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    refused = {f"X{i}" for i in range(main.FAILURES_BEFORE_GIVING_UP)}
    site = PriceStub(refused=refused)
    _, unknown, complete = main.fetch_averages(site, config, price_groups(8))
    assert complete and len(site.asked) == len(refused) and len(unknown) == 8
    # Accès interdit (401/403) : abandon dès le premier refus.
    (tmp_path / "b").mkdir()
    config = make_config(tmp_path / "b", "http://127.0.0.1:1", [])
    site = PriceStub(refused={"X0"}, transient=False)
    _, unknown, _ = main.fetch_averages(site, config, price_groups(3))
    assert site.asked == ["X0"] and len(unknown) == 3


def test_price_pause_gives_way_to_a_waiting_pass(tmp_path, monkeypatch):
    import wiki_seller.pricing as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    calls = iter([False, True])  # avant la carte 0 : rien ; pendant la pause : une passe attend
    site = PriceStub(refused={"X0"})
    _, unknown, complete = main.fetch_averages(site, config, price_groups(3), lambda: next(calls, True))
    assert not complete and site.asked == ["X0"]


def test_discard_restarts_sooner_when_prices_were_missing(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from wiki_seller.discard import next_start

    from .test_end_to_end import make_config

    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    config.state_dir.mkdir(parents=True, exist_ok=True)
    finished = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    progress = config.state_dir / "discard_progress.json"
    progress.write_text(json.dumps({"finished_at": finished, "unpriced": 0}), encoding="utf-8")
    assert next_start(config)  # tout lu : attente d'une heure
    progress.write_text(json.dumps({"finished_at": finished, "unpriced": 3}), encoding="utf-8")
    assert not next_start(config)  # prix manquants : relance après 15 min


def test_candidates_carry_the_copy_ids_not_on_sale():
    selling = [{"card_id": "g1", "status": "active"}]
    cards = {i.title: i.copy_ids for i in discard_candidates(GROUPS, set(), selling)}
    assert cards["Caillou"] == ["a1", "a2"] and cards["En vente"] == ["g2"]
