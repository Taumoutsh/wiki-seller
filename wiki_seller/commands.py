"""Commandes du bot Telegram : listes de cartes d'un compte (relues par les passes à
chaque réveil) et administration des comptes. Chaque fonction renvoie la réponse à
envoyer dans la conversation."""

import re
from html import escape

from . import accounts, settings
from .lists import (
    AUTO,
    AUTO_BUY_RATIO,
    AUTO_SELL_RATIO,
    SHINY,
    ListError,
    add_safe,
    buy_cards,
    parse_card_command,
    price_text,
    remove_buy,
    remove_safe,
    remove_sell,
    safe_names,
    safe_titles,
    sell_cards,
    set_buy,
    set_sell,
)
from .selection import normalize_name
from .state import read_json

HELP_TEXT = ("📖 Commandes\n"
             "\n📊 Suivi\n"
             "/update — bilan maintenant\n"
             "/ventes — historique de vos ventes\n"
             "/list — vos cartes protégées, à acheter, à vendre\n"
             "\n🛒 Acheter\n"
             "/buy <carte> [max] — acheter, protégée une fois obtenue\n"
             "/buyandsell <carte> [max] [revente] — acheter puis revendre\n"
             "/unbuy <carte> [rareté] — annuler un achat\n"
             "\n💰 Vendre\n"
             "/sell <carte> [prix] — vendre à ce prix, en priorité\n"
             "/unsell <carte> [rareté] — annuler une vente\n"
             "\n🛡️ Protéger\n"
             "/safe <carte> — jamais défaussée ni vendue\n"
             "/unsafe <carte> — ne plus la protéger\n"
             "\n⚙️ Compte\n"
             "/settings — voir les réglages\n"
             "/set <réglage> <valeur> — changer un réglage\n"
             "/pause — tout arrêter\n"
             "/resume — tout reprendre\n"
             "/simulation on|off — tout simuler, rien n'est envoyé au site\n"
             "\n🏪 Marché\n"
             "/details <carte> [rareté] — fiche des ventes : graphique, prix moyen, min, max\n"
             "/market — état de la session Marché\n"
             "/market <cookies> — fournir la session (compte ayant le Marché)\n"
             "\n🔧 Options de /buy, /sell, /buyandsell\n"
             "x2 — nombre d'exemplaires\n"
             "12h — durée de vente : 10m, 30m, 1h, 3h, 6h, 12h\n"
             "@Pseudo — n'acheter qu'à ce vendeur\n"
             "SR — seulement cette rareté : L, UR, SR, R, PC, C (sans les shiny) ; L+ = légendaire shiny, "
             "prix obligatoire\n"
             "auto — prix du marché : achat jusqu'à 90 %, vente à 110 % "
             "(prix omis pour une nouvelle carte = auto)\n"
             "; — termine le nom : /buy Apollo 11; 500\n"
             "\nEx. : /buyandsell Tour Eiffel 500 800 x2 12h SR")
ADMIN_HELP = ("\n\n👑 Administration\n"
              "/users — comptes du bot\n"
              "/adduser <chat_id> <nom> — ouvrir l'accès à une conversation\n"
              "/removeuser <chat_id> — retirer un compte (ses fichiers sont gardés)\n"
              "/set cache_prix <durée> — validité des prix en cache, pour tous (ex. 24h, 7j)\n"
              "/set ventes_min <n> — ventes connues minimales pour se fier au prix d'une carte, pour tous")
ADMIN_COMMANDS = ("/users", "/adduser", "/removeuser")
WELCOME_TEXT = ("✅ Bienvenue {name} ! Votre accès au bot WikiMasters est ouvert.\n\n"
                "1. Dans une fenêtre privée, connectez-vous à wiki-masters.com, ouvrez le favori « cookies » "
                "que l'administrateur vous a transmis, collez ici la ligne copiée, puis fermez la fenêtre "
                "privée (vos autres navigateurs restent connectés). Ne cliquez jamais sur « Se déconnecter » "
                "sur le site : cela fermerait aussi la session du bot.\n"
                "2. Vérifiez vos réglages avec /settings : au départ, rien n'est vendu automatiquement "
                "et la défausse est coupée.\n"
                "3. Envoyez /resume pour démarrer.\n\n")
