"""Fiche d'une carte du Marché (/details) : graphique des ventes, dernières ventes et
prix (moyen, médian, plus bas, plus haut, prix retenu par le bot), par rareté, avec les
enchères en cours de la carte.

La fiche est une page HTML autonome (graphique en SVG, sans ressource externe ; un petit
script facultatif montre la vente la plus proche là où l'on touche le graphique) envoyée
en document Telegram : elle s'ouvre sur téléphone comme sur ordinateur. Elle est
préparée en mémoire et n'est jamais écrite sur le serveur.

La carte se donne par son nom (titre exact, sans tenir compte des majuscules ni des
accents), cherché dans la base du site (table des cartes, lecture publique comme sa page
« Toutes les cartes »), par un lien ou un identifiant d'enchère, ou par l'identifiant de
la carte. Les ventes se lisent avec la session Marché, sinon avec
celle du compte (qui doit alors avoir accès au Marché) ; rien n'est jamais envoyé au site
(lectures seulement)."""

import html
import json
import logging
import math
import re
import statistics
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import collection, market
from .auth import USER_AGENT
from .config import Config
from .lists import AUTO_BUY_RATIO, AUTO_SELL_RATIO, SHINY, ListError, parse_card_command
from .selection import is_shiny, normalize_name
from .settings import RARITIES
from .site import SessionExpired, SiteClient, SiteError, WikiMasters, entry_rarity
from .state import SITE_TIMEZONE, now_utc, parse_time

log = logging.getLogger(__name__)

USAGE = ("Usage : /details <carte> [rareté], ex. /details Tour Eiffel, /details Tour Eiffel L+, "
         "/details Apollo 11; SR, ou le lien d'une enchère de la carte")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
LAST_SALES_LISTED = 40
CARD_FIELDS = "id,wikipedia_title,wikipedia_url,summary,category,rarity,atk,def,pageviews,q_score"
DB_TIMEOUT = 20  # s : requête à la base du site
SUMMARY_SHOWN = 280  # caractères de la description affichés avant « lire la suite »
OUT = " class='out'"  # vente écartée par le bot
# Graphique dessiné en deux tailles (sans script) : large sur ordinateur, étroit sur téléphone.
CHART_SIZES = {"wide": (720, 300, 56), "narrow": (360, 300, 44)}  # largeur, hauteur, marge gauche
PAD_R, PAD_T, PAD_B = 12, 14, 30
TREND_WINDOW = 15  # ventes autour de chacune pour la tendance (médiane glissante)
TREND_MIN = 4  # ventes retenues d'une rareté à partir desquelles la tendance est tracée
DENSE_SALES = 60  # au-delà, les ventes sont de petits points discrets
# Évolution du prix (sur la tendance) : depuis la première vente connue, puis sur ces périodes.
GROWTH_PERIODS = ((timedelta(days=30), "sur 30 j"), (timedelta(days=15), "sur 15 j"), (timedelta(days=7), "sur 7 j"),
                  (timedelta(days=1), "sur 24 h"))
GROWTH_MIN_SALES = 3  # ventes retenues dans la période, au moins, pour en donner l'évolution
TIP_H, TIP_CHAR = 36, 6.3  # bulle d'une vente : hauteur, largeur par caractère (police de 11 px)
COLORS = {"L": "#d49a00", SHINY: "#d0258f", "UR": "#dc2626", "SR": "#7c3aed", "R": "#2563eb", "PC": "#0d9488",
          "C": "#6b7280", "": "#475569"}
ORDER = ("L", SHINY, "UR", "SR", "R", "PC", "C", "")


class DetailsError(Exception):
    pass


@dataclass(frozen=True)
class Sale:
    at: datetime
    price: float
    rarity: str  # « L+ » pour une légendaire shiny ; « » si le site ne la donne pas


@dataclass
class Group:
    """Ventes d'une rareté, avec leurs prix."""

    rarity: str
    sales: list[Sale]
    site_average: float | None = None
    clean: market.MarketPrice | None = None  # prix nettoyé des ventes du Marché (au moins 3)
    retained: float | None = None  # prix sur lequel le bot décide (assez de ventes pour s'y fier)
    source: str = ""  # « marche » ou « moyenne » (moyenne du site)
    count: int | None = None  # ventes connues, aberrantes comprises (comparées à ventes_min)
    low: float | None = None  # trop peu de ventes (ventes_min) : la plus basse (vente en dernier recours)
    outliers: set[int] = field(default_factory=set)  # index (dans sales) des ventes écartées par le bot

    @property
    def prices(self) -> list[float]:
        return [s.price for s in self.sales]


@dataclass
class Details:
    card_id: str
    title: str
    groups: list[Group]
    auctions: list[dict]
    source: str  # « session Marché » ou « session du compte »
    at: datetime
    card: dict = field(default_factory=dict)  # ligne de la base du site (description, ATK…)


# ─────────────────────────── Lecture ───────────────────────────


