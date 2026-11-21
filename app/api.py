"""JSON API — used by the web UI and by any future iOS / SwiftUI client.

Household-scoped endpoints read the household from the `savr_household` cookie
or an `X-Household: <code>` header.
"""
from __future__ import annotations

import datetime as dt

from flask import Blueprint, Response, current_app, g, jsonify, request

from . import current_household, grocery, kroger
from .households import normalize_code
from .pantry import suggest as pantry_suggest
from .planner import plan_week, shared_ingredients
from .recommender import parse_mood, recommend

bp = Blueprint("api", __name__)


def db():
    return current_app.extensions["db"]


def hh_store():
    return current_app.extensions["households"]


def _summary(r: dict) -> dict:
    keys = ("id", "name", "description", "image", "source", "url", "rating", "rating_count",
            "difficulty", "difficulty_label", "prep_minutes", "cook_minutes", "total_minutes",
            "active_minutes", "hands_off_minutes", "cook_ratio", "meal_type", "cuisine", "styles",
            "diet_tags", "servings", "match_score", "match_reasons", "equipment", "pantry")
    return {k: r[k] for k in keys if k in r}


def search_args(args) -> dict:
    def num(name, cast=int):
        v = args.get(name)
        try:
            return cast(v) if v not in (None, "") else None
        except ValueError:
            return None
    tags = [t for t in args.get("diet", "").split(",") if t] + args.getlist("tag")
    exclude = [x.strip() for x in args.get("exclude", "").split(",") if x.strip()]
    return dict(q=args.get("q", ""), meal_type=args.get("meal_type") or None,
                cuisine=args.get("cuisine") or None, tags=tags or None,
                max_minutes=num("max_minutes"), max_difficulty=num("max_difficulty"),
                max_active=num("max_active"), min_rating=num("min_rating", float),
                exclude_ingredients=exclude or None, sort=args.get("sort", "rating"),
                limit=min(num("limit") or 60, 200), offset=num("offset") or 0)


def prefs_from_body(body: dict) -> dict:
    prefs = parse_mood(body.get("text", ""))
    for key in ("effort", "meal_type", "cuisine"):
        if body.get(key):
            prefs[key] = body[key]
    if body.get("max_minutes"):
        prefs["max_minutes"] = int(body["max_minutes"])
    if body.get("diet"):
        prefs["diet"] = sorted(set(prefs.get("diet", [])) | set(body["diet"]))
    if body.get("exclude"):
        prefs["exclude"] = sorted(set(prefs.get("exclude", [])) | set(body["exclude"]))
    return prefs


def household_context(hh) -> tuple[dict | None, list | None]:
    if not hh:
        return None, None
    return hh_store().ratings(hh["id"]), hh_store().pantry(hh["id"])


def need_household():
    hh = current_household(create=True)
    return hh


# ---------------------------------------------------------------- recipes
@bp.get("/health")
def health():
    return jsonify(ok=True, recipes=db().count())


@bp.get("/recipes")
def list_recipes():
    results = db().search(**search_args(request.args))
    return jsonify(count=len(results), results=[_summary(r) for r in results])


@bp.get("/recipes/<int:rid>")
def get_recipe(rid):
    r = db().get(rid)
    if not r:
        return jsonify(error="not found"), 404
    servings = request.args.get("servings", type=int)
    if servings and r.get("servings"):
        f = servings / r["servings"]
        for p in r["parsed_ingredients"]:
            if p["quantity"] is not None:
                p["quantity"] = round(p["quantity"] * f, 3)
        r["servings"] = servings
    return jsonify(r)


@bp.get("/categories")
def categories():
    return jsonify(db().facets())


@bp.post("/recommend")
def recommend_api():
    body = request.get_json(silent=True) or {}
    prefs = prefs_from_body(body)
    limit = max(1, min(int(body.get("limit", 5)), 50))
    ratings, pantry = household_context(current_household())
    results = recommend(db(), prefs, limit=limit, ratings=ratings,
                        pantry_items=pantry if body.get("use_pantry", True) else None)
    return jsonify(preferences=prefs, results=[_summary(r) for r in results])


# ---------------------------------------------------------------- household
@bp.get("/household")
def household_info():
    hh = need_household()
    return jsonify(code=hh["code"], name=hh["name"])


@bp.post("/household/join")
def household_join():
    code = normalize_code((request.get_json(silent=True) or {}).get("code"))
    hh = hh_store().get(code)
    if not hh:
        return jsonify(error="No household with that code. Check the spelling, e.g. basil-otter-1234."), 404
    g.set_household = hh["code"]
    return jsonify(code=hh["code"], name=hh["name"])


