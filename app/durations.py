"""Time parsing helpers: ISO-8601 durations (schema.org) and free-text times."""
from __future__ import annotations

import re

_ISO_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)

# "25 minutes", "1 hour", "1-2 hours", "1 1/2 hours", "45 mins", "2 hrs"
_TEXT_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)(?:\s*(?:-|to|–)\s*(?P<b>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>hours?|hrs?|minutes?|mins?|seconds?|secs?)\b",
    re.IGNORECASE,
)


def parse_iso_duration(value) -> int | None:
    """Return whole minutes for an ISO-8601 duration like 'PT1H30M', or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    m = _ISO_RE.match(s)
    if not m or not any(m.groupdict().values()):
        # Some sites put plain text like "30 minutes" in prepTime.
        return parse_text_minutes(s)
    d = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    total = d["days"] * 1440 + d["hours"] * 60 + d["minutes"] + d["seconds"] / 60
    return int(round(total))


def parse_text_minutes(text: str) -> int | None:
    """Sum the explicit times mentioned in a sentence. Ranges take the upper bound."""
    if not text:
        return None
    total = 0.0
    found = False
    for m in _TEXT_RE.finditer(text):
        found = True
        val = float(m.group("b") or m.group("a"))
        unit = m.group("unit").lower()
        if unit.startswith("h"):
            val *= 60
        elif unit.startswith("s"):
            val /= 60
        total += val
    return int(round(total)) if found else None


def fmt_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} hr {m} min" if m else f"{h} hr"
