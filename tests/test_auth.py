import json
import time

import pytest

from wiki_seller.auth import (
    SessionDead,
    SessionStore,
    decode_session,
    encode_session,
    find_anon_key,
)
from wiki_seller.site import SiteClient, min_bid

from .fake_site import ANON_KEY, APP_JS, COOKIE, LOGIN_HTML, FakeWikiMasters


def pairs(text):
    return [tuple(p.split("=", 1)) for p in text.split("; ")]


def test_session_cookies_round_trip_in_chunks():
    session = {"access_token": "a" * 3000, "refresh_token": "r", "expires_at": 1}
    cookies = encode_session(COOKIE, session)
    assert [name for name, _ in cookies] == [f"{COOKIE}.0", f"{COOKIE}.1"]
    assert all(len(value) <= 3180 for _, value in cookies)
    assert decode_session([("autre", "x"), *reversed(cookies)]) == (COOKIE, session)
    # Cookie non découpé, ou JSON brut encodé dans l'URL.
    assert decode_session([(COOKIE, '%7B%22access_token%22%3A%22a%22%2C%22refresh_token%22%3A%22r%22%7D')]) == (
        COOKIE, {"access_token": "a", "refresh_token": "r"})
    assert decode_session([(f"{COOKIE}.0", "base64-abc")]) is None
    assert decode_session([("session", "ok")]) is None


def test_anon_key_is_found_in_the_site_code():
    pages = {"https://site/login": LOGIN_HTML, "https://site/_next/static/chunks/app-1a2b.js?dpl=dpl_x": APP_JS}
    assert find_anon_key("https://site", "fakeproj", lambda url: pages[url]) == ANON_KEY
    assert find_anon_key("https://site", "autreprojet", lambda url: pages[url]) is None


def test_store_refreshes_saves_and_detects_a_dead_session(tmp_path, monkeypatch):
    monkeypatch.setattr("wiki_seller.auth.REFRESH_MARGIN", 5)
    with FakeWikiMasters([], {}, token_lifetime=3) as site:
        path = tmp_path / "session.json"
        store = SessionStore(path, site.url, site.url)
        store.adopt(*decode_session(pairs(site.session_cookies())))
        store.ensure_fresh()  # expire dans 3 s < 5 s : renouvelée
        saved = json.loads(path.read_text())
        assert site.refreshes == 1 and saved["session"]["refresh_token"] in site.refresh_tokens
        assert SessionStore(path, site.url, site.url).session == store.session  # relue au redémarrage
        assert json.loads((tmp_path / "site.json").read_text()) == {"anon_key": ANON_KEY}
        site.expire_all()
        with pytest.raises(SessionDead):
            store.ensure_fresh(force=True)


def test_client_retries_once_after_a_refused_token(tmp_path):
    with FakeWikiMasters([], {}) as site:
        store = SessionStore(tmp_path / "session.json", site.url, site.url)
        store.adopt(*decode_session(pairs(site.session_cookies())))
        site.access_tokens.clear()  # jeton d'accès refusé, jeton de renouvellement encore bon
        response = SiteClient(site.url, store).request("GET", "/api/wikibidous")
    assert response.status == 200 and response.data == {"balance": 1747} and site.refreshes == 1


def test_parallel_requests_refresh_the_session_only_once(tmp_path):
    import threading

    with FakeWikiMasters([], {}) as site:
        store = SessionStore(tmp_path / "session.json", site.url, site.url)
        store.adopt(*decode_session(pairs(site.session_cookies())))
        site.access_tokens.clear()  # jeton refusé pour tous les fils en même temps
        start, statuses = threading.Barrier(8), []

        def get():
            client = SiteClient(site.url, store)
            start.wait()
            statuses.append(client.request("GET", "/api/wikibidous").status)

        threads = [threading.Thread(target=get) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert statuses == [200] * 8 and site.refreshes == 1  # un seul jeton de renouvellement utilisé


def test_min_bid_follows_the_site_rule():
    assert min_bid({"current_bid": None, "base_amount": 20}) == 20
    assert min_bid({"current_bid": 100}) == 110
    assert min_bid({"current_bid": 250}) == 275
    assert min_bid({"current_bid": 7}) == 8  # ⌈7,7⌉
    assert min_bid({"current_bid": 1}) == 2  # au moins +1
    assert min_bid({"current_bid": 10}) == 11


def test_expires_at_is_computed_when_missing(tmp_path):
    store = SessionStore(tmp_path / "session.json", "https://site")
    store.adopt(COOKIE, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    assert abs(store.session["expires_at"] - (time.time() + 3600)) < 5
    assert store.project == "fakeproj" and store.supabase_url() == "https://fakeproj.supabase.co"


def test_unreachable_site_keeps_the_session(tmp_path, monkeypatch):
    from wiki_seller.config import Config
    from wiki_seller.session import ensure_logged_in
    from wiki_seller.site import SiteError, WikiMasters

    monkeypatch.setattr("wiki_seller.site.time.sleep", lambda s: None)
    url = "http://127.0.0.1:9"  # rien n'écoute : connexion refusée
    config = Config(url, tmp_path / "s.json", tmp_path, 0.7, "1 h", supabase_url=url)
    store = SessionStore(config.session_file, url, url)
    store.adopt(COOKIE, {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600})
    with pytest.raises(SiteError, match="ne répond pas"):
        ensure_logged_in(WikiMasters(SiteClient(url, store), config))
    assert config.session_file.exists()  # gardée pour la prochaine fois
