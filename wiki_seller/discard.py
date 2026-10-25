"""Défausse automatique en arrière-plan des cartes de faible valeur.

À chaque réveil, un thread à part est lancé (sauf s'il tourne déjà, ou vient de finir).
Il lit la collection (une seule fois, page par page) et, au-delà de DISCARD_TRIGGER
exemplaires, examine les cartes des plus communes aux plus rares : prix moyen lu (cache,
sinon l'API), puis défausse immédiate par l'API si la carte vaut moins de DISCARD_BELOW.

Il partage la session avec les passes du compte (session.gate) et leur cède la place dès qu'elles
attendent. L'avancement est gardé dans state/discard_progress.json : la défausse reprend
là où elle s'était arrêtée, même après un redémarrage."""

import dataclasses
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .lists import protected_names
from . import collection
from .price_worker import DISCARD
from .pricing import PriceReader, reliable
from .report import record_discard
from .selection import CardGroup, copies_not_on_sale, count_listed, normalize_name, parse_collection
from .session import gate, open_site
from .site import CollectionScan, DiscardStatus, HumanCheckRequired, SessionExpired, WikiMasters
from .state import now_utc, parse_time, read_json, write_json

log = logging.getLogger(__name__)

# Raretés défaussées même sans prix moyen connu (aucune vente sur le marché).
DISCARD_WITHOUT_PRICE = ("C", "PC")
# Ordre d'examen : les plus communes d'abord (les plus susceptibles de valoir peu).
RARITY_ORDER = ("C", "PC", "R", "SR", "UR", "L")
PAUSE = (1.0, 3.0)  # s entre deux défausses, pour garder un rythme humain
RECHECK = timedelta(hours=1)  # délai avant de relancer une défausse terminée
RECHECK_UNPRICED = timedelta(minutes=15)  # idem s'il restait des prix à lire
# Carte R ou plus rare au prix illisible : défaussée au deuxième constat, si le premier date
# d'au moins UNPRICED_GRACE (une panne courte ne compte pas deux fois) et si le site donne
# bien les autres prix de la défausse (HEALTHY_MIN_READS lus, au plus HEALTHY_MAX_FAILURES
# d'échecs) : une panne générale ne doit coûter aucune carte de valeur.
UNPRICED_GRACE = timedelta(hours=6)
HEALTHY_MIN_READS = 5
HEALTHY_MAX_FAILURES = 0.2
PROGRESS_MAX_AGE = timedelta(hours=24)  # au-delà, la collection lue est trop ancienne
SAVE_EVERY_PAGES = 20  # pages de collection lues entre deux sauvegardes
SAVE_EVERY_CARDS = 20  # cartes examinées entre deux sauvegardes
LOG_EVERY = 100  # cartes examinées entre deux lignes de progression


@dataclass
class DiscardItem:
    card_id: str
    title: str
    rarity: str | None
    average: float | None  # None : aucune vente connue (C/PC)
    copies: int
    copy_ids: list[str] = field(default_factory=list)
    unreadable: bool = False  # prix illisible (site en panne…) : défaussée seulement si C/PC
    few_sales: int | None = None  # ventes connues sous ventes_min : défaussée quel que soit son prix


def total_copies(groups: list[CardGroup]) -> int:
    return sum(g.count for g in groups)


def discard_candidates(groups: list[CardGroup], protected_names: set[str], selling: list[dict]) -> list[DiscardItem]:
    """Cartes à examiner, les plus communes d'abord. Jamais les cartes protégées
    (cartes à garder et cartes voulues), ni les exemplaires en vente, ni les shiny."""
    items = []
    for group in groups:
        if normalize_name(group.title) in protected_names or group.shiny:  # shiny : jamais défaussé
            continue
        copies = group.count - count_listed(group, selling)
        if copies > 0:
            items.append(DiscardItem(group.card_id, group.title, group.rarity, None, copies,
                                     copies_not_on_sale(group, selling)[:copies]))
    rank = {r: i for i, r in enumerate(RARITY_ORDER)}
    items.sort(key=lambda i: (rank.get(i.rarity or "", len(rank)), normalize_name(i.title)))
    return items


def should_discard(rarity: str | None, average: float | None, below: int) -> bool:
    """Prix moyen connu de `below` ou moins, ou aucune vente connue pour une C/PC."""
    if average:
        return average <= below
    return (rarity or "") in DISCARD_WITHOUT_PRICE


# ─────────────────────────── Avancement ───────────────────────────


def progress_file(config: Config):
    return config.state_dir / "discard_progress.json"


