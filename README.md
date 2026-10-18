# wiki-seller

Automatise un ou plusieurs comptes [WikiMasters](https://www.wiki-masters.com) : mise
aux enchères des cartes de valeur, achat des cartes voulues, ventes à prix fixé,
défausse des cartes sans valeur, le tout piloté par un bot Telegram (une conversation
par compte).

Le script parle à l'API du site en HTTP, comme la page elle-même, avec la session de
chaque compte : pas de navigateur, et aucune vérification humaine n'est jamais
contournée.

## Démarrage rapide (administrateur)

1. Créez le bot Telegram (voir « Bot Telegram ») et remplissez `.env` à partir de
   `.env.example` : au minimum `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` (le vôtre).
2. Lancez le conteneur :

   ```bash
   mkdir -p state
   docker compose up -d --build
   docker compose logs -f
   ```

3. Le bot vous demande votre session : envoyez-lui la ligne de cookies (voir
   « Session du site »).
4. Réglez votre compte avec `/settings` et `/set`, remplissez vos listes (`/safe`,
   `/buy`, `/sell`…). Tout se fait depuis Telegram.

Pour un premier essai sans rien envoyer au site : `/simulation on`.

## Ajouter un utilisateur

1. La personne écrit à votre bot (lien `https://t.me/<nom_du_bot>`, bouton
   « Démarrer ») : il lui répond son identifiant Telegram, qu'elle vous transmet.
2. Vous envoyez `/adduser <chat_id> <prénom>` : elle reçoit un message de bienvenue.
3. Elle envoie sa ligne de cookies au bot (voir « Session du site »), vérifie
   `/settings`, puis `/resume`.

Un nouveau compte démarre **en pause**, **sans vente automatique** (`raretes aucune`),
**sans défausse** et sans carte protégée : rien ne se passe tant que la personne ne l'a
pas décidé. Les cookies donnent un accès complet au compte et transitent par Telegram :
n'ouvrez le bot qu'à des personnes de confiance.

## Session du site

Le site s'appuie sur Supabase : la session tient dans les cookies `sb-…-auth-token.0` et
`.1` (un jeton d'accès valable 1 h et un jeton de renouvellement). Le script la
renouvelle lui-même avant qu'elle expire, comme la page, et enregistre chaque nouvelle
version dans `state/users/<chat_id>/session.json`. La connexion par mot de passe n'est
pas possible (la page `/login` attend une case Cloudflare cochée par une personne) : la
session vient toujours d'un navigateur connecté.

### Copier la ligne de cookies

Toujours dans une **fenêtre privée**, connectée à wiki-masters.com :

- **Ordinateur :** ouvrez la console (F12, onglet Console), collez cette ligne puis
  Entrée ; la ligne de cookies est copiée (la console affiche `undefined`, c'est
  normal) :

  ```js
  copy(document.cookie.split('; ').filter(c => c.startsWith('sb-')).join('; '))
  ```

  Chrome demande parfois d'écrire `allow pasting` avant le premier collage.
- **Téléphone (ou ordinateur) :** ajoutez un favori « cookies » dont l'adresse est la
  ligne ci-dessous, puis ouvrez-le sur wiki-masters.com :

  ```
  javascript:(()=>{const c=document.cookie.split('; ').filter(x=>x.startsWith('sb-')).join('; ');navigator.clipboard.writeText(c).then(()=>alert('Copié : '+c.split('; ').length+' cookie(s)'),()=>prompt('Tout sélectionner et copier :',c))})()
  ```

Collez la ligne (`sb-…-auth-token.0=base64-…; sb-…-auth-token.1=…`) dans votre
conversation avec le bot : il **supprime aussitôt le message**, essaie la session et
répond ✅ ou ❌. Puis :

- **fermez la fenêtre privée tout de suite** : laissée ouverte, elle tenterait un jour
  de renouveler la même session que le script, et le site l'annulerait pour les deux.
  Fermer la fenêtre ne ferme pas la session ; vos autres navigateurs restent connectés
  (chaque connexion a sa propre session) ;
- **ne cliquez jamais sur « Se déconnecter »** sur le site, sur aucun appareil : par
  défaut, cela ferme toutes les sessions du compte, celle du script comprise.

**Session expirée :** le bot envoie le lien du site et la marche à suivre dans la
conversation du compte, attend la réponse `TELEGRAM_WAIT` (60 min) sans bloquer les autres
comptes, puis réessaie au réveil suivant (sans nouveau message avant 3 h). Des cookies
peuvent aussi être envoyés à tout moment, sans demande : la session en place est gardée
s'ils ne fonctionnent pas.

## Commandes du bot

| Commande | Effet |
|---|---|
| `/update` (ou `/bilan`) | Bilan immédiat (voir « Bilan ») |
| `/ventes` (ou `/historique`) | Vos enchères terminées telles que le site les renvoie (40 au plus) : ✅ vendue, ❌ invendue, période couverte |
| `/list` | Cartes protégées, à acheter (avec les exemplaires obtenus) et à vendre |
| `/safe <carte>` · `/unsafe <carte>` | Protège une carte (jamais défaussée, un exemplaire toujours gardé), ou la retire des protégées |
| `/buy <carte> [prix max]` · `/unbuy <carte>` | Achète la carte à ce prix au plus (voir « Achats ») ; protégée une fois obtenue |
| `/sell <carte> [prix]` · `/unsell <carte>` | Vend la carte à ce prix exact, en priorité, jusqu'à la vente (voir « Ventes à prix fixé ») |
| `/buyandsell <carte> [prix max] [prix de vente]` | Achète la carte, puis la revend à ce prix |
| `/market [cookies]` | État de la session Marché, ou nouvelle session Marché (voir « Prix des cartes ») |
| `/settings` · `/set <réglage> <valeur>` | Réglages du compte (voir « Réglages par compte ») |
| `/pause` · `/resume` | Arrête ou reprend ventes, achats et défausse (reprise dans la minute) |
| `/simulation on\|off` | Tout simuler, sans rien envoyer au site |

**Options** de `/buy`, `/sell` et `/buyandsell`, avant ou après les prix, dans n'importe
quel ordre : `x2` (2 exemplaires), `12h` (durée de vente : `10m`, `30m`, `1h`, `3h`, `6h`,
`12h` ; `/sell` et `/buyandsell`), `@Pseudo` (n'acheter qu'à ce vendeur ; `/buy` et
`/buyandsell`). Exemple : `/buyandsell Tour Eiffel 500 800 x2 12h @Paul`.

**Prix omis ou `auto` :** calculé à chaque passe d'après le prix du marché (voir « Prix
des cartes ») : achat jusqu'à **90 %**, vente à **110 %**. `/buy Tour Eiffel`,
`/sell Tour Eiffel 12h`, `/buyandsell Tour Eiffel` (les deux auto) ou
`/buyandsell Tour Eiffel 500` (achat à 500 au plus, revente auto). Une carte déjà dans la
liste renvoyée sans prix passe en auto.

Les derniers nombres sont toujours les prix : `/buy Apollo 11 500` vise « Apollo 11 » à
500 au plus, mais `/buy Apollo 11` vise « Apollo » à 11 : pour un nom qui finit par un
nombre, écrivez le prix ou `auto` (`/buy Apollo 11 auto`). Les noms sont comparés sans majuscules, accents ni espaces en trop. Les
modifications comptent dès la passe suivante. Tout autre message reçoit la liste des
commandes. (`/want` est l'ancien nom de `/buy`, encore accepté.)

**Administration** (votre conversation seulement) :

| Commande | Effet |
|---|---|
| `/adduser <chat_id> <nom>` | Ouvre l'accès ; la personne reçoit un message de bienvenue |
| `/removeuser <chat_id>` | Retire le compte (ses fichiers restent dans `state/users/<chat_id>/`) |
| `/users` | Comptes : état (actif, pause, simulation), session, accès au Marché 🏪, nombre de cartes par liste |
| `/set cache_prix 168` | Durée de validité des prix en cache, pour tous les comptes (`168`, `168h` ou `7j`) |

Une conversation inconnue qui écrit au bot reçoit seulement son identifiant.

## Réglages par compte

`/settings` affiche les valeurs ; `/set <réglage> <valeur>` les change, pour la passe
suivante. Un réglage jamais modifié prend la valeur du `.env` (voir « Réglages communs »).

| Réglage | Exemple | Effet |
|---|---|---|
| `raretes` | `L,SR`, `toutes`, `aucune` | Raretés mises en vente automatiquement (`L`, `UR`, `SR`, `R`, `PC`, `C`) |
| `prix` | `70` | Mise de départ, en % du prix moyen |
| `ajustement` | `on` / `off` | Prix ajusté d'après les enchères en cours de la carte |
| `plancher` | `50` | Ajustement jamais sous ce % du prix moyen |
| `duree` | `1h` | Durée des ventes (`10m`, `30m`, `1h`, `3h`, `6h`, `12h`) |
| `nuit` | `1-3=12h;3-6=6h`, `aucune` | Durées de nuit, heures de Paris |
| `intervalle` | `1h`, `auto` | Délai entre deux passes (`auto` : durée des ventes) |
| `invendues` · `pause_invendues` | `3` · `5` | Ventes ratées avant de mettre une carte de côté, et jours de mise de côté |
| `defausse` · `seuil_defausse` | `15` · `150` | Défausse des cartes valant moins que ce prix (`0` = coupée), au-delà de ce nombre d'exemplaires |
| `budget` | `2000`, `aucun` | Somme maximale des mises en cours |
| `surenchere` | `30` | Secondes avant la fin d'une enchère pour surenchérir (2 à 300) |
| `bilan` | `2` | Bilan automatique toutes les N heures (`0` = jamais) |
| `simulation` | `on` / `off` | Tout simuler |

## Fonctionnement

Chaque compte tourne dans son propre fil, en parallèle des autres. À chaque passe, le
script :

1. vérifie la session (voir « Session du site ») ;
2. mise sur les cartes à acheter (voir « Achats ») ;
3. met en vente, s'il reste de la place (5 enchères à la fois sur le site), **d'abord
   les ventes demandées** (`/sell`), puis les cartes des raretés `raretes`, **de la plus
   chère à la moins chère**, sauf les cartes protégées (un exemplaire gardé, les doublons
   vendus) : mise de départ à `prix` (70 %) du prix moyen, arrondie à l'entier inférieur
   et ajustée d'après les enchères en cours de la même carte, pour `duree` (1h, ou la
   durée de nuit). Une carte sans prix moyen est ignorée ; une carte dont le prix n'a pas
   pu être lu attend la passe suivante ;
4. lance la défausse en arrière-plan (voir « Défausse ») ;
5. envoie le bilan si son heure est venue (voir « Bilan »).

La passe suivante démarre `intervalle` après la dernière mise en vente (par défaut la
durée des ventes). Si toutes les places étaient prises, elle démarre dès la fin de la
première enchère en cours. La boucle se réveille aussi juste avant la fin des enchères
suivies, pour surenchérir.

### Achats

`/buy Tour Eiffel 500` achète la carte à 500 au plus (sans prix : 90 % du prix du
marché, recalculé à chaque passe) ; `x2` pour 2 exemplaires, l'un
après l'autre ; `@Pseudo` pour n'acheter qu'aux enchères de ce vendeur (si rien ne
correspond, le journal liste les vendeurs vus). Pour chaque carte :

