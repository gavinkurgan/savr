"""'What do you feel like?' -> structured preferences -> ranked recipes.

parse_mood() turns plain English into preferences with simple rules, so it works
offline. Dietary needs are HARD filters (never relaxed); everything else is a
soft score, so you always get the closest matches even if nothing is perfect.
"""
from __future__ import annotations

import math
import re

from .quality import quality_score
from .tagger import CUISINES, MEAL_TYPES, STYLES

EFFORT_WORDS = {
    "low": ["lazy", "tired", "exhausted", "easy", "simple", "minimal", "low effort", "low-effort",
            "no effort", "not much work", "little work", "beginner", "no fuss", "fuss-free", "brain dead",
            "dump", "hands off", "hands-off", "set it and forget it", "busy", "wiped", "wiped out",
            "drained", "beat", "no energy", "long day", "worn out", "zero energy"],
    "high": ["project", "challenge", "challenging", "fancy", "impress", "date night", "elaborate",
             "show off", "gourmet", "weekend project", "ambitious", "advanced", "special occasion"],
    "medium": ["moderate", "some effort", "medium effort", "normal"],
}

DIET_WORDS = {
    "gluten_free": ["gluten free", "gluten-free", "celiac", "coeliac", "no gluten", "without gluten", "gf"],
    "dairy_free": ["dairy free", "dairy-free", "no dairy", "lactose", "without dairy", "non-dairy"],
    "egg_free": ["egg free", "egg-free", "no eggs", "no egg", "egg allergy", "without eggs"],
    "nut_free": ["nut free", "nut-free", "no nuts", "nut allergy", "tree nut", "without nuts"],
    "peanut_free": ["peanut free", "peanut-free", "no peanuts", "peanut allergy"],
    "shellfish_free": ["shellfish free", "no shellfish", "shellfish allergy", "no shrimp"],
    "fish_free": ["no fish", "fish allergy"],
    "soy_free": ["soy free", "soy-free", "no soy", "soy allergy"],
    "sesame_free": ["sesame free", "no sesame", "sesame allergy"],
    "vegetarian": ["vegetarian", "veggie", "meatless", "no meat", "meat-free", "meat free"],
    "vegan": ["vegan", "plant based", "plant-based"],
}

STYLE_WORDS = {
    "comfort": ["comfort", "cozy", "hearty", "warm", "cheesy", "indulgent", "rainy", "cold day"],
    "healthy": ["healthy", "light", "fresh", "clean", "low calorie", "lean", "nutritious"],
    "spicy": ["spicy", "kick", "fiery", "some heat"],
    "one-pot": ["one pot", "one-pot", "few dishes", "less cleanup", "sheet pan", "one pan", "minimal dishes"],
    "grill": ["grill", "bbq", "barbecue", "cookout"],
}


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", text) is not None


