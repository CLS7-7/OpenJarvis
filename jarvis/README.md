# abuai — private Jarvis command center

A single-owner command center served at `abuai.online`. It sits in the OpenJarvis
repository as a self-contained service. It does not import or change the
OpenJarvis package.

- **Owner-only auth:** one account, created once through `/setup`. Sign-in needs a password plus a TOTP code (or a single-use recovery code).
- **Dashboard:** a voice Talk button (ElevenLabs), a text command box, five employee cards with live status, and recent reports.
- **Command pipeline:** text and voice commands go through the same `POST /api/tasks`. Tasks are forwarded to the Hermes bridge on the owner's desktop, and a background poller tracks their real status.
- **Stack:** Flask, server-rendered templates and vanilla JS (no build step), SQLite in WAL mode, bcrypt and pyotp.

## Files

| Path | Purpose |
|---|---|
| `app.py` | The whole Flask app: auth, sessions, CSRF, pages, API, bridge client and poller |
| `qr.py` | Dependency-free QR encoder. Renders the TOTP enrollment QR as inline SVG, with no external QR service |
| `templates/` | Jinja templates (`base`, `setup`, `login`, `login_totp`, `dashboard`, `reports`, `report_detail`, `settings`, `404`, `500`, `error`, `_avatar`) |
| `static/` | `app.css`, `common.js`, `dashboard.js`, `voice.js`, `favicon.svg`, and the self-hosted ElevenLabs audio worklets (MIT) |
| `Dockerfile`, `docker-compose.yml` | Container image (python:3.12-slim, non-root, gunicorn) and the single `abuai-app` service |
| `tests/` | pytest suite |

## Environment variables

All variables are read on the server only. Put them in `jarvis/.env` on the VPS. That file is git-ignored and never baked into the image. `.env.example` lists the names with empty values.

| Name | Required | Meaning |
|---|---|---|
| `SECRET_KEY` | yes | Signs cookies and CSRF tokens. The app refuses to start if it is missing or shorter than 32 characters. Generate a long random value on the server. |
| `DATA_DIR` | no | Where the SQLite database lives. Defaults to `/data` (the named volume). |
| `BRIDGE_URL` | no | Base URL of the Hermes bridge, e.g. its tunnel or LAN address. |
| `BRIDGE_SECRET` | no | Bearer token for the bridge. Sent only in the server-side `Authorization` header. |
| `ELEVENLABS_API_KEY` | no | Used only server-side to mint short-lived conversation tokens. |
| `ELEVENLABS_AGENT_ID` | no | The ElevenLabs agent (with Authentication enabled). |

If the bridge variables are unset, tasks are still recorded with status `queued`, and the UI says *"Bridge not connected — tasks will send when Hermes is connected"*. If the voice variables are unset, the Talk button shows *"Voice not configured yet"* and `/api/voice/token` returns 503.

## Run

### Production (VPS, behind the existing Caddy)

```bash
cd jarvis
cp .env.example .env      # then fill in the values on the server
chmod 600 .env
docker compose up -d --build
```

The service joins the existing external Docker network `autoaccess_default` and only exposes port 8080 on that network; no host port is published. Point the host's Caddy at it:

```caddyfile
abuai.online {
    reverse_proxy abuai-app:8080
}
```

Notes on the proxy:

- **Cookies:** they are `Secure` and use the `__Host-` prefix, so the site must be served over HTTPS (Caddy does this).
- **Client IP:** it is taken from `CF-Connecting-IP` when present, otherwise from the TCP peer. If the domain is **not** behind Cloudflare, make Caddy strip that header so clients cannot spoof it (`reverse_proxy abuai-app:8080 { header_up -CF-Connecting-IP }`). Without Cloudflare, every request then appears to come from Caddy's address, so the per-IP lockout becomes shared. The TOTP step is also locked per account. If the domain **is** behind Cloudflare, restrict the origin to Cloudflare IPs so the header cannot be forged.

### Local development

```bash
cd jarvis
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" DATA_DIR=./.data
gunicorn -w 2 -b 127.0.0.1:8080 'app:create_app()'
```

Browsers accept `Secure` cookies on `http://localhost` and `http://127.0.0.1`.

### Tests

```bash
cd jarvis && python -m pytest
```

## First-time setup flow

Do this right after the first deploy. Until the owner account exists, `/setup` is open to whoever reaches it first.

1. Open `https://abuai.online/`. With no account yet, you are sent to `/setup`.
2. Choose a username (3–32 characters) and a password (at least 12 characters). The password is stored as a bcrypt hash with cost 12.
3. Scan the QR code with an authenticator app, or type the key shown under it. Enter the 6-digit code to confirm.
4. You are shown **8 recovery codes, once**. Only their bcrypt hashes are stored, so save them offline.
5. From then on `/setup` returns 404 for good. Sign in at `/login` (password), then `/login/totp` (code or a recovery code).

If you lose both the authenticator and the recovery codes, recover by stopping the container and deleting the database on the volume. This wipes everything, including task history. Then run setup again.

