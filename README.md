# wiki-seller

Automatise votre compte [WikiMasters](https://www.wiki-masters.com) : mise aux enchères
des cartes de valeur, achat des cartes voulues, défausse des cartes sans valeur, et un
bot Telegram pour la session et les bilans.

À chaque passe, le script :

1. se connecte (voir « Connexion ») ;
2. mise sur les cartes à acheter (`/buy`, voir « Achats ») ;
3. met en vente, s'il reste de la place (le site autorise 5 enchères à la fois),
   **d'abord les cartes demandées** (`/sell`, voir « Ventes à prix exact »), puis les
   cartes des raretés `SELL_RARITIES` (par défaut légendaires `L` et super rares `SR`),
   **de la plus chère à la moins chère**, sauf les cartes protégées (`/safe` : un
   exemplaire est conservé et les doublons sont vendus). Mise de
   départ : **70 % du prix moyen du marché**, arrondie à l'entier inférieur et ajustée
   d'après les enchères en cours de la même carte (`MARKET_ADJUST`), pour **1 h**. Une
   carte sans prix moyen est ignorée ; une carte dont le prix n'a pas pu être lu
   attend la passe suivante ;
4. lance la défausse en arrière-plan (voir « Défausse automatique ») ;
5. envoie le bilan Telegram si son heure est venue (voir « Bot Telegram »).

En mode `--loop`, la passe suivante démarre **`RUN_INTERVAL` après la dernière mise en
vente** (par défaut la durée des enchères, 1 h). Si toutes les places étaient prises,
elle démarre dès la fin de la première enchère en cours, et au plus tard `RUN_INTERVAL`
plus tard (les ventes de nuit durent jusqu'à 12 h).

Toutes les actions passent par l'API du site, comme la page elle-même : aucune
vérification humaine n'est jamais contournée.

## Défausse automatique (arrière-plan)

À chaque réveil (passe ou surenchère), un **thread à part** est lancé, sauf s'il tourne
déjà ou s'est terminé il y a moins d'une heure (15 min si des prix n'avaient pas pu
être lus). Il lit toute la collection et compte les exemplaires. Au-delà de
**`DISCARD_TRIGGER` (150)**, il examine les cartes **des plus communes aux plus rares**
(`C`, `PC`, `R`, `SR`, `UR`, `L`) : il lit le prix de chacune (cache, sinon le site) et
la **défausse aussitôt** si elle vaut moins de **`DISCARD_BELOW` (15)**, un exemplaire
à la fois (`POST /api/user-cards/{exemplaire}/discard`, 1 wikibidou chacun).

- **Défaussées :** prix moyen strictement inférieur à 15, ou aucune vente connue pour
  une commune (`C`) ou peu commune (`PC`). Tous les exemplaires, y compris le dernier.
- **Jamais défaussées :** les cartes protégées, à acheter et à vendre (voir « Listes de
  cartes »), les exemplaires en vente ou en échange, les cartes rares sans vente connue, et celles
  dont le prix n'a pas pu être lu (redemandées la fois suivante).
- **Une session à la fois :** la défausse et les passes partagent la session du site ;
  un seul navigateur est ouvert à la fois. Quand une passe se réveille, la défausse lui
  laisse la place après la page, le prix ou l'exemplaire en cours, puis reprend où elle
  en était.
- **Reprise :** l'avancement est gardé dans `state/discard_progress.json` (24 h au plus),
  même après un redémarrage. Supprimez ce fichier pour repartir de zéro.
- **Journal :** une ligne par exemplaire défaussé, et toutes les 100 cartes examinées un
  point d'étape avec le temps moyen par prix et le nombre de refus du site.

`DISCARD_BELOW=0` désactive la défausse. En `--dry-run`, rien n'est envoyé.

## Prix moyens

Les prix viennent de `/api/marketplace/cards/<id>/sales?scope=summary`, une requête par
carte, gardés `PRICE_CACHE_HOURS` (24 h) dans `state/price_cache.json`, partagé par la
vente et la défausse. Un refus passager du site (trop de requêtes, panne) ne fait
laisser de côté que la carte concernée, après 30 s de pause ; l'API n'est abandonnée
qu'après 5 refus de suite, ou dès un accès interdit.

## Listes de cartes

Trois listes, dans `state/`, modifiées par le bot Telegram (voir « Commandes ») et
relues à chaque passe :

| Fichier | Commandes | Contenu |
|---|---|---|
| `state/safe_cards.json` | `/safe`, `/unsafe` | Cartes protégées : jamais défaussées, un exemplaire toujours gardé |
| `state/buy_cards.json` | `/buy`, `/buyandsell`, `/unbuy` | Cartes à acheter, avec leur prix maximum |
| `state/sell_cards.json` | `/sell`, `/unsell` | Cartes à vendre à un prix exact |

`/list` les affiche. Les noms sont comparés sans majuscules, accents ni espaces en trop.
Sans liste de cartes protégées, aucune passe ne tourne (le script vendrait tout) :
envoyez `/safe <carte>`, ou écrivez `[]` dans `state/safe_cards.json` pour tout vendre.

Hors Docker, les anciens `safed_cards.json` et `wanted_cards.json` sont repris dans
`state/` au premier démarrage ; ils ne servent plus ensuite (le journal et le bot le
rappellent). Le conteneur ne les monte plus.

## Achats

`/buy Tour Eiffel 500` : achète la carte à 500 au plus ; `/buy Victor Hugo 120 x2` pour
2 exemplaires, l'un après l'autre. Pour chaque carte :

- si vous avez déjà une enchère sur cette carte dans « Mes enchères », elle est reprise ;
- sinon, le script cherche la carte dans le Marché et choisit l'enchère la moins chère
  dont la mise minimale ne dépasse pas le prix maximum ;
- il mise la mise minimale (lue sur la page de l'enchère), jamais directement votre
  maximum ;
- s'il est dépassé, il **ne surenchérit pas tout de suite** : si la nouvelle mise
  minimale reste dans la limite, il attend la fin de l'enchère ; sinon il en cherche
  une autre ;
- la boucle se réveille pour la **dernière minute** de l'enchère, la relit **chaque
  seconde** et, si vous n'êtes plus en tête, surenchérit à **`SNIPE_LEAD` secondes (30)
  de la fin**, dans la limite, puis **à chaque fois que vous êtes dépassé** ; une mise
  des 10 dernières secondes prolonge l'enchère de 60 s : la surveillance continue
  jusqu'à la vraie fin ;
- **une seule enchère à la fois par carte**.

Le site ne répond pas toujours pendant les enchères. Une mise sans réponse (au bout de
8 s, ou erreur du site) est vérifiée sur l'enchère : si elle est passée malgré tout, elle
n'est pas renvoyée ; sinon elle repart chaque seconde, jusqu'à la fin de l'enchère (au
plus 2 min pendant une passe). Une lecture de l'enchère sans réponse est refaite la
seconde suivante, sans interrompre la surveillance.

Chaque exemplaire obtenu est annoncé par le bot (« 🛒 Obtenue : Tour Eiffel pour 480 »).
Une fois tous les exemplaires obtenus, la carte **quitte la liste d'achat et devient
protégée**. Avec `/buyandsell Tour Eiffel 500 800`, elle part à la place dans les
ventes à 800 (voir ci-dessous). Le suivi des enchères est dans `state/wanted_state.json`.
`MAX_TOTAL_BIDS` limite la somme des mises en cours.

## Ventes à prix exact

`/sell Tour Eiffel 800` met la carte en vente **à 800 exactement** (ni `PRICE_RATIO`, ni
ajustement au marché), quelle que soit sa rareté, pour la durée habituelle (nuit
comprise) ; `/sell Victor Hugo 120 x2` pour 2 exemplaires.

- Ces cartes passent **en priorité** dans les places libres, dès la passe suivante ; les
  enchères en cours ne sont pas annulées.
- Une enchère terminée sans acheteur est **relancée au même prix** à chaque passe,
  jusqu'à la vente (la mise de côté des invendues ne s'applique pas).
- Chaque vente est annoncée (« 💰 Vendue : Tour Eiffel à 800 ») ; une fois tous les
  exemplaires vendus, la carte quitte la liste.
- **Carte protégée :** en un seul exemplaire, elle n'est plus protégée et part ; en
  plusieurs, elle reste protégée et le dernier exemplaire est gardé.
- Une carte absente de la collection est signalée une fois et reste dans la liste
  (`/unsell` pour l'annuler). Ces cartes ne sont jamais défaussées.

## Connexion

Le script essaie, dans l'ordre : la session enregistrée (`state/storage_state.json`, que
le site renouvelle tout seul), la dernière session reçue par Telegram, `SESSION_COOKIES`,
puis `MAIL`/`PASSWORD` sur `/login`. Sur `/login`, « Connexion » reste désactivé tant que
la case Cloudflare « Vérifiez que vous êtes humain » n'est pas validée : le script ne la
coche pas. Il attend 30 s qu'elle passe seule (5 min avec `--headed`, le temps de la
cocher vous-même), puis demande une session par Telegram si le bot est configuré.

