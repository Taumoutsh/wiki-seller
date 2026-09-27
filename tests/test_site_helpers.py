from datetime import datetime, timedelta, timezone

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
    from wiki_seller.config import Config

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
