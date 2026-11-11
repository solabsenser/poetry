import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, init_db

SITE_NAME = "poetrysite"  # поменяй на своё название
REACTIONS = {"heart": "❤️", "fire": "🔥", "tear": "😢", "spark": "✨"}
PER_PAGE = 15
PREVIEW_LINES = 12
MAX_TAGS = 5
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


# ---------- защита от спама (счётчики в памяти процесса) ----------

_hits = defaultdict(deque)


def too_many(key, limit, window):
    """True, если за последние `window` секунд уже было `limit` попыток."""
    now = time.time()
    dq = _hits[key]
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(_hits) > 20000:  # чистим старые ключи, чтобы память не росла
        for k in [k for k, v in _hits.items() if not v or v[-1] < now - 3600]:
            _hits.pop(k, None)
    return len(dq) >= limit


def hit(key):
    _hits[key].append(time.time())


def client_ip():
    ip = request.headers.get("CF-Connecting-IP")
    if not ip:
        ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return ip or request.remote_addr or "?"


# ---------- вспомогательное ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(16)
    return session["csrf"]


def page_url(n):
    """Ссылка на другую страницу списка с сохранением ?q= и ?feed=."""
    args = {k: v for k, v in request.args.items() if k in ("q", "feed")}
    args["page"] = n
    return url_for(request.endpoint, **(request.view_args or {}), **args)


