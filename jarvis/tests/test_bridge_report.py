import os
import secrets
import sqlite3

import pytest

import app as appmod
from conftest import do_setup, login
from test_tasks import BRIDGE_URL, StubBridge

URL = "/api/bridge/report"


@pytest.fixture
def bridge_secret():
    return secrets.token_urlsafe(32)  # generated per test run; not a real secret


@pytest.fixture
def report_app(make_app, bridge_secret):
    return make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=bridge_secret)


def auth(secret):
    return {"Authorization": "Bearer " + secret}


def body(**overrides):
    data = {"ref": "HRM-1001", "employee_key": "EMPLOYEE_3", "command_text": "Book the dentist for Tuesday",
            "source": "voice", "status": "queued"}
    data.update(overrides)
    return data


def test_report_is_off_without_bridge_secret(app):
    client = app.test_client()
    resp = client.post(URL, json=body(), headers=auth("anything-at-all"))
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not found."}


def test_report_rejects_missing_or_wrong_secret(report_app, bridge_secret):
    client = report_app.test_client()
    assert client.post(URL, json=body()).status_code == 401
    wrong = client.post(URL, json=body(), headers=auth(bridge_secret + "x"))
    assert wrong.status_code == 401
    assert wrong.get_json() == {"error": "Unauthorized."}
    assert bridge_secret not in wrong.get_data(as_text=True)
    basic = client.post(URL, json=body(), headers={"Authorization": "Basic " + bridge_secret})
    assert basic.status_code == 401


def test_report_needs_no_session_or_csrf_but_other_posts_still_do(report_app, bridge_secret):
    client = report_app.test_client()
    assert client.post(URL, json=body(), headers=auth(bridge_secret)).status_code == 201
    assert "Set-Cookie" not in client.post(URL, json=body(), headers=auth(bridge_secret)).headers
    # The bearer secret grants nothing elsewhere.
    resp = client.post("/api/tasks", json={"employee_id": 1, "command_text": "x"}, headers=auth(bridge_secret))
    assert resp.status_code == 401
    assert client.get("/api/state", headers=auth(bridge_secret)).status_code == 401
    assert client.get(URL, headers=auth(bridge_secret)).status_code == 401


def test_report_create_then_update_round_trip(report_app, bridge_secret):
    client = report_app.test_client()
    resp = client.post(URL, json=body(), headers=auth(bridge_secret))
    assert resp.status_code == 201
    task = resp.get_json()["task"]
    assert task["ref"] == "HRM-1001" and task["employee_id"] == 3 and task["source"] == "voice"
    assert task["status"] == "sent"  # Hermes holds it: shown as "Sent to Hermes"
    assert task["source_label"] == "Voice"

    resp = client.post(URL, json=body(status="working", progress=35), headers=auth(bridge_secret))
    assert resp.status_code == 200
    assert resp.get_json()["task"]["status"] == "working" and resp.get_json()["task"]["progress"] == 35

    long_result = "Booked.\n" + "z" * (appmod.RESULT_MAX_CHARS + 50)
    resp = client.post(URL, json=body(status="done", result=long_result), headers=auth(bridge_secret))
    assert resp.status_code == 200
    task = resp.get_json()["task"]
    assert task["status"] == "done" and task["progress"] == 100 and task["finished_at"]

    conn = sqlite3.connect(report_app.config["DB_PATH"])
    row = conn.execute("SELECT result_text, started_at, finished_at, sent_at, command_text FROM tasks WHERE ref = 'HRM-1001'").fetchone()
    conn.close()
    assert len(row[0]) == appmod.RESULT_MAX_CHARS and row[0].startswith("Booked.\n")
    assert row[1] and row[2] and row[3] and row[4] == "Book the dentist for Tuesday"


def test_report_never_resurrects_finished_tasks(report_app, bridge_secret):
    client = report_app.test_client()
    client.post(URL, json=body(status="failed", result="Clinic closed"), headers=auth(bridge_secret))
    for status in ("queued", "working", "done"):
        resp = client.post(URL, json=body(status=status, progress=10, result="changed"), headers=auth(bridge_secret))
        assert resp.status_code == 200
        task = resp.get_json()["task"]
        assert task["status"] == "failed"
    conn = sqlite3.connect(report_app.config["DB_PATH"])
    finished, result = conn.execute("SELECT finished_at, result_text FROM tasks WHERE ref = 'HRM-1001'").fetchone()
    conn.close()
    assert finished and result == "Clinic closed"
    # Working never steps back to queued either.
    client.post(URL, json=body(ref="HRM-2", status="working", progress=50), headers=auth(bridge_secret))
    resp = client.post(URL, json=body(ref="HRM-2", status="queued"), headers=auth(bridge_secret))
    assert resp.get_json()["task"]["status"] == "working"