### Copier la session d'un navigateur

1. Sur votre téléphone ou votre ordinateur, connectez-vous à wiki-masters.com.
2. Ajoutez un favori dont l'adresse est :

   ```
   javascript:(()=>{const c=document.cookie.split('; ').filter(x=>x.startsWith('sb-')).join('; ');navigator.clipboard.writeText(c).then(()=>alert('Copié : '+c.split('; ').length+' cookie(s)'),()=>prompt('Tout sélectionner et copier :',c))})()
   ```

   puis ouvrez-le sur wiki-masters.com : les cookies de session (`…-auth-token.0` et
   `…-auth-token.1`) sont copiés sur une seule ligne.
3. Envoyez cette ligne au bot Telegram quand il la demande, ou écrivez-la dans `.env`
   (`SESSION_COOKIES=sb-…-auth-token.0=base64-…; sb-…-auth-token.1=…`), supprimez
   `state/storage_state.json` et relancez le conteneur (`docker compose up -d`).
4. **Ne vous déconnectez pas** ensuite sur ce navigateur (cela fermerait aussi la
   session copiée), et ne l'utilisez plus sur un autre appareil : deux appareils qui la
   renouvellent chacun de leur côté peuvent la faire annuler.

## Bot Telegram

1. Dans Telegram, écrivez à **@BotFather** : `/newbot`, choisissez un nom ; il vous
   donne le jeton du bot (`123456:ABC…`) → `TELEGRAM_BOT_TOKEN` dans `.env`.
