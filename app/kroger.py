"""Kroger Public API: sign in, pick a store, push a shopping list into the PICKUP cart.

Covers Kroger-family banners (King Soopers, Ralphs, Fred Meyer, Smith's, Fry's,
Harris Teeter, ...). Setup: create an app at developer.kroger.com with scopes
product.compact + cart.basic:write and redirect URI <your site>/kroger/callback,
then set KROGER_CLIENT_ID, KROGER_CLIENT_SECRET, KROGER_REDIRECT_URI.

Flow: OAuth authorization-code sign-in → per-household tokens (auto-refreshed) →
client-credentials token searches products at the chosen store → PUT /cart/add.
The public API can add to the cart but not read it, so savr reports what it
matched and the shopper reviews & checks out in the Kroger app.
"""
from __future__ import annotations

import base64
import math
import os
import time
from urllib.parse import urlencode

import requests

BASE = os.environ.get("KROGER_API_BASE", "https://api.kroger.com/v1")
USER_SCOPES = "cart.basic:write product.compact profile.compact"


class KrogerError(Exception):
    pass


def _call(fn, *args, **kwargs):
    """Run an HTTP call, turning network failures into a readable KrogerError."""
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as e:
        raise KrogerError(f"Couldn't reach Kroger ({type(e).__name__}). Try again in a minute.") from e


def configured() -> bool:
    return bool(os.environ.get("KROGER_CLIENT_ID") and os.environ.get("KROGER_CLIENT_SECRET"))


def _basic() -> str:
    raw = f"{os.environ['KROGER_CLIENT_ID']}:{os.environ['KROGER_CLIENT_SECRET']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def authorize_url(state: str, redirect_uri: str | None = None) -> str:
    q = {"scope": USER_SCOPES, "response_type": "code", "client_id": os.environ["KROGER_CLIENT_ID"],
         "redirect_uri": redirect_uri or os.environ.get("KROGER_REDIRECT_URI", ""), "state": state}
    return f"{BASE}/connect/oauth2/authorize?{urlencode(q)}"


def _token(data: dict, http=requests) -> dict:
    resp = _call(http.post, f"{BASE}/connect/oauth2/token", data=data, timeout=20,
                     headers={"Authorization": _basic(), "Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise KrogerError(f"token request failed ({resp.status_code}): {resp.text[:200]}")
    tok = resp.json()
    return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
            "expires_at": time.time() + int(tok.get("expires_in", 1800)) - 60}


def exchange_code(code: str, redirect_uri: str | None = None, http=requests) -> dict:
    return _token({"grant_type": "authorization_code", "code": code,
                   "redirect_uri": redirect_uri or os.environ.get("KROGER_REDIRECT_URI", "")}, http)


def refresh(refresh_token: str, http=requests) -> dict:
    return _token({"grant_type": "refresh_token", "refresh_token": refresh_token}, http)


_client_cache: dict = {}


def client_token(http=requests) -> str:
    if _client_cache.get("expires_at", 0) > time.time():
        return _client_cache["access_token"]
    tok = _token({"grant_type": "client_credentials", "scope": "product.compact"}, http)
    _client_cache.update(tok)
    return tok["access_token"]


def _get(path: str, params: dict, token: str, http=requests) -> dict:
    resp = _call(http.get, f"{BASE}{path}", params=params, timeout=20,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if resp.status_code >= 400:
        raise KrogerError(f"GET {path} failed ({resp.status_code})")
    return resp.json()


def find_locations(zip_code: str, http=requests) -> list[dict]:
    data = _get("/locations", {"filter.zipCode.near": zip_code, "filter.limit": 8}, client_token(http), http)
    out = []
    for loc in data.get("data", []):
        addr = loc.get("address", {})
        out.append({"id": loc.get("locationId"), "name": loc.get("name"), "chain": loc.get("chain"),
                    "address": f"{addr.get('addressLine1', '')}, {addr.get('city', '')}"})
    return out


def search_product(term: str, location_id: str | None, http=requests) -> dict | None:
    params = {"filter.term": term[:100], "filter.limit": 5}
    if location_id:
        params["filter.locationId"] = location_id
    data = _get("/products", params, client_token(http), http)
    for p in data.get("data", []):
        upc = p.get("upc") or p.get("productId")
        if upc:
            item = (p.get("items") or [{}])[0]
            price = (item.get("price") or {}).get("regular")
            return {"upc": upc, "description": p.get("description"), "size": item.get("size"), "price": price}
    return None


def quantity_for(item: dict) -> int:
    """Shopping units: 3 limes -> 3, 2 cans -> 2, '1 1/2 cups milk' -> 1 container."""
    q = item.get("quantity")
    if q and item.get("unit") in (None, "can", "package", "jar", "head", "bunch"):
        return max(1, min(12, math.ceil(q)))
    return 1


def ensure_user_token(hh_store, hid: int, http=requests) -> str:
    tok = hh_store.kroger(hid)
    if not tok or not tok.get("access_token"):
        raise KrogerError("not connected to Kroger")
    if tok.get("expires_at", 0) < time.time():
        if not tok.get("refresh_token"):
            raise KrogerError("Kroger session expired — reconnect")
        new = refresh(tok["refresh_token"], http)
        hh_store.kroger_save(hid, **new)
        return new["access_token"]
    return tok["access_token"]


def push_list(hh_store, hid: int, items: list[dict], modality: str = "PICKUP", http=requests) -> dict:
    user_token = ensure_user_token(hh_store, hid, http)
    location = (hh_store.kroger(hid) or {}).get("location_id")
    matched, unmatched = [], []
    for it in items:
        try:
            prod = search_product(it["item"], location, http)
        except KrogerError:
            prod = None
        if prod:
            matched.append({"item": it["display"], "upc": prod["upc"], "product": prod["description"],
                            "size": prod.get("size"), "price": prod.get("price"), "quantity": quantity_for(it)})
        else:
            unmatched.append(it["display"])
    if matched:
        body = {"items": [{"upc": m["upc"], "quantity": m["quantity"], "modality": modality} for m in matched]}
        resp = _call(http.put, f"{BASE}/cart/add", json=body, timeout=30,
                        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"})
        if resp.status_code not in (200, 201, 204):
            raise KrogerError(f"cart add failed ({resp.status_code}): {getattr(resp, 'text', '')[:200]}")
    est = sum((m["price"] or 0) * m["quantity"] for m in matched)
    return {"added": matched, "unmatched": unmatched, "estimated_total": round(est, 2) if est else None}
