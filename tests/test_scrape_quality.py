"""Crawl efficiency (priorities, sitemaps, known URLs, back-off) and recipe quality
(review-weighted score, unrated, completeness, duplicates, prune)."""
import json
import sqlite3
import types

import pytest

from app import cli
from app.library import Library
from app.quality import dish_key, quality_score, sql_quality_expr
from app.scraper import (Checks, HTTPStatusError, PoliteFetcher, SiteCrawler, crawl_sites, normalize_url,
                         scrape_url, url_priority)


def recipe_page(name, rating=4.8, count=100, image=True, ingredients=5):
    data = {"@context": "https://schema.org", "@type": "Recipe", "name": name,
            "recipeIngredient": [f"{i + 1} cup thing {i}" for i in range(ingredients)],
            "recipeInstructions": [{"@type": "HowToStep", "text": "Mix everything. Bake 20 minutes."}]}
    if image:
        data["image"] = "https://img.example/x.jpg"
    if rating is not None:
        data["aggregateRating"] = {"ratingValue": rating, "ratingCount": count}
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def site_fetch(pages):
    order = []

    def fetch(url):
        order.append(url)
        if url not in pages:
            raise IOError("404")
        return pages[url]
    return fetch, order


# ---------------------------------------------------------------- quality score
def test_quality_score_weights_reviews():
    assert quality_score(4.9, 6) < quality_score(4.6, 19000)
    assert quality_score(5.0, 0) == 4.0
    assert quality_score(None, 0) < 4.0


def test_sql_quality_matches_python():
    c = sqlite3.connect(":memory:")
    for rating, n in [(4.9, 6), (4.6, 19000), (None, 0), (3.2, 50), (5.0, None)]:
        got = c.execute(f"SELECT {sql_quality_expr(':r', ':n')}", {"r": rating, "n": n}).fetchone()[0]
        assert got == pytest.approx(quality_score(rating, n), abs=1e-3)


def test_top_rated_sort_uses_score(app):
    db = app.extensions["db"]
    base = {"ingredients": ["x"], "steps": [], "diet_tags": [], "styles": []}
    db.upsert({**base, "url": "https://a/1", "name": "Few reviews", "rating": 5.0, "rating_count": 2})
    db.upsert({**base, "url": "https://a/2", "name": "Many reviews", "rating": 4.7, "rating_count": 5000})
    names = [r["name"] for r in db.search(q="reviews", sort="rating")]
    assert names == ["Many reviews", "Few reviews"]


def test_dish_key_matches_same_dish_across_sites():
    assert dish_key("The BEST Chocolate Chip Cookies (Easy!)") == dish_key("Best Chocolate Chip Cookie Recipe")
    assert dish_key("Vegan Espresso Donuts Recipe by Tasty") == dish_key("Vegan Espresso Donuts")
    assert dish_key("World's Best Lasagna") == dish_key("Lasagna")
    assert dish_key("Chicken Pot Pie") != dish_key("Chicken Pot Pie Soup")


# ---------------------------------------------------------------- urls
def test_normalize_url():
    assert normalize_url("https://Smittenkitchen.com/./recipes/x/#comments") == "https://smittenkitchen.com/recipes/x/"
    assert normalize_url("https://a.com/r/x/?utm_source=pin&id=3") == "https://a.com/r/x/?id=3"
    assert normalize_url("https://a.com") == "https://a.com/"


@pytest.mark.parametrize("url,prio", [
    ("https://www.allrecipes.com/recipe/23600/worlds-best-lasagna/", 1),
    ("https://www.seriouseats.com/the-food-lab-best-chocolate-chip-cookie-recipe", 1),
    ("https://smittenkitchen.com/2026/11/chocolate-pecan-pie-bars/", 1),
    ("https://www.foodnetwork.com/recipes/ina-garten/perfect-roast-chicken-recipe-1940592", 1),
    ("https://www.budgetbytes.com/one-pot-chili-mac/", 2),
    ("https://www.allrecipes.com/recipes/17562/dinner/", 3),
    ("https://www.allrecipes.com/recipes/", 3),
    ("https://a.com/recipe-sitemap.xml", 0),
    ("https://a.com/post-sitemap.xml", 2),
    ("https://a.com/category-sitemap.xml", None),
    ("https://a.com/page-sitemap.xml", None),
    ("https://a.com/sitemap.xml.gz", None),
])
def test_url_priority(url, prio):
    assert url_priority(url) == prio


# ---------------------------------------------------------------- crawling
def test_recipe_pages_are_fetched_before_listing_pages():
    pages = {
        "https://www.r.com/": '<a href="/recipes/1/dinner/">l1</a><a href="/recipes/2/lunch/">l2</a>'
                              '<a href="/recipe/10/soup/">r1</a><a href="/recipe/11/stew/">r2</a>',
        "https://www.r.com/recipe/10/soup/": recipe_page("Soup"),
        "https://www.r.com/recipe/11/stew/": recipe_page("Stew"),
        "https://www.r.com/recipes/1/dinner/": '<a href="/recipe/12/pie/">r3</a>',
        "https://www.r.com/recipe/12/pie/": recipe_page("Pie"),
    }
    fetch, order = site_fetch(pages)
    c = SiteCrawler(["https://www.r.com/"], fetch, max_pages=10, max_recipes=10, loose_links=True)
    while not c.done:
        c.step()
    assert order[:4] == ["https://www.r.com/", "https://www.r.com/recipe/10/soup/",
                         "https://www.r.com/recipe/11/stew/", "https://www.r.com/recipes/1/dinner/"]
    assert order[4] == "https://www.r.com/recipe/12/pie/"  # found on the listing page, fetched next
    assert c.saved == 3


def test_known_urls_are_skipped_without_fetching_or_using_budget():
    pages = {"https://www.r.com/": '<a href="/recipe/10/soup/">a</a><a href="/recipe/11/stew/">b</a>',
             "https://www.r.com/recipe/11/stew/": recipe_page("Stew")}
    fetch, order = site_fetch(pages)
    c = SiteCrawler(["https://www.r.com/"], fetch, max_pages=2, known={"https://www.r.com/recipe/10/soup/"})
    while not c.done:
        c.step()
    assert "https://www.r.com/recipe/10/soup/" not in order
    assert {r.url: r.status for r in c.results}["https://www.r.com/recipe/10/soup/"] == "known"
    assert c.saved == 1 and c.pages == 2  # home + stew; the known page didn't count


def test_sitemaps_from_robots_are_used():
    pages = {"https://b.com/": "<p>nothing linked</p>",
             "https://b.com/sitemap_index.xml": "<sitemapindex><sitemap><loc>https://b.com/recipe-sitemap.xml</loc></sitemap>"
                                                "<sitemap><loc>https://b.com/tag-sitemap.xml</loc></sitemap></sitemapindex>",
             "https://b.com/recipe-sitemap.xml": "<urlset><url><loc>https://b.com/lemon-pie-recipe/</loc></url></urlset>",
             "https://b.com/lemon-pie-recipe/": recipe_page("Lemon Pie")}
    site, order = site_fetch(pages)

    class Fetcher:  # like PoliteFetcher: callable, and lists robots.txt sitemaps
        def __call__(self, url):
            return site(url)

        def sitemaps(self, url):
            return ["https://b.com/sitemap_index.xml"]
    res = crawl_sites(["https://b.com/"], Fetcher(), per_site=5, max_pages_per_site=10)
    assert [r.status for r in res["b.com"]] == ["no_recipe", "saved"]
    assert "https://b.com/tag-sitemap.xml" not in order  # non-recipe sitemap skipped


def test_site_that_keeps_refusing_is_given_up():
    def fetch(url):
        if url == "https://c.com/":
            return "".join(f'<a href="/recipe/{i}/x/">x</a>' for i in range(20))
        raise HTTPStatusError(403, url)
    c = SiteCrawler(["https://c.com/"], fetch, max_pages=50)
    while not c.done:
        c.step()
    assert c.gave_up and "403" in c.gave_up
    assert c.pages == 1 + SiteCrawler.GIVE_UP_AFTER
    assert c.results[-1].status == "gave_up"
    assert all(r.http_status == 403 for r in c.results[1:-1])


def test_unrated_low_rated_incomplete_statuses():
    pages = {"u": recipe_page("U", rating=None), "l": recipe_page("L", rating=3.1),
             "few": recipe_page("F", rating=5.0, count=3), "i": recipe_page("I", image=False, ingredients=2)}
    fetch = pages.__getitem__
    strict = Checks(min_ingredients=3, require_instructions=True, require_image=True)
    assert scrape_url("u", fetch, checks=strict)[0].status == "unrated"
    assert scrape_url("l", fetch, checks=strict)[0].status == "low_rating"
    assert scrape_url("few", fetch, checks=strict)[0].status == "saved"
    assert scrape_url("few", fetch, checks=Checks(min_score=4.2))[0].status == "low_rating"
    r = scrape_url("i", fetch, checks=strict)[0]
    assert r.status == "incomplete" and "2 ingredients" in r.detail and "no image" in r.detail
    assert scrape_url("u", fetch, checks=Checks(allow_unrated=True))[0].status == "saved"


