"""Recipe rewriting: equipment list, tightened steps, and two voices.

* ``standard`` — plain, clear recipe language.
* ``unhinged`` — same instructions, same numbers, deranged delivery ("cook that shit").

Two engines:
1. Claude (when ANTHROPIC_API_KEY is set): merges/reorders steps, lists equipment,
   writes both voices. Output is validated (every time and temperature must survive)
   and falls back to rules if anything is off.
2. Rules (always available, offline): equipment by keyword, standard = simplified
   text, unhinged = action-specific openers + phrase swaps around the same text.

This runs on the developer side (``python -m app.cli rewrite``) and is stored with
the recipe, so users never wait on it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("SAVR_REWRITE_MODEL", "claude-haiku-5-5")
VALID_ACTIONS = {"Preheat", "Cut", "Mix", "Cook", "Bake", "Grill", "Rest", "Serve", "Prepare"}

# ---------------------------------------------------------------- equipment
EQUIPMENT = [
    ("oven", r"\boven\b|\bbake\b|\broast\b|\bbroil"),
    ("large skillet", r"\bskillet\b|\bfrying pan\b|\bsauté pan\b|\bsaute pan\b"),
    ("large pot", r"\b(?:large |stock|soup |heavy )?pot\b|\bdutch oven\b"),
    ("saucepan", r"\bsaucepan\b"),
    ("sheet pan", r"\bsheet pan\b|\bbaking sheet\b|\bsheet-pan\b"),
    ("baking dish", r"\bbaking dish\b|\bcasserole dish\b|\b\d+x\d+(?:-inch)? (?:pan|dish)\b"),
    ("muffin tin", r"\bmuffin (?:tin|pan|cups?)\b"),
    ("slow cooker", r"\bslow cooker\b|\bcrock ?pot\b"),
    ("wok", r"\bwok\b"),
    ("griddle", r"\bgriddle\b"),
    ("grill", r"\bgrill\b"),
    ("blender", r"\bblend|\bblender\b|\bpur[eé]e\b"),
    ("food processor", r"\bfood processor\b"),
    ("mixing bowls", r"\bbowl\b|\bwhisk\b|\bcombine\b|\btoss\b"),
    ("whisk", r"\bwhisk"),
    ("cutting board + knife", r"\bchop|\bdice|\bslice|\bmince|\bcut\b|\bjulienne"),
    ("grater / zester", r"\bgrate|\bzest|\bshred"),
    ("colander", r"\bdrain\b|\brinse\b"),
    ("parchment paper", r"\bparchment\b"),
    ("jar with lid", r"\bjar\b"),
    ("thermometer", r"\b\d+°F\b.*\b(?:reaches|internal)\b|\bthermometer\b"),
    ("roasting pan", r"\broasting pan\b"),
    ("kitchen twine", r"\btwine\b|\btruss"),
    ("cast iron skillet", r"\bcast iron\b"),
]


def detect_equipment(steps: list[dict], ingredients: list[str] | None = None) -> list[str]:
    text = " ".join(s.get("text", "") for s in steps).lower()
    found = []
    for name, pattern in EQUIPMENT:
        if re.search(pattern, text, re.I) and name not in found:
            found.append(name)
    if "large skillet" in found and "wok" in found:
        found.remove("large skillet")
    return found


# ---------------------------------------------------------------- unhinged (rules)
OPENERS = {
    "Preheat": ["Crank that oven, we're not playing.", "Get the oven hot as hell first.",
                "Oven on. Do it now, future you will thank you."],
    "Cut": ["Knife time.", "Chop that shit up.", "Get stabby (carefully).", "Board out, blade out."],
    "Mix": ["Mix that shit together.", "Stir it like you mean it.", "Get in there.",
            "Bowl time. Make it happen."],
    "Cook": ["Cook that shit.", "Let it sizzle, damn it.", "Heat is your friend now.",
             "Fire up the stove and show it who's boss."],
    "Bake": ["Into the hot box it goes.", "Let the oven do the damn work.",
             "Shove it in the oven and walk away."],
    "Grill": ["Fire time, baby.", "Grill that shit."],
    "Rest": ["Now leave it the hell alone.", "Hands off. Seriously.", "Patience, you animal."],
    "Serve": ["Plate that shit and eat.", "You did it, you magnificent bastard.",
              "Dinner is served, chef."],
    "Prepare": ["Get your shit together.", "Prep time, no excuses.", "Set yourself up, champ."],
}
CLOSERS = {
    "Rest": ["Do not touch it. I can see you.", "Go scroll your phone or something."],
    "Bake": ["Don't open the door every 30 seconds, it's not going anywhere."],
    "Serve": ["Take a picture or don't, I'm not your mom."],
    "Cook": ["Don't walk away and burn it.", ""],
}
SWAPS = [
    (r"\buntil golden(?: brown)?\b", "until it's golden as hell"),
    (r"\buntil (?:very )?soft(?:ened)?\b", "until it gives up and goes soft"),
    (r"\buntil (?:just )?combined\b", "until it's one big happy family"),
    (r"\buntil fragrant\b", "until your kitchen smells incredible"),
    (r"\buntil wilted\b", "until it collapses"),
    (r"\buntil smooth\b", "until it's smooth as hell"),
    (r"\buntil (?:crisp|crispy)\b", "until it's crispy enough to crunch loudly"),
    (r"\buntil glossy\b", "until it's shiny and smug"),
    (r"\bstir(?:ring)? occasionally\b", "stir every so damn often"),
    (r"\bseason with\b", "hit it with"),
    (r"\bset aside\b", "shove it aside"),
    (r"\bto taste\b", "until it tastes right to you"),
    (r"\bgently\b", "gently (yes, gently)"),
    (r"\bimmediately\b", "right the hell now"),
    (r"\bright away\b", "right the hell now"),
]


def _pick(options: list[str], seed: str) -> str:
    if not options:
        return ""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


HELL = ["the hell out of", "the absolute crap out of", "the living hell out of"]
# Rewrite the opening verb itself; {h} picks a HELL phrase.
VERB_SWAPS = [
    (r"^preheat (?:the )?oven to", "Crank the damn oven to"),
    (r"^(chop|dice|mince|slice|grate|shred|cube)\b(?=\s+(?!\d|for\b|until\b|into\b))", r"\1 {h}"),
    (r"^(sear|brown|roast|whisk|mash|beat)\b(?=\s+(?!\d|for\b|until\b|about\b|on\b|in\b))", r"\1 {h}"),
    (r"^(?:stir in|add)\b", "Chuck in"),
    (r"^pour (?:in )?", "Dump in "),
    (r"^toss\b", "Aggressively toss"),
    (r"^drain\.?$", "Drain that shit."),
    (r"^simmer\b", "Let that shit simmer"),
    (r"^bake\b", "Bake that shit"),
    (r"^boil\b", "Boil the hell out of"),
    (r"^cover and\b", "Slap a lid on it and"),
    (r"^garnish with\b", "Make it pretty with"),
    (r"^serve\b", "Serve it up, chef,"),
    (r"^heat (.+?) over (\S+(?:-\S+)?) heat", r"Get \1 hot as hell over \2 heat"),
]


def _swap_verb(text: str, seed: str) -> tuple[str, bool]:
    for pat, rep in VERB_SWAPS:
        m = re.match(pat, text, flags=re.I)
        if m:
            out = m.expand(rep.replace("{h}", _pick(HELL, seed))) + text[m.end():]
            return out[:1].upper() + out[1:], True
    return text, False


def unhinge_text(text: str, action: str, seed: str) -> str:
    body, swapped = _swap_verb(text, seed)
    for pat, rep in SWAPS:
        body = re.sub(pat, rep, body, flags=re.I)
    if swapped:  # the sentence is already unhinged; skip the opener most of the time
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        closer = _pick(CLOSERS.get(action, []), seed + "c") if h % 2 == 0 else ""
        return " ".join(x for x in [body, closer] if x).strip()
    options = OPENERS.get(action, OPENERS["Prepare"])
    if action == "Serve" and not re.search(r"\b(?:serve|plate|garnish|enjoy)", text, re.I):
        options = ["Finish it off.", "Final touch, you artist.", "Make it pretty."]
    opener = _pick(options, seed)
    closer = _pick(CLOSERS.get(action, []), seed + "c") if int(hashlib.md5(seed.encode()).hexdigest(), 16) % 3 == 0 else ""
    return " ".join(x for x in [opener, body, closer] if x).strip()


def rules_rewrite(recipe: dict) -> dict:
    steps = []
    for i, s in enumerate(recipe.get("steps", [])):
        seed = f"{recipe.get('name', '')}:{i}"
        steps.append({**s, "unhinged": unhinge_text(s["text"], s["action"], seed)})
    return {"steps": steps, "equipment": detect_equipment(steps), "voice_source": "rules"}


# ---------------------------------------------------------------- Claude
PROMPT = """You rewrite recipe steps for a cooking app called savr.

