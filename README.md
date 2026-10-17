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
- **Une tâche à la fois :** la défausse et les passes partagent la session du site.
  Quand une passe se réveille, la défausse lui laisse la place après la page, le prix ou
  l'exemplaire en cours, puis reprend où elle en était.
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
- il mise la mise minimale, calculée comme le site (la mise de départ, puis
  10 % de plus que la mise en cours, au moins +1), jamais directement votre maximum ;
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

Le bot annonce chaque résultat : « 🛒 Obtenue : Tour Eiffel pour 480 », ou « ❌ Perdue :
Tour Eiffel, remportée à 520 par un autre (max 500) » (aussi quand l'enchère où vous aviez
misé monte au-dessus de votre max). Pour une enchère surveillée jusqu'à la fin, le
résultat part dès que le site désigne le gagnant ; sinon, au réveil suivant.
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

Le script n'utilise pas de navigateur : il parle à l'API du site en HTTP, avec les
cookies de votre session. Le site s'appuie sur Supabase : la session tient dans les
cookies `sb-…-auth-token.0` et `.1` (un jeton d'accès valable 1 h et un jeton de
renouvellement). Le script la renouvelle lui-même avant qu'elle expire, comme le ferait
la page, et enregistre chaque nouvelle session dans `state/session.json`.

La connexion par mot de passe n'est pas possible : la page `/login` attend une case
Cloudflare cochée par une personne. Quand il n'a pas de session valide, le script
essaie, dans l'ordre : la session des anciennes versions (`state/storage_state.json`,
reprise une fois), `SESSION_COOKIES`, puis demande de nouveaux cookies par le bot
Telegram.

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
   `state/session.json` et relancez le conteneur (`docker compose up -d`).
4. Le mieux : faites-le dans une **fenêtre privée**, et **fermez-la aussitôt la ligne
   copiée**. Fermer la fenêtre efface ses cookies sans fermer la session côté site ; la
   laisser ouverte, au contraire, la ferait tenter un jour de renouveler la même session
   que le script, et le site l'annulerait pour les deux. Vos autres navigateurs restent
   connectés : chaque connexion a sa propre session.
5. **Ne cliquez jamais sur « Se déconnecter »** sur le site, sur aucun appareil : par
   défaut, cela ferme toutes les sessions du compte, celle du script comprise.

## Bot Telegram

1. Dans Telegram, écrivez à **@BotFather** : `/newbot`, choisissez un nom ; il vous
   donne le jeton du bot (`123456:ABC…`) → `TELEGRAM_BOT_TOKEN` dans `.env`.
2. Écrivez n'importe quoi à votre bot, puis ouvrez
   `https://api.telegram.org/bot<JETON>/getUpdates` : le nombre après `"chat":{"id":`
   est votre identifiant → `TELEGRAM_CHAT_ID` dans `.env`. Relancez le conteneur.

`TELEGRAM_CHAT_ID` est l'administrateur. Les cookies transitent par les serveurs de
Telegram : n'ouvrez le bot qu'à des personnes de confiance.

### Plusieurs comptes

Chaque compte WikiMasters correspond à une conversation Telegram (son chat_id), ajoutée
à la main par l'administrateur :

| Commande (administrateur) | Effet |
|---|---|
| `/adduser <chat_id> <nom>` | Ouvre l'accès ; la personne reçoit un message de bienvenue |
| `/removeuser <chat_id>` | Retire le compte (ses fichiers restent dans `state/users/<chat_id>/`) |
| `/users` | Liste des comptes : session, nombre de cartes protégées, à acheter et à vendre |

Chaque compte se règle depuis sa conversation (`/settings` affiche tout) :

| Commande | Effet |
|---|---|
| `/set <réglage> <valeur>` | `raretes` (ex. `L,SR`, `toutes`, `aucune`), `prix` (% du prix moyen), `ajustement`, `plancher`, `duree`, `nuit`, `intervalle`, `invendues`, `pause_invendues`, `defausse` (0 = coupée), `seuil_defausse`, `budget`, `surenchere`, `bilan` ; pris en compte à la passe suivante |
| `/pause` · `/resume` | Arrête ou reprend ventes, achats et défausse |
| `/simulation on\|off` | Tout simuler, sans rien envoyer au site |

Un réglage jamais modifié prend la valeur du `.env`. Un nouveau compte démarre en pause,
sans vente automatique (`raretes aucune`) ni défausse : la personne envoie ses cookies
(sans attendre de demande), vérifie `/settings`, puis `/resume`. Les ventes demandées
(`/sell`) partent même avec `raretes aucune`.

Les comptes tournent en parallèle, chacun dans son fil : passes, surenchères, défausse et
demandes de session ne s'attendent pas entre eux. Un compte ajouté démarre dans les
30 s, un compte retiré s'arrête à son réveil suivant. Dans le journal, chaque ligne porte
le nom du compte entre crochets (`[Paul]`, `[défausse-Paul]`).

Deux comptes du bot ne misent jamais l'un contre l'autre : une enchère suivie ou menée
par un compte est laissée aux autres. Si la seule enchère à votre prix est prise ainsi,
le bot vous prévient une fois et attend une autre enchère. Acheter une carte mise en
vente par un autre compte du bot reste possible.

