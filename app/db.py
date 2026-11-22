"""SQLite storage. Filter-able fields are columns; the full record is JSON."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from .quality import sql_quality_expr

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    source TEXT,
    rating REAL,
    rating_count INTEGER DEFAULT 0,
    difficulty INTEGER,
    total_minutes INTEGER,
    prep_minutes INTEGER,
    cook_minutes INTEGER,
    active_minutes INTEGER,
    meal_type TEXT,
    cuisine TEXT,
    tags TEXT,          -- ",gluten_free,vegan,quick,"  (diet tags + styles) for LIKE filters
    search_text TEXT,   -- lowercased name + ingredients + keywords
    data TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_meal ON recipes(meal_type);
CREATE INDEX IF NOT EXISTS idx_cuisine ON recipes(cuisine);
CREATE INDEX IF NOT EXISTS idx_total ON recipes(total_minutes);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT 'Our kitchen',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ratings (
    household_id INTEGER NOT NULL,
    recipe_id INTEGER NOT NULL,
    member TEXT NOT NULL DEFAULT 'me',
    value INTEGER NOT NULL,              -- +1 thumbs up, -1 thumbs down
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (household_id, recipe_id, member)
);
CREATE TABLE IF NOT EXISTS pantry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    use_by TEXT,                         -- ISO date, optional
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, item)
);
CREATE TABLE IF NOT EXISTS plans (
    household_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kroger_tokens (
    household_id INTEGER PRIMARY KEY,
    access_token TEXT, refresh_token TEXT, expires_at REAL,
    location_id TEXT, location_name TEXT
);
"""

_lock = threading.Lock()


