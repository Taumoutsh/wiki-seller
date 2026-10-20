"""Fiche d'une carte du Marché (/details) : graphique des ventes, dernières ventes et
prix (moyen, médian, plus bas, plus haut, prix retenu par le bot), par rareté, avec les
enchères en cours de la carte.

La fiche est une page HTML autonome (graphique en SVG, sans script ni ressource externe)
envoyée en document Telegram : elle s'ouvre sur téléphone comme sur ordinateur.

La carte se donne par son nom (titre exact), par un lien ou un identifiant d'enchère, ou
par l'identifiant de la carte. Les ventes se lisent avec la session Marché, sinon avec
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
OUT = " class='out'"  # vente écartée par le bot
# Graphique dessiné en deux tailles (sans script) : large sur ordinateur, étroit sur téléphone.
CHART_SIZES = {"wide": (720, 300, 56), "narrow": (360, 260, 44)}  # largeur, hauteur, marge gauche
PAD_R, PAD_T, PAD_B = 12, 14, 30
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
    clean: market.MarketPrice | None = None
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


def build_groups(sales: list[Sale], summary: dict | None, rarity: str | None, now: datetime) -> list[Group]:
    """Ventes par rareté (celle demandée seulement, si rarity), avec le prix retenu par le
    bot (market.clean_price) et les ventes qu'il écarte."""
    by_rarity: dict[str, list[Sale]] = {}
    for sale in sales:
        by_rarity.setdefault(sale.rarity, []).append(sale)
    groups = []
    for key in sorted(by_rarity, key=lambda r: ORDER.index(r) if r in ORDER else len(ORDER)):
        if rarity and key != rarity:
            continue
        group = Group(key, by_rarity[key])
        group.clean = market.clean_price([market.Sale(s.at, s.price) for s in group.sales], now)
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
    return groups


def _outliers(sales: list[Sale], now: datetime) -> set[int]:
    """Ventes que le calcul du bot écarte (même règle que market.clean_price)."""
    recent = [i for i, s in enumerate(sales) if s.at >= now - market.WINDOW][-market.MAX_SALES:]
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


def resolve_card(site: WikiMasters, config: Config, query: str) -> tuple[str | None, str, list[dict]]:
    """(identifiant de la carte, titre, enchères en cours de la carte) ; identifiant None
    si la carte est introuvable."""
    found = UUID.search(query)
    if found:  # lien ou identifiant d'enchère, sinon identifiant de carte
        auction = site.read_auction(found.group(0))
        if auction:
            card = auction.get("card") if isinstance(auction.get("card"), dict) else {}
            card_id = auction.get("card_id") or card.get("id")
            title = card.get("wikipedia_title") or ""
            auctions = [a for a in site.search_auctions(title) if _same_card(a, card_id, title)] if title else []
            return (str(card_id) if card_id else None), title, auctions or [auction]
        return found.group(0), "", []
    key = normalize_name(query)
    auctions = [a for a in site.search_auctions(query) if normalize_name(_title(a)) == key]
    for auction in auctions:
        card_id = _card_id(auction)
        if card_id:
            return card_id, _title(auction), [a for a in auctions if _card_id(a) in (None, card_id)]
    for entry in collection.cached_entries():  # collections lues récemment (tous les comptes)
        if normalize_name(_title(entry)) == key and _card_id(entry):
            return _card_id(entry), _title(entry), auctions
    card = _lookup_card(site, query)
    if card:
        return card[0], card[1], auctions
    return None, query, auctions


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


