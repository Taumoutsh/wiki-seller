"""Commandes du bot Telegram : listes de cartes d'un compte (relues par les passes à
chaque réveil) et administration des comptes. Chaque fonction renvoie la réponse à
envoyer dans la conversation."""

from . import accounts
from .lists import (
    ListError,
    add_safe,
    buy_cards,
    parse_card_args,
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
             "/ventes : historique de vos ventes, tel que le site le donne\n"
             "/list : cartes protégées, à acheter et à vendre\n"
             "/safe <carte> · /unsafe <carte> : protéger une carte (jamais défaussée, un exemplaire "
             "toujours gardé)\n"
             "/buy <carte> <prix max> [x2] · /unbuy <carte> : acheter une carte, protégée une fois obtenue\n"
             "/sell <carte> <prix> [x2] · /unsell <carte> : vendre une carte à ce prix exact, en priorité\n"
             "/buyandsell <carte> <prix max> <prix de vente> [x2] : acheter une carte puis la revendre")
ADMIN_HELP = ("\nAdministration :\n"
              "/users : comptes du bot\n"
              "/adduser <chat_id> <nom> : ouvrir l'accès à une conversation\n"
              "/removeuser <chat_id> : retirer un compte (ses fichiers sont gardés)")
ADMIN_COMMANDS = ("/users", "/adduser", "/removeuser")
WELCOME_TEXT = ("✅ Bienvenue {name} ! Votre accès au bot WikiMasters est ouvert.\n"
                "Pour commencer, envoyez ici les cookies de votre session (voir le favori « cookies » "
                "que l'administrateur vous a transmis).\n\n")
LIST_COMMANDS = ("/list", "/safe", "/unsafe", "/buy", "/want", "/unbuy", "/sell", "/unsell", "/buyandsell")

BUY_USAGE = "Usage : /buy <carte> <prix max> [x2], ex. /buy Apollo 11 500 ou /buy Victor Hugo 120 x2"
SELL_USAGE = "Usage : /sell <carte> <prix> [x2], ex. /sell Tour Eiffel 800"
BUY_AND_SELL_USAGE = ("Usage : /buyandsell <carte> <prix max> <prix de vente> [x2], "
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
        lines.append(f"• {account.name or '—'}{admin} · {account.chat_id} · {session} · {counts}")
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
        name, (price,), copies = parse_card_args(args, 1, BUY_USAGE)
        status, card = set_buy(config, name, price, copies)
        resale = f", revendue à {card.sell_price} une fois obtenue" if card.sell_price else ""
        return (f"🛒 « {card.name} » {status} dans les achats ({card.max_price} max, {card.copies} "
                f"exemplaire(s){resale}) : prise en compte à la prochaine passe."
                + (WANT_NOTICE if command == "/want" else ""))
    if command == "/buyandsell":
        name, (price, resale), copies = parse_card_args(args, 2, BUY_AND_SELL_USAGE)
        status, card = set_buy(config, name, price, copies, sell_price=resale)
        warning = "\n⚠️ Prix de vente sous le prix d'achat maximum." if resale < price else ""
        return (f"🔁 « {card.name} » {status} dans les achats ({card.max_price} max, {card.copies} "
                f"exemplaire(s)), revendue à {card.sell_price} une fois obtenue.{warning}")
    if command == "/sell":
        name, (price,), copies = parse_card_args(args, 1, SELL_USAGE)
        status, card = set_sell(config, name, price, copies)
        protected = ""
        if card.key in _safe_names(config):
            protected = ("\n🛡️ Elle est protégée : si vous n'en avez qu'un exemplaire, elle ne le sera plus ; "
                         "sinon le dernier exemplaire est gardé.")
        return (f"💰 « {card.name} » {status} dans les ventes : {card.remaining} exemplaire(s) à {card.price}, "
                f"mis en vente en priorité dès la prochaine passe.{protected}")
    raise ListError(HELP_TEXT)


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
        lines.append(f"• {card.name} : {card.max_price} max, {got}/{card.copies} obtenu(s)"
                     + (f", revente à {card.sell_price}" if card.sell_price else ""))
    lines.append(f"💰 À vendre ({len(sell)})" + (" :" if sell else " : aucune"))
    for card in sell:
        lines.append(f"• {card.name} : {card.price}, {len(card.sold)}/{card.copies} vendu(s)")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE] + "…"
