"""Categorize recipes (meal type, cuisine, style) and detect diet/allergen flags.

Allergen detection is keyword-based on ingredient lines. It is a helpful filter,
NOT a safety guarantee — the UI says so on every recipe.
"""
from __future__ import annotations

import re

MEAL_TYPES = {
    "breakfast": ["breakfast", "brunch", "pancake", "waffle", "omelet", "omelette", "frittata",
                  "oatmeal", "granola", "french toast", "scrambled", "hash brown", "breakfast burrito",
                  "smoothie bowl", "overnight oats", "quiche"],
    "dessert": ["dessert", "cake", "cookie", "brownie", "pie", "tart", "pudding", "cupcake",
                "cheesecake", "ice cream", "cobbler", "crisp", "fudge", "frosting", "blondie", "mousse"],
    "appetizer": ["appetizer", "starter", "dip", "bruschetta", "wings", "nachos", "finger food", "snack"],
    "side": ["side dish", "side", "slaw", "roasted vegetables", "mashed potatoes", "rice pilaf"],
    "salad": ["salad"],
    "soup": ["soup", "stew", "chili", "chowder", "bisque", "broth"],
    "drink": ["drink", "beverage", "cocktail", "smoothie", "lemonade", "latte"],
    "lunch": ["lunch", "sandwich", "wrap", "panini", "burger"],
    "dinner": ["dinner", "main course", "main dish", "entree", "entrée", "supper", "main"],
}

CUISINES = {
    "italian": ["italian", "pasta", "risotto", "parmesan", "marinara", "lasagna", "pesto",
                "gnocchi", "carbonara", "bolognese", "focaccia", "pizza"],
    "mexican": ["mexican", "taco", "tortilla", "enchilada", "salsa", "quesadilla", "burrito",
                "fajita", "chipotle", "pozole", "tamale", "guacamole"],
    "chinese": ["chinese", "stir-fry", "stir fry", "lo mein", "fried rice", "hoisin", "kung pao",
                "dumpling", "szechuan", "sichuan"],
    "japanese": ["japanese", "teriyaki", "miso", "ramen", "sushi", "katsu", "udon"],
    "thai": ["thai", "pad thai", "fish sauce", "lemongrass", "coconut curry", "green curry", "red curry"],
    "indian": ["indian", "curry", "garam masala", "tikka", "masala", "dal", "naan", "paneer", "biryani"],
    "mediterranean": ["mediterranean", "greek", "feta", "hummus", "tzatziki", "falafel", "pita", "shawarma"],
    "american": ["american", "bbq", "barbecue", "burger", "mac and cheese", "meatloaf", "cornbread",
                 "buffalo", "sloppy joe", "casserole"],
    "french": ["french", "gratin", "coq au vin", "ratatouille", "crepe", "crêpe", "béchamel", "bechamel"],
    "korean": ["korean", "gochujang", "kimchi", "bulgogi", "bibimbap"],
}

# Cuisines a site's recipeCuisine may name, as alias -> canonical. Anything else
# (keyword spam like "Bacon Cuisine Potato Cuisine…", diet labels, "Western",
# "General") is ignored and the cuisine is inferred from the recipe instead.
CUISINE_ALIASES = {
    **{k: k for k in CUISINES},
    "greek": "greek", "vietnamese": "vietnamese", "spanish": "spanish", "german": "german",
    "british": "british", "irish": "irish", "caribbean": "caribbean", "cajun": "cajun",
    "creole": "cajun", "louisiana": "cajun", "southern": "southern", "soul food": "southern",
    "middle eastern": "middle eastern", "lebanese": "middle eastern", "moroccan": "middle eastern",
    "turkish": "middle eastern", "persian": "middle eastern", "israeli": "middle eastern",
    "arabic": "middle eastern", "tex-mex": "tex-mex", "tex mex": "tex-mex", "filipino": "filipino",
    "indonesian": "southeast asian", "malaysian": "southeast asian", "singaporean": "southeast asian",
    "hawaiian": "hawaiian", "brazilian": "latin american", "argentinian": "latin american",
    "argentine": "latin american", "peruvian": "latin american", "cuban": "latin american",
    "latin american": "latin american", "latin": "latin american", "russian": "eastern european",
    "ukrainian": "eastern european", "polish": "eastern european", "hungarian": "eastern european",
    "eastern european": "eastern european", "ethiopian": "african", "african": "african",
    "jamaican": "caribbean", "asian": "asian", "european": "european",
}
# Longest aliases first so "tex-mex" beats "mexican" and "latin american" beats "american".
_CUISINE_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(CUISINE_ALIASES, key=len, reverse=True)) + r")\b")


