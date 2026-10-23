"""Faux wiki-masters.com minimal pour tester le parcours complet en local : session
Supabase dans les cookies « sb-fakeproj-auth-token.N » (renouvelable par
/auth/v1/token), clé publique dans le code JavaScript de /login, et API JSON (collection,
prix, enchères, mise en vente, défausse, mises)."""

import base64
import itertools
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from wiki_seller.auth import decode_session, encode_session

PROJECT = "fakeproj"
COOKIE = f"sb-{PROJECT}-auth-token"


def _jwt(claims: dict) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


ANON_KEY = _jwt({"iss": "supabase", "ref": PROJECT, "role": "anon"})
LOGIN_HTML = '<!doctype html><html><head><script src="/_next/static/chunks/app-1a2b.js?dpl=dpl_x" async=""></script></head></html>'
APP_JS = f'const e="https://{PROJECT}.supabase.co",t="{ANON_KEY}";'
MARKET_HTML = "<!doctype html><html><body>Marché</body></html>"
_TOKENS = itertools.count(1)
RUNNING: dict[str, "FakeWikiMasters"] = {}  # adresse → faux site (pour make_config)

ME = "me-id"


def market_auction(auction_id, title, base, end_at="2030-01-01T10:00:00+00:00", seller="other", current=None):
    return {"id": auction_id, "card": {"wikipedia_title": title}, "seller_id": seller, "base_amount": base,
            "effective_bid": current or base, "current_bid": current, "current_bidder_id": None if current is None else "rival",
            "end_at": end_at, "status": "active", "winner_id": None, "final_price": None}


