"""Client de wiki-masters.com : l'API JSON que le site appelle lui-même, en HTTP simple,
avec les cookies de la session (auth.py ; la connexion est dans session.py)."""

import dataclasses
import http.client
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from enum import Enum
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import quote_plus

from .auth import USER_AGENT, RefreshUnavailable, SessionDead, SessionStore, decode_session, jwt_claims
from .config import Config, parse_duration
from .selection import normalize_name, sale_price
from .state import parse_time

log = logging.getLogger(__name__)

COLLECTION_PAGE_GUARD = 500
DEFAULT_MAX_AUCTIONS = 5
MINE_PATH = "/api/marketplace?page=1&limit=50&sort=recent&mine=1"
MARKET_SALES_PATH = "/api/marketplace/cards/{card_id}/sales"  # détail des ventes (Marché)
API_TIMEOUT = 60  # s : une requête à l'API sans réponse est abandonnée
QUICK_TIMEOUT = 5  # s : lecture d'une enchère pendant sa fin
BID_TIMEOUT = 8  # s : mise sans réponse, vérifiée puis renvoyée
BID_RETRY_PAUSE = 1  # s entre deux essais de mise
LISTING_COPY_TRIES = 3  # exemplaires essayés pour une mise en vente (un peut être en vente ou en échange)

SLOTS_FULL_TEXT = re.compile(r"maximum|limite|trop d.ench|max.*ench|concurrent", re.I)
NOT_OWNED_TEXT = re.compile(r"ne poss[ée]dez pas", re.I)  # 409 : exemplaire défaussé, vendu, échangé…
# Erreurs réseau (délai dépassé, connexion coupée) : le site bogue parfois.
NETWORK_ERRORS = (OSError, http.client.HTTPException)


class SessionExpired(Exception):
    pass


def min_bid(auction: dict) -> int:
    """Mise minimale, calculée comme la page de l'enchère : la mise de départ s'il n'y a
    pas encore de mise, sinon max(⌈1,1 × mise actuelle⌉, mise actuelle + 1)."""
    current = auction.get("current_bid")
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        return max(math.ceil(round(1.1 * current, 6)), int(current) + 1)
    return int(auction.get("base_amount") or auction.get("effective_bid") or 0)


@dataclass
class Response:
    status: int
    data: object
    text: str
    headers: Message


class SiteClient:
    """Requêtes HTTP au site avec les cookies de la session. La session est renouvelée
    avant son expiration (et une fois après un refus 401) ; une session renvoyée par le
    site dans ses cookies est adoptée aussitôt."""

    def __init__(self, base_url: str, store: SessionStore):
        self.base_url, self.store = base_url.rstrip("/"), store
        self.cookies: dict[str, str] = {}  # autres cookies posés par le site
        self.last: Response | None = None  # dernière réponse (--debug)
        self.before = None  # appelé avant chaque requête (attente d'une fin d'enchère surveillée)

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 60,
                accept: str = "application/json") -> Response:
        if self.before is not None:
            self.before()
        for attempt in (1, 2):
            self._fresh()
            used = (self.store.session or {}).get("access_token")
            headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "fr-FR,fr;q=0.9",
                       "Referer": f"{self.base_url}/marketplace"}
            cookie = "; ".join(f"{k}={v}" for k, v in [*self.store.cookies(), *self.cookies.items()])
            if cookie:
                headers["Cookie"] = cookie
            data = None
            if method != "GET":
                headers["Origin"] = self.base_url
                data = b""
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode()
            request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status, raw, reply_headers = response.status, response.read(), response.headers
            except urllib.error.HTTPError as exc:
                status, raw, reply_headers = exc.code, exc.read(), exc.headers
            self._take_cookies(reply_headers)
            if status == 401 and attempt == 1 and self.store.has_session():
                if self._fresh(force=True, refused=used):
                    continue  # jeton refusé : renouvelé, on réessaie une fois
            break
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        self.last = Response(status, parsed, text, reply_headers)
        return self.last

    def _fresh(self, force: bool = False, refused: str | None = None) -> bool:
        try:
            self.store.ensure_fresh(force, refused)
            return True
        except SessionDead as exc:
            raise SessionExpired(str(exc)) from exc
        except RefreshUnavailable as exc:
            log.warning("Session non renouvelée pour l'instant (%s).", exc)
            return False

    def _take_cookies(self, headers: Message) -> None:
        auth: list[tuple[str, str]] = []
        for line in headers.get_all("Set-Cookie") or []:
            try:
                parsed = SimpleCookie(line)
            except CookieError:
                continue
            for name, morsel in parsed.items():
                if name.startswith("sb-") and "auth-token" in name:
                    if morsel.value and morsel["max-age"] != "0":
                        auth.append((name, morsel.value))
                elif morsel["max-age"] == "0" or not morsel.value:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        decoded = decode_session(auth) if auth else None
        if decoded and decoded[1].get("refresh_token") != (self.store.session or {}).get("refresh_token"):
            self.store.adopt(*decoded)  # le site a renouvelé la session lui-même


