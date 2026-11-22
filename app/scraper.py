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
* Multi-site: ``crawl_sites`` takes a list of many sites (see ``recipe_sites.txt``),
  gives each its own recipe/page budget and takes turns between them.
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


# Blog posts often live at a bare slug (site.com/one-pot-chili-mac/) with no
# "recipe" in the path. Multi-site crawls accept those too (3+ hyphenated words).
LOOSE_SLUG_HINT = re.compile(r"^/[a-z0-9]+(?:-[a-z0-9]+){2,}/?$", re.I)


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def discover_links(html: str, base_url: str, same_domain: bool = True, loose: bool = False) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    host = _host(base_url)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"]))[0]
        if not url.startswith("http"):
            continue
        if same_domain and _host(url) != host:
            continue
        if url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        path = urlparse(url).path
        looks_like_recipe = RECIPE_URL_HINT.search(path) or (loose and LOOSE_SLUG_HINT.match(path))
        if looks_like_recipe and not SKIP_URL_HINT.search(url):
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


def _is_sitemap(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".xml")


class SiteCrawler:
    """Crawl state for one set of start URLs. ``step()`` handles one page, so
    several crawlers can be interleaved (see ``crawl_sites``)."""

    def __init__(self, start_urls: list[str], fetch: Fetcher, *, min_rating=DEFAULT_MIN_RATING,
                 min_reviews=0, max_recipes=25, max_pages=60, follow_links=True,
                 allow_unrated=False, loose_links=False):
        self.queue = [u.strip() for u in start_urls if u and u.strip()]
        self.fetch = fetch
        self.min_rating, self.min_reviews, self.allow_unrated = min_rating, min_reviews, allow_unrated
        self.max_recipes, self.max_pages = max_recipes, max_pages
        self.follow_links, self.loose_links = follow_links, loose_links
        self.visited: set[str] = set()
        self.saved = 0
        self.results: list[ScrapeResult] = []

    @property
    def done(self) -> bool:
        return not self.queue or self.saved >= self.max_recipes or len(self.visited) >= self.max_pages

    def step(self) -> ScrapeResult | None:
        """Process the next queued URL. Returns its result (None for a sitemap)."""
        while self.queue and self.queue[0] in self.visited:
            self.queue.pop(0)
        if self.done:
            return None
        url = self.queue.pop(0)
        self.visited.add(url)
        if _is_sitemap(url):  # sitemap or sitemap index: expand, don't score
            try:
                for loc in parse_sitemap(self.fetch(url)):
                    if loc not in self.visited and loc not in self.queue:
                        self.queue.append(loc)
            except Exception as e:
                log.warning("sitemap %s failed: %s", url, e)
            return None
        result, html = scrape_url(url, self.fetch, self.min_rating, self.min_reviews, self.allow_unrated)
        self.results.append(result)
        if result.status == "saved":
            self.saved += 1
        if self.follow_links and html and result.status in ("no_recipe", "saved", "low_rating"):
            # listing pages (no recipe) are the main source of new links
            for link in discover_links(html, url, loose=self.loose_links):
                if link not in self.visited and link not in self.queue:
                    self.queue.append(link)
        return result


def crawl(start_urls: list[str], fetch: Fetcher, *, min_rating=DEFAULT_MIN_RATING, min_reviews=0,
          max_recipes=25, max_pages=60, follow_links=True, allow_unrated=False) -> list[ScrapeResult]:
    c = SiteCrawler(start_urls, fetch, min_rating=min_rating, min_reviews=min_reviews,
                    max_recipes=max_recipes, max_pages=max_pages, follow_links=follow_links,
                    allow_unrated=allow_unrated)
    while not c.done:
        c.step()
    return c.results


# ---------------------------------------------------------------- multi-site
def read_sources(path: str) -> list[str]:
    """Read a sources file: one URL per line; ``#`` starts a comment (whole-line or
    after whitespace). A bare domain such as ``allrecipes.com`` means its homepage."""
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
            if not line:
                continue
            if "://" not in line:
                line = "https://" + line.lstrip("/")
                if urlparse(line).path == "":
                    line += "/"
            urls.append(line)
    return urls


def group_by_site(urls: list[str]) -> dict[str, list[str]]:
    """Group start URLs by site (host without ``www.``), keeping file order."""
    sites: dict[str, list[str]] = {}
    for u in urls:
        sites.setdefault(_host(u), []).append(u)
    return sites


def crawl_sites(start_urls: list[str], fetch: Fetcher, *, per_site=10, max_pages_per_site=40,
                max_total=0, min_rating=DEFAULT_MIN_RATING, min_reviews=0, follow_links=True,
                allow_unrated=False, on_result: Callable[[str, ScrapeResult], None] | None = None,
                ) -> dict[str, list[ScrapeResult]]:
    """Crawl many sites with a separate recipe/page budget for each, taking turns
    between sites one page at a time. Taking turns spreads requests out, so the
    per-host delay rarely makes the crawl wait. ``max_total`` (0 = no limit) caps
    recipes saved across all sites. Returns {site: results}."""
    crawlers = {
        site: SiteCrawler(urls, fetch, min_rating=min_rating, min_reviews=min_reviews,
                          max_recipes=per_site, max_pages=max_pages_per_site,
                          follow_links=follow_links, allow_unrated=allow_unrated, loose_links=True)
        for site, urls in group_by_site(start_urls).items()
    }
    total = 0
    active = [s for s, c in crawlers.items() if not c.done]
    while active and not (max_total and total >= max_total):
        for site in list(active):
            if max_total and total >= max_total:
                break
            c = crawlers[site]
            result = c.step()
            if result is not None:
                total += result.status == "saved"
                if on_result:
                    on_result(site, result)
            if c.done:
                active.remove(site)
    return {site: c.results for site, c in crawlers.items()}
