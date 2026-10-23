"""Prix du Marché : le détail des ventes d'une carte, que seuls les comptes ayant accès au
Marché voient. La session d'un de ces comptes, à part de la sienne (/market, dans
state/market/session.json), lit les prix pour tous les comptes ; sans elle, ou quand
elle ne marche plus, le prix reste la moyenne donnée par le site.

Prix nettoyé, par rareté, sur les ventes des 30 derniers jours (50 au plus) :
- une vente est aberrante si elle s'écarte de la médiane de plus de 3 écarts robustes
  (écart absolu médian, au moins 10 % de la médiane) : un écart type ordinaire serait
  gonflé par la vente aberrante elle-même ;
- si les dernières ventes (au moins 2) sont aberrantes, toutes dans le même sens, la
  carte change de valeur : le prix est la médiane des dernières ventes nettement de ce
  côté (plus d'un écart robuste), qu'un échange d'argent parmi elles ne fausse pas ; une
  seule dernière vente aberrante peut être un échange d'argent : elle est écartée,
  jusqu'à ce qu'une autre la confirme ;
- sinon, les ventes aberrantes isolées (échanges d'argent) sont écartées et le prix est
  la moyenne des autres.
Moins de 3 ventes : pas de prix nettoyé, la moyenne du site sert."""

import dataclasses
import logging
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .auth import SessionStore, decode_session
from .config import Config
from .site import SessionExpired, SiteClient, SiteError, WikiMasters
from .state import now_utc, parse_time, read_json, write_json

log = logging.getLogger(__name__)

WINDOW = timedelta(days=30)
MAX_SALES = 50
MIN_SALES = 3
OUTLIER_SPREAD = 3  # écarts robustes au-delà desquels une vente est aberrante
MIN_SPREAD_RATIO = 0.1  # écart robuste minimal, en part de la médiane (prix très réguliers)
TREND_MIN_SALES = 2  # dernières ventes aberrantes (même sens) pour conclure à un changement de valeur
MAD_TO_STDEV = 1.4826  # écart absolu médian → écart type, pour des prix sans valeurs aberrantes


@dataclass(frozen=True)
class Sale:
    at: datetime
    price: float


@dataclass(frozen=True)
class MarketPrice:
    price: float
    sales: int  # ventes retenues
    removed: int  # ventes écartées (aberrantes, ou d'avant un changement de valeur)
    trend: bool  # dernières ventes aberrantes dans le même sens : la carte change de valeur
    low: float | None = field(default=None, compare=False)  # vente retenue la plus basse


def clean_price(sales: list[Sale], now: datetime) -> MarketPrice | None:
    """Prix des ventes récentes sans les aberrantes ; None sans vente récente. Avec moins de
    MIN_SALES ventes, rien ne permet de dire laquelle est aberrante : leur médiane, avec leur
    nombre (les décisions s'en méfient en dessous de ventes_min) et la plus basse."""
    recent = sorted((s for s in sales if s.at >= now - WINDOW and s.price > 0), key=lambda s: s.at)[-MAX_SALES:]
    if not recent:
        return None
    prices = [s.price for s in recent]
    if len(recent) < MIN_SALES:
        return MarketPrice(statistics.median(prices), len(prices), 0, False, min(prices))
    median = statistics.median(prices)
    spread = max(MAD_TO_STDEV * statistics.median(abs(p - median) for p in prices), MIN_SPREAD_RATIO * median)
    sides = [0 if abs(p - median) <= OUTLIER_SPREAD * spread else 1 if p > median else -1 for p in prices]
    tail = 0
    while tail < len(sides) and sides[-1 - tail] != 0 and sides[-1 - tail] == sides[-1]:
        tail += 1
    if tail >= TREND_MIN_SALES:
        # Nouvelle valeur : les dernières ventes nettement du même côté (au-delà d'un écart),
        # et leur médiane, qu'un échange d'argent parmi elles ne fausse pas.
        side, start = sides[-1], len(prices)
        while start > 0 and (prices[start - 1] - median) * side > spread:
            start -= 1
        kept = prices[start:]
        return MarketPrice(statistics.median(kept), len(kept), len(prices) - len(kept), True, min(kept))
    kept = [p for p, side in zip(prices, sides) if side == 0]
    return MarketPrice(statistics.fmean(kept), len(kept), len(prices) - len(kept), False, min(kept))


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_sales(data: object, rarity: str | None) -> list[Sale]:
    """Ventes d'une carte dans la réponse du site, pour cette rareté."""
    items = data.get("sales") if isinstance(data, dict) else data
    sales = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        card = item.get("card") if isinstance(item.get("card"), dict) else {}
        own = str(item.get("rarity") or card.get("rarity") or "").upper()
        if rarity and own and own != rarity.upper():
            continue
        price = next((p for k in ("final_price", "price", "amount") if (p := _number(item.get(k)))), None)
        at = next((t for k in ("settled_at", "sold_at", "created_at", "end_at") if (t := parse_time(item.get(k)))),
                  None)
        if price is None or at is None:
            continue
        sales.append(Sale(at if at.tzinfo else at.replace(tzinfo=timezone.utc), price))
    return sales


# ─────────────────────────── Session Marché ───────────────────────────


class MarketUnavailable(Exception):
    pass