@bp.post("/household/new")
def household_new():
    hh = hh_store().create((request.get_json(silent=True) or {}).get("name") or "Our kitchen")
    g.set_household = hh["code"]
    return jsonify(code=hh["code"], name=hh["name"])


@bp.post("/household/name")
def household_name():
    hh = need_household()
    hh_store().rename(hh["id"], (request.get_json(silent=True) or {}).get("name", ""))
    return jsonify(ok=True)


# ---------------------------------------------------------------- ratings
@bp.post("/ratings")
def rate():
    body = request.get_json(silent=True) or {}
    rid = int(body.get("recipe_id", 0))
    if not db().get(rid):
        return jsonify(error="unknown recipe"), 404
    hh = need_household()
    hh_store().rate(hh["id"], rid, int(body.get("value", 0)), body.get("member") or "me")
    return jsonify(ratings=hh_store().ratings(hh["id"]).get(rid, {"score": 0, "up": 0, "down": 0, "members": {}}))


@bp.get("/ratings")
def list_ratings():
    hh = need_household()
    return jsonify(hh_store().ratings(hh["id"]))


# ---------------------------------------------------------------- pantry
@bp.get("/pantry")
def pantry_list():
    hh = need_household()
    return jsonify(items=hh_store().pantry(hh["id"]))


@bp.post("/pantry")
def pantry_add():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if isinstance(items, str):
        items = [x for x in items.replace("\n", ",").split(",")]
    hh = need_household()
    n = hh_store().pantry_add(hh["id"], items, body.get("use_by"))
    return jsonify(added=n, items=hh_store().pantry(hh["id"]))


@bp.delete("/pantry")
def pantry_remove():
    hh = need_household()
    hh_store().pantry_remove(hh["id"], request.args.get("id", type=int), request.args.get("item"))
    return jsonify(items=hh_store().pantry(hh["id"]))


@bp.get("/pantry/suggest")
def pantry_suggestions():
    hh = need_household()
    pantry = hh_store().pantry(hh["id"])
    recs = db().search(limit=1000, tags=[t for t in request.args.get("diet", "").split(",") if t] or None)
    out = pantry_suggest(recs, pantry, limit=request.args.get("limit", 12, type=int))
    return jsonify(results=[_summary(r) for r in out])


# ---------------------------------------------------------------- planner
@bp.get("/plan")
def plan_get():
    hh = need_household()
    plan = hh_store().plan(hh["id"])
    return jsonify(plan=_hydrate_plan(plan))


def _hydrate_plan(plan):
    if not plan:
        return None
    recipes = {r["id"]: r for r in db().get_many([d["recipe_id"] for d in plan["days"]])}
    plan["days"] = [{**d, "recipe": _summary(recipes[d["recipe_id"]])} for d in plan["days"] if d["recipe_id"] in recipes]
    return plan


@bp.post("/plan")
def plan_create():
    body = request.get_json(silent=True) or {}
    hh = need_household()
    prefs = prefs_from_body(body)
    ratings, pantry = household_context(hh)
    prefs["text"] = (body.get("text") or "")[:300]
    plan = plan_week(db(), prefs, nights=int(body.get("nights", 5)), ratings=ratings, pantry_items=pantry,
                     servings=body.get("servings"))
    plan["excluded"] = []
    hh_store().save_plan(hh["id"], plan)
    return jsonify(plan=_hydrate_plan(plan))


@bp.post("/plan/swap")
def plan_swap():
    body = request.get_json(silent=True) or {}
    hh = need_household()
    plan = hh_store().plan(hh["id"])
    if not plan:
        return jsonify(error="no plan yet"), 404
    slot = int(body.get("slot", -1))
    current = {d["slot"]: d["recipe_id"] for d in plan["days"]}
    if slot not in current:
        return jsonify(error="bad slot"), 400
    excluded = set(plan.get("excluded", [])) | {current[slot]}
    locked = {s: rid for s, rid in current.items() if s != slot}
    ratings, pantry = household_context(hh)
    servings = next((d["servings"] for d in plan["days"]), None)
    new = plan_week(db(), plan["prefs"], nights=plan["nights"], ratings=ratings, pantry_items=pantry,
                    exclude_ids=excluded - set(locked.values()), locked=locked, servings=servings,
                    start=dt.date.fromisoformat(plan["days"][0]["date"]))
    old_why = {d["slot"]: d["why"] for d in plan["days"]}
    for d in new["days"]:
        if d["slot"] != slot:
            d["why"] = old_why.get(d["slot"], d["why"])
    new["excluded"] = sorted(excluded)
    hh_store().save_plan(hh["id"], new)
    return jsonify(plan=_hydrate_plan(new))


