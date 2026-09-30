"""Faux wiki-masters.com minimal pour tester le parcours complet en local.

Il reproduit les éléments utilisés par le script : page /login dont le bouton attend
une vérification anti-bot, popup anti-bot avec case à cocher, API JSON, page /collection avec
recherche, tuiles, fenêtre « Mettre aux enchères » et « Prix moyen » chargé en différé."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Comme le vrai site : /signup affiche l'inscription, avec un lien vers /login.
SIGNUP_HTML = """<!doctype html><html><body>
<h2>Créer un compte</h2>
<p>Déjà inscrit ? <a href="/login">Se connecter</a></p>
<form id="signup" onsubmit="event.preventDefault(); fetch('/auth/signup', {method: 'POST'})">
  <label for="username">Nom d'utilisateur</label><input id="username" autocomplete="username" placeholder="WikiMaster42">
  <label for="email">Adresse courriel</label><input id="email" type="email" autocomplete="email">
  <label for="password">Mot de passe</label><input id="password" type="password" autocomplete="new-password">
  <button type="submit">Créer mon compte</button>
</form></body></html>"""

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

COLLECTION_HTML = """<!doctype html><html><body>
<div id="antibot" class="fixed" style="display:none;position:fixed;inset:0;background:#fff;z-index:10">
  <label><input type="checkbox" id="human"> Je ne suis pas un robot</label>
  <button onclick="if(document.getElementById('human').checked){this.parentElement.style.display='none'}">Continuer</button>
