"""Pilotage de wiki-masters.com avec Playwright.

Les lectures (collection, enchères en cours, prix moyens) passent par l'API JSON que
le site appelle lui-même, exécutée dans la page connectée. La mise en vente, elle,
se fait par l'interface : clic sur la carte, « Mettre aux enchères », lecture du
« Prix moyen », saisie de la mise, durée, « Lancer l'enchère »."""

import dataclasses
import logging
import re
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .config import Config
from .selection import normalize_name, sale_price
from .verification import VERIFICATION_MODAL, wait_for_verification

log = logging.getLogger(__name__)

COLLECTION_PAGE_GUARD = 500
DEFAULT_MAX_AUCTIONS = 5
# Attente de la vérification anti-bot avant de pouvoir cliquer sur « Connexion ».
LOGIN_WAIT_HEADLESS = 30
LOGIN_WAIT_HEADED = 300

# Libellés de l'interface (repris du site).
SELL_BUTTON = re.compile(r"Mettre aux ench[èe]res", re.I)
LAUNCH_BUTTON = re.compile(r"Lancer l.ench[èe]re", re.I)
CANCEL_BUTTON = re.compile(r"^\s*Annuler\s*$", re.I)
SEARCH_PLACEHOLDER = re.compile(r"Rechercher", re.I)
AVERAGE_LABEL = re.compile(r"Prix moyen|Moyenne", re.I)
SLOTS_TEXT = re.compile(r"Ench[èe]res actives\s*:?\s*(\d+)\s*/\s*(\d+)", re.I)
AVERAGE_AFTER = re.compile(r"(?:Prix moyen|Moyenne)\s*(?:de vente)?\s*(?:\([^)]*\))?\s*:?\s*(\d[\d \u00a0\u202f.,]*)", re.I)
AVERAGE_UNAVAILABLE = re.compile(r"(?:Prix moyen|Moyenne)\s*:?\s*(indisponible|aucun|n/?a|[-—–]|0\b)", re.I)
LOGIN_SUBMIT = re.compile(r"^\s*(connexion|se connecter)\s*$", re.I)
ANTIBOT_TEXT = re.compile(r"robot|humain|anti-?bot|v[ée]rification|captcha", re.I)
ANTIBOT_CONFIRM = re.compile(r"continuer|valider|confirmer|v[ée]rifier|envoyer|^\s*ok\s*$", re.I)
COOKIE_ACCEPT = re.compile(r"tout accepter|accepter|j.accepte", re.I)
PACKS_AVAILABLE = re.compile(r"(\d+)\s*/\s*(\d+)\s*paquets?\s+disponibles?", re.I)
PACK_OPEN_BUTTON = 'button:has(span:text-is("Ouvrir"))'
PACK_MORE_CARDS = re.compile(r"Encore\s+\d+\s+cartes?", re.I)
PACK_CONTINUE = re.compile(r"^\s*Continuer\s*$", re.I)
BID_INPUT = 'input[aria-label="Montant de la mise"]'
BID_BUTTON = re.compile(r"^\s*Miser\s*$", re.I)
SLOTS_FULL_TEXT = re.compile(r"maximum|limite|trop d.ench|max.*ench|concurrent", re.I)
BALANCE_TEXT = re.compile(r"Votre solde\s*:\s*(\d[\d \u00a0\u202f]*)", re.I)
PACK_REVEAL_TIMEOUT = 60  # secondes pour faire défiler un paquet jusqu'à « Continuer »
PACK_SIZE = 5  # cartes par paquet
PACK_CARD_WAIT = 5  # secondes au plus sur une carte pour que le site la compte comme vue
PACK_COUNTER_SETTLE = 8  # secondes pour que le compteur de /pulls quitte son 0 provisoire
PACK_RATE_LIMIT_BUDGET = 600  # secondes d'attente cumulée au plus pour la limite de cadence
LISTING_COPY_TRIES = 3  # exemplaires essayés pour une mise en vente (un peut être en vente ou en échange)
API_ACTION_PAUSE = 1500  # ms entre deux actions par l'API, pour garder un rythme humain
PACK_GUARD = 20  # au plus 10 paquets stockés sur le site ; marge pour ceux qui arrivent
MINE_PATH = "/api/marketplace?page=1&limit=50&sort=recent&mine=1"
MARKERS = ["captcha", "turnstile", "challenge", "verify you are human"]


class SessionExpired(Exception):
    pass


class SiteError(Exception):
    pass


class HumanVerificationRequired(Exception):
    """Le site demande une vérification humaine (anti-bot)"""


class PriceApiUnavailable(Exception):
    """L'API des prix a refusé la requête : le prix sera lu dans la fenêtre d'enchère."""