def known_cuisine(value) -> str | None:
    """Map a site-supplied cuisine to a canonical name, or None if unrecognized."""
    if not value:
        return None
    text = " ".join(str(x) for x in value) if isinstance(value, list) else str(value)
    m = _CUISINE_ALIAS_RE.search(text.lower())
    return CUISINE_ALIASES[m.group(1)] if m else None


def infer_cuisine(head: str, ingredients: list[str]) -> str | None:
    """Guess cuisine from name/keywords (strong) or ingredients (needs 2+ signals,
    since one jar of feta doesn't make overnight oats Mediterranean)."""
    hits = _keyword_hits(head, CUISINES)
    if hits:
        return max(hits, key=hits.get)
    hits = _keyword_hits(" ".join(ingredients), CUISINES)
    if hits and max(hits.values()) >= 2:
        return max(hits, key=hits.get)
    return None


def normalize_cuisine(r: dict) -> str:
    """Clean the cuisine on an already-processed recipe (for libraries scraped
    before cuisines were validated)."""
    return (known_cuisine(r.get("cuisine")) or infer_cuisine(r.get("name", ""), r.get("ingredients", []))
            or "other")


STYLES = {
    "comfort": ["casserole", "mac and cheese", "creamy", "cheesy", "baked", "pot pie", "meatloaf",
                "mashed", "stew", "chili", "lasagna", "grilled cheese", "comfort"],
    "healthy": ["salad", "steamed", "quinoa", "kale", "spinach", "light", "healthy",
                "lean", "roasted vegetables", "low carb", "low-carb"],
    "spicy": ["spicy", "jalapeño", "jalapeno", "chipotle", "cayenne", "sriracha", "chili flakes",
              "red pepper flakes", "gochujang", "habanero", "hot sauce", "chile"],
    "one-pot": ["one-pot", "one pot", "sheet pan", "sheet-pan", "skillet", "slow cooker",
                "instant pot", "crock pot", "one pan"],
    "grill": ["grill", "grilled", "bbq", "barbecue"],
}

