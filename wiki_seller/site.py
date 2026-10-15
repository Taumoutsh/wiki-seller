"""Client de wiki-masters.com : l'API JSON que le site appelle lui-même, exécutée dans
une page connectée (mêmes cookies que le navigateur). La page ne sert qu'à garder la
session et à lire la mise minimale d'une enchère ; la connexion est dans session.py."""

import dataclasses
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .config import Config, parse_duration
from .selection import normalize_name, sale_price
from .state import parse_time

log = logging.getLogger(__name__)

COLLECTION_PAGE_GUARD = 500
DEFAULT_MAX_AUCTIONS = 5
MINE_PATH = "/api/marketplace?page=1&limit=50&sort=recent&mine=1"
LISTING_COPY_TRIES = 3  # exemplaires essayés pour une mise en vente (un peut être en vente ou en échange)

SEARCH_PLACEHOLDER = re.compile(r"Rechercher", re.I)
COOKIE_ACCEPT = re.compile(r"tout accepter|accepter|j.accepte", re.I)
BID_INPUT = 'input[aria-label="Montant de la mise"]'
SLOTS_FULL_TEXT = re.compile(r"maximum|limite|trop d.ench|max.*ench|concurrent", re.I)
BALANCE_TEXT = re.compile(r"Votre solde\s*:\s*(\d[\d   ]*)", re.I)


class SessionExpired(Exception):
    pass


