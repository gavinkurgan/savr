"""Polite recipe scraper.

Strategy: almost every major recipe site embeds a schema.org ``Recipe`` object as
JSON-LD for Google rich results. We read that structured data instead of the page
prose, which skips the life story automatically and gives us ratings, times and
ingredients in a consistent shape.

* Respects robots.txt, waits between requests to the same host, and backs off
  (honouring Retry-After) when a site answers 429/503.
* Keeps a recipe only if its aggregate rating >= ``min_rating`` (default 4.0 / 5),
  it has at least ``min_reviews`` ratings, and optionally its review-weighted
  ``quality_score`` >= ``min_score``. Unrated recipes are reported as ``unrated``
  and can be allowed per site.
* Optional completeness checks (ingredient count, instructions, image).
* Crawls efficiently: sitemaps listed in robots.txt are used automatically,
  single-recipe URLs are fetched before listing pages, and URLs already in the
  library are skipped without fetching.
* Multi-site: ``crawl_sites`` takes a list of many sites (see ``recipe_sites.txt``),
  gives each its own recipe/page budget and takes turns between them. A site
  that keeps refusing requests (403/429) is given up on.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
import posixpath
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable
from urllib import robotparser
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .quality import quality_score

log = logging.getLogger(__name__)

USER_AGENT = "SimpleRecipesBot/1.0 (+personal recipe organizer; respects robots.txt)"
DEFAULT_MIN_RATING = 4.0

Fetcher = Callable[[str], str]


class HTTPStatusError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


# ---------------------------------------------------------------- fetching
def _retry_after(resp) -> float | None:
    value = (resp.headers or {}).get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


@dataclass
class PoliteFetcher:
    delay_seconds: float = 1.5
    timeout: float = 15.0
    respect_robots: bool = True
    retries: int = 2               # extra attempts after a 429/503
    backoff_seconds: float = 5.0   # first wait when the site sends no Retry-After (then x3)
    max_backoff: float = 60.0
    max_host_delay: float = 15.0
    session: requests.Session = field(default_factory=requests.Session)
    sleep: Callable[[float], None] = time.sleep
    _robots: dict = field(default_factory=dict)
    _last_hit: dict = field(default_factory=dict)
    _host_delay: dict = field(default_factory=dict)

    def __post_init__(self):
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"})

    def _robots_for(self, url: str) -> robotparser.RobotFileParser:
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
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return self._robots_for(url).can_fetch(USER_AGENT, url)

    def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs the site lists in robots.txt (absolute)."""
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        return [urljoin(base, s.strip()) for s in (self._robots_for(url).site_maps() or []) if s.strip()]

    def __call__(self, url: str) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        for attempt in range(self.retries + 1):
            delay = self._host_delay.get(host, self.delay_seconds)
            wait = delay - (time.monotonic() - self._last_hit.get(host, 0))
            if wait > 0:
                self.sleep(wait)
            resp = self.session.get(url, timeout=self.timeout)
            self._last_hit[host] = time.monotonic()
            if resp.status_code in (429, 503) and attempt < self.retries:
                pause = _retry_after(resp)
                pause = min(self.max_backoff, pause if pause is not None else self.backoff_seconds * 3 ** attempt)
                # the site asked us to slow down: do so for the rest of the run
                self._host_delay[host] = min(self.max_host_delay, max(delay * 2, 2.0))
                log.info("%s answered %s; waiting %.0fs", host, resp.status_code, pause)
                self.sleep(pause)
                continue
            break
        if resp.status_code >= 400:
            raise HTTPStatusError(resp.status_code, url)
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
                  min_reviews: int = 0, allow_unrated: bool = False, min_score: float | None = None) -> bool:
    if rating is None:
        return allow_unrated
    if min_score is not None and quality_score(rating, count) < min_score:
        return False
    return rating >= min_rating and count >= min_reviews


def completeness_problems(raw: dict, min_ingredients: int = 0, require_instructions: bool = False,
                          require_image: bool = False) -> list[str]:
    """What's missing for this to be a usable recipe (empty list = fine)."""
    problems = []
    ingredients = raw.get("recipeIngredient") or raw.get("ingredients") or []
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    if len([i for i in ingredients if str(i).strip()]) < min_ingredients:
        problems.append(f"only {len(ingredients)} ingredients")
    if require_instructions and not raw.get("recipeInstructions"):
        problems.append("no instructions")
    if require_image and not _image_url(raw.get("image")):
        problems.append("no image")
    return problems