# allergen -> (include patterns, exclude patterns) checked per ingredient line
ALLERGENS = {
    "gluten": (
        [r"\bflour\b", r"\bwheat\b", r"\bbread", r"\bpasta\b", r"\bspaghetti\b", r"\bpenne\b",
         r"\bfettuccine\b", r"\blinguine\b", r"\bmacaroni\b", r"\bnoodles?\b", r"\bbarley\b",
         r"\brye\b", r"\bcouscous\b", r"\bpanko\b", r"\bcrackers?\b", r"\bsoy sauce\b",
         r"\bflour tortillas?\b", r"\bbuns?\b", r"\bcroutons?\b", r"\bbeer\b", r"\bseitan\b",
         r"\bpie crust\b", r"\bpuff pastry\b", r"\bbulgur\b", r"\bfarro\b", r"\bsemolina\b",
         r"\bcake mix\b", r"\bgraham\b", r"\borzo\b", r"\bgnocchi\b", r"\blasagna\b", r"\bpizza dough\b",
         r"\bramen\b", r"\budon\b", r"\bnaan\b", r"\bpita\b", r"\bcookies?\b", r"\boats\b"],
        [r"gluten[- ]free", r"\b(?:almond|coconut|rice|chickpea|cassava|tapioca|oat|corn) flour\b",
         r"\bcornbread\b", r"\brice noodles?\b", r"\bglass noodles?\b", r"\bcorn tortillas?\b",
         r"\btamari\b", r"\bbuckwheat\b", r"\bzucchini noodles\b"],
    ),
    "dairy": (
        [r"\bmilk\b", r"\bbutter\b", r"\bcheese\b", r"\bcream\b", r"\byogh?urt\b", r"\bparmesan\b",
         r"\bmozzarella\b", r"\bcheddar\b", r"\bricotta\b", r"\bfeta\b", r"\bghee\b",
         r"\bbuttermilk\b", r"\bsour cream\b", r"\bhalf[- ]and[- ]half\b", r"\bwhey\b",
         r"\bmascarpone\b", r"\bgruy[eè]re\b", r"\bparmigiano\b", r"\bpaneer\b", r"\bcasein\b"],
        [r"\b(?:almond|oat|coconut|soy|rice|cashew|plant[- ]based|dairy[- ]free|non[- ]dairy) (?:milk|cream|butter|cheese|yogh?urt)\b",
         r"\bpeanut butter\b", r"\b(?:almond|cashew|sunflower|apple) butter\b", r"\bcream of tartar\b",
         r"\bcocoa butter\b", r"\bvegan\b", r"\bcoconut cream\b"],
    ),
    "eggs": ([r"\beggs?\b", r"\begg (?:yolks?|whites?)\b", r"\bmayonnaise\b", r"\bmayo\b", r"\bmeringue\b"],
             [r"\begg[- ]free\b", r"\bvegan mayo", r"\bflax egg"]),
    "peanuts": ([r"\bpeanuts?\b", r"\bpeanut butter\b"], []),
    "tree_nuts": (
        [r"\balmonds?\b", r"\bwalnuts?\b", r"\bpecans?\b", r"\bcashews?\b", r"\bpistachios?\b",
         r"\bhazelnuts?\b", r"\bmacadamia\b", r"\bpine nuts?\b", r"\bbrazil nuts?\b", r"\bnutella\b",
         r"\balmond (?:flour|milk|butter|extract)\b", r"\bpraline\b", r"\bmarzipan\b"],
        [r"\bnut[- ]free\b"],
    ),
    "soy": ([r"\bsoy\b", r"\bsoya\b", r"\btofu\b", r"\bedamame\b", r"\bmiso\b", r"\btamari\b", r"\btempeh\b"], []),
    "fish": ([r"\bsalmon\b", r"\btuna\b", r"\bcod\b", r"\btilapia\b", r"\bhalibut\b", r"\banchov",
              r"\bfish\b", r"\bsardines?\b", r"\btrout\b", r"\bmahi\b", r"\bsnapper\b", r"\bworcestershire\b"], []),
    "shellfish": ([r"\bshrimp\b", r"\bprawns?\b", r"\bcrab\b", r"\blobster\b", r"\bscallops?\b",
                   r"\bclams?\b", r"\bmussels?\b", r"\boysters?\b", r"\bcrawfish\b", r"\bcrayfish\b"], []),
    "sesame": ([r"\bsesame\b", r"\btahini\b"], []),
}

MEAT = [r"\bchicken\b", r"\bbeef\b", r"\bpork\b", r"\bbacon\b", r"\bsausage", r"\bturkey\b",
        r"\blamb\b", r"\bham\b", r"\bveal\b", r"\bprosciutto\b", r"\bpancetta\b", r"\bsalami\b",
        r"\bpepperoni\b", r"\bchorizo\b", r"\bgelatin\b", r"\bsteak\b", r"\bground meat\b",
        r"\bbrisket\b", r"\bduck\b", r"\bmeatballs?\b", r"\blard\b", r"\bbone broth\b",
        r"\b(?:chicken|beef) (?:stock|broth|bouillon)\b", r"\bhot dogs?\b", r"\bribs\b"]
MEAT_EXCLUDE = [r"\b(?:vegan|vegetarian|plant[- ]based|meatless|impossible|beyond)\b", r"\bvegetable (?:stock|broth)\b"]

