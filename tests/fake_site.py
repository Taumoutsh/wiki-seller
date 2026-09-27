"""Faux wiki-masters.com minimal pour tester le parcours complet en local.

Il reproduit les éléments utilisés par le script : page /login dont le bouton attend
une vérification anti-bot, popup anti-bot avec case à cocher, API JSON, page /collection avec
recherche, tuiles, fenêtre « Mettre aux enchères » et « Prix moyen » chargé en différé."""

import json
import threading
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
    <div><span>Prix moyen</span> <strong id="avg">...</strong></div>
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


class FakeWikiMasters:
    def __init__(self, collection, averages, ui_averages=None, selling=None, max_auctions=5,
                 mail="me@example.com", password="secret", sales_forbidden=False,
                 challenge="auto"):
        self.collection = collection
        self.averages = averages  # card_id -> moyenne API
        self.ui_averages = ui_averages if ui_averages is not None else {}
        self.selling = list(selling or [])
        self.max_auctions = max_auctions
        self.mail, self.password = mail, password
        self.sales_forbidden = sales_forbidden
        self.listings = []
        self.challenge = challenge
        self.logins = 0
        self.signups = 0
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
                if url.path.startswith("/marketplace/"):
                    return self._send(200, "<html><body>Enchère créée</body></html>", "text/html")
                if not url.path.startswith("/api/"):
                    return self._send(404, "<html>404</html>", "text/html")
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if url.path == "/api/wikibidous":
                    return self._send(200, {"balance": 100})
                if url.path == "/api/my-collection":
                    page = int(qs.get("page", ["0"])[0])
                    return self._send(200, {"collection": site.collection if page == 0 else []})
                if url.path == "/api/marketplace" and qs.get("mine") == ["1"]:
                    return self._send(200, {"selling": site.selling, "bidding": [],
                                            "maxConcurrentAuctions": site.max_auctions})
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
                        return self._send(400, {"error": "quota"})
                    site.listings.append(body)
                    auction = {"id": f"a{len(site.listings)}", "card_id": body["card_id"], "status": "active",
                               "end_at": "2030-01-01T10:00:00Z"}
                    site.selling.append(auction)
                    return self._send(200, {"auction_id": auction["id"]})
                return self._send(404, {})

        return Handler