# ---------------------------------------------------------------- discovery
RECIPE_URL_HINT = re.compile(r"/recipes?/|/recipe-|-recipe\b|/\d{4,}/[\w-]+", re.I)
SKIP_URL_HINT = re.compile(r"/(?:tag|category|author|search|page|login|account|shop|video|collections?)/|[?&]page=|\.(?:jpe?g|png|gif|pdf)$", re.I)
# Blog posts often live at a bare slug (site.com/one-pot-chili-mac/) with no
# "recipe" in the path. Multi-site crawls accept those too (3+ hyphenated words).
LOOSE_SLUG_HINT = re.compile(r"^/[a-z0-9]+(?:-[a-z0-9]+){2,}/?$", re.I)
# One recipe per page: /recipe/123/name/, /x-recipe, /recipe-x, /2024/01/name/
SINGLE_RECIPE_HINT = re.compile(r"/recipe/|/recipe-|-recipe(?:-\d+)?/?$|/\d{4}/\d{2}/[\w-]+/?$|/recipes/[\w-]+-\d{5,}/?$", re.I)
# Sitemaps that never list recipes
NON_RECIPE_SITEMAP = re.compile(
    r"(?:tag|category|categories|author|video|image|attachment|product|news|gallery|web-?stor(?:y|ies)|page|"
    r"local|topic|collection)s?[-_]?sitemap|sitemap[-_]?(?:tag|category|author|video|image|news|page|topic)", re.I)

# Queue priority: lower runs first.
P_RECIPE_SITEMAP, P_RECIPE, P_OTHER, P_LISTING = 0, 1, 2, 3


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: no fragment, no tracking params,
    no ``/./`` or ``//`` in the path, lowercase host."""
    url = urldefrag(url)[0]
    p = urlparse(url)
    path = p.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if path in (".", "//"):
        path = "/"
    if trailing and not path.endswith("/"):
        path += "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def url_priority(url: str) -> int | None:
    """How soon to fetch a URL (None = don't)."""
    path = urlparse(url).path
    low = path.lower()
    if low.endswith(".gz"):
        return None
    if _is_sitemap(url):
        if NON_RECIPE_SITEMAP.search(low):
            return None
        return P_RECIPE_SITEMAP if "recipe" in low else P_OTHER
    if SINGLE_RECIPE_HINT.search(path):
        return P_RECIPE
    if re.match(r"^/recipes?(?:/|$)", low) or "/recipes/" in low:
        return P_LISTING  # /recipes/, /recipes/17562/dinner/, /cooking/recipes/
    return P_OTHER


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def discover_links(html: str, base_url: str, same_domain: bool = True, loose: bool = False) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    host = _host(base_url)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = normalize_url(urljoin(base_url, a["href"]))
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
    status: str  # saved | low_rating | unrated | incomplete | no_recipe | error | blocked | known | gave_up
    detail: str = ""
    recipe: dict | None = None
    http_status: int | None = None


@dataclass
class Checks:
    """What a recipe must have to be kept."""
    min_rating: float = DEFAULT_MIN_RATING
    min_reviews: int = 0
    min_score: float | None = None
    allow_unrated: bool = False
    min_ingredients: int = 0
    require_instructions: bool = False
    require_image: bool = False


def scrape_url(url: str, fetch: Fetcher, min_rating=DEFAULT_MIN_RATING, min_reviews=0,
               allow_unrated=False, checks: Checks | None = None) -> tuple[ScrapeResult, str | None]:
    """Scrape one URL. Returns (result, html) — html is reused for link discovery."""
    ck = checks or Checks(min_rating=min_rating, min_reviews=min_reviews, allow_unrated=allow_unrated)
    try:
        html = fetch(url)
    except PermissionError as e:
        return ScrapeResult(url, "blocked", str(e)), None
    except HTTPStatusError as e:
        return ScrapeResult(url, "error", f"HTTP {e.status}", http_status=e.status), None
    except Exception as e:  # network errors
        return ScrapeResult(url, "error", f"{type(e).__name__}: {e}"), None
    raw = extract_recipe_jsonld(html)
    if raw is None:
        return ScrapeResult(url, "no_recipe", "no schema.org Recipe found"), html
    rating, count = parse_rating(raw)
    detail = f"rating={rating} reviews={count}"
    if rating is None and not ck.allow_unrated:
        return ScrapeResult(url, "unrated", detail), html
    if not passes_rating(rating, count, ck.min_rating, ck.min_reviews, ck.allow_unrated, ck.min_score):
        return ScrapeResult(url, "low_rating", detail + f" score={quality_score(rating, count)}"), html
    problems = completeness_problems(raw, ck.min_ingredients, ck.require_instructions, ck.require_image)
    if problems:
        return ScrapeResult(url, "incomplete", "; ".join(problems)), html
    return ScrapeResult(url, "saved", detail, raw), html


def _is_sitemap(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".xml")