</div>
<input placeholder="Rechercher par titre ou catégorie..." id="q">
<div id="grid"></div>
<div id="detail"></div>
<script>
let cards = [];
const show = (html) => document.getElementById('detail').innerHTML = html;
async function load() {
  const r = await fetch('/api/my-collection?sort=rarity&page=0&stats=0');
  cards = (await r.json()).collection;
  render();
  if (!sessionStorage.getItem('antibot-done')) {
    sessionStorage.setItem('antibot-done', '1');
    document.getElementById('antibot').style.display = 'block';
  }
}
function render() {
  const q = document.getElementById('q').value.toLowerCase();
  const seen = new Set();
  document.getElementById('grid').innerHTML = cards
    .filter(c => !seen.has(c.card.id) && seen.add(c.card.id))
    .filter(c => c.card.wikipedia_title.toLowerCase().includes(q))
    .map(c => `<div class="cursor-pointer tile" data-id="${c.card.id}"><div><span>${c.card.wikipedia_title}</span></div><span>${c.card.rarity}</span></div>`)
    .join('');
  document.querySelectorAll('.tile').forEach(t => t.onclick = () => openCard(t.dataset.id));
}
document.getElementById('q').oninput = render;
function openCard(id) {
  const c = cards.find(x => x.card.id === id);
  show(`<div role="dialog"><h2>${c.card.wikipedia_title}</h2><button id="sell">🔨 Mettre aux enchères</button><button>Défausser</button></div>`);
  document.getElementById('sell').onclick = () => openAuction(c);
}
async function openAuction(c) {
  const mine = await (await fetch('/api/marketplace?page=1&limit=50&sort=recent&mine=1')).json();
  show(`<div role="dialog"><div class="card-frame">
    <p>Enchères actives : ${mine.selling.length}/${mine.maxConcurrentAuctions}</p>
    <div><span>Moyenne</span><span id="avg">...</span></div>
    <label>MISE DE DÉPART <input aria-label="Mise de départ" inputmode="numeric" id="price" value="1"></label>
    <div><button class="dur">10 min</button><button class="dur">1 h</button><button class="dur">24 h</button></div>
    <button id="cancel">Annuler</button><button id="launch">Lancer l'enchère</button>
  </div></div>`);
  let duration = null;
  document.querySelectorAll('.dur').forEach(b => b.onclick = () => duration = b.textContent);
  document.getElementById('cancel').onclick = () => show('');
  setTimeout(() => {
    const avg = window.UI_AVERAGES[c.card.wikipedia_title];
    document.getElementById('avg').textContent = avg ? `${avg.toLocaleString('fr-FR')} W` : 'Indisponible';
  }, 800);
  document.getElementById('launch').onclick = async () => {
    const r = await fetch('/api/marketplace', {method: 'POST', body: JSON.stringify({
      card_id: c.id, title: c.card.wikipedia_title,
      price: document.getElementById('price').value, duration})});
    if (r.ok) location.href = '/marketplace/' + (await r.json()).auction_id;
  };
}
window.UI_AVERAGES = __UI_AVERAGES__;
load();
</script></body></html>"""


# /pulls : comme le vrai site, « Ouvrir » (span dans le bouton), « N / 10 paquets
# disponibles », puis un carrousel de 5 cartes : flèches sans texte, points, et un bouton
# « Encore k cartes » (désactivé) qui devient « Continuer » sur la dernière carte. La
# touche flèche droite du clavier ne fait rien (comme sur le vrai site).
PULLS_HTML = """<!doctype html><html><body><main id="main"></main>
<script>
let packs = 0, card = 0, seen = new Set(), arrived = 0;
const SEEN_AFTER = __SEEN_AFTER__;  // ms sur une carte pour qu'elle compte comme vue
function leave() { if (Date.now() - arrived >= SEEN_AFTER) seen.add(card); }
setInterval(() => { if (main.querySelector('.card') && Date.now() - arrived >= SEEN_AFTER && !seen.has(card)) { seen.add(card); reveal(false); } }, 100);
const main = document.getElementById('main');
function home() {
  main.innerHTML = `<h1>Ouvrir un paquet</h1>
    <button id="open" ${packs ? '' : 'disabled'}><img alt="Paquet"><span>Ouvrir</span></button>
    <div>${packs} / 10</div><div>paquets disponibles</div>`;
  document.getElementById('open').onclick = async () => {
    packs = (await (await fetch('/api/packs/open', {method: 'POST'})).json()).remaining;
    card = 0; seen = new Set(); reveal();
  };
}
function reveal(moved = true) {
  if (moved) arrived = Date.now();
  const allSeen = seen.size >= 5 || (SEEN_AFTER === 0);
  const dots = [0,1,2,3,4].map(i => `<button class="dot">${''}</button>`).join('');
  main.innerHTML = `<div>Carte ${card + 1} / 5</div><div class="card">Carte n°${card + 1}</div>
    <div style="display:flex">
      <button id="prev" ${card ? '' : 'disabled'}><svg width="10" height="10"></svg></button>${dots}
      <button id="next" ${card < 4 ? '' : 'disabled'}><svg width="10" height="10"></svg></button>
    </div>
    ${card < 4 || !allSeen ? `<button disabled>Encore ${SEEN_AFTER ? Math.max(1, 5 - seen.size) : 4 - card} cartes</button>` : '<button id="done">Continuer</button>'}`;
  document.getElementById('prev').onclick = () => { leave(); card--; reveal(); };
  document.querySelectorAll('.dot').forEach((d, i) => d.onclick = () => { leave(); card = i; reveal(); });
  document.getElementById('next').onclick = () => { leave(); card++; reveal(); };
  const done = document.getElementById('done');
  if (done) done.onclick = home;
}
home();
// Comme le vrai site : 0 d'abord, le vrai nombre une fois chargé.
setTimeout(() => { packs = __PACKS__; if (!main.querySelector('.card')) home(); }, 1500);
</script></body></html>"""

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
    <input type="number" aria-label="Montant de la mise" value="${min}">
    <button id="bid">Miser</button>`;
  document.getElementById('bid').onclick = () => fetch('/api/marketplace/__ID__/bid', {method: 'POST',
    body: JSON.stringify({amount: Number(document.querySelector('input').value)})});
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
    def __init__(self, collection, averages, ui_averages=None, selling=None, max_auctions=5,
                 mail="me@example.com", password="secret", sales_forbidden=False,
                 challenge="auto", packs=0, auctions=None, seen_after_ms=0, listing_id="copy",
                 pack_cooldown=0.0):
        self.collection = collection
        self.averages = averages  # card_id -> moyenne API
        self.ui_averages = ui_averages if ui_averages is not None else {}
        self.selling = list(selling or [])
        self.max_auctions = max_auctions
        self.mail, self.password = mail, password
        self.sales_forbidden = sales_forbidden
        self.listings = []
        self.challenge = challenge
        self.packs = packs
        self.seen_after_ms = seen_after_ms
        self.pack_cooldown = pack_cooldown  # secondes entre deux ouvertures (429 sinon)
        self.last_pack_at = -1e9
        self.rate_limited = 0
        self.pack_refusals = 0
        self.history = []  # vos ventes terminées (onglet « Historique »)
        self.listing_id = listing_id  # identifiant attendu par POST /api/marketplace
        self.api_listings = []  # corps des mises en vente reçues par l'API
        self.packs_opened = 0
        self.auctions = {a["id"]: a for a in auctions or []}
        self.bids = []  # (auction_id, montant) misés par nous
        self.logins = 0
        self.signups = 0
        self.collection_requests = []
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
                if url.path == "/signup":
                    return self._send(200, SIGNUP_HTML, "text/html; charset=utf-8")
                if url.path == "/login":
                    html = LOGIN_HTML.replace("__CHALLENGE__", site.challenge)
                    return self._send(200, html, "text/html; charset=utf-8")
                if url.path == "/collection":
                    html = COLLECTION_HTML.replace("__UI_AVERAGES__", json.dumps(site.ui_averages))
                    return self._send(200, html, "text/html; charset=utf-8")
                if url.path == "/pulls":
                    html = PULLS_HTML.replace("__PACKS__", str(site.packs)).replace("__SEEN_AFTER__", str(site.seen_after_ms))
                    return self._send(200, html, "text/html; charset=utf-8")
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
                    return self._send(200, {"collection": items if page == 0 else []})
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
                    return self._send(200, {"auction": site.auctions[url.path.split("/")[3]], "bids": []})
                if url.path.startswith("/api/marketplace/cards/"):
                    if site.sales_forbidden:
                        return self._send(403, {"error": "forbidden"})
                    card_id = url.path.split("/")[4]
                    avg = site.averages.get(card_id)
                    card = next(c["card"] for c in site.collection if c["card"]["id"] == card_id)
                    summary = {card["rarity"]: {"average": avg, "count": 5}} if avg else {}
                    return self._send(200, {"summary": summary})
                return self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/packs/open" and self._authed() and site.packs > 0 and site.pack_cooldown:
                    now = time.monotonic()
                    if now - site.last_pack_at < site.pack_cooldown:
                        retry = datetime.now(timezone.utc) + timedelta(seconds=site.pack_cooldown - (now - site.last_pack_at))
                        site.rate_limited += 1
                        return self._send(429, {"error": "Ouverture trop rapide. Attends un instant avant d'ouvrir un autre paquet.",
                                                "rate_limited": True, "rate_limit_daily": False,
                                                "retry_after": retry.isoformat().replace("+00:00", "Z"),
                                                "packs_remaining": site.packs})
                    site.last_pack_at = now
                if self.path == "/api/packs/open" and self._authed() and site.packs == 0:
                    site.pack_refusals += 1
                    return self._send(400, {"error": "Aucun paquet disponible", "packs_remaining": 0})
                if self.path == "/api/packs/open" and self._authed() and site.packs > 0:
                    site.packs -= 1
                    site.packs_opened += 1
                    pulled = [{"id": f"u{site.packs_opened}{i}", "card": {"wikipedia_title": f"Tirage {site.packs_opened}-{i}",
                                                                           "rarity": r}, "is_shiny": i == 4}
                              for i, r in enumerate(["C", "C", "PC", "UR", "L"])]
                    return self._send(200, {"remaining": site.packs, "packs_remaining": site.packs, "cards": pulled})
                if self.path.startswith("/api/marketplace/") and self.path.endswith("/bid") and self._authed():
                    auction = site.auctions[self.path.split("/")[3]]
                    minimum = auction["base_amount"] if auction["current_bid"] is None else auction["current_bid"] + BID_STEP
                    if body.get("amount", 0) < minimum or auction["status"] != "active":
                        return self._send(400, {"error": "mise trop basse"})
                    site.bids.append((auction["id"], body["amount"]))
                    auction.update(current_bid=body["amount"], effective_bid=body["amount"], current_bidder_id=ME)
                    return self._send(200, {"auction_id": auction["id"], "current_bid": body["amount"],
                                            "bidder_balance": 1747 - body["amount"]})
                if self.path == "/auth/signup":
                    site.signups += 1
                    return self._send(200, {})
                if self.path == "/auth/login":
                    if body.get("mail") == site.mail and body.get("pwd") == site.password:
                        site.logins += 1
                        return self._send(200, {}, headers=[("Set-Cookie", "session=ok; Path=/")])
                    return self._send(400, {"error": "bad credentials"})
                if self.path == "/api/marketplace" and self._authed():
                    if len(site.selling) >= site.max_auctions:
                        return self._send(400, {"error": "Nombre maximum d'enchères actives atteint"})
                    if "base_amount" in body:  # requête directe, comme le vrai site
                        by_card = {c["card"]["id"]: c for c in site.collection}
                        by_copy = {c["id"]: c for c in site.collection}
                        known = by_copy if site.listing_id == "copy" else by_card
                        if body["card_id"] not in known:
                            return self._send(409, {"error": "Vous ne possédez pas cette carte"})
                        site.api_listings.append(body)
                        labels = {10: "10 min", 30: "30 min", 60: "1 h", 360: "6 h", 720: "12 h"}
                        body = {"card_id": body["card_id"], "title": known[body["card_id"]]["card"]["wikipedia_title"],
                                "price": str(body["base_amount"]), "duration": labels[body["duration_minutes"]]}
                    site.listings.append(body)
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
