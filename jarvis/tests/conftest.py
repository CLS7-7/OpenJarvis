import os
import re
import secrets
import sys

import pyotp
import pytest
from flask.testing import FlaskClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as appmod  # noqa: E402

OPTIONAL_ENV = ("BRIDGE_URL", "BRIDGE_SECRET", "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID")


class HttpsClient(FlaskClient):
    """Test client that talks HTTPS so Secure cookies round-trip like behind Caddy."""

    def open(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            kwargs.setdefault("base_url", "https://localhost")
        return super().open(*args, **kwargs)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    def _make(**env):
        for key in OPTIONAL_ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        application = appmod.create_app({
            "SECRET_KEY": secrets.token_urlsafe(48),  # generated per test run; not a real secret
            "TESTING": True,
            "BCRYPT_ROUNDS": 4,
        })
        application.test_client_class = HttpsClient
        return application
    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([0-9a-f]{64})"', html)
    assert match, "page has no CSRF meta tag"
    return match.group(1)


def get_csrf(client, path="/login") -> str:
    resp = client.get(path)
    return csrf_from(resp.get_data(as_text=True))


def wrong_code(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    now = appmod.now()
    valid = {totp.at(now + d) for d in (-60, -30, 0, 30, 60)}
    for n in range(1000000):
        candidate = f"{n:06d}"
        if candidate not in valid:
            return candidate
    raise AssertionError("unreachable")


OWNER = "owner_test"
PASSWORD = "correct horse battery staple"


def do_setup(client, username=OWNER, password=PASSWORD):
    token = get_csrf(client, "/setup")
    resp = client.post("/setup", data={
        "csrf_token": token, "action": "account", "username": username,
        "password": password, "password_confirm": password,
    })
    assert resp.status_code == 302
    page = client.get("/setup").get_data(as_text=True)
    secret = re.search(r'<p class="secret-text" translate="no">([A-Z2-7 ]+)</p>', page).group(1).replace(" ", "")
    resp = client.post("/setup", data={
        "csrf_token": csrf_from(page), "action": "verify", "code": pyotp.TOTP(secret).now(),
    })
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    codes = re.findall(r"<li><code>([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})</code></li>", html)
    assert len(codes) == 8
    return secret, codes


def password_step(client, username=OWNER, password=PASSWORD, headers=None):
    token = get_csrf(client, "/login")
    return client.post("/login", data={"csrf_token": token, "username": username, "password": password}, headers=headers or {})


def totp_step(client, code, headers=None):
    token = get_csrf(client, "/login/totp")
    return client.post("/login/totp", data={"csrf_token": token, "code": code}, headers=headers or {})


def login(client, secret, username=OWNER, password=PASSWORD):
    resp = password_step(client, username, password)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login/totp")
    resp = totp_step(client, pyotp.TOTP(secret).now())
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    return csrf_from(client.get("/").get_data(as_text=True))


@pytest.fixture
def owner(client):
    secret, codes = do_setup(client)
    return {"secret": secret, "codes": codes}


@pytest.fixture
def logged_in(client, owner):
    token = login(client, owner["secret"])
    return {"csrf": token, **owner}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload
