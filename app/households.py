"""Households: a shared code (no passwords) that links a family's phones together.

Everything personal hangs off a household: thumbs up/down ratings, pantry, the
weekly plan and a Kroger connection. A device joins by entering the code; it's
kept in a long-lived cookie. Treat the code like a Wi-Fi password.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets

from .db import RecipeDB, _lock

WORDS_A = ["basil", "sage", "thyme", "mint", "dill", "chive", "fennel", "ginger", "pepper", "clove",
           "nutmeg", "saffron", "cumin", "anise", "mango", "lemon", "olive", "plum", "fig", "pecan"]
WORDS_B = ["otter", "heron", "badger", "panda", "falcon", "walrus", "lynx", "koala", "moose", "raven",
           "gecko", "bison", "llama", "puffin", "ferret", "marmot", "yak", "ibis", "tapir", "wombat"]
COOKIE = "savr_household"
CODE_RE = re.compile(r"^[a-z]+-[a-z]+-\d{4}$")


def new_code() -> str:
    return f"{secrets.choice(WORDS_A)}-{secrets.choice(WORDS_B)}-{secrets.randbelow(9000) + 1000}"


def normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    code = re.sub(r"\s+", "-", code.strip().lower())
    return code if CODE_RE.match(code) else None


class Households:
    def __init__(self, db: RecipeDB):
        self.db = db

    # ------------------------------------------------------------ households
    def create(self, name: str = "Our kitchen") -> dict:
        for _ in range(20):
            code = new_code()
            try:
                with _lock, self.db.conn() as c:
                    c.execute("INSERT INTO households(code, name) VALUES (?, ?)", (code, name))
                return self.get(code)
            except Exception:
                continue
        raise RuntimeError("could not allocate household code")

    def get(self, code: str | None) -> dict | None:
        code = normalize_code(code)
        if not code:
            return None
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM households WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def rename(self, hid: int, name: str) -> None:
        with _lock, self.db.conn() as c:
            c.execute("UPDATE households SET name=? WHERE id=?", (name.strip()[:60] or "Our kitchen", hid))

    # ------------------------------------------------------------ ratings
    def rate(self, hid: int, recipe_id: int, value: int, member: str = "me") -> None:
        member = (member or "me").strip()[:30] or "me"
        with _lock, self.db.conn() as c:
            if value == 0:
                c.execute("DELETE FROM ratings WHERE household_id=? AND recipe_id=? AND member=?", (hid, recipe_id, member))
            else:
                c.execute("""INSERT INTO ratings(household_id, recipe_id, member, value) VALUES (?,?,?,?)
                             ON CONFLICT(household_id, recipe_id, member)
                             DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                          (hid, recipe_id, member, 1 if value > 0 else -1))

    def ratings(self, hid: int) -> dict[int, dict]:
        """{recipe_id: {"score": net, "up": n, "down": n, "members": {name: value}}}"""
        with self.db.conn() as c:
            rows = c.execute("SELECT recipe_id, member, value FROM ratings WHERE household_id=?", (hid,)).fetchall()
        out: dict[int, dict] = {}
        for r in rows:
            d = out.setdefault(r["recipe_id"], {"score": 0, "up": 0, "down": 0, "members": {}})
            d["score"] += r["value"]
            d["up" if r["value"] > 0 else "down"] += 1
            d["members"][r["member"]] = r["value"]
        return out

    # ------------------------------------------------------------ pantry
    def pantry(self, hid: int) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("""SELECT id, item, use_by, added_at FROM pantry WHERE household_id=?
                                ORDER BY use_by IS NULL, use_by, item""", (hid,)).fetchall()
        today = dt.date.today()
        out = []
        for r in rows:
            d = dict(r)
            d["days_left"] = None
            if d["use_by"]:
                try:
                    d["days_left"] = (dt.date.fromisoformat(d["use_by"]) - today).days
                except ValueError:
                    pass
            out.append(d)
        return out

    def pantry_add(self, hid: int, items: list[str], use_by: str | None = None) -> int:
        n = 0
        with _lock, self.db.conn() as c:
            for raw in items:
                item = re.sub(r"\s+", " ", raw.strip().lower())
                if not item:
                    continue
                c.execute("""INSERT INTO pantry(household_id, item, use_by) VALUES (?,?,?)
                             ON CONFLICT(household_id, item) DO UPDATE SET
                             use_by=COALESCE(excluded.use_by, pantry.use_by), added_at=CURRENT_TIMESTAMP""",
                          (hid, item[:80], use_by or None))
                n += 1
        return n

    def pantry_remove(self, hid: int, item_id: int | None = None, item: str | None = None) -> None:
        with _lock, self.db.conn() as c:
            if item_id:
                c.execute("DELETE FROM pantry WHERE household_id=? AND id=?", (hid, item_id))
            elif item:
                c.execute("DELETE FROM pantry WHERE household_id=? AND item=?", (hid, item.strip().lower()))

    # ------------------------------------------------------------ plans
    def save_plan(self, hid: int, plan: dict) -> None:
        with _lock, self.db.conn() as c:
            c.execute("""INSERT INTO plans(household_id, data) VALUES (?,?) ON CONFLICT(household_id)
                         DO UPDATE SET data=excluded.data, updated_at=CURRENT_TIMESTAMP""", (hid, json.dumps(plan)))

    def plan(self, hid: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT data FROM plans WHERE household_id=?", (hid,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ------------------------------------------------------------ kroger
    def kroger(self, hid: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM kroger_tokens WHERE household_id=?", (hid,)).fetchone()
        return dict(row) if row else None

    def kroger_save(self, hid: int, **fields) -> None:
        current = self.kroger(hid) or {"household_id": hid}
        current.update({k: v for k, v in fields.items()})
        cols = ["household_id", "access_token", "refresh_token", "expires_at", "location_id", "location_name"]
        vals = [current.get(k) for k in cols]
        with _lock, self.db.conn() as c:
            c.execute(f"INSERT OR REPLACE INTO kroger_tokens({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def kroger_disconnect(self, hid: int) -> None:
        with _lock, self.db.conn() as c:
            c.execute("DELETE FROM kroger_tokens WHERE household_id=?", (hid,))
