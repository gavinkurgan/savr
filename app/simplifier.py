"""Turn wordy recipe instructions into short, action-tagged steps and rate effort.

Everything here is deterministic and rule-based so it runs offline and is easy to
test. An LLM rewriter can be plugged in later (see README "Suggested features").
"""
from __future__ import annotations

import html
import re

from .durations import parse_text_minutes

# Ordered: first match wins, so more specific verbs come first.
ACTIONS: list[tuple[str, list[str]]] = [
    ("Preheat", ["preheat", "heat the oven", "heat oven"]),
    ("Cut", ["chop", "dice", "slice", "mince", "cut", "julienne", "grate", "peel",
             "shred", "quarter", "halve", "trim", "zest", "core", "cube", "crush"]),
    ("Bake", ["bake", "roast", "broil", "toast"]),
    ("Grill", ["grill", "barbecue", "char"]),
    ("Cook", ["sauté", "saute", "fry", "simmer", "boil", "brown", "sear", "cook", "heat",
              "melt", "reduce", "steam", "poach", "blanch", "caramelize", "deglaze", "stir-fry"]),
    ("Mix", ["whisk", "stir", "combine", "mix", "fold", "beat", "toss", "blend", "puree",
             "purée", "knead", "cream", "mash", "pour", "add"]),
    ("Rest", ["rest", "cool", "chill", "refrigerate", "freeze", "let stand", "let sit",
              "set aside", "marinate", "proof", "rise", "soak"]),
    ("Serve", ["serve", "garnish", "plate", "top with", "sprinkle", "drizzle", "enjoy"]),
    ("Prepare", ["prepare", "line", "grease", "measure", "rinse", "drain", "season",
                 "pat dry", "place", "arrange", "spread", "transfer", "remove", "spray",
                 "wash", "assemble", "roll", "shape", "form", "fill", "cover", "pat"]),
]
HANDS_OFF_ACTIONS = {"Bake", "Rest"}
HANDS_OFF_HINTS = re.compile(
    r"\b(?:simmer(?:s|ing)?|slow[- ]cook\w*|brais(?:e|es|ing)|bak(?:e|es|ing)|roast(?:s|ing)?|"
    r"marinat(?:e|es|ing)|chill(?:s|ing)?|refrigerat(?:e|es|ing)|ris(?:e|es|ing)|proof(?:s|ing)?|"
    r"brin(?:e|es|ing)|soak(?:s|ing)?|freez(?:e|es|ing)|"
    r"let(?:\s+\S+){0,4}?\s+(?:sit|stand|rest|cool|chill|rise|marinate|soak|set)|"
    r"pressure cook\w*|cook on (?:low|high)|slow cooker|crock ?pot|overnight)\b", re.I)
COOK_ACTIONS = {"Bake", "Cook", "Grill", "Preheat"}

# Sentences matching these are blog chatter, not instructions.
FLUFF_PATTERNS = [
    r"\b(?:i|we)\s+(?:love|like|think|prefer|always|usually|find|recommend|made|used)\b",
    r"\bmy (?:family|kids|husband|wife|mom|dad|grandma|grandmother|grandpa|nonna|aunt|friends?|partner)\b",
    r"^(?:this|that) (?:is|was) (?:the|my|our|a)\b", r"\bsecret\b", r"^seriously\b",
    r"\b(?:trust me|you won'?t regret|so good|so easy|yum|delicious|amazing|perfect for)\b",
    r"\b(?:see|check out) (?:the )?(?:notes|post|video|tips|faq|above|below)\b",
    r"\b(?:pin|subscribe|newsletter|affiliate|sponsored|comment below|leave a review|rate this)\b",
    r"\b(?:enjoy|bon app[eé]tit)!?\s*$",
    r"\b(?:don'?t worry|feel free|if you like|if desired|if you want|you can also)\b",
]
_FLUFF_RE = re.compile("|".join(FLUFF_PATTERNS), re.I)

