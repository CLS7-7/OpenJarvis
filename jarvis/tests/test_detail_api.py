import re
import secrets

import app as appmod
from conftest import do_setup, login
from test_tasks import BRIDGE_URL, StubBridge, age_tasks, poll

ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def bridged_client(make_app, monkeypatch):
    app = make_app(BRIDGE_URL=BRIDGE_URL, BRIDGE_SECRET=secrets.token_urlsafe(32))
    client = app.test_client()
    secret, _ = do_setup(client)
    csrf = login(client, secret)
    stub = StubBridge()
    monkeypatch.setattr(appmod.requests, "request", stub)
    return app, client, csrf, stub


def test_detail_endpoints_require_login(client, owner):
    assert client.get("/api/tasks/ABU-00001").status_code == 401
    assert client.get("/api/employees/1").status_code == 401


def test_task_detail_full_fields_and_timeline(make_app, monkeypatch):
    app, client, csrf, stub = bridged_client(make_app, monkeypatch)
    command = "Summarise the inbox\nthen flag anything urgent"
    client.post("/api/tasks", json={"employee_id": 3, "command_text": command, "source": "voice"},
                headers={"X-CSRF-Token": csrf})

    task = client.get("/api/tasks/ABU-00001").get_json()["task"]
    assert task["ref"] == "ABU-00001" and task["employee_id"] == 3 and task["employee_key"] == "EMPLOYEE_3"
    assert task["command_text"] == command and task["source"] == "voice"
    assert task["status"] == "sent" and task["bridge_task_id"] == "hb-ABU-00001"
    assert ISO.fullmatch(task["created_at"]) and ISO.fullmatch(task["sent_at"]) and ISO.fullmatch(task["updated_at"])
    assert task["started_at"] is None and task["finished_at"] is None and task["result_text"] is None

    stub.status["hb-ABU-00001"] = {"status": "working", "progress": 55}
    poll(app)
    task = client.get("/api/tasks/ABU-00001").get_json()["task"]
    assert task["status"] == "working" and task["progress"] == 55 and ISO.fullmatch(task["started_at"])

    long_result = "line one\n" + "x" * 5000
    stub.status["hb-ABU-00001"] = {"status": "done", "progress": 100, "result": long_result}
    poll(app)
    task = client.get("/api/tasks/ABU-00001").get_json()["task"]
    assert task["status"] == "done" and ISO.fullmatch(task["finished_at"])
    assert task["result_text"] == long_result  # full text, not truncated


def test_task_detail_unknown_ref_is_json_404(client, logged_in):
    resp = client.get("/api/tasks/ABU-99999")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Task not found."}


def test_employee_detail(make_app, monkeypatch):
    app, client, csrf, stub = bridged_client(make_app, monkeypatch)
    for n in range(12):
        client.post("/api/tasks", json={"employee_id": 2, "command_text": f"Job {n}"}, headers={"X-CSRF-Token": csrf})
    client.post("/api/tasks", json={"employee_id": 1, "command_text": "Other"}, headers={"X-CSRF-Token": csrf})
    stub.status["hb-ABU-00001"] = {"status": "done", "progress": 100, "result": "R" * 900}
    stub.status["hb-ABU-00002"] = {"status": "failed", "result": "boom"}
    stub.status["hb-ABU-00003"] = {"status": "working", "progress": 10}
    poll(app)

    emp = client.get("/api/employees/2").get_json()["employee"]
    assert emp["id"] == 2 and emp["key"] == "EMPLOYEE_2" and emp["status"] == "working"
    assert emp["current_task"]["ref"] == "ABU-00003"
    assert [t["ref"] for t in emp["recent_tasks"]] == [f"ABU-{n:05d}" for n in range(12, 2, -1)]
    assert set(emp["recent_tasks"][0]) >= {"ref", "status", "status_label", "progress", "title", "url"}
    assert emp["counts"]["total"] == 12
    assert emp["counts"]["done"] == 1 and emp["counts"]["failed"] == 1 and emp["counts"]["working"] == 1
    assert emp["counts"]["sent"] == 9
    latest = emp["latest_result"]
    assert latest["ref"] == "ABU-00002" and latest["preview"] == "boom" and latest["truncated"] is False

    assert client.get("/api/employees/99").status_code == 404
    assert client.get("/api/employees/99").get_json() == {"error": "Employee not found."}


def test_state_bridge_contact_times(make_app, monkeypatch):
    app, client, csrf, stub = bridged_client(make_app, monkeypatch)
    bridge = client.get("/api/state").get_json()["bridge"]
    assert bridge["last_ok"] is None and bridge["last_fail"] is None
    client.post("/api/tasks", json={"employee_id": 1, "command_text": "Ping"}, headers={"X-CSRF-Token": csrf})
    bridge = client.get("/api/state").get_json()["bridge"]
    assert ISO.fullmatch(bridge["last_ok"]) and bridge["last_fail"] is None
    stub.fail = True
    age_tasks(app)
    poll(app)
    bridge = client.get("/api/state").get_json()["bridge"]
    assert ISO.fullmatch(bridge["last_fail"])
