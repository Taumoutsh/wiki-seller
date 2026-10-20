"""Collection d'un compte gardée en mémoire COLLECTION_TTL : relire des milliers de cartes,
page par page, prend plusieurs minutes, pendant lesquelles la session du compte est tenue
(défausse en pause, réveils de fin d'enchère retardés). Une passe reprend donc la
dernière lecture récente, celle d'une passe ou celle de la défausse.

Un exemplaire disparu depuis (vendu, défaussé) fait seulement échouer sa mise en vente :
l'exemplaire suivant est essayé. Une carte obtenue (achat terminé) fait relire la
collection à la passe suivante (forget)."""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from .config import Config
from .site import entry_rarity

log = logging.getLogger(__name__)

COLLECTION_TTL = timedelta(hours=1)
_LOCK = threading.Lock()


@dataclass
class _Read:
    at: float  # time.monotonic()
    rarities: tuple[str, ...]  # raretés lues ; () : toutes
    entries: list[dict]
    in_trade: set[str]


_READS: dict[str, _Read] = {}


def _key(config: Config) -> str:
    return str(config.session_file)


def remember(config: Config, entries: list[dict], in_trade: set[str], rarities: tuple[str, ...] = ()) -> None:
    with _LOCK:
        _READS[_key(config)] = _Read(time.monotonic(), tuple(rarities), list(entries), set(in_trade))


def forget(config: Config) -> None:
    with _LOCK:
        _READS.pop(_key(config), None)


def cached_entries() -> list[dict]:
    """Entrées des collections lues récemment, tous comptes confondus (fiche /details)."""
    with _LOCK:
        reads = list(_READS.values())
    return [e for read in reads if time.monotonic() - read.at < COLLECTION_TTL.total_seconds() for e in read.entries]


def fetch(site, config: Config, rarities: tuple[str, ...] = ()) -> tuple[list[dict], set[str]]:
    """Entrées de la collection (raretés demandées) et exemplaires en échange : la dernière
    lecture de moins de COLLECTION_TTL qui les couvre, sinon une nouvelle lecture."""
    with _LOCK:
        read = _READS.get(_key(config))
    if read is not None and time.monotonic() - read.at < COLLECTION_TTL.total_seconds() \
            and (not read.rarities or (rarities and set(rarities) <= set(read.rarities))):
        entries = [e for e in read.entries if not rarities or entry_rarity(e) in rarities]
        log.info("Collection reprise de la lecture d'il y a %d min : %d entrée(s)%s.",
                 (time.monotonic() - read.at) // 60, len(entries),
                 f" de rareté {', '.join(rarities)}" if rarities else "")
        return entries, set(read.in_trade)
    entries, in_trade = site.fetch_collection(rarities)
    remember(config, entries, in_trade, rarities)
    return entries, in_trade
