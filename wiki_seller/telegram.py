"""Bot Telegram : commandes des comptes (une conversation par compte, voir accounts.py),
bilans, et renouvellement de session : quand la session WikiMasters d'un compte n'est
plus valide, il envoie le lien du site dans sa conversation, puis attend qu'on y réponde
avec les cookies de session (la ligne copiée par le favori du README).

Un seul lecteur des messages (l'écoute des commandes) les distribue : une demande de
session en cours reçoit ceux de sa conversation, les autres comptes continuent. Un
message qui contient des cookies est supprimé dès qu'il est lu. API HTTP de Telegram,
sans dépendance."""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from .commands import HELP_TEXT, LIST_COMMANDS, run_list_command
from .state import read_json, write_json

log = logging.getLogger("wiki_seller")

POLL_SECONDS = 50  # attente maximale d'une requête getUpdates (long polling)
LISTEN_SECONDS = 20  # idem pour l'écoute des commandes : laisse vite la place à une demande de session
_POLL_LOCK = threading.Lock()  # un seul lecteur des messages à la fois
_INBOX = threading.Condition()
_WAITING: dict[str, list[dict]] = {}  # conversation → messages reçus pendant sa demande de session
_LISTENING = threading.Event()  # l'écoute des commandes tourne : elle seule lit les messages
REMIND_AFTER = timedelta(hours=3)  # pas de nouveau message avant ce délai, on attend la réponse

ASK_TEXT = """🔑 WikiMasters : la session a expiré, le script est à l'arrêt.

1. Ouvrez {url} et connectez-vous (en cochant la case vous-même).
2. Ouvrez le favori « cookies » (voir le README) : il copie la ligne
   sb-…-auth-token.0=…; sb-…-auth-token.1=…
3. Collez-la ici en réponse.

Votre message sera supprimé dès qu'il sera lu."""


def session_cookie_names(text: str) -> list[str]:
    """Noms des cookies de session (« …auth-token… ») trouvés dans un message."""
    from .session import parse_cookies

    return [name for name, value in parse_cookies(text) if "auth-token" in name and value]


def normalize_cookies(text: str) -> str:
    from .session import parse_cookies

    return "; ".join(f"{name}={value}" for name, value in parse_cookies(text))


