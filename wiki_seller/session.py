"""Session du site : un seul navigateur à la fois, et connexion dans cet ordre :
session enregistrée (state/), cookies reçus par Telegram, SESSION_COOKIES, MAIL/PASSWORD,
puis demande de nouveaux cookies par le bot Telegram.

Le script ne contourne jamais une vérification humaine (Cloudflare) : il attend qu'elle
passe seule, ou qu'une personne la fasse (--headed), ou demande une session copiée."""

import logging
import re
import threading
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

from .config import Config
from .site import SiteError, WikiMasters
from .state import SITE_TIMEZONE

log = logging.getLogger(__name__)

SESSION_COOKIES_FILE = "session_cookies.txt"  # dans state/ : dernière session reçue par Telegram
LOGIN_SUBMIT = re.compile(r"^\s*(connexion|se connecter)\s*$", re.I)
# Attente de la vérification anti-bot avant de pouvoir cliquer sur « Connexion ».
LOGIN_WAIT_HEADLESS = 30
LOGIN_WAIT_HEADED = 300


class SessionGate:
    """Un seul navigateur à la fois sur la session : deux navigateurs qui la renouvellent
    en même temps peuvent la faire annuler. Les passes (prioritaires) passent avant la
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


@contextmanager
def open_site(config: Config, debug: bool = False, background: bool = False):
    """Navigateur connecté au site ; la session est enregistrée à la sortie. Un seul à la
    fois : une tâche de fond (background) attend et cède la place aux passes."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with GATE.background() if background else GATE.priority(), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless, executable_path=config.chromium_executable)
        storage = str(config.storage_state_file) if config.storage_state_file.exists() else None
        context = browser.new_context(storage_state=storage, locale="fr-FR", timezone_id=SITE_TIMEZONE,
                                      viewport={"width": 1440, "height": 900})
        context.set_default_timeout(20000)
        site = WikiMasters(context.new_page(), config, debug=debug)
        try:
            ensure_logged_in(site)
            context.storage_state(path=str(config.storage_state_file))
            yield site
        finally:
            try:
                context.storage_state(path=str(config.storage_state_file))
            except Exception:
                pass
            browser.close()


def parse_cookies(text: str) -> list[tuple[str, str]]:
    """« nom=valeur; nom2=valeur2 » (ou une paire par ligne) → [(nom, valeur), ...]."""
    pairs = []
    for part in re.split(r"[;\n]", text):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            pairs.append((name.strip(), value.strip()))
    return pairs


def ensure_logged_in(site: WikiMasters) -> None:
    config = site.config
    site.page.goto(site.url("/collection"), wait_until="domcontentloaded")
    if site.is_logged_in():
        log.info("Session existante réutilisée.")
        return
    received = received_session_cookies(config)
    if received and received != config.session_cookies and import_session_cookies(site, received, "reçue par Telegram"):
        return
    if config.session_cookies and import_session_cookies(site, config.session_cookies):
        return
    try:
        login(site)
    except SiteError:  # vérification Cloudflare, identifiants refusés… : on demande par Telegram
        if not session_from_telegram(site):
            raise


def import_session_cookies(site: WikiMasters, text: str, source: str = "SESSION_COOKIES") -> bool:
    """Session copiée depuis un navigateur connecté : évite la vérification Cloudflare de
    /login. Le site la renouvelle ensuite tout seul, et la version à jour est enregistrée."""
    cookies = parse_cookies(text)
    if not cookies:
        log.warning("La session %s ne contient aucun cookie « nom=valeur ».", source)
        return False
    context = site.page.context
    context.add_cookies([{"name": name, "value": value, "url": site.config.base_url} for name, value in cookies])
    site.page.goto(site.url("/collection"), wait_until="domcontentloaded")
    if site.is_logged_in():
        log.info("Connecté avec la session %s (%d cookie(s)).", source, len(cookies))
        return True
    log.warning("La session %s est expirée ou invalide.", source)
    context.clear_cookies()
    return False


def received_session_cookies(config: Config) -> str:
    try:
        return (config.state_dir / SESSION_COOKIES_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def session_from_telegram(site: WikiMasters) -> bool:
    """Le bot Telegram demande de nouveaux cookies et attend la réponse (TELEGRAM_WAIT).
    Les cookies acceptés sont gardés pour les redémarrages."""
    from .telegram import telegram_bot

    config = site.config
    bot = telegram_bot(config)
    if bot is None:
        return False

    def try_cookies(cookies: str) -> bool:
        if not import_session_cookies(site, cookies, "reçue par Telegram"):
            return False
        path = config.state_dir / SESSION_COOKIES_FILE
        path.write_text(cookies, encoding="utf-8")
        path.chmod(0o600)
        return True

    return bot.request_session(config.base_url, try_cookies, config.telegram_wait)


def login(site: WikiMasters) -> None:
    """Connexion par /login. « Connexion » reste désactivé tant que la vérification
    Cloudflare Turnstile n'est pas validée : on attend qu'elle passe seule, ou qu'une
    personne coche la case dans le navigateur ouvert avec --headed."""
    config, page = site.config, site.page
    log.info("Connexion à %s avec %s", config.base_url, config.mail)
    page.goto(site.url("/login"), wait_until="domcontentloaded")
    password = page.locator("input[type=password]:visible").first
    password.wait_for(state="visible", timeout=20000)
    page.locator("input[type=email]:visible, input[autocomplete=email]:visible").first.fill(config.mail)
    password.fill(config.password)
    site.step("login-filled")

    submit = page.get_by_role("button", name=LOGIN_SUBMIT).first
    wait_s = LOGIN_WAIT_HEADLESS if config.headless else LOGIN_WAIT_HEADED
    if not submit.is_enabled():
        log.info("Bouton « Connexion » désactivé : attente de la vérification anti-bot Cloudflare "
                 "(jusqu'à %d s)%s.", wait_s, "" if config.headless else " ; cochez la case dans le navigateur")
    deadline = time.monotonic() + wait_s
    while not submit.is_enabled():
        if time.monotonic() > deadline:
            site.snapshot("login-blocked")
            raise SiteError(
                "Connexion bloquée par la vérification anti-bot Cloudflare. Renouvelez la session par "
                "Telegram, ou lancez une fois `python -m wiki_seller --dry-run --headed` sur une machine "
                "avec écran et cochez la case vous-même.")
        page.wait_for_timeout(500)

    submit.click()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if site.is_logged_in():
            log.info("Connecté.")
            return
    site.snapshot("login-failed")
    raise SiteError("Connexion échouée (identifiants refusés ou formulaire différent ?)")