SETTINGS_COMMANDS = ("/settings", "/reglages", "/set", "/pause", "/resume", "/simulation")
LIST_COMMANDS = ("/list", "/safe", "/unsafe", "/buy", "/want", "/unbuy", "/sell", "/unsell", "/buyandsell")

BUY_USAGE = ("Usage : /buy <carte> [prix max] [x2] [@vendeur] [SR], ex. /buy Tour Eiffel 500 SR, "
             "/buy Victor Hugo 120 x2 @Paul, /buy Tour Eiffel (prix auto) ou /buy Apollo 11; 500 "
             "(« ; » après un nom qui finit par un nombre)")
SELL_USAGE = "Usage : /sell <carte> [prix] [x2] [12h] [SR], ex. /sell Tour Eiffel 800 12h ou /sell Tour Eiffel (prix auto)"
BUY_AND_SELL_USAGE = ("Usage : /buyandsell <carte> [prix max] [prix de vente] [x2] [12h] [@vendeur] [SR], "
                      "ex. /buyandsell Tour Eiffel 500 800 ou /buyandsell Tour Eiffel (prix auto)")
MARKET_HOWTO = ("Un compte ayant accès au Marché ouvre une nouvelle fenêtre privée, s'y connecte, copie les "
                "cookies avec le favori « cookies » et envoie /market suivi de la ligne copiée, puis ferme la "
                "fenêtre. Il faut une autre fenêtre que celle de la session du compte.")
# À supprimer avec /want (ancien nom de /buy) à une prochaine mise à jour.
WANT_NOTICE = "\n(/want est l'ancien nom de /buy : utilisez /buy, /want disparaîtra bientôt.)"
MAX_MESSAGE = 3800  # Telegram refuse les messages de plus de 4096 caractères


def help_text(admin: bool = False) -> str:
    return HELP_TEXT + (ADMIN_HELP if admin else "")


def menu_commands(admin: bool = False) -> list[dict]:
    """Menu des commandes de Telegram (affiché en tapant « / »), tiré de l'aide : chaque
    ligne « /commande <arguments> — description », une fois par commande."""
    menu: dict[str, str] = {}
    for line in help_text(admin).splitlines():
        if not line.startswith("/"):
            continue
        head, _, description = line.partition(" — ")
        name, _, arguments = head.lstrip("/").partition(" ")
        if re.fullmatch(r"[a-z0-9_]{1,32}", name) and name not in menu:
            text = description[:1].upper() + description[1:] + (f" ({arguments})" if arguments else "")
            menu[name] = text[:256]
    menu.setdefault("help", "Toutes les commandes et leurs options")
    return [{"command": name, "description": text} for name, text in menu.items()]


def run_admin_command(base, command: str, args: str, bot=None) -> str:
    """/users, /adduser <chat_id> <nom>, /removeuser <chat_id> (administrateur seulement)."""
    words = args.split()
    if command == "/users":
        return users_text(base)
    if command == "/adduser":
        if not words:
            return "Usage : /adduser <chat_id> <nom>, ex. /adduser 123456789 Paul"
        chat_id, name = words[0], " ".join(words[1:])
        try:
            new = accounts.add(base, chat_id, name)
        except ValueError as exc:
            return f"❌ {exc}"
        if new and bot is not None:
            bot.for_chat(chat_id).send(WELCOME_TEXT.format(name=name or "") + HELP_TEXT)
        return (f"✅ Compte {name or chat_id} ({chat_id}) ajouté : un message de bienvenue lui a été envoyé."
                if new else f"Le compte {chat_id} existait déjà" + (f" ; nom : {name}." if name else "."))
    if command == "/removeuser":
        if not words:
            return "Usage : /removeuser <chat_id>"
        if words[0] == accounts.admin_id(base):
            return "❌ Le compte de l'administrateur ne peut pas être retiré."
        if not accounts.remove(base, words[0]):
            return f"Aucun compte {words[0]}."
        return f"Compte {words[0]} retiré (ses fichiers restent dans state/users/{words[0]}/)."
    return help_text(admin=True)


