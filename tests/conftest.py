from datetime import timedelta

import pytest


@pytest.fixture(autouse=True)
def quick_outcome(monkeypatch):
    """Le faux site ne désigne pas toujours de gagnant : pas 3 min d'attente par test."""
    monkeypatch.setattr("wiki_seller.buying.OUTCOME_WAIT", timedelta(seconds=8))
    monkeypatch.setattr("wiki_seller.buying.OUTCOME_POLL", 1)