def test_allow_unrated_per_site():
    pages = {"https://k.com/": '<a href="/recipe/1/bread/">b</a>', "https://k.com/recipe/1/bread/": recipe_page("Bread", rating=None),
             "https://z.com/": '<a href="/recipe/1/bread/">b</a>', "https://z.com/recipe/1/bread/": recipe_page("Bread", rating=None)}
    fetch, _ = site_fetch(pages)
    res = crawl_sites(["https://k.com/", "https://z.com/"], fetch, per_site=5, allow_unrated_sites=["k.com"])
    assert [r.status for r in res["k.com"]][-1] == "saved"
    assert [r.status for r in res["z.com"]][-1] == "unrated"


# ---------------------------------------------------------------- polite fetcher
class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url.endswith("/robots.txt"):
            return types.SimpleNamespace(status_code=200, headers={},
                                         text="User-agent: *\nAllow: /\nSitemap: https://h.com/sitemap_index.xml\nSitemap: /recipe-sitemap.xml\n")
        status, headers = self.responses.pop(0)
        return types.SimpleNamespace(status_code=status, headers=headers, text="ok")


def test_fetcher_backs_off_on_429_and_honours_retry_after():
    sleeps = []
    f = PoliteFetcher(delay_seconds=0, session=FakeSession([(429, {"Retry-After": "7"}), (200, {})]), sleep=sleeps.append)
    assert f("https://h.com/recipe/1/") == "ok"
    assert 7 in sleeps
    assert f._host_delay["h.com"] > 0  # slowed down for the rest of the run


def test_fetcher_gives_up_after_retries():
    sleeps = []
    f = PoliteFetcher(delay_seconds=0, session=FakeSession([(503, {})] * 3), sleep=sleeps.append)
    with pytest.raises(HTTPStatusError) as e:
        f("https://h.com/recipe/1/")
    assert e.value.status == 503 and [s for s in sleeps if s >= 5] == [5, 15]


def test_fetcher_lists_sitemaps_from_robots():
    f = PoliteFetcher(session=FakeSession([]))
    assert f.sitemaps("https://h.com/") == ["https://h.com/sitemap_index.xml", "https://h.com/recipe-sitemap.xml"]


# ---------------------------------------------------------------- CLI: dedupe + prune
def test_scrape_keeps_best_copy_of_a_dish(tmp_path):
    lib_path = str(tmp_path / "library.json")
    pages = {"https://a.com/recipe/1/cookies/": recipe_page("The BEST Chocolate Chip Cookies", 4.9, 8),
             "https://b.com/recipe/2/cookies/": recipe_page("Chocolate Chip Cookie Recipe", 4.7, 9000),
             "https://c.com/recipe/3/cookies/": recipe_page("Easy Chocolate Chip Cookies", 4.5, 40)}
    fetch, _ = site_fetch(pages)
    for url in pages:
        cli.cmd_scrape(cli.build_parser().parse_args(["--library", lib_path, "scrape", url, "--no-follow"]), fetch=fetch)
    urls = [r["url"] for r in Library(lib_path).recipes]
    assert urls == ["https://b.com/recipe/2/cookies/"]


def test_prune_raises_the_bar_without_rescraping(tmp_path, capsys):
    lib_path = str(tmp_path / "library.json")
    lib = Library(lib_path)
    base = {"ingredients": [], "steps": [], "diet_tags": [], "styles": []}
    lib.recipes = [
        {**base, "url": "https://a.com/1", "name": "Good Soup", "rating": 4.8, "rating_count": 900},
        {**base, "url": "https://b.com/2", "name": "The Best Good Soup", "rating": 4.5, "rating_count": 30},
        {**base, "url": "https://c.com/3", "name": "Thin Stew", "rating": 4.9, "rating_count": 2},
        {**base, "url": "https://d.com/4", "name": "Mystery Bread", "rating": None, "rating_count": 0},
        {**base, "url": "https://sample.local/x", "name": "Sample", "rating": 3.0, "rating_count": 1},
    ]
    lib.save()
    cli.main(["--library", lib_path, "prune", "--min-score", "4.1", "--dedupe", "--dry-run"])
    assert "would drop 2" in capsys.readouterr().out and len(Library(lib_path).recipes) == 5
    cli.main(["--library", lib_path, "prune", "--min-score", "4.1", "--dedupe", "--drop-unrated"])
    assert sorted(r["url"] for r in Library(lib_path).recipes) == ["https://a.com/1", "https://sample.local/x"]