def parse_mood(text: str) -> dict:
    t = " " + (text or "").lower() + " "
    prefs: dict = {"diet": [], "styles": [], "exclude": [], "include": []}

    # ---- effort
    for level in ("high", "low", "medium"):
        if any(_has(t, w) for w in EFFORT_WORDS[level]):
            prefs["effort"] = level
            break

    # ---- time
    m = re.search(r"(?:under|less than|within|in|max(?:imum)?|no more than|about|around|<)\s*"
                  r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)", t)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b", t)
    if m:
        val = float(m.group(1))
        prefs["max_minutes"] = int(val * 60 if m.group(2).startswith("h") else val)
    elif any(_has(t, w) for w in ["quick", "fast", "hurry", "asap", "starving", "no time", "rushed"]):
        prefs["max_minutes"] = 30
    elif _has(t, "weeknight"):
        prefs["max_minutes"] = 45
    if any(_has(t, w) for w in ["slow", "all day", "slow cooker", "low and slow", "braise"]):
        prefs["styles"].append("slow")
        prefs.pop("max_minutes", None)

    # ---- meal type (explicit words beat food words)
    for meal in ("breakfast", "brunch", "lunch", "dinner", "dessert", "snack", "appetizer", "side",
                 "soup", "salad", "drink"):
        if _has(t, meal) or _has(t, meal + "s"):
            prefs["meal_type"] = {"brunch": "breakfast", "snack": "appetizer"}.get(meal, meal)
            break
    else:
        for meal, words in MEAL_TYPES.items():
            if any(_has(t, w) for w in words if len(w) > 3):
                prefs["meal_type"] = meal
                break

    # ---- cuisine
    for cuisine in CUISINES:
        if _has(t, cuisine):
            prefs["cuisine"] = cuisine
            break
    else:
        for cuisine, words in CUISINES.items():
            if any(_has(t, w) for w in words[1:] if len(w) > 3):
                prefs["cuisine"] = cuisine
                prefs["include"].extend(w for w in words[1:] if _has(t, w))
                break

    # ---- diet / allergens
    for tag, words in DIET_WORDS.items():
        if any(_has(t, w) for w in words):
            prefs["diet"].append(tag)
    if "vegan" in prefs["diet"]:
        prefs["diet"] = sorted(set(prefs["diet"]) | {"vegetarian", "dairy_free", "egg_free"})
    m = re.findall(r"allergic to ([a-z ]+?)(?:[,.;!]| and | but |$)", t)
    for item in m:
        for tag, words in DIET_WORDS.items():
            if any(_has(" " + item + " ", w.replace("no ", "")) for w in words):
                prefs["diet"].append(tag)
    prefs["diet"] = sorted(set(prefs["diet"]))

    # ---- style
    for style, words in STYLE_WORDS.items():
        if any(_has(t, w) for w in words):
            prefs["styles"].append(style)
    prefs["styles"] = sorted(set(prefs["styles"]))

    # ---- ingredients to avoid / include
    for m in re.finditer(r"\b(?:no|without|hate|hates|avoid|not|skip|allergic to)\s+([a-z]+(?:\s[a-z]+)?)", t):
        phrase = m.group(1).strip()
        first = phrase.split()[0]
        if first in {"time", "effort", "fuss", "dairy", "gluten", "meat", "nuts", "eggs", "egg", "soy",
                     "shellfish", "fish", "sesame", "peanuts", "much", "more", "too", "a", "the", "very",
                     "spicy", "fancy", "cooking", "oven", "dishes", "work"}:
            continue
        prefs["exclude"].append(first.rstrip("s") if len(first) > 4 else first)
    for m in re.finditer(r"\b(?:with|using|have|got|use up)\s+(?:some\s+)?([a-z]+)", t):
        w = m.group(1)
        if len(w) > 3 and w not in INCLUDE_STOPWORDS:
            prefs["include"].append(w)
    prefs["include"] = sorted(set(prefs["include"]))
    return prefs


INCLUDE_STOPWORDS = {
    "some", "kids", "family", "friends", "little", "time", "about", "only", "been", "lots", "kind",
    "kinda", "energy", "zero", "something", "enough", "plenty", "nothing", "leftover", "leftovers",
    "people", "guests", "minimal", "less", "more", "very", "much", "this", "that", "them", "what",
    "everything", "anything", "whatever", "extra", "company", "work", "effort", "dishes", "cleanup",
}

EFFORT_TARGET = {"low": 1.5, "medium": 3, "high": 4.5}


PROTEINS = ["chicken", "beef", "pork", "shrimp", "salmon", "fish", "turkey", "tofu", "lentil",
            "chickpea", "black bean", "bean", "egg", "sausage", "bacon"]


def main_protein(r: dict) -> str | None:
    text = " ".join([r.get("name", "")] + [p["item"] for p in r.get("parsed_ingredients", [])]).lower()
    return next((p for p in PROTEINS if re.search(rf"\b{p}", text)), None)


def taste_profile(db, ratings: dict[int, dict] | None) -> dict:
    """Summarize what a household liked/disliked so similar recipes can be nudged."""
    prof = {"cuisines": {}, "styles": {}, "proteins": {}, "ratings": ratings or {}}
    if not ratings:
        return prof
    for r in db.get_many(list(ratings)):
        sign = 1 if ratings[r["id"]]["score"] > 0 else -1 if ratings[r["id"]]["score"] < 0 else 0
        if not sign:
            continue
        for key, vals in (("cuisines", [r.get("cuisine")]), ("styles", r.get("styles", [])),
                          ("proteins", [main_protein(r)])):
            for v in vals:
                if v and v != "other":
                    prof[key][v] = prof[key].get(v, 0) + sign
    return prof