def parse_market_sales(data: object) -> list[Sale]:
    """Ventes de la réponse du site (« sales », et « recent » s'il est là), sans doublon."""
    if not isinstance(data, dict):
        data = {"sales": data}
    seen, sales = set(), []
    for key in ("sales", "recent"):
        for item in data.get(key) if isinstance(data.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            price = next((p for k in ("final_price", "price", "amount") if (p := market._number(item.get(k)))), None)
            at = next((t for k in ("settled_at", "sold_at", "created_at", "end_at")
                       if (t := parse_time(item.get(k)))), None)
            if price is None or at is None:
                continue
            at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            rarity = entry_rarity(item)
            if rarity == "L" and is_shiny(item):
                rarity = SHINY
            ident = item.get("id") or (at, price, rarity)
            if ident in seen:
                continue
            seen.add(ident)
            sales.append(Sale(at, price, rarity))
    return sorted(sales, key=lambda s: s.at)


def build_groups(sales: list[Sale], summary: dict | None, rarity: str | None, now: datetime,
                 min_sales: int = 1) -> list[Group]:
    """Ventes par rareté (celle demandée seulement, si rarity), avec le prix retenu par le
    bot et les ventes qu'il écarte, comme pricing.PriceReader : prix nettoyé du Marché avec
    au moins 3 ventes récentes, sinon moyenne du site ; avec moins de min_sales ventes
    (ventes_min), le bot ne s'y fie pas (défausse, ou vente en dernier recours au plus bas)."""
    by_rarity: dict[str, list[Sale]] = {}
    for sale in sales:
        by_rarity.setdefault(sale.rarity, []).append(sale)
    groups = []
    for key in sorted(by_rarity, key=lambda r: ORDER.index(r) if r in ORDER else len(ORDER)):
        if rarity and key != rarity:
            continue
        group = Group(key, by_rarity[key])
        group.clean = market.clean_price([market.Sale(s.at, s.price) for s in group.sales], now)
        if group.clean is not None and group.clean.sales + group.clean.removed < market.MIN_SALES:
            group.low, group.clean = group.clean.low, None  # trop peu de ventes récentes : moyenne du site
        group.outliers = _outliers(group.sales, now)
        groups.append(group)
    for key, entry in (summary or {}).items():  # moyenne du site, même sans détail des ventes
        key = str(key).upper()
        if rarity and key != ("L" if rarity == SHINY else rarity):
            continue
        average = market._number(entry.get("average")) if isinstance(entry, dict) else None
        group = next((g for g in groups if g.rarity == key), None)
        if group is None and average:
            group = Group(key, [])
            groups.append(group)
        if group is not None:
            group.site_average = average
            count, latest = entry.get("count"), market._number(entry.get("latest"))
            group.count = count if isinstance(count, int) and not isinstance(count, bool) else None
            group.low = min((p for p in (group.low, latest, average) if p), default=None)
    for group in groups:
        if group.clean is not None:
            group.retained, group.source = group.clean.price, "marche"
            group.count = group.clean.sales + group.clean.removed  # ventes connues, aberrantes comprises
        elif group.site_average:
            group.retained, group.source = group.site_average, "moyenne"
        if group.retained is not None and group.count is not None and group.count < min_sales:
            group.retained = None  # pas assez de ventes pour s'y fier : la plus basse (group.low)
        elif group.retained is not None:
            group.low = None
    return groups


def _outliers(sales: list[Sale], now: datetime) -> set[int]:
    """Ventes que le calcul du bot écarte (même règle que market.clean_price)."""
    used = {(s.at, s.price) for s in market.used_sales([market.Sale(s.at, s.price) for s in sales], now)}
    recent = [i for i, s in enumerate(sales) if (s.at, s.price) in used]
    prices = [sales[i].price for i in recent]
    if len(prices) < market.MIN_SALES:
        return set()
    median = statistics.median(prices)
    spread = max(market.MAD_TO_STDEV * statistics.median(abs(p - median) for p in prices),
                 market.MIN_SPREAD_RATIO * median)
    clean = market.clean_price([market.Sale(sales[i].at, sales[i].price) for i in recent], now)
    if clean is not None and clean.trend:  # nouvelle valeur : les ventes d'avant sont écartées
        return set(recent[:clean.removed])
    return {i for i, p in zip(recent, prices) if abs(p - median) > market.OUTLIER_SPREAD * spread}


def _site(config: Config, use_market: bool) -> tuple[WikiMasters, str]:
    from .session import gate, session_store

    if use_market:
        return market.market_site(config), "session Marché"
    store = session_store(config)
    if not store.has_session():
        raise DetailsError("Aucune session pour lire le Marché : envoyez d'abord les cookies de votre compte, "
                           "ou une session Marché (/market).")
    client = SiteClient(config.base_url, store)
    client.before = gate(config).wait_watches  # une fin d'enchère surveillée passe avant
    return WikiMasters(client, config), "session du compte"


def resolve_card(site: WikiMasters, query: str) -> tuple[dict | None, list[dict]]:
    """(carte de la base du site, ou au moins {"id", "wikipedia_title"} ; enchères en cours
    de la carte). Carte None si introuvable."""
    found = UUID.search(query)
    if found:  # identifiant de carte, sinon lien ou identifiant d'enchère
        card = card_by_id(site, found.group(0))
        if card is None:
            auction = site.read_auction(found.group(0))
            if auction and _card_id(auction):
                card = card_by_id(site, _card_id(auction)) or {"id": _card_id(auction),
                                                                "wikipedia_title": _title(auction)}
        card = card or {"id": found.group(0)}  # ventes lues quand même
    else:
        card = find_card(site, query)
        if card is None:  # base du site injoignable : enchères en cours, collections lues récemment
            key = normalize_name(query)
            entries = [a for a in site.search_auctions(query)] + collection.cached_entries()
            entry = next((e for e in entries if normalize_name(_title(e)) == key and _card_id(e)), None)
            card = {"id": _card_id(entry), "wikipedia_title": _title(entry)} if entry else None
    if card is None:
        return None, []
    title = card.get("wikipedia_title") or ""
    auctions = [a for a in site.search_auctions(title) if _same_card(a, card["id"], title)] if title else []
    return card, auctions


def _title(entry: dict) -> str:
    card = entry.get("card") if isinstance(entry.get("card"), dict) else {}
    return card.get("wikipedia_title") or entry.get("wikipedia_title") or ""


def _card_id(entry: dict) -> str | None:
    card = entry.get("card") if isinstance(entry.get("card"), dict) else {}
    value = entry.get("card_id") or card.get("id")
    return str(value) if value else None


def _same_card(auction: dict, card_id, title: str) -> bool:
    own = _card_id(auction)
    return own == str(card_id) if own and card_id else normalize_name(_title(auction)) == normalize_name(title)


# ─────────────────────────── Base du site (table « cards », lecture publique) ───────────────────────────


def _cards(site: WikiMasters, params: list[tuple[str, str]]) -> list[dict] | None:
    """Lignes de la table des cartes (clé publique du site, comme sa page « Toutes les
    cartes ») ; None si la base ne répond pas."""
    store = site.client.store
    try:
        key = store.anon_key()
        query = urllib.parse.urlencode([("select", CARD_FIELDS), *params])
        request = urllib.request.Request(f"{store.supabase_url()}/rest/v1/cards?{query}", headers={
            "apikey": key, "Authorization": f"Bearer {key}", "User-Agent": USER_AGENT, "Accept": "application/json",
            "Referer": f"{site.config.base_url}/global-collection"})
        with urllib.request.urlopen(request, timeout=DB_TIMEOUT) as response:
            rows = json.loads(response.read() or b"[]")
    except Exception as exc:
        log.info("Fiche : base du site injoignable (%s).", str(exc)[:200])
        return None
    return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else None


def card_by_id(site: WikiMasters, card_id: str) -> dict | None:
    rows = _cards(site, [("id", f"eq.{card_id}")])
    return rows[0] if rows else None


def find_card(site: WikiMasters, name: str) -> dict | None:
    """Carte de ce titre dans la base du site, sans tenir compte des majuscules ni des
    accents. Filtres que la base sert vite : titre exact, puis recherche plein texte
    (search_document : titre et description, en minuscules sans accents) avec le titre
    sans majuscules, puis avec le début de search_document (le titre sans accents)."""
    name, key = name.strip(), normalize_name(name)
    attempts = [[("wikipedia_title", f"eq.{name}"), ("limit", "5")]]
    if key and not any(c in name for c in "*%"):
        words = ("search_document", f"plfts(simple).{key}")
        attempts += [[words, ("wikipedia_title", f"ilike.{name}"), ("limit", "5")],
                     [words, ("search_document", f"like.{key}*"), ("limit", "200")]]
    for params in attempts:
        rows = _cards(site, params)
        if rows is None:
            return None
        match = next((r for r in rows if normalize_name(str(r.get("wikipedia_title") or "")) == key), None)
        if match:
            return match
    return None


def collect(config: Config, query: str, rarity: str | None) -> Details:
    """Lit la carte et ses ventes : session Marché d'abord, sinon celle du compte."""
    attempts = [True, False] if market.configured(config) else [False]
    last_error: Exception | None = None
    for use_market in attempts:
        site, source = _site(config, use_market)
        lock = market._LOCK if use_market else threading.RLock()
        try:
            with lock:
                card, auctions = resolve_card(site, query)
                if card is None:
                    raise DetailsError(f"« {query} » introuvable : donnez le titre exact de la carte (celui de "
                                       "Wikipédia), ou le lien d'une de ses enchères.")
                card_id, title = str(card["id"]), card.get("wikipedia_title") or ""
                data = site.fetch_market_sales(card_id)
                summary_data = site.api_get_ok(f"/api/marketplace/cards/{card_id}/sales?scope=summary")
        except SessionExpired as exc:
            last_error = exc
            log.info("Fiche : %s refusée (%s).", source, exc)
            continue
        except SiteError as exc:
            raise DetailsError(f"Le site ne répond pas ({exc}) : réessayez dans quelques minutes.") from exc
        title = title or (data.get("wikipedia_title") if isinstance(data, dict) else "") or card_id
        summary = summary_data.get("summary") if isinstance(summary_data, dict) else None
        now = now_utc()
        groups = build_groups(parse_market_sales(data), summary if isinstance(summary, dict) else None, rarity, now,
                              config.min_sales_to_sell)
        return Details(card_id, title, groups, auctions, source, now, card)
    raise DetailsError("Le Marché refuse la lecture des ventes : il faut une session Marché (/market) ou un compte "
                       f"qui y a accès ({last_error}).")


# ─────────────────────────── Fiche HTML ───────────────────────────


def _tz():
    try:
        return ZoneInfo(SITE_TIMEZONE)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def _nice_step(span: float) -> float:
    raw = span / 5 or 1
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)


