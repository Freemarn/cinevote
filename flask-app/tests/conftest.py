"""Shared test setup.

Gives app.py fake settings so it can load without a real
database, and provides small fakes for PostgreSQL and Redis.
"""
import os
import sys

import pytest

# Settings app.py reads when it loads. ADMIN_PASSWORD is left
# out on purpose: then app.py skips creating the admin account
# and never tries to reach PostgreSQL at startup.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.pop("ADMIN_PASSWORD", None)

# Let the tests "import app" from the flask-app folder.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as cinevote  # noqa: E402


class FakeRedis:
    """Just enough of Redis for the vote route."""

    def __init__(self):
        self.keys = {}
        self.lists = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def get(self, key):
        return self.keys.get(key)

    def setex(self, key, ttl, value):
        self.keys[key] = value

    def delete(self, key):
        self.keys.pop(key, None)

    def lpush(self, name, value):
        self.lists.setdefault(name, []).insert(0, value)

    def ping(self):
        return True


def fake_query(sql, params=(), one=False, commit=False):
    """Answers the few SQL queries the tests trigger."""
    if "FROM users" in sql:
        return {"id": 1, "username": "ada", "is_admin": False}
    if "FROM movies" in sql:
        return {"id": 3, "title": "Inception"}
    if "FROM votes" in sql:
        return None  # this user has not voted yet
    return None if one else []


@pytest.fixture
def client():
    cinevote.app.config.update(TESTING=True)
    with cinevote.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cinevote, "rdb", fake)
    return fake


@pytest.fixture
def logged_in(client, monkeypatch):
    """A client logged in as user 1 ("ada"), not an admin."""
    monkeypatch.setattr(cinevote, "query", fake_query)
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["csrf_token"] = "test-token"
    return client