class FakeWikiMasters:
    def __init__(self, collection, averages, selling=None, max_auctions=5, sales_forbidden=False, auctions=None,
                 listing_id="copy", page_size=None, token_lifetime=3600):
        self.collection = collection
        self.page_size = page_size  # None : toute la collection sur la page 0
        self.averages = averages  # card_id -> moyenne API
        self.selling = list(selling or [])
        self.max_auctions = max_auctions
        self.sales_forbidden = sales_forbidden
        self.listings = []
        self.history = []  # vos ventes terminées (onglet « Historique »)
        self.listing_id = listing_id  # identifiant attendu par POST /api/marketplace
        self.api_listings = []  # corps des mises en vente reçues par l'API
        self.auctions = {a["id"]: a for a in auctions or []}
        self.bids = []  # (auction_id, montant) misés par nous
        self.token_lifetime = token_lifetime  # s de validité d'un jeton d'accès
        self.access_tokens: dict[str, float] = {}  # jeton → expiration (epoch)
        self.refresh_tokens: set[str] = set()
        self.refreshes = 0  # sessions renouvelées par /auth/v1/token
        self.balance = 1747  # solde en wikibidous
        self.collection_requests = []
        self.discarded = []  # titres des exemplaires défaussés, dans l'ordre
        self.events = []  # ("prix", carte) et ("défausse", titre), dans l'ordre
        # Site qui bogue : les N prochaines mises restent sans réponse (503, non enregistrées),
        # ou sont enregistrées mais leur réponse se perd ; les N prochaines lectures d'une
        # enchère échouent.
        # Comme le vrai site : les enchères finies restent un moment dans « Mes enchères »,
        # toujours marquées « active ».
        self.stale_bidding = False
        # Marché : détail des ventes par carte (GET …/sales sans scope=summary), visible
        # seulement si le compte y a accès.
        self.sale_counts: dict[str, int] = {}  # ventes connues par carte (5 sinon)
        self.price_failures: set[str] = set()  # cartes dont le prix ne se lit pas
        self.latest: dict[str, int] = {}  # dernière vente par carte (absente sinon)
        self.has_market = True
        self.market_sales: dict[str, list[dict]] = {}
        self.bid_failures = 0
        self.bid_lost_responses = 0
        self.auction_read_failures = 0
        self.bid_attempts = 0
        # Mise d'un autre pas encore visible à la lecture de l'enchère (détail en retard sur
        # les mises) : le site refuse alors la nôtre en indiquant le minimum.
        self.hidden_bids: dict[str, int] = {}
        self.listing_attempts: list[str] = []  # identifiants envoyés à POST /api/marketplace
        self.cards: list[dict] = []  # table des cartes de la base (/rest/v1/cards, clé publique)
        self.extension: timedelta | None = None  # mise dans les 10 dernières s : fin repoussée d'autant
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self):
        self._thread.start()
        RUNNING[self.url] = self
        return self

    def __exit__(self, *exc):
        RUNNING.pop(self.url, None)
        self._server.shutdown()

    def issue_session(self) -> dict:
        n = next(_TOKENS)
        access, refresh = f"at-{n}", f"rt-{n}"
        expires_at = int(time.time()) + self.token_lifetime
        self.access_tokens[access] = expires_at
        self.refresh_tokens.add(refresh)
        return {"access_token": access, "token_type": "bearer", "expires_in": self.token_lifetime,
                "expires_at": expires_at, "refresh_token": refresh, "user": {"id": ME}}

    def session_cookies(self) -> str:
        """Ligne de cookies d'une session neuve, comme la copie le favori du README."""
        return "; ".join(f"{k}={v}" for k, v in encode_session(COOKIE, self.issue_session()))

    def expire_all(self) -> None:
        """Toutes les sessions deviennent invalides (déconnexion, jeton révoqué)."""
        self.access_tokens.clear()
        self.refresh_tokens.clear()

    def _handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json", headers=()):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                pairs = [p.strip().partition("=")[::2] for p in (self.headers.get("Cookie") or "").split(";")]
                decoded = decode_session([(k, v) for k, v in pairs if k])
                if decoded is None:
                    return False
                return site.access_tokens.get(decoded[1]["access_token"], 0) > time.time()

            def do_GET(self):
                url = urlparse(self.path)
                qs = parse_qs(url.query)
                if url.path == "/login":
                    return self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
                if url.path.endswith(".js") and url.path.startswith("/_next/static/"):
                    return self._send(200, APP_JS, "application/javascript")
                if url.path == "/marketplace":
                    return self._send(200, MARKET_HTML, "text/html; charset=utf-8")
                if url.path == "/rest/v1/cards":
                    if self.headers.get("apikey") != ANON_KEY:
                        return self._send(401, {"message": "No API key found in request"})
                    return self._send(200, site.query_cards(qs))
                if not url.path.startswith("/api/"):
                    return self._send(404, "<html>404</html>", "text/html")
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if url.path == "/api/wikibidous":
                    return self._send(200, {"balance": site.balance})
                if url.path == "/api/my-collection":
                    page = int(qs.get("page", ["0"])[0])
                    rarity = qs.get("rarity", [None])[0]
                    items = [c for c in site.collection if not rarity or c["card"]["rarity"] == rarity]
                    site.collection_requests.append(url.query)
                    if site.page_size:
                        items = items[page * site.page_size:(page + 1) * site.page_size]
                    elif page:
                        items = []
                    return self._send(200, {"collection": items})
                if url.path == "/api/marketplace" and qs.get("mine") == ["1"]:
                    mine = {a for a, _ in site.bids}
                    bidding = [a if a["status"] == "active" else dict(a, status="active", winner_id=None)
                               for i, a in site.auctions.items()
                               if i in mine and (a["status"] == "active" or site.stale_bidding)]
                    won = [a for a in site.auctions.values() if a["winner_id"] == ME]
                    selling = [dict(a, seller_id=ME) for a in site.selling]
                    selling += [a for a in site.auctions.values() if a["seller_id"] == ME and a["status"] == "active"]
                    return self._send(200, {"selling": selling, "bidding": bidding, "won": won,
                                            "history": site.history,
                                            "maxConcurrentAuctions": site.max_auctions})
                if url.path == "/api/marketplace" and "q" in qs:
                    q = qs["q"][0].lower()
                    # Comme le vrai site : une requête avec parenthèses ne trouve rien.
                    found = [] if "(" in q else [
                        a for a in site.auctions.values() if q in a["card"]["wikipedia_title"].lower()]
                    return self._send(200, {"auctions": found, "page": 1, "limit": 50, "hasMore": False})
                if url.path.startswith("/api/marketplace/") and url.path.split("/")[3] in site.auctions:
                    if site.auction_read_failures:
                        site.auction_read_failures -= 1
                        return self._send(503, {"error": "Service Unavailable"})
                    return self._send(200, {"auction": site.auctions[url.path.split("/")[3]], "bids": []})
                if url.path.startswith("/api/marketplace/cards/"):
                    if site.sales_forbidden:
                        return self._send(403, {"error": "forbidden"})
                    card_id = url.path.split("/")[4]
                    if card_id in site.price_failures:  # prix de cette carte illisible (panne passagère)
                        return self._send(503, {"error": "Service Unavailable"})
                    if qs.get("scope") != ["summary"]:
                        site.events.append(("marché", card_id))
                        if not site.has_market:
                            return self._send(403, {"error": "Accès au Marché requis"})
                        return self._send(200, {"sales": site.market_sales.get(card_id, [])})
                    site.events.append(("prix", card_id))
                    avg = site.averages.get(card_id)
                    cards = [c["card"] for c in site.collection] + [a["card"] for a in site.auctions.values()]
                    rarity = next((c.get("rarity", "R") for c in cards if c.get("id") == card_id), "R")
                    summary = {rarity: {"average": avg, "count": site.sale_counts.get(card_id, 5),
                                        **({"latest": site.latest[card_id]} if card_id in site.latest else {})}} \
                        if avg else {}
                    return self._send(200, {"summary": summary, "isPro": site.has_market})
                return self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path.startswith("/auth/v1/token"):
                    if self.headers.get("apikey") != ANON_KEY:
                        return self._send(401, {"message": "Invalid API key"})
                    if body.get("refresh_token") not in site.refresh_tokens:
                        return self._send(400, {"error": "invalid_grant",
                                                "error_description": "Invalid Refresh Token: Refresh Token Not Found"})
                    site.refresh_tokens.discard(body["refresh_token"])
                    site.refreshes += 1
                    return self._send(200, site.issue_session())
                discard = re.fullmatch(r"/api/user-cards/([^/]+)/discard", self.path)
                if discard and self._authed():
                    copy = next((c for c in site.collection if c["id"] == discard.group(1)), None)
                    if copy is None:
                        return self._send(404, {"error": "not found"})
                    site.collection.remove(copy)
                    site.discarded.append(copy["card"]["wikipedia_title"])
                    site.events.append(("défausse", copy["card"]["wikipedia_title"]))
                    return self._send(200, {"ok": True, "wikibidous": 1})
                if self.path.startswith("/api/marketplace/") and self.path.endswith("/bid") and self._authed():
                    auction = site.auctions[self.path.split("/")[3]]
                    site.bid_attempts += 1
                    if site.bid_failures:
                        site.bid_failures -= 1
                        return self._send(503, {"error": "Service Unavailable"})
                    current = site.hidden_bids.pop(auction["id"], None) or auction["current_bid"]
                    if current != auction["current_bid"]:  # la mise cachée devient visible
                        auction.update(current_bid=current, effective_bid=current, current_bidder_id="rival")
                    minimum = auction["base_amount"] if current is None else max(math.ceil(1.1 * current), current + 1)
                    if auction["status"] != "active":
                        return self._send(400, {"error": "Enchère terminée", "code": "auction_ended"})
                    if body.get("amount", 0) < minimum:
                        return self._send(400, {"error": "Mise trop basse", "code": "bid_too_low", "min": minimum})
                    site.bids.append((auction["id"], body["amount"]))
                    auction.update(current_bid=body["amount"], effective_bid=body["amount"], current_bidder_id=ME)
                    site.extend(auction["id"])
                    if site.bid_lost_responses:
                        site.bid_lost_responses -= 1
                        return self._send(502, {"error": "Bad Gateway"})
                    return self._send(200, {"auction_id": auction["id"], "current_bid": body["amount"],
                                            "bidder_balance": 1747 - body["amount"]})
                if self.path == "/api/marketplace" and self._authed():
                    site.listing_attempts.append(body.get("card_id"))
                    if len(site.selling) >= site.max_auctions:
                        return self._send(400, {"error": "Nombre maximum d'enchères actives atteint"})
                    by_card = {c["card"]["id"]: c for c in site.collection}
                    by_copy = {c["id"]: c for c in site.collection}
                    known = by_copy if site.listing_id == "copy" else by_card
                    if site.listing_id == "card" and body.get("card_id") in by_copy:  # identifiant d'un autre type
                        return self._send(400, {"error": "Identifiant de carte invalide"})
                    if body.get("card_id") not in known:
                        return self._send(409, {"error": "Vous ne possédez pas cette carte"})
                    site.api_listings.append(body)
                    labels = {10: "10 min", 30: "30 min", 60: "1 h", 360: "6 h", 720: "12 h"}
                    site.listings.append({"card_id": body["card_id"], "title": known[body["card_id"]]["card"]["wikipedia_title"],
                                          "price": str(body["base_amount"]), "duration": labels[body["duration_minutes"]]})
                    auction = {"id": f"a{len(site.listings)}", "card_id": body["card_id"], "status": "active",
                               "end_at": "2030-01-01T10:00:00Z"}
                    site.selling.append(auction)
                    return self._send(201, {"auction_id": auction["id"]})
                return self._send(404, {})

        return Handler


    def query_cards(self, qs):
        """Filtres de la base (PostgREST) que le bot utilise : eq, ilike, like (« * »), plfts."""
        from wiki_seller.selection import normalize_name

        def document(card):  # comme le site : titre puis description, minuscules sans accents
            return normalize_name(f"{card['wikipedia_title']} {card.get('category', '')}")

        def matches(card, column, condition):
            op, _, value = condition.partition(".")
            text = document(card) if column == "search_document" else str(card.get(column, ""))
            if op == "eq":
                return text == value
            if op == "ilike":
                return text.casefold() == value.casefold()
            if op == "like":
                return text.startswith(value.rstrip("*")) if value.endswith("*") else text == value
            if op.startswith("plfts"):
                return set(value.split()) <= set(document(card).split())
            raise AssertionError(condition)

        rows = [c for c in self.cards
                if all(matches(c, k, v) for k, values in qs.items() if k not in ("select", "limit") for v in values)]
        return rows[:int(qs.get("limit", ["1000"])[0])]

    def outbid(self, auction_id, amount):
        self.auctions[auction_id].update(current_bid=amount, effective_bid=amount, current_bidder_id="rival")
        self.extend(auction_id)

    def extend(self, auction_id):
        """Mise dans les 10 dernières secondes : la fin est repoussée (self.extension)."""
        auction = self.auctions[auction_id]
        end = datetime.fromisoformat(auction["end_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if self.extension is not None and now <= end <= now + timedelta(seconds=10):
            auction["end_at"] = (end + self.extension).isoformat()

    def we_bid(self, auction_id, amount):
        """Notre mise (celle de fin d'enchère, sans attendre la fin réelle)."""
        self.bids.append((auction_id, amount))
        self.auctions[auction_id].update(current_bid=amount, effective_bid=amount, current_bidder_id=ME)

    def we_win(self, auction_id, amount):
        """Fin d'enchère gagnée par notre mise de dernière seconde."""
        self.we_bid(auction_id, amount)
        self.settle(auction_id)

    def settle(self, auction_id):
        a = self.auctions[auction_id]
        a.update(status="settled_sold", winner_id=a["current_bidder_id"], final_price=a["current_bid"])