class SiteError(Exception):
    pass


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
    return str(card.get("rarity") or entry.get("rarity") or "").upper()


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
    def __init__(self, page: Page, config: Config, debug: bool = False):
        self.page = page
        self.config = config
        self.debug = debug
        self.retries = 0  # nouveaux essais après « trop de requêtes » ou panne du site
        # Bandeau des cookies : accepté automatiquement s'il gêne une action.
        banner = (
            page.locator("[role=dialog], .fixed, [id*=cookie i], [class*=cookie i]")
            .filter(has_text=re.compile(r"cookie", re.I))
            .filter(has=page.get_by_role("button", name=COOKIE_ACCEPT))
            .first
        )
        page.add_locator_handler(banner, lambda popup: popup.get_by_role("button", name=COOKIE_ACCEPT).first.click())

    # ─────────────────────────── Utilitaires ───────────────────────────

    def url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def snapshot(self, name: str) -> Path | None:
        """Capture d'écran + HTML, pour comprendre un problème après coup."""
        try:
            self.config.debug_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-zA-Z0-9]+', '_', name)[:60]}"
            png = self.config.debug_dir / f"{stem}.png"
            self.page.screenshot(path=str(png), full_page=True)
            (self.config.debug_dir / f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            log.info("Capture de débogage : %s", png)
            return png
        except Exception as exc:  # une capture ratée ne doit jamais faire échouer une action
            log.warning("Capture impossible (%s)", exc)
            return None

    def step(self, name: str) -> None:
        if self.debug:
            self.snapshot(name)

    # ─────────────────────────── API JSON ───────────────────────────

    def api_get(self, path: str) -> tuple[int, object]:
        result = self.page.evaluate(
            """async (path) => {
                const res = await fetch(path, {credentials: 'include', headers: {accept: 'application/json'}});
                let data = null;
                try { data = await res.json(); } catch (e) {}
                return {status: res.status, data};
            }""",
            path,
        )
        return result["status"], result["data"]

    def api_post(self, path: str, body: dict | None = None) -> tuple[int, object, str]:
        """POST depuis la page connectée. Renvoie (code, JSON ou None, texte brut)."""
        result = self.page.evaluate(
            """async ([path, body]) => {
                const init = {method: 'POST', credentials: 'include', headers: {accept: 'application/json'}};
                if (body !== null) { init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(body); }
                const res = await fetch(path, init);
                const text = await res.text();
                let data = null;
                try { data = JSON.parse(text); } catch (e) {}
                return {status: res.status, data, text: text.slice(0, 500)};
            }""",
            [path, body],
        )
        if result["status"] == 401 or (result["status"] == 403 and "Non autoris" in result["text"]):
            raise SessionExpired(f"{path} → HTTP {result['status']}")
        return result["status"], result["data"], result["text"]

    def api_get_ok(self, path: str) -> object:
        """GET avec trois essais (2 s puis 4 s) sur « trop de requêtes » ou panne du site."""
        for attempt in range(3):
            status, data = self.api_get(path)
            if status in (401, 403):
                raise SessionExpired(f"{path} → HTTP {status}")
            if 200 <= status < 300:
                return data
            if status == 404:
                return None
            if status != 429 and status < 500:
                raise SiteError(f"{path} → HTTP {status}")
            self.retries += 1
            time.sleep(2 * (attempt + 1))
        raise SiteError(f"{path} indisponible")

    def is_logged_in(self) -> bool:
        try:
            status, _ = self.api_get("/api/wikibidous")
        except Exception:  # page en cours de navigation
            return False
        return 200 <= status < 300

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

    def fetch_average(self, card_id: str, rarity: str | None) -> float | None:
        """Prix moyen de la carte (None si aucune vente connue)."""
        try:
            data = self.api_get_ok(f"/api/marketplace/cards/{card_id}/sales?scope=summary")
        except SessionExpired as exc:
            raise PriceApiUnavailable(str(exc)) from exc
        except SiteError as exc:
            raise PriceApiUnavailable(str(exc), transient=True) from exc
        return self._average_from(data, rarity)

    @staticmethod
    def _average_from(data: object, rarity: str | None) -> float | None:
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, dict) or not summary:
            return None
        entry = summary.get(rarity) if rarity else None
        if entry is None and len(summary) == 1:
            entry = next(iter(summary.values()))
        try:
            average = float(entry.get("average")) if isinstance(entry, dict) else None
        except (TypeError, ValueError):
            return None
        return average if average and average > 0 else None

    def refresh_market(self) -> None:
        """Recharge le Marché : après la fin d'une enchère, le site ne la compte pas
        toujours comme terminée tant que la page n'a pas été rechargée."""
        page = self.page
        try:
            for load in (lambda: page.goto(self.url("/marketplace"), wait_until="domcontentloaded"),
                         lambda: page.reload(wait_until="domcontentloaded")):
                load()
                page.get_by_placeholder(SEARCH_PLACEHOLDER).first.wait_for(state="visible", timeout=30000)
        except PlaywrightTimeout:
            log.warning("Le Marché a mis trop de temps à se recharger ; on continue.")

    def server_clock_offset(self) -> timedelta:
        """Écart entre l'horloge du site (en-tête Date) et la nôtre, pour viser juste
        avant la fin d'une enchère."""
        try:
            date = self.page.evaluate(
                "async () => (await fetch('/api/wikibidous', {credentials: 'include'})).headers.get('date')")
            server = parsedate_to_datetime(date) if date else None
        except Exception:
            server = None
        if not server:
            return timedelta(0)
        offset = server - datetime.now(timezone.utc)
        return offset if abs(offset) > timedelta(seconds=2) else timedelta(0)

    # ─────────────────────────── Vente et défausse ───────────────────────────

    def list_card(self, card_id: str, copy_ids: tuple[str, ...], title: str, average: float,
                  dry_run: bool, adjust=None, duration_label: str | None = None) -> ListingResult:
        """Mise en vente : POST /api/marketplace {card_id, base_amount, duration_minutes}.
        Malgré son nom, `card_id` attend l'identifiant de l'exemplaire possédé (avec celui
        de la carte, le site répond 409 « Vous ne possédez pas cette carte ») : on essaie
        les exemplaires, puis la carte en dernier recours."""
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
            status, _, text = self.api_post("/api/marketplace", body)
            if 200 <= status < 300:
                if candidate == card_id and copy_ids:
                    log.info("Mise en vente acceptée avec l'identifiant de la carte (%s).", title)
                return ListingResult(ListingStatus.LISTED, price=price, average=average)
            if SLOTS_FULL_TEXT.search(text):
                return ListingResult(ListingStatus.SLOTS_FULL, detail=text[:200])
            if status not in (400, 404, 409, 422):
                break
        return ListingResult(ListingStatus.FAILED, price, average, f"HTTP {status} : {text[:200]}")

    def discard_card(self, copy_id: str, dry_run: bool) -> tuple[DiscardStatus, str]:
        """Défausse un exemplaire : la requête que la fiche envoie après la confirmation
        « Défausser cette carte ? » (POST /api/user-cards/{id}/discard)."""
        path = f"/api/user-cards/{copy_id}/discard"
        if dry_run:
            return DiscardStatus.DRY_RUN, ""
        status, _, text = self.api_post(path)
        if 200 <= status < 300:
            return DiscardStatus.DISCARDED, f"POST {path}"
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

    def post_bid(self, auction_id: str, amount: int) -> bool:
        """Mise (POST /api/marketplace/<id>/bid {amount}) ; True si acceptée."""
        status, data, text = self.api_post(f"/api/marketplace/{auction_id}/bid", {"amount": amount})
        if 200 <= status < 300:
            confirmed = data if isinstance(data, dict) and "current_bid" in data else self.fetch_auction(auction_id)
            if (confirmed or {}).get("current_bid") and confirmed["current_bid"] >= amount:
                return True
        log.warning("Mise de %d refusée par le site (HTTP %d : %s).", amount, status, text[:200])
        return False

    def read_min_bid(self, auction_id: str) -> int | None:
        """Mise minimale exacte : le montant prérempli sur la page de l'enchère (l'API
        ne donne pas le pas de surenchère)."""
        page = self.page
        page.goto(self.url(f"/marketplace/{auction_id}"), wait_until="domcontentloaded")
        field = page.locator(BID_INPUT).first
        try:
            field.wait_for(state="visible", timeout=30000)
            page.wait_for_function(
                "(sel) => { const e = document.querySelector(sel); return e && e.value !== ''; }",
                arg=BID_INPUT, timeout=10000)
        except PlaywrightTimeout:
            self.snapshot(f"bid-{auction_id}")
            return None
        return parse_int(field.input_value())

    def place_bid(self, auction_id: str, max_price: int, dry_run: bool) -> tuple[str, int | None]:
        """Mise minimale (lue sur la page de l'enchère), si elle ne dépasse pas max_price
        ni le solde. Renvoie ("bid" | "dry_run" | "too_high" | "no_funds" | "failed", montant)."""
        amount = self.read_min_bid(auction_id)
        if amount is None:
            return "failed", None
        if amount > max_price:
            return "too_high", amount
        balance = BALANCE_TEXT.search(self.page.locator("body").inner_text())
        if balance and amount > parse_int(balance.group(1)):
            return "no_funds", amount
        self.step(f"bid-ready-{auction_id}")
        if dry_run:
            return "dry_run", amount
        if self.post_bid(auction_id, amount):
            return "bid", amount
        self.snapshot(f"bid-refused-{auction_id}")
        return "failed", amount
