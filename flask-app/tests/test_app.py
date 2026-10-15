"""Unit tests for the Flask app. No database or Redis needed."""
import json
from datetime import datetime, timezone

import app as cinevote


def test_week_key_format():
    monday = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert cinevote.week_key(monday) == "2026-W41"


def test_week_key_belongs_to_iso_year():
    # 1 Jan 2027 is a Friday, so it is still week 53 of 2026
    new_year = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert cinevote.week_key(new_year) == "2026-W53"


def test_login_page_loads(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b"Welcome back" in response.data


def test_register_page_loads(client):
    response = client.get("/register")
    assert response.status_code == 200
    assert b"Join the vote" in response.data


def test_home_page_needs_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_form_without_csrf_token_is_rejected(client):
    response = client.post("/vote", data={"movie_id": "3"})
    assert response.status_code == 400


def test_vote_goes_onto_the_redis_queue(logged_in, fake_redis):
    response = logged_in.post(
        "/vote", data={"movie_id": "3", "csrf_token": "test-token"})
    assert response.status_code == 302
    queue = fake_redis.lists["votes"]
    assert len(queue) == 1
    vote = json.loads(queue[0])
    assert vote["user_id"] == 1
    assert vote["movie_id"] == 3


def test_only_one_vote_per_user(logged_in, fake_redis):
    form = {"movie_id": "3", "csrf_token": "test-token"}
    logged_in.post("/vote", data=form)
    logged_in.post("/vote", data=form)
    assert len(fake_redis.lists["votes"]) == 1


def test_admin_page_forbidden_for_normal_user(logged_in):
    response = logged_in.get("/admin")
    assert response.status_code == 403


def test_health_ok_when_services_answer(client, fake_redis, monkeypatch):
    monkeypatch.setattr(cinevote, "query", lambda *a, **k: [])
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"redis": "ok", "postgres": "ok"}


def test_health_fails_when_services_are_down(client):
    # Nothing runs on 127.0.0.1 in the test, so both checks fail
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"redis": "down", "postgres": "down"}
