# wiki-seller

Met automatiquement aux enchères les cartes de votre collection
[WikiMasters](https://www.wiki-masters.com), sauf celles listées dans `safed_cards.json`.

À chaque passe, le script :

0. entre **0 h et 6 h** (heure de Paris), ouvre tous les paquets disponibles (voir
   « Paquets »), puis mise sur les cartes de `wanted_cards.json` (voir « Achats ») ;
1. réutilise la session enregistrée dans `state/`, sinon celle de `SESSION_COOKIES`,
   sinon se connecte avec `MAIL` / `PASSWORD` sur `/login` (voir « Première connexion »).
2. compte les enchères en cours. Le site en autorise 5 à la fois : s'il n'y a plus
   de place, le script s'arrête là.
3. lit la collection, **limitée aux raretés de `SELL_RARITIES`** (par défaut
   légendaires `L` et super rares `SR`), et retire les cartes protégées. Pour une carte protégée,
   **un exemplaire est conservé** et les doublons sont vendus.
4. classe les cartes restantes **de la plus chère à la moins chère**, selon le prix
   moyen du marché.
5. pour chaque carte, tant qu'il reste de la place : ouvre la carte dans
   « Collection », clique sur **« Mettre aux enchères »**, attend la **« Moyenne »**,
   saisit une mise de départ de **70 % du prix moyen arrondi à l'entier inférieur**,
   choisit **1 h** et clique sur **« Lancer l'enchère »**. Une carte sans prix moyen
   (vide ou 0) est ignorée.

En mode `--loop`, la passe suivante démarre **`RUN_INTERVAL` après la dernière mise en
vente** (par défaut la durée des enchères, 1 h). Si toutes les places étaient déjà
prises, elle démarre dès la fin de la première enchère en cours.

## Paquets

Dans la plage `OPEN_PACKS_HOURS` (par défaut `0-6`), chaque passe va dans « Paquets »,
clique sur « Ouvrir », fait défiler les 5 cartes avec la flèche, clique sur « Continuer »
et recommence tant qu'il reste des paquets. Le site en stocke 10 au maximum. En
simulation (`--dry-run`), aucun paquet n'est ouvert.

## Achats (wanted_cards.json)

```json
[
  {"name": "Tour Eiffel", "max_price": 500},
  {"name": "Victor Hugo", "max_price": 120}
]
```

Pour chaque carte, **un seul exemplaire** est acheté :

- si vous avez déjà une enchère sur cette carte dans « Mes enchères », elle est reprise ;
- sinon, le script cherche la carte dans « Marché » et choisit l'enchère la moins chère
  dont la mise minimale ne dépasse pas `max_price` ;
- il clique sur « Miser » avec le montant prérempli par le site (la mise minimale),
  jamais directement votre maximum ;
- s'il est dépassé, il surenchérit à la passe suivante, tant que la limite le permet ;
- la boucle se réveille aussi **`SNIPE_LEAD` secondes (20 par défaut) avant la fin**
  pour surenchérir si besoin, puis surveille l'enchère jusqu'à sa vraie fin (le site la
  prolonge de 60 s après toute mise dans les 10 dernières secondes) ;
- **une seule enchère à la fois par carte** : si vous menez déjà une enchère de cette
  carte dans « Mes enchères », le script la suit et ne mise nulle part ailleurs ;
- une fois la carte gagnée (y compris sur une autre enchère que celle suivie), elle
  n'est plus recherchée : un seul exemplaire est acheté.

Le suivi (enchère choisie, heure de fin, carte obtenue) est dans
`state/wanted_state.json` ; `wanted_cards.json` n'est jamais modifié par le script. Pour
racheter une carte déjà obtenue, supprimez sa ligne de `state/wanted_state.json`. Le
fichier est relu à chaque passe. `MAX_TOTAL_BIDS` limite la somme des mises en cours.
En simulation, aucune mise n'est faite.

## Installation locale

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env                              # puis renseigner MAIL et PASSWORD
cp safed_cards.example.json safed_cards.json      # puis lister vos cartes à garder
```

`.env` et `safed_cards.json` ne sont jamais commités (voir `.gitignore`).

### safed_cards.json

Une liste JSON des noms de cartes à ne pas vendre, tels qu'affichés sur le site :

```json
[
  "Tour Eiffel",
  "Victor Hugo"
]
```

La comparaison ignore les majuscules, les accents et les espaces en trop. Le fichier
est relu à chaque passe : on peut le modifier sans redémarrer le script. S'il est
absent, le script refuse de tourner. Une liste vide `[]` vend tout.

## Première connexion (vérification Cloudflare)

Sur la page `/login`, le bouton « Connexion » reste désactivé tant que la case
Cloudflare « Vérifiez que vous êtes humain » n'est pas validée. Le script ne coche pas
cette case à votre place. Il remplit le formulaire, puis attend :

- sans écran (par défaut), 30 s. Si la vérification ne passe pas toute seule, il
  s'arrête avec le message « Connexion bloquée par la vérification anti-bot Cloudflare » ;
- avec `--headed`, 5 min, le temps que vous cochiez la case dans la fenêtre ouverte.

Une fois connecté, la session est enregistrée dans `state/storage_state.json` et
réutilisée aux passes suivantes. Le site la renouvelle tout seul tant que le script
tourne régulièrement.

### Sur un serveur sans écran : copier la session d'un navigateur

1. Sur votre téléphone ou votre ordinateur, connectez-vous à wiki-masters.com (en
   cochant la case vous-même).
2. Ajoutez un favori dont l'adresse est :

   ```
   javascript:(()=>{const c=document.cookie.split('; ').filter(x=>x.startsWith('sb-')).join('; ');navigator.clipboard.writeText(c).then(()=>alert('Copié : '+c.split('; ').length+' cookie(s)'),()=>prompt('Tout sélectionner et copier :',c))})()
   ```

   puis ouvrez ce favori sur wiki-masters.com : les cookies de session (au moins deux,
   `…-auth-token.0` et `…-auth-token.1`) sont copiés, sur une seule ligne.
3. Sur le serveur, lancez `./update_session.sh`, collez les cookies (une ou plusieurs
   lignes, ils ne s'affichent pas) et validez par une ligne vide. Le script réécrit
   `SESSION_COOKIES` dans `.env`, supprime l'ancienne session de `state/` et relance le
   conteneur (`--no-restart` pour ne pas le relancer). À la main, la ligne s'écrit
   `SESSION_COOKIES=sb-…-auth-token.0=base64-…;sb-…-auth-token.1=…`.
4. **Ne vous déconnectez pas** ensuite sur ce navigateur : « Se déconnecter » ferme
   aussi la session copiée. N'utilisez plus non plus cette session sur le téléphone :
   deux appareils qui la renouvellent chacun de leur côté peuvent la faire annuler.
   Effacez plutôt les données du site (Safari : Réglages → Safari → Avancé → Données
   des sites web) puis reconnectez-vous : vous aurez une session à vous.

Le script n'utilise `SESSION_COOKIES` que s'il n'a pas de session valide dans `state/`.
Si le journal indique « La session de SESSION_COOKIES est expirée », recommencez ces
étapes avec une nouvelle copie.

## Utilisation

```bash
python -m wiki_seller --dry-run     # tout sauf « Lancer l'enchère » : à faire en premier
python -m wiki_seller               # une passe
python -m wiki_seller --loop        # en continu
```

Options : `--headed` affiche le navigateur ; `--debug` enregistre une capture d'écran
et le HTML à chaque étape dans `state/debug/`.

Le journal est écrit dans `state/seller.log`. En cas d'erreur sur une carte, une
capture d'écran et le HTML de la page sont enregistrés dans `state/debug/`.

### Réglages optionnels (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `BASE_URL` | `https://www.wiki-masters.com` | Adresse du site |
| `SAFE_CARDS_FILE` | `safed_cards.json` | Liste des cartes protégées |
| `STATE_DIR` | `state` | Session, journal, captures |
| `PRICE_RATIO` | `0.70` | Part du prix moyen utilisée pour la mise |
| `AUCTION_DURATION_LABEL` | `1 h` | Durée des enchères : libellé exact d'un bouton du site (`10 min`, `30 min`, `1 h`, `3 h`, `6 h`, `12 h`) |
| `RUN_INTERVAL` | *(durée des enchères)* | Délai avant la passe suivante (`10 min`, `1 h`, `1h30`… ; 5 min minimum) |
| `HEADLESS` | `true` | `false` pour voir le navigateur |
| `CHROMIUM_EXECUTABLE` | *(vide)* | Chromium déjà installé à utiliser |
| `SESSION_COOKIES` | *(vide)* | Session copiée d'un navigateur (voir plus haut) |
| `OPEN_PACKS_HOURS` | `0-6` | Heures de Paris où les paquets sont ouverts ; vide = jamais |
| `WANTED_CARDS_FILE` | `wanted_cards.json` | Cartes à acheter |
| `MAX_TOTAL_BIDS` | *(vide)* | Somme maximale des mises en cours |
| `SNIPE_LEAD` | `20` | Secondes avant la fin d'une enchère pour surenchérir |
| `ACTIONS_VIA` | `api` | `api` : paquets, ventes et mises par requêtes directes ; `page` : clics dans les pages (ancien mode) |
| `API_TRACE` | *(vide)* | `1` : noter les appels à l'API du site dans `state/api.log` (sans cookies ni en-têtes) ; équivaut à `--trace-api` |
| `CPU_LIMIT` / `MEM_LIMIT` | `2` / `2g` | Ressources maximales du conteneur |
| `SELL_RARITIES` | `L,SR` | Raretés mises en vente (`L`, `UR`, `SR`, `R`, `PC`, `C`) ; vide = toutes |

## Docker (serveur)

```bash
cp .env.example .env && cp safed_cards.example.json safed_cards.json   # à remplir
cp wanted_cards.example.json wanted_cards.json                          # ou echo '[]' > wanted_cards.json
mkdir -p state
docker compose up -d --build
docker compose logs -f
```

Créez bien `safed_cards.json` et `wanted_cards.json` **avant** le premier `up`. Sinon Docker crée un
dossier à sa place.

Le conteneur tourne en mode `--loop` et redémarre tout seul. Il est limité à 2 cœurs et
2 Go de RAM (`CPU_LIMIT` et `MEM_LIMIT` dans `.env`). `safed_cards.json` est
monté depuis le serveur : il suffit de le modifier, sans reconstruire l'image.

Le `.env`, lui, n'est lu qu'à la création du conteneur. Après l'avoir modifié, lancez
`docker compose up -d` (et non `docker compose restart`, qui garde les anciennes
valeurs). La session, le cache des prix et l'heure de la dernière vente sont conservés
dans `state/`.

## Fonctionnement technique

- **Lectures** (collection, enchères en cours, prix moyens pour le classement) : le
  script utilise l'API JSON que le site appelle lui-même (`/api/my-collection`,
  `/api/marketplace?mine=1`, `/api/marketplace/cards/<id>/sales?scope=summary`),
  depuis la page connectée.
- **Actions** (par défaut, `ACTIONS_VIA=api`) : les requêtes que la page envoie elle-même,
  depuis la page connectée : `POST /api/packs/open` (sans corps, renvoie les 5 cartes),
  `POST /api/marketplace` (`card_id`, `base_amount`, `duration_minutes`) et
  `POST /api/marketplace/<id>/bid` (`amount`, lu dans le champ prérempli de la page de
  l'enchère). Une carte sans prix moyen côté API passe par la fenêtre d'enchère.
- **Mise en vente avec `ACTIONS_VIA=page`** : le script passe par l'interface, comme à la main.
  Le prix de la mise est calculé à partir de la « Moyenne » affichée dans la fenêtre
  d'enchère. Le prix moyen de l'API ne sert qu'au classement, et de secours si le
  libellé est absent.
- Les libellés utilisés sont regroupés en haut de `wiki_seller/site.py`, pour les
  ajuster si le site change : « Mettre aux enchères », « Moyenne »,
  « Mise de départ », « 1 h », « Lancer l'enchère », « Enchères actives : X/5 ».

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_end_to_end.py` lance un vrai Chromium contre une imitation locale du site
(`tests/fake_site.py`) : connexion, popup anti-bot, collection, fenêtre d'enchère.