## Security model (summary)

- **Lockout:** 5 failed attempts lock that step for 15 minutes. The password step is counted per client IP. The TOTP step is counted per IP and per account. Counts live in the SQLite `rate_hits` table, so they hold across gunicorn workers. A TOTP code cannot be replayed within its time window.
- **Sessions:** stored server-side in SQLite; the cookie holds only a random token, and the table holds only its SHA-256. Cookies are HttpOnly, Secure and SameSite=Lax. Sessions expire after 12 hours idle (sliding). Login and logout clear all session state.
- **CSRF:** every POST (forms and AJAX) carries an HMAC token. Before login, the token is tied to a signed anonymous cookie; after login, it is tied to the session.
- **Headers:** a strict CSP is sent on every response, plus `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS, and a Permissions-Policy that allows only the microphone.
  - Beyond `'self'`, the CSP allows `https://cdn.jsdelivr.net` for scripts and `api.elevenlabs.io` for connections. It also allows `livekit.rtc.elevenlabs.io` for connections, because a conversation token starts a WebRTC (LiveKit) session on that host.
  - There are no inline scripts or styles. The SDK's audio worklets are self-hosted, so `blob:`/`data:` script sources are not needed.
- **Secrets:** the bridge secret and the ElevenLabs key never reach a template, JS, an API response or a log line. The browser only ever receives the short-lived conversation token.
- **Errors:** no debug mode. Users see branded 404/500 pages; stack traces stay in the server log.

## Hermes bridge contract

All requests carry `Authorization: Bearer <BRIDGE_SECRET>`.

| Call | Request | Expected response |
|---|---|---|
| `POST {BRIDGE_URL}/tasks` | `{"ref": "ABU-00001", "employee_key": "EMPLOYEE_2", "command": "..."}` | `{"bridge_task_id": "..."}` (1–128 chars of `[A-Za-z0-9._:-]`) |
| `GET {BRIDGE_URL}/tasks/{bridge_task_id}` | — | `{"status": "queued|working|done|failed", "progress": 0-100, "result": "..."}` |
| `GET {BRIDGE_URL}/health` | — | any 2xx JSON object. Used to show *Connected* when no tasks are in flight. |

- **Idempotency:** `ref` is unique per task. Hermes should use it as an idempotency key, because a forward that timed out is retried.
- **Poller:** it runs every 5 seconds with per-task exponential backoff. A lease in SQLite ensures only one gunicorn worker talks to the bridge at a time.
- **Status mapping:** a bridge status of `queued` shows as *Sent to Hermes*. Unknown statuses or malformed payloads are ignored rather than guessed. Progress is only what the bridge reports, except that a `done` with no progress value is shown as 100.
- **Bridge status:** *Connected* means the bridge answered successfully within the last 45 seconds.

## Dashboard API (login required, JSON)

| Endpoint | Returns |
|---|---|
| `GET /api/state` | Employees with live status, bridge state (incl. `last_ok` / `last_fail` contact times), voice flag, the 8 most recent tasks, a `stats` block, server time. Polled every 5 s by the dashboard. |
| `GET /api/tasks/<ref>` | One task with every stored field: command text, full result text, `created_at`, `sent_at`, `started_at`, `updated_at`, `finished_at`. 404 JSON if unknown. |
| `GET /api/employees/<id>` | The employee with current task, last 10 tasks, per-status counts and a preview of its latest result. 404 JSON if unknown. |
| `POST /api/tasks` | Creates a task (text or voice) and forwards it to Hermes. |
| `GET /api/voice/token` | Short-lived ElevenLabs conversation token. |

The `sent_at` and `started_at` columns are added automatically to existing databases on boot. They record when Hermes accepted a task and when it first reported `working`. Tasks created before this change show those steps as "time not recorded".

The dashboard has one poller (`dashboard.js`):
- It broadcasts each state as an `abuai:state` event.
- It diffs consecutive states into the live-feed events (task created, handed to Hermes, work started, completed, failed, Hermes connected or lost). The first load seeds silently.
- Open holo-windows (`windows.js`) refresh their own data on each `abuai:state` event.

## Voice flow

1. When you press **Talk**, the browser asks for microphone permission. A denial is shown as a plain message.
2. The browser calls `GET /api/voice/token`. This requires login.
3. The backend calls `GET https://api.elevenlabs.io/v1/convai/conversation/token?agent_id=…` with the `xi-api-key` header and returns only `{token}`.
4. The page loads `@elevenlabs/client@1.26.0` (`dist/lib.iife.js`) from jsDelivr, pinned with Subresource Integrity, and starts a session with `conversationToken`.
5. The user's transcript is collected from `onMessage` events whose role is `user`.
6. When the conversation ends, the transcript is posted to `/api/tasks` with `source: "voice"` for the employee selected in the picker. The page shows the result, e.g. *"Voice command sent to EMPLOYEE_2 — ABU-00007"*. If no command was captured, it shows *"No command captured from that conversation."*
