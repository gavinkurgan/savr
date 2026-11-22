"""Walmart I/O (affiliate product API): match a shopping list to Walmart items and
build one "add all to cart" link.

Walmart has no public API that fills a signed-in shopper's cart (unlike Kroger).
The supported route is: look each ingredient up with the product search API,
then send the shopper to Walmart's add-to-cart URL with every matched item ID.
Walmart opens with the items in the cart, and the shopper picks pickup or
delivery and checks out there.

Setup (walmart.io):
  1. Generate an RSA key pair and upload the PUBLIC key in the Walmart I/O portal.
  2. Set WALMART_CONSUMER_ID (from the portal) and WALMART_KEY_VERSION (shown next
     to the uploaded key, usually 1).
  3. Give savr the PRIVATE key, either as a file (WALMART_PRIVATE_KEY_PATH) or
     inline (WALMART_PRIVATE_KEY: the PEM text, or the PEM base64-encoded on one line).
  Optional: WALMART_PUBLISHER_ID (Impact affiliate ID) to get affiliate credit.

Every request is signed: RSA-SHA256 over "consumerId\\ntimestamp\\nkeyVersion\\n".

If anything is missing or a live test search fails, ``status()`` says so and the
app falls back to plain walmart.com search links (grocery.store_links).
"""
from __future__ import annotations

import base64
import math
import os
import threading
import time
from urllib.parse import quote, quote_plus

import requests

from .grocery import STORES

API_BASE = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2"
CART_URL = "https://affil.walmart.com/cart/addToCart"
STATUS_TTL_OK = 15 * 60       # re-test a working setup every 15 minutes
STATUS_TTL_FAIL = 2 * 60      # retry a failing one sooner (outages like a 521 are often brief)
MAX_LOOKUPS = 60              # cap API calls per cart push


class WalmartError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> bool:
    return bool(_env("WALMART_CONSUMER_ID") and (_env("WALMART_PRIVATE_KEY") or _env("WALMART_PRIVATE_KEY_PATH")))


def missing_settings() -> list[str]:
    out = []
    if not _env("WALMART_CONSUMER_ID"):
        out.append("WALMART_CONSUMER_ID")
    if not (_env("WALMART_PRIVATE_KEY") or _env("WALMART_PRIVATE_KEY_PATH")):
        out.append("WALMART_PRIVATE_KEY_PATH (or WALMART_PRIVATE_KEY)")
    return out


# ------------------------------------------------------------------ signing
_key_cache: dict = {}


def _pem_bytes() -> bytes:
    path = _env("WALMART_PRIVATE_KEY_PATH")
    if path:
        try:
            with open(os.path.expanduser(path), "rb") as f:
                return f.read()
        except OSError as e:
            raise WalmartError(f"Can't read the Walmart private key file ({e.strerror}).") from e
    raw = _env("WALMART_PRIVATE_KEY")
    if "BEGIN" in raw:                      # PEM text, possibly with literal \n escapes
        return raw.replace("\\n", "\n").encode()
    try:
        data = base64.b64decode(raw, validate=False)
    except ValueError as e:
        raise WalmartError("WALMART_PRIVATE_KEY isn't PEM text or base64.") from e
    return data                             # base64 of a PEM file, or of the DER key itself


def _private_key():
    src = _env("WALMART_PRIVATE_KEY_PATH") or _env("WALMART_PRIVATE_KEY")
    if _key_cache.get("src") == src:
        return _key_cache["key"]
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:  # pragma: no cover - listed in requirements.txt
        raise WalmartError("The 'cryptography' package is needed for Walmart signing.") from e
    data = _pem_bytes()
    password = _env("WALMART_PRIVATE_KEY_PASSWORD").encode() or None
    try:
        if b"-----BEGIN" in data:
            key = serialization.load_pem_private_key(data, password=password)
        else:
            key = serialization.load_der_private_key(data, password=password)
    except (ValueError, TypeError) as e:
        raise WalmartError("The Walmart private key couldn't be loaded. Use the unencrypted private key "
                           "(BEGIN PRIVATE KEY), or set WALMART_PRIVATE_KEY_PASSWORD.") from e
    _key_cache.update(src=src, key=key)
    return key