def _quantile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * share
    low, high = math.floor(k), math.ceil(k)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def _trend(sales: list[Sale]) -> list[tuple[Sale, float, float, float]]:
    """(vente, médiane, premier et troisième quartiles) des TREND_WINDOW ventes autour de
    chacune : la tendance du prix, et la bande des ventes courantes."""
    half = TREND_WINDOW // 2
    points = []
    for k, sale in enumerate(sales):
        window = [s.price for s in sales[max(0, k - half):k + half + 1]]
        points.append((sale, statistics.median(window), _quantile(window, 0.25), _quantile(window, 0.75)))
    return points


def growth(group: Group, now: datetime) -> list[tuple[str, float]]:
    """Évolution du prix en % sur la tendance (médiane glissante, que les ventes isolées
    ne faussent pas) : depuis la première vente connue, puis sur 30 j, 15 j, 7 j et 24 h
    quand il y a des ventes avant le début de la période et assez de ventes depuis."""
    kept = [s for i, s in enumerate(group.sales) if i not in group.outliers]
    if len(kept) < TREND_MIN:
        return []
    trend = _trend(kept)
    current = trend[-1][1]
    first = kept[0].at.astimezone(_tz()).strftime("%d/%m")
    found = [(f"depuis le {first}", (current / trend[0][1] - 1) * 100)]
    for period, label in GROWTH_PERIODS:
        since = now - period
        before = [i for i, s in enumerate(kept) if s.at <= since]
        if not before or len(kept) - 1 - before[-1] < GROWTH_MIN_SALES:
            continue  # aucune vente avant la période (déjà « depuis le … »), ou trop peu depuis
        found.append((label, (current / trend[before[-1]][1] - 1) * 100))
    return found