@dataclass
class DiscardProgress:
    started_at: str = field(default_factory=lambda: now_utc().isoformat())
    scan: CollectionScan = field(default_factory=CollectionScan)
    cards: list[DiscardItem] | None = None  # cartes à examiner, les plus communes d'abord
    position: int = 0  # carte en cours d'examen
    copies_done: int = 0  # exemplaires déjà défaussés de cette carte
    done: int = 0  # exemplaires défaussés en tout
    unpriced: int = 0  # cartes dont le prix n'a pas pu être lu : relance plus tôt
    persist: bool = True  # False en simulation : rien n'est enregistré

    def save(self, config: Config, finished: bool = False) -> None:
        if not self.persist:
            return
        data = {"started_at": self.started_at, "position": self.position,
                "copies_done": self.copies_done, "done": self.done, "unpriced": self.unpriced}
        if finished:
            data["finished_at"] = now_utc().isoformat()
        elif self.cards is not None:  # la collection lue ne sert plus une fois les cartes listées
            data["cards"] = [dataclasses.asdict(i) for i in self.cards]
        else:
            scan = self.scan
            data["scan"] = {"rarity": scan.rarity, "entries": scan.entries, "seen": sorted(scan.seen),
                            "in_trade": sorted(scan.in_trade), "next_page": scan.next_page, "done": scan.done}
        write_json(progress_file(config), data, indent=None)

    @classmethod
    def load(cls, config: Config) -> "DiscardProgress":
        """Reprend la défausse sauvegardée, ou en commence une nouvelle si elle est finie,
        trop ancienne ou illisible."""
        data = read_json(progress_file(config), {})
        try:
            started = datetime.fromisoformat(data["started_at"])
            if data.get("finished_at") or now_utc() - started > PROGRESS_MAX_AGE:
                return cls()
            progress = cls(started_at=data["started_at"], position=data["position"],
                           copies_done=data["copies_done"], done=data["done"], unpriced=data.get("unpriced", 0))
            if data.get("cards") is not None:
                if not any(i.get("copy_ids") for i in data["cards"]):
                    # Cartes listées par une ancienne version, sans identifiant d'exemplaire :
                    # impossibles à défausser par l'API, on relit la collection.
                    log.info("Défausse : avancement d'une ancienne version ; la collection va être relue.")
                    return cls()
                progress.cards = [DiscardItem(**i) for i in data["cards"]]
            elif data.get("scan"):
                raw = data["scan"]
                progress.scan = CollectionScan(rarity=raw["rarity"], entries=raw["entries"], seen=set(raw["seen"]),
                                               in_trade=set(raw["in_trade"]), next_page=raw["next_page"],
                                               done=raw["done"])
            else:
                return cls()
        except (KeyError, TypeError, ValueError):
            return cls()
        if progress.cards is not None:
            log.info("Défausse reprise : %d/%d carte(s) examinée(s), %d exemplaire(s) défaussé(s).",
                     progress.position, len(progress.cards), progress.done)
        else:
            log.info("Défausse reprise : %d entrée(s) de la collection déjà lues.", len(progress.scan.entries))
        return progress


def next_start(config: Config) -> datetime | None:
    """Heure à partir de laquelle la défausse peut être relancée (None : tout de suite)."""
    data = read_json(progress_file(config), {})
    finished = parse_time(data.get("finished_at"))
    if finished is None:
        return None
    restart = finished + (RECHECK_UNPRICED if data.get("unpriced") else RECHECK)
    return restart if now_utc() < restart else None


# ─────────────────────────── Thread ───────────────────────────


class DiscardWorkers:
    """Un thread de défausse à la fois par compte."""

    def __init__(self):
        self._threads: dict = {}
        self._lock = threading.Lock()

    def running(self, config: Config | None = None) -> bool:
        """Défausse en cours pour ce compte (pour n'importe lequel si config est None)."""
        with self._lock:
            threads = [self._threads.get(config.state_dir)] if config else list(self._threads.values())
        return any(t is not None and t.is_alive() for t in threads)

    def start(self, config: Config, dry_run: bool, debug: bool) -> bool:
        with self._lock:
            current = self._threads.get(config.state_dir)
            if current is not None and current.is_alive():
                return False
            thread = threading.Thread(target=run_discard, args=(config, dry_run, debug),
                                      name=f"défausse-{config.account_name or 'compte'}", daemon=True)
            self._threads[config.state_dir] = thread
            thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout)


DISCARDER = DiscardWorkers()


def check_discard(config: Config, dry_run: bool, debug: bool) -> None:
    """À chaque réveil : lance la défausse en arrière-plan, sauf si elle tourne déjà ou
    vient de finir. Le thread lit lui-même la collection : la passe ne l'attend pas."""
    if not config.discard_below:
        return
    if DISCARDER.running(config):
        log.info("Défausse déjà en cours en arrière-plan.")
        return
    restart = next_start(config)
    if restart:
        log.info("Défausse terminée récemment : prochaine au plus tôt à %s.", restart.astimezone().strftime("%H:%M"))
        return
    log.info("Défausse lancée en arrière-plan (cartes à %d ou moins au-delà de %d exemplaires).",
             config.discard_below, config.discard_trigger)
    DISCARDER.start(config, dry_run, debug)


