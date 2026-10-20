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

# Requête d'une passe ou de la défausse pendant une fin d'enchère surveillée : attente au
# plus ce temps (une guerre d'enchères ajoute une minute à chaque mise).
WATCH_WAIT_LIMIT = 65 * 60
SESSION_COOKIES_FILE = "session_cookies.txt"  # ancienne version : dernière session reçue par Telegram


class SessionGate:
    """Une seule tâche à la fois sur la session du site. Les passes (prioritaires) passent avant la
    défausse, qui vérifie should_yield() entre deux étapes."""

    def __init__(self):
        self._cond = threading.Condition()
        self._busy = False
        self._priority_waiting = 0
        self._in_pass = False  # la session est tenue par une passe (pas par la défausse)
        self._watching = 0  # fins d'enchère surveillées à part (watcher.py), sans tenir la session

    @contextmanager
    def priority(self):
        with self._cond:
            self._priority_waiting += 1
            try:
                while self._busy:
                    self._cond.wait()
            finally:
                self._priority_waiting -= 1
            self._busy = self._in_pass = True
        try:
            yield
        finally:
            self._release()

    @contextmanager
    def background(self):
        with self._cond:
            while self._busy or self._priority_waiting or self._watching:
                self._cond.wait()
            self._busy = True
        try:
            yield
        finally:
            self._release()

    @contextmanager
    def watching(self):
        """Une fin d'enchère est surveillée : les requêtes des passes et de la défausse
        attendent (wait_watches) et le fil des prix laisse cette session, pour que rien ne
        gêne la mise de dernière seconde."""
        with self._cond:
            self._watching += 1
        try:
            yield
        finally:
            with self._cond:
                self._watching -= 1
                self._cond.notify_all()

    def wait_watches(self) -> None:
        """Avant chaque requête d'une passe ou de la défausse : une fin d'enchère surveillée
        passe avant tout le reste ; la requête attend qu'elle soit terminée."""
        with self._cond:
            if not self._watching:
                return
            log.info("Fin d'enchère surveillée en cours : la passe attend qu'elle soit terminée.")
            self._cond.wait_for(lambda: not self._watching, WATCH_WAIT_LIMIT)

    def in_pass(self) -> bool:
        """Une passe utilise la session, ou l'attend, ou une fin d'enchère est surveillée :
        le fil des prix ne s'en sert pas."""
        return self._in_pass or self._priority_waiting > 0 or self._watching > 0

    def should_yield(self) -> bool:
        """Vrai si une passe attend la session ou qu'une fin d'enchère est surveillée : la
        tâche de fond doit s'interrompre."""
        return self._priority_waiting > 0 or self._watching > 0

    def _release(self) -> None:
        with self._cond:
            self._busy = self._in_pass = False
            self._cond.notify_all()


_GATES: dict = {}
_STORES: dict = {}
_STORES_LOCK = threading.Lock()


def gate(config: Config) -> SessionGate:
    """Barrière de la session d'un compte (les comptes ne s'attendent pas entre eux)."""
    with _STORES_LOCK:
        return _GATES.setdefault(config.session_file, SessionGate())


def session_store(config: Config) -> SessionStore:
    """Session du compte, partagée par les passes et la défausse (un seul renouvellement
    à la fois)."""
    path = config.session_file
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = SessionStore(path, config.base_url, config.supabase_url, config.supabase_anon_key,
                                                 config.shared_state_dir / "site.json")
        return store


@contextmanager
def open_site(config: Config, debug: bool = False, background: bool = False):
    """Client connecté au site. Une seule tâche à la fois : une tâche de fond (background)
    attend et cède la place aux passes."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock = gate(config)
    with lock.background() if background else lock.priority():
        client = SiteClient(config.base_url, session_store(config))
        client.before = lock.wait_watches  # les fins d'enchère surveillées passent avant
        site = WikiMasters(client, config, debug=debug)
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


def replace_session(config: Config, cookies: str) -> bool | None:
    """Cookies reçus sans demande : adoptés s'ils fonctionnent (True), sinon la session en
    place est gardée (False) ; None si le site ne répond pas."""
    store = session_store(config)
    previous = (store.cookie_name, store.session) if store.has_session() else None
    site = WikiMasters(SiteClient(config.base_url, store), config)
    try:
        if import_session_cookies(site, cookies, "reçue par Telegram"):
            return True
        result = False
    except SiteError:
        result = None
    if previous:
        store.adopt(*previous)
    else:
        store.clear()
    return result


def session_from_telegram(site: WikiMasters) -> bool:
    """Le bot Telegram demande de nouveaux cookies et attend la réponse (TELEGRAM_WAIT)."""
    from .telegram import telegram_bot

    config = site.config
    bot = telegram_bot(config)
    if bot is None:
        return False
    return bot.request_session(config.base_url, lambda cookies: import_session_cookies(
        site, cookies, "reçue par Telegram"), config.telegram_wait)
