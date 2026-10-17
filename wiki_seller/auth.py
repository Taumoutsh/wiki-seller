"""Session du site, sans navigateur. Le site s'appuie sur Supabase : la session (jeton
d'accès valable 1 h, jeton de renouvellement) est rangée dans les cookies
« sb-<projet>-auth-token.0 », « .1 »… (JSON encodé en base64, découpé en morceaux).

Dans un navigateur, le code de la page renouvelle la session ; ici, SessionStore le fait
lui-même avant l'expiration, comme lui (POST <supabase>/auth/v1/token). Le jeton de
renouvellement change à chaque fois : la nouvelle session est enregistrée aussitôt dans
state/session.json, sinon elle serait perdue au redémarrage."""

import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote

from .state import read_json, write_json

log = logging.getLogger(__name__)

COOKIE_NAME = re.compile(r"^(sb-([a-z0-9]+)-auth-token)(?:\.(\d+))?$")
CHUNK_SIZE = 3180  # taille des morceaux de cookie, comme la bibliothèque du site
REFRESH_MARGIN = 300  # s : la session est renouvelée 5 min avant son expiration
REFRESH_TIMEOUT = 30
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
SCRIPT = re.compile(r"""["'(]((?:https?://[^"'()\s]+)?/_next/static/[^"'()\s]+?\.js(?:\?[^"'()\s]*)?)["')]""")


class SessionDead(Exception):
    """Le site refuse de renouveler la session : il faut de nouveaux cookies."""


class RefreshUnavailable(Exception):
    """Renouvellement impossible pour l'instant (réseau, panne) : la session est gardée."""


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decode_session(pairs: list[tuple[str, str]]) -> tuple[str, dict] | None:
    """(nom du cookie, session) à partir des cookies « sb-…-auth-token[.N] », ou None."""
    groups: dict[str, dict[int, str]] = {}
    for name, value in pairs:
        match = COOKIE_NAME.match(name.strip())
        if match:
            groups.setdefault(match.group(1), {})[int(match.group(3) or 0)] = value.strip()
    for base, chunks in groups.items():
        raw = unquote("".join(chunks[i] for i in sorted(chunks)))
        try:
            data = json.loads(_b64decode(raw[7:]) if raw.startswith("base64-") else raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("access_token") and data.get("refresh_token"):
            return base, data
    return None


def encode_session(cookie_name: str, session: dict) -> list[tuple[str, str]]:
    """Cookies « nom.0=…; nom.1=… » d'une session, au format du site."""
    encoded = "base64-" + base64.urlsafe_b64encode(
        json.dumps(session, separators=(",", ":")).encode()).decode().rstrip("=")
    return [(f"{cookie_name}.{i}", encoded[start:start + CHUNK_SIZE])
            for i, start in enumerate(range(0, len(encoded), CHUNK_SIZE))]


def jwt_claims(token: str) -> dict:
    try:
        return json.loads(_b64decode(token.split(".")[1]))
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}


def find_anon_key(base_url: str, project: str, fetch) -> str | None:
    """Clé publique Supabase du site (rôle « anon »), lue dans son code JavaScript, comme
    le navigateur la reçoit. fetch(url) → texte."""
    seen: set[str] = set()
    for page in ("/login", "/", "/marketplace"):
        try:
            html = fetch(base_url + page)
        except Exception:
            continue
        for script in SCRIPT.findall(html):
            url = script if script.startswith("http") else base_url + script
            if url in seen:
                continue
            seen.add(url)
            try:
                code = fetch(url)
            except Exception:
                continue
            for token in JWT.findall(code):
                claims = jwt_claims(token)
                if claims.get("role") == "anon" and claims.get("ref") in (project, None):
                    return token
    return None


class SessionStore:
    """Session d'un compte (state/session.json), partagée par les fils du compte."""

    def __init__(self, path: Path, base_url: str, supabase_url: str = "", anon_key: str = ""):
        self.path, self.base_url = path, base_url
        self._supabase_url, self._anon_key = supabase_url.rstrip("/"), anon_key
        self._lock = threading.RLock()
        data = read_json(path, {})
        self.cookie_name: str | None = data.get("cookie")
        self.session: dict | None = data.get("session")

    # ─────────────────────────── État ───────────────────────────

    @property
    def project(self) -> str | None:
        match = COOKIE_NAME.match(self.cookie_name or "")
        return match.group(2) if match else None

    def has_session(self) -> bool:
        return bool(self.session and self.cookie_name)

    def adopt(self, cookie_name: str, session: dict) -> None:
        """Nouvelle session (cookies reçus, renouvellement) : enregistrée aussitôt."""
        session = dict(session)
        if not session.get("expires_at") and session.get("expires_in"):
            session["expires_at"] = int(time.time()) + int(session["expires_in"])
        with self._lock:
            self.cookie_name, self.session = cookie_name, session
            write_json(self.path, {"cookie": cookie_name, "session": session})
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def clear(self) -> None:
        with self._lock:
            self.session = None
            self.path.unlink(missing_ok=True)

    def cookies(self) -> list[tuple[str, str]]:
        with self._lock:
            return encode_session(self.cookie_name, self.session) if self.has_session() else []

    # ─────────────────────────── Renouvellement ───────────────────────────

    def ensure_fresh(self, force: bool = False) -> None:
        """Renouvelle la session si elle expire dans moins de REFRESH_MARGIN (ou si force)."""
        with self._lock:
            if not self.has_session():
                return
            expires_at = self.session.get("expires_at") or 0
            if not force and expires_at - time.time() > REFRESH_MARGIN:
                return
            self._refresh()

    def _refresh(self) -> None:
        url = f"{self.supabase_url()}/auth/v1/token?grant_type=refresh_token"
        body = json.dumps({"refresh_token": self.session["refresh_token"]}).encode()
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "apikey": self.anon_key(), "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT) as response:
                fresh = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            if exc.code in (400, 401, 403):
                raise SessionDead(f"renouvellement refusé (HTTP {exc.code} : {detail})") from exc
            raise RefreshUnavailable(f"HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise RefreshUnavailable(str(exc).splitlines()[0] if str(exc) else type(exc).__name__) from exc
        if not fresh.get("access_token") or not fresh.get("refresh_token"):
            raise RefreshUnavailable("réponse inattendue du renouvellement")
        self.adopt(self.cookie_name, fresh)
        log.info("Session renouvelée (valable %d min).", int(fresh.get("expires_in") or 3600) // 60)

    def supabase_url(self) -> str:
        return self._supabase_url or f"https://{self.project}.supabase.co"

    def anon_key(self) -> str:
        """Clé publique du site : réglage SUPABASE_ANON_KEY, sinon lue dans son code (et
        gardée dans state/site.json)."""
        if self._anon_key:
            return self._anon_key
        cache = self.path.with_name("site.json")
        cached = read_json(cache, {}).get("anon_key")
        if cached and jwt_claims(cached).get("ref") in (self.project, None):
            self._anon_key = cached
            return cached

        def fetch(url: str) -> str:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")

        key = find_anon_key(self.base_url, self.project, fetch)
        if not key:
            raise RefreshUnavailable("clé publique du site introuvable (réglez SUPABASE_ANON_KEY)")
        write_json(cache, {"anon_key": key})
        self._anon_key = key
        return key