def _growth_chips(group: Group, now: datetime) -> str:
    chips = []
    for label, pct in growth(group, now):
        kind = "up" if pct >= 0.5 else "down" if pct <= -0.5 else "flat"
        arrow = {"up": "▲", "down": "▼", "flat": "▶"}[kind]
        chips.append(f'<span class="{kind}">{arrow} {pct:+.0f} %<small> {html.escape(label)}</small></span>')
    return f'<div class="growth">{"".join(chips)}</div>' if chips else ""


def _chart(groups: list[Group], now: datetime, size: str) -> str:
    """Tendance (médiane glissante, trait épais) sur la bande des ventes courantes, chaque
    vente en petit point ; ventes écartées par le bot en cercles ; prix retenu en pointillé."""
    sales = [s for g in groups for s in g.sales]
    if not sales:
        return '<p class="muted">Aucune vente détaillée à afficher.</p>'
    chart_w, chart_h, pad_l = CHART_SIZES[size]
    kept = {g.rarity: [s for i, s in enumerate(g.sales) if i not in g.outliers] for g in groups}
    # Échelle d'après les ventes retenues : une vente écartée hors échelle est posée au bord.
    prices = [s.price for v in kept.values() for s in v] or [s.price for s in sales]
    prices += [g.retained for g in groups if g.retained]
    step = _nice_step(max(prices) - min(prices) or max(prices))
    y_max = step * math.ceil(max(prices) * 1.05 / step)
    y_min = max(0.0, step * math.floor(min(prices) * 0.95 / step))
    start, end = min(s.at for s in sales), max(max(s.at for s in sales), now)
    if end - start < timedelta(hours=1):
        start = end - timedelta(hours=1)
    plot_w, plot_h = chart_w - pad_l - PAD_R, chart_h - PAD_T - PAD_B
    tz = _tz()

    def x(at: datetime) -> float:
        return pad_l + plot_w * (at - start).total_seconds() / (end - start).total_seconds()

    def y(price: float) -> float:
        return PAD_T + plot_h * (1 - (min(max(price, y_min), y_max) - y_min) / (y_max - y_min))

    out = [f'<svg viewBox="0 0 {chart_w} {chart_h}" role="img" aria-label="Ventes de la carte" '
           f'class="chart chart-{size}">']
    value = y_min
    while value <= y_max + 1e-9:
        out.append(f'<line x1="{pad_l}" x2="{chart_w - PAD_R}" y1="{y(value):.1f}" y2="{y(value):.1f}" class="grid"/>'
                   f'<text x="{pad_l - 6}" y="{y(value) + 4:.1f}" class="axis" text-anchor="end">{_money(value)}</text>')
        value += step
    span = end - start
    ticks = 5 if size == "wide" else 3
    for i in range(ticks):
        at = start + span * i / (ticks - 1)
        label = at.astimezone(tz).strftime("%d/%m %Hh" if span < timedelta(days=3) else "%d/%m")
        anchor = "start" if i == 0 else "end" if i == ticks - 1 else "middle"
        out.append(f'<text x="{x(at):.1f}" y="{chart_h - 8}" class="axis" text-anchor="{anchor}">{label}</text>')
    dense = len(sales) > DENSE_SALES  # beaucoup de ventes : points discrets, la tendance compte
    for group in groups:
        color = COLORS.get(group.rarity, COLORS[""])
        trend = _trend(kept[group.rarity]) if len(kept[group.rarity]) >= TREND_MIN else []
        if trend:
            band = [f"{x(s.at):.1f},{y(q3):.1f}" for s, _, _, q3 in trend]
            band += [f"{x(s.at):.1f},{y(q1):.1f}" for s, _, q1, _ in reversed(trend)]
            out.append(f'<polygon points="{" ".join(band)}" fill="{color}" class="band"/>')
        for i, sale in enumerate(group.sales):
            cx, cy = x(sale.at), y(sale.price)
            if i in group.outliers:
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="none" stroke="{color}" class="out"/>')
            else:
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{2.4 if dense else 3.5}" fill="{color}" '
                           f'class="{"dot" if dense else "dot big"}"/>')
        if trend:
            points = " ".join(f"{x(s.at):.1f},{y(median):.1f}" for s, median, _, _ in trend)
            out.append(f'<polyline points="{points}" stroke="{color}" class="trend"/>')
        if group.retained:
            out.append(f'<line x1="{pad_l}" x2="{chart_w - PAD_R}" y1="{y(group.retained):.1f}" '
                       f'y2="{y(group.retained):.1f}" stroke="{color}" class="clean"/>')
        # Par-dessus tout : chaque vente touchée (ou survolée) affiche sa bulle.
        medians = {id(s): median for s, median, _, _ in trend}
        for i, sale in enumerate(group.sales):
            line1 = f"{sale.at.astimezone(tz).strftime('%d/%m %H:%M')} · {_money(sale.price)}"
            line2 = ("écartée par le bot" if i in group.outliers
                     else f"tendance {_money(medians[id(sale)])}" if id(sale) in medians else group.rarity or "")
            out.append(_point(x(sale.at), y(sale.price), color, line1, line2, (pad_l, chart_w - PAD_R, PAD_T)))
    out.append("</svg>")
    return "".join(out)


