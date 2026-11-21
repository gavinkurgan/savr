"""Build one combined shopping list from several recipes and export it for pickup.

Reality check on "importable pickup orders": no big grocer accepts a generic file
upload into a pickup cart. The workable routes are:

* Instacart Developer Platform (IDP) — POST a list of line items, get back a
  link to an Instacart shopping-list page where the user picks a store (Kroger,
  Costco, Safeway, Aldi, Publix, Sprouts, … many offer pickup) and checks out.
  Needs a free IDP API key (INSTACART_API_KEY). Without a key we still return
  the exact JSON payload.
* Kroger Public API — sign in once, savr matches UPCs at your store and adds
  everything to the pickup cart (see kroger.py). Search links as a fallback.
* Walmart / Target / others — per-item search links + CSV / text checklist.
"""
from __future__ import annotations

import csv
import re
import io
import os
from urllib.parse import quote_plus

import requests

from .ingredients import VOLUME_TO_TSP, WEIGHT_TO_OZ, pretty_qty, scale_quantity

INSTACART_PROD = "https://connect.instacart.com"
INSTACART_DEV = "https://connect.dev.instacart.tools"

# our unit -> Instacart IDP unit string
INSTACART_UNITS = {
    "teaspoon": "teaspoon", "tablespoon": "tablespoon", "cup": "cup", "fl oz": "fl oz",
    "ounce": "ounce", "pound": "pound", "gram": "gram", "kilogram": "kilogram",
    "milliliter": "milliliter", "liter": "liter", "pint": "pint", "quart": "quart",
    "gallon": "gallon", "can": "can", "package": "package", "bunch": "bunch", "head": "head",
    "clove": "each", "slice": "each", "stick": "each", "jar": "each", "sprig": "each",
    None: "each",
}

STORES = {
    "walmart": "https://www.walmart.com/search?q={q}",
    "target": "https://www.target.com/s?searchTerm={q}",
    "kroger": "https://www.kroger.com/search?query={q}",
    "instacart": "https://www.instacart.com/store/s?k={q}",
    "amazon_fresh": "https://www.amazon.com/s?k={q}&i=amazonfresh",
}

# A shopper buys the whole thing, not "3 cloves".
WHOLE_ITEM_HINTS = {"clove": "head", "sprig": "bunch"}


def _family(unit):
    if unit in VOLUME_TO_TSP:
        return "volume"
    if unit in WEIGHT_TO_OZ:
        return "weight"
    return unit or "each"


def _best_volume(tsp: float) -> tuple[float, str]:
    if tsp >= 48 * 0.25:
        return tsp / 48, "cup"
    if tsp >= 3:
        return tsp / 3, "tablespoon"
    return tsp, "teaspoon"


def _best_weight(oz: float) -> tuple[float, str]:
    return (oz / 16, "pound") if oz >= 16 else (oz, "ounce")


