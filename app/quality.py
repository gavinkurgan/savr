"""Recipe quality measures shared by the scraper, the CLI and the app.

``quality_score`` is a Bayesian (review-weighted) average: a rating is pulled
toward ``PRIOR`` until it has enough reviews to stand on its own, so 4.9 stars
from 6 reviews ranks below 4.6 stars from 19,000. Unrated recipes get the prior
minus a small penalty.
"""
from __future__ import annotations

import re

PRIOR = 4.0          # what we assume about a recipe with no reviews
WEIGHT = 20          # reviews needed before the recipe's own rating counts as much as the prior
UNRATED_PENALTY = 0.2


def quality_score(rating: float | None, count: int | None) -> float:
    if rating is None:
        return round(PRIOR - UNRATED_PENALTY, 3)
    n = max(0, int(count or 0))
    return round((rating * n + PRIOR * WEIGHT) / (n + WEIGHT), 3)


def sql_quality_expr(rating_col: str = "rating", count_col: str = "rating_count") -> str:
    """The same formula as SQL, for ORDER BY."""
    n = f"COALESCE({count_col}, 0)"
    return (f"(CASE WHEN {rating_col} IS NULL THEN {PRIOR - UNRATED_PENALTY} "
            f"ELSE ({rating_col} * {n} + {PRIOR * WEIGHT}) / ({n} + {WEIGHT}.0) END)")


# Words that differ between sites' names for the same dish.
_FILLER = {"the", "best", "easy", "easiest", "perfect", "ultimate", "homemade", "simple", "quick", "my",
           "favorite", "favourite", "classic", "authentic", "world", "worlds", "copycat", "recipe", "recipes",
           "a", "an", "and", "really", "super", "amazing", "delicious", "famous", "foolproof", "minute", "s"}


def dish_key(name: str) -> str:
    """Normalize a recipe name so near-identical dishes from different sites match:
    "The BEST Chocolate Chip Cookies (Easy!)" and "Best Chocolate Chip Cookie Recipe" ->
    "chocolate chip cookie"."""
    s = name.lower()
    s = re.sub(r"\(.*?\)|\bby [\w .'-]+$", " ", s)       # "(Easy!)", "... by Tasty"
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("’", "'").replace("'s ", " "))
    words = []
    for w in s.split():
        if w in _FILLER or w.isdigit():
            continue
        # crude singular: cookies -> cookie, berries/berry -> berrie, dishes -> dish
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-1]
        elif len(w) > 4 and w.endswith(("shes", "ches", "xes", "sses")):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) > 2 and w.endswith("y") and w[-2] not in "aeiou":
            w = w[:-1] + "ie"
        words.append(w)
    return " ".join(words)
