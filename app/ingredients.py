"""Parse free-text ingredient lines into quantity / unit / item."""
from __future__ import annotations

import html
import re
from fractions import Fraction

UNICODE_FRACTIONS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5",
    "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅛": "1/8",
    "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}

# canonical unit -> aliases
UNIT_ALIASES = {
    "teaspoon": ["teaspoons", "teaspoon", "tsps", "tsp", "t"],
    "tablespoon": ["tablespoons", "tablespoon", "tbsps", "tbsp", "tbs", "tbl", "T"],
    "cup": ["cups", "cup", "c"],
    "fl oz": ["fluid ounces", "fluid ounce", "fl. oz.", "fl oz", "fl. oz"],
    "ounce": ["ounces", "ounce", "oz.", "oz"],
    "pound": ["pounds", "pound", "lbs.", "lbs", "lb.", "lb"],
    "gram": ["grams", "gram", "g"],
    "kilogram": ["kilograms", "kilogram", "kg"],
    "milliliter": ["milliliters", "millilitres", "milliliter", "millilitre", "ml"],
    "liter": ["liters", "litres", "liter", "litre", "l"],
    "pint": ["pints", "pint", "pt"],
    "quart": ["quarts", "quart", "qt"],
    "gallon": ["gallons", "gallon", "gal"],
    "clove": ["cloves", "clove"],
    "can": ["cans", "can"],
    "jar": ["jars", "jar"],
    "package": ["packages", "package", "pkg", "pkgs", "packet", "packets"],
    "bunch": ["bunches", "bunch"],
    "stick": ["sticks", "stick"],
    "slice": ["slices", "slice"],
    "head": ["heads", "head"],
    "sprig": ["sprigs", "sprig"],
    "pinch": ["pinches", "pinch"],
    "dash": ["dashes", "dash"],
    "large": ["large"],
    "medium": ["medium"],
    "small": ["small"],
}
_ALIAS_TO_UNIT = {}
for canon, aliases in UNIT_ALIASES.items():
    for a in aliases:
        _ALIAS_TO_UNIT[a if a in ("T", "t") else a.lower()] = canon

# Size words are treated as "each" for shopping but kept in the item text.
SIZE_UNITS = {"large", "medium", "small"}

VOLUME_TO_TSP = {"teaspoon": 1, "tablespoon": 3, "cup": 48, "fl oz": 6, "pint": 96,
                 "quart": 192, "gallon": 768, "milliliter": 0.202884, "liter": 202.884}
WEIGHT_TO_OZ = {"ounce": 1, "pound": 16, "gram": 0.035274, "kilogram": 35.274}

PREP_WORDS = (
    r"finely|roughly|coarsely|thinly|freshly|chopped|diced|minced|sliced|grated|shredded|"
    r"crushed|peeled|seeded|cubed|halved|quartered|softened|melted|beaten|divided|packed|"
    r"rinsed|drained|trimmed|julienned|zested|juiced|room temperature|at room temperature|"
    r"to taste|optional|for serving|for garnish|plus more|or more|as needed"
)
_PREP_RE = re.compile(rf"\b(?:{PREP_WORDS})\b", re.IGNORECASE)

PANTRY_STAPLES = {
    "salt", "kosher salt", "sea salt", "black pepper", "pepper", "salt and pepper",
    "water", "ice", "cooking spray", "nonstick cooking spray",
}

_NUM = r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?"
_QTY_RE = re.compile(rf"^\s*(?P<q1>{_NUM})(?:\s*(?:-|–|to)\s*(?P<q2>{_NUM}))?\s*")
_PAREN_SIZE_RE = re.compile(r"^\(\s*(?P<size>[^)]*)\)\s*")


def _to_number(s: str) -> float:
    s = s.strip()
    total = Fraction(0)
    for part in s.split():
        total += Fraction(part)
    return float(total)


def _normalize_fractions(text: str) -> str:
    for k, v in UNICODE_FRACTIONS.items():
        # "1½" -> "1 1/2"
        text = re.sub(rf"(\d){k}", rf"\1 {v}", text)
        text = text.replace(k, v)
    return text.replace("⁄", "/")


def clean_item_name(item: str, packaged: bool = False) -> str:
    item = re.sub(r"\([^)]*\)", "", item)          # drop parentheticals
    parts = [x.strip() for x in item.split(",")]   # "onion, diced" -> "onion"
    # ...but "bone-in, skin-on chicken thighs": skip leading one-word descriptors.
    while len(parts) > 1 and re.fullmatch(r"[\w-]+", parts[0]) and (
            "-" in parts[0] or parts[0].lower() in {"boneless", "skinless", "fresh", "frozen", "raw"}):
        parts.pop(0)
    item = parts[0]
    item = re.sub(r"\s+or\s+.*$", "", item)        # "butter or margarine" -> "butter"
    if not packaged:  # "a can of diced tomatoes" is a product; "diced onion" is prep
        item = _PREP_RE.sub("", item)
    item = re.sub(r"^\s*(?:of)\s+", "", item)
    item = re.sub(r"\s+", " ", item).strip(" -,.;:*")
    return item.lower()


def parse_ingredient(line: str) -> dict:
    raw = html.unescape(str(line)).strip()
    text = _normalize_fractions(raw)
    text = re.sub(r"\s+", " ", text)
    qty = None
    unit = None
    size_note = None
    rest = text

    m = _QTY_RE.match(rest)
    if m:
        try:
            qty = _to_number(m.group("q2") or m.group("q1"))
        except (ValueError, ZeroDivisionError):
            qty = None
        rest = rest[m.end():]

    pm = _PAREN_SIZE_RE.match(rest)
    if pm:  # "2 (14 oz) cans"
        size_note = pm.group("size")
        rest = rest[pm.end():]

    # Unit: try two-word then one-word aliases (case-sensitive for T/t).
    words = rest.split(" ")
    for n in (3, 2, 1):
        if len(words) >= n:
            cand = " ".join(words[:n]).rstrip(".")
            key = cand if cand in ("T", "t") else cand.lower()
            if key in _ALIAS_TO_UNIT:
                unit = _ALIAS_TO_UNIT[key]
                rest = " ".join(words[n:])
                break

    item = clean_item_name(rest, packaged=unit in ("can", "jar", "package"))
    if unit in SIZE_UNITS:
        item = f"{unit} {item}".strip() if item else unit
        unit = None
    if not item:
        item = clean_item_name(text)

    pantry = item in PANTRY_STAPLES or bool(re.search(r"\bto taste\b", raw, re.I)) and qty is None
    return {
        "raw": raw,
        "quantity": round(qty, 3) if qty is not None else None,
        "unit": unit,
        "item": item,
        "size_note": size_note,
        "pantry": pantry,
    }


def scale_quantity(qty: float | None, factor: float) -> float | None:
    if qty is None:
        return None
    return round(qty * factor, 3)


def pretty_qty(qty: float | None) -> str:
    if qty is None:
        return ""
    whole = int(qty)
    frac = qty - whole
    best = min([(0, ""), (1 / 8, "1/8"), (1 / 4, "1/4"), (1 / 3, "1/3"), (1 / 2, "1/2"),
                (2 / 3, "2/3"), (3 / 4, "3/4"), (1, "")], key=lambda p: abs(p[0] - frac))
    if best[0] == 1:
        whole, best = whole + 1, (0, "")
    if abs(best[0] - frac) > 0.06:
        return f"{qty:g}"
    if whole and best[1]:
        return f"{whole} {best[1]}"
    return best[1] or str(whole)