2. Écrivez n'importe quoi à votre bot, puis ouvrez
   `https://api.telegram.org/bot<JETON>/getUpdates` : le nombre après `"chat":{"id":`
   est votre identifiant → `TELEGRAM_CHAT_ID` dans `.env`. Relancez le conteneur.

Seuls les messages de `TELEGRAM_CHAT_ID` sont lus. Les cookies transitent par les
serveurs de Telegram : gardez le bot pour vous seul.

**Session expirée :** le bot vous envoie le lien du site et la marche à suivre ; vous
lui répondez avec la ligne copiée par le favori. Il **supprime aussitôt votre message**,
essaie la session et confirme (✅) ou en demande une autre (❌). Le script attend
`TELEGRAM_WAIT` (60 min), puis réessaie au réveil suivant, sans nouveau message avant
3 h. La session acceptée est gardée dans `state/session_cookies.txt`.

**Bilan toutes les 2 h** (`REPORT_HOURS`, `0` pour le couper) :

```
📊 Bilan · 14:00 → 16:02
👛 1 234 wikibidous  (+207 depuis le dernier bilan)

💰 Ventes · 2 cartes · +87
• Loire · 40
• Rhône · 47

🗑️ Défausses · 120 exemplaires · +120

🛒 Achats en cours
✅ Tour Eiffel
   en tête à 300 · max 500 · fin 18:42
⚠️ Victor Hugo
   dépassé : 80 → 100 · max 120 · fin 19:05
   ↳ surenchère à 110 juste avant la fin
❌ Loire
   dépassé : 90 → 100 · max 100
   ↳ abandonnée (au-dessus du max)
```

