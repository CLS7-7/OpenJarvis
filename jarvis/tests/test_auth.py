import sqlite3

import pyotp
import pytest

import app as appmod
from conftest import (
    OWNER, PASSWORD, csrf_from, do_setup, get_csrf, login, password_step, totp_step, wrong_code,
)


def test_refuses_to_boot_without_strong_secret_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        appmod.create_app()
    monkeypatch.setenv("SECRET_KEY", "x" * 31)
    with pytest.raises(RuntimeError):
        appmod.create_app()


def test_setup_is_one_time_only(app, client):
    assert client.get("/login").headers["Location"].endswith("/setup")
    page = client.get("/setup")
    assert page.status_code == 200
    assert "<svg" in page.get_data(as_text=True)

    secret, codes = do_setup(client)

    assert client.get("/setup").status_code == 404
    token = get_csrf(client, "/login")
    resp = client.post("/setup", data={
        "csrf_token": token, "action": "account", "username": "intruder",
        "password": "another long password", "password_confirm": "another long password",
    })
    assert resp.status_code == 404
    assert client.post("/setup", data={}).status_code == 404

    conn = sqlite3.connect(app.config["DB_PATH"])
    users = conn.execute("SELECT username, pw_hash FROM users").fetchall()
    assert users == [(OWNER, users[0][1])]
    assert users[0][1].startswith("$2b$")
    stored = [r[0] for r in conn.execute("SELECT code_hash FROM recovery_codes").fetchall()]
    assert len(stored) == 8
    for code in codes:
        assert code not in stored
    assert all(h.startswith("$2b$") for h in stored)


def test_setup_qr_is_inline_svg_without_external_calls(client):
    token = get_csrf(client, "/setup")
    client.post("/setup", data={
        "csrf_token": token, "action": "account", "username": OWNER,
        "password": PASSWORD, "password_confirm": PASSWORD,
    })
    html = client.get("/setup").get_data(as_text=True)
    assert '<svg xmlns="http://www.w3.org/2000/svg" viewBox=' in html
    assert "otpauth://" not in html  # the URI is only inside the QR, not in the markup
    assert "chart.googleapis" not in html and "api.qrserver" not in html


def test_setup_production_bcrypt_cost_is_at_least_12(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    application = appmod.create_app({"SECRET_KEY": "k" * 40, "START_POLLER": False, "BCRYPT_ROUNDS": 4})
    assert application.config["BCRYPT_ROUNDS"] >= 12


def test_pages_redirect_to_login_when_logged_out(client, owner):
    for path in ("/", "/reports", "/reports/ABU-00001", "/settings", "/no-such-page"):
        resp = client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["Location"].endswith("/login"), path


def test_login_requires_password_and_totp(client, owner):
    resp = password_step(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login/totp")
    # Password alone grants nothing.
    assert client.get("/").status_code == 302
    assert client.get("/api/state").status_code == 401

    resp = totp_step(client, wrong_code(owner["secret"]))
    assert resp.status_code == 401
    assert client.get("/").status_code == 302

    resp = totp_step(client, pyotp.TOTP(owner["secret"]).now())
    assert resp.status_code == 302
    assert client.get("/").status_code == 200


def test_totp_step_without_password_step_redirects(client, owner):
    resp = client.get("/login/totp")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


def test_wrong_password_rejected(client, owner):
    resp = password_step(client, password="not the password at all")
    assert resp.status_code == 401
    assert "Incorrect username or password." in resp.get_data(as_text=True)
    resp = password_step(client, username="nobody")
    assert resp.status_code == 401


def test_recovery_code_works_once(client, owner):
    code = owner["codes"][0]
    assert password_step(client).status_code == 302
    resp = totp_step(client, code.lower().replace("-", " "))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    csrf = csrf_from(client.get("/").get_data(as_text=True))
    client.post("/logout", data={"csrf_token": csrf})

    assert password_step(client).status_code == 302
    resp = totp_step(client, code)
    assert resp.status_code == 401


def test_totp_code_cannot_be_replayed(client, owner):
    login(client, owner["secret"])
    csrf = csrf_from(client.get("/").get_data(as_text=True))
    client.post("/logout", data={"csrf_token": csrf})
    assert password_step(client).status_code == 302
    resp = totp_step(client, pyotp.TOTP(owner["secret"]).now())
    assert resp.status_code == 401


def test_password_lockout_after_five_failures(client, owner):
    for _ in range(4):
        assert password_step(client, password="wrong password guess").status_code == 401
    resp = password_step(client, password="wrong password guess")
    assert resp.status_code == 429
    # Even the correct password is refused while locked.
    resp = password_step(client)
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.get_data(as_text=True)
    # Lockout is per client IP: a different client is not affected.
    other = {"CF-Connecting-IP": "203.0.113.9"}
    assert password_step(client, headers=other).status_code == 302


def test_lockout_expires_after_15_minutes(client, owner, monkeypatch):
    for _ in range(5):
        password_step(client, password="wrong password guess")
    assert password_step(client).status_code == 429
    real_now = appmod.now()
    monkeypatch.setattr(appmod, "now", lambda: real_now + 15 * 60 + 1)
    assert password_step(client).status_code == 302


def test_totp_lockout_counted_separately(client, owner):
    # Four password failures do not count against the TOTP step.
    for _ in range(4):
        password_step(client, password="wrong password guess")
    assert password_step(client).status_code == 302
    bad = wrong_code(owner["secret"])
    for _ in range(4):
        assert totp_step(client, bad).status_code == 401
    assert totp_step(client, bad).status_code == 429
    resp = totp_step(client, pyotp.TOTP(owner["secret"]).now())
    assert resp.status_code == 429
    assert client.get("/").status_code == 302


def test_logout_clears_session(client, logged_in):
    old_cookie = client.get_cookie(appmod.SESSION_COOKIE).value
    resp = client.post("/logout", data={"csrf_token": logged_in["csrf"]})
    assert resp.status_code == 302
    assert client.get("/").status_code == 302
    client.set_cookie(appmod.SESSION_COOKIE, old_cookie, secure=True, httponly=True)
    assert client.get("/").status_code == 302


def test_session_cookie_flags(client, owner):
    password_step(client)
    resp = totp_step(client, pyotp.TOTP(owner["secret"]).now())
    cookies = [v for k, v in resp.headers.items() if k == "Set-Cookie" and v.startswith(appmod.SESSION_COOKIE + "=")]
    assert cookies
    flags = cookies[0].lower()
    assert "secure" in flags and "httponly" in flags and "samesite=lax" in flags


def test_session_idle_timeout(client, logged_in, monkeypatch):
    real_now = appmod.now()
    monkeypatch.setattr(appmod, "now", lambda: real_now + 12 * 3600 + 5)
    assert client.get("/").status_code == 302
