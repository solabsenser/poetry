import os
import re
import secrets
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, init_db

SITE_NAME = "Немножко о стихах"  # поменяй на своё название
REACTIONS = {"heart": "❤️", "fire": "🔥", "tear": "😢", "spark": "✨"}
PER_PAGE = 15
PREVIEW_LINES = 12
USERNAME_RE = re.compile(r"^\w{3,30}$")

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # на Render true, локально false
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)

try:
    init_db()
except Exception as e:  # сайт всё равно запустится, ошибка будет видна в логах
    print("init_db failed:", e)


# ---------- вспомогательное ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(16)
    return session["csrf"]


app.jinja_env.globals.update(
    SITE_NAME=SITE_NAME, REACTIONS=REACTIONS, csrf_token=csrf_token
)


def db():
    if "db" not in g:
        g.db = get_conn()
    return g.db


def q(sql, params=()):
    return db().execute(sql, params).fetchall()


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


@app.before_request
def before():
    g.user = None
    if request.endpoint in ("health", "static"):
        return
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
        real = session.get("csrf", "")
        if not real or not secrets.compare_digest(sent.encode(), real.encode()):
            abort(400)
    if session.get("uid"):
        g.user = {"id": session["uid"], "username": session["username"]}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def login_user(uid, username):
    session.clear()
    session.permanent = True
    session["uid"] = uid
    session["username"] = username


def load_poems(where="", params=(), page=1, per_page=PER_PAGE):
    """Возвращает (список стихов с реакциями, есть_ли_следующая_страница)."""
    rows = q(
        f"""SELECT p.id, p.title, p.body, p.created_at, u.username
            FROM poems p JOIN users u ON u.id = p.user_id
            {where}
            ORDER BY p.id DESC LIMIT ? OFFSET ?""",
        (*params, per_page + 1, (page - 1) * per_page),
    )
    has_next = len(rows) > per_page
    poems = {}
    for pid, title, body, created, author in rows[:per_page]:
        lines = body.split("\n")
        poems[pid] = {
            "id": pid, "title": title, "body": body, "author": author,
            "date": (created or "")[:10],
            "preview": "\n".join(lines[:PREVIEW_LINES]),
            "truncated": len(lines) > PREVIEW_LINES,
            "counts": {k: 0 for k in REACTIONS}, "mine": None,
        }
    if poems:
        ids = tuple(poems)
        ph = ",".join("?" * len(ids))
        for pid, kind, n in q(
            f"SELECT poem_id, kind, COUNT(*) FROM reactions "
            f"WHERE poem_id IN ({ph}) GROUP BY poem_id, kind", ids
        ):
            if kind in REACTIONS:
                poems[pid]["counts"][kind] = n
        if g.user:
            for pid, kind in q(
                f"SELECT poem_id, kind FROM reactions "
                f"WHERE user_id = ? AND poem_id IN ({ph})", (g.user["id"], *ids)
            ):
                poems[pid]["mine"] = kind
    return list(poems.values()), has_next


# ---------- страницы ----------

@app.route("/health")
def health():
    return "ok"  # для UptimeRobot, базу не трогает


@app.route("/")
def index():
    page = max(1, request.args.get("page", 1, type=int))
    poems, has_next = load_poems(page=page)
    return render_template("index.html", poems=poems, page=page, has_next=has_next)


@app.route("/poem/<int:poem_id>")
def poem(poem_id):
    poems, _ = load_poems("WHERE p.id = ?", (poem_id,), per_page=1)
    if not poems:
        abort(404)
    return render_template("poem.html", p=poems[0])


@app.route("/u/<username>")
def user(username):
    row = q1("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,))
    if not row:
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    poems, has_next = load_poems("WHERE p.user_id = ?", (row[0],), page=page)
    return render_template("user.html", author=row[1], poems=poems, page=page, has_next=has_next)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    error, username = None, ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not USERNAME_RE.match(username):
            error = "Имя: от 3 до 30 символов, только буквы, цифры и подчёркивание."
        elif not 8 <= len(password) <= 128:
            error = "Пароль: от 8 до 128 символов."
        elif q1("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)):
            error = "Это имя уже занято."
        else:
            try:
                row = db().execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?) RETURNING id",
                    (username, generate_password_hash(password)),
                ).fetchone()
                db().commit()
            except Exception:
                app.logger.exception("register failed")
                error = "Не получилось создать аккаунт. Попробуй другое имя или повтори позже."
            else:
                login_user(row[0], username)
                return redirect(url_for("index"))
    return render_template("register.html", error=error, username=username)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error, username = None, ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = q1(
            "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        )
        if row and check_password_hash(row[2], password):
            login_user(row[0], row[1])
            return redirect(url_for("index"))
        error = "Неверное имя или пароль."
    return render_template("login.html", error=error, username=username)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_poem():
    error, title, body = None, "", ""
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").replace("\r\n", "\n").strip("\n")
        if not title or not body.strip():
            error = "Нужны и название, и текст."
        elif len(title) > 100:
            error = "Название длиннее 100 символов."
        elif len(body) > 5000:
            error = "Текст длиннее 5000 символов."
        else:
            row = db().execute(
                "INSERT INTO poems (user_id, title, body) VALUES (?, ?, ?) RETURNING id",
                (g.user["id"], title, body),
            ).fetchone()
            db().commit()
            return redirect(url_for("poem", poem_id=row[0]))
    return render_template("new.html", error=error, title=title, body=body)


@app.post("/poem/<int:poem_id>/delete")
@login_required
def delete_poem(poem_id):
    row = q1("SELECT user_id FROM poems WHERE id = ?", (poem_id,))
    if not row:
        abort(404)
    if row[0] != g.user["id"]:
        abort(403)
    db().execute("DELETE FROM reactions WHERE poem_id = ?", (poem_id,))
    db().execute("DELETE FROM poems WHERE id = ?", (poem_id,))
    db().commit()
    return redirect(url_for("user", username=g.user["username"]))


@app.post("/poem/<int:poem_id>/react")
def react(poem_id):
    if not g.user:
        return jsonify(error="login"), 401
    kind = (request.get_json(silent=True) or {}).get("kind")
    if kind not in REACTIONS:
        return jsonify(error="bad kind"), 400
    if not q1("SELECT 1 FROM poems WHERE id = ?", (poem_id,)):
        return jsonify(error="not found"), 404

    current = q1("SELECT kind FROM reactions WHERE user_id = ? AND poem_id = ?",
                 (g.user["id"], poem_id))
    if current and current[0] == kind:  # повторный клик снимает реакцию
        db().execute("DELETE FROM reactions WHERE user_id = ? AND poem_id = ?",
                     (g.user["id"], poem_id))
        mine = None
    else:
        db().execute("INSERT OR REPLACE INTO reactions (user_id, poem_id, kind) VALUES (?, ?, ?)",
                     (g.user["id"], poem_id, kind))
        mine = kind
    db().commit()

    counts = {k: 0 for k in REACTIONS}
    for k, n in q("SELECT kind, COUNT(*) FROM reactions WHERE poem_id = ? GROUP BY kind", (poem_id,)):
        if k in counts:
            counts[k] = n
    return jsonify(counts=counts, mine=mine)


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", text="Такой страницы нет."), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", text="Это действие недоступно."), 403


@app.errorhandler(400)
def bad_request(_):
    return render_template("error.html", text="Запрос не прошёл проверку. Обнови страницу и попробуй снова."), 400


if __name__ == "__main__":
    app.run(debug=True)
