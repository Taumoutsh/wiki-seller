"""Prix des cartes, partagés par tous les comptes (vente, défausse, prix automatiques
des achats et ventes) : cache state/price_cache.json (cache_prix), sinon le prix nettoyé
du Marché (market.py) s'il y a une session Marché, sinon la moyenne donnée par le site,
carte par carte."""

import logging
import threading
import time
from datetime import timedelta

from . import market
from .config import Config
from .site import PriceApiUnavailable, WikiMasters
from .market import MarketUnavailable
from .state import now_utc, read_json, write_json

log = logging.getLogger(__name__)

SAVE_EVERY = 50  # prix lus entre deux écritures du cache
RETRY_PAUSE = 30  # s de pause après un refus passager de l'API des prix
FAILURES_BEFORE_GIVING_UP = 5  # refus passagers de suite avant d'abandonner l'API des prix
REQUEST_PAUSE = 0.3  # s entre deux lectures de prix
MARKET_FAILURES_BEFORE_GIVING_UP = 3  # échecs du Marché de suite avant la moyenne du site seule
FALLBACK_TTL = timedelta(hours=1)  # moyenne du site prise faute de Marché : relue plus tôt
_CACHE_LOCK = threading.Lock()


def cache_file(config: Config):
    return config.shared_state_dir / "price_cache.json"  # un prix est le même pour tous les comptes


def load_price_cache(config: Config) -> dict:
    cache = read_json(cache_file(config), {})
    limit = (now_utc() - config.price_cache_ttl).isoformat()
    fallback_limit = max(limit, (now_utc() - FALLBACK_TTL).isoformat())
    return {k: v for k, v in cache.items()
            if isinstance(v, dict) and v.get("at", "") > (fallback_limit if v.get("fallback") else limit)}


def pause(seconds: float, should_stop) -> bool:
    """Attend `seconds` secondes ; False dès qu'une passe attend la session."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return True


class PriceReader:
    """Un refus passager (trop de requêtes, panne) ne fait laisser de côté que la carte
    concernée ; l'API n'est abandonnée qu'après FAILURES_BEFORE_GIVING_UP refus de suite,
    ou dès un accès interdit. Les cartes laissées de côté ne sont pas mises en cache :
    elles seront redemandées la fois suivante."""

    OK, UNKNOWN, STOPPED = "ok", "unknown", "stopped"

    def __init__(self, site: WikiMasters, config: Config):
        self.site, self.config = site, config
        self.cache = load_price_cache(config)
        self.api_ok = True
        self.failures = 0  # refus passagers de suite
        self.fetched = 0  # prix demandés au site
        self.seconds = 0.0  # temps passé à les demander
        self.retries_at_start = site.retries
        self.market_ok = True
        self.market_failures = 0  # échecs du Marché de suite

    def read(self, group, should_stop=lambda: False) -> tuple[str, float | None]:
        """(OK, prix ou None si aucune vente connue), (UNKNOWN, None) si le prix n'a pas
        pu être lu, ou (STOPPED, None) si une passe attend la session."""
        key = f"{group.card_id}:{group.rarity}"
        if key in self.cache:
            return self.OK, self.cache[key]["average"]
        if not self.api_ok:
            return self.UNKNOWN, None
        if should_stop():
            return self.STOPPED, None
        started = time.monotonic()
        found, fallback = self._market(group)
        if found is not None:
            self.seconds += time.monotonic() - started
            return self._keep(key, {"average": found.price, "source": "marche"})
        try:
            average = self.site.fetch_average(group.card_id, group.rarity)
        except PriceApiUnavailable as exc:
            self.failures += 1
            if exc.transient and self.failures < FAILURES_BEFORE_GIVING_UP:
                log.info("Prix de « %s » non lu (%s) : carte laissée de côté, pause de %d s.",
                         group.title, exc, RETRY_PAUSE)
                return (self.UNKNOWN if pause(RETRY_PAUSE, should_stop) else self.STOPPED), None
            log.warning("API des prix indisponible (%s) : les cartes sans prix en cache sont laissées "
                        "de côté jusqu'à la prochaine fois.", exc)
            self.api_ok = False
            return self.UNKNOWN, None
        finally:
            self.seconds += time.monotonic() - started
        self.failures = 0
        return self._keep(key, {"average": average, "source": "moyenne", **({"fallback": True} if fallback else {})})

    def _keep(self, key: str, entry: dict) -> tuple[str, float | None]:
        self.fetched += 1
        self.cache[key] = {**entry, "at": now_utc().isoformat()}
        if self.fetched % SAVE_EVERY == 0:
            self.save()  # une passe interrompue ne perd pas les prix déjà lus
        time.sleep(REQUEST_PAUSE)
        return self.OK, entry["average"]

    def _market(self, group) -> tuple[market.MarketPrice | None, bool]:
        """(prix nettoyé du Marché ou None, True si le Marché aurait dû répondre : la
        moyenne du site prise à la place sera relue plus tôt)."""
        if not market.configured(self.config):
            return None, market.owner(self.config) is not None  # session Marché expirée : à renouveler
        if not self.market_ok:
            return None, True
        try:
            found = market.market_price(self.config, group.card_id, group.rarity)
        except MarketUnavailable as exc:
            self.market_failures += 1
            if self.market_failures >= MARKET_FAILURES_BEFORE_GIVING_UP or not market.configured(self.config):
                log.warning("Marché indisponible (%s) : moyenne du site jusqu'à la prochaine fois.", exc)
                self.market_ok = False
            else:
                log.info("Marché : ventes de « %s » non lues (%s) ; moyenne du site.", group.title, exc)
            return None, True
        self.market_failures = 0
        if found is None:
            log.info("Marché : trop peu de ventes récentes de « %s » ; moyenne du site.", group.title)
        else:
            log.info("Marché : « %s » à %.0f (%d vente(s) retenue(s), %d écartée(s)%s).", group.title, found.price,
                     found.sales, found.removed, ", changement de valeur" if found.trend else "")
        return found, False

    def stats(self) -> str:
        """« 120 prix demandés au site, 7,9 s en moyenne, 85 nouvel(s) essai(s) après refus »."""
        if not self.fetched:
            return "aucun prix demandé au site"
        retries = self.site.retries - self.retries_at_start
        average = f"{self.seconds / self.fetched:.1f}".replace(".", ",")
        return (f"{self.fetched} prix demandé(s) au site, {average} s en moyenne, "
                f"{retries} nouvel(s) essai(s) après refus du site")

    def save(self) -> None:
        """Fusionne avec le fichier : plusieurs comptes lisent des prix en même temps."""
        with _CACHE_LOCK:
            merged = read_json(cache_file(self.config), {})
            for key, entry in self.cache.items():
                if not isinstance(merged.get(key), dict) or entry.get("at", "") >= merged[key].get("at", ""):
                    merged[key] = entry
            write_json(cache_file(self.config), merged)


def fetch_averages(site: WikiMasters, config: Config, groups, should_stop=lambda: False):
    """Prix d'une liste de cartes. Renvoie (prix par carte et rareté (CardGroup.key),
    cartes dont le prix n'a pas pu être lu, True si tout a été lu sans interruption)."""
    reader = PriceReader(site, config)
    averages: dict[str, float | None] = {}
    unknown: set[str] = set()
    try:
        for group in groups:
            status, average = reader.read(group, should_stop)
            if status == reader.STOPPED:
                return averages, unknown, False
            if status == reader.UNKNOWN:
                unknown.add(group.key)
            else:
                averages[group.key] = average
    finally:
        reader.save()
    return averages, unknown, True