class ListingStatus(Enum):
    LISTED = "mise en vente"
    DRY_RUN = "simulation (non validée)"
    SLOTS_FULL = "plus de place aux enchères"
    NO_AVERAGE = "prix moyen indisponible"
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
        ends = []
        for auction in self.selling:
            raw = auction.get("end_at") if isinstance(auction, dict) else None
            if isinstance(raw, str):
                try:
                    ends.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
                except ValueError:
                    pass
        return min(ends) if ends else None


def parse_int(text: str) -> int | None:
    """Les prix du site sont des entiers : on ne garde que les chiffres (espaces
    et séparateurs de milliers ignorés)."""
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def entry_rarity(entry: dict) -> str:
    card = entry.get("card") if isinstance(entry.get("card"), dict) else entry
    return str(card.get("rarity") or entry.get("rarity") or "").upper()


def parse_cookies(text: str) -> list[tuple[str, str]]:
    """« nom=valeur; nom2=valeur2 » (ou une paire par ligne) → [(nom, valeur), ...]."""
    pairs = []
    for part in re.split(r"[;\n]", text):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            pairs.append((name.strip(), value.strip()))
    return pairs


NOTABLE_RARITIES = ("L", "UR")
RARITY_ORDER = ("L", "UR", "SR", "R", "PC", "C")


def pack_cards(data: object) -> list[dict]:
    """Cartes tirées, trouvées dans la réponse de /api/packs/open quelle que soit sa
    forme : tout objet qui porte un titre Wikipédia et une rareté."""
    cards: list[dict] = []
    seen: set[int] = set()

    def walk(node: object, shiny: bool) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, shiny)
        elif isinstance(node, dict):
            shiny = shiny or bool(node.get("is_shiny"))  # souvent porté par l'exemplaire, pas la carte
            title = node.get("wikipedia_title") or node.get("title")
            rarity = node.get("rarity") or node.get("snapshot_rarity")
            if isinstance(title, str) and isinstance(rarity, str) and id(node) not in seen:
                seen.add(id(node))
                cards.append({"title": title, "rarity": rarity.upper(), "shiny": shiny})
                return
            for value in node.values():
                walk(value, shiny)

    walk(data, False)
    return cards


def pack_summary(cards: list[dict]) -> str:
    if not cards:
        return "contenu non lu"
    counts = {r: sum(1 for c in cards if c["rarity"] == r) for r in RARITY_ORDER}
    parts = [f"{n} {r}" for r, n in counts.items() if n]
    others = len(cards) - sum(counts.values())
    if others:
        parts.append(f"{others} autre(s)")
    return f"{len(cards)} cartes : {', '.join(parts)}"


def rate_limit_wait(retry_after: object, offset: timedelta = timedelta(0)) -> float:
    """Secondes à attendre avant `retry_after` (heure ISO du site), + 1 s de marge ;
    entre 2 et 120 s si l'heure est absente ou incohérente."""
    when = None
    if isinstance(retry_after, str):
        try:
            when = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
        except ValueError:
            when = None
    if when is None:
        return 10.0
    seconds = (when - (datetime.now(timezone.utc) + offset)).total_seconds() + 1
    return min(max(seconds, 2.0), 120.0)


def auction_minutes(label: str) -> int:
    """« 1 h » → 60, « 10 min » → 10 (durée envoyée à l'API)."""
    from .config import parse_duration

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


def duration_pattern(label: str) -> re.Pattern:
    """« 1 h » accepte aussi « 1h », « 1 heure »."""
    parts = [re.escape(p) for p in label.split()]
    body = r"\s*".join(parts)
    if label.strip().lower().endswith("h"):
        body += r"(?:eures?)?"
    return re.compile(rf"^\s*{body}\s*$", re.I)