def _point(cx: float, cy: float, color: str, line1: str, line2: str, bounds: tuple[float, float, float]) -> str:
    """Zone touchable d'une vente, avec sa bulle (montrée au toucher, au survol ou au
    clavier ; sans script, :focus suffit)."""
    left, right, top = bounds
    width = max(len(line1), len(line2)) * TIP_CHAR + 14
    bx = min(max(cx - width / 2, left), right - width)
    by = cy - TIP_H - 10 if cy - TIP_H - 10 >= top else cy + 10
    return (f'<g class="pt" tabindex="0" data-x="{cx:.1f}" data-y="{cy:.1f}">'
            f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="10"/>'
            f'<circle class="ring" cx="{cx:.1f}" cy="{cy:.1f}" r="5.5" stroke="{color}"/>'
            f'<g class="tip"><rect x="{bx:.1f}" y="{by:.1f}" width="{width:.1f}" height="{TIP_H}" rx="6" '
            f'stroke="{color}"/><text x="{bx + 7:.1f}" y="{by + 15:.1f}" class="t1">{html.escape(line1)}</text>'
            f'<text x="{bx + 7:.1f}" y="{by + 29:.1f}" class="t2">{html.escape(line2)}</text></g></g>')


def _badge(rarity: str) -> str:
    color = COLORS.get(rarity, COLORS[""])
    return f'<span class="badge" style="background:{color}">{html.escape(rarity or "?")}</span>'


