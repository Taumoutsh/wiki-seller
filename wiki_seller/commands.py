"""Commandes du bot Telegram : listes de cartes d'un compte (relues par les passes à
chaque réveil) et administration des comptes. Chaque fonction renvoie la réponse à
envoyer dans la conversation."""

from html import escape

from . import accounts, settings
from .lists import (
    ListError,
    add_safe,
    buy_cards,
    parse_card_command,
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

HELP_TEXT = ("Commandes :\n"
             "/update : bilan maintenant (ventes, défausses, solde)\n"
             "/settings · /set <réglage> <valeur> : réglages du compte\n"
             "/pause · /resume : arrêter ou reprendre ventes, achats et défausse\n"
             "/simulation on|off : tout simuler, sans rien envoyer au site\n"
             "/ventes : historique de vos ventes, tel que le site le donne\n"
             "/list : cartes protégées, à acheter et à vendre\n"
             "/safe <carte> · /unsafe <carte> : protéger une carte (jamais défaussée, un exemplaire "
             "toujours gardé)\n"
             "/buy <carte> <prix max> [x2] [@vendeur] · /unbuy <carte> : acheter une carte, protégée une fois "
             "obtenue\n"
             "/sell <carte> <prix> [x2] [12h] · /unsell <carte> : vendre une carte à ce prix exact, en priorité\n"
             "/buyandsell <carte> <prix max> <prix de vente> [x2] [12h] [@vendeur] : acheter une carte puis la "
             "revendre\n"
             "Options : x2 = 2 exemplaires ; 10m, 30m, 1h, 3h, 6h, 12h = durée de vente ; "
             "@Pseudo = n'acheter qu'à ce vendeur")
ADMIN_HELP = ("\nAdministration :\n"
              "/users : comptes du bot\n"
              "/adduser <chat_id> <nom> : ouvrir l'accès à une conversation\n"
              "/removeuser <chat_id> : retirer un compte (ses fichiers sont gardés)")
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

BUY_USAGE = ("Usage : /buy <carte> <prix max> [x2] [@vendeur], ex. /buy Apollo 11 500 ou "
             "/buy Victor Hugo 120 x2 @Paul")
SELL_USAGE = "Usage : /sell <carte> <prix> [x2] [12h], ex. /sell Tour Eiffel 800 12h"
BUY_AND_SELL_USAGE = ("Usage : /buyandsell <carte> <prix max> <prix de vente> [x2] [12h] [@vendeur], "
                      "ex. /buyandsell Tour Eiffel 500 800")
# À supprimer avec /want (ancien nom de /buy) à une prochaine mise à jour.
WANT_NOTICE = "\n(/want est l'ancien nom de /buy : utilisez /buy, /want disparaîtra bientôt.)"
MAX_MESSAGE = 3800  # Telegram refuse les messages de plus de 4096 caractères


def help_text(admin: bool = False) -> str:
    return HELP_TEXT + (ADMIN_HELP if admin else "")


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
    if command in ("/settings", "/reglages"):
        return settings_text(config)
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
        setting = settings.set_value(config, key, value)
    except settings.SettingError as exc:
        return f"❌ {key} : {exc}"
    updated = accounts.account_config(base, chat_id)
    if setting.key == "pause":
        return ("⏸️ Compte en pause : ni vente, ni achat, ni défausse jusqu'à /resume." if updated.paused else
                "▶️ Compte actif : la prochaine passe démarre dans la minute.")
    return f"✅ {setting.key} : {setting.show(updated)} (pris en compte à la prochaine passe)."


def settings_text(config) -> str:
    state = "⏸️ en pause (/resume pour démarrer)" if config.paused else "▶️ actif"
    lines = [f"<b>⚙️ Réglages</b> · {state}" + (" · 🧪 simulation" if config.simulation else ""),
             "Modifier : /set &lt;réglage&gt; &lt;valeur&gt;, ex. /set prix 65", ""]
    for setting in settings.SETTINGS:
        if setting.key in ("pause",):
            continue
        lines.append(f"• <b>{setting.key}</b> : {escape(setting.show(config))}\n   <i>{escape(setting.help)}</i>")
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
        lines.append(f"• {account.name or '—'}{admin} · {account.chat_id} · {state} · {session} · {counts}")
    return "\n".join(lines)


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
        return (f"🛡️ « {args} » protégée : jamais défaussée, et toujours un exemplaire gardé "
                f"({total} carte(s) protégée(s))." if added else f"« {args} » est déjà protégée.")
    if command == "/unsafe":
        return f"« {args} » n'est plus protégée." if remove_safe(config, args) else f"« {args} » n'était pas protégée."
    if command == "/unbuy":
        return f"« {args} » retirée des achats." if remove_buy(config, args) else f"« {args} » n'est pas dans les achats."
    if command == "/unsell":
        return (f"« {args} » retirée des ventes (une enchère déjà lancée continue)." if remove_sell(config, args)
                else f"« {args} » n'est pas dans les ventes.")
    if command in ("/buy", "/want"):
        parsed = parse_card_command(args, 1, BUY_USAGE, ("copies", "vendeur"))
        status, card = set_buy(config, parsed.name, parsed.prices[0], parsed.copies, seller=parsed.seller)
        resale = f", revendue à {card.sell_price}{_for(card.sell_duration)} une fois obtenue" if card.sell_price else ""
        return (f"🛒 « {card.name} » {status} dans les achats ({card.max_price} max, {card.copies} "
                f"exemplaire(s){_from(card.seller)}{resale}) : prise en compte à la prochaine passe."
                + (WANT_NOTICE if command == "/want" else ""))
    if command == "/buyandsell":
        parsed = parse_card_command(args, 2, BUY_AND_SELL_USAGE, ("copies", "duree", "vendeur"))
        price, resale = parsed.prices
        status, card = set_buy(config, parsed.name, price, parsed.copies, sell_price=resale, seller=parsed.seller,
                               sell_duration=parsed.duration)
        warning = "\n⚠️ Prix de vente sous le prix d'achat maximum." if resale < price else ""
        return (f"🔁 « {card.name} » {status} dans les achats ({card.max_price} max, {card.copies} "
                f"exemplaire(s){_from(card.seller)}), revendue à {card.sell_price}{_for(card.sell_duration)} "
                f"une fois obtenue.{warning}")
    if command == "/sell":
        parsed = parse_card_command(args, 1, SELL_USAGE, ("copies", "duree"))
        status, card = set_sell(config, parsed.name, parsed.prices[0], parsed.copies, parsed.duration or None)
        protected = ""
        if card.key in _safe_names(config):
            protected = ("\n🛡️ Elle est protégée : si vous n'en avez qu'un exemplaire, elle ne le sera plus ; "
                         "sinon le dernier exemplaire est gardé.")
        return (f"💰 « {card.name} » {status} dans les ventes : {card.remaining} exemplaire(s) à {card.price}"
                f"{_for(card.duration)}, mis en vente en priorité dès la prochaine passe.{protected}")
    raise ListError(HELP_TEXT)


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
        got = len((won.get(card.key) or {}).get("won") or {})
        lines.append(f"• {card.name} : {card.max_price} max, {got}/{card.copies} obtenu(s){_from(card.seller)}"
                     + (f", revente à {card.sell_price}{_for(card.sell_duration)}" if card.sell_price else ""))
    lines.append(f"💰 À vendre ({len(sell)})" + (" :" if sell else " : aucune"))
    for card in sell:
        lines.append(f"• {card.name} : {card.price}{_for(card.duration)}, {len(card.sold)}/{card.copies} vendu(s)")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE] + "…"