Return ONLY JSON, no prose, shaped exactly like:
{{"equipment": ["..."], "steps": [{{"action": "Cook", "minutes": 6, "standard": "...", "unhinged": "..."}}]}}

Rules:
- Merge tiny steps and reorder only if it makes cooking smoother. Keep 3-14 steps.
- "action" is one of: Preheat, Cut, Mix, Cook, Bake, Grill, Rest, Serve, Prepare.
- "minutes" is the timer for that step as an integer, or null.
- "standard": one or two short, clear imperative sentences. No stories, no fluff.
- "unhinged": the SAME instruction in a deranged, profane, hype-man voice
  (e.g. "Cook that shit for 6 minutes until it's golden as hell."). Swearing is
  welcome; slurs, sexual content and insults about groups are not.
- Never change, drop or invent a quantity, time or temperature. Every number in the
  original steps must appear in both voices.
- "equipment": the tools needed, short names, most important first.

Recipe: {name}
Ingredients:
{ingredients}

Current steps:
{steps}
"""

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return set(_NUM_RE.findall(text))


def validate_llm_output(recipe: dict, data: dict) -> list[str]:
    """Return a list of problems (empty = OK)."""
    problems = []
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not (1 <= len(steps) <= max(14, len(recipe["steps"]) + 2)):
        return ["bad step list"]
    for s in steps:
        if s.get("action") not in VALID_ACTIONS:
            problems.append(f"bad action {s.get('action')!r}")
        if not str(s.get("standard", "")).strip() or not str(s.get("unhinged", "")).strip():
            problems.append("empty voice")
    original = _numbers(" ".join(s["text"] for s in recipe["steps"]))
    std = _numbers(" ".join(str(s.get("standard", "")) for s in steps))
    unh = _numbers(" ".join(str(s.get("unhinged", "")) for s in steps))
    if original - std:
        problems.append(f"standard lost numbers {sorted(original - std)}")
    if original - unh:
        problems.append(f"unhinged lost numbers {sorted(original - unh)}")
    if not isinstance(data.get("equipment", []), list):
        problems.append("equipment not a list")
    return problems


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in response")
    return json.loads(m.group())


def claude_rewrite(recipe: dict, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60) -> dict:
    prompt = PROMPT.format(
        name=recipe["name"],
        ingredients="\n".join(f"- {i}" for i in recipe.get("ingredients", [])),
        steps="\n".join(f"{i + 1}. [{s['action']}] {s['text']}" for i, s in enumerate(recipe["steps"])),
    )
    resp = requests.post(
        ANTHROPIC_URL, timeout=timeout,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]},
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
    data = _extract_json(text)
    problems = validate_llm_output(recipe, data)
    if problems:
        raise ValueError("; ".join(problems))
    steps = []
    for i, s in enumerate(data["steps"], 1):
        minutes = s.get("minutes")
        steps.append({
            "n": i, "action": s["action"], "text": str(s["standard"]).strip(),
            "unhinged": str(s["unhinged"]).strip(),
            "minutes": int(minutes) if isinstance(minutes, (int, float)) and minutes > 0 else None,
            "hands_off": s["action"] in ("Bake", "Rest") or bool(re.search(r"\bsimmer|slow cook|chill|marinate", s["standard"], re.I)),
            "section": None,
        })
    return {"steps": steps, "equipment": [str(e) for e in data.get("equipment", [])][:12],
            "voice_source": f"claude:{model}"}


def apply_rewrite(recipe: dict, use_llm: bool | None = None, api_key: str | None = None,
                  model: str = DEFAULT_MODEL) -> dict:
    """Mutates and returns recipe with equipment, steps[*].unhinged and voice_source."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if use_llm is None:
        use_llm = bool(api_key)
    result = None
    if use_llm and api_key:
        try:
            result = claude_rewrite(recipe, api_key, model)
            recipe.setdefault("steps_rules", recipe["steps"])
        except Exception as e:  # network, JSON, validation
            log.warning("Claude rewrite failed for %s, using rules: %s", recipe.get("name"), e)
            recipe["rewrite_error"] = str(e)[:300]
    if result is None:
        result = rules_rewrite(recipe)
    recipe["steps"] = result["steps"]
    recipe["equipment"] = result["equipment"]
    recipe["voice_source"] = result["voice_source"]
    return recipe