def _stats(group: Group) -> str:
    prices = group.prices
    last = group.sales[-1] if group.sales else None
    tz = _tz()
    rows = [
        ("Ventes connues", str(len(prices))),
        ("Prix retenu par le bot", _retained(group)),
        ("Moyenne", _money(statistics.fmean(prices)) if prices else "—"),
        ("Médiane", _money(statistics.median(prices)) if prices else "—"),
        ("Plus bas", _money(min(prices)) if prices else "—"),
        ("Plus haut", _money(max(prices)) if prices else "—"),
        ("Dernière vente", f"{_money(last.price)} le {last.at.astimezone(tz).strftime('%d/%m à %H:%M')}"
         if last else "—"),
        ("Moyenne du site", _money(group.site_average)),
    ]
    if group.retained and group.rarity != SHINY:
        rows.append(("Prix auto du bot", f"achat ≤ {_money(math.floor(group.retained * AUTO_BUY_RATIO))} · "
                                         f"vente {_money(math.floor(group.retained * AUTO_SELL_RATIO))}"))
    if group.outliers:
        rows.append(("Ventes écartées", f"{len(group.outliers)} (aberrantes ou d'avant un changement de valeur)"))
    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return f'<section class="card"><h2>{_badge(group.rarity)} Rareté {html.escape(group.rarity or "inconnue")}' \
           f'</h2><table class="stats">{body}</table></section>'


def _retained(group: Group) -> str:
    if group.retained is not None:
        if group.source == "marche":
            return _money(group.retained) + (" (nouvelle valeur)" if group.clean and group.clean.trend else "")
        return f"{_money(group.retained)} (moyenne du site{f', {group.count} ventes' if group.count else ''})"
    if group.low:  # une vente aberrante pèserait autant que les autres
        return (f"— ({group.count if group.count is not None else 'trop peu de'} vente(s), pas assez pour s'y "
                "fier) : défaussée si sa rareté est dans raretes_defausse, sinon vendue seulement en dernier "
                f"recours, au plus bas ({_money(group.low)})")
    return "— (aucune vente connue)"


def _last_sales(groups: list[Group]) -> str:
    rows = sorted(((s, g, i) for g in groups for i, s in enumerate(g.sales)), key=lambda r: r[0].at, reverse=True)
    if not rows:
        return ""
    tz = _tz()
    body = "".join(
        f"<tr{OUT if i in g.outliers else ''}><td>{s.at.astimezone(tz).strftime('%d/%m %H:%M')}</td>"
        f"<td>{_badge(g.rarity)}</td><td class='num'>{_money(s.price)}</td>"
        f"<td class='num'>{_gap(s.price, g)}</td></tr>"
        for s, g, i in rows[:LAST_SALES_LISTED])
    more = f'<p class="muted">… et {len(rows) - LAST_SALES_LISTED} vente(s) plus ancienne(s).</p>' \
        if len(rows) > LAST_SALES_LISTED else ""
    return ('<section class="card"><h2>Dernières ventes</h2><table class="sales"><thead><tr><th>Date</th>'
            '<th>Rareté</th><th class="num">Prix</th><th class="num">vs retenu</th></tr></thead>'
            f"<tbody>{body}</tbody></table>{more}"
            '<p class="muted">Lignes grisées : ventes écartées par le bot pour son prix.</p></section>')


def _gap(price: float, group: Group) -> str:
    if not group.retained:
        return ""
    gap = (price / group.retained - 1) * 100
    return f"{gap:+.0f} %"


def _auctions(details: Details, base_url: str) -> str:
    if not details.auctions:
        return ""
    tz = _tz()
    items = []
    for auction in sorted(details.auctions, key=lambda a: a.get("end_at") or ""):
        end = parse_time(auction.get("end_at"))
        current = auction.get("current_bid")
        price = f"mise actuelle {_money(current)}" if current else f"départ {_money(auction.get('base_amount'))}"
        rarity = entry_rarity(auction)
        if rarity == "L" and is_shiny(auction):
            rarity = SHINY
        url = f"{base_url.rstrip('/')}/marketplace/{urllib.parse.quote(str(auction.get('id')))}"
        when = end.astimezone(tz).strftime("%d/%m à %H:%M") if end else "?"
        items.append(f'<li>{_badge(rarity)} <a href="{html.escape(url)}">{html.escape(price)}</a>'
                     f' · fin le {when}</li>')
    return f'<section class="card"><h2>Enchères en cours</h2><ul class="auctions">{"".join(items)}</ul></section>'