- une enchère que vous menez déjà sur cette carte (« Mes enchères ») est reprise ;
- sinon, le script choisit dans le Marché l'enchère la moins chère dont la mise minimale
  ne dépasse pas votre maximum, et mise **la mise minimale**, calculée comme le site (la
  mise de départ, puis 10 % de plus que la mise en cours, au moins +1) ;
- dépassé, il **ne surenchérit pas tout de suite** : il revient pour la dernière minute,
  relit l'enchère **chaque seconde** et surenchérit à `surenchere` secondes (30) de la
  fin, puis **à chaque fois qu'il est dépassé**, dans la limite ; une mise des 10
  dernières secondes prolonge l'enchère de 60 s, la surveillance continue jusqu'à la
  vraie fin ;
- au-dessus de votre maximum, il abandonne cette enchère et en cherche une autre ;
- **une seule enchère à la fois par carte** ; `budget` limite la somme des mises en
  cours.

Le site ne répond pas toujours pendant les enchères : une mise sans réponse (8 s, ou
erreur du site) est vérifiée sur l'enchère ; passée malgré tout, elle n'est pas renvoyée,
sinon elle repart chaque seconde jusqu'à la fin de l'enchère (2 min au plus pendant une
passe). Une lecture sans réponse est refaite la seconde suivante.

