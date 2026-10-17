"""Session du site, sans navigateur : la session enregistrée (state/session.json, tenue
à jour par auth.py), sinon des cookies copiés d'un navigateur connecté (anciens fichiers
de session, SESSION_COOKIES), sinon le bot Telegram en demande de nouveaux.

La connexion par mot de passe n'est pas possible : la page /login attend une case
Cloudflare cochée par une personne, que le script ne contourne jamais."""

import json
import logging
import re
import threading
from contextlib import contextmanager

from .auth import SessionStore, decode_session
from .config import Config
from .site import SiteClient, SiteError, WikiMasters

log = logging.getLogger(__name__)

SESSION_COOKIES_FILE = "session_cookies.txt"  # ancienne version : dernière session reçue par Telegram


class SessionGate:
    """Une seule tâche à la fois sur la session du site. Les passes (prioritaires) passent avant la
    défausse, qui vérifie should_yield() entre deux étapes."""

    def __init__(self):
        self._cond = threading.Condition()
        self._busy = False
        self._priority_waiting = 0

    @contextmanager
    def priority(self):
        with self._cond:
            self._priority_waiting += 1
            try:
                while self._busy:
                    self._cond.wait()
            finally:
                self._priority_waiting -= 1
            self._busy = True
        try:
            yield
        finally:
            self._release()

    @contextmanager
    def background(self):
        with self._cond:
            while self._busy or self._priority_waiting:
                self._cond.wait()
            self._busy = True
        try:
            yield
        finally:
            self._release()

    def should_yield(self) -> bool:
        """Vrai si une passe attend la session : la tâche de fond doit s'interrompre."""
        return self._priority_waiting > 0

    def _release(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()


GATE = SessionGate()
_STORES: dict = {}
_STORES_LOCK = threading.Lock()


def session_store(config: Config) -> SessionStore:
    """Session du compte, partagée par les passes et la défausse (un seul renouvellement
    à la fois)."""
    path = config.session_file
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = SessionStore(path, config.base_url, config.supabase_url, config.supabase_anon_key)
        return store


@contextmanager
def open_site(config: Config, debug: bool = False, background: bool = False):
    """Client connecté au site. Une seule tâche à la fois : une tâche de fond (background)
    attend et cède la place aux passes."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with GATE.background() if background else GATE.priority():
        site = WikiMasters(SiteClient(config.base_url, session_store(config)), config, debug=debug)
        ensure_logged_in(site)
        yield site


def parse_cookies(text: str) -> list[tuple[str, str]]:
    """« nom=valeur; nom2=valeur2 » (ou une paire par ligne) → [(nom, valeur), ...]."""
    pairs = []
    for part in re.split(r"[;\n]", text):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            pairs.append((name.strip(), value.strip()))
    return pairs


def ensure_logged_in(site: WikiMasters) -> None:
    config, store = site.config, site.client.store
    if store.has_session():
        if site.is_logged_in():
            return
        log.warning("La session enregistrée ne fonctionne plus.")
        store.clear()
    for source, text, path in _saved_cookies(config):
        if import_session_cookies(site, text, source):
            if path is not None:
                path.unlink(missing_ok=True)  # reprise dans state/session.json
            return
    if not session_from_telegram(site):
        raise SiteError("Aucune session valide : envoyez au bot Telegram les cookies d'un navigateur connecté "
                        "(voir le README), ou réglez SESSION_COOKIES.")


def _saved_cookies(config: Config):
    """Cookies à essayer quand il n'y a pas de session enregistrée : ceux du navigateur des
    anciennes versions, la dernière session reçue par Telegram, puis SESSION_COOKIES."""
    try:
        cookies = json.loads(config.storage_state_file.read_text(encoding="utf-8")).get("cookies") or []
        text = "; ".join(f"{c['name']}={c['value']}" for c in cookies if "auth-token" in c.get("name", ""))
        if text:
            yield "de l'ancien navigateur", text, config.storage_state_file
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    try:
        text = (config.state_dir / SESSION_COOKIES_FILE).read_text(encoding="utf-8").strip()
        if text:
            yield "reçue par Telegram", text, config.state_dir / SESSION_COOKIES_FILE
    except OSError:
        pass
    if config.session_cookies:
        yield "SESSION_COOKIES", config.session_cookies, None


def import_session_cookies(site: WikiMasters, text: str, source: str = "SESSION_COOKIES") -> bool:
    """Session copiée depuis un navigateur connecté (cookies « sb-…-auth-token… »). Elle
    est ensuite renouvelée par le script, et enregistrée à chaque fois."""
    decoded = decode_session(parse_cookies(text))
    if decoded is None:
        log.warning("La session %s ne contient pas de cookie « sb-…-auth-token » lisible.", source)
        return False
    store = site.client.store
    store.adopt(*decoded)
    if site.is_logged_in():
        log.info("Connecté avec la session %s.", source)
        return True
    log.warning("La session %s est expirée ou invalide.", source)
    store.clear()
    return False


def session_from_telegram(site: WikiMasters) -> bool:
    """Le bot Telegram demande de nouveaux cookies et attend la réponse (TELEGRAM_WAIT)."""
    from .telegram import telegram_bot

    config = site.config
    bot = telegram_bot(config)
    if bot is None:
        return False
    return bot.request_session(config.base_url, lambda cookies: import_session_cookies(
        site, cookies, "reçue par Telegram"), config.telegram_wait)