def run_settings_command(base, chat_id: str, command: str, args: str) -> str:
    """/settings, /set <réglage> <valeur>, /pause, /resume, /simulation on|off."""
    config = accounts.account_config(base, chat_id)
    admin = str(chat_id) == accounts.admin_id(base)
    if command in ("/settings", "/reglages"):
        return settings_text(config, admin)
    if command == "/set":
        key, _, value = " ".join(args.split()).partition(" ")
        if not key or not value:
            return "Usage : /set <réglage> <valeur>, ex. /set prix 65 (voir /settings)"
    else:
        key = "simulation" if command == "/simulation" else "pause"
        value = args.strip() if command == "/simulation" else ("on" if command == "/pause" else "off")
        if not value:
            return "Usage : /simulation on ou /simulation off"
    try:
        setting = settings.set_value(config, key, value, admin)
    except settings.SettingError as exc:
        return f"❌ {key} : {exc}"
    updated = accounts.account_config(base, chat_id)
    if setting.key == "pause":
        return ("⏸️ Compte en pause : ni vente, ni achat, ni défausse jusqu'à /resume." if updated.paused else
                "▶️ Compte actif : la prochaine passe démarre dans la minute.")
    shared = " pour tous les comptes" if setting.key in settings.GLOBAL_KEYS else ""
    return f"✅ {setting.key} : {setting.show(updated)} (pris en compte à la prochaine passe{shared})."


def settings_text(config, admin: bool = False) -> str:
    state = "⏸️ en pause (/resume pour démarrer)" if config.paused else "▶️ actif"
    lines = [f"<b>⚙️ Réglages</b> · {state}" + (" · 🧪 simulation" if config.simulation else ""),
             "Modifier : /set &lt;réglage&gt; &lt;valeur&gt;, ex. /set prix 65", ""]
    for setting in settings.SETTINGS:
        if setting.key in ("pause",):
            continue
        lines.append(f"• <b>{setting.key}</b> : {escape(setting.show(config))}\n   <i>{escape(setting.help)}</i>")
    if admin:
        lines += ["", "<b>Communs à tous les comptes</b> (administrateur) :"]
        lines += [f"• <b>{s.key}</b> : {escape(s.show(config))}\n   <i>{escape(s.help)}</i>"
                  for s in settings.GLOBAL_SETTINGS]
    return "\n".join(lines)


def users_text(base) -> str:
    lines = [f"👥 Comptes ({len(accounts.accounts(base))}) :"]
    for account in accounts.accounts(base):
        config = accounts.account_config(base, account.chat_id)
        try:
            counts = f"{len(safe_titles(config))} protégée(s), {len(buy_cards(config))} achat(s), " \
                     f"{len(sell_cards(config))} vente(s)"
        except ListError:
            counts = "aucune liste"
        session = "session ✅" if config.session_file.exists() else "pas de session"
        admin = " (admin)" if account.chat_id == accounts.admin_id(base) else ""
        state = "⏸️ pause" if config.paused else "🧪 simulation" if config.simulation else "▶️ actif"
        shop = " · 🏪 Marché" if account.has_market else ""
        lines.append(f"• {account.name or '—'}{admin} · {account.chat_id} · {state} · {session}{shop} · {counts}")
    return "\n".join(lines)


