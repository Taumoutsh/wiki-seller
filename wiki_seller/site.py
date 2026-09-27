"""Pilotage de wiki-masters.com avec Playwright.

Les lectures (collection, enchères en cours, prix moyens) passent par l'API JSON que
le site appelle lui-même, exécutée dans la page connectée. La mise en vente, elle,
se fait par l'interface : clic sur la carte, « Mettre aux enchères », lecture du
« Prix moyen », saisie de la mise, durée, « Lancer l'enchère »."""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .config import Config
from .selection import sale_price

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
AVERAGE_LABEL = re.compile(r"Prix moyen", re.I)
SLOTS_TEXT = re.compile(r"Ench[èe]res actives\s*:?\s*(\d+)\s*/\s*(\d+)", re.I)
AVERAGE_AFTER = re.compile(r"Prix moyen\s*(?:de vente)?\s*(?:\([^)]*\))?\s*:?\s*(\d[\d \u00a0\u202f.,]*)", re.I)
AVERAGE_UNAVAILABLE = re.compile(r"Prix moyen\s*:?\s*(indisponible|aucun|n/?a|[-—–]|0\b)", re.I)
LOGIN_SUBMIT = re.compile(r"^\s*(connexion|se connecter)\s*$", re.I)
ANTIBOT_TEXT = re.compile(r"robot|humain|anti-?bot|v[ée]rification|captcha", re.I)
ANTIBOT_CONFIRM = re.compile(r"continuer|valider|confirmer|v[ée]rifier|envoyer|^\s*ok\s*$", re.I)
COOKIE_ACCEPT = re.compile(r"tout accepter|accepter|j.accepte", re.I)


class SessionExpired(Exception):
    pass


class SiteError(Exception):
    pass


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
        self.login()

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

    def fetch_collection(self) -> tuple[list[dict], set[str]]:
        """Toutes les entrées de la collection + les exemplaires engagés dans un échange."""
        entries: list[dict] = []
        seen: set[str] = set()
        in_trade: set[str] = set()
        for page_index in range(COLLECTION_PAGE_GUARD):
            data = self.api_get_ok(f"/api/my-collection?sort=rarity&page={page_index}&stats=0")
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
                entries.append(item)
                new += 1
            if not new:
                break
            time.sleep(0.2)
        log.info("Collection chargée : %d entrée(s).", len(entries))
        return entries, in_trade

    def fetch_market_state(self) -> MarketState:
        data = self.api_get_ok("/api/marketplace?page=1&limit=50&sort=recent&mine=1")
        if not isinstance(data, dict) or not isinstance(data.get("selling"), list):
            raise SiteError("Réponse inattendue pour les enchères en cours")
        selling = [a for a in data["selling"] if isinstance(a, dict) and a.get("status", "active") == "active"]
        max_auctions = data.get("maxConcurrentAuctions")
        if not isinstance(max_auctions, int) or max_auctions < 1:
            max_auctions = DEFAULT_MAX_AUCTIONS
        return MarketState(selling=selling, max_auctions=max_auctions)

    def fetch_average(self, card_id: str, rarity: str | None) -> float | None:
        """Prix moyen de la carte (None si aucune vente connue)."""
        try:
            data = self.api_get_ok(f"/api/marketplace/cards/{card_id}/sales?scope=summary")
        except (SessionExpired, SiteError) as exc:
            raise PriceApiUnavailable(str(exc)) from exc
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

    def _select_duration(self, launch: Locator) -> None:
        page = self.page
        pattern = duration_pattern(self.config.auction_duration_label)
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
        raise SiteError(f"Durée « {self.config.auction_duration_label} » introuvable")

    def _cancel(self) -> None:
        cancel = self.page.get_by_role("button", name=CANCEL_BUTTON)
        try:
            if cancel.count():
                cancel.first.click(timeout=5000)
            else:
                self.page.keyboard.press("Escape")
        except Exception:
            self.page.keyboard.press("Escape")

    def list_card(self, title: str, fallback_average: float | None, dry_run: bool) -> ListingResult:
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
            if price < 1:
                self._cancel()
                return ListingResult(ListingStatus.NO_AVERAGE, average=average, detail="prix calculé nul")

            price_input = self._price_input(launch)
            price_input.fill(str(price))
            typed = parse_int(price_input.input_value())
            if typed != price:
                raise SiteError(f"le champ de mise affiche {price_input.input_value()!r} au lieu de {price}")

            self._select_duration(launch)
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
