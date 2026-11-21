"""Polite recipe scraper.

Strategy: almost every major recipe site embeds a schema.org ``Recipe`` object as
JSON-LD for Google rich results. We read that structured data instead of the page
prose, which skips the life story automatically and gives us ratings, times and
ingredients in a consistent shape.

* Respects robots.txt and waits between requests to the same host.
* Keeps a recipe only if its aggregate rating >= ``min_rating`` (default 4.0 / 5)
  and it has at least ``min_reviews`` ratings.
* Can crawl: give it a category/listing page or a sitemap.xml and it follows
  recipe-looking links on the same domain.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "SimpleRecipesBot/1.0 (+personal recipe organizer; respects robots.txt)"
DEFAULT_MIN_RATING = 4.0

Fetcher = Callable[[str], str]


# ---------------------------------------------------------------- fetching
@dataclass
class PoliteFetcher:
    delay_seconds: float = 1.5
    timeout: float = 15.0
    respect_robots: bool = True
    session: requests.Session = field(default_factory=requests.Session)
    _robots: dict = field(default_factory=dict)
    _last_hit: dict = field(default_factory=dict)

    def __post_init__(self):
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"})

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.session.get(base + "/robots.txt", timeout=self.timeout)
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)

    def __call__(self, url: str) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        wait = self.delay_seconds - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, timeout=self.timeout)
        self._last_hit[host] = time.monotonic()
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------- parsing
def _load_json_loose(text: str):
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Common breakage: raw newlines/tabs inside strings, trailing commas.
        cleaned = re.sub(r"[\r\n\t]+", " ", text)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _is_recipe(node) -> bool:
    if not isinstance(node, dict):
        return False
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower().endswith("recipe") for x in types if x)


def _find_recipes(node) -> Iterable[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _find_recipes(n)
    elif isinstance(node, dict):
        if _is_recipe(node):
            yield node
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
            if key in node:
                yield from _find_recipes(node[key])


def extract_recipe_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        data = _load_json_loose(tag.string or tag.get_text() or "")
        if data is None:
            continue
        for recipe in _find_recipes(data):
            if recipe.get("recipeIngredient") or recipe.get("ingredients"):
                return recipe
    return None


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _image_url(v) -> str | None:
    v = _first(v)
    if isinstance(v, dict):
        return v.get("url") or v.get("contentUrl")
    return v if isinstance(v, str) else None


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_rating(recipe: dict) -> tuple[float | None, int]:
    """Return (rating normalized to a 5-point scale, rating count)."""
    agg = recipe.get("aggregateRating")
    agg = _first(agg) if isinstance(agg, list) else agg
    if not isinstance(agg, dict):
        return None, 0
    value = _num(agg.get("ratingValue"))
    best = _num(agg.get("bestRating")) or 5.0
    count = _num(agg.get("ratingCount")) or _num(agg.get("reviewCount")) or 0
    if value is None:
        return None, int(count)
    if best != 5.0 and best > 0:
        value = 5 * value / best  # e.g. 9/10 -> 4.5, 88/100 -> 4.4
    return round(max(0.0, min(5.0, value)), 2), int(count)


def passes_rating(rating: float | None, count: int, min_rating: float = DEFAULT_MIN_RATING,
                  min_reviews: int = 0, allow_unrated: bool = False) -> bool:
    if rating is None:
        return allow_unrated
    return rating >= min_rating and count >= min_reviews


# ---------------------------------------------------------------- discovery
RECIPE_URL_HINT = re.compile(r"/recipes?/|/recipe-|-recipe\b|/\d{4,}/[\w-]+", re.I)
SKIP_URL_HINT = re.compile(r"/(?:tag|category|author|search|page|login|account|shop|video|collections?)/|[?&]page=|\.(?:jpe?g|png|gif|pdf)$", re.I)


def discover_links(html: str, base_url: str, same_domain: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base_url).netloc
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"]))[0]
        if not url.startswith("http"):
            continue
        if same_domain and urlparse(url).netloc != host:
            continue
        if url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        if RECIPE_URL_HINT.search(urlparse(url).path) and not SKIP_URL_HINT.search(url):
            seen.add(url)
            out.append(url)
    return out


def parse_sitemap(xml: str) -> list[str]:
    soup = BeautifulSoup(xml, "xml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")]


# ---------------------------------------------------------------- crawl
@dataclass
class ScrapeResult:
    url: str
    status: str  # saved | low_rating | no_recipe | error | blocked | duplicate
    detail: str = ""
    recipe: dict | None = None


def scrape_url(url: str, fetch: Fetcher, min_rating=DEFAULT_MIN_RATING, min_reviews=0,
               allow_unrated=False) -> tuple[ScrapeResult, str | None]:
    """Scrape one URL. Returns (result, html) — html is reused for link discovery."""
    try:
        html = fetch(url)
    except PermissionError as e:
        return ScrapeResult(url, "blocked", str(e)), None
    except Exception as e:  # network, HTTP errors
        return ScrapeResult(url, "error", f"{type(e).__name__}: {e}"), None
    raw = extract_recipe_jsonld(html)
    if raw is None:
        return ScrapeResult(url, "no_recipe", "no schema.org Recipe found"), html
    rating, count = parse_rating(raw)
    if not passes_rating(rating, count, min_rating, min_reviews, allow_unrated):
        return ScrapeResult(url, "low_rating", f"rating={rating} reviews={count}"), html
    return ScrapeResult(url, "saved", f"rating={rating} reviews={count}", raw), html


def crawl(start_urls: list[str], fetch: Fetcher, *, min_rating=DEFAULT_MIN_RATING, min_reviews=0,
          max_recipes=25, max_pages=60, follow_links=True, allow_unrated=False) -> list[ScrapeResult]:
    queue: list[str] = []
    for u in start_urls:
        u = u.strip()
        if not u:
            continue
        if u.endswith(".xml"):
            try:
                queue.extend(parse_sitemap(fetch(u)))
            except Exception as e:
                log.warning("sitemap %s failed: %s", u, e)
        else:
            queue.append(u)

    results: list[ScrapeResult] = []
    visited: set[str] = set()
    saved = 0
    while queue and saved < max_recipes and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        result, html = scrape_url(url, fetch, min_rating, min_reviews, allow_unrated)
        results.append(result)
        if result.status == "saved":
            saved += 1
        if follow_links and html and result.status in ("no_recipe", "saved", "low_rating"):
            # listing pages (no recipe) are the main source of new links
            for link in discover_links(html, url):
                if link not in visited and link not in queue:
                    queue.append(link)
    return results