def run_market_command(base, chat_id: str, args: str) -> str:
    """/market : état de la session Marché ; /market <cookies> : nouvelle session Marché,
    d'une autre fenêtre privée que la session du compte."""
    from . import market
    from .telegram import normalize_cookies, session_cookie_names

    if not session_cookie_names(args):
        return market_status(base)
    config = accounts.account_config(base, chat_id)
    own = read_json(config.session_file, {}).get("session")
    probe = next((key.split(":")[0] for key in read_json(config.shared_state_dir / "price_cache.json", {})), None)
    adopted, message = market.set_market_session(config, chat_id, normalize_cookies(args), own, probe)
    if adopted:
        accounts.set_market(base, chat_id, True)
    return message


def market_status(base) -> str:
    from . import market

    if not market.configured(base):
        return ("🏪 Marché : aucune session, les prix sont la moyenne donnée par le site.\n" + MARKET_HOWTO)
    owner = accounts.find(base, market.owner(base) or "")
    return (f"🏪 Marché : session active{f', fournie par {owner.name or owner.chat_id}' if owner else ''}. "
            "Les prix de tous les comptes viennent des ventes du Marché, valeurs aberrantes écartées.\n"
            "Pour la remplacer : " + MARKET_HOWTO[0].lower() + MARKET_HOWTO[1:])


def run_list_command(config, command: str, args: str) -> str:
    try:
        return _run(config, command, " ".join(args.split()))
    except (ListError, OSError) as exc:
        return f"❌ {exc}"


def _run(config, command: str, args: str) -> str:
    if command == "/list":
        return list_text(config)
    if command in ("/safe", "/unsafe", "/unbuy", "/unsell") and not args:
        raise ListError(f"Usage : {command} <carte>, ex. {command} Tour Eiffel")
    if command == "/safe":
        added, total = add_safe(config, args)
        return (f"🛡️ « {args} » protégée : jamais défaussée ni vendue automatiquement "
                f"({total} carte(s) protégée(s))." if added else f"« {args} » est déjà protégée.")
    if command == "/unsafe":
        return f"« {args} » n'est plus protégée." if remove_safe(config, args) else f"« {args} » n'était pas protégée."
    if command in ("/unbuy", "/unsell"):  # « /unbuy Tour Eiffel L+ » : cette rareté seulement
        remove, where = (remove_buy, "achats") if command == "/unbuy" else (remove_sell, "ventes")
        parsed = parse_card_command(args, 0, f"Usage : {command} <carte> [rareté]", ("rarete",), optional_prices=True)
        removed = remove(config, parsed.name, parsed.rarity or None)
        if not removed and parsed.rarity:  # le nom finit par un code de rareté (« Vitamine C »)
            parsed = parsed.__class__(" ".join(args.replace(";", " ").split()), [])
            removed = remove(config, parsed.name, None)
        what = f"« {parsed.name} »{f' ({parsed.rarity})' if parsed.rarity else ''}"
        if not removed:
            return f"{what} n'est pas dans les {where}."
        lines = f" ({removed} raretés)" if removed > 1 else ""
        return f"{what} retirée des {where}{lines}" + (" (une enchère déjà lancée continue)." if where == "ventes" else ".")
    if command in ("/buy", "/want"):
        parsed = parse_card_command(args, 1, BUY_USAGE, ("copies", "vendeur", "rarete"), optional_prices=True)
        status, card = set_buy(config, parsed.name, parsed.prices[0], parsed.copies, seller=parsed.seller,
                               rarity=parsed.rarity or None, resale=False)
        return (f"🛒 « {card.name} »{_rarity(card)} {status} dans les achats ({_max(card.max_price)}, {card.copies} "
                f"exemplaire(s){_from(card.seller)}, protégée une fois obtenue) : prise en compte à la prochaine "
                "passe."
                + (WANT_NOTICE if command == "/want" else ""))
    if command == "/buyandsell":
        parsed = parse_card_command(args, 2, BUY_AND_SELL_USAGE, ("copies", "duree", "vendeur", "rarete"),
                                    optional_prices=True)
        price, resale = parsed.prices
        if resale is None:  # revente au prix déjà demandé, sinon auto
            known = next((c for c in buy_cards(config) if c.key == normalize_name(parsed.name)
                          and (not parsed.rarity or c.rarity == parsed.rarity)), None)
            resale = (known.sell_price if known else None) or AUTO
        status, card = set_buy(config, parsed.name, price, parsed.copies, sell_price=resale, seller=parsed.seller,
                               sell_duration=parsed.duration, rarity=parsed.rarity or None)
        warning = "\n⚠️ Prix de vente sous le prix d'achat maximum." \
            if AUTO not in (card.max_price, card.sell_price) and card.sell_price < card.max_price else ""
        return (f"🔁 « {card.name} »{_rarity(card)} {status} dans les achats ({_max(card.max_price)}, {card.copies} "
                f"exemplaire(s){_from(card.seller)}), revendue à {_sell(card.sell_price)}{_for(card.sell_duration)} "
                f"une fois obtenue.{warning}")
    if command == "/sell":
        parsed = parse_card_command(args, 1, SELL_USAGE, ("copies", "duree", "rarete"), optional_prices=True)
        status, card = set_sell(config, parsed.name, parsed.prices[0], parsed.copies, parsed.duration or None,
                                parsed.rarity or None)
        protected = ""
        if card.key in _safe_names(config):
            protected = ("\n🛡️ Elle est protégée : si vous n'en avez qu'un exemplaire, elle ne le sera plus ; "
                         "sinon le dernier exemplaire est gardé.")
        return (f"💰 « {card.name} »{_rarity(card)} {status} dans les ventes : {card.remaining} exemplaire(s) à {_sell(card.price)}"
                f"{_for(card.duration)}, mis en vente en priorité dès la prochaine passe.{protected}")
    raise ListError(HELP_TEXT)


