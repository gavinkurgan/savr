"""Weekly meal planner: pick N meals that fit the mood AND share ingredients.

Greedy search: each night takes the recipe with the best
    fit score + shared-perishables bonus − repetition penalty
so half a bunch of cilantro or a tub of yogurt gets used twice instead of rotting.
"""
from __future__ import annotations

import datetime as dt

from .grocery import guess_aisle
from .pantry import is_staple
from .recommender import main_protein, score_recipe, taste_profile

PERISHABLE_AISLES = {"Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAIN_MEALS = {"dinner", "soup", "salad", "lunch"}


def perishables(r: dict) -> set[str]:
    out = set()
    for p in r.get("parsed_ingredients", []):
        item = p["item"]
        if item and not is_staple(item) and guess_aisle(item) in PERISHABLE_AISLES:
            out.add(item.replace("small ", "").replace("large ", "").replace("medium ", ""))
    return out


def _candidates(db, prefs: dict, exclude_ids: set) -> list[dict]:
    rows = db.search(tags=prefs.get("diet") or None, exclude_ingredients=prefs.get("exclude") or None,
                     meal_type=prefs.get("meal_type") if prefs.get("meal_type") else None, limit=1000)
    if not prefs.get("meal_type"):
        rows = [r for r in rows if r.get("meal_type") in MAIN_MEALS]
    return [r for r in rows if r["id"] not in exclude_ids]


def _pick(cands, chosen, prefs, profile, pantry_items, protein_counts, cuisine_counts):
    chosen_items = [perishables(c) for c in chosen]
    best = None
    for r in cands:
        if any(r["id"] == c["id"] for c in chosen):
            continue
        s, why = score_recipe(r, prefs, profile, pantry_items)
        mine = perishables(r)
        shared = sorted({x for items in chosen_items for x in items} & mine)
        s += min(len(shared), 3) * 1.5
        s -= cuisine_counts.get(r.get("cuisine"), 0) * 1.5 if r.get("cuisine") != "other" else 0
        prot = main_protein(r)
        s -= protein_counts.get(prot, 0) * 2.0 if prot else 0
        if best is None or s > best[0]:
            best = (s, r, why, shared)
    return best


def plan_week(db, prefs: dict, nights: int = 5, ratings=None, pantry_items=None,
              exclude_ids: set | None = None, locked: dict[int, int] | None = None, start: dt.date | None = None,
              servings: int | None = None) -> dict:
    """locked: {slot_index: recipe_id} kept as-is (used for swapping one night)."""
    nights = max(1, min(int(nights), 7))
    exclude_ids = set(exclude_ids or [])
    locked = locked or {}
    profile = taste_profile(db, ratings)
    cands = _candidates(db, prefs, exclude_ids)
    chosen: list[dict | None] = [None] * nights
    for slot, rid in locked.items():
        got = db.get_many([rid])
        if got and slot < nights:
            chosen[slot] = got[0]
    protein_counts, cuisine_counts = {}, {}
    for c in chosen:
        if c:
            protein_counts[main_protein(c)] = protein_counts.get(main_protein(c), 0) + 1
            cuisine_counts[c.get("cuisine")] = cuisine_counts.get(c.get("cuisine"), 0) + 1
    reasons = {}
    for slot in range(nights):
        if chosen[slot]:
            continue
        pick = _pick(cands, [c for c in chosen if c], prefs, profile, pantry_items, protein_counts, cuisine_counts)
        if not pick:
            break
        _, r, why, shared = pick
        chosen[slot] = r
        reasons[slot] = why[:3] + ([f"shares {', '.join(shared[:2])}"] if shared else [])
        protein_counts[main_protein(r)] = protein_counts.get(main_protein(r), 0) + 1
        cuisine_counts[r.get("cuisine")] = cuisine_counts.get(r.get("cuisine"), 0) + 1

    start = start or dt.date.today()
    days = []
    for i, r in enumerate(chosen):
        if not r:
            continue
        d = start + dt.timedelta(days=i)
        days.append({"slot": i, "date": d.isoformat(), "label": DAY_NAMES[d.weekday()],
                     "recipe_id": r["id"], "servings": servings or r.get("servings"),
                     "why": reasons.get(i, ["kept"])})
    return {"prefs": prefs, "nights": nights, "created": dt.datetime.now().isoformat(timespec="minutes"),
            "days": days, "shared": shared_ingredients(db.get_many([d["recipe_id"] for d in days]))}


def shared_ingredients(recipes: list[dict]) -> list[dict]:
    uses: dict[str, list[str]] = {}
    for r in recipes:
        for item in perishables(r):
            uses.setdefault(item, []).append(r["name"])
    return sorted(({"item": k, "recipes": v} for k, v in uses.items() if len(v) > 1),
                  key=lambda x: (-len(x["recipes"]), x["item"]))