- **En haut :** le solde et le gain depuis le dernier bilan automatique ; « Rien de neuf »
  s'il ne s'est rien passé. Les sections vides sont omises.
- **Ventes :** prix finaux de vos enchères conclues (historique du site), 30 au plus.
- **Défausses :** 1 wikibidou par exemplaire (compteur dans `state/report.json`).
- **Achats en cours :** les enchères des cartes à acheter (`/buy`) : ✅ vous menez ;
  ⚠️ dépassé (votre mise → prix actuel), surenchère prévue juste avant la fin, avec son
  montant une fois le pas du site appris ; ❌ abandonnée, au-dessus du max. Fin : l'heure
  seule si c'est aujourd'hui.

Le bilan est mis en forme (titres en gras) ; si Telegram refuse la mise en forme, il part
en texte brut. Pas de bilan en `--dry-run`.

**Commandes** (en mode `--loop`) :

| Commande | Effet |
|---|---|
| `/ventes` (ou `/historique`) | Vos enchères terminées telles que le site les renvoie (les 40 plus récentes) : ✅ vendue, ❌ invendue, et la période couverte |
| `/update` (ou `/bilan`) | Bilan immédiat depuis le dernier bilan automatique, sans changer le rythme des 2 h ; juste avant un réveil (moins de 90 s), il part juste après |
| `/list` | Cartes protégées, à acheter (avec les exemplaires obtenus) et à vendre |
| `/safe Tour Eiffel` · `/unsafe Tour Eiffel` | Protège la carte (jamais défaussée, toujours un exemplaire gardé), ou la retire des protégées. Une défausse en cours la respecte aussitôt |
| `/buy Tour Eiffel 500` · `/unbuy Tour Eiffel` | Achète la carte à 500 au plus (ou met ce prix à jour) ; `x2` pour 2 exemplaires. Protégée une fois obtenue |
| `/sell Tour Eiffel 800` · `/unsell Tour Eiffel` | Vend la carte à 800 exactement, en priorité, jusqu'à la vente ; `x2` pour 2 exemplaires |
| `/buyandsell Tour Eiffel 500 800` | Achète la carte à 500 au plus, puis la revend à 800 ; `x2` pour 2 exemplaires |

Les derniers nombres sont toujours les prix : `/buy Apollo 11 500` vise « Apollo 11 »
à 500 au plus. Les modifications comptent dès la passe suivante. `/want` est l'ancien
nom de `/buy`, encore accepté pour l'instant. Tout autre message reçoit la liste des
commandes.

## Utilisation

```bash
python -m wiki_seller --dry-run     # tout simuler : à faire en premier
python -m wiki_seller               # une passe (attend la fin de la défausse)
python -m wiki_seller --loop        # en continu
```

`--headed` affiche le navigateur ; `--debug` enregistre une capture d'écran et le HTML à
chaque étape dans `state/debug/`. Le journal est dans `state/seller.log`.

### Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `MAIL` / `PASSWORD` | *(requis)* | Identifiants du site |
| `SESSION_COOKIES` | *(vide)* | Session copiée d'un navigateur (voir « Connexion ») |
| `SELL_RARITIES` | `L,SR` | Raretés mises en vente (`L`, `UR`, `SR`, `R`, `PC`, `C`) ; vide = toutes |
| `PRICE_RATIO` | `0.70` | Part du prix moyen utilisée pour la mise de départ |
| `MARKET_ADJUST` | `true` | Ajuste la mise d'après les enchères en cours de la même carte : sous leur moyenne, + un écart type ; au-dessus, − un écart type ; moins de 2 enchères : inchangée |
| `PRICE_FLOOR_RATIO` | `0.5` | Plancher de l'ajustement : jamais sous cette part du prix moyen |
| `AUCTION_DURATION_LABEL` | `1 h` | Durée des enchères (`10 min`, `30 min`, `1 h`, `3 h`, `6 h`, `12 h`) |
| `NIGHT_DURATIONS` | `1-3=12 h;3-6=6 h` | Durée des ventes selon l'heure de Paris ; vide = jamais |
| `RUN_INTERVAL` | *(durée des enchères)* | Délai avant la passe suivante (`10 min`, `1 h`, `1h30`… ; 5 min minimum) |
| `UNSOLD_MAX_TRIES` / `UNSOLD_COOLDOWN_DAYS` | `3` / `5` | Carte invendue N fois : mise de côté pendant N jours (`state/unsold.json`) |
| `PRICE_CACHE_HOURS` | `24` | Validité d'un prix moyen en cache |
| `DISCARD_BELOW` | `15` | Défausse des cartes valant moins que ce prix moyen ; `0` = désactivée |
| `DISCARD_TRIGGER` | `150` | Exemplaires dans la collection au-delà desquels la défausse est lancée |
| `MAX_TOTAL_BIDS` | *(vide)* | Somme maximale des mises en cours |
| `SNIPE_LEAD` | `30` | Secondes avant la fin d'une enchère pour surenchérir |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(vide)* | Bot Telegram (voir « Bot Telegram ») |
| `TELEGRAM_WAIT` | `60` | Minutes d'attente de votre réponse quand la session a expiré |
| `REPORT_HOURS` | `2` | Bilan Telegram toutes les N heures ; `0` = jamais |
| `STATE_DIR` | `state` | Dossier des fichiers d'état et des listes de cartes |
| `BASE_URL` | `https://www.wiki-masters.com` | Adresse du site |
| `HEADLESS` / `CHROMIUM_EXECUTABLE` | `true` / *(vide)* | Navigateur visible, Chromium déjà installé |
| `CPU_LIMIT` / `MEM_LIMIT` | `2` / `2g` | Ressources maximales du conteneur |

Les anciennes options `ACTIONS_VIA`, `OPEN_PACKS_HOURS`, `PACKS_VERIFY_WAIT`,
`NOTIFY_URL` et `API_TRACE` n'ont plus d'effet ; le journal le signale au démarrage.

## Docker (serveur)

```bash
# créer .env avec au moins MAIL et PASSWORD
mkdir -p state
docker compose up -d --build
docker compose logs -f
```

Le conteneur tourne en `--loop` et redémarre tout seul. Les listes de cartes se gèrent
ensuite avec le bot (`/safe`, `/buy`, `/sell`…). Le `.env` n'est
lu qu'à la création du conteneur : après l'avoir modifié, lancez `docker compose up -d`
(et non `restart`). Session, caches et suivis sont conservés dans `state/`.

## Organisation du code

| Module | Rôle |
|---|---|
| `main.py` | Ligne de commande, journal |
| `scheduler.py` | Passes, boucle, réveils (surenchères, `/update`) |
| `session.py` | Un seul navigateur à la fois, connexion |
| `site.py` | Client de l'API du site |
| `selling.py` | Passe de vente (ventes demandées, puis plan de vente) |
| `buying.py` | Achats et surenchères |
| `lists.py` / `commands.py` | Listes de cartes (`state/`) et commandes du bot qui les modifient |
| `discard.py` | Défausse en arrière-plan |
| `pricing.py` | Prix moyens et leur cache |
| `selection.py` | Logique pure : collection, plan de vente, ajustement au marché |
| `unsold.py` | Cartes invendues mises de côté |
| `telegram.py` / `report.py` | Bot Telegram et bilans |
| `state.py` / `config.py` | Fichiers d'état et horloge ; réglages |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Les tests tournent en parallèle (`pytest-xdist`). `tests/test_end_to_end.py` lance un
vrai Chromium contre une imitation locale du site (`tests/fake_site.py`) et du bot
Telegram (`tests/fake_telegram.py`).
