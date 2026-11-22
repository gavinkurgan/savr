"""Tests for the second wave: developer CLI + library, rewrite voices, households,
ratings, pantry, meal planner, Kroger cart push."""
import json
import re
import time
import types

import pytest

from app import cli, kroger
from app.db import RecipeDB
from app.library import Library, sync_library
from app.pantry import coverage, matches
from app.pipeline import process_recipe
from app.rewrite import apply_rewrite, detect_equipment, unhinge_text, validate_llm_output
from conftest import fixture, recipe_id


# ---------------------------------------------------------------- library + CLI
def _fake_site():
    pages = {
        "https://example.com/dinners/": fixture("listing.html"),
        "https://example.com/recipes/lemon-pasta/": fixture("blog_recipe.html"),
        "https://example.com/recipes/soggy-casserole/": fixture("low_rated.html"),
    }

    def fetch(url):
        if url not in pages:
            raise IOError("404")
        return pages[url]
    return pages, fetch


def test_cli_scrape_writes_library_and_respects_rating(tmp_path):
    lib_path = str(tmp_path / "library.json")
    pages, fetch = _fake_site()
    args = cli.build_parser().parse_args(["--library", lib_path, "scrape", "https://example.com/dinners/"])
    assert cli.cmd_scrape(args, fetch=fetch) == 0
    lib = Library(lib_path)
    assert [r["name"] for r in lib.recipes] == ["The BEST Lemon Parmesan Pasta"]
    rec = lib.recipes[0]
    assert rec["equipment"] and all(s.get("unhinged") for s in rec["steps"])
    # re-scraping the same URL updates instead of duplicating
    cli.cmd_scrape(args, fetch=fetch)
    assert len(Library(lib_path).recipes) == 1
    # lower the bar → the 3.2-star casserole gets in
    args = cli.build_parser().parse_args(["--library", lib_path, "scrape", "--min-rating", "3",
                                          "https://example.com/recipes/soggy-casserole/", "--no-follow"])
    cli.cmd_scrape(args, fetch=fetch)
    assert len(Library(lib_path).recipes) == 2


def test_cli_refresh_drops_recipes_that_fall_below_bar(tmp_path):
    lib_path = str(tmp_path / "library.json")
    pages, fetch = _fake_site()
    cli.cmd_scrape(cli.build_parser().parse_args(["--library", lib_path, "scrape", "https://example.com/recipes/lemon-pasta/", "--no-follow"]), fetch=fetch)
    pages["https://example.com/recipes/lemon-pasta/"] = pages["https://example.com/recipes/lemon-pasta/"].replace('"ratingValue":"4.62"', '"ratingValue":"3.1"')
    cli.cmd_refresh(cli.build_parser().parse_args(["--library", lib_path, "refresh"]), fetch=fetch)
    assert Library(lib_path).recipes == []


def test_cli_remove_and_stats(tmp_path, capsys):
    lib_path = str(tmp_path / "library.json")
    _, fetch = _fake_site()
    cli.cmd_scrape(cli.build_parser().parse_args(["--library", lib_path, "scrape", "https://example.com/recipes/lemon-pasta/", "--no-follow"]), fetch=fetch)
    assert cli.main(["--library", lib_path, "stats"]) == 0
    assert "1 recipes" in capsys.readouterr().out
    cli.main(["--library", lib_path, "remove", "The BEST Lemon Parmesan Pasta"])
    assert Library(lib_path).recipes == []


def test_sync_library_is_idempotent_and_keeps_ids(tmp_path):
    lib_path = str(tmp_path / "library.json")
    _, fetch = _fake_site()
    cli.cmd_scrape(cli.build_parser().parse_args(["--library", lib_path, "scrape", "--min-rating", "3", "https://example.com/dinners/"]), fetch=fetch)
    db = RecipeDB(str(tmp_path / "db.sqlite"))
    assert sync_library(db, lib_path) == 2
    ids = sorted(db.all_ids())
    assert sync_library(db, lib_path) == 0  # unchanged file → no work
    lib = Library(lib_path)
    lib.recipes[0]["name"] = "Renamed"
    lib.remove(lib.recipes[1]["url"])
    lib.save()
    assert sync_library(db, lib_path) == 1
    assert db.count() == 1 and db.all_ids()[0] in ids  # same id kept, removed recipe pruned


