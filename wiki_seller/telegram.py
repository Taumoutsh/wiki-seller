"""Bot Telegram : quand la session WikiMasters n'est plus valide, il envoie un message
avec le lien du site, puis attend que l'on réponde dans la conversation avec les
cookies de session (la ligne copiée par le favori du README).

Seuls les messages de TELEGRAM_CHAT_ID sont lus ; le message qui contient les cookies
est supprimé dès qu'il est lu. API HTTP de Telegram, sans dépendance."""

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("wiki_seller")

POLL_SECONDS = 50  # attente maximale d'une requête getUpdates (long polling)
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

    # ─────────────────────────── Demande de session ───────────────────────────

    def request_session(self, site_url: str, try_cookies, wait: timedelta) -> bool:
        """Demande les cookies de session dans la conversation et attend la réponse
        pendant `wait`. `try_cookies(cookies)` les essaie sur le site (True : connecté).
        Renvoie True dès qu'une session envoyée fonctionne."""
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
            poll = max(1, min(POLL_SECONDS, int(deadline - time.monotonic())))
            try:
                updates = self.call("getUpdates", http_timeout=poll + 15, offset=state.get("offset", 0),
                                    timeout=poll, allowed_updates=["message"])
            except Exception as exc:
                log.warning("Telegram : lecture des messages impossible (%s) ; nouvel essai dans 30 s.", exc)
                time.sleep(30)
                continue
            for update in updates or []:
                state["offset"] = update["update_id"] + 1
                self._save_state(state)
                if self._handle(update.get("message") or {}, try_cookies):
                    state.pop("asked_at", None)
                    self._save_state(state)
                    return True
        return False

    def _next_offset(self) -> int:
        try:
            updates = self.call("getUpdates", offset=-1, timeout=0)
            return updates[-1]["update_id"] + 1 if updates else 0
        except Exception:
            return 0

    def _handle(self, message: dict, try_cookies) -> bool:
        chat = str((message.get("chat") or {}).get("id", ""))
        text = message.get("text") or ""
        if chat != self.chat_id:
            if chat:
                log.info("Telegram : message ignoré d'une autre conversation (id %s).", chat)
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


def telegram_bot(config) -> TelegramBot | None:
    if not (config.telegram_token and config.telegram_chat_id):
        return None
    return TelegramBot(config.telegram_token, config.telegram_chat_id, config.state_dir / "telegram.json",
                       config.telegram_api_url)
