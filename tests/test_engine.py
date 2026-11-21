"""Unit tests: parsing, simplifying, rating, tagging, recommending, grocery building."""
import pytest

from app.durations import parse_iso_duration, parse_text_minutes
from app.grocery import build_list, to_csv, to_instacart_payload, to_text
from app.ingredients import parse_ingredient, pretty_qty
from app.pipeline import process_recipe
from app.recommender import parse_mood, score_recipe
from app.scraper import (crawl, discover_links, extract_recipe_jsonld, parse_rating, parse_sitemap,
                         passes_rating)
from app.simplifier import classify_action, simplify_instructions
from app.tagger import detect_allergens, diet_tags
from conftest import fixture


# ---------------------------------------------------------------- durations
@pytest.mark.parametrize("value,expected", [
    ("PT1H30M", 90), ("PT45M", 45), ("P0DT2H", 120), ("PT90S", 2), ("30 minutes", 30), (None, None), ("", None),
])
def test_iso_duration(value, expected):
    assert parse_iso_duration(value) == expected


def test_text_minutes():
    assert parse_text_minutes("Bake 25 to 30 minutes") == 30
    assert parse_text_minutes("Simmer for 1 hour") == 60
    assert parse_text_minutes("Stir well") is None


# ---------------------------------------------------------------- ingredients
@pytest.mark.parametrize("line,qty,unit,item", [
    ("1 1/2 cups all-purpose flour", 1.5, "cup", "all-purpose flour"),
    ("½ tsp kosher salt", 0.5, "teaspoon", "kosher salt"),
    ("2 (14 oz) cans diced tomatoes", 2, "can", "diced tomatoes"),
    ("3 cloves garlic, minced", 3, "clove", "garlic"),
    ("1-2 tbsp olive oil", 2, "tablespoon", "olive oil"),
    ("2 large eggs", 2, None, "large eggs"),
    ("1 lb boneless chicken breast, cut into cubes", 1, "pound", "boneless chicken breast"),
    ("Salt and pepper, to taste", None, None, "salt and pepper"),
    ("1 &frac12; tbsp chopped parsley", 1.5, "tablespoon", "parsley"),
])
def test_parse_ingredient(line, qty, unit, item):
    p = parse_ingredient(line)
    assert p["quantity"] == qty
    assert p["unit"] == unit
    assert p["item"] == item


def test_pantry_flag_and_pretty_qty():
    assert parse_ingredient("Salt and pepper to taste")["pantry"] is True
    assert parse_ingredient("2 cups milk")["pantry"] is False
    assert pretty_qty(1.5) == "1 1/2"
    assert pretty_qty(0.333) == "1/3"
    assert pretty_qty(2.0) == "2"


# ---------------------------------------------------------------- scraping
def test_extract_from_graph_and_rating():
    raw = extract_recipe_jsonld(fixture("blog_recipe.html"))
    assert raw["name"] == "The BEST Lemon Parmesan Pasta"
    assert parse_rating(raw) == (4.62, 212)


def test_rating_normalized_from_10_point_scale():
    raw = extract_recipe_jsonld(fixture("ten_point.html"))
    rating, count = parse_rating(raw)
    assert rating == 4.5 and count == 30


def test_passes_rating_default_four_stars():
    assert passes_rating(4.0, 10)
    assert not passes_rating(3.9, 10)
    assert not passes_rating(4.5, 2, min_reviews=5)
    assert not passes_rating(None, 0)
    assert passes_rating(None, 0, allow_unrated=True)
    assert passes_rating(3.5, 10, min_rating=3.5)


def test_no_recipe_page_returns_none():
    assert extract_recipe_jsonld(fixture("listing.html")) is None


def test_discover_links_same_domain_dedup():
    links = discover_links(fixture("listing.html"), "https://example.com/dinners/")
    assert links == ["https://example.com/recipes/lemon-pasta/", "https://example.com/recipes/soggy-casserole/"]


def test_sitemap():
    xml = '<?xml version="1.0"?><urlset><url><loc>https://a.com/recipe/1</loc></url><url><loc>https://a.com/recipe/2</loc></url></urlset>'
    assert parse_sitemap(xml) == ["https://a.com/recipe/1", "https://a.com/recipe/2"]