def build_list(selections: list[tuple[dict, int | None]], exclude_pantry: bool = True,
               have_items: list[str] | None = None) -> list[dict]:
    """selections: [(recipe, desired_servings)]. Returns combined list items."""
    from .pantry import matches
    have = [h.strip().lower() for h in (have_items or []) if h.strip()]
    buckets: dict[tuple[str, str], dict] = {}
    for recipe, servings in selections:
        base = recipe.get("servings") or 4
        factor = (servings / base) if servings else 1.0
        for ing in recipe.get("parsed_ingredients", []):
            item = re.sub(r"^(?:large|medium|small|extra[- ]large)\s+", "", ing["item"] or "")
            if not item or (exclude_pantry and ing.get("pantry")) or any(matches(h, item) for h in have):
                continue
            fam = _family(ing["unit"])
            key = (item, fam)
            b = buckets.setdefault(key, {"item": item, "family": fam, "unit": ing["unit"],
                                         "amount": 0.0, "has_qty": False, "recipes": [], "raw": []})
            q = scale_quantity(ing["quantity"], factor)
            if q is not None:
                b["has_qty"] = True
                if fam == "volume":
                    b["amount"] += q * VOLUME_TO_TSP[ing["unit"]]
                elif fam == "weight":
                    b["amount"] += q * WEIGHT_TO_OZ[ing["unit"]]
                else:
                    b["amount"] += q
            if recipe["name"] not in b["recipes"]:
                b["recipes"].append(recipe["name"])
            b["raw"].append(ing["raw"])

    out = []
    for b in buckets.values():
        qty, unit = (b["amount"] if b["has_qty"] else None), b["unit"]
        if b["has_qty"] and b["family"] == "volume":
            qty, unit = _best_volume(b["amount"])
        elif b["has_qty"] and b["family"] == "weight":
            qty, unit = _best_weight(b["amount"])
        qty = round(qty, 2) if qty is not None else None
        name = _plural(b["item"]) if (unit is None and qty and qty > 1) else b["item"]
        display = " ".join(x for x in [pretty_qty(qty), _unit_label(unit, qty), name] if x)
        out.append({
            "item": b["item"],
            "quantity": qty,
            "unit": unit,
            "display": display,
            "shop_unit": WHOLE_ITEM_HINTS.get(unit),
            "recipes": b["recipes"],
            "source_lines": b["raw"],
            "aisle": "Pantry" if unit in ("can", "jar") else guess_aisle(b["item"]),
        })
    aisle_order = list(AISLES) + ["Other"]
    out.sort(key=lambda x: (aisle_order.index(x["aisle"]), x["item"]))
    return out


def _plural(item: str) -> str:
    head, _, last = item.rpartition(" ")
    if last.endswith(("s", "o")) or not last:
        return item
    if last.endswith(("ch", "sh", "x")):
        last += "es"
    elif last.endswith("y") and last[-2:-1] not in "aeiou":
        last = last[:-1] + "ies"
    else:
        last += "s"
    return f"{head} {last}".strip()


def _unit_label(unit, qty):
    if not unit:
        return ""
    plural = qty is not None and qty > 1 and unit not in ("fl oz",)
    return unit + ("s" if plural and not unit.endswith("s") else "")


AISLES = {
    "Produce": ["eggplant", "bell pepper", "chile", "onion", "garlic", "tomato", "lettuce", "spinach", "kale", "pepper", "carrot", "celery",
                "potato", "lemon", "lime", "apple", "banana", "berries", "avocado", "cilantro", "parsley",
                "basil", "ginger", "mushroom", "zucchini", "broccoli", "cucumber", "scallion", "green onion",
                "shallot", "jalapeño", "jalapeno", "herb", "thyme", "rosemary", "mint", "cabbage", "corn",
                "squash", "sweet potato", "asparagus", "green beans", "berries", "cauliflower", "orange", "fruit", "dill"],
    "Meat & Seafood": ["chicken", "beef", "pork", "bacon", "sausage", "turkey", "lamb", "shrimp",
                       "salmon", "fish", "steak", "ham", "cod", "tuna steak"],
    "Dairy & Eggs": ["milk", "butter", "cheese", "cream", "yogurt", "egg", "parmesan", "mozzarella",
                     "cheddar", "sour cream", "ricotta", "feta"],
    "Bakery": ["bread", "bun", "tortilla", "pita", "naan", "baguette", "rolls"],
    "Pantry": ["peanut butter", "almond butter", "black pepper", "red pepper flakes", "flour", "sugar", "oil", "vinegar", "rice", "pasta", "spaghetti", "noodle", "broth",
               "stock", "sauce", "beans", "lentil", "can", "baking", "vanilla", "cocoa", "chocolate",
               "honey", "syrup", "oats", "salt", "pepper", "spice", "cumin", "paprika", "oregano",
               "cinnamon", "chili powder", "soy", "nut", "seed", "breadcrumb", "panko", "tomato paste",
               "chickpea", "coconut milk", "powder", "masala", "salsa", "seasoning", "curry", "mustard", "ketchup", "mayonnaise", "yeast", "cornstarch"],
    "Frozen": ["frozen", "ice cream", "peas"],
}