DIET_LABELS = {
    "gluten_free": "Gluten-free",
    "dairy_free": "Dairy-free",
    "egg_free": "Egg-free",
    "nut_free": "Nut-free",
    "peanut_free": "Peanut-free",
    "soy_free": "Soy-free",
    "shellfish_free": "Shellfish-free",
    "fish_free": "Fish-free",
    "sesame_free": "Sesame-free",
    "vegetarian": "Vegetarian",
    "vegan": "Vegan",
}

SCHEMA_DIETS = {
    "glutenfreediet": "gluten_free", "vegandiet": "vegan", "vegetariandiet": "vegetarian",
    "lowlactosediet": "dairy_free",
}


def _matches(line: str, includes, excludes) -> bool:
    if not any(re.search(p, line, re.I) for p in includes):
        return False
    if excludes:
        # Remove excluded phrases, then re-check (handles "almond milk and butter").
        stripped = line
        for p in excludes:
            stripped = re.sub(p, " ", stripped, flags=re.I)
        return any(re.search(p, stripped, re.I) for p in includes)
    return True


def detect_allergens(ingredients: list[str]) -> dict[str, list[str]]:
    """Return {allergen: [ingredient lines that triggered it]}."""
    found: dict[str, list[str]] = {}
    for line in ingredients:
        for allergen, (inc, exc) in ALLERGENS.items():
            if _matches(line, inc, exc):
                found.setdefault(allergen, []).append(line)
    return found


def contains_meat(ingredients: list[str]) -> bool:
    return any(_matches(line, MEAT, MEAT_EXCLUDE) for line in ingredients)


def diet_tags(ingredients: list[str], schema_diets=None) -> tuple[list[str], dict]:
    allergens = detect_allergens(ingredients)
    has_meat = contains_meat(ingredients)
    tags = []
    if "gluten" not in allergens: tags.append("gluten_free")
    if "dairy" not in allergens: tags.append("dairy_free")
    if "eggs" not in allergens: tags.append("egg_free")
    if "tree_nuts" not in allergens and "peanuts" not in allergens: tags.append("nut_free")
    if "peanuts" not in allergens: tags.append("peanut_free")
    if "soy" not in allergens: tags.append("soy_free")
    if "shellfish" not in allergens: tags.append("shellfish_free")
    if "fish" not in allergens: tags.append("fish_free")
    if "sesame" not in allergens: tags.append("sesame_free")
    vegetarian = not has_meat and "fish" not in allergens and "shellfish" not in allergens
    honey = any(re.search(r"\bhoney\b", i, re.I) for i in ingredients)
    if vegetarian:
        tags.append("vegetarian")
        if "dairy" not in allergens and "eggs" not in allergens and not honey:
            tags.append("vegan")
    # Site diet labels (schema.org suitableForDiet) are never trusted on their own:
    # ingredient evidence wins, so a "gluten-free" label on a recipe with flour is ignored.
    return tags, allergens


def _keyword_hits(text: str, table: dict[str, list[str]]) -> dict[str, int]:
    hits = {}
    for key, words in table.items():
        n = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text, re.I))
        if n:
            hits[key] = n
    return hits


def categorize(name: str, description: str, category, cuisine, keywords, ingredients: list[str],
               total_minutes: int | None = None) -> dict:
    def as_text(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        return str(v)

    head = " ".join([name or "", as_text(category), as_text(keywords)])
    body = " ".join([head, description or "", " ".join(ingredients)])

    # Meal type: site category first, then the name/keywords, then everything.
    meal = None
    for text in (as_text(category), f"{name} {as_text(keywords)}"):
        hits = _keyword_hits(text, MEAL_TYPES)
        if hits:
            meal = max(hits, key=lambda k: (hits[k], -list(MEAL_TYPES).index(k)))
            break
    if meal is None:
        meal = "dinner"

    cuisine_val = known_cuisine(cuisine) or infer_cuisine(head, ingredients)
    styles = sorted(_keyword_hits(body, STYLES))
    if total_minutes is not None and total_minutes <= 30:
        styles.append("quick")
    return {"meal_type": meal, "cuisine": cuisine_val or "other", "styles": sorted(set(styles))}