FILLER_PHRASES = [
    r"\bgo ahead and\b", r"\byou(?:'ll| will) want to\b", r"\byou(?:'ll| will) need to\b",
    r"\bsimply\b", r"\bjust\b", r"\bnow,?\s+", r"\bnext,?\s+", r"\bthen,?\s+", r"\bfinally,?\s+",
    r"\bonce that(?:'s| is) done,?\s*", r"\bmake sure (?:to|that you)\b", r"\bbe sure to\b",
    r"\bcarefully\b", r"\bnicely\b", r"\breally\b", r"\bin order to\b", r"\bat this point,?\s*",
    r"\bafter that,?\s*", r"\bstart by\b", r"\bbegin by\b",
]
_FILLER_RE = re.compile("|".join(FILLER_PHRASES), re.I)

ADVANCED_TECHNIQUES = [
    "temper", "fold in", "proof", "knead", "deglaze", "emulsif", "caramel", "sous vide",
    "deep fry", "deep-fry", "flambé", "flambe", "laminate", "julienne", "brunoise", "confit",
    "reduce by half", "candy thermometer", "water bath", "bain-marie", "double boiler",
    "butterfly", "debone", "truss", "pipe", "blind bake", "souffl", "roux", "clarif",
]


def clean_text(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    text = clean_text(text)
    # protect common abbreviations / decimals
    protected = re.sub(r"\b(approx|tsp|tbsp|oz|lb|lbs|min|mins|hr|hrs|e\.g|i\.e|vs|deg)\.", r"\1<DOT>", text, flags=re.I)
    protected = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", protected)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", protected)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


WEAK_VERBS = {"add", "pour", "place", "transfer", "remove", "cover", "season", "spread",
              "arrange", "top with", "sprinkle", "drizzle", "fill", "heat"}


def classify_action(sentence: str) -> str:
    """Pick the action whose verb appears earliest, ignoring generic verbs like
    'add' or 'transfer' when a more telling verb ('cook', 'bake') is present."""
    s = sentence.lower()
    # Tool names aren't actions: "roasting pan", "baking sheet", "mixing bowl"...
    s = re.sub(r"\b(?:roasting|baking|frying|mixing|cutting|cooking|serving|grill) (?:pan|sheet|dish|bowl|board|spray|rack|plate|powder|soda)s?\b", "x", s)
    strong = (None, 10**9)
    weak = (None, 10**9)
    for action, verbs in ACTIONS:
        for v in verbs:
            m = re.search(rf"\b{re.escape(v)}\w{{0,3}}\b", s)
            if not m:
                continue
            slot = weak if v in WEAK_VERBS else strong
            if m.start() < slot[1]:
                if v in WEAK_VERBS:
                    weak = (action, m.start())
                else:
                    strong = (action, m.start())
    return strong[0] or weak[0] or "Prepare"


def is_fluff(sentence: str) -> bool:
    s = sentence.strip()
    if len(s) < 4:
        return True
    if _FLUFF_RE.search(s):
        # Keep it if it clearly carries an instruction with a time/temp.
        has_instruction = re.search(r"\d+\s*(?:°|degrees|minutes?|mins?|hours?)", s, re.I)
        return not has_instruction
    return False


def tighten(sentence: str) -> str:
    s = _FILLER_RE.sub("", sentence)
    s = re.sub(r"\((?:[^()]*?\b(?:optional|i |we |see |note|tip)[^()]*)\)", "", s, flags=re.I)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,;")
    s = re.sub(r"!+$", ".", s)
    if s and not s.endswith((".", "?")):
        s += "."
    return s[:1].upper() + s[1:] if s else s


def _flatten_instructions(instructions) -> list[tuple[str | None, str]]:
    """schema.org recipeInstructions can be a string, list of strings, HowToStep,
    or HowToSection with itemListElement. Return [(section_name, text)]."""
    out: list[tuple[str | None, str]] = []

    def walk(node, section=None):
        if node is None:
            return
        if isinstance(node, str):
            # A single big blob: split on newlines / numbered markers first.
            chunks = re.split(r"\n+|(?:^|\s)(?:step\s*)?\d+[.)]\s+", node, flags=re.I)
            for c in chunks:
                if c and c.strip():
                    out.append((section, c.strip()))
        elif isinstance(node, list):
            for n in node:
                walk(n, section)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "HowToSection" in types:
                walk(node.get("itemListElement"), clean_text(node.get("name") or "") or section)
            elif node.get("itemListElement") and not node.get("text"):
                walk(node.get("itemListElement"), section)
            else:
                walk(node.get("text") or node.get("name") or "", section)

    walk(instructions)
    return out


