from datetime import datetime, timedelta, timezone

import pytest

from wiki_seller.config import Config, ConfigError, load_config, parse_duration

from wiki_seller.scheduler import next_run_at
from wiki_seller.selling import RunResult
from wiki_seller.session import parse_cookies


def test_next_run_is_one_hour_after_last_listing(tmp_path):
    config = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    listed = RunResult(3, now - timedelta(minutes=5), False, None)
    assert next_run_at(listed, config, now) == now + timedelta(minutes=56)
    full = RunResult(0, None, True, now + timedelta(minutes=20))
    assert next_run_at(full, config, now) == now + timedelta(minutes=21)
    nothing = RunResult(0, None, False, None)
    assert next_run_at(nothing, config, now) == now + timedelta(hours=1)


def test_parse_cookies():
    assert parse_cookies("a.0=base64-xy=; a.1=zz") == [("a.0", "base64-xy="), ("a.1", "zz")]
    assert parse_cookies("a.0=1\na.1=2\n") == [("a.0", "1"), ("a.1", "2")]
    assert parse_cookies("  ") == []


def test_parse_duration():
    assert parse_duration("10 min") == timedelta(minutes=10)
    assert parse_duration("30min") == timedelta(minutes=30)
    assert parse_duration("1 h") == parse_duration("1h") == parse_duration("1 heure") == timedelta(hours=1)
    assert parse_duration("2 heures") == timedelta(hours=2)
    assert parse_duration("1h30") == timedelta(minutes=90)
    for bad in ("10", "", "demain", "0 min"):
        assert parse_duration(bad) is None


def test_next_run_follows_auction_duration_or_run_interval(tmp_path):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    listed = RunResult(1, now, False, None)
    nothing = RunResult(0, None, False, None)

    ten_minutes = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "10 min")
    assert next_run_at(listed, ten_minutes, now) == now + timedelta(minutes=11)
    assert next_run_at(nothing, ten_minutes, now) == now + timedelta(minutes=10)

    explicit = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h",
                      run_interval=timedelta(minutes=20))
    assert next_run_at(listed, explicit, now) == now + timedelta(minutes=21)
    # Places pleines : fin de la première enchère, mais au plus RUN_INTERVAL.
    full = RunResult(0, None, True, now + timedelta(minutes=45))
    assert next_run_at(full, explicit, now) == now + timedelta(minutes=20)
    soon = RunResult(0, None, True, now + timedelta(minutes=5))
    assert next_run_at(soon, explicit, now) == now + timedelta(minutes=6)


@pytest.mark.parametrize("value, expected", [("", None), ("15 min", timedelta(minutes=15)), ("2 h", timedelta(hours=2))])
def test_run_interval_setting(monkeypatch, value, expected):
    monkeypatch.setenv("MAIL", "m")
    monkeypatch.setenv("PASSWORD", "p")
    monkeypatch.setenv("RUN_INTERVAL", value)
    assert load_config().run_interval == expected


@pytest.mark.parametrize("value", ["2 min", "souvent", "10"])
def test_run_interval_rejects_bad_values(monkeypatch, value):
    monkeypatch.setenv("MAIL", "m")
    monkeypatch.setenv("PASSWORD", "p")
    monkeypatch.setenv("RUN_INTERVAL", value)
    with pytest.raises(ConfigError):
        load_config()


def test_next_wake(tmp_path):
    import json as _json

    from wiki_seller.scheduler import next_wake

    night = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h")

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    next_pass = now + timedelta(minutes=30)
    assert next_wake(next_pass, night, now) == (next_pass, False)
    end = now + timedelta(minutes=10)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": end.isoformat()}}}))
    wake, snipe = next_wake(next_pass, night, now)
    # Réveil 60 s (début de la surveillance) + 15 s (vérification de la session) avant la fin.
    assert snipe and wake == end - timedelta(seconds=60) - timedelta(seconds=15)
    # Une fin d'enchère juste après la prochaine passe passe avant elle.
    later = now + timedelta(minutes=33)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": later.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (later - timedelta(seconds=75), True)
    # Fin repoussée (surenchères) : l'heure de réveil est passée mais la fin est à venir.
    soon = now + timedelta(seconds=40)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": soon.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (now, True)
    # Enchère terminée (pas encore réglée par une passe) : pas de réveil en boucle.
    past = now - timedelta(seconds=10)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": past.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (next_pass, False)


def test_search_queries_drop_parentheses_then_punctuation():
    from wiki_seller.site import search_queries

    assert search_queries("Dewey Martin (acteur)") == ["Dewey Martin (acteur)", "Dewey Martin", "Dewey Martin acteur"]
    assert search_queries("Nick Jonas") == ["Nick Jonas"]
    assert search_queries("Zelda II: The Adventure of Link") == ["Zelda II: The Adventure of Link",
                                                                 "Zelda II The Adventure of Link"]


def test_night_durations(tmp_path, monkeypatch):
    from wiki_seller.config import _night_durations

    config = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h",
                    night_durations=_night_durations("1-3=12 h;3-6=6 h"))
    assert [config.duration_label_at(h) for h in (0, 1, 2, 3, 5, 6, 23)] == \
        ["1 h", "12 h", "12 h", "6 h", "6 h", "1 h", "1 h"]
    assert _night_durations("") == ()
    for bad in ("1-3", "1-3=bientôt", "25-3=1 h"):
        with pytest.raises(ConfigError):
            _night_durations(bad)
    monkeypatch.setenv("MAIL", "m")
    monkeypatch.setenv("PASSWORD", "p")
    monkeypatch.delenv("NIGHT_DURATIONS", raising=False)
    assert load_config().night_durations == ((1, 3, "12 h"), (3, 6, "6 h"))


def test_full_slots_wait_is_capped_by_run_interval(tmp_path):
    config = Config("http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h")
    now = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    full = RunResult(0, None, True, now + timedelta(hours=11))  # ventes de nuit de 12 h
    assert next_run_at(full, config, now) == now + timedelta(hours=1)


def test_price_cache_ttl_from_env(monkeypatch):
    monkeypatch.setenv("MAIL", "m")
    monkeypatch.setenv("PASSWORD", "p")
    monkeypatch.delenv("PRICE_CACHE_HOURS", raising=False)
    assert load_config().price_cache_ttl == timedelta(hours=24)
    monkeypatch.setenv("PRICE_CACHE_HOURS", "6")
    assert load_config().price_cache_ttl == timedelta(hours=6)


def test_obsolete_settings_are_reported(monkeypatch):
    from wiki_seller.config import OBSOLETE_SETTINGS, obsolete_settings

    for name in OBSOLETE_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    assert obsolete_settings() == []
    monkeypatch.setenv("ACTIONS_VIA", "page")
    monkeypatch.setenv("OPEN_PACKS_HOURS", "0-6")
    monkeypatch.setenv("MAIL", "me@example.com")
    assert obsolete_settings() == ["ACTIONS_VIA", "OPEN_PACKS_HOURS", "MAIL"]