class SiteCrawler:
    """Crawl state for one set of start URLs. ``step()`` handles one page, so
    several crawlers can be interleaved (see ``crawl_sites``).

    The queue is a priority queue: recipe sitemaps, then single-recipe pages,
    then other pages, then listing pages. Start URLs go first. URLs in ``known``
    (already in the library) are reported as ``known`` without being fetched and
    don't use the page budget. Sitemaps from robots.txt are added when
    ``use_sitemaps`` is on and the fetcher can list them."""

    GIVE_UP_AFTER = 6  # consecutive 403/429 answers

    def __init__(self, start_urls: list[str], fetch: Fetcher, *, min_rating=DEFAULT_MIN_RATING,
                 min_reviews=0, max_recipes=25, max_pages=60, follow_links=True,
                 allow_unrated=False, loose_links=False, known: set[str] | None = None,
                 use_sitemaps=False, checks: Checks | None = None):
        self.fetch = fetch
        self.checks = checks or Checks(min_rating=min_rating, min_reviews=min_reviews, allow_unrated=allow_unrated)
        self.max_recipes, self.max_pages = max_recipes, max_pages
        self.follow_links, self.loose_links = follow_links, loose_links
        self.known = {normalize_url(u) for u in (known or ())}
        self.visited: set[str] = set()
        self.pages = 0
        self.saved = 0
        self.refusals = 0
        self.gave_up: str | None = None
        self.results: list[ScrapeResult] = []
        self._heap: list = []
        self._queued: set[str] = set()
        self._seq = itertools.count()
        starts = [u.strip() for u in start_urls if u and u.strip()]
        for u in starts:
            self.push(u, -1)  # start URLs first, in file order
        if use_sitemaps and follow_links and hasattr(fetch, "sitemaps") and starts:
            try:
                for sm in fetch.sitemaps(starts[0]):
                    self.push(sm)
            except Exception as e:  # robots.txt trouble shouldn't stop the crawl
                log.warning("couldn't read sitemaps for %s: %s", starts[0], e)

    @property
    def queue(self) -> list[str]:
        return [u for _, _, u in sorted(self._heap)]

    def push(self, url: str, priority: int | None = None) -> None:
        url = normalize_url(url)
        if url in self._queued or url in self.visited:
            return
        prio = url_priority(url) if priority is None else priority
        if prio is None:
            return
        self._queued.add(url)
        heapq.heappush(self._heap, (prio, next(self._seq), url))

    @property
    def done(self) -> bool:
        return (not self._heap or self.saved >= self.max_recipes or self.pages >= self.max_pages
                or self.gave_up is not None)

    def step(self) -> ScrapeResult | None:
        """Process the next queued URL. Returns its result (None for a sitemap)."""
        if self.done:
            return None
        _, _, url = heapq.heappop(self._heap)
        self._queued.discard(url)
        self.visited.add(url)
        if url in self.known:
            result = ScrapeResult(url, "known", "already in the library")
            self.results.append(result)
            return result
        self.pages += 1
        if _is_sitemap(url):  # sitemap or sitemap index: expand, don't score
            try:
                for loc in parse_sitemap(self.fetch(url)):
                    self.push(loc)
            except Exception as e:
                log.warning("sitemap %s failed: %s", url, e)
            return None
        result, html = scrape_url(url, self.fetch, checks=self.checks)
        self.results.append(result)
        if result.status == "saved":
            self.saved += 1
        if result.http_status in (403, 429):
            self.refusals += 1
            if self.refusals >= self.GIVE_UP_AFTER:
                self.gave_up = f"gave up after {self.refusals} refusals in a row (HTTP {result.http_status})"
                stop = ScrapeResult(url, "gave_up", self.gave_up)
                self.results.append(stop)
                return stop
        elif result.status != "error":
            self.refusals = 0
        if self.follow_links and html and result.status not in ("error", "blocked"):
            # listing pages (no recipe) are the main source of new links
            for link in discover_links(html, url, loose=self.loose_links):
                self.push(link)
        return result


def crawl(start_urls: list[str], fetch: Fetcher, *, min_rating=DEFAULT_MIN_RATING, min_reviews=0,
          max_recipes=25, max_pages=60, follow_links=True, allow_unrated=False,
          known: set[str] | None = None, checks: Checks | None = None) -> list[ScrapeResult]:
    c = SiteCrawler(start_urls, fetch, min_rating=min_rating, min_reviews=min_reviews,
                    max_recipes=max_recipes, max_pages=max_pages, follow_links=follow_links,
                    allow_unrated=allow_unrated, known=known, checks=checks)
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
                known: set[str] | None = None, use_sitemaps=True, checks: Checks | None = None,
                allow_unrated_sites: Iterable[str] = (),
                ) -> dict[str, list[ScrapeResult]]:
    """Crawl many sites with a separate recipe/page budget for each, taking turns
    between sites one page at a time. Taking turns spreads requests out, so the
    per-host delay rarely makes the crawl wait. ``max_total`` (0 = no limit) caps
    recipes saved across all sites. Returns {site: results}."""
    base = checks or Checks(min_rating=min_rating, min_reviews=min_reviews, allow_unrated=allow_unrated)
    unrated_ok = {s.strip().lower().removeprefix("www.") for s in allow_unrated_sites if s.strip()}
    crawlers = {}
    for site, urls in group_by_site(start_urls).items():
        ck = Checks(**{**base.__dict__, "allow_unrated": base.allow_unrated or site in unrated_ok})
        crawlers[site] = SiteCrawler(urls, fetch, max_recipes=per_site, max_pages=max_pages_per_site,
                                     follow_links=follow_links, loose_links=True, known=known,
                                     use_sitemaps=use_sitemaps, checks=ck)
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
