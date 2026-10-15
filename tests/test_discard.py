import threading
import time

from wiki_seller.discard import SessionGate, build_discard_plan, total_copies
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


def test_plan_discards_cheap_cards_and_unpriced_commons():
    selling = [{"card_id": "g1", "status": "active"}]
    plan = build_discard_plan(GROUPS, AVERAGES, 15, {"tour eiffel"}, selling, unknown_prices={"H"})
    assert [(i.title, i.copies, i.average) for i in plan] == [
        ("Feuille", 1, None),   # sans prix d'abord
        ("En vente", 1, 3),     # un exemplaire en vente : l'autre seulement
        ("Caillou", 2, 5),      # tous les exemplaires, y compris le dernier
        ("Presque", 1, 14),
    ]


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

    def fetch_average(self, card_id, rarity):
        from wiki_seller.site import PriceApiUnavailable

        self.asked.append(card_id)
        if card_id in self.refused:
            raise PriceApiUnavailable("trop de requêtes", transient=self.transient)
        return 5.0


def price_groups(n):
    return parse_collection([entry(f"x{i}", f"X{i}", f"Carte {i}", "C") for i in range(n)])


def test_transient_price_refusal_skips_only_that_card(tmp_path, monkeypatch):
    import wiki_seller.main as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main, "PRICE_RETRY_PAUSE", 0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    site = PriceStub(refused={"X1"})
    averages, unknown, complete = main.fetch_averages(site, config, price_groups(4))
    assert complete and unknown == {"X1"}
    assert averages == {"X0": 5.0, "X2": 5.0, "X3": 5.0}
    # La carte refusée n'est pas mise en cache : elle sera redemandée la prochaine fois.
    site = PriceStub()
    main.fetch_averages(site, config, price_groups(4))
    assert site.asked == ["X1"]


def test_price_api_given_up_after_repeated_or_forbidden_refusals(tmp_path, monkeypatch):
    import wiki_seller.main as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main, "PRICE_RETRY_PAUSE", 0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    refused = {f"X{i}" for i in range(main.PRICE_FAILURES_BEFORE_GIVING_UP)}
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
    import wiki_seller.main as main

    from .test_end_to_end import make_config

    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    config = make_config(tmp_path, "http://127.0.0.1:1", [])
    calls = iter([False, True])  # avant la carte 0 : rien ; pendant la pause : une passe attend
    site = PriceStub(refused={"X0"})
    _, unknown, complete = main.fetch_averages(site, config, price_groups(3), lambda: next(calls, True))
    assert not complete and site.asked == ["X0"]