# ---------------------------------------------------------------- grocery
def _selections(body):
    selections, recipes = [], []
    for sel in body.get("recipes", []):
        r = db().get(int(sel.get("id", 0)))
        if r:
            selections.append((r, sel.get("servings")))
            recipes.append(r)
    return selections, recipes


def _items_from_body(body):
    selections, recipes = _selections(body)
    if not selections:
        return None, None
    have = list(body.get("have") or [])
    if body.get("use_pantry", True):
        hh = current_household()
        if hh:
            have += [p["item"] for p in hh_store().pantry(hh["id"])]
    items = grocery.build_list(selections, exclude_pantry=body.get("exclude_pantry", True), have_items=have)
    return items, recipes


@bp.post("/grocery")
def grocery_list():
    """Body: {"recipes":[{"id":1,"servings":4}], "format":"json|text|csv|instacart|kroger|walmart|target",
              "exclude_pantry":true, "use_pantry":true, "have":["olive oil"], "title":"..."}"""
    body = request.get_json(silent=True) or {}
    items, recipes = _items_from_body(body)
    if items is None:
        return jsonify(error="no valid recipes selected"), 400
    title = body.get("title") or (recipes[0]["name"] if len(recipes) == 1 else f"savr: {len(recipes)} recipes")
    fmt = body.get("format", "json")
    if fmt == "text":
        return Response(grocery.to_text(items, title), mimetype="text/plain",
                        headers={"Content-Disposition": 'attachment; filename="savr-shopping-list.txt"'})
    if fmt == "csv":
        return Response(grocery.to_csv(items), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="savr-shopping-list.csv"'})
    if fmt == "instacart":
        payload = grocery.to_instacart_payload(items, title, recipes)
        return jsonify(grocery.create_instacart_link(payload) if body.get("create_link") else
                       {"ok": True, "payload": payload,
                        "endpoint": "POST https://connect.instacart.com/idp/v1/products/products_link"})
    if fmt == "kroger":
        return jsonify(grocery.to_kroger(items))
    if fmt in grocery.STORES:
        return jsonify(store=fmt, links=grocery.store_links(items, fmt))
    return jsonify(title=title, count=len(items), items=items,
                   shared=shared_ingredients(recipes) if len(recipes) > 1 else [])


@bp.post("/pantry/bought")
def pantry_bought():
    """After shopping: add every list item to the pantry (optionally with a use-by date)."""
    body = request.get_json(silent=True) or {}
    items, _ = _items_from_body({**body, "use_pantry": False})
    if items is None:
        return jsonify(error="no valid recipes selected"), 400
    hh = need_household()
    n = hh_store().pantry_add(hh["id"], [i["item"] for i in items], body.get("use_by"))
    return jsonify(added=n)


# ---------------------------------------------------------------- kroger
@bp.get("/kroger/status")
def kroger_status():
    hh = current_household()
    tok = hh_store().kroger(hh["id"]) if hh else None
    return jsonify(enabled=kroger.configured(), connected=bool(tok and tok.get("access_token")),
                   location_id=(tok or {}).get("location_id"), location_name=(tok or {}).get("location_name"))


@bp.get("/kroger/locations")
def kroger_locations():
    if not kroger.configured():
        return jsonify(error="Kroger is not configured on this server"), 503
    zip_code = (request.args.get("zip") or "").strip()
    if not zip_code.isdigit() or len(zip_code) != 5:
        return jsonify(error="enter a 5-digit ZIP"), 400
    try:
        return jsonify(locations=kroger.find_locations(zip_code))
    except kroger.KrogerError as e:
        return jsonify(error=str(e)), 502


@bp.post("/kroger/location")
def kroger_set_location():
    body = request.get_json(silent=True) or {}
    hh = need_household()
    hh_store().kroger_save(hh["id"], location_id=body.get("id"), location_name=body.get("name"))
    return jsonify(ok=True)


@bp.post("/kroger/cart")
def kroger_cart():
    if not kroger.configured():
        return jsonify(error="Kroger is not configured on this server"), 503
    body = request.get_json(silent=True) or {}
    items, _ = _items_from_body(body)
    if items is None:
        return jsonify(error="no valid recipes selected"), 400
    hh = need_household()
    try:
        return jsonify(kroger.push_list(hh_store(), hh["id"], items, body.get("modality", "PICKUP")))
    except kroger.KrogerError as e:
        return jsonify(error=str(e)), 401 if "connect" in str(e) else 502


@bp.post("/kroger/disconnect")
def kroger_disconnect():
    hh = need_household()
    hh_store().kroger_disconnect(hh["id"])
    return jsonify(ok=True)
