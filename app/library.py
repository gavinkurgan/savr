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


def file_sha(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_library(db, path: str = DEFAULT_LIBRARY, force: bool = False, prune: bool = True) -> int:
    """Upsert the library into the DB if the file changed. Returns recipes synced."""
    sha = file_sha(path)
    if sha is None:
        return 0
    if not force and db.get_meta("library_sha") == sha:
        return 0
    lib = Library(path)
    for r in lib.recipes:
        db.upsert(r)
    if prune:
        db.prune_except({r["url"] for r in lib.recipes})
    db.set_meta("library_sha", sha)
    return len(lib.recipes)