def test_shipped_library_is_complete():
    lib = Library()
    assert len(lib.recipes) >= 40
    for r in lib.recipes:
        assert r["rating"] >= 4.0
        assert r["steps"] and all(s["unhinged"] and s["text"] for s in r["steps"])
        # unhinged keeps every number from the standard steps
        std = set(re.findall(r"\d+", " ".join(s["text"] for s in r["steps"])))
        unh = set(re.findall(r"\d+", " ".join(s["unhinged"] for s in r["steps"])))
        assert std <= unh, r["name"]


# ---------------------------------------------------------------- rewrite
def test_unhinged_rules_keep_facts():
    out = unhinge_text("Bake 25 to 28 minutes until golden.", "Bake", "x")
    assert "25" in out and "28" in out and "golden as hell" in out
    out = unhinge_text("Preheat oven to 425°F.", "Preheat", "x")
    assert out.startswith("Crank the damn oven to 425°F")
    out = unhinge_text("Chop the onion.", "Cut", "seed")
    assert re.match(r"Chop the (?:hell|absolute crap|living hell) out of the onion", out)


def test_equipment_detection():
    steps = [{"text": "Preheat oven to 400°F and line a baking sheet with parchment."},
             {"text": "Heat oil in a large skillet over medium heat."}, {"text": "Whisk the eggs."}]
    eq = detect_equipment(steps)
    assert {"oven", "sheet pan", "parchment paper", "large skillet", "whisk"} <= set(eq)


def _recipe():
    raw = {"name": "Test Soup", "recipeIngredient": ["1 onion", "4 cups broth"],
           "recipeInstructions": ["Chop the onion.", "Simmer the onion in broth for 20 minutes.", "Serve hot."]}
    return process_recipe(raw, "https://a.com/soup")


def _fake_claude(payload):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": json.dumps(payload)}]}
    return lambda *a, **kw: Resp()


def test_claude_rewrite_success(monkeypatch):
    good = {"equipment": ["large pot", "knife"], "steps": [
        {"action": "Cut", "minutes": None, "standard": "Chop the onion.", "unhinged": "Chop that damn onion."},
        {"action": "Cook", "minutes": 20, "standard": "Simmer it in the broth for 20 minutes, then serve hot.",
         "unhinged": "Let that shit simmer in broth for 20 minutes, then serve it hot."}]}
    monkeypatch.setattr("app.rewrite.requests.post", _fake_claude(good))
    r = apply_rewrite(_recipe(), use_llm=True, api_key="test")
    assert r["voice_source"].startswith("claude:")
    assert len(r["steps"]) == 2 and r["steps"][1]["minutes"] == 20
    assert r["equipment"] == ["large pot", "knife"]
    assert r["steps_rules"][0]["text"] == "Chop the onion."


def test_claude_rewrite_rejected_when_numbers_change(monkeypatch):
    bad = {"equipment": [], "steps": [
        {"action": "Cook", "minutes": 15, "standard": "Simmer 15 minutes.", "unhinged": "Simmer that shit 15 minutes."}]}
    monkeypatch.setattr("app.rewrite.requests.post", _fake_claude(bad))
    r = apply_rewrite(_recipe(), use_llm=True, api_key="test")
    assert r["voice_source"] == "rules" and "lost numbers" in r["rewrite_error"]
    assert validate_llm_output(_recipe(), bad)


# ---------------------------------------------------------------- households & ratings
def test_household_cookie_join_and_ratings(app, client):
    code = client.get("/api/household").get_json()["code"]
    assert re.match(r"^[a-z]+-[a-z]+-\d{4}$", code)
    rid = recipe_id(client, "brownies")
    client.post("/api/ratings", json={"recipe_id": rid, "value": 1, "member": "Sam"})
    # a second phone joins with the code and sees the same ratings
    phone2 = app.test_client()
    assert phone2.post("/api/household/join", json={"code": code.upper()}).status_code == 200
    phone2.post("/api/ratings", json={"recipe_id": rid, "value": 1, "member": "Alex"})
    r = phone2.get("/api/ratings").get_json()[str(rid)]
    assert r["up"] == 2 and r["members"] == {"Sam": 1, "Alex": 1}
    assert phone2.post("/api/household/join", json={"code": "nope-nope-0000"}).status_code == 404
    # header works for API clients (iOS)
    other = app.test_client()
    assert other.get("/api/ratings", headers={"X-Household": code}).get_json()[str(rid)]["up"] == 2