def guess_aisle(item: str) -> str:
    # Check specific multi-word names first (e.g. "tomato paste" before "tomato").
    best, best_len = "Other", 0
    for aisle, words in AISLES.items():
        for w in words:
            if w in item and len(w) > best_len:
                best, best_len = aisle, len(w)
    return best


# ---------------------------------------------------------------- exports
def to_text(items: list[dict], title="Shopping list") -> str:
    lines = [title, "=" * len(title)]
    aisle = None
    for it in items:
        if it["aisle"] != aisle:
            aisle = it["aisle"]
            lines.append(f"\n{aisle}")
        lines.append(f"[ ] {it['display']}")
    return "\n".join(lines) + "\n"


def to_csv(items: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Item", "Quantity", "Unit", "Aisle", "For recipes", "Walmart search", "Target search"])
    for it in items:
        q = quote_plus(it["item"])
        w.writerow([it["item"], pretty_qty(it["quantity"]), it["unit"] or "", it["aisle"],
                    "; ".join(it["recipes"]), STORES["walmart"].format(q=q), STORES["target"].format(q=q)])
    return buf.getvalue()


def to_instacart_payload(items: list[dict], title: str, recipes: list[dict] | None = None,
                         link_type: str = "shopping_list") -> dict:
    """Body for POST /idp/v1/products/products_link (Instacart Developer Platform)."""
    line_items = []
    for it in items:
        li = {"name": it["item"], "display_text": it["display"]}
        if it["quantity"] is not None:
            li["quantity"] = it["quantity"]
            li["unit"] = INSTACART_UNITS.get(it["unit"], "each")
        line_items.append(li)
    payload = {"title": title[:100], "link_type": link_type, "line_items": line_items,
               "landing_page_configuration": {"enable_pantry_items": True} if link_type == "recipe" else {}}
    if recipes and len(recipes) == 1 and recipes[0].get("image"):
        payload["image_url"] = recipes[0]["image"]
    if not payload["landing_page_configuration"]:
        payload.pop("landing_page_configuration")
    return payload


def create_instacart_link(payload: dict, api_key: str | None = None, dev: bool | None = None) -> dict:
    api_key = api_key or os.environ.get("INSTACART_API_KEY")
    if not api_key:
        return {"ok": False, "reason": "INSTACART_API_KEY not set — payload returned for manual use",
                "payload": payload}
    dev = (os.environ.get("INSTACART_ENV", "development") != "production") if dev is None else dev
    base = INSTACART_DEV if dev else INSTACART_PROD
    try:
        resp = requests.post(f"{base}/idp/v1/products/products_link", json=payload, timeout=20,
                             headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json",
                                      "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return {"ok": True, "url": data.get("products_link_url"), "response": data}
    except requests.RequestException as e:
        return {"ok": False, "reason": str(e), "payload": payload}


def to_kroger(items: list[dict]) -> dict:
    """Search terms for Kroger's Products API (filter.term) + browser search links.
    Turning these into a cart needs a user OAuth token and UPC lookup — see README."""
    return {
        "note": "Kroger cart API needs UPCs + customer OAuth. Use these terms with "
                "GET /v1/products?filter.term=…&filter.locationId=…, then PUT /v1/cart/add.",
        "items": [{"term": it["item"], "quantity": max(1, round(it["quantity"] or 1)) if it["unit"] in (None, "can", "package") else 1,
                   "search_url": STORES["kroger"].format(q=quote_plus(it["item"]))} for it in items],
    }


def store_links(items: list[dict], store: str) -> list[dict]:
    tpl = STORES[store]
    return [{"item": it["display"], "url": tpl.format(q=quote_plus(it["item"]))} for it in items]
