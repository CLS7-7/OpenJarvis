import sqlite3

import app as appmod


def seed(app, rows):
    conn = sqlite3.connect(app.config["DB_PATH"])
    for i, (status, created_at) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO tasks (ref, employee_id, command_text, source, status, progress, created_at, updated_at) "
            "VALUES (?, 1, 'seeded task', 'text', ?, 0, ?, ?)",
            (f"ABU-{i:05d}", status, created_at, created_at),
        )
    conn.commit()
    conn.close()


def test_state_stats_math(app, client, logged_in):
    empty = client.get("/api/state").get_json()["stats"]
    assert empty == {"total": 0, "today": 0, "done": 0, "working": 0, "failed": 0, "completion_rate": None}

    now = appmod.now()
    two_days_ago = now - 2 * 86400
    seed(app, [
        ("done", now), ("done", now), ("done", two_days_ago),
        ("failed", two_days_ago), ("working", now), ("queued", now), ("sent", two_days_ago),
    ])
    stats = client.get("/api/state").get_json()["stats"]
    assert stats == {
        "total": 7,
        "today": 4,
        "done": 3,
        "working": 1,
        "failed": 1,
        "completion_rate": 75,  # round(100 * 3 / (3 + 1))
    }

