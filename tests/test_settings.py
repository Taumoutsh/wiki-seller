from datetime import timedelta

import pytest

from wiki_seller import accounts
from wiki_seller.commands import run_settings_command

from .fake_telegram import CHAT_ID
from .test_end_to_end import API_AVERAGES, COLLECTION, make_config


@pytest.fixture
def base(tmp_path):
    config = make_config(tmp_path, "http://127.0.0.1:1", [], telegram_chat_id=CHAT_ID)
    accounts.migrate(config)
    return config


def test_new_account_starts_paused_without_selling_or_discard(base):
    accounts.add(base, "555", "Paul")
    paul = accounts.account_config(base, "555")
    admin = accounts.account_config(base, CHAT_ID)
    assert paul.paused and not paul.selling and paul.discard_below == 0
    assert not admin.paused and admin.selling  # l'administrateur garde les réglages du .env


def test_settings_are_checked_saved_and_applied(base):
    run = lambda command, args="": run_settings_command(base, CHAT_ID, command, args)  # noqa: E731
    assert run("/set", "prix 65") == "✅ prix : 65 % (pris en compte à la prochaine passe)."
    assert run("/set", "raretes l, sr") == "✅ raretes : L,SR (pris en compte à la prochaine passe)."
    assert run("/set", "duree 3h").startswith("✅ duree : 3 h")
    assert run("/set", "intervalle 1h30").startswith("✅ intervalle : 1 h30")
    assert run("/set", "budget aucun").startswith("✅ budget : aucun")
    assert run("/set", "bilan 0").startswith("✅ bilan : jamais")
    assert run("/set", "surenchère 20").startswith("✅ surenchere : 20 s")
    assert run("/set", "nuit aucune").startswith("✅ nuit : aucune")
    for bad in ("prix 0", "raretes XL", "duree 2 h", "intervalle 2 min", "surenchere 1", "ajustement peut-être"):
        assert run("/set", bad).startswith("❌"), bad
    assert run("/set", "couleur rouge").startswith("❌ couleur : réglage inconnu")
    assert run("/set", "prix").startswith("Usage")
    config = accounts.account_config(base, CHAT_ID)
    assert (config.price_ratio, config.sell_rarities, config.auction_duration_label) == (0.65, ("L", "SR"), "3 h")
    assert config.run_interval == timedelta(minutes=90) and config.max_total_bids is None
    assert config.report_every is None and config.snipe_lead == timedelta(seconds=20) and config.night_durations == ()
    assert run("/pause").startswith("⏸️") and accounts.account_config(base, CHAT_ID).paused
    assert run("/resume").startswith("▶️") and not accounts.account_config(base, CHAT_ID).paused
    assert run("/simulation", "on").startswith("✅ simulation : on")
    text = run("/settings")
    assert "<b>⚙️ Réglages</b> · ▶️ actif · 🧪 simulation" in text and "• <b>prix</b> : 65 %" in text
    assert "&lt;réglage&gt;" in text


def test_unreadable_stored_setting_is_ignored(base):
    config = accounts.account_config(base, CHAT_ID)
    (config.state_dir / "settings.json").write_text('{"prix": "beaucoup", "inconnu": "1", "defausse": "3"}')
    config = accounts.account_config(base, CHAT_ID)
    assert config.price_ratio == base.price_ratio and config.discard_below == 3


def test_no_automatic_sale_when_rarities_are_none(tmp_path):
    from wiki_seller.lists import set_sell
    from wiki_seller.scheduler import run_once

    from .fake_site import FakeWikiMasters

    with FakeWikiMasters(COLLECTION, API_AVERAGES) as site:
        config = make_config(tmp_path, site.url, [], selling=False)
        assert run_once(config).listed == 0 and site.listings == []
        set_sell(config, "Pomme", 999)  # une vente demandée part quand même
        run_once(config)
    assert [(l["title"], l["price"]) for l in site.listings] == [("Pomme", "999")]


def test_paused_account_does_nothing_until_resumed(tmp_path, monkeypatch):
    import threading

    import wiki_seller.scheduler as scheduler

    monkeypatch.setattr(scheduler, "PAUSE_CHECK", timedelta(seconds=0.2))
    passes = []
    def fake_pass(config, dry_run, debug):
        passes.append(dry_run)
        raise SystemExit

    monkeypatch.setattr(scheduler, "run_once", fake_pass)
    config = make_config(tmp_path, "http://127.0.0.1:1", [], paused=True, telegram_chat_id="999")
    state = {"config": config}
    def run():
        try:
            scheduler.loop(lambda: state["config"], False, False)
        except SystemExit:  # la fausse passe arrête la boucle
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(0.6)
    assert passes == [] and thread.is_alive()
    import dataclasses
    state["config"] = dataclasses.replace(config, paused=False, simulation=True)
    thread.join(2)
    assert passes == [True]  # reprise, en simulation