def test_thumbs_down_removes_from_picks_and_likes_boost(client):
    body = {"text": "easy dinner", "limit": 40}
    before = client.post("/api/recommend", json=body).get_json()["results"]
    top = before[0]
    client.post("/api/ratings", json={"recipe_id": top["id"], "value": -1})
    after = client.post("/api/recommend", json={**body, "limit": 5}).get_json()["results"]
    assert top["id"] not in [r["id"] for r in after]
    low = before[-1]
    client.post("/api/ratings", json={"recipe_id": low["id"], "value": 1})
    boosted = client.post("/api/recommend", json=body).get_json()["results"]
    new_pos = [r["id"] for r in boosted].index(low["id"])
    assert new_pos < len(before) - 1
    assert "your household loved this" in boosted[new_pos]["match_reasons"]


# ---------------------------------------------------------------- pantry
def test_pantry_matching_rules():
    assert matches("chicken", "boneless chicken breast")
    assert matches("lemons", "lemon")
    assert not matches("chicken", "chicken broth")
    assert not matches("milk", "coconut milk")
    assert not matches("rice", "rice vinegar")


def test_pantry_flow(client):
    client.post("/api/pantry", json={"items": "baby spinach, eggs, feta", "use_by": time.strftime("%Y-%m-%d")})
    items = client.get("/api/pantry").get_json()["items"]
    assert {i["item"] for i in items} == {"baby spinach", "eggs", "feta"}
    assert all(i["days_left"] == 0 for i in items)
    sugg = client.get("/api/pantry/suggest").get_json()["results"]
    assert sugg[0]["name"] == "Spinach and Feta Egg Muffins"
    assert set(sugg[0]["pantry"]["uses_expiring"]) >= {"baby spinach", "feta"}
    # pantry items are skipped from shopping lists
    rid = sugg[0]["id"]
    names = [i["item"] for i in client.post("/api/grocery", json={"recipes": [{"id": rid}]}).get_json()["items"]]
    assert "feta" not in names and not any("egg" in n for n in names)
    names = [i["item"] for i in client.post("/api/grocery", json={"recipes": [{"id": rid}], "use_pantry": False}).get_json()["items"]]
    assert "crumbled feta" in names or "feta" in names
    # mark as bought puts list items into the pantry
    client.post("/api/pantry/bought", json={"recipes": [{"id": recipe_id(client, "brownies")}]})
    assert "cocoa powder" in {i["item"] for i in client.get("/api/pantry").get_json()["items"]}
    iid = items[0]["id"]
    left = client.delete(f"/api/pantry?id={iid}").get_json()["items"]
    assert iid not in [i["id"] for i in left]


def test_pantry_shows_in_recommendations(client):
    client.post("/api/pantry", json={"items": ["chickpeas", "coconut milk", "spinach"]})
    res = client.post("/api/recommend", json={"text": "vegan dinner", "limit": 3}).get_json()["results"]
    assert res[0]["name"] == "Vegan Coconut Chickpea Curry"
    assert any("ingredients" in w or "uses up" in w for w in res[0]["match_reasons"])


# ---------------------------------------------------------------- planner
def test_plan_week_and_swap(client):
    plan = client.post("/api/plan", json={"text": "easy dinners", "nights": 5, "diet": ["gluten_free"]}).get_json()["plan"]
    ids = [d["recipe_id"] for d in plan["days"]]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert all("gluten_free" in d["recipe"]["diet_tags"] for d in plan["days"])
    assert all(d["recipe"]["meal_type"] in ("dinner", "soup", "salad", "lunch") for d in plan["days"])
    cuisines = [d["recipe"]["cuisine"] for d in plan["days"] if d["recipe"]["cuisine"] != "other"]
    assert len(cuisines) - len(set(cuisines)) <= 2  # variety
    swapped = client.post("/api/plan/swap", json={"slot": 2}).get_json()["plan"]
    new_ids = [d["recipe_id"] for d in swapped["days"]]
    assert new_ids[2] != ids[2] and new_ids[:2] == ids[:2] and new_ids[3:] == ids[3:]
    assert client.get("/api/plan").get_json()["plan"]["days"][2]["recipe_id"] == new_ids[2]
    # whole week → one grocery list with shared ingredients merged
    g = client.post("/api/grocery", json={"recipes": [{"id": i} for i in new_ids]}).get_json()
    assert g["count"] > 10 and len({i["item"] for i in g["items"]}) == g["count"]


def test_planner_skips_thumbs_down(client):
    first = client.post("/api/plan", json={"nights": 7}).get_json()["plan"]["days"]
    for d in first[:3]:
        client.post("/api/ratings", json={"recipe_id": d["recipe_id"], "value": -1})
    second = client.post("/api/plan", json={"nights": 7}).get_json()["plan"]["days"]
    assert not {d["recipe_id"] for d in first[:3]} & {d["recipe_id"] for d in second}