CSS = """
:root{--bg:#f6f7f9;--fg:#1f2430;--muted:#6b7280;--card:#fff;--line:#e5e7eb;--grid:#eceef2}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e7e9ee;--muted:#9aa1ad;--card:#1b1e26;
--line:#2a2f3a;--grid:#262a33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.4em;margin:.2em 0}h2{font-size:1.05em;margin:0 0 .6em;display:flex;gap:.5em;align-items:center}
a{color:#2563eb}.muted{color:var(--muted);font-size:.85em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
.grid-2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:0 12px}
.chart{width:100%;height:auto;display:block}.chart-narrow{display:none}
@media (max-width:600px){.chart-wide{display:none}.chart-narrow{display:block}}.chart .grid{stroke:var(--grid)}
.chart .axis{fill:var(--muted);font-size:11px}.chart .band{opacity:.16}
.chart .dot{opacity:.38}.chart .dot.big{opacity:.75}.chart .out{stroke-width:1.5;opacity:.75}
.chart .trend{fill:none;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.chart .clean{stroke-width:1.5;stroke-dasharray:6 4;opacity:.9}
.pt{outline:none;cursor:pointer}.pt .hit{fill:transparent}.pt .ring{fill:none;stroke-width:2.5}
.pt .ring,.pt .tip{visibility:hidden}.pt .tip rect{fill:var(--card);stroke-width:1.5}
.pt .t1{fill:var(--fg);font-size:11px;font-weight:600}.pt .t2{fill:var(--muted);font-size:11px}
.pt:focus .ring,.pt:focus .tip,.pt.on .ring,.pt.on .tip{visibility:visible}
@media (hover:hover){.pt:hover .ring,.pt:hover .tip{visibility:visible}}
.growth{display:flex;flex-wrap:wrap;gap:6px;margin:.2em 0 .5em}.growth span{padding:2px 8px;border-radius:999px;
font-size:.85em;font-weight:600;font-variant-numeric:tabular-nums}.growth small{font-weight:400;opacity:.8}
.growth .up{background:#16a34a22;color:#15803d}.growth .down{background:#dc262622;color:#b91c1c}
.growth .flat{background:#6b728022;color:var(--muted)}
@media (prefers-color-scheme:dark){.growth .up{color:#4ade80}.growth .down{color:#f87171}}
h3{font-size:.95em;margin:.8em 0 .2em;display:flex;gap:.5em;align-items:center}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:.85em;color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{padding:5px 4px;border-bottom:1px solid var(--line);text-align:left}
.stats th{font-weight:500;color:var(--muted);width:48%}.num{text-align:right;font-variant-numeric:tabular-nums}
.sales tr.out td{opacity:.45}
.badge{display:inline-block;min-width:2.2em;padding:1px 6px;border-radius:6px;color:#fff;font-size:.78em;
font-weight:700;text-align:center}
.auctions{margin:0;padding-left:1.1em}.auctions li{margin:.3em 0}
.info p{margin:.4em 0}.info details{margin:.4em 0}.info summary{cursor:pointer;list-style:none}
.info summary::-webkit-details-marker{display:none}.info details[open] summary{display:none}
.more{color:#2563eb;white-space:nowrap}.chips{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:.9em}

"""


def _card_info(card: dict) -> str:
    """Carte telle que la base du site la décrit : rareté, ATK/DEF, vues, description."""
    chips = []
    if card.get("rarity"):
        chips.append(f"{_badge(str(card['rarity']))} rareté de la carte")
    for label, key in (("ATK", "atk"), ("DEF", "def"), ("Vues", "pageviews"), ("Q-score", "q_score")):
        if isinstance(card.get(key), (int, float)) and not isinstance(card.get(key), bool):
            value = _money(card[key]) if key != "q_score" else f"{card[key]:g}"
            chips.append(f"<span><b>{label}</b> {value}</span>")
    summary = str(card.get("summary") or "").strip()
    text = ""
    if len(summary) > SUMMARY_SHOWN:  # début seulement, la suite se déplie (sans script)
        cut = summary.rfind(" ", 0, SUMMARY_SHOWN)
        text = (f"<details><summary>{html.escape(summary[:cut if cut > 0 else SUMMARY_SHOWN])}… "
                f"<span class=\"more\">lire la suite</span></summary><p>{html.escape(summary)}</p></details>")
    elif summary:
        text = f"<p>{html.escape(summary)}</p>"
    category = f'<p class="muted">{html.escape(str(card["category"]))}</p>' if card.get("category") else ""
    if not (chips or text):
        return ""
    return f'<section class="card info"><div class="chips">{"".join(chips)}</div>{category}{text}</section>'


def _charts(groups: list[Group], now: datetime) -> str:
    """Un graphique par rareté, chacun à son échelle (une L à 5 000 écraserait une SR à
    900), en deux tailles : large sur ordinateur, étroit sur téléphone."""
    shown = [g for g in groups if g.sales]
    if not shown:
        return '<p class="muted">Aucune vente détaillée à afficher.</p>'
    parts = []
    for group in shown:
        title = f'<h3>{_badge(group.rarity)} {len(group.sales)} vente(s)</h3>' if len(shown) > 1 else ""
        parts.append(f"{title}{_growth_chips(group, now)}{_chart([group], now, 'wide')}"
                     f"{_chart([group], now, 'narrow')}")
    return "".join(parts)