@pytest.mark.parametrize("bad", [
    {"employee_key": "EMPLOYEE_9"},
    {"employee_key": None},
    {"status": "sent"},
    {"status": "teleported"},
    {"ref": "bad ref with spaces"},
    {"ref": "x" * 65},
    {"ref": ""},
    {"command_text": "   "},
    {"command_text": "y" * 4001},
    {"source": "text"},
    {"progress": 101},
    {"progress": True},
    {"progress": "50"},
    {"result": 42},
    {"unexpected": "field"},
])
def test_report_validation(report_app, bridge_secret, bad):
    client = report_app.test_client()
    resp = client.post(URL, json=body(**bad), headers=auth(bridge_secret))
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_report_rejects_non_json(report_app, bridge_secret):
    client = report_app.test_client()
    resp = client.post(URL, data="not json", headers={**auth(bridge_secret), "Content-Type": "text/plain"})
    assert resp.status_code == 400


def test_report_rate_limited_per_ip(report_app, bridge_secret, monkeypatch):
    monkeypatch.setattr(appmod, "REPORT_RATE_PER_MINUTE", 3)
    client = report_app.test_client()
    for _ in range(3):
        assert client.post(URL, json=body(), headers=auth(bridge_secret)).status_code in (200, 201)
    assert client.post(URL, json=body(), headers=auth(bridge_secret)).status_code == 429
    other = {**auth(bridge_secret), "CF-Connecting-IP": "203.0.113.50"}
    assert client.post(URL, json=body(), headers=other).status_code == 200


def test_reported_task_flows_to_every_surface(report_app, bridge_secret, monkeypatch):
    hermes = report_app.test_client()
    owner = report_app.test_client()
    secret, _ = do_setup(owner)
    login(owner, secret)

    hermes.post(URL, json=body(status="working", progress=40), headers=auth(bridge_secret))
    state = owner.get("/api/state").get_json()
    assert state["recent_tasks"][0]["ref"] == "HRM-1001"
    emp = next(e for e in state["employees"] if e["key"] == "EMPLOYEE_3")
    assert emp["status"] == "working" and emp["progress"] == 40
    assert emp["current_task"]["ref"] == "HRM-1001"
    assert state["stats"]["working"] == 1

    assert owner.get("/api/employees/3").get_json()["employee"]["counts"]["working"] == 1
    assert owner.get("/api/tasks/HRM-1001").get_json()["task"]["command_text"] == "Book the dentist for Tuesday"
    assert "HRM-1001" in owner.get("/reports").get_data(as_text=True)
    detail = owner.get("/reports/HRM-1001").get_data(as_text=True)
    assert "Book the dentist for Tuesday" in detail and "Voice" in detail

    # A reported task is never forwarded back out to Hermes or polled by the site's poller.
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)
    conn = appmod.connect(report_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE tasks SET updated_at = updated_at - 600")
        appmod.Poller(report_app.config["DB_PATH"], appmod.bridge_from_config(report_app.config)).run_once(conn)
    finally:
        conn.close()
    assert [(c["method"], c["url"]) for c in stub.calls] == [("GET", BRIDGE_URL + "/health")]


def test_tasks_table_migrates_to_allow_external_source(tmp_path):
    db_path = os.path.join(tmp_path, "old.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
        "color TEXT NOT NULL, avatar_style TEXT NOT NULL, sort INTEGER NOT NULL);"
        "INSERT INTO employees VALUES (1, 'EMPLOYEE_1', 'Nova', '#7C5CFF', 'antenna', 1);"
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, employee_id INTEGER NOT NULL REFERENCES employees(id),"
        " command_text TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('text', 'voice')),"
        " status TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'working', 'done', 'failed')),"
        " progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100), result_text TEXT, bridge_task_id TEXT,"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, finished_at INTEGER);"
        "INSERT INTO tasks (ref, employee_id, command_text, source, status, progress, created_at, updated_at)"
        " VALUES ('ABU-00001', 1, 'old task', 'text', 'done', 100, 1, 2);"
    )
    conn.commit()
    conn.close()
    appmod.init_db(db_path)
    appmod.init_db(db_path)  # idempotent
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT ref, command_text, sent_at FROM tasks").fetchall() == [("ABU-00001", "old task", None)]
    conn.execute("INSERT INTO tasks (ref, employee_id, command_text, source, status, created_at, updated_at)"
                 " VALUES ('HRM-1', 1, 'from hermes', 'external', 'sent', 3, 3)")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'")}
    assert {"idx_tasks_employee", "idx_tasks_status"} <= names
    conn.close()


def test_voice_path_no_longer_submits_tasks():
    path = os.path.join(os.path.dirname(appmod.__file__), "static", "voice.js")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    assert "/api/tasks" not in source
    assert "createTask" not in source
    assert '"Heard: "' in source
    assert "Your agent passes voice orders to Hermes — the task appears here as soon as Hermes reports it." in source
    assert "No command captured from that conversation." in source