Le bot annonce chaque résultat dès que le site désigne le gagnant : « 🛒 Obtenue : Tour
Eiffel pour 480 » ou « ❌ Perdue : Tour Eiffel, remportée à 520 par un autre (max 500) »
(aussi quand l'enchère où vous aviez misé dépasse votre maximum). Une fois tous les
exemplaires obtenus, la carte **quitte la liste d'achat et devient protégée** ; avec
`/buyandsell`, elle part dans les ventes au prix (et pour la durée) demandés.

**Plusieurs comptes :** deux comptes du bot ne misent jamais l'un contre l'autre. Une
enchère suivie ou menée par un compte est laissée aux autres ; si la seule enchère à
votre prix est prise ainsi, le bot vous prévient une fois et attend une autre enchère.
Acheter une carte mise en vente par un autre compte du bot reste possible.

### Ventes à prix fixé

`/sell Tour Eiffel 800` met la carte en vente **à 800 exactement** (ni `prix`, ni
ajustement ; sans prix : 110 % du prix du marché au moment de la mise en vente), quelle que soit sa rareté et même avec `raretes aucune`, pour la durée
demandée (`12h`…) ou à défaut la durée habituelle.

- Ces cartes passent **en priorité** dans les places libres ; les enchères en cours ne
  sont pas annulées.
- Une enchère terminée sans acheteur est **relancée au même prix** à chaque passe,
  jusqu'à la vente.
- Chaque vente est annoncée (« 💰 Vendue : Tour Eiffel à 800 ») ; une fois tous les
  exemplaires vendus, la carte quitte la liste.
- **Carte protégée :** en un seul exemplaire, elle n'est plus protégée et part ; en
  plusieurs, elle reste protégée et le dernier exemplaire est gardé.
- Une carte absente de la collection est signalée une fois et reste dans la liste
  (`/unsell` pour l'annuler). Ces cartes ne sont jamais défaussées.

### Défausse

À chaque réveil, un fil à part par compte lit la collection, sauf s'il tourne déjà ou a
fini il y a moins d'une heure (15 min si des prix n'avaient pas pu être lus). Au-delà de
`seuil_defausse` exemplaires (150), il examine les cartes **des plus communes aux plus
rares** (`C`, `PC`, `R`, `SR`, `UR`, `L`), lit le prix de chacune (cache, sinon le site)
et la **défausse aussitôt** si elle vaut moins de `defausse` (1 wikibidou par
exemplaire).

- **Défaussées :** prix moyen inférieur au seuil, ou aucune vente connue pour une
  commune (`C`) ou peu commune (`PC`). Tous les exemplaires, y compris le dernier.
- **Jamais défaussées :** les cartes protégées, à acheter et à vendre, les exemplaires en
  vente ou en échange, les cartes rares sans vente connue, et celles dont le prix n'a pas
  pu être lu (redemandées la fois suivante).
- La défausse laisse la place aux passes du compte, puis reprend où elle en était.

### Prix des cartes

Un seul prix par carte (et par rareté) sert à tous les comptes : ventes automatiques,
défausse, et prix `auto` des achats et ventes. Il est gardé dans `state/price_cache.json`,
commun, pendant `cache_prix` (24 h ; `/set cache_prix` ou `PRICE_CACHE_HOURS`), puis
relu par le premier compte qui en a besoin.

**Avec le Marché** (prix nettoyé). Certains comptes ont accès au Marché, qui détaille
toutes les ventes d'une carte. Un de ces comptes ouvre une **nouvelle fenêtre privée**,
s'y connecte, copie les cookies (voir « Copier la ligne de cookies ») et envoie
`/market` suivi de la ligne, puis ferme la fenêtre. Il faut une autre fenêtre que celle
de sa session de compte : les deux se gêneraient (le bot refuse la même session). Cette
session, dans `state/market/`, ne sert qu'à lire les prix, pour tous les comptes. Le prix
est calculé sur les ventes des 30 derniers jours (50 au plus) :

- une vente est **aberrante** si elle s'écarte de la médiane de plus de 3 écarts
  « robustes » (écart absolu médian, au moins 10 % de la médiane) : un échange d'argent
  ne fausse pas le calcul de l'écart lui-même, comme il fausserait un écart type ;
- si **les dernières ventes** sont aberrantes, toutes dans le même sens (dès la toute
  dernière), la carte **change de valeur** : le prix est la moyenne de ces ventes ;
- sinon les ventes aberrantes isolées (échanges d'argent) sont écartées, et le prix est la
  **moyenne des autres** ;
- moins de 3 ventes récentes : la moyenne du site.

Le journal donne pour chaque carte le prix retenu, les ventes gardées et écartées.

**Sans le Marché**, ou s'il ne répond pas : la moyenne donnée par le site
(`/api/marketplace/cards/<id>/sales?scope=summary`). Si la session Marché expire, le
compte qui l'a fournie est prévenu une fois ; en attendant sa nouvelle session, les prix
pris à la place sont relus au bout d'une heure plutôt que de `cache_prix`.

Une requête par carte (environ 5 s). Un refus passager du site ne fait laisser de côté que
la carte concernée, après 30 s de pause ; l'API n'est abandonnée qu'après 5 refus de
suite.

### Bilan

Toutes les `bilan` heures (2), ou à la demande avec `/update` :

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

- **En haut :** le solde et le gain depuis le dernier bilan automatique (« Rien de neuf »
  sinon) ; les sections vides sont omises.
- **Ventes :** prix finaux de vos enchères conclues (historique du site, 30 au plus),
  chacune comptée une seule fois : une vente qui arrive en retard dans l'historique
  compte au bilan suivant.
- **Défausses :** 1 wikibidou par exemplaire.
- **Achats en cours :** ✅ en tête ; ⚠️ dépassé (votre mise → prix actuel), surenchère
  prévue ; ❌ abandonnée, au-dessus du max. Fin : l'heure seule si c'est aujourd'hui.

Le bilan automatique part en fin de passe, une fois les enchères placées, quand aucune
de vos ventes ne se termine dans les 5 minutes ni n'est en cours de règlement (sinon le
solde compterait une vente que le bilan ne montre pas) ; au plus tard 30 minutes après
l'heure. `/update` part tout de suite (juste après un réveil s'il en tombe un dans les
90 s). Pas de bilan en simulation.

