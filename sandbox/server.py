"""Banc d'essai : un mini-site qui protège l'ouverture de paquets avec Cloudflare
Turnstile, comme WikiMasters, mais avec les CLÉS DE TEST officielles de Cloudflare.
Rien de réel n'est protégé : c'est fait pour comprendre et tester le parcours.

    python sandbox/server.py                     # clé de test qui valide toujours
    TURNSTILE_MODE=interactive python sandbox/server.py   # défi interactif forcé
    TURNSTILE_MODE=block python sandbox/server.py         # vérification toujours refusée

Puis ouvrir http://127.0.0.1:8765/ dans un navigateur, ou lancer sandbox/client.py.

Parcours (le même que celui observé sur WikiMasters) :
1. POST /api/packs/open → 403 {"human_verification_required": true} tant que la
   session n'est pas vérifiée (ou après VERIFY_EVERY ouvertures).
2. La page affiche « Vérification rapide » avec le widget Turnstile.
3. Le widget produit un jeton ; la page l'envoie à POST /api/verify.
4. Le serveur vérifie le jeton auprès de Cloudflare (siteverify, avec la clé secrète)
   et, si c'est bon, lève la vérification pour VERIFY_EVERY ouvertures.
5. La page réessaie POST /api/packs/open.
"""

import json
import os
import random
import secrets
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Clés de test publiées par Cloudflare (developers.cloudflare.com/turnstile/troubleshooting/testing/).
TEST_KEYS = {
    "pass": ("1x00000000000000000000AA", "1x0000000000000000000000000000000AA"),         # valide toujours
    "block": ("2x00000000000000000000AB", "2x0000000000000000000000000000000AA"),        # refuse toujours
    "interactive": ("3x00000000000000000000FF", "1x0000000000000000000000000000000AA"),  # force le défi
}
MODE = os.getenv("TURNSTILE_MODE", "pass")
SITE_KEY, SECRET_KEY = TEST_KEYS[MODE]
VERIFY_EVERY = int(os.getenv("VERIFY_EVERY", "3"))  # ouvertures autorisées après une vérification
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
PORT = int(os.getenv("PORT", "8765"))

sessions: dict[str, dict] = {}  # sid → {"packs": n, "allowance": ouvertures restantes}

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Banc d'essai Turnstile</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
<style>body{font-family:sans-serif;background:#111;color:#eee;text-align:center}
#modal{display:none;position:fixed;inset:0;background:#000a;align-items:center;justify-content:center}
#modal>div{background:#222;padding:24px;border-radius:12px}</style></head><body>
<h1>Ouvrir un paquet</h1>
<button id="open"><span>Ouvrir</span></button>
<p><span id="count">__PACKS__</span> / 10 paquets disponibles</p>
<pre id="log"></pre>
<div id="modal"><div><h2>Vérification rapide</h2><p>On vérifie que tu es bien humain.</p>
<div id="widget"></div><button id="cancel">Annuler</button></div></div>
<script>
const log = (t) => document.getElementById('log').textContent = t + '\\n' + document.getElementById('log').textContent;
const modal = document.getElementById('modal');
async function openPack() {
  const r = await fetch('/api/packs/open', {method: 'POST'});
  const d = await r.json();
  if (r.status === 403 && d.human_verification_required) return verify();
  if (!r.ok) return log('Refusé : ' + d.error);
  document.getElementById('count').textContent = d.packs_remaining;
  log('Paquet ouvert : ' + d.cards.map(c => c.rarity + ' ' + c.title).join(', '));
}
function verify() {
  modal.style.display = 'flex';
  document.getElementById('widget').innerHTML = '';
  turnstile.render('#widget', {
    sitekey: '__SITE_KEY__',
    callback: async (token) => {
      const r = await fetch('/api/verify', {method: 'POST', headers: {'content-type': 'application/json'},
                                            body: JSON.stringify({token})});
      modal.style.display = 'none';
      if (r.ok) { log('Vérification acceptée par le serveur.'); openPack(); }
      else log('Vérification refusée : ' + (await r.json()).error);
    },
    'error-callback': () => log('Erreur du widget Turnstile.'),
  });
}
document.getElementById('open').onclick = openPack;
document.getElementById('cancel').onclick = () => modal.style.display = 'none';
</script></body></html>"""


def siteverify(token: str, ip: str) -> dict:
    """Vérification du jeton côté serveur : c'est elle qui fait foi, jamais la page."""
    data = urllib.parse.urlencode({"secret": SECRET_KEY, "response": token, "remoteip": ip}).encode()
    with urllib.request.urlopen(urllib.request.Request(SITEVERIFY, data=data), timeout=10) as res:
        return json.load(res)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[serveur] {self.command} {self.path} → {args[1] if len(args) > 1 else ''}")

    def _session(self) -> tuple[str, dict]:
        cookie = self.headers.get("Cookie") or ""
        sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("sid=")), None)
        if sid not in sessions:
            sid = secrets.token_hex(8)
            sessions[sid] = {"packs": 10, "allowance": 0}
        return sid, sessions[sid]

    def _send(self, status: int, body, sid: str, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        sid, session = self._session()
        if self.path in ("/", "/pulls"):
            html = PAGE.replace("__SITE_KEY__", SITE_KEY).replace("__PACKS__", str(session["packs"]))
            return self._send(200, html, sid, "text/html; charset=utf-8")
        return self._send(404, {"error": "introuvable"}, sid)

    def do_POST(self):
        sid, session = self._session()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/packs/open":
            if session["allowance"] <= 0:
                return self._send(403, {"error": "Vérification anti-bot requise pour continuer à ouvrir des paquets.",
                                        "human_verification_required": True,
                                        "code": "human_verification_required"}, sid)
            if session["packs"] <= 0:
                return self._send(400, {"error": "Aucun paquet disponible", "packs_remaining": 0}, sid)
            session["packs"] -= 1
            session["allowance"] -= 1
            cards = [{"title": f"Carte {random.randint(1, 999)}",
                      "rarity": random.choice(["C", "C", "C", "PC", "R", "SR", "UR", "L"])} for _ in range(5)]
            return self._send(200, {"cards": cards, "packs_remaining": session["packs"]}, sid)
        if self.path == "/api/verify":
            try:
                result = siteverify(str(body.get("token") or ""), self.client_address[0])
            except Exception as exc:
                return self._send(502, {"error": f"siteverify injoignable : {exc}"}, sid)
            if not result.get("success"):
                return self._send(403, {"error": f"jeton refusé {result.get('error-codes')}"}, sid)
            session["allowance"] = VERIFY_EVERY
            return self._send(200, {"ok": True, "allowance": VERIFY_EVERY,
                                    "testing_key": result.get("metadata", {}).get("result_with_testing_key")}, sid)
        return self._send(404, {"error": "introuvable"}, sid)


if __name__ == "__main__":
    print(f"Banc d'essai sur http://127.0.0.1:{PORT}/ — mode {MODE} (clé de site {SITE_KEY}), "
          f"vérification toutes les {VERIFY_EVERY} ouvertures.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
