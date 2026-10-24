"""abuai - private single-owner Jarvis command center.

Single-file Flask app: owner auth (password + TOTP), server-side sessions,
CSRF, employee dashboard, task pipeline to the Hermes bridge, and the
ElevenLabs conversation-token endpoint. Run with gunicorn: ``app:create_app()``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import uuid

import bcrypt
import pyotp
import requests
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from itsdangerous import BadSignature, Signer, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

import qr

log = logging.getLogger("abuai")

# --- constants ---------------------------------------------------------

SESSION_COOKIE = "__Host-abuai_session"
ANON_COOKIE = "__Host-abuai_anon"
PENDING_COOKIE = "__Host-abuai_2fa"

SESSION_IDLE_SECONDS = 12 * 60 * 60
PENDING_2FA_SECONDS = 5 * 60
SETUP_PENDING_SECONDS = 15 * 60
LOCK_MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
BCRYPT_ROUNDS = 12
RECOVERY_CODE_COUNT = 8
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COMMAND_MAX_CHARS = 4000
RESULT_MAX_CHARS = 200_000
NAME_MAX_CHARS = 40
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,32}")
BRIDGE_TASK_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
TASK_STATUSES = ("queued", "sent", "working", "done", "failed")
BRIDGE_STATUSES = ("queued", "working", "done", "failed")
BRIDGE_FRESH_SECONDS = 45
POLL_INTERVAL = 5.0
FORWARD_GRACE_SECONDS = 20
ELEVENLABS_TOKEN_URL = "https://api.elevenlabs.io/v1/convai/conversation/token"

STATUS_LABELS = {
    "queued": "Queued",
    "sent": "Sent to Hermes",
    "working": "Working",
    "done": "Done",
    "failed": "Failed",
}

EMPLOYEE_SEED = (
    ("EMPLOYEE_1", "#7C5CFF", "antenna"),
    ("EMPLOYEE_2", "#FF7A59", "headset"),
    ("EMPLOYEE_3", "#12B5A5", "glasses"),
    ("EMPLOYEE_4", "#FFB020", "cap"),
    ("EMPLOYEE_5", "#38BDF8", "headphones"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    totp_secret TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_hits (
    bucket TEXT NOT NULL,
    subject TEXT NOT NULL,
    hit_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits(bucket, subject, hit_at);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    avatar_style TEXT NOT NULL,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    ref TEXT NOT NULL UNIQUE,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    command_text TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('text', 'voice')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'working', 'done', 'failed')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    result_text TEXT,
    bridge_task_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_employee ON tasks(employee_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> int:
    return int(time.time())


# --- client IP (canonical hardened version; keep verbatim) -------------

def client_ip():
    cf = request.headers.get('CF-Connecting-IP', '')
    if cf and len(cf) <= 45 and re.fullmatch(r'[0-9a-fA-F.:]+', cf):
        try:
            ipaddress.ip_address(cf)
            return cf
        except ValueError:
            pass
    return request.remote_addr or 'unknown'


# --- database ------------------------------------------------------------

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for sort, (key, color, style) in enumerate(EMPLOYEE_SEED, start=1):
            conn.execute(
                "INSERT OR IGNORE INTO employees (key, name, color, avatar_style, sort) VALUES (?, ?, ?, ?, ?)",
                (key, key, color, style, sort),
            )
    finally:
        conn.close()


def db() -> sqlite3.Connection:
    if "db" not in g:
        from flask import current_app
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def delete_setting(conn, key) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# --- crypto helpers ------------------------------------------------------

def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


def check_password(password: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pw_hash.encode("ascii"))
    except ValueError:
        return False


def new_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def normalize_recovery_code(code: str) -> str:
    cleaned = re.sub(r"[\s-]", "", code or "").upper()
    if len(cleaned) != 12 or any(c not in RECOVERY_ALPHABET for c in cleaned):
        return ""
    return f"{cleaned[0:4]}-{cleaned[4:8]}-{cleaned[8:12]}"


def totp_matching_step(secret: str, code: str, at: int):
    """Return the 30s time-step the code matches (window +-1), else None."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    totp = pyotp.TOTP(secret)
    for offset in (0, -1, 1):
        t = at + offset * 30
        if hmac.compare_digest(totp.at(t), code):
            return t // totp.interval
    return None


# --- lockout (rate_hits; shared across gunicorn workers) ---------------

def lock_remaining(conn, bucket: str, subject: str) -> int:
    row = conn.execute(
        "SELECT MAX(hit_at) FROM rate_hits WHERE bucket = ? AND subject = ? AND hit_at > ?",
        (bucket + ":lock", subject, now() - LOCK_SECONDS),
    ).fetchone()
    if not row or row[0] is None:
        return 0
    return max(1, row[0] + LOCK_SECONDS - now())


