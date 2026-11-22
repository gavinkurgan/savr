"""Server-rendered pages (mobile-first, installable as a PWA on iOS)."""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

from flask import (Blueprint, abort, current_app, g, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import current_household, kroger
from .api import search_args
from .pantry import suggest as pantry_suggest
from .recommender import parse_mood, recommend
from .tagger import DIET_LABELS

bp = Blueprint("web", __name__)

DIET_FILTERS = ["gluten_free", "dairy_free", "egg_free", "nut_free", "vegetarian", "vegan", "shellfish_free"]
MEALS = ["breakfast", "lunch", "dinner", "soup", "salad", "side", "appetizer", "dessert", "drink"]
PAGE_SIZE = 60


def db():
    return current_app.extensions["db"]


def hh_store():
    return current_app.extensions["households"]


def _ratings():
    hh = current_household()
    return hh_store().ratings(hh["id"]) if hh else {}


@bp.get("/")
def index():
    args = search_args(request.args)
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    args.update(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    results = db().search(**args)
    total = db().search_count(**args)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)

    def page_url(n):
        qs = [(k, v) for k, v in request.args.items(multi=True) if k != "page"]
        if n > 1:
            qs.append(("page", str(n)))
        return "/?" + urlencode(qs) if qs else "/"
    return render_template("index.html", results=results, facets=db().facets(), args=request.args,
                           diet_filters=DIET_FILTERS, meals=MEALS, selected_diets=args["tags"] or [],
                           ratings=_ratings(), total=total, page=page, pages=pages,
                           first=args["offset"] + 1, page_url=page_url)


def _recipe_or_404(rid):
    r = db().get(rid)
    if not r:
        abort(404)
    return r


@bp.get("/recipe/<int:rid>")
def recipe(rid):
    r = _recipe_or_404(rid)
    servings = request.args.get("servings", type=int) or r["servings"]
    factor = servings / (r["servings"] or 4)
    for p in r["parsed_ingredients"]:
        p["scaled"] = round(p["quantity"] * factor, 3) if p["quantity"] is not None else None
    allergens = sorted(r.get("allergens", {}).keys())
    rating = _ratings().get(rid, {"score": 0, "up": 0, "down": 0, "members": {}})
    return render_template("recipe.html", r=r, servings=servings, factor=factor, allergens=allergens,
                           hh_rating=rating)


@bp.get("/recipe/<int:rid>/cook")
def cook(rid):
    r = _recipe_or_404(rid)
    return render_template("cook.html", r=r)


@bp.get("/recommend")
def recommend_page():
    mood = request.args.get("mood", "").strip()
    limit = request.args.get("limit", type=int) or 5
    diets = request.args.getlist("diet")
    prefs, results = None, []
    if mood or diets or request.args.get("effort") or request.args.get("max_minutes"):
        prefs = parse_mood(mood)
        if diets:
            prefs["diet"] = sorted(set(prefs["diet"]) | set(diets))
        for key in ("effort", "meal_type", "cuisine"):
            if request.args.get(key):
                prefs[key] = request.args[key]
        if request.args.get("max_minutes", type=int):
            prefs["max_minutes"] = request.args.get("max_minutes", type=int)
        hh = current_household()
        results = recommend(db(), prefs, limit=min(limit, 30),
                            ratings=hh_store().ratings(hh["id"]) if hh else None,
                            pantry_items=hh_store().pantry(hh["id"]) if hh else None)
    return render_template("recommend.html", mood=mood, prefs=prefs, results=results, limit=limit,
                           diet_filters=DIET_FILTERS, meals=MEALS, args=request.args, labels=DIET_LABELS,
                           ratings=_ratings())


@bp.get("/plan")
def plan_page():
    hh = current_household()
    plan = hh_store().plan(hh["id"]) if hh else None
    recipes = {}
    if plan:
        recipes = {r["id"]: r for r in db().get_many([d["recipe_id"] for d in plan["days"]])}
    return render_template("plan.html", plan=plan, recipes=recipes, diet_filters=DIET_FILTERS,
                           labels=DIET_LABELS)


@bp.get("/pantry")
def pantry_page():
    hh = current_household()
    items = hh_store().pantry(hh["id"]) if hh else []
    suggestions = pantry_suggest(db().search(limit=1000), items, limit=10) if items else []
    return render_template("pantry.html", items=items, suggestions=suggestions, ratings=_ratings())


@bp.get("/cart")
def cart():
    return render_template("cart.html")


@bp.get("/household")
def household_page():
    hh = current_household(create=True)
    ratings = hh_store().ratings(hh["id"])
    liked = db().get_many([rid for rid, v in ratings.items() if v["score"] > 0])
    disliked = db().get_many([rid for rid, v in ratings.items() if v["score"] < 0])
    k = hh_store().kroger(hh["id"])
    return render_template("household.html", hh=hh, liked=liked, disliked=disliked, ratings=ratings,
                           kroger_tok=k)


# ---------------------------------------------------------------- Kroger OAuth
@bp.get("/kroger/connect")
def kroger_connect():
    if not kroger.configured():
        abort(404)
    current_household(create=True)
    session["kroger_state"] = state = secrets.token_urlsafe(16)
    session["kroger_next"] = request.args.get("next", "/cart")
    return redirect(kroger.authorize_url(state, _redirect_uri()))


def _redirect_uri():
    import os
    return os.environ.get("KROGER_REDIRECT_URI") or url_for("web.kroger_callback", _external=True)


@bp.get("/kroger/callback")
def kroger_callback():
    if not kroger.configured():
        abort(404)
    if not request.args.get("state") or request.args.get("state") != session.pop("kroger_state", None):
        return render_template("message.html", title="Kroger sign-in failed",
                               body="The sign-in link expired or was opened in another browser. Try again."), 400
    if request.args.get("error") or not request.args.get("code"):
        return render_template("message.html", title="Kroger sign-in cancelled",
                               body=request.args.get("error_description", "No changes made.")), 400
    hh = current_household(create=True)
    try:
        tok = kroger.exchange_code(request.args["code"], _redirect_uri())
    except kroger.KrogerError as e:
        return render_template("message.html", title="Kroger sign-in failed", body=str(e)), 502
    hh_store().kroger_save(hh["id"], **tok)
    return redirect(session.pop("kroger_next", "/cart"))


# ---------------------------------------------------------------- PWA plumbing
@bp.get("/sw.js")
def service_worker():
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(current_app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")


@bp.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="icon-192.png"), code=301)


@bp.get("/healthz")
def healthz():
    return {"ok": True, "recipes": db().count()}