def household_bonus(r: dict, profile: dict | None) -> tuple[float, list[str]]:
    if not profile or not profile.get("ratings"):
        return 0.0, []
    score, why = 0.0, []
    own = profile["ratings"].get(r.get("id"))
    if own:
        if own["score"] > 0:
            score += 2.5; why.append("your household loved this")
        elif own["score"] < 0:
            score -= 15; why.append("thumbs-down in your household")
        else:
            score -= 1
    c = profile["cuisines"].get(r.get("cuisine"), 0)
    if c > 0 and not own:
        score += min(c, 3) * 0.8; why.append(f"you like {r['cuisine']}")
    elif c < 0:
        score += max(c, -3) * 0.8
    prot = main_protein(r)
    pv = profile["proteins"].get(prot, 0) if prot else 0
    score += max(-2, min(2, pv)) * 0.6
    sv = sum(profile["styles"].get(s, 0) for s in r.get("styles", []))
    score += max(-2, min(2, sv)) * 0.4
    return score, why


def score_recipe(r: dict, prefs: dict, profile: dict | None = None, pantry_items: list[dict] | None = None
                 ) -> tuple[float, list[str]]:
    """Higher is better. Returns (score, human-readable reasons)."""
    score, why = 0.0, []
    hb, hwhy = household_bonus(r, profile)
    score += hb
    why.extend(hwhy)
    if pantry_items:
        from .pantry import coverage
        cov = coverage(r, pantry_items)
        score += cov["ratio"] * 3 + len(cov["uses_expiring"]) * 2
        if cov["uses_expiring"]:
            why.append("uses up your " + ", ".join(cov["uses_expiring"][:2]))
        elif len(cov["have"]) >= 2:
            why.append(f"you have {len(cov['have'])} of the ingredients")

    # rating, with a pull toward 4.0 for recipes with few reviews (Bayesian average)
    rating = r.get("rating") or 4.0
    n = r.get("rating_count") or 0
    score += (quality_score(r.get("rating"), n) - 4.0) * 4
    if rating >= 4.7 and n >= 50:
        why.append(f"★ {rating} from {n:,} ratings")

    effort = prefs.get("effort")
    if effort:
        diff = abs(r.get("difficulty", 3) - EFFORT_TARGET[effort])
        score += 3 - diff * 1.5
        if diff <= 1:
            why.append(f"{r.get('difficulty_label', '').lower()} difficulty fits '{effort} effort'")
        if effort == "low":
            score -= max(0, (r.get("active_minutes", 0) - 25)) / 10
            if r.get("hands_off_minutes", 0) >= r.get("total_minutes", 1) * 0.5:
                score += 1; why.append("mostly hands-off")

    max_min = prefs.get("max_minutes")
    total = r.get("total_minutes") or 0
    if max_min:
        if total <= max_min:
            score += 3; why.append(f"ready in {total} min")
        else:
            score -= (total - max_min) / max(max_min, 10) * 6

    if prefs.get("meal_type"):
        if r.get("meal_type") == prefs["meal_type"]:
            score += 4; why.append(prefs["meal_type"])
        else:
            score -= 3
    if prefs.get("cuisine"):
        if r.get("cuisine") == prefs["cuisine"]:
            score += 3; why.append(f"{prefs['cuisine']} cuisine")
        else:
            score -= 1

    styles = set(r.get("styles", []))
    if "slow" in prefs.get("styles", []):
        if r.get("hands_off_minutes", 0) >= 60:
            score += 2; why.append("low-and-slow")
    for s in prefs.get("styles", []):
        if s in styles:
            score += 2; why.append(s)

    haystack = (r["name"] + " " + " ".join(r.get("ingredients", []))).lower()
    for inc in prefs.get("include", []):
        if inc in haystack:
            score += 2.5; why.append(f"uses {inc}")
    if prefs.get("diet"):
        why.append(", ".join(t.replace("_", "-") for t in prefs["diet"]))
    return round(score, 3), why


def recommend(db, prefs: dict, limit: int = 5, candidate_pool: int = 500, ratings: dict | None = None,
              pantry_items: list[dict] | None = None, exclude_ids: set | None = None) -> list[dict]:
    # Hard filters in SQL: diet tags and excluded ingredients.
    candidates = db.search(tags=prefs.get("diet") or None,
                           exclude_ingredients=prefs.get("exclude") or None,
                           limit=candidate_pool)
    profile = taste_profile(db, ratings)
    scored = []
    for r in candidates:
        if exclude_ids and r["id"] in exclude_ids:
            continue
        s, why = score_recipe(r, prefs, profile, pantry_items)
        scored.append((s, r, why))
    scored.sort(key=lambda x: (-x[0], x[1].get("total_minutes") or 0))
    out = []
    for s, r, why in scored[:limit]:
        out.append({**r, "match_score": s, "match_reasons": why[:4]})
    return out