# ---------------------------------------------------------------- kroger
class FakeHTTP:
    def __init__(self):
        self.calls = []

    def _resp(self, status, data=None):
        return types.SimpleNamespace(status_code=status, json=lambda: data or {}, text=json.dumps(data or {}))

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        if data.get("grant_type") == "client_credentials":
            return self._resp(200, {"access_token": "client-tok", "expires_in": 1800})
        if data.get("grant_type") == "authorization_code":
            return self._resp(200, {"access_token": "user-tok", "refresh_token": "r1", "expires_in": 1800})
        if data.get("grant_type") == "refresh_token":
            return self._resp(200, {"access_token": "user-tok-2", "refresh_token": "r2", "expires_in": 1800})
        return self._resp(400)

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        if url.endswith("/locations"):
            return self._resp(200, {"data": [{"locationId": "620", "name": "King Soopers Pearl", "chain": "KINGSOOPERS",
                                             "address": {"addressLine1": "1 Main", "city": "Denver"}}]})
        term = params["filter.term"]
        if "unicorn" in term:
            return self._resp(200, {"data": []})
        return self._resp(200, {"data": [{"upc": f"000{len(term)}", "description": term.title(),
                                          "items": [{"size": "1 ct", "price": {"regular": 2.5}}]}]})

    def put(self, url, json=None, headers=None, **kw):
        self.calls.append(("PUT", url, json, headers))
        return self._resp(204)


@pytest.fixture
def kroger_env(monkeypatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "cid")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("KROGER_REDIRECT_URI", "http://localhost/kroger/callback")
    kroger._client_cache.clear()
    http = FakeHTTP()
    for name in ("post", "get", "put"):
        monkeypatch.setattr(kroger.requests, name, getattr(http, name))
    return http


def test_kroger_oauth_and_cart(client, kroger_env):
    resp = client.get("/kroger/connect")
    assert resp.status_code == 302 and "connect/oauth2/authorize" in resp.location
    state = re.search(r"state=([^&]+)", resp.location).group(1)
    assert "cart.basic%3Awrite" in resp.location
    assert client.get("/kroger/callback?code=abc&state=wrong").status_code == 400
    resp = client.get("/kroger/connect")
    state = re.search(r"state=([^&]+)", resp.location).group(1)
    assert client.get(f"/kroger/callback?code=abc&state={state}").status_code == 302
    assert client.get("/api/kroger/status").get_json()["connected"] is True
    locs = client.get("/api/kroger/locations?zip=80202").get_json()["locations"]
    client.post("/api/kroger/location", json=locs[0])
    out = client.post("/api/kroger/cart", json={"recipes": [{"id": recipe_id(client, "tacos")}]}).get_json()
    assert out["added"] and out["estimated_total"] > 0
    put = [c for c in kroger_env.calls if c[0] == "PUT"][0]
    assert put[1].endswith("/cart/add") and put[3]["Authorization"] == "Bearer user-tok"
    assert all(i["modality"] == "PICKUP" for i in put[2]["items"])
    searches = [c for c in kroger_env.calls if c[0] == "GET" and c[1].endswith("/products")]
    assert all(c[2]["filter.locationId"] == "620" for c in searches)


def test_kroger_refreshes_expired_token(app, kroger_env):
    hh = app.extensions["households"].create()
    app.extensions["households"].kroger_save(hh["id"], access_token="old", refresh_token="r1", expires_at=time.time() - 5)
    tok = kroger.ensure_user_token(app.extensions["households"], hh["id"])
    assert tok == "user-tok-2"
    assert app.extensions["households"].kroger(hh["id"])["refresh_token"] == "r2"


def test_kroger_disabled_without_keys(client, monkeypatch):
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    assert client.get("/kroger/connect").status_code == 404
    assert client.post("/api/kroger/cart", json={}).status_code == 503


def test_kroger_network_failure_is_a_clean_error(client, kroger_env, monkeypatch):
    def boom(*a, **kw):
        raise kroger.requests.ConnectionError("no route")
    monkeypatch.setattr(kroger.requests, "post", boom)
    resp = client.get("/api/kroger/locations?zip=80202")
    assert resp.status_code == 502 and "Couldn't reach Kroger" in resp.get_json()["error"]


