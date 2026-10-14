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
