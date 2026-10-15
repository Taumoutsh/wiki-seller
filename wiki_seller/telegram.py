"""Bot Telegram : quand la session WikiMasters n'est plus valide, il envoie un message
avec le lien du site, puis attend que l'on réponde dans la conversation avec les
cookies de session (la ligne copiée par le favori du README).

Seuls les messages de TELEGRAM_CHAT_ID sont lus ; le message qui contient les cookies
est supprimé dès qu'il est lu. API HTTP de Telegram, sans dépendance."""

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("wiki_seller")

POLL_SECONDS = 50  # attente maximale d'une requête getUpdates (long polling)
LISTEN_SECONDS = 20  # idem pour l'écoute des commandes : laisse vite la place à une demande de session
_POLL_LOCK = threading.Lock()  # un seul lecteur des messages à la fois
_SESSION_WAITING = threading.Event()  # une demande de session attend la réponse
_HANDOFF: list[dict] = []  # cookies reçus par l'écoute des commandes, pour la demande en cours
REMIND_AFTER = timedelta(hours=3)  # pas de nouveau message avant ce délai, on attend la réponse

ASK_TEXT = """🔑 WikiMasters : la session a expiré, le script est à l'arrêt.

1. Ouvrez {url} et connectez-vous (en cochant la case vous-même).
2. Ouvrez le favori « cookies » (voir le README) : il copie la ligne
   sb-…-auth-token.0=…; sb-…-auth-token.1=…
3. Collez-la ici en réponse.

Votre message sera supprimé dès qu'il sera lu."""


def session_cookie_names(text: str) -> list[str]:
    """Noms des cookies de session (« …auth-token… ») trouvés dans un message."""
    from .site import parse_cookies

    return [name for name, value in parse_cookies(text) if "auth-token" in name and value]


def normalize_cookies(text: str) -> str:
    from .site import parse_cookies

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

    def send(self, text: str) -> None:
        try:
            self.call("sendMessage", chat_id=self.chat_id, text=text, disable_web_page_preview=True)
        except Exception as exc:
            log.warning("Telegram : message non envoyé (%s).", exc)

    # ─────────────────────────── État (dernier message lu, dernière demande) ───────────────────────────

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    # ─────────────────────────── Lecture des messages ───────────────────────────

    def poll(self, wait: int) -> list[dict]:
        """Messages reçus de TELEGRAM_CHAT_ID (attente jusqu'à `wait` s). Un seul lecteur
        à la fois : la demande de session et l'écoute des commandes se partagent le fil."""
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
                chat = str((message.get("chat") or {}).get("id", ""))
                if chat == self.chat_id:
                    messages.append(message)
                elif chat:
                    log.info("Telegram : message ignoré d'une autre conversation (id %s).", chat)
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
        _SESSION_WAITING.set()  # avant la demande : l'écoute des commandes laisse les réponses ici
        try:
            return self._request_session(site_url, try_cookies, wait)
        finally:
            _SESSION_WAITING.clear()

    def _request_session(self, site_url: str, try_cookies, wait: timedelta) -> bool:
        state = self._state()
        now = datetime.now(timezone.utc)
        asked = state.get("asked_at")
        if not asked or now - datetime.fromisoformat(asked) > REMIND_AFTER:
            if "offset" not in state:  # ignore les messages envoyés avant la demande
                state["offset"] = self._next_offset()
            self.send(ASK_TEXT.format(url=site_url))
            state["asked_at"] = now.isoformat()
            self._save_state(state)
            log.info("Telegram : session demandée ; attente de la réponse (jusqu'à %d min).",
                     wait.total_seconds() // 60)
        else:
            log.info("Telegram : session déjà demandée ; attente de la réponse (jusqu'à %d min).",
                     wait.total_seconds() // 60)

        deadline = time.monotonic() + wait.total_seconds()
        while time.monotonic() < deadline:
            with _POLL_LOCK:
                messages = _HANDOFF[:]
                _HANDOFF.clear()
            if not messages:
                try:
                    messages = self.poll(max(1, min(POLL_SECONDS, int(deadline - time.monotonic()))))
                except Exception as exc:
                    log.warning("Telegram : lecture des messages impossible (%s) ; nouvel essai dans 30 s.", exc)
                    time.sleep(30)
                    continue
            for message in messages:
                if self._handle(message, try_cookies):
                    state = self._state()
                    state.pop("asked_at", None)
                    self._save_state(state)
                    return True
        return False

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


HELP_TEXT = "Commandes :\n/update : bilan maintenant (ventes, défausses, solde)"
REPORT_COMMANDS = ("/update", "/bilan")


class CommandListener:
    """Écoute les commandes envoyées au bot (en mode --loop) : /update demande un bilan
    tout de suite. Thread à part ; la demande de session reste prioritaire (même fil)."""

    def __init__(self, bot: TelegramBot, on_report):
        self.bot, self.on_report = bot, on_report
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)

    def start(self) -> "CommandListener":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(POLL_SECONDS + 20)

    def _run(self) -> None:
        while not self._stop.is_set():
            if _SESSION_WAITING.is_set():  # la demande de session lit elle-même les messages
                self._stop.wait(1)
                continue
            try:
                messages = self.bot.poll(LISTEN_SECONDS)
            except Exception as exc:
                log.warning("Telegram : lecture des commandes impossible (%s) ; nouvel essai dans 60 s.", exc)
                self._stop.wait(60)
                continue
            for message in messages:
                text = message.get("text") or ""
                command = text.strip().split("@")[0].split(" ")[0].lower()
                if session_cookie_names(text):
                    self._cookies(message)
                elif command in REPORT_COMMANDS:
                    self.bot.send("⏳ Bilan en préparation…")
                    self.on_report()
                else:
                    self.bot.send(HELP_TEXT)

    def _cookies(self, message: dict) -> None:
        """Cookies reçus pendant l'écoute : pour la demande de session en cours, sinon
        supprimés (aucune session n'est demandée)."""
        if _SESSION_WAITING.is_set():
            with _POLL_LOCK:
                _HANDOFF.append(message)
            return
        try:
            self.bot.call("deleteMessage", chat_id=self.bot.chat_id, message_id=message.get("message_id"))
        except Exception:
            pass
        self.bot.send("La session actuelle fonctionne, aucune n'est demandée : message supprimé.")


def telegram_bot(config) -> TelegramBot | None:
    if not (config.telegram_token and config.telegram_chat_id):
        return None
    return TelegramBot(config.telegram_token, config.telegram_chat_id, config.state_dir / "telegram.json",
                       config.telegram_api_url)
