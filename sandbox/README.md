# Banc d'essai Turnstile (sandbox)

Un mini-site local qui protège l'ouverture de paquets avec **Cloudflare Turnstile**,
exactement comme WikiMasters, mais avec les **clés de test officielles** de Cloudflare
(<https://developers.cloudflare.com/turnstile/troubleshooting/testing/>).
Il sert à comprendre et à tester le parcours « vérification humaine », sans jamais
toucher au vrai site.

## Lancer

```bash
# Terminal 1 : le site (stdlib uniquement)
python sandbox/server.py                               # mode pass (par défaut)
TURNSTILE_MODE=block python sandbox/server.py          # vérification toujours refusée
TURNSTILE_MODE=interactive python sandbox/server.py    # case à cocher forcée

# Terminal 2 : le client Playwright
CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-*/chrome-linux/chrome \
  python sandbox/client.py --packs 5
python sandbox/client.py --packs 5 --headed --wait 300   # mode interactive : vous cochez
```

Variables : `TURNSTILE_MODE`, `VERIFY_EVERY` (ouvertures accordées par vérification,
3 par défaut), `PORT` (8765), `SANDBOX_URL` côté client.

## Les trois modes

| Mode          | Clé de site                | Effet                                                    |
|---------------|----------------------------|----------------------------------------------------------|
| `pass`        | `1x00000000000000000000AA` | le widget se valide seul en ~2 s (jeton de test factice) |
| `block`       | `2x00000000000000000000AB` | le widget échoue toujours, le client abandonne           |
| `interactive` | `3x00000000000000000000FF` | une case à cocher apparaît : il faut un humain           |

## Le parcours (identique à celui observé sur WikiMasters)

```
POST /api/packs/open ──► 403 {"human_verification_required": true}
        │
        ▼
page : fenêtre « Vérification rapide » + turnstile.render(sitekey)
        │  le widget produit un jeton (cf-turnstile-response)
        ▼
POST /api/verify {token} ──► serveur → siteverify(secret, token) chez Cloudflare
        │  success: true  → la session peut ouvrir VERIFY_EVERY paquets
        ▼
POST /api/packs/open ──► 200 {cards: [...]}
```

Points clés :
- **C'est le serveur qui décide**, via `siteverify` et sa clé secrète. La page ne fait
  que transporter le jeton.
- Un jeton est **à usage unique** et expire au bout de 300 s.
- Le mode `pass` passe tout seul **uniquement parce que ce sont des clés de test**.
  Avec de vraies clés, Cloudflare évalue le navigateur. Un navigateur automatisé,
  headless ou en datacenter, se voit en général proposer la case ou être refusé.

Exemple de sortie (mode `pass`) :

```
[client] Vérification demandée par le site ; attente (au plus 40 s)…
[client] Vérification faite en 2.3 s.
[client] Paquet 1 ouvert ; reste 9.
...
[serveur] POST /api/packs/open → 403
[serveur] POST /api/verify → 200
[serveur] POST /api/packs/open → 200
```

## Porter dans le projet principal

Ce qui se porte, c'est l'**architecture « humain dans la boucle »**, pas un passage
automatique. Sur le vrai WikiMasters, une personne doit faire la vérification. Ce
projet ne contient et ne contiendra pas de code qui résout ou contourne le défi.

Aujourd'hui, `wiki_seller/main.py::_open_packs` attrape `HumanVerificationRequired`
et met les paquets en pause 12 h (`state/packs_pause.json`). Pour remplacer cette
pause par « je préviens et j'attends », il faut quatre briques :

1. **Détection** : elle existe déjà. `site.open_packs_api` lève
   `HumanVerificationRequired` sur le 403.
2. **Un navigateur visible à distance** : l'image Docker lance Chromium en
   `headless=False` sous **Xvfb** et expose l'écran avec **x11vnc + noVNC**
   (port 6080, protégé par mot de passe ou derrière un tunnel SSH ou Tailscale).
   Paquets apt : `xvfb x11vnc novnc`.
3. **Une notification** : un `POST` vers ntfy (`https://ntfy.sh/<sujet>`) ou
   Telegram, avec le lien noVNC. Par exemple
   `requests.post(NTFY_URL, data="Vérification WikiMasters à faire : http://serveur:6080")`.
4. **L'attente** : sur `/pulls`, cliquer « Ouvrir » pour afficher la fenêtre, puis
   appeler l'équivalent de `sandbox/client.py::wait_for_verification`. Elle attend
   que la fenêtre disparaisse, avec un délai maximal (par exemple 15 min). Si
   personne ne vient, on retombe sur la pause actuelle. Si c'est fait, on reprend
   `open_packs_api` : la session est vérifiée pour quelques ouvertures.

Esquisse (dans `main.py`) :

```python
except HumanVerificationRequired:
    if settings.packs_verify == "vnc":          # nouvelle option PACKS_VERIFY
        notify(settings.ntfy_url, "Vérification requise : " + settings.novnc_url)
        page.goto(PULLS_URL); page.get_by_role("button", name="Ouvrir").click()
        if wait_for_verification(page, timeout_s=900):   # un humain a coché
            return open_packs_api(page, ...)             # on reprend
    pause_packs(PACKS_VERIFICATION_PAUSE)                # sinon, comportement actuel
```

À prévoir : `headless=False` seulement quand `PACKS_VERIFY=vnc`, augmenter `shm_size`,
et ne jamais exposer noVNC sur Internet sans authentification. L'écran donne accès
à votre compte.
