import secrets

import requests

import app as appmod
from conftest import FakeResponse, do_setup, login


def test_voice_token_requires_login(client, owner):
    assert client.get("/api/voice/token").status_code == 401


def test_voice_token_503_when_unconfigured(client, logged_in):
    resp = client.get("/api/voice/token")
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "Voice not configured yet."}
    assert "Voice not configured yet" in client.get("/").get_data(as_text=True)
    assert "Not configured" in client.get("/settings").get_data(as_text=True)


def test_voice_token_passthrough_never_leaks_key(make_app, monkeypatch):
    api_key = "test-" + secrets.token_hex(24)
    agent_id = "agent_" + secrets.token_hex(6)
    app = make_app(ELEVENLABS_API_KEY=api_key, ELEVENLABS_AGENT_ID=agent_id)
    client = app.test_client()
    secret, _ = do_setup(client)
    login(client, secret)

    seen = {}
    short_lived = "convtok_" + secrets.token_hex(12)

    def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        seen.update(url=url, params=params, headers=headers)
        return FakeResponse(200, {"token": short_lived})

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    resp = client.get("/api/voice/token")
    assert resp.status_code == 200
    assert resp.get_json() == {"token": short_lived}
    body = resp.get_data(as_text=True)
    assert api_key not in body
    assert seen["url"] == "https://api.elevenlabs.io/v1/convai/conversation/token"
    assert seen["params"] == {"agent_id": agent_id}
    assert seen["headers"]["xi-api-key"] == api_key
    assert resp.headers["Cache-Control"] == "no-store"

    for path in ("/", "/settings", "/reports", "/api/state"):
        page = client.get(path).get_data(as_text=True)
        assert api_key not in page
    assert "Configured" in client.get("/settings").get_data(as_text=True)


def test_voice_token_upstream_failure_is_generic(make_app, monkeypatch):
    api_key = "test-" + secrets.token_hex(24)
    app = make_app(ELEVENLABS_API_KEY=api_key, ELEVENLABS_AGENT_ID="agent_x")
    client = app.test_client()
    secret, _ = do_setup(client)
    login(client, secret)

    def failing_get(*args, **kwargs):
        raise requests.ConnectionError("failed for key " + api_key)

    monkeypatch.setattr(appmod.requests, "get", failing_get)
    resp = client.get("/api/voice/token")
    assert resp.status_code == 502
    assert api_key not in resp.get_data(as_text=True)

    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: FakeResponse(401, {"detail": api_key}))
    resp = client.get("/api/voice/token")
    assert resp.status_code == 502
    assert api_key not in resp.get_data(as_text=True)
