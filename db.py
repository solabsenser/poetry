"""Подключение к Turso через HTTP API (без нативных библиотек).

Интерфейс повторяет то, чем пользуется app.py:
get_conn().execute(sql, params).fetchall() / .fetchone(), commit(), close().
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

_session = requests.Session()  # переиспользует соединение, чтобы запросы были быстрее


def _pipeline_url():
    url = os.environ["TURSO_DATABASE_URL"].strip().rstrip("/")
    for prefix in ("libsql://", "wss://", "ws://"):
        if url.startswith(prefix):
            url = "https://" + url[len(prefix):]
    return url + "/v2/pipeline"


def _encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, (bool, int)):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(cell):
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")


class Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Connection:
    def execute(self, sql, params=()):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [_encode(p) for p in params]
        body = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
        headers = {"Authorization": "Bearer " + os.environ["TURSO_AUTH_TOKEN"].strip()}

        for attempt in (1, 2):
            try:
                resp = _session.post(_pipeline_url(), json=body, headers=headers, timeout=15)
                break
            except requests.ConnectionError:
                if attempt == 2:  # устаревшее соединение: пробуем ещё один раз
                    raise
        if resp.status_code != 200:
            raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:300]}")

        result = resp.json()["results"][0]
        if result.get("type") != "ok":
            message = (result.get("error") or {}).get("message", "unknown error")
            raise RuntimeError("Turso error: " + message)
        rows = result["response"]["result"]["rows"]
        return Cursor([tuple(_decode(c) for c in row) for row in rows])

    def commit(self):
        pass  # каждый запрос применяется сразу

    def close(self):
        pass


def get_conn():
    return Connection()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS poems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS reactions (
        user_id INTEGER NOT NULL REFERENCES users(id),
        poem_id INTEGER NOT NULL REFERENCES poems(id),
        kind TEXT NOT NULL,
        PRIMARY KEY (user_id, poem_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_poems_user ON poems(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reactions_poem ON reactions(poem_id)",
]


def init_db():
    conn = get_conn()
    for sql in SCHEMA:
        conn.execute(sql)


if __name__ == "__main__":
    init_db()
    print(get_conn().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
