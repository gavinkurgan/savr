"""Raw schema.org Recipe -> the clean recipe record the app stores and serves."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .durations import parse_iso_duration
from .ingredients import parse_ingredient
from .scraper import _image_url, parse_rating
from .simplifier import clean_text, estimate_times, is_hands_off, rate_difficulty, simplify_instructions
from .tagger import categorize, diet_tags, normalize_cuisine


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str) and "," in v:
        return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


def _servings(v) -> int | None:
    for item in _as_list(v):
        m = re.search(r"\d+", str(item))
        if m:
            return int(m.group())
    return None


def _author(v) -> str | None:
    v = v[0] if isinstance(v, list) and v else v
    if isinstance(v, dict):
        return v.get("name")
    return v if isinstance(v, str) else None


def process_recipe(raw: dict, url: str) -> dict:
    name = clean_text(raw.get("name") or "Untitled recipe")
    description = clean_text(raw.get("description") or "")
    # One-sentence description is plenty; the rest is usually the life story.
    description = re.split(r"(?<=[.!?])\s", description)[0][:220] if description else ""
    ingredients = [clean_text(i) for i in _as_list(raw.get("recipeIngredient") or raw.get("ingredients")) if clean_text(i)]
    steps = simplify_instructions(raw.get("recipeInstructions"))

    times = estimate_times(
        steps, len(ingredients),
        parse_iso_duration(raw.get("prepTime")),
        parse_iso_duration(raw.get("cookTime")),
        parse_iso_duration(raw.get("totalTime")),
    )
    difficulty = rate_difficulty(steps, ingredients, times)
    tags, allergens = diet_tags(ingredients, _as_list(raw.get("suitableForDiet")))
    cats = categorize(name, description, raw.get("recipeCategory"), raw.get("recipeCuisine"),
                      raw.get("keywords"), ingredients, times["total_minutes"])
    rating, count = parse_rating(raw)

    return {
        "url": url,
        "source": urlparse(url).netloc.replace("www.", "") if "://" in url else url,
        "name": name,
        "description": description,
        "image": _image_url(raw.get("image")),
        "author": _author(raw.get("author")),
        "servings": _servings(raw.get("recipeYield")) or 4,
        "rating": rating,
        "rating_count": count,
        "ingredients": ingredients,
        "parsed_ingredients": [parse_ingredient(i) for i in ingredients],
        "steps": steps,
        **times,
        **difficulty,
        **cats,
        "diet_tags": tags,
        "allergens": {k: v for k, v in allergens.items()},
    }


def reprocess_recipe(r: dict) -> dict:
    """Re-apply the offline cleanups to an already-processed library recipe, without
    re-scraping: cuisine validation, hands-off detection, hands-on minutes and
    difficulty. Site-provided prep/cook/total times are kept as they are."""
    for key in ("steps", "steps_rules"):
        for s in r.get(key) or []:
            s["hands_off"] = is_hands_off(s.get("action", ""), s.get("text", ""))
    base = r.get("steps_rules") or r.get("steps") or []
    total, prep = r.get("total_minutes") or 0, r.get("prep_minutes") or 0
    hands_off = min(sum(s.get("minutes") or 0 for s in base if s["hands_off"]), total)
    r["hands_off_minutes"] = int(hands_off)
    r["active_minutes"] = int(max(total - hands_off, prep))
    r.update(rate_difficulty(base, r.get("ingredients", []), r))
    r["cuisine"] = normalize_cuisine(r)
    return r