def test_crawl_filters_by_rating():
    pages = {
        "https://example.com/dinners/": fixture("listing.html"),
        "https://example.com/recipes/lemon-pasta/": fixture("blog_recipe.html"),
        "https://example.com/recipes/soggy-casserole/": fixture("low_rated.html"),
    }

    def fake_fetch(url):
        if url not in pages:
            raise IOError("404")
        return pages[url]

    results = crawl(["https://example.com/dinners/"], fake_fetch, min_rating=4.0, max_recipes=10)
    status = {r.url: r.status for r in results}
    assert status["https://example.com/dinners/"] == "no_recipe"
    assert status["https://example.com/recipes/lemon-pasta/"] == "saved"
    assert status["https://example.com/recipes/soggy-casserole/"] == "low_rating"
    # Lowering the bar keeps it.
    results = crawl(["https://example.com/recipes/soggy-casserole/"], fake_fetch, min_rating=3.0, follow_links=False)
    assert results[0].status == "saved"


def test_crawl_reports_blocked_and_errors():
    def fetch(url):
        if "blocked" in url:
            raise PermissionError("robots.txt disallows")
        raise ConnectionError("boom")
    res = crawl(["https://x.com/blocked/recipe-1", "https://x.com/recipe-2"], fetch)
    assert [r.status for r in res] == ["blocked", "error"]


# ---------------------------------------------------------------- simplifier
def test_simplifier_strips_stories_and_tags_actions():
    raw = extract_recipe_jsonld(fixture("blog_recipe.html"))
    steps = simplify_instructions(raw["recipeInstructions"])
    texts = " ".join(s["text"] for s in steps).lower()
    assert "bronze-cut" not in texts and "i love" not in texts and "enjoy" not in texts
    assert "go ahead" not in texts and not texts.startswith("now")
    actions = [s["action"] for s in steps]
    assert "Cook" in actions and "Serve" in actions and "Mix" in actions
    assert {s["section"] for s in steps} == {"Pasta", "Sauce"}
    boil = next(s for s in steps if "spaghetti" in s["text"])
    assert boil["minutes"] == 9


@pytest.mark.parametrize("sentence,action", [
    ("Dice the onion.", "Cut"), ("Add the onions and sauté 5 minutes.", "Cook"),
    ("Bake for 25 minutes.", "Bake"), ("Whisk the eggs and sugar.", "Mix"),
    ("Let rest 10 minutes.", "Rest"), ("Garnish with basil.", "Serve"),
    ("Preheat oven to 400°F.", "Preheat"), ("Line a baking sheet with parchment.", "Prepare"),
])
def test_classify_action(sentence, action):
    assert classify_action(sentence) == action


def test_string_blob_instructions_are_split():
    steps = simplify_instructions("1. Chop the onion. 2. Fry it in oil for 5 minutes. 3. Serve.")
    assert [s["action"] for s in steps] == ["Cut", "Cook", "Serve"]


def test_process_recipe_full():
    r = process_recipe(extract_recipe_jsonld(fixture("blog_recipe.html")), "https://example.com/recipes/lemon-pasta/")
    assert r["source"] == "example.com"
    assert r["servings"] == 4
    assert (r["prep_minutes"], r["cook_minutes"], r["total_minutes"]) == (10, 15, 25)
    assert r["time_source"] == "site"
    assert 1 <= r["difficulty"] <= 3
    assert r["meal_type"] == "dinner" and r["cuisine"] == "italian"
    assert "quick" in r["styles"]
    assert "gluten_free" not in r["diet_tags"] and "vegetarian" in r["diet_tags"]
    assert set(r["allergens"]) >= {"gluten", "dairy"}
    assert "grandma" not in r["description"]


def test_times_estimated_when_missing():
    raw = {"name": "x", "recipeIngredient": ["1 onion", "2 cups rice"],
           "recipeInstructions": ["Chop the onion.", "Simmer the rice for 20 minutes.", "Let rest 5 minutes."]}
    r = process_recipe(raw, "https://a.com/r")
    assert r["time_source"] == "estimated"
    assert r["cook_minutes"] == 25 and r["hands_off_minutes"] == 25
    assert r["prep_minutes"] > 0


# ---------------------------------------------------------------- allergens / diets
def test_allergen_exclusions():
    found = detect_allergens(["1 cup almond milk", "2 tbsp peanut butter", "1 cup rice flour",
                              "1/4 tsp cream of tartar", "1 eggplant", "1 tbsp tamari"])
    assert "dairy" not in found and "gluten" not in found and "eggs" not in found
    assert {"peanuts", "tree_nuts", "soy"} <= set(found)


