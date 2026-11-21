"""Integration tests through the Flask test client."""
from conftest import recipe_id


def test_health_and_seed(client):
    r = client.get("/api/health").get_json()
    assert r["ok"] and r["recipes"] == 40  # 41 samples, the 3.6-star one is filtered out


def test_search_filters(client):
    res = client.get("/api/recipes?diet=gluten_free&max_minutes=30").get_json()["results"]
    assert res and all("gluten_free" in r["diet_tags"] and r["total_minutes"] <= 30 for r in res)
    res = client.get("/api/recipes?meal_type=breakfast").get_json()["results"]
    assert res and all(r["meal_type"] == "breakfast" for r in res)
    res = client.get("/api/recipes?q=chickpea&tag=vegan").get_json()["results"]
    assert "Vegan Coconut Chickpea Curry" in [r["name"] for r in res]
    assert all("vegan" in r["diet_tags"] for r in res)
    res = client.get("/api/recipes?sort=time&limit=3").get_json()["results"]
    assert [r["total_minutes"] for r in res] == sorted(r["total_minutes"] for r in res)
    res = client.get("/api/recipes?max_difficulty=1").get_json()["results"]
    assert res and all(r["difficulty"] == 1 for r in res)
    res = client.get("/api/recipes?exclude=garlic").get_json()["results"]
    assert res and all("Garlic" not in r["name"] for r in res)


def test_categories(client):
    f = client.get("/api/categories").get_json()
    assert any(m["value"] == "dinner" for m in f["meal_types"])
    assert any(t["value"] == "vegan" for t in f["tags"])


def test_recipe_detail_and_scaling(client):
    rid = recipe_id(client, "brownies")
    r = client.get(f"/api/recipes/{rid}?servings=32").get_json()
    sugar = next(p for p in r["parsed_ingredients"] if p["item"] == "sugar")
    assert sugar["quantity"] == 2.0  # 1 cup for 16 -> 2 cups for 32
    assert client.get("/api/recipes/9999").status_code == 404


def test_recommend_api(client):
    body = {"text": "lazy vegetarian dinner, gluten free, under 45 minutes", "limit": 3}
    data = client.post("/api/recommend", json=body).get_json()
    assert data["preferences"]["effort"] == "low"
    assert 1 <= len(data["results"]) <= 3
    for r in data["results"]:
        assert {"vegetarian", "gluten_free"} <= set(r["diet_tags"])
    assert data["results"][0]["total_minutes"] <= 45
    assert data["results"][0]["match_reasons"]


def test_recommend_never_breaks_hard_diet_filters(client):
    data = client.post("/api/recommend", json={"text": "fancy dinner", "diet": ["vegan"], "limit": 10}).get_json()
    assert data["results"] and all("vegan" in r["diet_tags"] for r in data["results"])


def test_grocery_formats(client):
    ids = [r["id"] for r in client.get("/api/recipes?limit=2").get_json()["results"]]
    body = {"recipes": [{"id": ids[0], "servings": 8}, {"id": ids[1]}]}
    j = client.post("/api/grocery", json=body).get_json()
    assert j["count"] > 3 and all("aisle" in i for i in j["items"])
    for fmt, mime in [("csv", "text/csv"), ("text", "text/plain")]:
        resp = client.post("/api/grocery", json={**body, "format": fmt})
        assert resp.status_code == 200 and resp.mimetype == mime
    ic = client.post("/api/grocery", json={**body, "format": "instacart"}).get_json()
    assert ic["payload"]["line_items"]
    ic = client.post("/api/grocery", json={**body, "format": "instacart", "create_link": True}).get_json()
    assert ic["ok"] is False and "INSTACART_API_KEY" in ic["reason"]  # no key in tests
    kr = client.post("/api/grocery", json={**body, "format": "kroger"}).get_json()
    assert kr["items"][0]["search_url"].startswith("https://www.kroger.com/search?query=")
    wm = client.post("/api/grocery", json={**body, "format": "walmart"}).get_json()
    assert wm["links"][0]["url"].startswith("https://www.walmart.com/search?q=")
    assert client.post("/api/grocery", json={"recipes": []}).status_code == 400


def test_scraping_is_not_exposed_to_users(client):
    assert client.post("/api/scrape", json={"urls": ["https://x.com"]}).status_code in (404, 405)
    assert client.get("/admin").status_code == 404
    assert client.delete("/api/recipes/1").status_code == 405


def test_pages_render(client):
    for path in ["/", "/?meal_type=dinner&tag=gluten_free&max_minutes=30", "/recommend",
                 "/recommend?mood=quick+vegetarian+lunch", "/cart", "/recipe/1", "/recipe/1/cook",
                 "/recipe/1?servings=8", "/plan", "/pantry", "/household",
                 "/manifest.webmanifest", "/sw.js", "/healthz"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
    html = client.get("/recommend?mood=lazy+vegan+dinner").get_data(as_text=True)
    assert "What I took from that" in html and "Vegan" in html
    assert client.get("/recipe/999").status_code == 404
    assert "savr" in client.get("/").get_data(as_text=True)
    assert "savr" in client.get("/manifest.webmanifest").get_data(as_text=True)


def test_recipe_page_has_both_voices_and_equipment(client):
    rid = recipe_id(client, "brownies")
    html = client.get(f"/recipe/{rid}").get_data(as_text=True)
    assert 'class="v-std"' in html and 'class="v-unh"' in html
    assert "Unhinged" in html and "You'll need" in html
    cook = client.get(f"/recipe/{rid}/cook").get_data(as_text=True)
    assert "CookMode.init(" in cook and "v-unh" in cook
