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


def test_packs_window_and_next_wake(tmp_path):
    import json as _json

    from wiki_seller.main import in_packs_window, next_wake

    default = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None)
    assert not in_packs_window(default, datetime(2026, 1, 1, 1, 0))  # désactivé par défaut
    night = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None, packs_hours=(0, 6))
    assert in_packs_window(night, datetime(2026, 1, 1, 0, 0)) and in_packs_window(night, datetime(2026, 1, 1, 5, 59))
    assert not in_packs_window(night, datetime(2026, 1, 1, 6, 0))
    wrap = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None, packs_hours=(22, 2))
    assert in_packs_window(wrap, datetime(2026, 1, 1, 23)) and in_packs_window(wrap, datetime(2026, 1, 1, 1))
    assert not in_packs_window(wrap, datetime(2026, 1, 1, 12))

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    next_pass = now + timedelta(minutes=30)
    assert next_wake(next_pass, night, now) == (next_pass, False)
    end = now + timedelta(minutes=10)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": end.isoformat()}}}))
    wake, snipe = next_wake(next_pass, night, now)
    # Réveil 60 s (début de la surveillance) + 45 s (démarrage) avant la fin.
    assert snipe and wake == end - timedelta(seconds=60) - timedelta(seconds=45)
    # Une fin d'enchère juste après la prochaine passe passe avant elle.
    later = now + timedelta(minutes=33)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": later.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (later - timedelta(seconds=105), True)
    # Fin repoussée (surenchères) : l'heure de réveil est passée mais la fin est à venir.
    soon = now + timedelta(seconds=40)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": soon.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (now, True)
    # Enchère terminée (pas encore réglée par une passe) : pas de réveil en boucle.
    past = now - timedelta(seconds=10)
    night.wanted_state_file.write_text(_json.dumps({"cards": {"x": {"status": "bidding", "end_at": past.isoformat()}}}))
    assert next_wake(next_pass, night, now) == (next_pass, False)


def test_wanted_cards_file(tmp_path):
    from wiki_seller.buying import WantedCardsError, load_wanted_cards

    path = tmp_path / "wanted.json"
    assert load_wanted_cards(path) == []
    path.write_text('[{"name": " Tour Eiffel ", "max_price": 500}, {"name": "Pomme", "max_price": 5, "copies": 3}]')
    assert [(c.name, c.max_price, c.copies, c.key) for c in load_wanted_cards(path)] == [
        ("Tour Eiffel", 500, 1, "tour eiffel"), ("Pomme", 5, 3, "pomme")]
    for bad in ('{"name": "x"}', '[{"name": "x", "max_price": "10"}]', '[{"max_price": 10}]', "[,",
                '[{"name": "x", "max_price": 10, "copies": 0}]'):
        path.write_text(bad)
        with pytest.raises(WantedCardsError):
            load_wanted_cards(path)


def test_search_queries_drop_parentheses_then_punctuation():
    from wiki_seller.site import search_queries

    assert search_queries("Dewey Martin (acteur)") == ["Dewey Martin (acteur)", "Dewey Martin", "Dewey Martin acteur"]
    assert search_queries("Nick Jonas") == ["Nick Jonas"]
    assert search_queries("Zelda II: The Adventure of Link") == ["Zelda II: The Adventure of Link",
                                                                 "Zelda II The Adventure of Link"]


def test_pack_cards_and_summary():
    from wiki_seller.site import pack_cards, pack_summary

    data = {"remaining": 3, "cards": [{"id": "x", "card": {"wikipedia_title": "Tour Eiffel", "rarity": "ur"}},
                                      {"card": {"wikipedia_title": "Pomme", "rarity": "C", "is_shiny": True}},
                                      {"wikipedia_title": "Loire", "rarity": "L"}]}
    cards = pack_cards(data)
    assert cards == [{"title": "Tour Eiffel", "rarity": "UR", "shiny": False},
                     {"title": "Pomme", "rarity": "C", "shiny": True},
                     {"title": "Loire", "rarity": "L", "shiny": False}]
    assert pack_summary(cards) == "3 cartes : 1 L, 1 UR, 1 C"
    assert pack_summary([]) == "contenu non lu"


def test_rate_limit_wait():
    from wiki_seller.site import rate_limit_wait

    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    assert 29 <= rate_limit_wait(soon) <= 32
    assert rate_limit_wait(None) == 10.0
    assert rate_limit_wait("2020-01-01T00:00:00Z") == 2.0  # déjà passé : petit délai
    far = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert rate_limit_wait(far) == 120.0


def test_night_durations(tmp_path, monkeypatch):
    from wiki_seller.config import _night_durations

    config = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None,
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
    config = Config("m", "p", "http://x", tmp_path / "s.json", tmp_path, 0.7, "1 h", True, None)
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
