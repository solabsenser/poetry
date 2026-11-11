"""Подключение к Turso через HTTP API (без нативных библиотек).

get_conn().execute(sql, params)  -> Cursor (fetchall / fetchone)
get_conn().run_many([...])       -> несколько запросов за ОДИН HTTP-запрос
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

_session = requests.Session()  # переиспользует соединение


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


def _stmt(item):
    sql, params = (item, ()) if isinstance(item, str) else item
    stmt = {"sql": sql}
    if params:
        stmt["args"] = [_encode(p) for p in params]
    return {"type": "execute", "stmt": stmt}


class Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Connection:
    def run_many(self, statements):
        """Выполняет список запросов одним HTTP-запросом, по порядку.
        Каждый элемент: "SQL" или ("SQL", (параметры,)). Возвращает список Cursor.
        Если один из запросов упал, бросает RuntimeError."""
        requests_list = [_stmt(s) for s in statements]
        body = {"requests": requests_list + [{"type": "close"}]}
        headers = {"Authorization": "Bearer " + os.environ["TURSO_AUTH_TOKEN"].strip()}

        for attempt in (1, 2):
            try:
                resp = _session.post(_pipeline_url(), json=body, headers=headers, timeout=15)
                break
            except requests.ConnectionError:
                if attempt == 2:  # устаревшее соединение: пробуем ещё раз
                    raise
        if resp.status_code != 200:
            raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:300]}")

        cursors = []
        for result in resp.json()["results"][: len(requests_list)]:
            if result.get("type") != "ok":
                message = (result.get("error") or {}).get("message", "unknown error")
                raise RuntimeError("Turso error: " + message)
            rows = result["response"]["result"]["rows"]
            cursors.append(Cursor([tuple(_decode(c) for c in row) for row in rows]))
        return cursors

    def execute(self, sql, params=()):
        return self.run_many([(sql, params)])[0]

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
    """CREATE TABLE IF NOT EXISTS poem_tags (
        poem_id INTEGER NOT NULL REFERENCES poems(id),
        tag TEXT NOT NULL,
        PRIMARY KEY (poem_id, tag)
    )""",
    """CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        poem_id INTEGER NOT NULL REFERENCES poems(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS follows (
        follower_id INTEGER NOT NULL REFERENCES users(id),
        followed_id INTEGER NOT NULL REFERENCES users(id),
        PRIMARY KEY (follower_id, followed_id)
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL,
        poem_id INTEGER,
        detail TEXT NOT NULL DEFAULT '',
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_poems_user ON poems(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reactions_poem ON reactions(poem_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_tag ON poem_tags(tag)",
    "CREATE INDEX IF NOT EXISTS idx_comments_poem ON comments(poem_id)",
    "CREATE INDEX IF NOT EXISTS idx_follows_followed ON follows(followed_id)",
    "CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read)",
]


def init_db():
    """Создаёт недостающие таблицы и колонки. Старые данные не трогает."""
    conn = get_conn()
    conn.run_many(SCHEMA)

    users_info, poems_info = conn.run_many(
        ["PRAGMA table_info(users)", "PRAGMA table_info(poems)"]
    )
    ucols = {row[1] for row in users_info.fetchall()}
    pcols = {row[1] for row in poems_info.fetchall()}

    alters = []
    if "bio" not in ucols:
        alters.append("ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
    if "updated_at" not in pcols:
        alters.append("ALTER TABLE poems ADD COLUMN updated_at TEXT")
    if "search" not in pcols:
        alters.append("ALTER TABLE poems ADD COLUMN search TEXT")
    if alters:
        conn.run_many(alters)

    # для старых стихов заполняем поле поиска (в нижнем регистре, чтобы работала кириллица)
    rows = conn.execute(
        "SELECT p.id, p.title, p.body, u.username FROM poems p "
        "JOIN users u ON u.id = p.user_id WHERE p.search IS NULL"
    ).fetchall()
    if rows:
        conn.run_many([
            ("UPDATE poems SET search = ? WHERE id = ?", (f"{t}\n{b}\n{u}".lower(), i))
            for i, t, b, u in rows
        ])


if __name__ == "__main__":
    init_db()
    print(get_conn().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