class RecipeDB:
    def __init__(self, path: str):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _upsert_row(c, r: dict) -> int:
        tags = "," + ",".join(r.get("diet_tags", []) + r.get("styles", [])) + ","
        search = " ".join([r["name"], r.get("description", ""), " ".join(r.get("ingredients", [])),
                           r.get("cuisine") or "", r.get("meal_type") or ""]).lower()
        row = (r["url"], r["name"], r.get("source"), r.get("rating"), r.get("rating_count", 0),
               r.get("difficulty"), r.get("total_minutes"), r.get("prep_minutes"), r.get("cook_minutes"),
               r.get("active_minutes"), r.get("meal_type"), r.get("cuisine"), tags, search,
               json.dumps(r))
        c.execute(
            """INSERT INTO recipes (url,name,source,rating,rating_count,difficulty,total_minutes,
               prep_minutes,cook_minutes,active_minutes,meal_type,cuisine,tags,search_text,data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET name=excluded.name, source=excluded.source,
               rating=excluded.rating, rating_count=excluded.rating_count,
               difficulty=excluded.difficulty, total_minutes=excluded.total_minutes,
               prep_minutes=excluded.prep_minutes, cook_minutes=excluded.cook_minutes,
               active_minutes=excluded.active_minutes, meal_type=excluded.meal_type,
               cuisine=excluded.cuisine, tags=excluded.tags, search_text=excluded.search_text,
               data=excluded.data""", row)
        return c.execute("SELECT id FROM recipes WHERE url=?", (r["url"],)).fetchone()["id"]

    def upsert(self, r: dict) -> int:
        with _lock, self.conn() as c:
            return self._upsert_row(c, r)

    def replace_library(self, recipes: list[dict], version: str, prune: bool = True,
                        meta_key: str = "library_sha") -> bool:
        """Load a whole library in ONE transaction. ``BEGIN IMMEDIATE`` takes the
        write lock first, so when several app workers start at once, one loads the
        library and the others see the new version and skip. Returns False if this
        version was already loaded."""
        with _lock:
            c = sqlite3.connect(self.path, timeout=120, isolation_level=None)
            c.row_factory = sqlite3.Row
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT value FROM meta WHERE key=?", (meta_key,)).fetchone()
                if row and row["value"] == version:
                    c.execute("ROLLBACK")
                    return False
                for r in recipes:
                    self._upsert_row(c, r)
                if prune:
                    keep = {r["url"] for r in recipes}
                    gone = [(u,) for (u,) in c.execute("SELECT url FROM recipes") if u not in keep]
                    c.executemany("DELETE FROM recipes WHERE url=?", gone)
                c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (meta_key, version))
                c.execute("COMMIT")
                return True
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    # ------------------------------------------------------------ meta / library
    def get_meta(self, key: str):
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with _lock, self.conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    def prune_except(self, urls: set[str]) -> int:
        with self.conn() as c:
            existing = [r["url"] for r in c.execute("SELECT url FROM recipes")]
        gone = [u for u in existing if u not in urls]
        with _lock, self.conn() as c:
            c.executemany("DELETE FROM recipes WHERE url=?", [(u,) for u in gone])
        return len(gone)

    def all_ids(self) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM recipes")]

    def get_many(self, ids) -> list[dict]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        with self.conn() as c:
            rows = c.execute(f"SELECT id, data FROM recipes WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        by_id = {r["id"]: self._hydrate(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def count(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]

    def get(self, recipe_id: int) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT id, data FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        return self._hydrate(row) if row else None

    def delete(self, recipe_id: int) -> bool:
        with _lock, self.conn() as c:
            return c.execute("DELETE FROM recipes WHERE id=?", (recipe_id,)).rowcount > 0

    @staticmethod
    def _hydrate(row) -> dict:
        d = json.loads(row["data"])
        d["id"] = row["id"]
        return d

    @staticmethod
    def _where(q: str = "", meal_type=None, cuisine=None, tags: list[str] | None = None,
               max_minutes=None, max_difficulty=None, max_active=None, min_rating=None,
               exclude_ingredients: list[str] | None = None) -> tuple[str, list]:
        where, args = [], []
        for word in (q or "").lower().split():
            where.append("search_text LIKE ?"); args.append(f"%{word}%")
        if meal_type:
            where.append("meal_type = ?"); args.append(meal_type)
        if cuisine:
            where.append("cuisine = ?"); args.append(cuisine)
        for t in tags or []:
            where.append("tags LIKE ?"); args.append(f"%,{t},%")
        if max_minutes:
            where.append("total_minutes <= ?"); args.append(int(max_minutes))
        if max_active:
            where.append("active_minutes <= ?"); args.append(int(max_active))
        if max_difficulty:
            where.append("difficulty <= ?"); args.append(int(max_difficulty))
        if min_rating:
            where.append("rating >= ?"); args.append(float(min_rating))
        for ex in exclude_ingredients or []:
            where.append("search_text NOT LIKE ?"); args.append(f"%{ex.lower()}%")
        return (" WHERE " + " AND ".join(where) if where else ""), args

    def search(self, q: str = "", meal_type=None, cuisine=None, tags: list[str] | None = None,
               max_minutes=None, max_difficulty=None, max_active=None, min_rating=None,
               exclude_ingredients: list[str] | None = None, sort="rating", limit=50, offset=0) -> list[dict]:
        where, args = self._where(q, meal_type, cuisine, tags, max_minutes, max_difficulty, max_active,
                                  min_rating, exclude_ingredients)
        order = {
            "rating": f"{sql_quality_expr()} DESC, rating_count DESC",  # review-weighted
            "time": "total_minutes ASC",
            "difficulty": "difficulty ASC, total_minutes ASC",
            "newest": "id DESC",
            "name": "name ASC",
        }.get(sort, "rating DESC")
        sql = f"SELECT id, data FROM recipes{where} ORDER BY {order}, id LIMIT ? OFFSET ?"
        with self.conn() as c:
            rows = c.execute(sql, args + [int(limit), int(offset)]).fetchall()
        return [self._hydrate(r) for r in rows]

    def search_count(self, sort=None, limit=None, offset=None, **filters) -> int:
        """Total matches for the same filters as ``search`` (paging args ignored)."""
        where, args = self._where(**filters)
        with self.conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM recipes{where}", args).fetchone()[0]

    def facets(self) -> dict:
        with self.conn() as c:
            meal = c.execute("SELECT meal_type, COUNT(*) n FROM recipes GROUP BY meal_type ORDER BY n DESC").fetchall()
            cuisine = c.execute("SELECT cuisine, COUNT(*) n FROM recipes GROUP BY cuisine ORDER BY n DESC").fetchall()
            tag_rows = c.execute("SELECT tags FROM recipes").fetchall()
        tag_counts: dict[str, int] = {}
        for r in tag_rows:
            for t in (r["tags"] or "").strip(",").split(","):
                if t:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
        return {
            "meal_types": [{"value": r["meal_type"], "count": r["n"]} for r in meal],
            "cuisines": [{"value": r["cuisine"], "count": r["n"]} for r in cuisine],
            "tags": [{"value": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])],
        }