# ---------------------------------------------------------------- multi-site scrape
def _multi_site():
    from conftest import fixture as fx
    pages = {
        "https://www.alpha.com/": '<a href="/recipes/lemon-pasta/">a</a><a href="/recipes/soggy-casserole/">b</a>',
        "https://www.alpha.com/recipes/lemon-pasta/": fx("blog_recipe.html"),
        "https://www.alpha.com/recipes/soggy-casserole/": fx("low_rated.html"),
        # blog-style slugs with no "recipe" in the path, found through a sitemap index
        "https://beta.net/sitemap_index.xml": '<sitemapindex><sitemap><loc>https://beta.net/post-sitemap.xml</loc></sitemap></sitemapindex>',
        "https://beta.net/post-sitemap.xml": '<urlset><url><loc>https://beta.net/one-pot-lemon-pasta/</loc></url></urlset>',
        "https://beta.net/one-pot-lemon-pasta/": fx("blog_recipe.html").replace("Lemon Parmesan Pasta", "Beta Lemon Pasta"),
    }
    order = []

    def fetch(url):
        order.append(url)
        if url not in pages:
            raise IOError("404")
        return pages[url]
    return fetch, order


def test_read_sources_handles_comments_and_bare_domains(tmp_path):
    from app.scraper import group_by_site, read_sources
    f = tmp_path / "sites.txt"
    f.write_text("# header\n\nallrecipes.com\nhttps://www.allrecipes.com/recipes/  # dinner\n"
                 "  # indented comment\nhttps://seriouseats.com/x#frag\n")
    urls = read_sources(str(f))
    assert urls == ["https://allrecipes.com/", "https://www.allrecipes.com/recipes/", "https://seriouseats.com/x#frag"]
    assert list(group_by_site(urls)) == ["allrecipes.com", "seriouseats.com"]


def test_crawl_sites_budgets_per_site_and_interleaves():
    from app.scraper import crawl_sites
    fetch, order = _multi_site()
    res = crawl_sites(["https://www.alpha.com/", "https://beta.net/sitemap_index.xml"], fetch,
                      per_site=5, min_rating=4.0)
    assert {r.url: r.status for r in res["alpha.com"]}["https://www.alpha.com/recipes/lemon-pasta/"] == "saved"
    beta = {r.url: r.status for r in res["beta.net"]}
    assert beta["https://beta.net/one-pot-lemon-pasta/"] == "saved"  # found via nested sitemap
    assert order[:2] == ["https://www.alpha.com/", "https://beta.net/sitemap_index.xml"]  # sites take turns
    # a per-site cap of 1 stops alpha after its first saved recipe
    fetch, order = _multi_site()
    res = crawl_sites(["https://www.alpha.com/"], fetch, per_site=1, min_rating=0)
    assert sum(r.status == "saved" for r in res["alpha.com"]) == 1


def test_crawl_sites_max_total():
    from app.scraper import crawl_sites
    fetch, _ = _multi_site()
    res = crawl_sites(["https://www.alpha.com/", "https://beta.net/sitemap_index.xml"], fetch,
                      per_site=5, max_total=1, min_rating=4.0)
    assert sum(r.status == "saved" for rs in res.values() for r in rs) == 1


def test_cli_scrape_sites(tmp_path, capsys):
    lib_path = str(tmp_path / "library.json")
    sites = tmp_path / "sites.txt"
    sites.write_text("https://www.alpha.com/\nhttps://beta.net/sitemap_index.xml\n")
    fetch, _ = _multi_site()
    argv = ["--library", lib_path, "scrape-sites", str(sites)]
    assert cli.cmd_scrape_sites(cli.build_parser().parse_args(argv), fetch=fetch) == 0
    names = sorted(r["name"] for r in Library(lib_path).recipes)
    assert names == ["The BEST Beta Lemon Pasta", "The BEST Lemon Parmesan Pasta"]
    out = capsys.readouterr().out
    assert "alpha.com" in out and "beta.net" in out
    # --only filters sites; --list doesn't scrape
    assert cli.main(argv + ["--only", "beta.net", "--list"]) == 0
    out = capsys.readouterr().out
    assert "beta.net" in out and "alpha.com" not in out


def test_default_sites_file_is_valid():
    from app.scraper import group_by_site, read_sources
    sites = group_by_site(read_sources(cli.DEFAULT_SITES_FILE))
    assert len(sites) >= 30
    for s in ("allrecipes.com", "simplyrecipes.com", "seriouseats.com"):
        assert s in sites
