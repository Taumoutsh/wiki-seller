from datetime import datetime, timedelta, timezone

import pytest

from wiki_seller.config import Config, ConfigError, load_config, parse_duration

from wiki_seller.main import RunResult, next_run_at
from wiki_seller.site import AVERAGE_AFTER, AVERAGE_UNAVAILABLE, duration_pattern, parse_cookies, parse_int


def test_average_regex():
    assert parse_int(AVERAGE_AFTER.search("Prix moyen\n1 234 W").group(1)) == 1234
    assert parse_int(AVERAGE_AFTER.search("Prix moyen : 117\nMise de départ").group(1)) == 117
    assert AVERAGE_AFTER.search("Prix moyen ...") is None
    # Libellé du vrai site : « Moyenne » puis la valeur, dans la fenêtre d'enchère.
    text = "Akira Toriyama\nMarché · Ultra Rare\nMoyenne\n1 953\nMise de départ\n1367"
    assert parse_int(AVERAGE_AFTER.search(text).group(1)) == 1953
    assert AVERAGE_UNAVAILABLE.search("Prix moyen Indisponible")


def test_duration_pattern():
    pattern = duration_pattern("1 h")
    assert pattern.match("1 h") and pattern.match("1h") and pattern.match(" 1 heure ")
    assert not pattern.match("12 h") and not pattern.match("10 min")


def test_next_run_is_one_hour_after_last_listing(tmp_path):
    config = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None)
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

    ten_minutes = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "10 min", True, None)
    assert next_run_at(listed, ten_minutes, now) == now + timedelta(minutes=11)
    assert next_run_at(nothing, ten_minutes, now) == now + timedelta(minutes=10)

    explicit = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None,
                      run_interval=timedelta(minutes=20))
    assert next_run_at(listed, explicit, now) == now + timedelta(minutes=21)
    # Places pleines : on attend toujours la fin de la première enchère.
    full = RunResult(0, None, True, now + timedelta(minutes=45))
    assert next_run_at(full, explicit, now) == now + timedelta(minutes=46)


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