class SiteError(Exception):
    pass


class HumanCheckRequired(SiteError):
    """Le site demande une vérification anti-bot (captcha), à faire par une personne dans
    un navigateur : le script ne la contourne jamais."""


HUMAN_CHECK_CODE = "human_verification_required"
HUMAN_CHECK_REMIND = timedelta(hours=3)  # pas de nouveau message Telegram avant ce délai (par compte)
HUMAN_CHECK_TEXT = "🤖 Vérification anti-bot demandée ({what}) : validez-la sur wiki-masters.com, le bot reprendra seul."
_HUMAN_CHECKS: dict[str, datetime] = {}  # compte → dernier message envoyé
_HUMAN_CHECKS_LOCK = threading.Lock()


def human_check_message(status: int, data: object, text: str) -> str | None:
    """Message du site s'il demande une vérification anti-bot, sinon None."""
    if isinstance(data, dict) and data.get("code") == HUMAN_CHECK_CODE:
        return str(data.get("error") or data.get("message") or HUMAN_CHECK_CODE)
    if status in (401, 403, 429) and (HUMAN_CHECK_CODE in text or re.search(r"anti.?bot", text, re.I)):
        return text[:200]
    return None


class PriceApiUnavailable(Exception):
    """L'API des prix a refusé la requête. `transient` : refus passager (trop de
    requêtes, panne), et non un accès interdit."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class DiscardStatus(Enum):
    DISCARDED = "défaussée"
    DRY_RUN = "défausse simulée (non envoyée)"
    FAILED = "échec de la défausse"


class ListingStatus(Enum):
    LISTED = "mise en vente"
    DRY_RUN = "simulation (non envoyée)"
    SLOTS_FULL = "plus de place aux enchères"
    NO_PRICE = "prix calculé nul"
    NOT_OWNED = "plus dans la collection"
    HUMAN_CHECK = "vérification anti-bot demandée"
    FAILED = "échec"


@dataclass
class ListingResult:
    status: ListingStatus
    price: int | None = None
    average: float | None = None
    detail: str = ""


@dataclass
class MarketState:
    selling: list[dict]
    max_auctions: int
    history: list = dataclasses.field(default_factory=list)  # vos ventes terminées
    mine: dict = dataclasses.field(default_factory=dict)  # réponse complète de mine=1

    @property
    def free_slots(self) -> int:
        return max(0, self.max_auctions - len(self.selling))

    def earliest_end(self) -> datetime | None:
        ends = [parse_time(a.get("end_at")) for a in self.selling if isinstance(a, dict)]
        ends = [e for e in ends if e]
        return min(ends) if ends else None


@dataclass
class CollectionScan:
    """Lecture de la collection en cours, reprise page par page après une pause."""
    rarity: str | None = None
    entries: list[dict] = dataclasses.field(default_factory=list)
    seen: set[str] = dataclasses.field(default_factory=set)
    in_trade: set[str] = dataclasses.field(default_factory=set)
    next_page: int = 0
    done: bool = False


def parse_int(text: str) -> int | None:
    """Les prix du site sont des entiers : on ne garde que les chiffres."""
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def entry_rarity(entry: dict) -> str:
    card = entry.get("card") if isinstance(entry.get("card"), dict) else entry
    # snapshot_rarity : rareté de l'exemplaire mis en vente (une carte existe en plusieurs raretés)
    return str(entry.get("snapshot_rarity") or card.get("rarity") or entry.get("rarity") or "").upper()


def auction_minutes(label: str) -> int:
    """« 1 h » → 60, « 10 min » → 10 (durée envoyée à l'API)."""
    duration = parse_duration(label)
    if duration is None:
        raise SiteError(f"Durée d'enchère illisible : {label!r}")
    return int(duration.total_seconds() // 60)


def search_queries(name: str) -> list[str]:
    """Requêtes à essayer pour la recherche du Marché, de la plus précise à la plus large."""
    queries = [name.strip()]
    without_parens = re.sub(r"\s*\([^)]*\)", " ", name).strip()
    plain = re.sub(r"[^\w\s'-]", " ", name)
    for query in (without_parens, plain):
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in queries:
            queries.append(query)
    return queries


class WikiMasters:
    def __init__(self, client: SiteClient, config: Config, debug: bool = False):
        self.client = client
        self.config = config
        self.debug = debug
        self.retries = 0  # nouveaux essais après « trop de requêtes » ou panne du site

    # ─────────────────────────── Utilitaires ───────────────────────────

    def url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def snapshot(self, name: str) -> Path | None:
        """Dernière réponse du site, enregistrée pour comprendre un problème après coup."""
        last = self.client.last
        if last is None:
            return None
        try:
            self.config.debug_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-zA-Z0-9]+', '_', name)[:60]}"
            path = self.config.debug_dir / f"{stem}.txt"
            path.write_text(f"HTTP {last.status}\n\n{last.text}", encoding="utf-8")
            log.info("Réponse enregistrée : %s", path)
            return path
        except Exception as exc:  # un enregistrement raté ne doit jamais faire échouer une action
            log.warning("Enregistrement impossible (%s)", exc)
            return None

    def step(self, name: str) -> None:
        if self.debug:
            self.snapshot(name)

    # ─────────────────────────── API JSON ───────────────────────────

    def api_get(self, path: str, timeout: float = API_TIMEOUT) -> tuple[int, object]:
        """GET ; une requête sans réponse au bout de `timeout` secondes lève une exception."""
        response = self.client.request("GET", path, timeout=timeout)
        return response.status, response.data

    def api_post(self, path: str, body: dict | None = None, timeout: float = API_TIMEOUT) -> tuple[int, object, str]:
        """POST. Renvoie (code, JSON ou None, texte brut)."""
        response = self.client.request("POST", path, body, timeout=timeout)
        if response.status == 401 or (response.status == 403 and "Non autoris" in response.text):
            raise SessionExpired(f"{path} → HTTP {response.status}")
        return response.status, response.data, response.text[:500]

    def api_get_ok(self, path: str) -> object:
        """GET avec trois essais (2 s puis 4 s) sur « trop de requêtes » ou panne du site."""
        for attempt in range(3):
            try:
                status, data = self.api_get(path)
            except NETWORK_ERRORS as exc:  # pas de réponse : comme une panne
                log.info("%s : pas de réponse du site (%s).", path, str(exc).splitlines()[0][:100] if str(exc) else exc)
                status, data = 0, None
            if status in (401, 403):
                raise SessionExpired(f"{path} → HTTP {status}")
            if 200 <= status < 300:
                return data
            if status == 404:
                return None
            if status and status != 429 and status < 500:
                raise SiteError(f"{path} → HTTP {status}")
            self.retries += 1
            time.sleep(2 * (attempt + 1))
        raise SiteError(f"{path} indisponible")

    def user_id(self) -> str | None:
        """Identifiant du compte sur le site, tiré de la session (utilisateur Supabase)."""
        session = self.client.store.session or {}
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        return user.get("id") or jwt_claims(session.get("access_token") or "").get("sub")

    def is_logged_in(self) -> bool:
        """True si le site accepte la session, False s'il la refuse. Site injoignable ou
        en panne : SiteError (la session n'est pas en cause, elle est gardée)."""
        if not self.client.store.has_session():
            return False
        for attempt in range(3):
            try:
                status, _ = self.api_get("/api/wikibidous", timeout=QUICK_TIMEOUT * 4)
            except SessionExpired:
                return False
            except NETWORK_ERRORS as exc:
                status, detail = 0, str(exc).splitlines()[0][:100] if str(exc) else type(exc).__name__
            else:
                detail = f"HTTP {status}"
            if 200 <= status < 300:
                return True
            if status in (401, 403):
                return False
            time.sleep(2 * (attempt + 1))
        raise SiteError(f"le site ne répond pas ({detail}) ; nouvel essai plus tard")

    # ─────────────────────────── Collection ───────────────────────────

    def fetch_collection(self, rarities: tuple[str, ...] = ()) -> tuple[list[dict], set[str]]:
        """Entrées de la collection (limitées aux raretés demandées, filtrées par le site
        lui-même) + les exemplaires engagés dans un échange."""
        entries: list[dict] = []
        in_trade: set[str] = set()
        seen: set[str] = set()
        for rarity in rarities or (None,):
            scan = CollectionScan(rarity=rarity, seen=seen)
            self.scan_collection(scan)
            entries += [e for e in scan.entries if not rarities or entry_rarity(e) in rarities]
            in_trade |= scan.in_trade
        log.info("Collection chargée : %d entrée(s)%s.", len(entries),
                 f" de rareté {', '.join(rarities)}" if rarities else "")
        return entries, in_trade

    def scan_collection(self, scan: CollectionScan, should_stop=lambda: False, on_page=lambda scan: None) -> bool:
        """Lit la collection page par page à partir de scan.next_page. S'arrête entre deux
        pages si should_stop() : un nouvel appel reprend là où la lecture s'était arrêtée.
        Renvoie True quand toute la collection est lue."""
        query = f"&rarity={scan.rarity}" if scan.rarity else ""
        while not scan.done:
            if scan.next_page >= COLLECTION_PAGE_GUARD:
                scan.done = True
                break
            if should_stop():
                return False
            data = self.api_get_ok(f"/api/my-collection?sort=rarity&page={scan.next_page}&stats=0{query}")
            items = data.get("collection") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise SiteError("Réponse inattendue de /api/my-collection")
            scan.in_trade.update(str(i) for i in (data.get("pendingTradeCardIds") or []))
            new = 0
            for item in items:
                key = str(item.get("id") or id(item))
                if key not in scan.seen:
                    scan.seen.add(key)
                    scan.entries.append(item)
                    new += 1
            scan.next_page += 1
            on_page(scan)
            if not new:
                scan.done = True
            else:
                time.sleep(0.2)
        return True

    # ─────────────────────────── Marché ───────────────────────────

    def fetch_balance(self) -> int | None:
        """Solde du compte en wikibidous (/api/wikibidous), ou None s'il est illisible."""
        try:
            status, data = self.api_get("/api/wikibidous")
        except Exception:
            return None
        if not 200 <= status < 300:
            return None
        if isinstance(data, dict):
            data = next((data[k] for k in ("balance", "wikibidous", "amount", "total", "solde")
                         if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool)), None)
        return int(data) if isinstance(data, (int, float)) and not isinstance(data, bool) else None

    def fetch_mine(self) -> dict:
        """Onglets « Mes ventes » (selling), « Mes enchères » (bidding), « Gagnées » (won)
        et l'historique de vos ventes (history)."""
        data = self.api_get_ok(MINE_PATH)
        if not isinstance(data, dict) or not isinstance(data.get("selling"), list):
            raise SiteError("Réponse inattendue pour les enchères en cours")
        return data

    def fetch_market_state(self) -> MarketState:
        data = self.fetch_mine()
        selling = [a for a in data["selling"] if isinstance(a, dict) and a.get("status", "active") == "active"]
        max_auctions = data.get("maxConcurrentAuctions")
        if not isinstance(max_auctions, int) or max_auctions < 1:
            max_auctions = DEFAULT_MAX_AUCTIONS
        history = [a for a in data.get("history") or [] if isinstance(a, dict)]
        return MarketState(selling=selling, max_auctions=max_auctions, history=history, mine=data)

    def fetch_average(self, card_id: str, rarity: str | None) -> tuple[float | None, int | None, float | None]:
        """(prix moyen de la carte, nombre de ventes, plus bas des prix connus : moyenne ou
        dernière vente) ; prix None si aucune vente connue."""
        try:
            data = self.api_get_ok(f"/api/marketplace/cards/{card_id}/sales?scope=summary")
        except SessionExpired as exc:
            raise PriceApiUnavailable(str(exc)) from exc
        except SiteError as exc:
            raise PriceApiUnavailable(str(exc), transient=True) from exc
        return self._average_from(data, rarity)

    def fetch_market_sales(self, card_id: str) -> object:
        """Détail des ventes de la carte (comptes ayant accès au Marché) ; SessionExpired
        si le site le refuse."""
        return self.api_get_ok(MARKET_SALES_PATH.format(card_id=card_id))

    @staticmethod
    def _average_from(data: object, rarity: str | None) -> tuple[float | None, int | None, float | None]:
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, dict) or not summary:
            return None, 0, None
        entry = summary.get(rarity) if rarity else None
        if entry is None and len(summary) == 1:
            entry = next(iter(summary.values()))
        if not isinstance(entry, dict):
            return None, 0, None

        def number(value) -> float | None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        average, latest = number(entry.get("average")), number(entry.get("latest"))
        count = entry.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else None
        return average, count, (min(average, latest) if average and latest else average)

    def refresh_market(self) -> None:
        """Recharge le Marché : après la fin d'une enchère, le site ne la compte pas
        toujours comme terminée tant que la page n'a pas été rechargée."""
        try:
            self.client.request("GET", "/marketplace", accept="text/html", timeout=API_TIMEOUT)
        except NETWORK_ERRORS:
            log.warning("Le Marché n'a pas répondu à temps ; on continue.")

    def server_clock_offset(self) -> timedelta:
        """Écart entre l'horloge du site (en-tête Date) et la nôtre, pour viser juste
        avant la fin d'une enchère."""
        try:
            date = self.client.request("GET", "/api/wikibidous", timeout=QUICK_TIMEOUT).headers.get("Date")
            server = parsedate_to_datetime(date) if date else None
        except Exception:
            server = None
        if not server:
            return timedelta(0)
        offset = server - datetime.now(timezone.utc)
        return offset if abs(offset) > timedelta(seconds=2) else timedelta(0)

    # ─────────────────────────── Vente et défausse ───────────────────────────

    def human_check(self, what: str, message: str) -> None:
        """Vérification anti-bot demandée par le site : journal, et message Telegram au plus
        une fois toutes les HUMAN_CHECK_REMIND pour ce compte."""
        log.warning("Le site demande une vérification anti-bot (%s) : %s", what, message)
        key, now = str(self.config.session_file), datetime.now(timezone.utc)
        with _HUMAN_CHECKS_LOCK:
            last = _HUMAN_CHECKS.get(key)
            if last is not None and now - last < HUMAN_CHECK_REMIND:
                return
            _HUMAN_CHECKS[key] = now
        from .telegram import notify  # telegram lit les commandes, qui lisent le site

        notify(self.config, HUMAN_CHECK_TEXT.format(what=what))

    def list_card(self, card_id: str, copy_ids: tuple[str, ...], title: str, average: float | None,
                  dry_run: bool, adjust=None, duration_label: str | None = None,
                  price: int | None = None) -> ListingResult:
        """Mise en vente : POST /api/marketplace {card_id, base_amount, duration_minutes}.
        Malgré son nom, `card_id` attend l'identifiant de l'exemplaire possédé (avec celui
        de la carte, le site répond 409 « Vous ne possédez pas cette carte ») : on essaie
        les exemplaires, puis la carte en dernier recours. Un exemplaire que le site dit ne
        plus vous appartenir (409 « Vous ne possédez pas cette carte » : défaussé, vendu,
        échangé depuis la lecture de la collection) arrête tout : NOT_OWNED, sans autre
        essai. `price` : prix exact (/sell), sinon PRICE_RATIO du prix moyen, ajusté par
        `adjust`."""
        if price is None:
            price = sale_price(average, self.config.price_ratio)
            if adjust and price >= 1:
                price = adjust(price, average)
        if price < 1:
            return ListingResult(ListingStatus.NO_PRICE, average=average, detail=f"moyenne {average:g}")
        duration = auction_minutes(duration_label or self.config.auction_duration_label)
        if dry_run:
            return ListingResult(ListingStatus.DRY_RUN, price=price, average=average)
        status, text = 0, ""
        for candidate in (*copy_ids[:LISTING_COPY_TRIES], card_id):
            body = {"card_id": candidate, "base_amount": price, "duration_minutes": duration}
            try:
                status, data, text = self.api_post("/api/marketplace", body)
            except NETWORK_ERRORS as exc:  # réponse perdue : la passe suivante verra si elle est en vente
                return ListingResult(ListingStatus.FAILED, price, average, f"pas de réponse ({exc})")
            if 200 <= status < 300:
                if candidate == card_id and copy_ids:
                    log.info("Mise en vente acceptée avec l'identifiant de la carte (%s).", title)
                return ListingResult(ListingStatus.LISTED, price=price, average=average)
            if SLOTS_FULL_TEXT.search(text):
                return ListingResult(ListingStatus.SLOTS_FULL, detail=text[:200])
            message = str(data.get("error") or data.get("message") or text) if isinstance(data, dict) else text
            if (check := human_check_message(status, data, text)) is not None:
                self.human_check(f"mise en vente de « {title} »", check)
                return ListingResult(ListingStatus.HUMAN_CHECK, price, average, f"HTTP {status} : {check}")
            if status == 409 and NOT_OWNED_TEXT.search(message):
                return ListingResult(ListingStatus.NOT_OWNED, price, average, f"HTTP 409 : {message[:200]}")
            if status not in (400, 404, 409, 422):
                break
        return ListingResult(ListingStatus.FAILED, price, average, f"HTTP {status} : {text[:200]}")

    def discard_card(self, copy_id: str, dry_run: bool) -> tuple[DiscardStatus, str]:
        """Défausse un exemplaire : la requête que la fiche envoie après la confirmation
        « Défausser cette carte ? » (POST /api/user-cards/{id}/discard)."""
        path = f"/api/user-cards/{copy_id}/discard"
        if dry_run:
            return DiscardStatus.DRY_RUN, ""
        try:
            status, data, text = self.api_post(path)
        except NETWORK_ERRORS as exc:
            return DiscardStatus.FAILED, f"POST {path} → pas de réponse ({exc})"
        if 200 <= status < 300:
            return DiscardStatus.DISCARDED, f"POST {path}"
        if (check := human_check_message(status, data, text)) is not None:
            self.human_check("défausse", check)
            raise HumanCheckRequired(check)  # chaque défausse échouerait : la défausse s'arrête
        return DiscardStatus.FAILED, f"POST {path} → HTTP {status} : {text[:200]}"

    # ─────────────────────────── Achats ───────────────────────────

    def search_auctions(self, name: str) -> list[dict]:
        """Enchères trouvées par la recherche du Marché. Elle ne trouve rien dès que la
        requête contient des parenthèses (« Dewey Martin (acteur) ») : on réessaie alors
        sans elles, puis sans ponctuation. Le titre exact est vérifié par l'appelant."""
        found: dict[str, dict] = {}
        for query in search_queries(name):
            data = self.api_get_ok(f"/api/marketplace?page=1&limit=50&sort=recent&q={quote_plus(query)}")
            for auction in (data.get("auctions") if isinstance(data, dict) else None) or []:
                if isinstance(auction, dict) and auction.get("id"):
                    found.setdefault(auction["id"], auction)
            titles = {normalize_name(((a.get("card") or {}).get("wikipedia_title")) or "") for a in found.values()}
            if normalize_name(name) in titles:
                break
        return list(found.values())

    def fetch_auction(self, auction_id: str) -> dict | None:
        data = self.api_get_ok(f"/api/marketplace/{auction_id}")
        auction = data.get("auction") if isinstance(data, dict) else None
        return auction if isinstance(auction, dict) else None

    def read_auction(self, auction_id: str, quiet: bool = False) -> dict | None:
        """Lecture rapide d'une enchère, en un seul essai (surveillance de fin d'enchère) :
        None si le site ne répond pas à temps ou se trompe. quiet : sans ligne au journal
        (l'appelant résume les lectures sans réponse)."""
        try:
            status, data = self.api_get(f"/api/marketplace/{auction_id}", timeout=QUICK_TIMEOUT)
        except Exception as exc:
            (log.debug if quiet else log.info)("Enchère %s : pas de réponse du site (%s).", auction_id,
                                               str(exc).splitlines()[0][:100])
            return None
        if status in (401, 403):
            raise SessionExpired(f"/api/marketplace/{auction_id} → HTTP {status}")
        auction = data.get("auction") if 200 <= status < 300 and isinstance(data, dict) else None
        return auction if isinstance(auction, dict) else None

    def _bid_went_through(self, auction_id: str, amount: int, my_id: str | None) -> bool:
        """La mise est-elle enregistrée (malgré une réponse perdue) ?"""
        auction = self.read_auction(auction_id) or {}
        current = auction.get("current_bid")
        return (isinstance(current, (int, float)) and current >= amount
                and (not my_id or auction.get("current_bidder_id") == my_id))

    def post_bid(self, auction_id: str, amount: int, deadline: float | None = None,
                 my_id: str | None = None, refusal: dict | None = None) -> bool:
        """Mise (POST /api/marketplace/<id>/bid {amount}) ; True si acceptée. Le site ne
        répond pas toujours (délai dépassé, erreur 5xx) : on vérifie alors si la mise est
        passée, sinon on la renvoie, jusqu'à une réponse ou jusqu'à `deadline`
        (time.monotonic()). Mise refusée : la réponse du site est copiée dans `refusal`
        (code « bid_too_low » avec le minimum « min », « insufficient_balance »…)."""
        path = f"/api/marketplace/{auction_id}/bid"
        tries = 0
        while True:
            tries += 1
            try:
                status, data, text = self.api_post(path, {"amount": amount}, timeout=BID_TIMEOUT)
            except SessionExpired:
                raise
            except Exception as exc:
                status, data, text = 0, None, str(exc).splitlines()[0][:200]
            if 200 <= status < 300:
                confirmed = data if isinstance(data, dict) and "current_bid" in data else self.read_auction(auction_id)
                if (confirmed or {}).get("current_bid") and confirmed["current_bid"] >= amount:
                    return True
            answered = 400 <= status < 500 and status not in (408, 425, 429)
            if (tries > 1 or not answered) and self._bid_went_through(auction_id, amount, my_id):
                return True  # un essai précédent était passé sans que la réponse arrive
            if answered:
                log.warning("Mise de %d refusée par le site (HTTP %d : %s).", amount, status, text[:200])
                if refusal is not None and isinstance(data, dict):
                    refusal.update(data)
                return False
            if deadline is None or time.monotonic() + BID_RETRY_PAUSE >= deadline:
                log.warning("Mise de %d : pas de réponse du site après %d essai(s) (%s).", amount, tries,
                            f"HTTP {status}" if status else text)
                return False
            log.info("Mise de %d : pas de réponse du site (%s), nouvel essai.", amount,
                     f"HTTP {status}" if status else text)
            time.sleep(BID_RETRY_PAUSE)

    def place_bid(self, auction_id: str, max_price: int, dry_run: bool, deadline: float | None = None,
                  my_id: str | None = None, exact: bool = False) -> tuple[str, int | None]:
        """Mise minimale (calculée sur l'enchère relue), ou max_price lui-même si `exact`,
        si la mise minimale ne dépasse pas max_price et la mise ne dépasse pas le solde.
        Enchère ou mise sans réponse : nouvel essai jusqu'à `deadline`.
        Renvoie ("bid" | "dry_run" | "too_high" | "no_funds" | "failed", montant)."""
        auction = self.read_auction(auction_id)
        while auction is None and deadline is not None and time.monotonic() + BID_RETRY_PAUSE < deadline:
            time.sleep(BID_RETRY_PAUSE)
            auction = self.read_auction(auction_id)
        if auction is None:
            return "failed", None
        amount = min_bid(auction)
        if amount > max_price:
            return "too_high", amount
        if exact:
            amount = max_price
        balance = self.fetch_balance()
        if balance is not None and amount > balance:
            return "no_funds", amount
        if dry_run:
            return "dry_run", amount
        if self.post_bid(auction_id, amount, deadline, my_id):
            return "bid", amount
        self.snapshot(f"bid-refused-{auction_id}")
        return "failed", amount
