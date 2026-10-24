import re
import secrets
import sqlite3

import requests

import app as appmod
from conftest import FakeResponse, login, do_setup

BRIDGE_URL = "https://hermes.bridge.test"


class StubBridge:
    """Stands in for requests.request against the Hermes bridge."""

    def __init__(self):
        self.calls = []
        self.status = {}
        self.fail = False

    def __call__(self, method, url, headers=None, json=None, timeout=None, allow_redirects=None, **kw):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        if self.fail:
            raise requests.ConnectionError("connection refused")
        if method == "POST" and url == BRIDGE_URL + "/tasks":
            return FakeResponse(200, {"bridge_task_id": "hb-" + json["ref"]})
        if method == "GET" and url.startswith(BRIDGE_URL + "/tasks/"):
            return FakeResponse(200, self.status.get(url.rsplit("/", 1)[1], {"status": "queued"}))
        if method == "GET" and url == BRIDGE_URL + "/health":
            return FakeResponse(200, {"ok": True})
        return FakeResponse(404, {})


def setup_logged_in(client):
    secret, _ = do_setup(client)
    return login(client, secret)


def poll(app):
    # One poller per app, like one long-lived thread per worker (it holds the lease).
    poller = app.extensions.setdefault(
        "test_poller", appmod.Poller(app.config["DB_PATH"], appmod.bridge_from_config(app.config))
    )
    conn = appmod.connect(app.config["DB_PATH"])
    try:
        poller.run_once(conn)
    finally:
        conn.close()