## Arrêt et reprise

Tout est enregistré au fil de l'eau dans `state/` : arrêter ou mettre à jour le
conteneur ne fait rien recommencer.

- **Prix :** le cache est enregistré tous les 50 prix lus et à la fin de chaque lecture ;
  un arrêt en pleine lecture perd au plus les 49 derniers (quelques minutes).
- **Défausse :** l'avancement est enregistré toutes les 20 pages de collection, toutes
  les 20 cartes examinées et après chaque carte défaussée ; elle reprend là où elle en
  était (24 h au plus), sans jamais défausser deux fois.
- **Sessions, listes, réglages, achats suivis, bilans :** enregistrés à chaque
  changement.

## Réglages communs (`.env`)

Lus à la création du conteneur : après une modification, `docker compose up -d` (et non
`restart`). Les réglages « par compte » sont les valeurs de départ des comptes,
modifiables ensuite par `/set`.

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(vide)* | Bot Telegram ; `TELEGRAM_CHAT_ID` est l'administrateur |
| `TELEGRAM_WAIT` | `60` | Minutes d'attente des cookies quand la session a expiré |
| `SESSION_COOKIES` | *(vide)* | Session de l'administrateur copiée d'un navigateur, sans passer par le bot |
| `SELL_RARITIES` | `L,SR` | Par compte (`raretes`) ; vide = toutes |
| `PRICE_RATIO` | `0.70` | Par compte (`prix`) |
| `MARKET_ADJUST` / `PRICE_FLOOR_RATIO` | `true` / `0.5` | Par compte (`ajustement`, `plancher`) : sous la moyenne des enchères en cours, + un écart type ; au-dessus, − un écart type |
| `AUCTION_DURATION_LABEL` | `1h` | Par compte (`duree`) |
| `NIGHT_DURATIONS` | `1-3=12h;3-6=6h` | Par compte (`nuit`) |
| `RUN_INTERVAL` | *(durée des ventes)* | Par compte (`intervalle`) ; 5 min minimum |
| `UNSOLD_MAX_TRIES` / `UNSOLD_COOLDOWN_DAYS` | `3` / `5` | Par compte (`invendues`, `pause_invendues`) |
| `DISCARD_BELOW` / `DISCARD_TRIGGER` | `15` / `150` | Par compte (`defausse`, `seuil_defausse`) |
| `MAX_TOTAL_BIDS` | *(vide)* | Par compte (`budget`) |
| `SNIPE_LEAD` | `30` | Par compte (`surenchere`) |
| `REPORT_HOURS` | `2` | Par compte (`bilan`) |
| `PRICE_CACHE_HOURS` | `24` | Commun (`/set cache_prix`) |
| `STATE_DIR` / `BASE_URL` | `state` / `https://www.wiki-masters.com` | Dossier des fichiers d'état ; adresse du site |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | *(trouvées seules)* | Session du site, à ne régler qu'en cas de problème |
| `CPU_LIMIT` / `MEM_LIMIT` | `1` / `512m` | Ressources maximales du conteneur |