def run_discard(config: Config, dry_run: bool, debug: bool) -> bool:
    """Corps du thread. Renvoie True si la défausse est allée au bout (False après une
    erreur : elle reprendra au réveil suivant, là où elle s'était arrêtée)."""
    # Une simulation ne reprend ni n'enregistre l'avancement : la vraie défausse ne
    # sauterait pas les cartes « simulées », et ne serait pas retardée par sa fin.
    progress = DiscardProgress(persist=False) if dry_run else DiscardProgress.load(config)
    try:
        try:
            while not _round(config, dry_run, debug, progress):
                progress.save(config)
                log.info("Défausse en pause : une passe a besoin de la session ; reprise ensuite.")
        except BaseException:
            progress.save(config)
            raise
        progress.save(config, finished=True)
        return True
    except SessionExpired as exc:
        log.warning("Défausse interrompue : session expirée (%s).", exc)
    except HumanCheckRequired as exc:
        log.warning("Défausse interrompue : vérification anti-bot demandée (%s) ; reprise au prochain réveil.", exc)
    except Exception:
        log.exception("Défausse interrompue par une erreur ; nouvel essai au prochain réveil.")
    return False


def _round(config: Config, dry_run: bool, debug: bool, progress: DiscardProgress) -> bool:
    """Une période avec la session. True quand la défausse est finie, False si elle a
    cédé la place à une passe."""
    with open_site(config, debug, background=True) as site:
        if progress.cards is None and not _list_cards(site, config, progress):
            return False
        if progress.cards is None:
            return True  # seuil non dépassé
        reader = PriceReader(site, config, priority=DISCARD, wait=True)
        if reader.worker is not None:  # prix lus par le fil commun : toute la suite est demandée d'avance
            for item in progress.cards[progress.position:]:
                if (item.rarity or "") in config.discard_rarities and f"{item.card_id}:{item.rarity}" not in reader.cache:
                    reader.worker.request(item, config, DISCARD)
        unreadable = Unreadable(config)
        try:
            while progress.position < len(progress.cards):
                item = progress.cards[progress.position]
                if progress.copies_done == 0:  # sinon : prix déjà lu, défausse en cours
                    if (item.rarity or "") not in config.discard_rarities:  # rareté exclue (réglage changé)
                        _next_card(config, progress, reader)
                        continue
                    if gate(config).should_yield():
                        return False
                    status, average = reader.read(item, gate(config).should_yield)
                    if status == reader.STOPPED:
                        return False
                    unreadable.saw(item, status == reader.UNKNOWN)
                    if (status == reader.UNKNOWN and (item.rarity or "") not in DISCARD_WITHOUT_PRICE
                            and not unreadable.second_time(item)):
                        progress.unpriced += 1  # rare au prix illisible : gardée, redemandée plus tard
                        _next_card(config, progress, reader)
                        continue
                    # Trop peu de ventes (ventes_min), ou aucune : pas de prix auquel se fier, la
                    # carte est défaussée quel que soit son prix moyen.
                    known = reader.entry(item)
                    item.few_sales = None
                    if status == reader.OK and (not average or not reliable(known, config.min_sales_to_sell)):
                        item.few_sales = known.get("count") if isinstance(known.get("count"), int) else 0
                    item.average, item.unreadable = average, status == reader.UNKNOWN
                    if (not item.unreadable and item.few_sales is None
                            and not should_discard(item.rarity, average, config.discard_below)):
                        _next_card(config, progress, reader)
                        continue
                if not _discard_copies(site, config, item, progress, dry_run):
                    return False
                _next_card(config, progress, reader)
        finally:
            reader.save()
            unreadable.save()
        log.info("Défausse terminée : %d exemplaire(s) %s, %d carte(s) examinée(s)%s ; %s.", progress.done,
                 "simulé(s)" if dry_run else "défaussé(s)", len(progress.cards),
                 f", {progress.unpriced} sans prix lu (redemandées dans {RECHECK_UNPRICED.seconds // 60} min)"
                 if progress.unpriced else "", reader.stats())
        return True


