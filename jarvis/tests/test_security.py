import app as appmod
from conftest import OWNER, PASSWORD, get_csrf


def test_csrf_required_on_login_form(client, owner):
    resp = client.post("/login", data={"username": OWNER, "password": PASSWORD})
    assert resp.status_code == 400
    resp = client.post("/login", data={"csrf_token": "0" * 64, "username": OWNER, "password": PASSWORD})
    assert resp.status_code == 400


def test_csrf_required_on_setup_form(client):
    resp = client.post("/setup", data={
        "action": "account", "username": OWNER, "password": PASSWORD, "password_confirm": PASSWORD,
    })
    assert resp.status_code == 400


def test_pre_auth_token_bound_to_anonymous_cookie(make_app, owner, client):
    token = get_csrf(client, "/login")
    other = client.application.test_client()
    other.get("/login")  # gets its own anonymous cookie
    resp = other.post("/login", data={"csrf_token": token, "username": OWNER, "password": PASSWORD})
    assert resp.status_code == 400


def test_csrf_required_on_authenticated_posts(client, logged_in):
    resp = client.post("/api/tasks", json={"employee_id": 1, "command_text": "hello"})
    assert resp.status_code == 400
    resp = client.post("/api/tasks", json={"employee_id": 1, "command_text": "hello"}, headers={"X-CSRF-Token": "f" * 64})
    assert resp.status_code == 400
    resp = client.post("/settings", data={"name_1": "Nova"})
    assert resp.status_code == 400
    resp = client.post("/logout", data={})
    assert resp.status_code == 400
    assert client.get("/").status_code == 200  # still logged in


def test_task_api_requires_login(client, owner):
    resp = client.post("/api/tasks", json={"employee_id": 1, "command_text": "hello"})
    assert resp.status_code == 401
    assert client.get("/api/state").status_code == 401


def test_security_headers(client, logged_in):
    for path in ("/login", "/", "/does-not-exist", "/api/state"):
        resp = client.get(path)
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp
        assert "connect-src 'self' https://api.elevenlabs.io wss://api.elevenlabs.io" in csp
        assert "frame-ancestors 'none'" in csp
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Strict-Transport-Security"].startswith("max-age=")


def test_branded_404(client, logged_in):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert "Page not found" in resp.get_data(as_text=True)


def test_generic_500_without_details(make_app):
    app = make_app()

    @app.get("/boom-test")
    def boom():
        raise RuntimeError("internal detail that must not leak")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    client = app.test_client()
    appmod.PUBLIC_ENDPOINTS.add("boom")
    try:
        resp = client.get("/boom-test")
    finally:
        appmod.PUBLIC_ENDPOINTS.discard("boom")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "Something went wrong" in body
    assert "internal detail" not in body and "Traceback" not in body


def test_no_inline_scripts_or_console_logs(client, logged_in):
    import os
    import re
    html = client.get("/").get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    static = os.path.join(os.path.dirname(appmod.__file__), "static")
    for name in ("common.js", "dashboard.js", "voice.js"):
        with open(os.path.join(static, name), encoding="utf-8") as fh:
            assert "console.log" not in fh.read()