def _max(price, current: int | None = None) -> str:
    """« 300 max », « max auto (90 % du marché, 270 pour l'instant) »."""
    if price != AUTO:
        return f"{price} max"
    now = f", {current} pour l'instant" if current else ""
    return f"max auto ({round(AUTO_BUY_RATIO * 100)} % du marché{now})"


def _sell(price) -> str:
    return price_text(price, AUTO_SELL_RATIO)


def _rarity(card) -> str:
    return f" ({card.rarity}{' shiny' if card.rarity == SHINY else ''})" if card.rarity else ""


def _for(duration: str) -> str:
    return f" pour {duration}" if duration else ""


def _from(seller: str) -> str:
    return f", chez @{seller} seulement" if seller else ""


def _safe_names(config) -> set[str]:
    try:
        return safe_names(config)
    except ListError:
        return set()


def list_text(config) -> str:
    try:
        safe = sorted(safe_titles(config), key=normalize_name)
    except ListError:
        safe = []
    won = read_json(config.wanted_state_file, {}).get("cards", {})
    buy = buy_cards(config)
    sell = sell_cards(config)
    lines = [f"🛡️ Protégées ({len(safe)}) : " + (", ".join(safe) if safe else "aucune")]
    lines.append(f"🛒 À acheter ({len(buy)})" + (" :" if buy else " : aucune"))
    for card in buy:
        tracked = won.get(card.id) or {}
        got = len(tracked.get("won") or {})
        lines.append(f"• {card.name}{_rarity(card)} : {_max(card.max_price, tracked.get('auto_max'))}, {got}/{card.copies} obtenu(s)"
                     f"{_from(card.seller)}"
                     + (f", revente à {_sell(card.sell_price)}{_for(card.sell_duration)}" if card.sell_price else ""))
    lines.append(f"💰 À vendre ({len(sell)})" + (" :" if sell else " : aucune"))
    for card in sell:
        lines.append(f"• {card.name}{_rarity(card)} : {_sell(card.price)}{_for(card.duration)}, {len(card.sold)}/{card.copies} "
                     "vendu(s)")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE] + "…"