Une conversation inconnue qui écrit au bot reçoit seulement son identifiant, à vous
transmettre ; rien d'autre ne lui est répondu. Chaque compte a ses fichiers dans
`state/users/<chat_id>/` (session, listes, suivis, bilans) ; le cache des prix, l'état du
bot et le journal sont communs, dans `state/`. Au premier démarrage, les fichiers de
l'ancienne version passent dans le dossier de l'administrateur.

**Session expirée :** le bot vous envoie le lien du site et la marche à suivre ; vous
lui répondez avec la ligne copiée par le favori. Il **supprime aussitôt votre message**,
essaie la session et confirme (✅) ou en demande une autre (❌). Le script attend
`TELEGRAM_WAIT` (60 min), puis réessaie au réveil suivant, sans nouveau message avant
3 h. La session acceptée est gardée (et tenue à jour) dans `state/session.json`.

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
  Chaque vente est comptée une seule fois : une vente qui apparaît en retard dans
  l'historique compte au bilan suivant.
- **Défausses :** 1 wikibidou par exemplaire (compteur dans `state/report.json`).
- **Achats en cours :** les enchères des cartes à acheter (`/buy`) : ✅ vous menez ;
  ⚠️ dépassé (votre mise → prix actuel), surenchère prévue juste avant la fin, avec son
  montant une fois le pas du site appris ; ❌ abandonnée, au-dessus du max. Fin : l'heure
  seule si c'est aujourd'hui.

Le bilan automatique part en fin de passe, une fois les enchères placées, et attend
qu'aucune de vos ventes ne se termine dans les 5 minutes ni ne soit en cours de
règlement (terminée, mais pas encore dans l'historique) : sinon le solde compterait une
vente que le bilan ne montre pas. Il part au réveil suivant, et quoi qu'il arrive au-delà
de 30 minutes de retard.

Le bilan est mis en forme (titres en gras) ; si Telegram refuse la mise en forme, il part
en texte brut. Pas de bilan en `--dry-run`.

**Commandes** (en mode `--loop`) :

| Commande | Effet |
|---|---|
| `/ventes` (ou `/historique`) | Vos enchères terminées telles que le site les renvoie (les 40 plus récentes) : ✅ vendue, ❌ invendue, et la période couverte |
| `/update` (ou `/bilan`) | Bilan immédiat depuis le dernier bilan automatique, sans changer le rythme des 2 h ; juste avant un réveil (moins de 90 s), il part juste après |
| `/list` | Cartes protégées, à acheter (avec les exemplaires obtenus) et à vendre |
| `/safe Tour Eiffel` · `/unsafe Tour Eiffel` | Protège la carte (jamais défaussée, toujours un exemplaire gardé), ou la retire des protégées. Une défausse en cours la respecte aussitôt |
| `/buy Tour Eiffel 500` · `/unbuy Tour Eiffel` | Achète la carte à 500 au plus (ou met ce prix à jour) ; `x2` pour 2 exemplaires, `@Pseudo` pour n'acheter qu'à ce vendeur. Protégée une fois obtenue |
| `/sell Tour Eiffel 800` · `/unsell Tour Eiffel` | Vend la carte à 800 exactement, en priorité, jusqu'à la vente ; `x2` pour 2 exemplaires, `12h` (ou `10m`, `30m`, `1h`, `3h`, `6h`) pour la durée de vente |
| `/buyandsell Tour Eiffel 500 800` | Achète la carte à 500 au plus, puis la revend à 800 ; options `x2`, `12h` (durée de la revente) et `@Pseudo` |

Les options s'écrivent avant ou après les prix, dans n'importe quel ordre :
`/buyandsell Tour Eiffel 500 800 x2 12h @Paul`. Sans durée, la vente prend la durée
habituelle du compte (nuit comprise). Pour `@Pseudo`, le nom est comparé à ce que le site
donne pour le vendeur ; si aucune enchère ne correspond, le journal liste les vendeurs
vus. Une enchère que vous menez déjà sur la carte reste suivie, quel que soit son vendeur.
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

`--debug` enregistre la réponse du site dans `state/debug/` quand une mise est refusée.
Le journal est dans `state/seller.log`.

### Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
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
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | *(trouvées seules)* | Session du site, à ne régler qu'en cas de problème |
| `CPU_LIMIT` / `MEM_LIMIT` | `1` / `512m` | Ressources maximales du conteneur |

Les anciennes options `MAIL`, `PASSWORD`, `HEADLESS`, `CHROMIUM_EXECUTABLE`,
`ACTIONS_VIA`, `OPEN_PACKS_HOURS`, `PACKS_VERIFY_WAIT`, `NOTIFY_URL` et `API_TRACE`
n'ont plus d'effet ; le journal le signale au démarrage.

## Docker (serveur)

```bash
# créer .env (voir .env.example), au moins avec le bot Telegram
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
| `session.py` | Une tâche à la fois sur la session, connexion par cookies |
| `auth.py` | Session Supabase : cookies, renouvellement, `state/session.json` |
| `site.py` | Client HTTP de l'API du site |
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

Les tests tournent en parallèle (`pytest-xdist`). `tests/test_end_to_end.py` fait
tourner le script contre une imitation locale du site, session Supabase comprise
(`tests/fake_site.py`), et du bot Telegram (`tests/fake_telegram.py`).
