"""Faux wiki-masters.com minimal pour tester le parcours complet en local : page /login
dont le bouton attend une vérification anti-bot, API JSON (collection, prix, enchères,
mise en vente, défausse, mises), et page d'enchère avec la mise minimale préremplie."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# /login : « Connexion » reste désactivé tant que la vérification anti-bot (Cloudflare
# Turnstile sur le vrai site) n'est pas validée. __CHALLENGE__ : "auto" la valide seule
# au bout d'une seconde, "manual" jamais (il faudrait qu'une personne coche la case).
LOGIN_HTML = """<!doctype html><html><body>
<form id="login">
  <label for="email">Adresse courriel</label><input id="email" type="email">
  <label for="password">Mot de passe</label><input id="password" type="password">
  <div id="challenge">Vérifiez que vous êtes humain.</div>
  <button type="submit" id="do-login" disabled>Connexion</button>
</form>
<script>
if ('__CHALLENGE__' === 'auto') setTimeout(() => document.getElementById('do-login').disabled = false, 1000);
document.getElementById('login').onsubmit = async (e) => {
  e.preventDefault();
  const mail = document.getElementById('email').value, pwd = document.getElementById('password').value;
  const r = await fetch('/auth/login', {method: 'POST', body: JSON.stringify({mail, pwd})});
  if (r.ok) location.href = '/collection';
};
</script></body></html>"""

COLLECTION_HTML = "<!doctype html><html><body><h1>Collection</h1></body></html>"

MARKET_HTML = """<!doctype html><html><body>
<input type="search" placeholder="Rechercher une carte…"><button disabled>Rechercher</button>
</body></html>"""

# /marketplace/<id> : champ « Montant de la mise » prérempli avec la mise minimale.
AUCTION_HTML = """<!doctype html><html><body><div id="a"></div>
<script>
async function load() {
  const a = (await (await fetch('/api/marketplace/__ID__')).json()).auction;
  const min = a.current_bid === null ? a.base_amount : a.current_bid + __STEP__;
  document.getElementById('a').innerHTML = `<h1>${a.card.wikipedia_title}</h1>
    <p>Votre solde : 1 747 wikibidous</p>
    <input type="number" aria-label="Montant de la mise" value="${min}">`;
}
setTimeout(load, 300);
</script></body></html>"""

ME = "me-id"
BID_STEP = 10


def market_auction(auction_id, title, base, end_at="2030-01-01T10:00:00+00:00", seller="other", current=None):
    return {"id": auction_id, "card": {"wikipedia_title": title}, "seller_id": seller, "base_amount": base,
            "effective_bid": current or base, "current_bid": current, "current_bidder_id": None if current is None else "rival",
            "end_at": end_at, "status": "active", "winner_id": None, "final_price": None}


class FakeWikiMasters:
    def __init__(self, collection, averages, selling=None, max_auctions=5, mail="me@example.com",
                 password="secret", sales_forbidden=False, challenge="auto", auctions=None,
                 listing_id="copy", page_size=None):
        self.collection = collection
        self.page_size = page_size  # None : toute la collection sur la page 0
        self.averages = averages  # card_id -> moyenne API
        self.selling = list(selling or [])
        self.max_auctions = max_auctions
        self.mail, self.password = mail, password
        self.sales_forbidden = sales_forbidden
        self.listings = []
        self.challenge = challenge
        self.history = []  # vos ventes terminées (onglet « Historique »)
        self.listing_id = listing_id  # identifiant attendu par POST /api/marketplace
        self.api_listings = []  # corps des mises en vente reçues par l'API
        self.auctions = {a["id"]: a for a in auctions or []}
        self.bids = []  # (auction_id, montant) misés par nous
        self.logins = 0
        self.collection_requests = []
        self.discarded = []  # titres des exemplaires défaussés, dans l'ordre
        self.events = []  # ("prix", carte) et ("défausse", titre), dans l'ordre
        # Site qui bogue : les N prochaines mises restent sans réponse (503, non enregistrées),
        # ou sont enregistrées mais leur réponse se perd ; les N prochaines lectures d'une
        # enchère échouent.
        self.bid_failures = 0
        self.bid_lost_responses = 0
        self.auction_read_failures = 0
        self.bid_attempts = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()

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
                return "session=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                url = urlparse(self.path)
                qs = parse_qs(url.query)
                if url.path == "/login":
                    html = LOGIN_HTML.replace("__CHALLENGE__", site.challenge)
                    return self._send(200, html, "text/html; charset=utf-8")
                if url.path == "/collection":
                    return self._send(200, COLLECTION_HTML, "text/html; charset=utf-8")
                if url.path == "/marketplace":
                    return self._send(200, MARKET_HTML, "text/html; charset=utf-8")
                if url.path.startswith("/marketplace/"):
                    auction_id = url.path.split("/")[2]
                    if auction_id in site.auctions:
                        html = AUCTION_HTML.replace("__ID__", auction_id).replace("__STEP__", str(BID_STEP))
                        return self._send(200, html, "text/html; charset=utf-8")
                    return self._send(200, "<html><body>Enchère créée</body></html>", "text/html")
                if not url.path.startswith("/api/"):
                    return self._send(404, "<html>404</html>", "text/html")
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if url.path == "/api/wikibidous":
                    return self._send(200, {"balance": 100})
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
                    bidding = [a for i, a in site.auctions.items() if i in mine and a["status"] == "active"]
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
                    site.events.append(("prix", card_id))
                    avg = site.averages.get(card_id)
                    card = next(c["card"] for c in site.collection if c["card"]["id"] == card_id)
                    summary = {card["rarity"]: {"average": avg, "count": 5}} if avg else {}
                    return self._send(200, {"summary": summary})
                return self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
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
                    minimum = auction["base_amount"] if auction["current_bid"] is None else auction["current_bid"] + BID_STEP
                    if body.get("amount", 0) < minimum or auction["status"] != "active":
                        return self._send(400, {"error": "mise trop basse"})
                    site.bids.append((auction["id"], body["amount"]))
                    auction.update(current_bid=body["amount"], effective_bid=body["amount"], current_bidder_id=ME)
                    if site.bid_lost_responses:
                        site.bid_lost_responses -= 1
                        return self._send(502, {"error": "Bad Gateway"})
                    return self._send(200, {"auction_id": auction["id"], "current_bid": body["amount"],
                                            "bidder_balance": 1747 - body["amount"]})
                if self.path == "/auth/login":
                    if body.get("mail") == site.mail and body.get("pwd") == site.password:
                        site.logins += 1
                        return self._send(200, {}, headers=[("Set-Cookie", "session=ok; Path=/")])
                    return self._send(400, {"error": "bad credentials"})
                if self.path == "/api/marketplace" and self._authed():
                    if len(site.selling) >= site.max_auctions:
                        return self._send(400, {"error": "Nombre maximum d'enchères actives atteint"})
                    by_card = {c["card"]["id"]: c for c in site.collection}
                    by_copy = {c["id"]: c for c in site.collection}
                    known = by_copy if site.listing_id == "copy" else by_card
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


    def outbid(self, auction_id, amount):
        self.auctions[auction_id].update(current_bid=amount, effective_bid=amount, current_bidder_id="rival")

    def settle(self, auction_id):
        a = self.auctions[auction_id]
        a.update(status="settled_sold", winner_id=a["current_bidder_id"], final_price=a["current_bid"])