def register_failure(conn, bucket: str, subject: str) -> None:
    t = now()
    conn.execute("DELETE FROM rate_hits WHERE hit_at < ?", (t - 86400,))
    conn.execute("INSERT INTO rate_hits (bucket, subject, hit_at) VALUES (?, ?, ?)", (bucket, subject, t))
    count = conn.execute(
        "SELECT COUNT(*) FROM rate_hits WHERE bucket = ? AND subject = ? AND hit_at > ?",
        (bucket, subject, t - LOCK_SECONDS),
    ).fetchone()[0]
    if count >= LOCK_MAX_FAILURES:
        conn.execute("INSERT INTO rate_hits (bucket, subject, hit_at) VALUES (?, ?, ?)", (bucket + ":lock", subject, t))
        conn.execute("DELETE FROM rate_hits WHERE bucket = ? AND subject = ?", (bucket, subject))


def clear_failures(conn, bucket: str, subject: str) -> None:
    conn.execute("DELETE FROM rate_hits WHERE bucket = ? AND subject = ?", (bucket, subject))


def count_hits(conn, bucket: str, subject: str, window: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rate_hits WHERE bucket = ? AND subject = ? AND hit_at > ?",
        (bucket, subject, now() - window),
    ).fetchone()[0]


def lock_message(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many failed attempts. Try again in {minutes} {unit}."


# --- formatting ----------------------------------------------------------

def iso(ts):
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_time(ts):
    if ts is None:
        return ""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_duration(start, end):
    if start is None or end is None:
        return ""
    secs = max(0, int(end - start))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def short_title(text: str, limit: int = 90) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def safe_color(color: str) -> str:
    return color if HEX_COLOR_RE.fullmatch(color or "") else "#22D3EE"


def clean_name(value: str) -> str:
    value = " ".join((value or "").split())
    if not value or len(value) > NAME_MAX_CHARS:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    return value


# --- Hermes bridge -------------------------------------------------------

class BridgeError(Exception):
    """A bridge call failed. Message is safe to log (never contains secrets)."""


class Bridge:
    def __init__(self, url: str, secret: str):
        url = (url or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(url) if url else None
        valid_url = bool(parsed and parsed.scheme in ("http", "https") and parsed.netloc)
        self.url = url if valid_url else ""
        self._secret = secret or ""
        self.configured = bool(self.url and self._secret)

    def _headers(self):
        return {"Authorization": f"Bearer {self._secret}", "Accept": "application/json"}

    def _request(self, method, path, **kwargs):
        if not self.configured:
            raise BridgeError("bridge not configured")
        try:
            resp = requests.request(
                method, self.url + path, headers=self._headers(),
                timeout=(4, 10), allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise BridgeError(f"bridge unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 300:
            raise BridgeError(f"bridge returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise BridgeError("bridge returned invalid JSON") from None
        if not isinstance(data, dict):
            raise BridgeError("bridge returned unexpected JSON")
        return data

    def submit(self, ref: str, employee_key: str, command: str) -> str:
        data = self._request("POST", "/tasks", json={"ref": ref, "employee_key": employee_key, "command": command})
        task_id = data.get("bridge_task_id")
        if isinstance(task_id, int) and not isinstance(task_id, bool):
            task_id = str(task_id)
        if not isinstance(task_id, str) or not BRIDGE_TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("bridge response missing a valid bridge_task_id")
        return task_id

    def fetch(self, bridge_task_id: str) -> dict:
        return self._request("GET", "/tasks/" + urllib.parse.quote(bridge_task_id, safe=""))

    def health(self) -> None:
        self._request("GET", "/health")


def bridge_from_config(config) -> Bridge:
    return Bridge(config.get("BRIDGE_URL", ""), config.get("BRIDGE_SECRET", ""))


def mark_bridge(conn, ok: bool, error: str = "") -> None:
    if ok:
        set_setting(conn, "bridge_last_ok", now())
    else:
        set_setting(conn, "bridge_last_fail", now())
        if error:
            log.warning("Hermes bridge: %s", error)


def bridge_state(conn, bridge: Bridge) -> dict:
    if not bridge.configured:
        return {"configured": False, "connected": False, "label": "Not configured"}
    last_ok = int(get_setting(conn, "bridge_last_ok", 0) or 0)
    last_fail = int(get_setting(conn, "bridge_last_fail", 0) or 0)
    connected = last_ok >= now() - BRIDGE_FRESH_SECONDS and last_ok >= last_fail
    return {
        "configured": True,
        "connected": connected,
        "label": "Connected" if connected else "Configured — not reachable",
    }


def forward_task(conn, bridge: Bridge, task_id: int) -> bool:
    """Send one queued task to the bridge. Returns True when the bridge accepted it."""
    row = conn.execute(
        "SELECT t.id, t.ref, t.command_text, t.status, t.bridge_task_id, e.key AS employee_key "
        "FROM tasks t JOIN employees e ON e.id = t.employee_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["status"] != "queued" or row["bridge_task_id"]:
        return False
    try:
        bridge_task_id = bridge.submit(row["ref"], row["employee_key"], row["command_text"])
    except BridgeError as exc:
        mark_bridge(conn, False, f"forward of {row['ref']} failed: {exc}")
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ? AND status = 'queued'", (now(), task_id))
        return False
    mark_bridge(conn, True)
    conn.execute(
        "UPDATE tasks SET status = 'sent', bridge_task_id = ?, updated_at = ? "
        "WHERE id = ? AND status = 'queued' AND bridge_task_id IS NULL",
        (bridge_task_id, now(), task_id),
    )
    return True


def apply_bridge_update(conn, task_id: int, data: dict) -> None:
    """Update a task row from a bridge status payload. Never invents values."""
    status = data.get("status")
    if status not in BRIDGE_STATUSES:
        raise BridgeError("bridge returned an unknown task status")
    row = conn.execute("SELECT status, progress, result_text FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row or row["status"] in ("done", "failed"):
        return
    progress = row["progress"]
    reported = data.get("progress")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        progress = max(0, min(100, int(reported)))
    result = data.get("result")
    result_text = row["result_text"]
    if isinstance(result, str):
        result_text = result[:RESULT_MAX_CHARS]
    elif result is not None:
        result_text = json.dumps(result, ensure_ascii=False, indent=2)[:RESULT_MAX_CHARS]

    new_status = {"queued": "sent", "working": "working", "done": "done", "failed": "failed"}[status]
    finished_at = now() if new_status in ("done", "failed") else None
    if new_status == "done" and not (isinstance(reported, (int, float)) and not isinstance(reported, bool)):
        progress = 100  # the bridge reported completion without a progress value
    conn.execute(
        "UPDATE tasks SET status = ?, progress = ?, result_text = ?, updated_at = ?, finished_at = ? "
        "WHERE id = ? AND status NOT IN ('done', 'failed')",
        (new_status, progress, result_text, now(), finished_at, task_id),
    )


class Poller:
    """Background loop: forwards queued tasks and polls active ones.

    Every gunicorn worker runs one, but a lease in the settings table means
    only one of them talks to the bridge at a time.
    """

    def __init__(self, db_path: str, bridge: Bridge):
        self.db_path = db_path
        self.bridge = bridge
        self.worker_id = uuid.uuid4().hex
        self.backoff: dict[int, tuple[int, float]] = {}
        self.last_health = 0.0

    def _acquire_lease(self, conn) -> bool:
        t = now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = get_setting(conn, "poller_lease", "")
            owner, _, expires = current.partition("|")
            if not current or owner == self.worker_id or int(expires or 0) < t:
                set_setting(conn, "poller_lease", f"{self.worker_id}|{t + int(POLL_INTERVAL * 4)}")
                conn.execute("COMMIT")
                return True
            conn.execute("COMMIT")
            return False
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            return False

    def _due(self, task_id: int) -> bool:
        entry = self.backoff.get(task_id)
        return entry is None or entry[1] <= time.monotonic()

    def _fail(self, task_id: int) -> None:
        failures = self.backoff.get(task_id, (0, 0))[0] + 1
        delay = min(300, POLL_INTERVAL * (2 ** min(failures, 6)))
        self.backoff[task_id] = (failures, time.monotonic() + delay)

    def run_once(self, conn) -> None:
        if not self.bridge.configured or not self._acquire_lease(conn):
            return
        contacted = False
        stale = now() - FORWARD_GRACE_SECONDS
        queued = conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' AND bridge_task_id IS NULL AND updated_at <= ? ORDER BY id LIMIT 20",
            (stale,),
        ).fetchall()
        for row in queued:
            if not self._due(row["id"]):
                continue
            contacted = True
            if forward_task(conn, self.bridge, row["id"]):
                self.backoff.pop(row["id"], None)
            else:
                self._fail(row["id"])
        active = conn.execute(
            "SELECT id, bridge_task_id FROM tasks WHERE status IN ('sent', 'working') AND bridge_task_id IS NOT NULL ORDER BY id LIMIT 50"
        ).fetchall()
        for row in active:
            if not self._due(row["id"]):
                continue
            contacted = True
            try:
                data = self.bridge.fetch(row["bridge_task_id"])
                apply_bridge_update(conn, row["id"], data)
                mark_bridge(conn, True)
                self.backoff.pop(row["id"], None)
            except BridgeError as exc:
                mark_bridge(conn, False, f"status poll failed: {exc}")
                self._fail(row["id"])
        if not contacted and time.monotonic() - self.last_health >= 15:
            self.last_health = time.monotonic()
            try:
                self.bridge.health()
                mark_bridge(conn, True)
            except BridgeError as exc:
                mark_bridge(conn, False, f"health check failed: {exc}")
        conn.execute("DELETE FROM rate_hits WHERE hit_at < ?", (now() - 86400,))

    def loop(self) -> None:
        conn = connect(self.db_path)
        while True:
            try:
                self.run_once(conn)
            except Exception:  # keep the thread alive; details stay server-side
                log.exception("bridge poller iteration failed")
            time.sleep(POLL_INTERVAL)

    def start(self) -> None:
        threading.Thread(target=self.loop, name="hermes-poller", daemon=True).start()


# --- view-model helpers ------------------------------------------------

def task_dict(row) -> dict:
    return {
        "id": row["id"],
        "ref": row["ref"],
        "employee_id": row["employee_id"],
        "employee_name": row["employee_name"],
        "title": short_title(row["command_text"]),
        "source": row["source"],
        "status": row["status"],
        "status_label": STATUS_LABELS[row["status"]],
        "progress": row["progress"],
        "created_at": iso(row["created_at"]),
        "finished_at": iso(row["finished_at"]),
        "created_human": human_time(row["created_at"]),
        "finished_human": human_time(row["finished_at"]),
        "duration": human_duration(row["created_at"], row["finished_at"]),
        "url": url_for("report_detail", ref=row["ref"]),
    }


TASK_SELECT = (
    "SELECT t.*, e.name AS employee_name FROM tasks t JOIN employees e ON e.id = t.employee_id "
)


def employees_with_status(conn) -> list[dict]:
    result = []
    for emp in conn.execute("SELECT * FROM employees ORDER BY sort, id").fetchall():
        active = conn.execute(
            TASK_SELECT + "WHERE t.employee_id = ? AND t.status IN ('working', 'sent', 'queued') "
            "ORDER BY CASE t.status WHEN 'working' THEN 0 ELSE 1 END, t.id DESC LIMIT 1",
            (emp["id"],),
        ).fetchone()
        latest = active or conn.execute(
            TASK_SELECT + "WHERE t.employee_id = ? ORDER BY t.id DESC LIMIT 1", (emp["id"],)
        ).fetchone()
        if latest is None:
            status = "idle"
        elif latest["status"] == "working":
            status = "working"
        elif latest["status"] == "done" and active is None:
            status = "done"
        else:
            status = "idle"
        current = task_dict(latest) if latest else None
        result.append({
            "id": emp["id"],
            "key": emp["key"],
            "name": emp["name"],
            "color": safe_color(emp["color"]),
            "avatar_style": emp["avatar_style"],
            "status": status,
            "status_label": {"idle": "Idle", "working": "Working", "done": "Done"}[status],
            "current_task": current,
            "progress": current["progress"] if current else 0,
        })
    return result


def recent_tasks(conn, limit: int) -> list[dict]:
    rows = conn.execute(TASK_SELECT + "ORDER BY t.id DESC LIMIT ?", (limit,)).fetchall()
    return [task_dict(r) for r in rows]


# --- app factory ---------------------------------------------------------

PUBLIC_ENDPOINTS = {"login", "login_totp", "setup", "static"}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")

    secret_key = (test_config or {}).get("SECRET_KEY", os.environ.get("SECRET_KEY", ""))
    if not secret_key or len(secret_key) < 32:
        raise RuntimeError("SECRET_KEY is missing or shorter than 32 characters; refusing to start.")

    data_dir = os.environ.get("DATA_DIR", "/data")
    app.config.update(
        SECRET_KEY=secret_key,
        DATA_DIR=data_dir,
        BRIDGE_URL=os.environ.get("BRIDGE_URL", ""),
        BRIDGE_SECRET=os.environ.get("BRIDGE_SECRET", ""),
        ELEVENLABS_API_KEY=os.environ.get("ELEVENLABS_API_KEY", ""),
        ELEVENLABS_AGENT_ID=os.environ.get("ELEVENLABS_AGENT_ID", ""),
        BCRYPT_ROUNDS=BCRYPT_ROUNDS,
        START_POLLER=True,
        MAX_CONTENT_LENGTH=64 * 1024,
        TEMPLATES_AUTO_RELOAD=False,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("TESTING"):
        app.config["BCRYPT_ROUNDS"] = max(BCRYPT_ROUNDS, app.config["BCRYPT_ROUNDS"])
    app.config.setdefault("DB_PATH", os.path.join(app.config["DATA_DIR"], "abuai.sqlite3"))
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db(app.config["DB_PATH"])

    anon_signer = Signer(secret_key, salt="abuai-anon-v1")
    pending_serializer = URLSafeTimedSerializer(secret_key, salt="abuai-2fa-pending-v1")
    dummy_hash = hash_password(secrets.token_urlsafe(16), app.config["BCRYPT_ROUNDS"])

    def bridge() -> Bridge:
        return bridge_from_config(app.config)

    def voice_configured() -> bool:
        return bool(app.config["ELEVENLABS_API_KEY"] and app.config["ELEVENLABS_AGENT_ID"])

    def csrf_for(kind: str, value: str) -> str:
        return hmac.new(secret_key.encode(), f"csrf|{kind}|{value}".encode(), hashlib.sha256).hexdigest()

    def set_cookie(resp, name, value, max_age=None):
        resp.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="Lax")

    def clear_cookie(resp, name):
        resp.delete_cookie(name, path="/", secure=True, httponly=True, samesite="Lax")

    def rotate_anon():
        g.anon_id = secrets.token_urlsafe(32)
        g.anon_dirty = True

    def start_session(user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        t = now()
        db().execute(
            "INSERT INTO sessions (id, user_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
            (sha256_hex(token), user_id, t, t),
        )
        db().execute("DELETE FROM sessions WHERE last_seen < ?", (t - SESSION_IDLE_SECONDS,))
        return token

    def end_current_session():
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            db().execute("DELETE FROM sessions WHERE id = ?", (sha256_hex(token),))
        g.user = None
        g.session_id = None

    def is_api() -> bool:
        return request.path.startswith("/api/")

    # --- request lifecycle ---------------------------------------------

    @app.before_request
    def load_context():
        g.anon_dirty = False
        g.session_token = None
        g.session_id = None
        g.user = None
        raw_anon = request.cookies.get(ANON_COOKIE, "")
        try:
            g.anon_id = anon_signer.unsign(raw_anon).decode() if raw_anon else ""
        except BadSignature:
            g.anon_id = ""
        if not g.anon_id:
            rotate_anon()

        token = request.cookies.get(SESSION_COOKIE)
        if token and len(token) <= 128:
            sid = sha256_hex(token)
            row = db().execute(
                "SELECT s.id, s.last_seen, u.id AS uid, u.username FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.id = ?",
                (sid,),
            ).fetchone()
            if row and row["last_seen"] >= now() - SESSION_IDLE_SECONDS:
                g.user = {"id": row["uid"], "username": row["username"]}
                g.session_id = sid
                g.session_token = token
                if now() - row["last_seen"] >= 60:
                    db().execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now(), sid))
            elif row:
                db().execute("DELETE FROM sessions WHERE id = ?", (sid,))

        endpoint = request.endpoint
        if endpoint == "setup" and user_count(db()) > 0:
            abort(404)
        if g.user is None and endpoint not in PUBLIC_ENDPOINTS:
            if is_api():
                return jsonify(error="Login required."), 401
            return redirect(url_for("login"))

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not sent or not hmac.compare_digest(sent, current_csrf()):
                if is_api():
                    return jsonify(error="Security token missing or expired. Reload the page."), 400
                return render_error(400, "Form expired", "Your form expired for security reasons. Go back, reload the page and try again.")
        return None

    def current_csrf() -> str:
        if g.get("session_id"):
            return csrf_for("sess", g.session_id)
        return csrf_for("anon", g.get("anon_id", ""))

    @app.context_processor
    def inject_globals():
        return {
            "csrf_token": current_csrf(),
            "current_user": g.get("user"),
            "current_year": _dt.datetime.now(_dt.timezone.utc).year,
        }

    @app.after_request
    def finalize(resp):
        if g.get("anon_dirty"):
            set_cookie(resp, ANON_COOKIE, anon_signer.sign(g.anon_id.encode()).decode())
        if g.get("session_token") and g.get("user"):
            set_cookie(resp, SESSION_COOKIE, g.session_token, max_age=SESSION_IDLE_SECONDS)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' https://api.elevenlabs.io wss://api.elevenlabs.io "
            "https://livekit.rtc.elevenlabs.io wss://livekit.rtc.elevenlabs.io; "
            "img-src 'self' data:; style-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        resp.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    # --- errors ----------------------------------------------------------

    def render_error(code, title, message):
        if is_api():
            return jsonify(error=message), code
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(404)
    def not_found(_e):
        if is_api():
            return jsonify(error="Not found."), 404
        return render_template("404.html"), 404

    @app.errorhandler(HTTPException)
    def http_error(e):
        if e.code == 404:
            return not_found(e)
        if e.code >= 500:
            return server_error(e)
        titles = {400: "Bad request", 405: "Not allowed", 413: "Too large", 429: "Slow down"}
        return render_error(e.code, titles.get(e.code, "Request error"), "That request could not be completed.")

    @app.errorhandler(Exception)
    def server_error(e):
        if not isinstance(e, HTTPException):
            log.exception("unhandled error on %s", request.path)
        if is_api():
            return jsonify(error="Something went wrong on the server."), 500
        return render_template("500.html"), 500

    # --- setup -----------------------------------------------------------

    def setup_pending():
        raw = get_setting(db(), "setup_pending")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if data.get("anon") != sha256_hex(g.anon_id) or data.get("created", 0) < now() - SETUP_PENDING_SECONDS:
            return None
        return data

    def render_setup_enroll(pending, error=None, status=200):
        uri = pyotp.TOTP(pending["totp_secret"]).provisioning_uri(name=pending["username"], issuer_name="abuai")
        secret = pending["totp_secret"]
        return render_template(
            "setup.html", step="enroll", error=error, qr_svg=qr.svg(uri),
            secret_groups=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            username=pending["username"],
        ), status

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        conn = db()
        if user_count(conn) > 0:
            abort(404)
        ip = client_ip()
        if request.method == "GET":
            pending = setup_pending()
            if pending:
                return render_setup_enroll(pending)
            return render_template("setup.html", step="account", error=None, username="")

        action = request.form.get("action", "")
        if action == "restart":
            if setup_pending():
                delete_setting(conn, "setup_pending")
            return redirect(url_for("setup"))

        if action == "account":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("password_confirm") or ""
            error = None
            if not USERNAME_RE.fullmatch(username):
                error = "Username must be 3-32 characters: letters, numbers, dot, dash or underscore."
            elif len(password) < 12:
                error = "Password must be at least 12 characters."
            elif len(password.encode("utf-8")) > 72:
                error = "Password must be at most 72 bytes."
            elif not hmac.compare_digest(password.encode(), confirm.encode()):
                error = "The two passwords do not match."
            if error:
                return render_template("setup.html", step="account", error=error, username=username), 400
            pending = {
                "anon": sha256_hex(g.anon_id),
                "username": username,
                "pw_hash": hash_password(password, app.config["BCRYPT_ROUNDS"]),
                "totp_secret": pyotp.random_base32(),
                "created": now(),
            }
            set_setting(conn, "setup_pending", json.dumps(pending))
            return redirect(url_for("setup"))

        if action == "verify":
            pending = setup_pending()
            if not pending:
                return redirect(url_for("setup"))
            remaining = lock_remaining(conn, "setup_totp", ip)
            if remaining:
                return render_setup_enroll(pending, lock_message(remaining), 429)
            code = (request.form.get("code") or "").strip().replace(" ", "")
            if totp_matching_step(pending["totp_secret"], code, now()) is None:
                register_failure(conn, "setup_totp", ip)
                return render_setup_enroll(pending, "That code did not match. Check your authenticator app's clock and try again.", 400)
            codes = [new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
            code_hashes = [hash_password(c, app.config["BCRYPT_ROUNDS"]) for c in codes]
            conn.execute("BEGIN IMMEDIATE")
            try:
                if user_count(conn) > 0:
                    conn.execute("ROLLBACK")
                    abort(404)
                cur = conn.execute(
                    "INSERT INTO users (username, pw_hash, totp_secret, created_at) VALUES (?, ?, ?, ?)",
                    (pending["username"], pending["pw_hash"], pending["totp_secret"], now()),
                )
                for h in code_hashes:
                    conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)", (cur.lastrowid, h))
                delete_setting(conn, "setup_pending")
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            clear_failures(conn, "setup_totp", ip)
            rotate_anon()
            return render_template("setup.html", step="done", recovery_codes=codes, username=pending["username"])

        abort(400)

    # --- login -----------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        conn = db()
        if user_count(conn) == 0:
            return redirect(url_for("setup"))
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "GET":
            return render_template("login.html", error=None, username="")

        ip = client_ip()
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        remaining = lock_remaining(conn, "login_pw", ip)
        if remaining:
            return render_template("login.html", error=lock_message(remaining), username=username), 429
        user = conn.execute("SELECT id, pw_hash FROM users WHERE username = ?", (username,)).fetchone()
        ok = check_password(password, user["pw_hash"] if user else dummy_hash) and user is not None
        if not ok:
            register_failure(conn, "login_pw", ip)
            remaining = lock_remaining(conn, "login_pw", ip)
            msg = lock_message(remaining) if remaining else "Incorrect username or password."
            return render_template("login.html", error=msg, username=username), 429 if remaining else 401
        clear_failures(conn, "login_pw", ip)
        end_current_session()
        rotate_anon()
        token = pending_serializer.dumps({"uid": user["id"], "anon": sha256_hex(g.anon_id), "n": secrets.token_hex(8)})
        resp = redirect(url_for("login_totp"))
        set_cookie(resp, PENDING_COOKIE, token, max_age=PENDING_2FA_SECONDS)
        clear_cookie(resp, SESSION_COOKIE)
        return resp

    def pending_user_id():
        raw = request.cookies.get(PENDING_COOKIE, "")
        if not raw:
            return None
        try:
            data = pending_serializer.loads(raw, max_age=PENDING_2FA_SECONDS)
        except BadSignature:
            return None
        if not isinstance(data, dict) or data.get("anon") != sha256_hex(g.anon_id):
            return None
        return data.get("uid")

    @app.route("/login/totp", methods=["GET", "POST"])
    def login_totp():
        conn = db()
        if g.user:
            return redirect(url_for("dashboard"))
        uid = pending_user_id()
        user = conn.execute("SELECT id, totp_secret FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
        if user is None:
            resp = redirect(url_for("login"))
            clear_cookie(resp, PENDING_COOKIE)
            return resp
        if request.method == "GET":
            return render_template("login_totp.html", error=None)

        ip = client_ip()
        subjects = (ip, f"user:{user['id']}")
        remaining = max(lock_remaining(conn, "login_totp", s) for s in subjects)
        if remaining:
            return render_template("login_totp.html", error=lock_message(remaining)), 429

        code = (request.form.get("code") or "").strip()
        ok = False
        compact = code.replace(" ", "")
        if re.fullmatch(r"\d{6}", compact):
            step = totp_matching_step(user["totp_secret"], compact, now())
            last_key = f"totp_last_step:{user['id']}"
            if step is not None and step > int(get_setting(conn, last_key, -1)):
                set_setting(conn, last_key, step)
                ok = True
        else:
            normalized = normalize_recovery_code(code)
            if normalized:
                for rc in conn.execute(
                    "SELECT id, code_hash FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user["id"],)
                ).fetchall():
                    if check_password(normalized, rc["code_hash"]):
                        cur = conn.execute(
                            "UPDATE recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL", (now(), rc["id"])
                        )
                        ok = cur.rowcount == 1
                        break
        if not ok:
            for s in subjects:
                register_failure(conn, "login_totp", s)
            remaining = max(lock_remaining(conn, "login_totp", s) for s in subjects)
            msg = lock_message(remaining) if remaining else "That code was not accepted."
            return render_template("login_totp.html", error=msg), 429 if remaining else 401

        for s in subjects:
            clear_failures(conn, "login_totp", s)
        end_current_session()
        rotate_anon()
        token = start_session(user["id"])
        resp = redirect(url_for("dashboard"))
        set_cookie(resp, SESSION_COOKIE, token, max_age=SESSION_IDLE_SECONDS)
        clear_cookie(resp, PENDING_COOKIE)
        return resp

    @app.post("/logout")
    def logout():
        end_current_session()
        rotate_anon()
        resp = redirect(url_for("login"))
        clear_cookie(resp, SESSION_COOKIE)
        clear_cookie(resp, PENDING_COOKIE)
        return resp

    # --- pages -----------------------------------------------------------

    def state_payload() -> dict:
        conn = db()
        return {
            "employees": employees_with_status(conn),
            "bridge": bridge_state(conn, bridge()),
            "voice": {"configured": voice_configured()},
            "recent_tasks": recent_tasks(conn, 8),
            "server_time": iso(now()),
        }

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", state=state_payload())

    @app.get("/reports")
    def reports():
        conn = db()
        per_page = 50
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        rows = conn.execute(
            TASK_SELECT + "ORDER BY t.id DESC LIMIT ? OFFSET ?", (per_page, (page - 1) * per_page)
        ).fetchall()
        return render_template("reports.html", tasks=[task_dict(r) for r in rows], page=page, pages=pages, total=total)

    @app.get("/reports/<ref>")
    def report_detail(ref):
        row = db().execute(TASK_SELECT + "WHERE t.ref = ?", (ref,)).fetchone()
        if row is None:
            abort(404)
        task = task_dict(row)
        task["command_text"] = row["command_text"]
        task["result_text"] = row["result_text"]
        task["updated_human"] = human_time(row["updated_at"])
        return render_template("report_detail.html", task=task)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        conn = db()
        employees = conn.execute("SELECT * FROM employees ORDER BY sort, id").fetchall()
        errors = {}
        saved = request.args.get("saved") == "1"
        if request.method == "POST":
            updates = {}
            for emp in employees:
                value = clean_name(request.form.get(f"name_{emp['id']}", ""))
                if not value:
                    errors[emp["id"]] = f"Enter a name of 1-{NAME_MAX_CHARS} characters."
                updates[emp["id"]] = value
            if not errors:
                for emp_id, name in updates.items():
                    conn.execute("UPDATE employees SET name = ? WHERE id = ?", (name, emp_id))
                return redirect(url_for("settings", saved=1))
            saved = False
        return render_template(
            "settings.html",
            employees=employees,
            form=request.form if request.method == "POST" else None,
            errors=errors,
            saved=saved,
            bridge=bridge_state(conn, bridge()),
            voice_configured=voice_configured(),
        ), 400 if errors else 200

    # --- API -------------------------------------------------------------

    @app.get("/api/state")
    def api_state():
        return jsonify(state_payload())

    @app.post("/api/tasks")
    def api_create_task():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Expected a JSON body."), 400
        command = payload.get("command_text")
        command = command.strip() if isinstance(command, str) else ""
        source = payload.get("source", "text")
        if source not in ("text", "voice"):
            return jsonify(error="Unknown command source."), 400
        if not command:
            return jsonify(error="Type a command first."), 400
        if len(command) > COMMAND_MAX_CHARS:
            return jsonify(error=f"Commands are limited to {COMMAND_MAX_CHARS} characters."), 400
        try:
            employee_id = int(payload.get("employee_id"))
        except (TypeError, ValueError):
            return jsonify(error="Pick an employee."), 400
        conn = db()
        if conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone() is None:
            return jsonify(error="Pick an employee."), 400

        t = now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO tasks (ref, employee_id, command_text, source, status, progress, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)",
                ("pending-" + uuid.uuid4().hex, employee_id, command, source, t, t),
            )
            task_id = cur.lastrowid
            conn.execute("UPDATE tasks SET ref = ? WHERE id = ?", (f"ABU-{task_id:05d}", task_id))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        b = bridge()
        if b.configured:
            forward_task(conn, b, task_id)
        row = conn.execute(TASK_SELECT + "WHERE t.id = ?", (task_id,)).fetchone()
        return jsonify(task=task_dict(row), bridge=bridge_state(conn, b)), 201

    @app.get("/api/voice/token")
    def api_voice_token():
        if not voice_configured():
            return jsonify(error="Voice not configured yet."), 503
        conn = db()
        subject = f"user:{g.user['id']}"
        if count_hits(conn, "voice_token", subject, 600) >= 30:
            return jsonify(error="Too many voice sessions started. Wait a few minutes."), 429
        conn.execute("INSERT INTO rate_hits (bucket, subject, hit_at) VALUES (?, ?, ?)", ("voice_token", subject, now()))
        try:
            resp = requests.get(
                ELEVENLABS_TOKEN_URL,
                params={"agent_id": app.config["ELEVENLABS_AGENT_ID"]},
                headers={"xi-api-key": app.config["ELEVENLABS_API_KEY"], "Accept": "application/json"},
                timeout=(4, 10),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log.warning("ElevenLabs token request failed (%s)", type(exc).__name__)
            return jsonify(error="Voice service is unreachable right now."), 502
        if resp.status_code != 200:
            log.warning("ElevenLabs token request returned HTTP %s", resp.status_code)
            return jsonify(error="Voice service rejected the token request."), 502
        try:
            token = resp.json().get("token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token or len(token) > 8192:
            return jsonify(error="Voice service returned an unexpected response."), 502
        return jsonify(token=token)

    if app.config.get("START_POLLER") and not app.config.get("TESTING"):
        Poller(app.config["DB_PATH"], bridge()).start()

    return app
