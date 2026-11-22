"""Guards for library updates: cleanups on scraped data, paging, startup sync,
and a smoke test that the shipped library.json loads and every page renders."""
import json
import multiprocessing as mp

import pytest

from app import create_app
from app.db import RecipeDB
from app.library import DEFAULT_LIBRARY, Library, sync_library
from app.simplifier import is_hands_off
from app.tagger import CUISINE_ALIASES, known_cuisine, normalize_cuisine

CANONICAL = set(CUISINE_ALIASES.values()) | {"other"}


# ---------------------------------------------------------------- cuisine cleanup
@pytest.mark.parametrize("value,expected", [
    ("Italian", "italian"), (["Mexican"], "mexican"), ("Tex Mex", "tex-mex"), ("tex-mex", "tex-mex"),
    ("Cajun, Louisiana, New Orleans", "cajun"), ("Arabic, Middle Eastern, Moroccan", "middle eastern"),
    ("Latin American", "latin american"), ("Asian-Inspired", "asian"),
    ("Bacon Cuisine Potato Cuisine Hash Brown Cuisine Lime Cuisine", None),
    ("Western", None), ("Gluten-Free, Vegan", None), ("Baking Recipes", None), ("", None), (None, None),
])
def test_known_cuisine(value, expected):
    assert known_cuisine(value) == expected


def test_normalize_cuisine_infers_when_site_value_is_junk():
    r = {"cuisine": "Western", "name": "Classic Spaghetti Carbonara", "ingredients": ["spaghetti"]}
    assert normalize_cuisine(r) == "italian"
    r = {"cuisine": "Squash Cuisine Pumpkin Cuisine", "name": "Pumpkin Bread", "ingredients": ["pumpkin"]}
    assert normalize_cuisine(r) == "other"


# ---------------------------------------------------------------- hands-off time
@pytest.mark.parametrize("text", [
    "Let the turkey stand at room temperature for 30 minutes.", "Brine the turkey for 18-24 hours.",
    "Continue roasting, basting every 30 minutes.", "Refrigerate overnight.", "Soak the beans for 8 hours.",
])
def test_hands_off_detection(text):
    assert is_hands_off("Prepare", text)


def test_hands_on_steps_stay_hands_on():
    assert not is_hands_off("Cut", "Dice the onion and mince the garlic.")
    assert not is_hands_off("Mix", "Whisk the eggs and sugar for 2 minutes.")


# ---------------------------------------------------------------- paging
def test_home_page_pages_through_everything(client, monkeypatch):
    from app import web
    monkeypatch.setattr(web, "PAGE_SIZE", 15)
    html = client.get("/").get_data(as_text=True)
    assert "1–15 of 40 recipes" in html and "Page 1 of 3" in html and 'rel="next"' in html
    html = client.get("/?page=3").get_data(as_text=True)
    assert "31–40 of 40 recipes" in html and 'rel="prev"' in html and 'rel="next"' not in html
    # filters stay applied across pages, and the count is the filtered total
    html = client.get("/?meal_type=dinner&page=1").get_data(as_text=True)
    total = len(client.get("/api/recipes?meal_type=dinner&limit=200").get_json()["results"])
    assert f"{total} recipes" in html
    seen = set()
    for p in (1, 2, 3):
        ids = [int(x.split('"')[0]) for x in client.get(f"/?page={p}").get_data(as_text=True).split('data-add="')[1:]]
        seen.update(ids)
    assert len(seen) == 40  # no recipe skipped or repeated across pages


# ---------------------------------------------------------------- startup sync
def _sync_worker(db_path, lib_path, q):
    q.put(sync_library(RecipeDB(db_path), lib_path))


def test_concurrent_startup_sync_loads_library_once(tmp_path, samples_library):
    """Gunicorn starts several workers at once; only one should load the library."""
    db_path = str(tmp_path / "db.sqlite")
    RecipeDB(db_path)  # create schema
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_sync_worker, args=(db_path, samples_library, q)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    results = sorted(q.get(timeout=5) for _ in procs)
    assert results == [0, 0, 40]
    assert RecipeDB(db_path).count() == 40


def test_sync_rolls_back_on_failure(tmp_path, samples_library):
    db = RecipeDB(str(tmp_path / "db.sqlite"))
    sync_library(db, samples_library)
    lib = Library(samples_library)
    lib.recipes.append({"url": "https://x.com/broken"})  # missing name → upsert fails mid-sync
    bad = str(tmp_path / "bad.json")
    lib.path = bad
    lib.save()
    with pytest.raises(Exception):
        sync_library(db, bad)
    assert db.count() == 40  # nothing half-applied


def test_sync_cleans_cuisine_from_old_library_files(tmp_path, samples_library):
    lib = Library(samples_library)
    lib.recipes[0]["cuisine"] = "bacon cuisine potato cuisine"
    lib.path = str(tmp_path / "old.json")
    lib.save()
    app = create_app({"DATABASE_PATH": str(tmp_path / "db.sqlite"), "LIBRARY_PATH": lib.path, "TESTING": True})
    cuisines = {c["value"] for c in app.extensions["db"].facets()["cuisines"]}
    assert cuisines <= CANONICAL


# ---------------------------------------------------------------- shipped library smoke test
@pytest.fixture(scope="module")
def shipped(tmp_path_factory):
    app = create_app({"DATABASE_PATH": str(tmp_path_factory.mktemp("shipped") / "db.sqlite"),
                      "LIBRARY_PATH": DEFAULT_LIBRARY, "TESTING": True, "SECRET_KEY": "t"})
    return app, app.test_client()


def test_shipped_library_is_well_formed():
    with open(DEFAULT_LIBRARY, encoding="utf-8") as f:
        data = json.load(f)
    recs = data["recipes"]
    assert data["count"] == len(recs) > 0
    assert len({r["url"] for r in recs}) == len(recs)
    for r in recs:
        assert r["name"] and r["ingredients"] and r["steps"], r["url"]
        assert r["cuisine"] in CANONICAL, (r["url"], r["cuisine"])
        assert 0 <= r["active_minutes"] <= r["total_minutes"], (r["url"], r["active_minutes"])


def test_shipped_library_loads_and_every_page_renders(shipped):
    app, client = shipped
    db = app.extensions["db"]
    with open(DEFAULT_LIBRARY, encoding="utf-8") as f:
        n = len(json.load(f)["recipes"])
    assert client.get("/api/health").get_json()["recipes"] == n == db.count()
    for path in ("/", "/recommend", "/recommend?q=cozy+dinner+under+45+min", "/plan", "/pantry", "/cart",
                 "/household", "/api/categories"):
        assert client.get(path).status_code == 200, path
    for c in db.facets()["cuisines"]:
        assert client.get("/?cuisine=" + c["value"]).status_code == 200
    for rid in db.all_ids():
        assert client.get(f"/recipe/{rid}").status_code == 200, rid
        assert client.get(f"/recipe/{rid}/cook").status_code == 200, rid
