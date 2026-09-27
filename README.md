# wiki-seller

Met automatiquement aux enchères les cartes de votre collection
[WikiMasters](https://www.wiki-masters.com), sauf celles listées dans `safed_cards.json`.

À chaque passe, le script :

1. se connecte avec `MAIL` / `PASSWORD` sur la page `/login` (voir « Première
   connexion » ci-dessous), puis réutilise la session enregistrée dans `state/`.
2. compte les enchères en cours. Le site en autorise 5 à la fois : s'il n'y a plus
   de place, le script s'arrête là.
3. lit la collection et retire les cartes protégées. Pour une carte protégée,
   **un exemplaire est conservé** et les doublons sont vendus.
4. classe les cartes restantes **de la plus chère à la moins chère**, selon le prix
   moyen du marché.
5. pour chaque carte, tant qu'il reste de la place : ouvre la carte dans
   « Collection », clique sur **« Mettre aux enchères »**, attend le **« Prix moyen »**,
   saisit une mise de départ de **70 % du prix moyen arrondi à l'entier inférieur**,
   choisit **1 h** et clique sur **« Lancer l'enchère »**. Une carte sans prix moyen
   (vide ou 0) est ignorée.

En mode `--loop`, la passe suivante démarre **1 h après la dernière mise en vente**.
Si toutes les places étaient déjà prises, elle démarre dès la fin de la première
enchère en cours.

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
réutilisée aux passes suivantes. Sur un serveur sans écran, faites cette première
connexion sur votre ordinateur (`python -m wiki_seller --dry-run --headed`), puis copiez
`state/storage_state.json` dans le dossier `state/` du serveur. Recommencez si la
session expire.

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
| `AUCTION_DURATION_LABEL` | `1 h` | Libellé du bouton de durée |
| `HEADLESS` | `true` | `false` pour voir le navigateur |
| `CHROMIUM_EXECUTABLE` | *(vide)* | Chromium déjà installé à utiliser |

## Docker (serveur)

```bash
cp .env.example .env && cp safed_cards.example.json safed_cards.json   # à remplir
mkdir -p state
docker compose up -d --build
docker compose logs -f
```

Créez bien `safed_cards.json` **avant** le premier `up`. Sinon Docker crée un
dossier à sa place.

Le conteneur tourne en mode `--loop` et redémarre tout seul. `safed_cards.json` est
monté depuis le serveur : il suffit de le modifier, sans reconstruire l'image.

## Fonctionnement technique

- **Lectures** (collection, enchères en cours, prix moyens pour le classement) : le
  script utilise l'API JSON que le site appelle lui-même (`/api/my-collection`,
  `/api/marketplace?mine=1`, `/api/marketplace/cards/<id>/sales?scope=summary`),
  depuis la page connectée.
- **Mise en vente** : le script passe uniquement par l'interface, comme à la main.
  Le prix de la mise est calculé à partir du « Prix moyen » affiché dans la fenêtre
  d'enchère. Le prix moyen de l'API ne sert qu'au classement, et de secours si le
  libellé est absent.
- Les libellés utilisés sont regroupés en haut de `wiki_seller/site.py`, pour les
  ajuster si le site change : « Mettre aux enchères », « Prix moyen »,
  « Mise de départ », « 1 h », « Lancer l'enchère », « Enchères actives : X/5 ».

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_end_to_end.py` lance un vrai Chromium contre une imitation locale du site
(`tests/fake_site.py`) : connexion, popup anti-bot, collection, fenêtre d'enchère.