# Toucher le graphique n'importe où : la vente la plus proche (dans le temps d'abord) montre
# sa bulle. Sans script (aperçu qui les bloque), toucher un point la montre aussi (:focus).
SCRIPT = """document.querySelectorAll("svg.chart").forEach(function(svg){svg.addEventListener("click",function(e){
var m=svg.getScreenCTM();if(!m)return;var p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;
p=p.matrixTransform(m.inverse());var best=null,dist=1e18;svg.querySelectorAll(".pt").forEach(function(g){
var dx=g.dataset.x-p.x,dy=(g.dataset.y-p.y)*0.35,d=dx*dx+dy*dy;if(d<dist){dist=d;best=g}});
document.querySelectorAll(".pt.on").forEach(function(g){g.classList.remove("on")});
var f=document.activeElement;if(f&&f!==best&&f.blur)f.blur();
if(best){best.classList.add("on");best.parentNode.appendChild(best)}})});"""


def render(details: Details, base_url: str) -> str:
    groups = details.groups
    title = html.escape(details.title)
    tz = _tz()
    shown = [g for g in groups if g.sales]
    legend = ""
    if len(shown) == 1:  # plusieurs raretés : compte au-dessus de chaque graphique
        legend = f"<span>{_badge(shown[0].rarity)} {len(shown[0].sales)} vente(s)</span>"
    if shown:
        legend += ("<span>━ tendance (prix médian des 15 ventes autour) · zone claire : moitié centrale des "
                   "ventes · ● vente · ○ écartée par le bot · ┄ prix retenu par le bot · touchez le graphique "
                   "pour voir une vente · évolution calculée sur la tendance</span>")
    wiki = details.card.get("wikipedia_url") or \
        f"https://fr.wikipedia.org/wiki/{urllib.parse.quote(details.title.replace(' ', '_'))}"
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} · Marché</title><style>{CSS}</style></head><body><main>"
        f"<h1>{title}</h1>"
        f'<p class="muted">Fiche du Marché du {details.at.astimezone(tz).strftime("%d/%m/%Y à %H:%M")} · '
        f'lue avec la {html.escape(details.source)} · <a href="{html.escape(wiki)}">Wikipédia</a></p>'
        f"{_card_info(details.card)}"
        f'<section class="card"><h2>Ventes</h2>{_charts(groups, details.at)}<div class="legend">{legend}</div>'
        "</section>"
        f'<div class="grid-2">{"".join(_stats(g) for g in groups)}</div>'
        f"{_auctions(details, base_url)}{_last_sales(groups)}"
        '<p class="muted">Prix retenu par le bot : ventes des 30 derniers jours (50 au plus), sans les ventes '
        "aberrantes ; si les dernières ventes changent nettement de niveau, c'est leur médiane.</p>"
        f"</main><script>{SCRIPT}</script></body></html>")


def caption(details: Details) -> str:
    parts = []
    for group in details.groups:
        prices = group.prices
        if not prices:
            continue
        clean = (f", retenu {_money(group.retained)}" if group.retained else
                 ", trop peu de ventes pour s'y fier" if group.low else "")
        parts.append(f"{group.rarity or '?'} : {len(prices)} vente(s), moyenne {_money(statistics.fmean(prices))}, "
                     f"de {_money(min(prices))} à {_money(max(prices))}{clean}")
        evolution = growth(group, details.at)
        if evolution:
            parts.append("   " + " · ".join(f"{'📈' if pct >= 0 else '📉'} {pct:+.0f} % {label}"
                                            for label, pct in evolution))
    return f"📊 {details.title}\n" + ("\n".join(parts) or "Aucune vente détaillée.")


def filename(details: Details) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_name(details.title)).strip("-")[:60] or "carte"
    return f"fiche-{slug}.html"


# ─────────────────────────── Commande ───────────────────────────


def parse(args: str) -> tuple[str, str | None]:
    """(carte, rareté) de « /details <carte> [rareté] »."""
    if not args.strip():
        raise ListError(USAGE)
    if UUID.search(args):
        rarity = next((w.upper() for w in args.split() if w.upper() in (*RARITIES, SHINY)), None)
        return args.strip(), rarity
    parsed = parse_card_command(args, 0, USAGE, ("rarete",), optional_prices=True)
    return parsed.name, parsed.rarity or None


def send_details(config: Config, args: str, reply) -> None:
    """Prépare la fiche et l'envoie dans la conversation (reply : TelegramBot)."""
    try:
        query, rarity = parse(args)
        details = collect(config, query, rarity)
    except (ListError, DetailsError) as exc:
        reply.send(f"❌ {exc}")
        return
    except Exception:
        log.exception("Fiche : erreur.")
        reply.send("❌ Fiche impossible : erreur inattendue (voir le journal).")
        return
    if not any(g.sales or g.site_average for g in details.groups):
        reply.send(f"📊 « {details.title} »{f' {rarity}' if rarity else ''} : aucune vente connue sur le Marché.")
        return
    if not reply.send_document(filename(details), render(details, config.base_url).encode("utf-8"),
                               caption(details)):
        reply.send("❌ Fiche prête mais Telegram ne l'a pas acceptée ; réessayez.")