def age_tasks(app):
    """Move the retry grace window into the past (the poller waits 20s after a forward attempt)."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("UPDATE tasks SET updated_at = updated_at - 60")
    conn.commit()
    conn.close()


def test_task_forwarding_contract(make_app, monkeypatch):
    bridge_secret = secrets.token_urlsafe(32)
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=bridge_secret)
    client = app.test_client()
    csrf = setup_logged_in(client)
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)

    resp = client.post("/api/tasks", json={"employee_id": 2, "command_text": "Draft the weekly summary"},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["task"]["ref"] == "ABU-00001"
    assert data["task"]["status"] == "sent"
    assert data["bridge"]["connected"] is True

    call = stub.calls[0]
    assert call["method"] == "POST" and call["url"] == BRIDGE_URL + "/tasks"
    assert call["json"] == {"ref": "ABU-00001", "employee_key": "EMPLOYEE_2", "command": "Draft the weekly summary"}
    assert call["headers"]["Authorization"] == "Bearer " + bridge_secret

    # Bridge reports progress.
    stub.status["hb-ABU-00001"] = {"status": "working", "progress": 40}
    poll(app)
    state = client.get("/api/state").get_json()
    emp = next(e for e in state["employees"] if e["id"] == 2)
    assert emp["status"] == "working" and emp["progress"] == 40
    assert emp["current_task"]["ref"] == "ABU-00001"
    assert 'class="emp-card is-working" data-employee-id="2"' in client.get("/").get_data(as_text=True)

    # Bridge reports completion with a result that must be escaped on display.
    stub.status["hb-ABU-00001"] = {"status": "done", "progress": 100, "result": "<b>Summary</b>\nline two"}
    poll(app)
    state = client.get("/api/state").get_json()
    emp = next(e for e in state["employees"] if e["id"] == 2)
    assert emp["status"] == "done"
    assert state["recent_tasks"][0]["status"] == "done"
    assert state["recent_tasks"][0]["finished_at"]

    detail = client.get("/reports/ABU-00001").get_data(as_text=True)
    assert "&lt;b&gt;Summary&lt;/b&gt;\nline two" in detail
    assert "<b>Summary</b>" not in detail

    # The bridge secret never leaves the server.
    for path in ("/", "/reports", "/reports/ABU-00001", "/settings", "/api/state"):
        assert bridge_secret not in client.get(path).get_data(as_text=True)


def test_text_and_voice_share_one_pipeline(make_app, monkeypatch):
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=secrets.token_urlsafe(32))
    client = app.test_client()
    csrf = setup_logged_in(client)
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)
    resp = client.post("/api/tasks", json={"employee_id": 3, "command_text": "Book the venue", "source": "voice"},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 201
    assert resp.get_json()["task"]["source"] == "voice"
    assert stub.calls[0]["json"]["employee_key"] == "EMPLOYEE_3"
    resp = client.post("/api/tasks", json={"employee_id": 3, "command_text": "x", "source": "email"},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_task_validation(client, logged_in):
    h = {"X-CSRF-Token": logged_in["csrf"]}
    assert client.post("/api/tasks", json={"employee_id": 1, "command_text": "   "}, headers=h).status_code == 400
    assert client.post("/api/tasks", json={"employee_id": 99, "command_text": "hi"}, headers=h).status_code == 400
    assert client.post("/api/tasks", json={"employee_id": 1, "command_text": "x" * 4001}, headers=h).status_code == 400


def test_bridge_not_configured_is_honest(client, logged_in):
    resp = client.post("/api/tasks", json={"employee_id": 1, "command_text": "Check the inbox"},
                       headers={"X-CSRF-Token": logged_in["csrf"]})
    assert resp.status_code == 201
    task = resp.get_json()["task"]
    assert task["status"] == "queued" and task["progress"] == 0
    state = client.get("/api/state").get_json()
    assert state["bridge"] == {"configured": False, "connected": False, "label": "Not configured"}
    emp = next(e for e in state["employees"] if e["id"] == 1)
    assert emp["status"] == "idle" and emp["progress"] == 0
    html = client.get("/").get_data(as_text=True)
    assert "Bridge not connected — tasks will send when Hermes is connected." in html
    banner_tag = re.search(r'<p id="bridge-banner"[^>]*>', html).group(0)
    assert "hidden" not in banner_tag
    assert "Not configured" in client.get("/settings").get_data(as_text=True)


def test_bridge_unreachable_keeps_task_queued(make_app, monkeypatch):
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=secrets.token_urlsafe(32))
    client = app.test_client()
    csrf = setup_logged_in(client)
    stub = StubBridge()
    stub.fail = True
    monkeypatch.setattr(appmod.requests, "request", stub)

    resp = client.post("/api/tasks", json={"employee_id": 4, "command_text": "Sync files"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 201
    assert resp.get_json()["task"]["status"] == "queued"
    assert resp.get_json()["bridge"]["connected"] is False

    # A later poll while still unreachable changes nothing.
    age_tasks(app)
    poll(app)
    state = client.get("/api/state").get_json()
    assert state["bridge"]["connected"] is False
    assert state["bridge"]["label"] == "Configured — not reachable"
    assert state["recent_tasks"][0]["status"] == "queued"
    assert state["recent_tasks"][0]["progress"] == 0

    # Once Hermes is back, the poller sends the queued task.
    stub.fail = False
    age_tasks(app)
    app.extensions["test_poller"].backoff.clear()  # skip the per-task retry backoff
    poll(app)
    state = client.get("/api/state").get_json()
    assert state["recent_tasks"][0]["status"] == "sent"
    assert state["bridge"]["connected"] is True
    posts = [c for c in stub.calls if c["method"] == "POST"]
    assert posts[-1]["json"]["ref"] == "ABU-00001"


def test_only_one_worker_polls_at_a_time(make_app, monkeypatch):
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=secrets.token_urlsafe(32))
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)
    bridge = appmod.bridge_from_config(app.config)
    first = appmod.Poller(app.config["DB_PATH"], bridge)
    second = appmod.Poller(app.config["DB_PATH"], bridge)
    conn = appmod.connect(app.config["DB_PATH"])
    try:
        assert first._acquire_lease(conn) is True
        assert second._acquire_lease(conn) is False
        assert first._acquire_lease(conn) is True
    finally:
        conn.close()


def test_bridge_garbage_does_not_fake_status(make_app, monkeypatch):
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=secrets.token_urlsafe(32))
    client = app.test_client()
    csrf = setup_logged_in(client)
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)
    client.post("/api/tasks", json={"employee_id": 5, "command_text": "Run report"}, headers={"X-CSRF-Token": csrf})
    stub.status["hb-ABU-00001"] = {"status": "teleported", "progress": 77}
    poll(app)
    task = client.get("/api/state").get_json()["recent_tasks"][0]
    assert task["status"] == "sent" and task["progress"] == 0


def test_reports_list_and_empty_state(client, logged_in):
    html = client.get("/reports").get_data(as_text=True)
    assert "No reports yet" in html
    client.post("/api/tasks", json={"employee_id": 1, "command_text": "First <task>"},
                headers={"X-CSRF-Token": logged_in["csrf"]})
    html = client.get("/reports").get_data(as_text=True)
    assert "ABU-00001" in html and "First &lt;task&gt;" in html
    assert client.get("/reports/ABU-99999").status_code == 404