def test_vegan_detection():
    tags, _ = diet_tags(["1 can coconut milk", "2 cups chickpeas", "1 cup vegetable broth"])
    assert "vegan" in tags and "gluten_free" in tags
    tags, _ = diet_tags(["2 cups chicken broth", "1 cup rice"])
    assert "vegetarian" not in tags
    tags, _ = diet_tags(["1 tbsp honey", "1 cup oats"])
    assert "vegetarian" in tags and "vegan" not in tags


# ---------------------------------------------------------------- recommender
def test_parse_mood():
    p = parse_mood("I'm exhausted, want something cozy for dinner in under 40 minutes, gluten free, no mushrooms")
    assert p["effort"] == "low"
    assert p["max_minutes"] == 40
    assert p["meal_type"] == "dinner"
    assert "gluten_free" in p["diet"]
    assert "comfort" in p["styles"]
    assert "mushroom" in p["exclude"]


def test_parse_mood_variants():
    p = parse_mood("fancy italian date night, vegan")
    assert p["effort"] == "high" and p["cuisine"] == "italian"
    assert {"vegan", "vegetarian", "dairy_free", "egg_free"} <= set(p["diet"])
    assert parse_mood("quick lunch")["max_minutes"] == 30
    assert parse_mood("something with chicken, about 1 hour")["max_minutes"] == 60
    assert "chicken" in parse_mood("something with chicken")["include"]
    assert parse_mood("allergic to peanuts")["diet"] == ["peanut_free"]


def test_score_prefers_fit():
    quick = {"name": "Quick", "rating": 4.5, "rating_count": 100, "difficulty": 1, "total_minutes": 20,
             "active_minutes": 15, "hands_off_minutes": 0, "meal_type": "dinner", "cuisine": "mexican",
             "styles": ["quick"], "ingredients": []}
    slow = {**quick, "name": "Slow", "difficulty": 4, "total_minutes": 120, "active_minutes": 90}
    prefs = parse_mood("lazy dinner under 30 minutes")
    assert score_recipe(quick, prefs)[0] > score_recipe(slow, prefs)[0]


# ---------------------------------------------------------------- grocery
def _recipe(name, ingredients, servings=4):
    return {"name": name, "servings": servings, "parsed_ingredients": [parse_ingredient(i) for i in ingredients]}


def test_build_list_combines_and_scales():
    a = _recipe("A", ["2 tablespoons olive oil", "1 onion", "Salt to taste", "1 lb ground beef"])
    b = _recipe("B", ["1/4 cup olive oil", "2 onions", "8 oz ground beef"], servings=2)
    items = {i["item"]: i for i in build_list([(a, 8), (b, None)])}
    # A doubled: 4 tbsp (12 tsp) + B 1/4 cup (12 tsp) = 24 tsp = 1/2 cup
    assert items["olive oil"]["quantity"] == 0.5 and items["olive oil"]["unit"] == "cup"
    assert items["ground beef"]["quantity"] == 2.5 and items["ground beef"]["unit"] == "pound"
    assert "salt" not in items  # pantry skipped
    assert items["olive oil"]["recipes"] == ["A", "B"]


def test_build_list_have_items():
    a = _recipe("A", ["2 eggs", "1 cup milk"])
    names = [i["item"] for i in build_list([(a, None)], have_items=["milk"])]
    assert names == ["eggs"]


def test_exports():
    items = build_list([(_recipe("Tacos", ["1 lb ground beef", "8 corn tortillas", "1 cup shredded cheddar"]), None)])
    assert "[ ] 1 pound ground beef" in to_text(items)
    csv_text = to_csv(items)
    assert csv_text.splitlines()[0].startswith("Item,Quantity,Unit")
    assert "walmart.com/search?q=ground+beef" in csv_text
    payload = to_instacart_payload(items, "Tacos")
    assert payload["link_type"] == "shopping_list"
    li = {x["name"]: x for x in payload["line_items"]}
    assert li["ground beef"]["unit"] == "pound" and li["corn tortillas"]["unit"] == "each"


def test_leading_descriptor_commas_and_aisles():
    from app.grocery import guess_aisle
    assert parse_ingredient("6 bone-in, skin-on chicken thighs")["item"] == "skin-on chicken thighs"
    assert guess_aisle("creamy peanut butter") == "Pantry"
    assert guess_aisle("skin-on chicken thighs") == "Meat & Seafood"
    assert guess_aisle("eggplant") == "Produce"
