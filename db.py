import os
import libsql
from dotenv import load_dotenv

load_dotenv()

def get_conn():
    return libsql.connect(
        database=os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            user_id INTEGER NOT NULL REFERENCES users(id),
            poem_id INTEGER NOT NULL REFERENCES poems(id),
            kind TEXT NOT NULL,
            PRIMARY KEY (user_id, poem_id)
        )
    """)
    conn.commit()

if __name__ == "__main__":
    init_db()
    conn = get_conn()
    print(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