app.jinja_env.globals.update(
    SITE_NAME=SITE_NAME, REACTIONS=REACTIONS, csrf_token=csrf_token, page_url=page_url
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


def parse_tags(raw):
    """'#любовь, осень Любовь' -> ['любовь', 'осень']"""
    tags = []
    for t in re.findall(r"[\w-]{1,30}", raw.lower().replace("#", " ")):
        t = t.strip("-_")
        if t and t not in tags:
            tags.append(t)
    return tags


def search_text(title, body, username, tags):
    return f"{title}\n{body}\n{username}\n{' '.join(tags)}".lower()


def read_poem_form():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").replace("\r\n", "\n").strip("\n")
    tags = parse_tags(request.form.get("tags", ""))
    error = None
    if not title or not body.strip():
        error = "Нужны и название, и текст."
    elif len(title) > 100:
        error = "Название длиннее 100 символов."
    elif len(body) > 5000:
        error = "Текст длиннее 5000 символов."
    elif len(tags) > MAX_TAGS:
        error = f"Не больше {MAX_TAGS} тегов."
    return title, body, tags, error


def notify_stmt(user_id, kind, poem_id=None, detail=""):
    """SQL-запрос для уведомления (или None, если себе). Повторы без прочтения не плодим."""
    if user_id == g.user["id"]:
        return None
    me = g.user["id"]
    return (
        "INSERT INTO notifications (user_id, actor_id, kind, poem_id, detail) "
        "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM notifications "
        "WHERE user_id = ? AND actor_id = ? AND kind = ? AND poem_id IS ? AND is_read = 0)",
        (user_id, me, kind, poem_id, detail, user_id, me, kind, poem_id),
    )


def load_poems(where="", params=(), page=1, per_page=PER_PAGE):
    """Возвращает (список стихов с реакциями/тегами/комментариями, есть_ли_следующая_страница)."""
    rows = q(
        f"""SELECT p.id, p.title, p.body, p.created_at, u.username, p.user_id, p.updated_at
            FROM poems p JOIN users u ON u.id = p.user_id
            {where}
            ORDER BY p.id DESC LIMIT ? OFFSET ?""",
        (*params, per_page + 1, (page - 1) * per_page),
    )
    has_next = len(rows) > per_page
    poems = {}
    for pid, title, body, created, author, uid, updated in rows[:per_page]:
        lines = body.split("\n")
        poems[pid] = {
            "id": pid, "title": title, "body": body, "author": author, "user_id": uid,
            "date": (created or "")[:10], "edited": bool(updated),
            "preview": "\n".join(lines[:PREVIEW_LINES]),
            "truncated": len(lines) > PREVIEW_LINES,
            "counts": {k: 0 for k in REACTIONS}, "mine": None,
            "tags": [], "comments": 0,
        }
    if poems:
        ids = tuple(poems)
        ph = ",".join("?" * len(ids))
        stmts = [
            (f"SELECT poem_id, kind, COUNT(*) FROM reactions WHERE poem_id IN ({ph}) "
             f"GROUP BY poem_id, kind", ids),
            (f"SELECT poem_id, COUNT(*) FROM comments WHERE poem_id IN ({ph}) "
             f"GROUP BY poem_id", ids),
            (f"SELECT poem_id, tag FROM poem_tags WHERE poem_id IN ({ph}) ORDER BY tag", ids),
        ]
        if g.user:
            stmts.append((f"SELECT poem_id, kind FROM reactions "
                          f"WHERE user_id = ? AND poem_id IN ({ph})", (g.user["id"], *ids)))
        res = db().run_many(stmts)  # один HTTP-запрос вместо четырёх
        for pid, kind, n in res[0].fetchall():
            if kind in REACTIONS:
                poems[pid]["counts"][kind] = n
        for pid, n in res[1].fetchall():
            poems[pid]["comments"] = n
        for pid, tag in res[2].fetchall():
            poems[pid]["tags"].append(tag)
        if g.user:
            for pid, kind in res[3].fetchall():
                poems[pid]["mine"] = kind
    return list(poems.values()), has_next


def page_number():
    return max(1, request.args.get("page", 1, type=int))


# ---------- страницы: ленты ----------

@app.route("/health")
def health():
    return "ok"  # для UptimeRobot, базу не трогает


@app.route("/")
def index():
    feed = request.args.get("feed", "all")
    page = page_number()
    if feed == "following" and g.user:
        poems, has_next = load_poems(
            "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)",
            (g.user["id"],), page)
        empty = "Здесь появятся стихи авторов, на которых ты подпишешься."
    else:
        feed = "all"
        poems, has_next = load_poems(page=page)
        empty = None
    return render_template("index.html", poems=poems, page=page, has_next=has_next,
                           tabs=bool(g.user), feed=feed, heading=None, empty=empty)


@app.route("/tag/<tag>")
def tag(tag):
    tags = parse_tags(tag)
    if not tags:
        abort(404)
    page = page_number()
    poems, has_next = load_poems(
        "WHERE p.id IN (SELECT poem_id FROM poem_tags WHERE tag = ?)", (tags[0],), page)
    return render_template("index.html", poems=poems, page=page, has_next=has_next,
                           tabs=False, feed="all", heading="#" + tags[0],
                           empty="Стихов с этим тегом пока нет.")


@app.route("/search")
def search():
    text = request.args.get("q", "").strip()[:100]
    if text.startswith("#") and parse_tags(text):
        return redirect(url_for("tag", tag=parse_tags(text)[0]))
    poems, has_next, page = [], False, page_number()
    if text:
        if too_many(("search", client_ip()), 60, 60):
            abort(429)
        hit(("search", client_ip()))
        words = text.lower().split()[:5]
        where = "WHERE " + " AND ".join(["p.search LIKE ? ESCAPE '\\'"] * len(words))
        params = tuple("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                       for w in words)
        poems, has_next = load_poems(where, params, page)
    return render_template("index.html", poems=poems, page=page, has_next=has_next,
                           tabs=False, feed="all",
                           heading=f"Поиск: {text}" if text else "Поиск",
                           empty="Ничего не нашлось. Попробуй другое слово или тег." if text
                           else "Введи слово, имя автора или #тег.")


# ---------- стихотворения ----------

@app.route("/poem/<int:poem_id>")
def poem(poem_id):
    poems, _ = load_poems("WHERE p.id = ?", (poem_id,), per_page=1)
    if not poems:
        abort(404)
    comments = [
        {"id": r[0], "body": r[1], "date": (r[2] or "")[:10], "author": r[3], "user_id": r[4]}
        for r in q(
            "SELECT c.id, c.body, c.created_at, u.username, c.user_id FROM comments c "
            "JOIN users u ON u.id = c.user_id WHERE c.poem_id = ? ORDER BY c.id LIMIT 300",
            (poem_id,))
    ]
    return render_template("poem.html", p=poems[0], comments=comments)


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_poem():
    error, title, body, tags = None, "", "", []
    if request.method == "POST":
        title, body, tags, error = read_poem_form()
        key = ("poem", g.user["id"])
        if not error and too_many(key, 10, 3600):
            error = "Слишком много публикаций за час. Попробуй чуть позже."
        if not error:
            hit(key)
            row = db().execute(
                "INSERT INTO poems (user_id, title, body, search) VALUES (?, ?, ?, ?) RETURNING id",
                (g.user["id"], title, body, search_text(title, body, g.user["username"], tags)),
            ).fetchone()
            if tags:
                db().run_many([("INSERT INTO poem_tags (poem_id, tag) VALUES (?, ?)", (row[0], t))
                               for t in tags])
            return redirect(url_for("poem", poem_id=row[0]))
    return render_template("poem_form.html", heading="Новое стихотворение", submit="Опубликовать",
                           error=error, title=title, body=body, tags=", ".join(tags), cancel=None)


@app.route("/poem/<int:poem_id>/edit", methods=["GET", "POST"])
@login_required
def edit_poem(poem_id):
    row = q1("SELECT user_id, title, body FROM poems WHERE id = ?", (poem_id,))
    if not row:
        abort(404)
    if row[0] != g.user["id"]:
        abort(403)
    cancel = url_for("poem", poem_id=poem_id)
    if request.method == "GET":
        tags = [r[0] for r in q("SELECT tag FROM poem_tags WHERE poem_id = ? ORDER BY tag", (poem_id,))]
        return render_template("poem_form.html", heading="Редактирование", submit="Сохранить",
                               error=None, title=row[1], body=row[2], tags=", ".join(tags),
                               cancel=cancel)
    title, body, tags, error = read_poem_form()
    key = ("edit", g.user["id"])
    if not error and too_many(key, 30, 3600):
        error = "Слишком много правок за час. Попробуй чуть позже."
    if error:
        return render_template("poem_form.html", heading="Редактирование", submit="Сохранить",
                               error=error, title=title, body=body, tags=", ".join(tags),
                               cancel=cancel)
    hit(key)
    stmts = [
        ("UPDATE poems SET title = ?, body = ?, search = ?, updated_at = CURRENT_TIMESTAMP "
         "WHERE id = ?", (title, body, search_text(title, body, g.user["username"], tags), poem_id)),
        ("DELETE FROM poem_tags WHERE poem_id = ?", (poem_id,)),
    ]
    stmts += [("INSERT INTO poem_tags (poem_id, tag) VALUES (?, ?)", (poem_id, t)) for t in tags]
    db().run_many(stmts)
    return redirect(cancel)


@app.post("/poem/<int:poem_id>/delete")
@login_required
def delete_poem(poem_id):
    row = q1("SELECT user_id FROM poems WHERE id = ?", (poem_id,))
    if not row:
        abort(404)
    if row[0] != g.user["id"]:
        abort(403)
    db().run_many([  # сам стих удаляем последним
        ("DELETE FROM reactions WHERE poem_id = ?", (poem_id,)),
        ("DELETE FROM comments WHERE poem_id = ?", (poem_id,)),
        ("DELETE FROM poem_tags WHERE poem_id = ?", (poem_id,)),
        ("DELETE FROM notifications WHERE poem_id = ?", (poem_id,)),
        ("DELETE FROM poems WHERE id = ?", (poem_id,)),
    ])
    return redirect(url_for("user", username=g.user["username"]))


@app.post("/poem/<int:poem_id>/react")
def react(poem_id):
    if not g.user:
        return jsonify(error="login"), 401
    kind = (request.get_json(silent=True) or {}).get("kind")
    if kind not in REACTIONS:
        return jsonify(error="bad kind"), 400
    key = ("react", g.user["id"])
    if too_many(key, 120, 60):
        return jsonify(error="rate"), 429
    hit(key)

    owner_c, current_c = db().run_many([
        ("SELECT user_id FROM poems WHERE id = ?", (poem_id,)),
        ("SELECT kind FROM reactions WHERE user_id = ? AND poem_id = ?", (g.user["id"], poem_id)),
    ])
    owner, current = owner_c.fetchone(), current_c.fetchone()
    if not owner:
        return jsonify(error="not found"), 404

    stmts = []
    if current and current[0] == kind:  # повторный клик снимает реакцию
        stmts.append(("DELETE FROM reactions WHERE user_id = ? AND poem_id = ?",
                      (g.user["id"], poem_id)))
        mine = None
    else:
        stmts.append(("INSERT OR REPLACE INTO reactions (user_id, poem_id, kind) VALUES (?, ?, ?)",
                      (g.user["id"], poem_id, kind)))
        n = notify_stmt(owner[0], "reaction", poem_id, kind)
        if n:
            stmts.append(n)
        mine = kind
    stmts.append(("SELECT kind, COUNT(*) FROM reactions WHERE poem_id = ? GROUP BY kind", (poem_id,)))
    counts = {k: 0 for k in REACTIONS}
    for k, n in db().run_many(stmts)[-1].fetchall():
        if k in counts:
            counts[k] = n
    return jsonify(counts=counts, mine=mine)


# ---------- комментарии ----------

@app.post("/poem/<int:poem_id>/comment")
@login_required
def add_comment(poem_id):
    body = request.form.get("body", "").replace("\r\n", "\n").strip()
    back = url_for("poem", poem_id=poem_id) + "#comments"
    key = ("comment", g.user["id"])
    if not body:
        flash("Комментарий пустой.", "error")
    elif len(body) > 1000:
        flash("Комментарий длиннее 1000 символов.", "error")
    elif too_many(key, 30, 3600):
        flash("Слишком много комментариев за час. Попробуй чуть позже.", "error")
    else:
        owner = q1("SELECT user_id FROM poems WHERE id = ?", (poem_id,))
        if not owner:
            abort(404)
        hit(key)
        stmts = [("INSERT INTO comments (poem_id, user_id, body) VALUES (?, ?, ?)",
                  (poem_id, g.user["id"], body))]
        n = notify_stmt(owner[0], "comment", poem_id, body[:80])
        if n:
            stmts.append(n)
        db().run_many(stmts)
    return redirect(back)


@app.post("/comment/<int:comment_id>/delete")
@login_required
def delete_comment(comment_id):
    row = q1("SELECT c.user_id, c.poem_id, p.user_id FROM comments c "
             "JOIN poems p ON p.id = c.poem_id WHERE c.id = ?", (comment_id,))
    if not row:
        abort(404)
    if g.user["id"] not in (row[0], row[2]):  # автор комментария или автор стиха
        abort(403)
    db().execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return redirect(url_for("poem", poem_id=row[1]) + "#comments")


# ---------- профили и подписки ----------

@app.route("/u/<username>")
def user(username):
    row = q1("SELECT id, username, bio FROM users WHERE username = ? COLLATE NOCASE", (username,))
    if not row:
        abort(404)
    uid = row[0]
    stmts = [
        ("SELECT COUNT(*) FROM poems WHERE user_id = ?", (uid,)),
        ("SELECT COUNT(*) FROM follows WHERE followed_id = ?", (uid,)),
        ("SELECT COUNT(*) FROM follows WHERE follower_id = ?", (uid,)),
    ]
    if g.user and g.user["id"] != uid:
        stmts.append(("SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?",
                      (g.user["id"], uid)))
    res = db().run_many(stmts)
    profile = {
        "username": row[1], "bio": row[2] or "",
        "poems": res[0].fetchone()[0], "followers": res[1].fetchone()[0],
        "following": res[2].fetchone()[0],
        "is_following": len(res) > 3 and bool(res[3].fetchall()),
        "is_me": bool(g.user and g.user["id"] == uid),
    }
    page = page_number()
    poems, has_next = load_poems("WHERE p.user_id = ?", (uid,), page)
    return render_template("user.html", profile=profile, poems=poems, page=page, has_next=has_next)


@app.post("/u/<username>/follow")
@login_required
def follow(username):
    row = q1("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,))
    if not row:
        abort(404)
    if row[0] == g.user["id"]:
        abort(400)
    key = ("follow", g.user["id"])
    if too_many(key, 60, 3600):
        flash("Слишком много действий. Попробуй чуть позже.", "error")
    else:
        hit(key)
        exists = q1("SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?",
                    (g.user["id"], row[0]))
        if exists:
            db().execute("DELETE FROM follows WHERE follower_id = ? AND followed_id = ?",
                         (g.user["id"], row[0]))
        else:
            stmts = [("INSERT OR IGNORE INTO follows (follower_id, followed_id) VALUES (?, ?)",
                      (g.user["id"], row[0]))]
            n = notify_stmt(row[0], "follow")
            if n:
                stmts.append(n)
            db().run_many(stmts)
    return redirect(url_for("user", username=row[1]))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    error = None
    if request.method == "POST":
        bio = request.form.get("bio", "").replace("\r\n", "\n").strip()
        if len(bio) > 300:
            error = "О себе: не больше 300 символов."
        else:
            db().execute("UPDATE users SET bio = ? WHERE id = ?", (bio, g.user["id"]))
            flash("Профиль сохранён.")
            return redirect(url_for("user", username=g.user["username"]))
        return render_template("settings.html", error=error, bio=bio)
    row = q1("SELECT bio FROM users WHERE id = ?", (g.user["id"],))
    return render_template("settings.html", error=None, bio=(row[0] if row else "") or "")


# ---------- уведомления ----------

@app.route("/notifications")
@login_required
def notifications():
    rows, _ = db().run_many([
        ("SELECT n.kind, n.detail, n.is_read, n.created_at, a.username, p.id, p.title "
         "FROM notifications n JOIN users a ON a.id = n.actor_id "
         "LEFT JOIN poems p ON p.id = n.poem_id "
         "WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 50", (g.user["id"],)),
        ("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (g.user["id"],)),
    ])
    items = []
    for kind, detail, is_read, created, actor, pid, title in rows.fetchall():
        if kind == "reaction":
            text = f"{actor}: {REACTIONS.get(detail, '')} к стихотворению «{title}»"
        elif kind == "comment":
            text = f"{actor} прокомментировал «{title}»: {detail}"
        else:
            text = f"{actor} теперь читает тебя"
        link = url_for("poem", poem_id=pid) if pid else url_for("user", username=actor)
        items.append({"text": text, "link": link, "unread": not is_read, "date": (created or "")[:10]})
    return render_template("notifications.html", items=items)


@app.route("/notifications/count")
def notifications_count():
    count = 0
    if g.user:
        row = q1("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                 (g.user["id"],))
        count = row[0] if row else 0
    resp = jsonify(count=count)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- вход и регистрация ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    error, username = None, ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = ("register", client_ip())
        if too_many(key, 5, 3600):
            error = "Слишком много регистраций с этого адреса. Попробуй позже."
        elif not USERNAME_RE.match(username):
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
            except Exception:
                app.logger.exception("register failed")
                error = "Не получилось создать аккаунт. Попробуй другое имя или повтори позже."
            else:
                hit(key)
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
        key = ("loginfail", client_ip())
        if too_many(key, 10, 900):
            error = "Слишком много неудачных попыток. Подожди 15 минут."
        else:
            row = q1("SELECT id, username, password_hash FROM users "
                     "WHERE username = ? COLLATE NOCASE", (username,))
            if row and check_password_hash(row[2], password):
                login_user(row[0], row[1])
                return redirect(url_for("index"))
            hit(key)
            error = "Неверное имя или пароль."
    return render_template("login.html", error=error, username=username)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- ошибки ----------

@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", text="Такой страницы нет."), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", text="Это действие недоступно."), 403


@app.errorhandler(400)
def bad_request(_):
    return render_template("error.html", text="Запрос не прошёл проверку. Обнови страницу и попробуй снова."), 400


@app.errorhandler(429)
def too_many_requests(_):
    return render_template("error.html", text="Слишком много запросов. Подожди минуту."), 429


if __name__ == "__main__":
    app.run(debug=True)