class TelegramBot:
    def __init__(self, token: str, chat_id: str, state_file: Path, api_url: str = "https://api.telegram.org"):
        self.token, self.chat_id, self.state_file = token, str(chat_id), state_file
        self.api_url = api_url.rstrip("/")

    # ─────────────────────────── API Telegram ───────────────────────────

    def call(self, method: str, http_timeout: float = 15, **params) -> object:
        request = urllib.request.Request(
            f"{self.api_url}/bot{self.token}/{method}", data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            data = json.loads(response.read() or b"{}")
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"{method} refusé")
        return data.get("result")

    def for_chat(self, chat_id: str) -> "TelegramBot":
        """Le même bot, pour écrire dans une autre conversation."""
        return TelegramBot(self.token, chat_id, self.state_file, self.api_url)

    def send(self, text: str, html: bool = False) -> None:
        """html : mise en forme (<b>, <pre>) ; si Telegram la refuse, le texte part brut."""
        try:
            if html:
                try:
                    self.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                              disable_web_page_preview=True)
                    return
                except (RuntimeError, urllib.error.HTTPError) as exc:  # 400 « can't parse entities »
                    log.warning("Telegram : mise en forme refusée (%s) ; message envoyé sans.", exc)
                    text = unescape(re.sub(r"</?(b|pre)>", "", text))
            self.call("sendMessage", chat_id=self.chat_id, text=text, disable_web_page_preview=True)
        except Exception as exc:
            log.warning("Telegram : message non envoyé (%s).", exc)

    # ─────────────────────────── État (dernier message lu, dernière demande) ───────────────────────────

    def _state(self) -> dict:
        return read_json(self.state_file, {})

    def _save_state(self, state: dict) -> None:
        write_json(self.state_file, state)

    # ─────────────────────────── Lecture des messages ───────────────────────────

    def poll(self, wait: int) -> list[dict]:
        """Messages reçus, toutes conversations (attente jusqu'à `wait` s). Un seul lecteur
        à la fois."""
        with _POLL_LOCK:
            state = self._state()
            if "offset" not in state:  # premier lancement : ignore les anciens messages
                state["offset"] = self._next_offset()
                self._save_state(state)
            updates = self.call("getUpdates", http_timeout=wait + 15, offset=state["offset"],
                                timeout=wait, allowed_updates=["message"])
            messages = []
            for update in updates or []:
                state = self._state()
                state["offset"] = update["update_id"] + 1
                self._save_state(state)
                message = update.get("message") or {}
                if chat_of(message):
                    messages.append(message)
            return messages

    def _next_offset(self) -> int:
        try:
            updates = self.call("getUpdates", offset=-1, timeout=0)
            return updates[-1]["update_id"] + 1 if updates else 0
        except Exception:
            return 0

    # ─────────────────────────── Demande de session ───────────────────────────

    def request_session(self, site_url: str, try_cookies, wait: timedelta) -> bool:
        """Demande les cookies de session dans la conversation et attend la réponse
        pendant `wait`. `try_cookies(cookies)` les essaie sur le site (True : connecté).
        Renvoie True dès qu'une session envoyée fonctionne."""
        with _INBOX:  # avant la demande : l'écoute des commandes garde les réponses ici
            _WAITING[self.chat_id] = []
        try:
            return self._request_session(site_url, try_cookies, wait)
        finally:
            with _INBOX:
                _WAITING.pop(self.chat_id, None)

    def _request_session(self, site_url: str, try_cookies, wait: timedelta) -> bool:
        state = self._state()
        now = datetime.now(timezone.utc)
        asked = state.get("asked", {}).get(self.chat_id)
        if not asked or now - datetime.fromisoformat(asked) > REMIND_AFTER:
            if "offset" not in state:  # ignore les messages envoyés avant la demande
                state["offset"] = self._next_offset()
            self.send(ASK_TEXT.format(url=site_url))
            state.setdefault("asked", {})[self.chat_id] = now.isoformat()
            self._save_state(state)
            log.info("Telegram : session demandée ; attente de la réponse (jusqu'à %d min).",
                     wait.total_seconds() // 60)
        else:
            log.info("Telegram : session déjà demandée ; attente de la réponse (jusqu'à %d min).",
                     wait.total_seconds() // 60)

        deadline = time.monotonic() + wait.total_seconds()
        while time.monotonic() < deadline:
            for message in self._replies(max(1, min(POLL_SECONDS, int(deadline - time.monotonic())))):
                if self._handle(message, try_cookies):
                    state = self._state()
                    state.get("asked", {}).pop(self.chat_id, None)
                    self._save_state(state)
                    return True
        return False

    def _replies(self, wait: int) -> list[dict]:
        """Messages de cette conversation : transmis par l'écoute des commandes si elle
        tourne, sinon lus directement (passe unique)."""
        if _LISTENING.is_set():
            with _INBOX:
                if not _WAITING.get(self.chat_id):
                    _INBOX.wait(wait)
                messages, _WAITING[self.chat_id] = _WAITING.get(self.chat_id) or [], []
            return messages
        try:
            return [m for m in self.poll(wait) if chat_of(m) == self.chat_id]
        except Exception as exc:
            log.warning("Telegram : lecture des messages impossible (%s) ; nouvel essai dans 30 s.", exc)
            time.sleep(30)
            return []

    def _handle(self, message: dict, try_cookies) -> bool:
        text = message.get("text") or ""
        if text.startswith("/"):
            self.send("🔑 La session a expiré : envoyez d'abord la ligne de cookies, "
                      "le script reprendra ensuite.")
            return False
        names = session_cookie_names(text)
        if not names:
            self.send("Je n'ai pas reconnu de cookie « sb-…-auth-token…=… ». Collez la ligne copiée par le favori.")
            return False
        try:
            self.call("deleteMessage", chat_id=self.chat_id, message_id=message.get("message_id"))
        except Exception as exc:
            log.warning("Telegram : message avec les cookies non supprimé (%s) ; supprimez-le vous-même.", exc)
        cookies = normalize_cookies(text)
        if try_cookies(cookies):
            self.send(f"✅ Session acceptée ({len(names)} cookie(s)) : le script reprend.")
            return True
        self.send("❌ Le site refuse cette session (expirée ?). Reconnectez-vous puis renvoyez la ligne.")
        return False


REPORT_COMMANDS = ("/update", "/bilan")
HISTORY_COMMANDS = ("/ventes", "/historique")
UNKNOWN_TEXT = ("👋 Ce bot est privé. Votre identifiant Telegram : {chat}\n"
                "Transmettez-le à l'administrateur pour obtenir un accès.")
NOT_RUNNING_TEXT = "⏳ Votre compte n'est pas encore actif : réessayez un peu plus tard."


def chat_of(message: dict) -> str:
    return str((message.get("chat") or {}).get("id", ""))


class CommandListener:
    """Écoute les messages de toutes les conversations (en mode --loop) et les distribue :
    réponses attendues par une demande de session, commandes des comptes, commandes
    d'administration, identifiant renvoyé aux inconnus. on_request(chat, "report" |
    "history") demande un bilan ou l'historique ; False si le compte ne tourne pas.

    config : réglages communs (plusieurs comptes) ; sans eux, seule la conversation du bot
    est servie, sans les commandes de listes."""

    def __init__(self, bot: TelegramBot, on_request, config=None):
        self.bot, self.on_request, self.config = bot, on_request, config
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)

    def start(self) -> "CommandListener":
        _LISTENING.set()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(POLL_SECONDS + 20)
        _LISTENING.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                messages = self.bot.poll(LISTEN_SECONDS)
            except Exception as exc:
                log.warning("Telegram : lecture des commandes impossible (%s) ; nouvel essai dans 60 s.", exc)
                self._stop.wait(60)
                continue
            for message in messages:
                try:
                    self._dispatch(message)
                except Exception:
                    log.exception("Telegram : message non traité.")

    def _dispatch(self, message: dict) -> None:
        chat = chat_of(message)
        with _INBOX:
            if chat in _WAITING:  # une demande de session attend la réponse de ce compte
                _WAITING[chat].append(message)
                _INBOX.notify_all()
                return
        from . import accounts
        from .commands import ADMIN_COMMANDS, SETTINGS_COMMANDS, help_text, run_admin_command, run_settings_command

        reply = self.bot.for_chat(chat)
        text = message.get("text") or ""
        command = text.strip().split("@")[0].split(" ")[0].lower()
        args = text.strip().partition(" ")[2]
        if self.config is None:
            known, account, admin = chat == self.bot.chat_id, None, False
        else:
            known = accounts.find(self.config, chat) is not None
            account = accounts.account_config(self.config, chat) if known else None
            admin = chat == accounts.admin_id(self.config)
        if not known:
            log.info("Telegram : message d'une conversation inconnue (id %s).", chat)
            if session_cookie_names(text):
                self._delete(reply, message)
            reply.send(UNKNOWN_TEXT.format(chat=chat))
        elif session_cookie_names(text):
            self._delete(reply, message)
            if account is None:
                reply.send("Message supprimé : aucune session n'est demandée.")
            else:
                self._new_session(reply, account, normalize_cookies(text))
        elif command in REPORT_COMMANDS or command in HISTORY_COMMANDS:
            kind = "report" if command in REPORT_COMMANDS else "history"
            accepted = self.on_request(chat, kind)
            reply.send(NOT_RUNNING_TEXT if accepted is False else
                       "⏳ Bilan en préparation…" if kind == "report" else "⏳ Historique en préparation…")
        elif command in ADMIN_COMMANDS and admin and self.config is not None:
            reply.send(run_admin_command(self.config, command, args, self.bot))
        elif command in SETTINGS_COMMANDS and self.config is not None:
            reply.send(run_settings_command(self.config, chat, command, args),
                       html=command in ("/settings", "/reglages"))
        elif command in LIST_COMMANDS and account is not None:
            reply.send(run_list_command(account, command, args))
        else:
            reply.send(help_text(admin) if self.config is not None else HELP_TEXT)

    @staticmethod
    def _new_session(reply: TelegramBot, account, cookies: str) -> None:
        """Cookies envoyés sans demande (nouveau compte, ou session à remplacer) : essayés
        aussitôt ; la session en place est gardée s'ils ne fonctionnent pas."""
        from .session import replace_session

        result = replace_session(account, cookies)
        reply.send("✅ Session enregistrée." + (" Envoyez /resume pour démarrer." if account.paused else "")
                   if result else "❌ Le site refuse cette session (expirée ?). Reconnectez-vous puis renvoyez la ligne."
                   if result is False else "⏳ Le site ne répond pas : renvoyez la ligne dans quelques minutes.")

    @staticmethod
    def _delete(bot: TelegramBot, message: dict) -> None:
        try:
            bot.call("deleteMessage", chat_id=bot.chat_id, message_id=message.get("message_id"))
        except Exception:
            pass


def telegram_bot(config) -> TelegramBot | None:
    if not (config.telegram_token and config.telegram_chat_id):
        return None
    return TelegramBot(config.telegram_token, config.telegram_chat_id, config.shared_state_dir / "telegram.json",
                       config.telegram_api_url)


def notify(config, text: str) -> None:
    """Message dans la conversation du bot (achat obtenu, vente conclue…), s'il est configuré."""
    bot = telegram_bot(config)
    if bot is not None:
        bot.send(text)