def _lookup_card(site: WikiMasters, name: str) -> tuple[str, str] | None:
    """Carte cherchée par son titre dans la base du site (lecture seule) ; None si la base
    ne répond pas ou ne la connaît pas."""
    if any(c in name for c in "*%,()"):
        return None
    store = site.client.store
    try:
        store.ensure_fresh()
        token = (store.session or {}).get("access_token")
        query = urllib.parse.urlencode({"select": "id,wikipedia_title", "wikipedia_title": f"ilike.{name.strip()}",
                                        "limit": "10"})
        request = urllib.request.Request(f"{store.supabase_url()}/rest/v1/cards?{query}", headers={
            "apikey": store.anon_key(), "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
            "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            rows = json.loads(response.read() or b"[]")
    except Exception as exc:
        log.info("Fiche : recherche de « %s » dans la base du site impossible (%s).", name, exc)
        return None
    key = normalize_name(name)
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("id") and normalize_name(str(row.get("wikipedia_title") or "")) == key:
            return str(row["id"]), str(row["wikipedia_title"])
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
                card_id, title, auctions = resolve_card(site, config, query)
                if card_id is None:
                    raise DetailsError(
                        f"« {query} » introuvable : aucune enchère en cours de cette carte et elle n'est pas dans "
                        "vos collections. Donnez son titre exact, ou le lien d'une de ses enchères.")
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
        groups = build_groups(parse_market_sales(data), summary if isinstance(summary, dict) else None, rarity, now)
        return Details(card_id, title, groups, auctions, source, now)
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


def _chart(groups: list[Group], now: datetime, size: str) -> str:
    sales = [s for g in groups for s in g.sales]
    if not sales:
        return '<p class="muted">Aucune vente détaillée à afficher.</p>'
    chart_w, chart_h, pad_l = CHART_SIZES[size]
    # Échelle d'après les ventes retenues de chaque rareté : une vente écartée hors échelle est posée en haut.
    kept = [p for g in groups for i, p in enumerate(g.prices) if i not in g.outliers] or [s.price for s in sales]
    top = max([*kept, *(g.clean.price for g in groups if g.clean)])
    step = _nice_step(top)
    y_max = step * math.ceil(top * 1.05 / step)
    start, end = min(s.at for s in sales), max(max(s.at for s in sales), now)
    if end - start < timedelta(hours=1):
        start = end - timedelta(hours=1)
    plot_w, plot_h = chart_w - pad_l - PAD_R, chart_h - PAD_T - PAD_B
    tz = _tz()

    def x(at: datetime) -> float:
        return pad_l + plot_w * (at - start).total_seconds() / (end - start).total_seconds()

    def y(price: float) -> float:
        return PAD_T + plot_h * (1 - min(price, y_max) / y_max)

    out = [f'<svg viewBox="0 0 {chart_w} {chart_h}" role="img" aria-label="Ventes de la carte" '
           f'class="chart chart-{size}">']
    value = 0.0
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
    for group in groups:
        color = COLORS.get(group.rarity, COLORS[""])
        if group.clean:
            out.append(f'<line x1="{pad_l}" x2="{chart_w - PAD_R}" y1="{y(group.clean.price):.1f}" '
                       f'y2="{y(group.clean.price):.1f}" stroke="{color}" class="clean"/>')
        traced = [s for i, s in enumerate(group.sales) if i not in group.outliers]  # sans les ventes écartées
        if len(traced) > 1:
            points = " ".join(f"{x(s.at):.1f},{y(s.price):.1f}" for s in traced)
            out.append(f'<polyline points="{points}" stroke="{color}" class="trace"/>')
        for i, sale in enumerate(group.sales):
            when = sale.at.astimezone(tz).strftime("%d/%m %H:%M")
            tip = html.escape(f"{group.rarity or '?'} · {_money(sale.price)} · {when}"
                              + (" · écartée par le bot" if i in group.outliers else ""))
            cx, cy = x(sale.at), y(sale.price)
            if sale.price > y_max:  # hors échelle : triangle en haut
                out.append(f'<path d="M{cx:.1f},{cy - 1:.1f} l5,9 h-10 z" fill="{color}"><title>{tip}</title></path>')
            elif i in group.outliers:
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="none" stroke="{color}" '
                           f'stroke-width="2"><title>{tip}</title></circle>')
            else:
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}"><title>{tip}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def _badge(rarity: str) -> str:
    color = COLORS.get(rarity, COLORS[""])
    return f'<span class="badge" style="background:{color}">{html.escape(rarity or "?")}</span>'


