"""savr — top-rated recipes, simplified, straight to a pickup order.  Flask app factory."""
from __future__ import annotations

import logging
import os
import secrets

from flask import Flask, g, request

from .db import RecipeDB
from .households import COOKIE, Households
from .library import DEFAULT_LIBRARY, sync_library

log = logging.getLogger(__name__)


class Config:
    DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.getcwd(), "data", "savr.db"))
    LIBRARY_PATH = os.environ.get("LIBRARY_PATH", DEFAULT_LIBRARY)
    LIBRARY_SYNC = os.environ.get("LIBRARY_SYNC", "1") == "1"
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if config:
        app.config.update(config)
    if not os.environ.get("SECRET_KEY") and not (config or {}).get("SECRET_KEY"):
        log.warning("SECRET_KEY not set; Kroger sign-in state won't survive restarts")

    db_path = app.config["DATABASE_PATH"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = RecipeDB(db_path)
    app.extensions["db"] = db
    app.extensions["households"] = Households(db)

    if app.config["LIBRARY_SYNC"]:
        n = sync_library(db, app.config["LIBRARY_PATH"])
        if n:
            log.info("Synced %d recipes from %s", n, app.config["LIBRARY_PATH"])

    @app.after_request
    def _set_household_cookie(resp):
        code = g.pop("set_household", None)
        if code:
            resp.set_cookie(COOKIE, code, max_age=60 * 60 * 24 * 365 * 3, samesite="Lax",
                            secure=app.config["COOKIE_SECURE"], httponly=False)
        return resp

    from .api import bp as api_bp
    from .web import bp as web_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)

    from .durations import fmt_minutes
    from .ingredients import pretty_qty
    from .tagger import DIET_LABELS
    app.jinja_env.filters["mins"] = fmt_minutes
    app.jinja_env.filters["qty"] = pretty_qty
    app.jinja_env.globals["DIET_LABELS"] = DIET_LABELS
    from . import kroger
    app.jinja_env.globals["kroger_enabled"] = kroger.configured
    return app


def current_household(create: bool = False) -> dict | None:
    """Household from the cookie (or X-Household header for API/iOS clients)."""
    from flask import current_app
    store: Households = current_app.extensions["households"]
    if "household" in g:
        return g.household
    code = request.headers.get("X-Household") or request.cookies.get(COOKIE)
    hh = store.get(code)
    if hh is None and create:
        hh = store.create()
        g.set_household = hh["code"]
    g.household = hh
    return hh