Les anciennes options `MAIL`, `PASSWORD`, `HEADLESS`, `CHROMIUM_EXECUTABLE`,
`ACTIONS_VIA`, `OPEN_PACKS_HOURS`, `PACKS_VERIFY_WAIT`, `NOTIFY_URL` et `API_TRACE`
n'ont plus d'effet ; le journal le signale au démarrage.

## Bot Telegram

1. Dans Telegram, écrivez à **@BotFather** : `/newbot`, choisissez un nom ; il vous
   donne le jeton du bot (`123456:ABC…`) → `TELEGRAM_BOT_TOKEN` dans `.env`.
2. Écrivez n'importe quoi à votre bot, puis ouvrez
   `https://api.telegram.org/bot<JETON>/getUpdates` : le nombre après `"chat":{"id":`
   est votre identifiant → `TELEGRAM_CHAT_ID` dans `.env`. Relancez le conteneur.

## Fichiers

| Emplacement | Contenu |
|---|---|
| `state/users.json` | Liste des comptes |
| `state/users/<chat_id>/` | Un compte : `session.json`, `settings.json`, `safe_cards.json`, `buy_cards.json`, `sell_cards.json`, `wanted_state.json` (achats suivis), `report.json` (bilans), `discard_progress.json`, `unsold.json`, `debug/` |
| `state/price_cache.json` | Prix des cartes, communs à tous les comptes |
| `state/market/` | Session Marché (`/market`) et compte qui l'a fournie |
| `state/global_settings.json` | Réglages communs (`cache_prix`) |
| `state/telegram.json` · `state/site.json` | État du bot ; clé publique du site |
| `state/seller.log` | Journal, avec le compte entre crochets sur chaque ligne (`[Paul]`, `[défausse-Paul]`) |