class Unreadable:
    """Cartes dont le prix n'a pas pu être lu, avec la date du premier constat
    (state/users/<chat_id>/unpriced.json), et état du site pendant cette défausse."""

    def __init__(self, config: Config):
        self.path = config.state_dir / "unpriced.json"
        self.first_seen: dict[str, str] = read_json(self.path, {})
        self.read, self.failed = 0, 0  # prix lus et ratés pendant cette défausse

    @staticmethod
    def _key(item) -> str:
        return f"{item.card_id}:{item.rarity}"

    def saw(self, item, failed: bool) -> None:
        if failed:
            self.failed += 1
            self.first_seen.setdefault(self._key(item), now_utc().isoformat())
        else:
            self.read += 1
            self.first_seen.pop(self._key(item), None)

    def healthy(self) -> bool:
        total = self.read + self.failed
        return self.read >= HEALTHY_MIN_READS and self.failed <= HEALTHY_MAX_FAILURES * total

    def second_time(self, item) -> bool:
        """Prix déjà illisible il y a au moins UNPRICED_GRACE, site en bon état : à défausser."""
        first = parse_time(self.first_seen.get(self._key(item)))
        return bool(first and now_utc() - first >= UNPRICED_GRACE and self.healthy())

    def save(self) -> None:
        write_json(self.path, self.first_seen)


def _discard_copies(site: WikiMasters, config: Config, item: DiscardItem, progress: DiscardProgress,
                    dry_run: bool) -> bool:
    """Défausse les exemplaires de la carte. False si une passe attend la session."""
    few = f"{item.few_sales} vente(s) connue(s), il en faut {config.min_sales_to_sell}"
    price = (f"{few}, prix moyen {item.average:g} peu fiable" if item.few_sales is not None and item.average
             else few if item.few_sales is not None
             else f"prix moyen {item.average:g}" if item.average else
             "prix illisible, déjà constaté" if item.unreadable and (item.rarity or "") not in DISCARD_WITHOUT_PRICE
             else "prix illisible" if item.unreadable else "aucune vente connue")
    if normalize_name(item.title) in protected_names(config):  # protégée entre-temps (/safe, /buy, /sell)
        log.info("Défausse : %s est protégée ; carte suivante.", item.title)
        return True
    if not item.copy_ids:
        log.warning("Défausse : %s : exemplaires sans identifiant, impossible à défausser ; carte suivante.",
                    item.title)
    while progress.copies_done < min(item.copies, len(item.copy_ids)):
        if gate(config).should_yield():
            return False
        status, detail = site.discard_card(item.copy_ids[progress.copies_done], dry_run)
        if status is DiscardStatus.FAILED:
            log.warning("Défausse : %s : %s (%s) ; carte suivante.", item.title, status.value, detail)
            break
        progress.copies_done += 1
        progress.done += 1
        if status is DiscardStatus.DISCARDED:
            record_discard(config)
        progress.save(config)
        log.info("Défausse : %s (%s, %s) : %s%s.", item.title, item.rarity or "?", price, status.value,
                 f" via {detail}" if detail else "")
        time.sleep(random.uniform(*PAUSE))
    return True


def _next_card(config: Config, progress: DiscardProgress, reader: PriceReader) -> None:
    progress.position += 1
    progress.copies_done = 0
    if progress.position % LOG_EVERY == 0:
        log.info("Défausse : %d/%d carte(s) examinée(s), %d exemplaire(s) défaussé(s) ; %s.",
                 progress.position, len(progress.cards), progress.done, reader.stats())
    if progress.position % SAVE_EVERY_CARDS == 0:
        progress.save(config)


def _list_cards(site: WikiMasters, config: Config, progress: DiscardProgress) -> bool:
    """Lit la collection (reprise page par page) et liste les cartes à examiner. False si
    une passe attend la session avant la fin ; progress.cards reste None si la collection
    ne dépasse pas DISCARD_TRIGGER."""
    def save_now_and_then(scan: CollectionScan) -> None:
        if scan.next_page % SAVE_EVERY_PAGES == 0:
            progress.save(config)

    if not site.scan_collection(progress.scan, gate(config).should_yield, on_page=save_now_and_then):
        log.info("Défausse : %d entrée(s) de la collection lues avant la pause.", len(progress.scan.entries))
        return False
    log.info("Collection chargée : %d entrée(s).", len(progress.scan.entries))
    collection.remember(config, progress.scan.entries, progress.scan.in_trade)  # reprise par les passes
    count = total_copies(parse_collection(progress.scan.entries))
    if count <= config.discard_trigger:
        log.info("Collection : %d exemplaire(s), seuil de défausse %d non dépassé.", count, config.discard_trigger)
        return True
    groups = parse_collection(progress.scan.entries, excluded_copy_ids=progress.scan.in_trade)
    progress.cards = [i for i in discard_candidates(groups, protected_names(config), site.fetch_market_state().selling)
                      if (i.rarity or "") in config.discard_rarities]
    progress.save(config)
    log.info("Collection : %d exemplaire(s) > %d. Défausse au fil de l'eau de %d carte(s), les plus "
             "communes d'abord, à %d ou moins.", count, config.discard_trigger, len(progress.cards),
             config.discard_below)
    return True
