"""Walmart I/O integration: request signing, product matching, the add-to-cart link,
and the fallback to walmart.com search links when the API isn't usable."""
import base64
import json
import types

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import walmart
from conftest import recipe_id


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key, password: bytes | None = None) -> bytes:
    enc = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)


class FakeWalmart:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params, headers))
        if self.status != 200:
            return types.SimpleNamespace(status_code=self.status, json=lambda: {}, text="")
        q = params["query"]
        items = [] if "unicorn" in q or "salt" in q else [
            {"itemId": 1000 + len(q), "name": q.title(), "salePrice": 2.0, "stock": "Available"}]
        data = {"query": q, "items": items}
        return types.SimpleNamespace(status_code=200, json=lambda: data, text=json.dumps(data))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("WALMART_CONSUMER_ID", "WALMART_PRIVATE_KEY", "WALMART_PRIVATE_KEY_PATH",
              "WALMART_KEY_VERSION", "WALMART_PRIVATE_KEY_PASSWORD", "WALMART_PUBLISHER_ID"):
        monkeypatch.delenv(k, raising=False)
    walmart.reset_status()
    walmart._key_cache.clear()
    yield
    walmart.reset_status()
    walmart._key_cache.clear()


@pytest.fixture
def walmart_env(monkeypatch, tmp_path, rsa_key):
    path = tmp_path / "wm_private.pem"
    path.write_bytes(_pem(rsa_key))
    monkeypatch.setenv("WALMART_CONSUMER_ID", "consumer-123")
    monkeypatch.setenv("WALMART_KEY_VERSION", "2")
    monkeypatch.setenv("WALMART_PRIVATE_KEY_PATH", str(path))
    http = FakeWalmart()
    monkeypatch.setattr(walmart.requests, "get", http.get)
    return http


def _verify(rsa_key, headers):
    msg = f"{headers['WM_CONSUMER.ID']}\n{headers['WM_CONSUMER.INTIMESTAMP']}\n{headers['WM_SEC.KEY_VERSION']}\n"
    rsa_key.public_key().verify(base64.b64decode(headers["WM_SEC.AUTH_SIGNATURE"]), msg.encode(),
                                padding.PKCS1v15(), hashes.SHA256())


def test_signature_verifies_with_public_key(walmart_env, rsa_key):
    h = walmart.auth_headers(now_ms=1700000000000)
    assert h["WM_CONSUMER.ID"] == "consumer-123" and h["WM_SEC.KEY_VERSION"] == "2"
    assert h["WM_CONSUMER.INTIMESTAMP"] == "1700000000000"
    _verify(rsa_key, h)


@pytest.mark.parametrize("form", ["pem", "escaped", "b64pem", "b64der"])
def test_inline_key_formats(monkeypatch, rsa_key, form):
    pem = _pem(rsa_key)
    der = rsa_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    value = {"pem": pem.decode(), "escaped": pem.decode().replace("\n", "\\n"),
             "b64pem": base64.b64encode(pem).decode(), "b64der": base64.b64encode(der).decode()}[form]
    monkeypatch.setenv("WALMART_CONSUMER_ID", "c")
    monkeypatch.setenv("WALMART_PRIVATE_KEY", value)
    _verify(rsa_key, walmart.auth_headers())


def test_encrypted_key_needs_password(monkeypatch, tmp_path, rsa_key):
    path = tmp_path / "enc.pem"
    path.write_bytes(_pem(rsa_key, b"hunter2"))
    monkeypatch.setenv("WALMART_CONSUMER_ID", "c")
    monkeypatch.setenv("WALMART_PRIVATE_KEY_PATH", str(path))
    with pytest.raises(walmart.WalmartError, match="PASSWORD"):
        walmart.auth_headers()
    walmart._key_cache.clear()
    monkeypatch.setenv("WALMART_PRIVATE_KEY_PASSWORD", "hunter2")
    _verify(rsa_key, walmart.auth_headers())


def test_cart_uses_api_when_test_passes(client, walmart_env, rsa_key):
    st = client.get("/api/walmart/status").get_json()
    assert st == {"available": True, "configured": True}
    out = client.post("/api/walmart/cart", json={"recipes": [{"id": recipe_id(client, "tacos")}]}).get_json()
    assert out["mode"] == "api" and out["added"]
    assert out["cart_url"].startswith("https://affil.walmart.com/cart/addToCart?items=")
    ids = [e.split("|")[0] for e in out["cart_url"].split("items=")[1].split(",")]
    assert len(ids) == len(set(ids)) and set(ids) == {a["item_id"] for a in out["added"]}
    assert all(u["url"].startswith("https://www.walmart.com/search?q=") for u in out["unmatched"])
    url, params, headers = walmart_env.calls[-1]
    assert url.endswith("/affil/product/v2/search") and params["numItems"] == 5
    _verify(rsa_key, headers)


def test_status_is_cached(client, walmart_env):
    client.get("/api/walmart/status"); client.get("/api/walmart/status")
    assert len(walmart_env.calls) == 1
    client.get("/api/walmart/status?refresh=1")
    assert len(walmart_env.calls) == 2


def test_falls_back_to_links_when_not_configured(client):
    st = client.get("/api/walmart/status").get_json()
    assert st["available"] is False and "WALMART_CONSUMER_ID" in st["reason"]
    out = client.post("/api/walmart/cart", json={"recipes": [{"id": recipe_id(client, "tacos")}]}).get_json()
    assert out["mode"] == "links" and out["links"]
    assert out["links"][0]["url"].startswith("https://www.walmart.com/search?q=")


@pytest.mark.parametrize("code,word", [(521, "521"), (401, "credentials")])
def test_falls_back_to_links_when_api_fails(client, walmart_env, code, word):
    walmart_env.status = code
    st = client.get("/api/walmart/status").get_json()
    assert st["available"] is False and word in st["reason"]
    out = client.post("/api/walmart/cart", json={"recipes": [{"id": recipe_id(client, "tacos")}]}).get_json()
    assert out["mode"] == "links" and out["links"] and word in out["reason"]


def test_falls_back_when_api_breaks_mid_cart(client, walmart_env):
    assert client.get("/api/walmart/status").get_json()["available"]
    walmart_env.status = 503  # passed the cached test, then went down
    out = client.post("/api/walmart/cart", json={"recipes": [{"id": recipe_id(client, "tacos")}]}).get_json()
    assert out["mode"] == "links" and out["links"]


def test_cart_page_has_walmart_panel(client):
    html = client.get("/cart").get_data(as_text=True)
    assert 'id="walmart-panel"' in html and 'data-export="walmart"' not in html