Au premier démarrage d'une version multi-comptes, les fichiers de l'ancienne version
(dans `state/`, et les anciens `safed_cards.json` / `wanted_cards.json`) passent dans le
dossier de l'administrateur.

## Ligne de commande

```bash
python -m wiki_seller --dry-run     # tout simuler : une passe du compte administrateur
python -m wiki_seller               # une passe (attend la fin de la défausse)
python -m wiki_seller --loop        # en continu, tous les comptes (utilisé par le conteneur)
```

`--debug` enregistre la réponse du site dans `state/users/<chat_id>/debug/` quand une
mise est refusée. Avec Docker, lancez-les par `docker compose run --rm wiki-seller
python -m wiki_seller …`, **conteneur arrêté** : une même session utilisée par deux
processus à la fois peut être annulée.

## Organisation du code

| Module | Rôle |
|---|---|
| `main.py` | Ligne de commande, journal, une boucle par compte |
| `accounts.py` / `settings.py` | Comptes (`state/users/`) et leurs réglages |
| `scheduler.py` | Passes, boucle d'un compte, réveils (surenchères, `/update`) |
| `session.py` / `auth.py` | Connexion par cookies ; session Supabase et son renouvellement |
| `site.py` | Client HTTP de l'API du site |
| `selling.py` / `buying.py` | Passe de vente ; achats et surenchères |
| `discard.py` / `pricing.py` | Défausse en arrière-plan ; prix des cartes et leur cache |
| `market.py` | Session Marché et prix nettoyé des ventes du Marché |
| `lists.py` / `commands.py` | Listes de cartes ; commandes du bot |
| `selection.py` / `unsold.py` | Logique pure (collection, plan de vente, ajustement) ; invendues mises de côté |
| `telegram.py` / `report.py` | Bot Telegram ; bilans et historique |
| `state.py` / `config.py` | Fichiers d'état et horloge ; réglages du `.env` |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Les tests tournent en parallèle (`pytest-xdist`), contre une imitation locale du site,
session Supabase comprise (`tests/fake_site.py`), et du bot Telegram
(`tests/fake_telegram.py`).