_LOCK = threading.RLock()  # une requête à la fois : la session est commune à tous les comptes
_STORES: dict = {}


def market_dir(config: Config):
    return config.shared_state_dir / "market"


def _info_file(config: Config):
    return market_dir(config) / "market.json"


def _store(config: Config) -> SessionStore:
    path = market_dir(config) / "session.json"
    with _LOCK:
        if path not in _STORES:
            _STORES[path] = SessionStore(path, config.base_url, config.supabase_url, config.supabase_anon_key,
                                         config.shared_state_dir / "site.json")
        return _STORES[path]


def market_site(config: Config) -> WikiMasters:
    """Site avec la session Marché (lecture des prix seulement)."""
    return WikiMasters(SiteClient(config.base_url, _store(config)), config)


def configured(config: Config) -> bool:
    return _store(config).has_session()


def owner(config: Config) -> str | None:
    """Conversation du compte qui a fourni la session Marché."""
    return read_json(_info_file(config), {}).get("owner")


def market_price(config: Config, card_id: str, rarity: str | None) -> MarketPrice | None:
    """Prix nettoyé (None : trop peu de ventes) ; MarketUnavailable sans session Marché
    qui marche (expirée : son propriétaire est prévenu une fois)."""
    with _LOCK:
        store = _store(config)
        if not store.has_session():
            raise MarketUnavailable("aucune session Marché")
        site = WikiMasters(SiteClient(config.base_url, store), config)
        try:
            data = site.fetch_market_sales(card_id)
        except SessionExpired as exc:
            if _logged_in(site) is not False:  # session bonne : refus du Marché lui-même
                raise MarketUnavailable(f"Marché refusé ({exc})") from exc
            store.clear()
            _expired(config)
            raise MarketUnavailable("session Marché expirée") from exc
        except SiteError as exc:
            raise MarketUnavailable(str(exc)) from exc
    return clean_price(parse_sales(data, rarity), now_utc())


def _logged_in(site: WikiMasters) -> bool | None:
    try:
        return site.is_logged_in()
    except SiteError:
        return None


def _expired(config: Config) -> None:
    from .telegram import notify

    chat = owner(config)
    log.warning("Session Marché expirée : les prix reviennent à la moyenne du site.")
    info = read_json(_info_file(config), {})
    if chat and not info.get("expired_notified"):
        write_json(_info_file(config), {**info, "expired_notified": True})
        notify(dataclasses.replace(config, telegram_chat_id=chat),
               "🏪 La session Marché a expiré : les prix reviennent à la moyenne du site. Ouvrez une "
               "nouvelle fenêtre privée, connectez-vous, puis envoyez /market suivi des cookies (comme pour "
               "votre session, mais d'une autre fenêtre).")


def set_market_session(config: Config, chat: str, cookies: str, own_session: dict | None,
                       probe_card: str | None) -> tuple[bool, str]:
    """Cookies reçus par /market : adoptés si le site les accepte et si le Marché répond
    (essayé sur probe_card) ; sinon la session Marché en place est gardée. Renvoie
    (adoptée, message pour l'utilisateur)."""
    from .session import parse_cookies

    decoded = decode_session(parse_cookies(cookies))
    if decoded is None:
        return False, "❌ Ces cookies ne contiennent pas de session « sb-…-auth-token » lisible."
    if own_session and decoded[1].get("refresh_token") == own_session.get("refresh_token"):
        return False, ("❌ C'est la session de votre compte : les deux se gêneraient. Ouvrez une autre fenêtre "
                       "privée, connectez-vous, et envoyez /market avec les cookies de cette fenêtre.")
    with _LOCK:
        store = _store(config)
        previous = (store.cookie_name, store.session) if store.has_session() else None
        store.adopt(*decoded)
        site = WikiMasters(SiteClient(config.base_url, store), config)
        verdict = _check(site, probe_card)
        if verdict is not True:
            if previous:
                store.adopt(*previous)
            else:
                store.clear()
            return False, verdict
        write_json(_info_file(config), {"owner": str(chat), "since": now_utc().isoformat()})
    log.info("Session Marché enregistrée (compte %s).", chat)
    return True, "✅ Session Marché enregistrée : les prix de tous les comptes viennent désormais du Marché."


def _check(site: WikiMasters, probe_card: str | None):
    logged = _logged_in(site)
    if logged is None:
        return "⏳ Le site ne répond pas : renvoyez /market dans quelques minutes."
    if not logged:
        return "❌ Le site refuse cette session (expirée ?). Reconnectez-vous puis renvoyez /market."
    if probe_card is None:
        return True
    try:
        summary = site.api_get_ok(f"/api/marketplace/cards/{probe_card}/sales?scope=summary")
        data = site.fetch_market_sales(probe_card)
    except SessionExpired:
        return "❌ Ce compte n'a pas accès au Marché."
    except SiteError:
        return "⏳ Le Marché ne répond pas : renvoyez /market dans quelques minutes."
    if isinstance(summary, dict) and summary.get("isPro") is False:  # accès au Marché, selon le site
        return "❌ Ce compte n'a pas accès au Marché."
    if isinstance(data, dict) and isinstance(data.get("sales"), list):
        return True
    return "❌ Le Marché n'a pas renvoyé le détail des ventes : ce compte n'y a sans doute pas accès."