def _stats(group: Group) -> str:
    prices = group.prices
    last = group.sales[-1] if group.sales else None
    tz = _tz()
    rows = [
        ("Ventes connues", str(len(prices))),
        ("Prix retenu par le bot", (_money(group.clean.price) + (" (nouvelle valeur)" if group.clean.trend else "")
                                    if group.clean else "— (moins de 3 ventes en 30 j)")),
        ("Moyenne", _money(statistics.fmean(prices)) if prices else "—"),
        ("Médiane", _money(statistics.median(prices)) if prices else "—"),
        ("Plus bas", _money(min(prices)) if prices else "—"),
        ("Plus haut", _money(max(prices)) if prices else "—"),
        ("Dernière vente", f"{_money(last.price)} le {last.at.astimezone(tz).strftime('%d/%m à %H:%M')}"
         if last else "—"),
        ("Moyenne du site", _money(group.site_average)),
    ]
    if group.clean and group.rarity != SHINY:
        rows.append(("Prix auto du bot", f"achat ≤ {_money(math.floor(group.clean.price * AUTO_BUY_RATIO))} · "
                                         f"vente {_money(math.floor(group.clean.price * AUTO_SELL_RATIO))}"))
    if group.outliers:
        rows.append(("Ventes écartées", f"{len(group.outliers)} (aberrantes ou d'avant un changement de valeur)"))
    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return f'<section class="card"><h2>{_badge(group.rarity)} Rareté {html.escape(group.rarity or "inconnue")}' \
           f'</h2><table class="stats">{body}</table></section>'


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
    if not group.clean:
        return ""
    gap = (price / group.clean.price - 1) * 100
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
.chart .axis{fill:var(--muted);font-size:11px}.chart .trace{fill:none;stroke-width:1.5;opacity:.45}
.chart .clean{stroke-width:1.5;stroke-dasharray:6 4;opacity:.9}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:.85em;color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{padding:5px 4px;border-bottom:1px solid var(--line);text-align:left}
.stats th{font-weight:500;color:var(--muted);width:48%}.num{text-align:right;font-variant-numeric:tabular-nums}
.sales tr.out td{opacity:.45}
.badge{display:inline-block;min-width:2.2em;padding:1px 6px;border-radius:6px;color:#fff;font-size:.78em;
font-weight:700;text-align:center}
.auctions{margin:0;padding-left:1.1em}.auctions li{margin:.3em 0}
"""


def render(details: Details, base_url: str) -> str:
    groups = details.groups
    title = html.escape(details.title)
    tz = _tz()
    legend = "".join(f"<span>{_badge(g.rarity)} {len(g.sales)} vente(s)</span>" for g in groups if g.sales)
    legend += ("<span>● vente · ○ écartée par le bot · ▲ hors échelle · ┄ prix retenu par le bot</span>"
               if legend else "")
    wiki = f"https://fr.wikipedia.org/wiki/{urllib.parse.quote(details.title.replace(' ', '_'))}"
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} · Marché</title><style>{CSS}</style></head><body><main>"
        f"<h1>{title}</h1>"
        f'<p class="muted">Fiche du Marché du {details.at.astimezone(tz).strftime("%d/%m/%Y à %H:%M")} · '
        f'lue avec la {html.escape(details.source)} · <a href="{html.escape(wiki)}">Wikipédia</a></p>'
        f'<section class="card"><h2>Ventes</h2>{_chart(groups, details.at, "wide")}'
        f'{_chart(groups, details.at, "narrow")}<div class="legend">{legend}</div>'
        "</section>"
        f'<div class="grid-2">{"".join(_stats(g) for g in groups)}</div>'
        f"{_auctions(details, base_url)}{_last_sales(groups)}"
        '<p class="muted">Prix retenu par le bot : ventes des 30 derniers jours (50 au plus), sans les ventes '
        "aberrantes ; si les dernières ventes changent nettement de niveau, c'est leur médiane.</p>"
        "</main></body></html>")


def caption(details: Details) -> str:
    parts = []
    for group in details.groups:
        prices = group.prices
        if not prices:
            continue
        clean = f", retenu {_money(group.clean.price)}" if group.clean else ""
        parts.append(f"{group.rarity or '?'} : {len(prices)} vente(s), moyenne {_money(statistics.fmean(prices))}, "
                     f"de {_money(min(prices))} à {_money(max(prices))}{clean}")
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
