import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def app(tmp_path):
    from app import create_app
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db"), "TESTING": True, "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    return app.test_client()


def recipe_id(client, q):
    return client.get(f"/api/recipes?q={q}").get_json()["results"][0]["id"]
