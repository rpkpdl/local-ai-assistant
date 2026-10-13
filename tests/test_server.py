"""The web server only serves this machine and refuses requests other websites could forge."""

import pytest
from fastapi.testclient import TestClient

from assistant.server import app

ORIGIN = {"Origin": "http://127.0.0.1:8000"}


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


@pytest.mark.parametrize("host", ["attacker.example", "192.168.1.20:8000", "0.0.0.0:8000"])
def test_rejects_foreign_host_header(client, host):
    """DNS rebinding: a page on attacker.example re-pointed to 127.0.0.1 still sends its Host."""
    assert client.get("/", headers={"Host": host}).status_code == 400


def test_allows_localhost_host_header(client):
    assert client.get("/", headers={"Host": "localhost:8000"}).status_code == 200


def test_rejects_cross_origin_post(client):
    r = client.post("/api/reset", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_rejects_non_json_post(client):
    """HTML forms can POST cross-site without CORS preflight, but can't send application/json."""
    r = client.post("/api/reset", content="x", headers={**ORIGIN, "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_accepts_same_origin_json_post(client):
    assert client.post("/api/reset", json={}, headers=ORIGIN).status_code == 200


def test_chat_rejects_empty_message(client):
    assert client.post("/api/chat", json={"message": "  "}, headers=ORIGIN).status_code == 400


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_api_docs(client, path):
    assert client.get(path).status_code == 404
