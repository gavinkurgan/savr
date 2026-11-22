"""The recipe library flat file (app/data/library.json) is the source of truth.

Developers update it with the CLI (scrape / refresh / rewrite); the web app only
reads it. On startup the app upserts the file into SQLite when its hash changes,
so household data (ratings, pantry, plans) keyed by recipe id survives updates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

DEFAULT_LIBRARY = os.path.join(os.path.dirname(__file__), "data", "library.json")
LIBRARY_VERSION = 2
# Bump when sync_library starts transforming recipes differently, so databases
# that already loaded an unchanged library.json pick up the change.
SYNC_VERSION = 2


class Library:
    def __init__(self, path: str = DEFAULT_LIBRARY):
        self.path = path
        self.recipes: list[dict] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.recipes = data.get("recipes", [])

    def by_url(self) -> dict[str, dict]:
        return {r["url"]: r for r in self.recipes}

    def urls(self) -> set[str]:
        return {r["url"] for r in self.recipes}

    def find_duplicate(self, recipe: dict) -> dict | None:
        """Another scraped recipe for the same dish (same normalized name), if any.
        The bundled samples are never treated as duplicates."""
        from .quality import dish_key
        key = dish_key(recipe.get("name", ""))
        if not key or is_sample(recipe):
            return None
        for r in self.recipes:
            if r["url"] != recipe["url"] and not is_sample(r) and dish_key(r.get("name", "")) == key:
                return r
        return None

    def upsert(self, recipe: dict) -> str:
        recipe = {k: v for k, v in recipe.items() if k != "id"}
        for i, r in enumerate(self.recipes):
            if r["url"] == recipe["url"]:
                self.recipes[i] = recipe
                return "updated"
        self.recipes.append(recipe)
        return "added"

    def remove(self, key: str) -> int:
        before = len(self.recipes)
        k = key.lower()
        self.recipes = [r for r in self.recipes if r["url"] != key and r["name"].lower() != k]
        return before - len(self.recipes)

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        payload = {
            "version": LIBRARY_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "count": len(self.recipes),
            "recipes": sorted(self.recipes, key=lambda r: r["url"]),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def is_sample(recipe: dict) -> bool:
    return "sample.local" in recipe.get("url", "")


def file_sha(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_library(db, path: str = DEFAULT_LIBRARY, force: bool = False, prune: bool = True) -> int:
    """Load the library into the DB if the file (or the sync logic) changed.
    Returns the number of recipes synced (0 when already up to date)."""
    from .tagger import normalize_cuisine
    sha = file_sha(path)
    if sha is None:
        return 0
    version = f"{sha}:{SYNC_VERSION}"
    if not force and db.get_meta("library_sha") == version:
        return 0
    lib = Library(path)
    for r in lib.recipes:
        r["cuisine"] = normalize_cuisine(r)
    if force:
        db.set_meta("library_sha", "")
    return len(lib.recipes) if db.replace_library(lib.recipes, version, prune=prune) else 0