def is_hands_off(action: str, text: str) -> bool:
    return action in HANDS_OFF_ACTIONS or bool(HANDS_OFF_HINTS.search(text))


def simplify_instructions(instructions) -> list[dict]:
    steps: list[dict] = []
    for section, chunk in _flatten_instructions(instructions):
        for sentence in split_sentences(chunk):
            if is_fluff(sentence):
                continue
            text = tighten(sentence)
            if len(text) < 4:
                continue
            action = classify_action(text)
            minutes = parse_text_minutes(text)
            hands_off = is_hands_off(action, text)
            # Merge tiny consecutive fragments of the same action ("Stir. Add salt.")
            if steps and steps[-1]["action"] == action and len(steps[-1]["text"]) + len(text) < 110 \
                    and steps[-1]["section"] == section and not minutes and not steps[-1]["minutes"]:
                steps[-1]["text"] = steps[-1]["text"].rstrip(".") + "; " + text[0].lower() + text[1:]
                continue
            steps.append({
                "action": action,
                "text": text,
                "minutes": minutes,
                "hands_off": hands_off,
                "section": section,
            })
    for i, s in enumerate(steps, 1):
        s["n"] = i
    return steps


def estimate_times(steps: list[dict], n_ingredients: int, prep: int | None, cook: int | None,
                   total: int | None) -> dict:
    """Return prep/cook/total/hands-off minutes, preferring site-provided values."""
    est_cook = sum(s["minutes"] or 0 for s in steps if s["action"] != "Preheat" and
                   (s["action"] in COOK_ACTIONS or s["hands_off"]))
    hands_off = sum(s["minutes"] or 0 for s in steps if s["hands_off"])
    active_steps = [s for s in steps if s["action"] in ("Cut", "Mix", "Prepare")]
    est_prep = 3 * len(active_steps) + n_ingredients + sum(
        s["minutes"] or 0 for s in active_steps)

    source = "site"
    if prep is None and cook is None and total is None:
        prep, cook, source = est_prep, est_cook, "estimated"
    elif prep is None and cook is None and total is not None:
        cook = min(est_cook, total) if est_cook else int(total * 0.6)
        prep = max(total - cook, 0)
        source = "partly estimated"
    else:
        if prep is None:
            prep = max((total or 0) - (cook or 0), 0) if total else est_prep
            source = "partly estimated"
        if cook is None:
            cook = max((total or 0) - prep, 0) if total else est_cook
            source = "partly estimated"
    total = total if total else prep + cook
    total = max(total, prep + cook)
    hands_off = min(hands_off, total)
    return {
        "prep_minutes": int(prep),
        "cook_minutes": int(cook),
        "total_minutes": int(total),
        "hands_off_minutes": int(hands_off),
        "active_minutes": int(max(total - hands_off, prep)),
        "cook_ratio": round(cook / total, 2) if total else 0.0,
        "time_source": source,
    }


def rate_difficulty(steps: list[dict], ingredients: list[str], times: dict) -> dict:
    """Score 1-5 (Very easy -> Expert) from a weighted effort total, with reasons."""
    text = " ".join(s["text"] for s in steps).lower()
    n_ing, n_steps = len(ingredients), len(steps)
    active = times.get("active_minutes", 0)
    techniques = sorted({t.strip() for t in ADVANCED_TECHNIQUES if t in text})
    sections = {s["section"] for s in steps if s["section"]}

    points = (0.12 * n_ing + 0.15 * n_steps + 0.02 * min(active, 180)
              + 0.8 * min(len(techniques), 3) + (0.5 if len(sections) >= 2 else 0))
    score = 1 + sum(points >= t for t in (1.8, 3.0, 4.2, 8.0))
    label = {1: "Very easy", 2: "Easy", 3: "Medium", 4: "Hard", 5: "Expert"}[score]

    reasons = [f"{n_ing} ingredients", f"{n_steps} steps", f"~{active} min hands-on"]
    if techniques:
        reasons.append("technique: " + ", ".join(techniques[:3]))
    if len(sections) >= 2:
        reasons.append(f"{len(sections)} components")
    return {"difficulty": score, "difficulty_label": label, "difficulty_reasons": reasons,
            "difficulty_points": round(points, 2)}
