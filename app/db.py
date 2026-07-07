"""SQLite storage: device list + latest/history snapshots."""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "redfish_manager.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    profile TEXT          -- JSON capability profile from discovery
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    data TEXT,            -- JSON snapshot, NULL on failure
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_device_ts ON snapshots(device_id, ts DESC);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    kind TEXT NOT NULL,       -- backup | update | restore | clearlog
    status TEXT NOT NULL,     -- running | success | failed
    message TEXT,             -- legacy pre-rendered text (older rows only)
    msg_code TEXT,            -- i18n message key, rendered on the frontend
    msg_params TEXT,          -- JSON array of positional params for msg_code
    task_id TEXT,             -- BMC task id for firmware updates
    started REAL NOT NULL,
    updated REAL NOT NULL
);
"""

_MIGRATIONS = [
    ("devices", "profile", "TEXT"),
    ("activities", "msg_code", "TEXT"),
    ("activities", "msg_params", "TEXT"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, coltype in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def ensure_device(conn, name: str, host: str, username: str, password: str) -> int:
    row = conn.execute("SELECT id FROM devices WHERE host=?", (host,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO devices(name, host, username, password) VALUES(?,?,?,?)",
        (name, host, username, password),
    )
    conn.commit()
    return cur.lastrowid


def list_devices(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, host FROM devices")]


def get_device(conn, device_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    return dict(r) if r else None


def save_profile(conn, device_id: int, profile: dict):
    conn.execute("UPDATE devices SET profile=? WHERE id=?",
                 (json.dumps(profile), device_id))
    conn.commit()


def get_profile(conn, device_id: int) -> dict | None:
    r = conn.execute("SELECT profile FROM devices WHERE id=?", (device_id,)).fetchone()
    return json.loads(r["profile"]) if r and r["profile"] else None


def save_snapshot(conn, device_id: int, data: dict | None, error: str | None):
    conn.execute(
        "INSERT INTO snapshots(device_id, ts, ok, data, error) VALUES(?,?,?,?,?)",
        (device_id, time.time(), 1 if data else 0,
         json.dumps(data) if data else None, error),
    )
    # keep last 2880 snapshots per device (~1 day at 30s interval)
    conn.execute(
        """DELETE FROM snapshots WHERE device_id=? AND id NOT IN
           (SELECT id FROM snapshots WHERE device_id=? ORDER BY ts DESC LIMIT 2880)""",
        (device_id, device_id),
    )
    conn.commit()


def add_activity(conn, device_id: int, kind: str, msg_code: str,
                 msg_params: list | None = None,
                 status: str = "running", task_id: str | None = None) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO activities(device_id, kind, status, msg_code, msg_params, task_id, started, updated)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (device_id, kind, status, msg_code, json.dumps(msg_params or []), task_id, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_activity(conn, activity_id: int, status: str,
                    msg_code: str | None = None, msg_params: list | None = None,
                    task_id: str | None = None):
    sets, vals = ["status=?", "updated=?"], [status, time.time()]
    if msg_code is not None:
        sets.append("msg_code=?"); vals.append(msg_code)
        sets.append("msg_params=?"); vals.append(json.dumps(msg_params or []))
    if task_id is not None:
        sets.append("task_id=?"); vals.append(task_id)
    vals.append(activity_id)
    conn.execute(f"UPDATE activities SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def find_running_update_by_task(conn, device_id: int, task_id: str) -> int | None:
    r = conn.execute(
        "SELECT id FROM activities WHERE device_id=? AND task_id=? AND status='running'"
        " ORDER BY started DESC LIMIT 1",
        (device_id, task_id),
    ).fetchone()
    return r["id"] if r else None


def list_activities(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT a.*, d.name AS device_name, d.host AS device_host
           FROM activities a JOIN devices d ON d.id = a.device_id
           ORDER BY a.started DESC LIMIT ?""",
        (limit,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["msg_params"] = json.loads(d["msg_params"]) if d.get("msg_params") else []
        out.append(d)
    return out


def latest_snapshot(conn, device_id: int) -> dict | None:
    r = conn.execute(
        "SELECT ts, ok, data, error FROM snapshots WHERE device_id=? ORDER BY ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if not r:
        return None
    return {
        "ts": r["ts"],
        "ok": bool(r["ok"]),
        "data": json.loads(r["data"]) if r["data"] else None,
        "error": r["error"],
    }