class WikiMasters:
    def __init__(self, page: Page, config: Config, debug: bool = False):
        self.page = page
        self.config = config
        self.debug = debug
        self._install_popup_handlers()

    # ─────────────────────────── Utilitaires ───────────────────────────

    def url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def snapshot(self, name: str) -> Path | None:
        """Capture d'écran + HTML, pour ajuster les sélecteurs en cas de problème."""
        try:
            self.config.debug_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-zA-Z0-9]+', '_', name)[:60]}"
            png = self.config.debug_dir / f"{stem}.png"
            self.page.screenshot(path=str(png), full_page=True)
            (self.config.debug_dir / f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            log.info("Capture de débogage : %s", png)
            return png
        except Exception as exc:  # une capture ratée ne doit jamais faire échouer la vente
            log.warning("Capture impossible (%s)", exc)
            return None

    def step(self, name: str) -> None:
        if self.debug:
            self.snapshot(name)

    def _install_popup_handlers(self) -> None:
        # La petite popup anti-bot avec une case à cocher peut apparaître à tout moment :
        # Playwright la traite automatiquement avant chaque action (clic, saisie...).
        antibot = (
            self.page.locator("[role=dialog], [role=alertdialog], .fixed")
            .filter(has_text=ANTIBOT_TEXT)
            .filter(has=self.page.locator("input[type=checkbox], [role=checkbox]"))
            .first
        )
        self.page.add_locator_handler(antibot, self._solve_antibot)
        self._antibot = antibot

        cookies = (
            self.page.locator("[role=dialog], .fixed, [id*=cookie i], [class*=cookie i]")
            .filter(has_text=re.compile(r"cookie", re.I))
            .filter(has=self.page.get_by_role("button", name=COOKIE_ACCEPT))
            .first
        )
        self.page.add_locator_handler(
            cookies, lambda popup: popup.get_by_role("button", name=COOKIE_ACCEPT).first.click()
        )

    def _solve_antibot(self, popup: Locator) -> None:
        log.info("Popup anti-bot détectée : coche de la case.")
        box = popup.locator("input[type=checkbox], [role=checkbox]").first
        try:
            box.check(timeout=5000)
        except Exception:
            box.click(timeout=5000, force=True)
        confirm = popup.get_by_role("button", name=ANTIBOT_CONFIRM)
        if confirm.count():
            confirm.first.click(timeout=5000)

    def check_antibot(self) -> None:
        """Déclenche explicitement la vérification anti-bot (utile après une navigation)."""
        try:
            self.page.locator("body").hover(timeout=2000, position={"x": 1, "y": 1})
        except Exception:
            pass

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
        """POST JSON depuis la page connectée (mêmes cookies que les clics).
        Renvoie (code, JSON ou None, texte brut)."""
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
        if result["status"] in (401, 403) and path.startswith("/api/") and "Non autoris" in result["text"]:
            raise SessionExpired(f"{path} → HTTP {result['status']}")
        return result["status"], result["data"], result["text"]

    def api_get_ok(self, path: str) -> object:
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
            time.sleep(2 * (attempt + 1))
        raise SiteError(f"{path} indisponible")

    # ─────────────────────────── Connexion ───────────────────────────

    def is_logged_in(self) -> bool:
        try:
            status, _ = self.api_get("/api/wikibidous")
        except Exception:  # page en cours de navigation
            return False
        return 200 <= status < 300

    def ensure_logged_in(self) -> None:
        self.page.goto(self.url("/collection"), wait_until="domcontentloaded")
        if self.is_logged_in():
            log.info("Session existante réutilisée.")
            return
        if self.config.session_cookies and self.import_session_cookies():
            return
        self.login()

    def import_session_cookies(self) -> bool:
        """Session copiée depuis un navigateur connecté (SESSION_COOKIES) : évite la
        vérification Cloudflare de la page /login. Le site la renouvelle ensuite tout
        seul et la version à jour est enregistrée dans state/."""
        cookies = parse_cookies(self.config.session_cookies)
        if not cookies:
            log.warning("SESSION_COOKIES ne contient aucun cookie « nom=valeur ».")
            return False
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.config.base_url} for name, value in cookies])
        self.page.goto(self.url("/collection"), wait_until="domcontentloaded")
        if self.is_logged_in():
            log.info("Connecté avec la session de SESSION_COOKIES (%d cookie(s)).", len(cookies))
            return True
        log.warning("La session de SESSION_COOKIES est expirée ou invalide : "
                    "copiez-en une nouvelle depuis un navigateur connecté.")
        self.page.context.clear_cookies()
        return False

    def _wait_logged_in(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1000)
            self.check_antibot()
            if self.is_logged_in():
                log.info("Connecté.")
                return True
        return False

    def login(self) -> None:
        """Connexion par la page /login (et non /signup, qui affiche l'inscription).

        Le bouton « Connexion » reste désactivé tant que la vérification Cloudflare
        Turnstile (« Vérifiez que vous êtes humain ») n'est pas validée. Le script ne
        la contourne pas : il attend qu'elle passe seule, ou qu'une personne coche la
        case dans le navigateur ouvert avec --headed. La session est ensuite réutilisée."""
        page = self.page
        log.info("Connexion à %s avec %s", self.config.base_url, self.config.mail)
        page.goto(self.url("/login"), wait_until="domcontentloaded")
        password = page.locator("input[type=password]:visible").first
        password.wait_for(state="visible", timeout=20000)
        email = page.locator("input[type=email]:visible, input[autocomplete=email]:visible").first
        email.fill(self.config.mail)
        password.fill(self.config.password)
        self.step("login-filled")

        submit = page.get_by_role("button", name=LOGIN_SUBMIT).first
        wait_s = LOGIN_WAIT_HEADLESS if self.config.headless else LOGIN_WAIT_HEADED
        if not submit.is_enabled():
            log.info("Bouton « Connexion » désactivé : attente de la vérification anti-bot "
                     "Cloudflare (jusqu'à %d s)%s.", wait_s,
                     "" if self.config.headless else " ; cochez la case dans le navigateur")
        deadline = time.monotonic() + wait_s
        while not submit.is_enabled():
            if time.monotonic() > deadline:
                self.snapshot("login-blocked")
                raise SiteError(
                    "Connexion bloquée par la vérification anti-bot Cloudflare. Lancez une fois "
                    "`python -m wiki_seller --dry-run --headed` sur une machine avec écran et cochez "
                    "la case : la session enregistrée dans state/ sera ensuite réutilisée.")
            page.wait_for_timeout(500)

        submit.click()
        if self._wait_logged_in(30):
            return
        self.snapshot("login-failed")
        raise SiteError("Connexion échouée (identifiants refusés ou formulaire différent ?)")

    # ─────────────────────────── Lectures ───────────────────────────

    def fetch_collection(self, rarities: tuple[str, ...] = ()) -> tuple[list[dict], set[str]]:
        """Entrées de la collection (limitées aux raretés demandées, filtrées par le site
        lui-même) + les exemplaires engagés dans un échange."""
        entries: list[dict] = []
        seen: set[str] = set()
        in_trade: set[str] = set()
        for rarity in rarities or (None,):
            query = f"&rarity={rarity}" if rarity else ""
            for page_index in range(COLLECTION_PAGE_GUARD):
                data = self.api_get_ok(f"/api/my-collection?sort=rarity&page={page_index}&stats=0{query}")
                items = data.get("collection") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise SiteError("Réponse inattendue de /api/my-collection")
                in_trade.update(str(i) for i in (data.get("pendingTradeCardIds") or []))
                new = 0
                for item in items:
                    key = str(item.get("id") or id(item))
                    if key in seen:
                        continue
                    seen.add(key)
                    new += 1
                    if not rarities or entry_rarity(item) in rarities:
                        entries.append(item)
                if not new:
                    break
                time.sleep(0.2)
        log.info("Collection chargée : %d entrée(s)%s.", len(entries),
                 f" de rareté {', '.join(rarities)}" if rarities else "")
        return entries, in_trade

    def fetch_mine(self) -> dict:
        """Onglets « Mes ventes » (selling), « Mes enchères » (bidding) et « Gagnées » (won)."""
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
        except (SessionExpired, SiteError) as exc:
            raise PriceApiUnavailable(str(exc)) from exc
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

    # ─────────────────────────── Mise en vente (UI) ───────────────────────────

    def open_card(self, title: str) -> None:
        page = self.page
        page.goto(self.url("/collection"), wait_until="domcontentloaded")
        search = page.get_by_placeholder(SEARCH_PLACEHOLDER).first
        search.wait_for(state="visible", timeout=20000)
        search.fill(title)

        exact = page.locator(".cursor-pointer").filter(has=page.get_by_text(title, exact=True))
        loose = page.locator(".cursor-pointer").filter(
            has=page.get_by_text(re.compile(rf"^\s*{re.escape(title)}\s*$", re.I))
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for tiles in (exact, loose):
                if tiles.count():
                    self.step(f"search-{title}")
                    tiles.last.click()
                    return
            page.wait_for_timeout(300)
        raise SiteError(f"Carte « {title} » introuvable dans la collection")

    def read_slots(self) -> tuple[int, int] | None:
        match = SLOTS_TEXT.search(self.page.locator("body").inner_text())
        return (int(match.group(1)), int(match.group(2))) if match else None

    def read_ui_average(self, launch: Locator, timeout_s: float = 15) -> tuple[str, int | None]:
        """Attend la valeur de « Prix moyen » dans la fenêtre de mise aux enchères.

        Renvoie ("ok", valeur), ("unavailable", None) si le site n'a pas de prix, ou
        ("missing", None) si le libellé n'existe pas du tout."""
        container = self.page.locator("div").filter(has=launch).filter(has_text=AVERAGE_LABEL).last
        deadline = time.monotonic() + timeout_s
        seen_label = False
        while time.monotonic() < deadline:
            if container.count():
                seen_label = True
                text = container.inner_text()
                match = AVERAGE_AFTER.search(text)
                value = parse_int(match.group(1)) if match else None
                if value:
                    return "ok", value
                if AVERAGE_UNAVAILABLE.search(text):
                    return "unavailable", None
            self.page.wait_for_timeout(300)
        return ("unavailable" if seen_label else "missing"), None

    def _price_input(self, launch: Locator) -> Locator:
        page = self.page
        by_label = page.locator('input[aria-label="Mise de départ"], input[aria-label*="départ" i]')
        if by_label.count():
            return by_label.first
        modal = page.locator("div").filter(has=launch).filter(has=page.locator("input")).last
        return modal.locator("input[type=number], input[inputmode=numeric], input[type=text]").first

    def _select_duration(self, launch: Locator, label: str) -> None:
        page = self.page
        pattern = duration_pattern(label)
        # Plus petit bloc contenant à la fois « Lancer l'enchère » et l'option de durée.
        modal = page.locator("div").filter(has=launch).filter(has=page.get_by_text(pattern)).last
        if not modal.count():
            modal = page.locator("div").filter(has=launch).filter(has=page.locator("select")).last
        for candidate in (
            modal.get_by_role("button", name=pattern),
            modal.get_by_role("radio", name=pattern),
            modal.get_by_role("tab", name=pattern),
        ):
            if candidate.count():
                candidate.first.click()
                return
        selects = modal.locator("select")
        for i in range(selects.count()):
            select = selects.nth(i)
            for option in select.locator("option").all_inner_texts():
                if pattern.match(option):
                    select.select_option(label=option)
                    return
        text = modal.get_by_text(pattern)
        if text.count():
            text.first.click()
            return
        raise SiteError(f"Durée « {label} » introuvable")

    def _cancel(self) -> None:
        cancel = self.page.get_by_role("button", name=CANCEL_BUTTON)
        try:
            if cancel.count():
                cancel.first.click(timeout=5000)
            else:
                self.page.keyboard.press("Escape")
        except Exception:
            self.page.keyboard.press("Escape")

    def list_card_api(self, card_id: str, copy_ids: tuple[str, ...], title: str, average: float,
                      dry_run: bool, adjust=None, duration_label: str | None = None) -> ListingResult:
        """Mise en vente par l'API : POST /api/marketplace {card_id, base_amount,
        duration_minutes}. Malgré son nom, `card_id` attend l'identifiant de l'exemplaire
        possédé (avec celui de la carte, le site répond 409 « Vous ne possédez pas cette
        carte ») : on essaie les exemplaires, puis la carte en dernier recours."""
        price = sale_price(average, self.config.price_ratio)
        if adjust and price >= 1:
            price = adjust(price, average)
        if price < 1:
            return ListingResult(ListingStatus.NO_AVERAGE, average=average, detail="prix calculé nul")
        duration = auction_minutes(duration_label or self.config.auction_duration_label)
        if dry_run:
            return ListingResult(ListingStatus.DRY_RUN, price=price, average=average)
        text = ""
        for candidate in (*copy_ids[:LISTING_COPY_TRIES], card_id):
            body = {"card_id": candidate, "base_amount": price, "duration_minutes": duration}
            status, data, text = self.api_post("/api/marketplace", body)
            if 200 <= status < 300:
                if candidate == card_id and copy_ids:
                    log.info("Mise en vente acceptée avec l'identifiant de la carte (%s).", title)
                return ListingResult(ListingStatus.LISTED, price=price, average=average)
            if SLOTS_FULL_TEXT.search(text):
                return ListingResult(ListingStatus.SLOTS_FULL, detail=text[:200])
            if status not in (400, 404, 409, 422):
                break
        return ListingResult(ListingStatus.FAILED, price, average, f"HTTP {status} : {text[:200]}")

    def list_card(self, title: str, fallback_average: float | None, dry_run: bool,
                  adjust=None, duration_label: str | None = None) -> ListingResult:
        page = self.page
        try:
            self.open_card(title)
            page.get_by_role("button", name=SELL_BUTTON).first.click(timeout=15000)
            launch = page.get_by_role("button", name=LAUNCH_BUTTON).first
            launch.wait_for(state="visible", timeout=15000)

            slots = self.read_slots()
            if slots and slots[0] >= slots[1]:
                self._cancel()
                return ListingResult(ListingStatus.SLOTS_FULL, detail=f"{slots[0]}/{slots[1]}")

            state, ui_average = self.read_ui_average(launch)
            if state == "ok":
                average: float | None = float(ui_average)
            elif state == "missing" and fallback_average:
                log.warning("« Prix moyen » absent de la fenêtre : utilisation du prix moyen de l'API.")
                average = fallback_average
            else:
                self._cancel()
                return ListingResult(ListingStatus.NO_AVERAGE)

            price = sale_price(average, self.config.price_ratio)
            if adjust and price >= 1:
                price = adjust(price, average)
            if price < 1:
                self._cancel()
                return ListingResult(ListingStatus.NO_AVERAGE, average=average, detail="prix calculé nul")

            price_input = self._price_input(launch)
            price_input.fill(str(price))
            typed = parse_int(price_input.input_value())
            if typed != price:
                raise SiteError(f"le champ de mise affiche {price_input.input_value()!r} au lieu de {price}")

            self._select_duration(launch, duration_label or self.config.auction_duration_label)
            self.step(f"ready-{title}")

            if dry_run:
                self._cancel()
                return ListingResult(ListingStatus.DRY_RUN, price=price, average=average)

            launch.click()
            try:
                launch.wait_for(state="hidden", timeout=15000)
            except PlaywrightTimeout:
                self.snapshot(f"refused-{title}")
                return ListingResult(ListingStatus.FAILED, price, average, "le site n'a pas validé l'enchère")
            return ListingResult(ListingStatus.LISTED, price=price, average=average)
        except SessionExpired:
            raise
        except Exception as exc:
            self.snapshot(f"error-{title}")
            try:
                self._cancel()
            except Exception:
                pass
            return ListingResult(ListingStatus.FAILED, detail=str(exc).splitlines()[0])

    # ─────────────────────────── Marché : rafraîchissement ───────────────────────────

    def refresh_market(self) -> None:
        """Recharge le Marché : après la fin d'une enchère, la page (et le compteur
        « Enchères actives ») n'est pas toujours à jour tant qu'on ne l'a pas rechargée."""
        page = self.page
        try:
            page.goto(self.url("/marketplace"), wait_until="domcontentloaded")
            page.get_by_placeholder(SEARCH_PLACEHOLDER).first.wait_for(state="visible", timeout=30000)
            page.reload(wait_until="domcontentloaded")
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

    # ─────────────────────────── Paquets ───────────────────────────

    def packs_available(self) -> int | None:
        match = PACKS_AVAILABLE.search(self.page.locator("body").inner_text())
        return int(match.group(1)) if match else None

    def _next_card_button(self) -> Locator | None:
        return self._carousel_arrow(right=True)

    def _carousel_row(self) -> list[dict]:
        """Boutons sans texte de la ligne du carrousel, de gauche à droite : flèche
        gauche, un point par carte, flèche droite."""
        boxes = self.page.evaluate("""() => [...document.querySelectorAll('button')]
            .map((b, i) => ({i, text: (b.innerText || '').trim(), aria: b.getAttribute('aria-label') || '',
                             disabled: b.disabled, r: b.getBoundingClientRect()}))
            .filter(b => b.r.width > 0 && b.r.height > 0)
            .map(b => ({i: b.i, text: b.text, aria: b.aria, disabled: b.disabled,
                        x: b.r.x, y: b.r.y + b.r.height / 2, w: b.r.width}))""")
        blank = [b for b in boxes if not b["text"] and not b["aria"]]
        if not blank:
            return []
        # Les points et les deux flèches sont sur une même ligne : la plus peuplée.
        rows: dict[int, list[dict]] = {}
        for b in blank:
            rows.setdefault(round(b["y"] / 10), []).append(b)
        return sorted(max(rows.values(), key=len), key=lambda b: b["x"])

    def _carousel_arrow(self, right: bool) -> Locator | None:
        """Flèche du carrousel (None si absente ou désactivée)."""
        row = self._carousel_row()
        if not row:
            return None
        best = row[-1] if right else row[0]
        return None if best["disabled"] else self.page.locator("button").nth(best["i"])

    def _carousel_dots(self) -> list[Locator]:
        row = self._carousel_row()
        return [self.page.locator("button").nth(b["i"]) for b in row[1:-1]]

    def packs_counter(self) -> int | None:
        """Compteur « N / 10 paquets disponibles » de /pulls. La page affiche d'abord
        0 le temps de charger le vrai nombre : on attend qu'il se stabilise."""
        page = self.page
        page.goto(self.url("/pulls"), wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => /paquets?\\s+disponibles?/i.test(document.body.innerText)", timeout=30000)
        except PlaywrightTimeout:
            return None
        return self._settled_packs_available()

    def _settled_packs_available(self) -> int | None:
        deadline = time.monotonic() + PACK_COUNTER_SETTLE
        available = self.packs_available()
        while not available and time.monotonic() < deadline:
            self.page.wait_for_timeout(300)
            available = self.packs_available()
        return available

    def open_packs_api(self, dry_run: bool, limit: int | None = None) -> int:
        """Ouvre les paquets par l'API : POST /api/packs/open (sans corps) renvoie
        directement les 5 cartes ; le défilé à l'écran n'est qu'un affichage.

        Le compteur de la page ne sert qu'au journal : on ouvre jusqu'à ce que le site
        refuse (plus de paquet), en respectant sa limite de cadence."""
        available = self.packs_counter()
        log.info("Paquets disponibles (page) : %s.", "compteur illisible" if available is None else available)
        if dry_run:
            log.info("Simulation : %s paquet(s) seraient ouverts.", available if available is not None else "?")
            return 0
        page = self.page
        opened = 0
        target = min(limit or PACK_GUARD, PACK_GUARD)
        waited = 0.0
        while opened < target:
            unlock_step = False
            status, data, text = self.api_post("/api/packs/open")
            remaining = data.get("packs_remaining") if isinstance(data, dict) else None
            if status == 429 and isinstance(data, dict) and not data.get("rate_limit_daily") \
                    and remaining != 0:
                # « Ouverture trop rapide » : le site dit quand réessayer (retry_after).
                wait = rate_limit_wait(data.get("retry_after"), self.server_clock_offset())
                if waited + wait > PACK_RATE_LIMIT_BUDGET:
                    log.info("Paquets : cadence limitée par le site, suite à la prochaine passe.")
                    break
                log.info("Paquets : ouverture trop rapide, nouvel essai dans %.0f s.", wait)
                waited += wait
                page.wait_for_timeout(wait * 1000)
                continue
            if isinstance(data, dict) and (data.get("human_verification_required")
                                           or data.get("code") == "human_verification_required"):
                cookies = parse_cookies(self.config.session_cookies)
                r = requests.post("http://localhost:8191/v1", json={
                    "cmd": "request.get",
                    "url": self.url("/pulls"),
                    "maxTimeout": 60000,
                    "cookies": cookies,
                })
                sol = r.json().get("solutions", {})
                html = (sol.get("response") or "").lower()
                blocked = any(m in html for m in MARKERS)
                log.info("FlareSolverr: HTTP %s -> %s", sol.get("status"), "BLOCKED" if blocked else "BYPASSED")
                unlock_step = True
            if not 200 <= status < 300:
                if opened or available:
                    log.info("Paquets : plus d'ouverture possible (HTTP %d : %s).", status, text[:200])
                break
            if not unlock_step:
                opened += 1
            cards = pack_cards(data)
            left = f"{remaining} restant(s)" if isinstance(remaining, int) else f"n°{opened}"
            log.info("Paquet ouvert (%s) : %s.", left, pack_summary(cards))
            for card in cards:
                if card["rarity"] in NOTABLE_RARITIES:
                    log.info("  ★ %s — %s%s", card["rarity"], card["title"], " (brillante)" if card["shiny"] else "")
            if remaining == 0:
                break
            page.wait_for_timeout(API_ACTION_PAUSE)
        return opened

    def await_pack_verification(self, timeout_s: float) -> bool:
        """Affiche la vérification humaine de /pulls (clic sur « Ouvrir ») et attend qu'une
        personne la fasse dans ce navigateur. Rien n'est coché par le script : la popup
        automatique est désactivée pendant l'attente."""
        page = self.page
        page.goto(self.url("/pulls"), wait_until="domcontentloaded")
        page.locator(PACK_OPEN_BUTTON).first.click(timeout=30000)
        modal = page.locator(VERIFICATION_MODAL).first
        try:
            modal.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeout:
            return True  # pas de fenêtre : le site a ouvert le paquet directement
        self.step("packs-verification")
        handler = getattr(self, "_antibot", None)
        if handler is not None:
            page.remove_locator_handler(handler)
        try:
            return wait_for_verification(page, timeout_s)
        finally:
            if handler is not None:
                page.add_locator_handler(handler, self._solve_antibot)

    def open_packs(self, dry_run: bool, limit: int | None = None) -> int:
        """Ouvre les paquets disponibles (tous, ou au plus `limit`) : « Ouvrir », faire
        défiler les cartes, « Continuer ». Renvoie le nombre de paquets ouverts."""
        page = self.page
        opened = 0
        for _ in range(min(PACK_GUARD, limit or PACK_GUARD)):
            page.goto(self.url("/pulls"), wait_until="domcontentloaded")
            button = page.locator(PACK_OPEN_BUTTON).first
            try:
                button.wait_for(state="visible", timeout=30000)
                page.wait_for_function(
                    "() => /paquets?\\s+disponibles?/i.test(document.body.innerText)", timeout=15000)
            except PlaywrightTimeout:
                self.snapshot("packs-page")
                raise SiteError("Page des paquets inattendue (bouton « Ouvrir » introuvable)")
            available = self._settled_packs_available()
            if opened == 0:
                log.info("Paquets disponibles : %s%s.", "compteur introuvable" if available is None else available,
                         "" if button.is_enabled() else " (bouton « Ouvrir » désactivé)")
            if available is None:
                self.snapshot("packs-counter")
            if not available or not button.is_enabled():
                break
            if dry_run:
                log.info("Simulation : %d paquet(s) seraient ouverts.", available)
                return 0
            try:
                with page.expect_response(lambda r: "/api/packs/open" in r.url, timeout=30000) as response:
                    button.click()
                cards = pack_cards(response.value.json())
            except Exception:  # le contenu n'est qu'informatif : on ouvre quand même
                cards = []
            self._reveal_pack()
            opened += 1
            log.info("Paquet ouvert (%d restant(s) avant ouverture) : %s.", available - 1, pack_summary(cards))
            for card in cards:
                if card["rarity"] in NOTABLE_RARITIES:
                    log.info("  ★ %s — %s%s", card["rarity"], card["title"], " (brillante)" if card["shiny"] else "")
        return opened

    def _cards_left(self) -> int | None:
        """Nombre du bouton « Encore N cartes » : les cartes que le site ne compte pas
        encore comme vues. 0 quand « Continuer » est affiché, None si illisible."""
        page = self.page
        done = page.get_by_role("button", name=PACK_CONTINUE)
        if done.count() and done.first.is_visible() and done.first.is_enabled():
            return 0
        more = page.get_by_role("button", name=PACK_MORE_CARDS)
        if not more.count():
            return None
        match = re.search(r"\d+", more.first.inner_text())
        return int(match.group()) if match else None

    def _wait_card_counted(self, before: int | None) -> int | None:
        """Attend que le compteur « Encore N cartes » baisse (au plus PACK_CARD_WAIT)."""
        deadline = time.monotonic() + PACK_CARD_WAIT
        left = self._cards_left()
        while left is not None and before is not None and left >= before and left > 0 \
                and time.monotonic() < deadline:
            self.page.wait_for_timeout(150)
            left = self._cards_left()
        return left

    def _reveal_pack(self) -> None:
        """Fait défiler le paquet : le site ne compte une carte comme vue qu'au bout d'un
        moment (« Encore N cartes » baisse alors) ; on attend ce décompte avant la carte
        suivante, sinon « Continuer » n'apparaît jamais."""
        page = self.page
        page.get_by_role("button", name=PACK_MORE_CARDS).or_(
            page.get_by_role("button", name=PACK_CONTINUE)).first.wait_for(state="visible", timeout=30000)
        self.step("pack-revealed")
        deadline = time.monotonic() + PACK_REVEAL_TIMEOUT
        # La première carte aussi doit être comptée avant de passer à la suivante.
        left = self._wait_card_counted(PACK_SIZE)
        revisited = False
        while left != 0:
            if time.monotonic() > deadline:
                self.snapshot("pack-stuck")
                raise SiteError(f"Impossible d'atteindre « Continuer » (encore {left} carte(s))")
            arrow = self._next_card_button()
            if arrow is not None:
                arrow.click()
                left = self._wait_card_counted(left)
            elif not revisited:
                # Dernière carte, mais des cartes pas comptées : on repasse sur chacune.
                log.info("Paquet : encore %s carte(s) non comptée(s) sur la dernière carte, "
                         "nouveau passage sur chaque carte.", left)
                for dot in self._carousel_dots():
                    dot.click()
                    left = self._wait_card_counted(left)
                    if left == 0:
                        break
                revisited = True
            else:
                page.wait_for_timeout(500)
                left = self._cards_left()
        self.step("pack-last-card")
        page.get_by_role("button", name=PACK_CONTINUE).first.click()
        page.wait_for_timeout(1000)

    # ─────────────────────────── Achats ───────────────────────────

    def search_auctions(self, name: str) -> list[dict]:
        """Enchères trouvées par la recherche du Marché. Elle ne trouve rien dès que la
        requête contient des parenthèses (« Dewey Martin (acteur) ») : on réessaie alors
        sans elles, puis sans ponctuation. Le titre exact est vérifié par l'appelant."""
        from urllib.parse import quote_plus

        found: dict[str, dict] = {}
        for query in search_queries(name):
            data = self.api_get_ok(f"/api/marketplace?page=1&limit=50&sort=recent&q={quote_plus(query)}")
            auctions = data.get("auctions") if isinstance(data, dict) else None
            for auction in auctions or []:
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
        """Mise par l'API (POST /api/marketplace/<id>/bid {amount}) ; True si acceptée."""
        status, data, text = self.api_post(f"/api/marketplace/{auction_id}/bid", {"amount": amount})
        if 200 <= status < 300:
            confirmed = data if isinstance(data, dict) and "current_bid" in data else self.fetch_auction(auction_id)
            if (confirmed or {}).get("current_bid") and confirmed["current_bid"] >= amount:
                return True
        log.warning("Mise de %d refusée par le site (HTTP %d : %s).", amount, status, text[:200])
        return False

    def read_min_bid(self, auction_id: str) -> int | None:
        """Mise minimale exacte : le montant prérempli sur la page de l'enchère."""
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
        """Ouvre l'enchère et mise le montant prérempli par le site (la mise minimale),
        s'il ne dépasse pas max_price.

        Renvoie ("bid" | "dry_run" | "too_high" | "no_funds" | "failed", montant)."""
        page = self.page
        amount = self.read_min_bid(auction_id)
        if amount is None:
            return "failed", None
        if amount > max_price:
            return "too_high", amount
        balance = BALANCE_TEXT.search(page.locator("body").inner_text())
        if balance and amount > parse_int(balance.group(1)):
            return "no_funds", amount
        self.step(f"bid-ready-{auction_id}")
        if dry_run:
            return "dry_run", amount
        if self.config.actions_via_api:
            if self.post_bid(auction_id, amount):
                return "bid", amount
            self.snapshot(f"bid-refused-{auction_id}")
            return "failed", amount
        page.get_by_role("button", name=BID_BUTTON).first.click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            auction = self.fetch_auction(auction_id)
            if auction and (auction.get("current_bid") or 0) >= amount:
                return "bid", amount
        self.snapshot(f"bid-refused-{auction_id}")
        return "failed", amount
