"""Match pantry items to recipes: 'what can I make with what I have?'"""
from __future__ import annotations

import re

STAPLES = {"salt", "kosher salt", "sea salt", "black pepper", "pepper", "salt and pepper", "water",
           "olive oil", "vegetable oil", "oil", "cooking spray", "sugar", "all-purpose flour", "flour",
           "butter", "garlic powder", "ground cumin", "paprika", "dried oregano", "dried thyme"}

_STOP = {"fresh", "large", "small", "medium", "chopped", "ground", "dried", "whole", "boneless",
         "skinless", "skin-on", "baby", "extra", "virgin", "low-sodium", "unsalted", "shredded",
         "grated", "cooked", "frozen", "raw", "can", "cans", "of", "and", "the", "a", "crumbled"}


def _singular(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("oes"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def key_words(item: str) -> set[str]:
    words = re.findall(r"[a-z]+(?:-[a-z]+)?", item.lower())
    return {_singular(w) for w in words if w not in _STOP}


# Extra words that turn an ingredient into a different product:
# 'chicken' does not cover 'chicken broth', 'milk' does not cover 'coconut milk'.
PRODUCT_WORDS = {"broth", "stock", "bouillon", "sauce", "paste", "powder", "flour", "oil", "butter",
                 "juice", "extract", "vinegar", "noodle", "seasoning", "coconut", "almond", "oat", "soy",
                 "peanut", "cashew", "flake", "chip", "syrup", "zest", "mix", "starch"}


def matches(pantry_item: str, ingredient_item: str) -> bool:
    """'chicken' covers 'boneless chicken breast'; 'lemons' covers 'lemon'."""
    p, i = key_words(pantry_item), key_words(ingredient_item)
    if not p or not i:
        return False
    if p <= i:
        return not ((i - p) & PRODUCT_WORDS)
    if i <= p:
        return not ((p - i) & PRODUCT_WORDS)
    return False


def is_staple(item: str) -> bool:
    return item in STAPLES or key_words(item) <= {"salt", "pepper", "water", "oil"}


def coverage(recipe: dict, pantry: list[dict]) -> dict:
    needed = [p["item"] for p in recipe.get("parsed_ingredients", []) if p["item"] and not is_staple(p["item"])
              and not p.get("pantry")]
    have, missing, uses_expiring = [], [], []
    for item in needed:
        hit = next((p for p in pantry if matches(p["item"], item)), None)
        if hit:
            have.append(item)
            if hit.get("days_left") is not None and hit["days_left"] <= 3:
                uses_expiring.append(hit["item"])
        else:
            missing.append(item)
    total = len(needed) or 1
    return {"have": have, "missing": missing, "ratio": round(len(have) / total, 2),
            "uses_expiring": sorted(set(uses_expiring))}


def suggest(recipes: list[dict], pantry: list[dict], limit: int = 12) -> list[dict]:
    if not pantry:
        return []
    out = []
    for r in recipes:
        cov = coverage(r, pantry)
        if not cov["have"]:
            continue
        score = cov["ratio"] * 10 + len(cov["uses_expiring"]) * 3 - len(cov["missing"]) * 0.4 + (r.get("rating") or 4) * 0.3
        out.append({**r, "pantry": cov, "pantry_score": round(score, 2)})
    out.sort(key=lambda x: -x["pantry_score"])
    return out[:limit]