def auth_headers(now_ms: int | None = None) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    consumer_id = _env("WALMART_CONSUMER_ID")
    version = _env("WALMART_KEY_VERSION", "1")
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    message = f"{consumer_id}\n{ts}\n{version}\n".encode()
    sig = _private_key().sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "WM_CONSUMER.ID": consumer_id,
        "WM_CONSUMER.INTIMESTAMP": ts,
        "WM_SEC.KEY_VERSION": version,
        "WM_SEC.AUTH_SIGNATURE": base64.b64encode(sig).decode(),
        "Accept": "application/json",
    }


# ------------------------------------------------------------------ API calls
def _get(path: str, params: dict, http=requests) -> dict:
    base = _env("WALMART_API_BASE", API_BASE).rstrip("/")
    publisher = _env("WALMART_PUBLISHER_ID")
    if publisher:
        params = {**params, "publisherId": publisher}
    try:
        resp = http.get(f"{base}{path}", params=params, headers=auth_headers(), timeout=20)
    except requests.RequestException as e:
        raise WalmartError(f"Couldn't reach Walmart ({type(e).__name__}).") from e
    if resp.status_code in (401, 403):
        raise WalmartError(f"Walmart rejected the credentials ({resp.status_code}). Check the consumer ID, "
                           "key version and that the uploaded public key matches the private key.")
    if resp.status_code >= 400:
        raise WalmartError(f"Walmart API error ({resp.status_code}).")
    try:
        return resp.json()
    except ValueError as e:
        raise WalmartError("Walmart returned something that isn't JSON.") from e


def search_product(term: str, http=requests) -> dict | None:
    data = _get("/search", {"query": term[:100], "numItems": 5}, http)
    for p in data.get("items") or []:
        if p.get("itemId") and str(p.get("stock", "Available")).lower() != "not available":
            return {"item_id": str(p["itemId"]), "name": p.get("name"), "price": p.get("salePrice"),
                    "size": p.get("size"), "url": p.get("productUrl") or p.get("productTrackingUrl")}
    return None


_status_lock = threading.Lock()
_status_cache: dict = {}


def status(force: bool = False, http=requests) -> dict:
    """Is the Walmart API usable right now? Runs a one-item test search (cached)."""
    if not configured():
        return {"available": False, "configured": False,
                "reason": "Walmart API isn't set up (missing " + ", ".join(missing_settings()) + ")."}
    with _status_lock:
        if not force and _status_cache.get("expires", 0) > time.time():
            return _status_cache["result"]
        try:
            search_product("milk", http)
            result = {"available": True, "configured": True}
            ttl = STATUS_TTL_OK
        except WalmartError as e:
            result = {"available": False, "configured": True, "reason": str(e)}
            ttl = STATUS_TTL_FAIL
        _status_cache.update(result=result, expires=time.time() + ttl)
        return result


def reset_status() -> None:
    _status_cache.clear()


def quantity_for(item: dict) -> int:
    q = item.get("quantity")
    if q and item.get("unit") in (None, "can", "package", "jar", "head", "bunch"):
        return max(1, min(12, math.ceil(q)))
    return 1


def cart_url(matched: list[dict]) -> str:
    qty: dict[str, int] = {}
    for m in matched:  # two list lines can match the same product (e.g. two cilantro lines)
        qty[m["item_id"]] = min(12, qty.get(m["item_id"], 0) + m["quantity"])
    items = ",".join(f"{i}|{q}" for i, q in qty.items())
    return f"{_env('WALMART_CART_URL', CART_URL)}?items={quote(items, safe=',|')}"


def build_cart(items: list[dict], http=requests) -> dict:
    """Match every list item; return the add-to-cart link plus what didn't match.
    Raises WalmartError if the API stops working partway (caller falls back)."""
    matched, unmatched, failures = [], [], 0
    for n, it in enumerate(items):
        prod = None
        if n < MAX_LOOKUPS:
            try:
                prod = search_product(it["item"], http)
            except WalmartError:
                failures += 1
                if failures >= 3 and not matched:
                    reset_status()
                    raise
        if prod:
            matched.append({"item": it["display"], "item_id": prod["item_id"], "product": prod["name"],
                            "size": prod.get("size"), "price": prod.get("price"), "quantity": quantity_for(it)})
        else:
            unmatched.append({"item": it["display"], "url": STORES["walmart"].format(q=quote_plus(it["item"]))})
    est = sum((m["price"] or 0) * m["quantity"] for m in matched)
    return {"cart_url": cart_url(matched) if matched else None, "added": matched, "unmatched": unmatched,
            "estimated_total": round(est, 2) if est else None}
