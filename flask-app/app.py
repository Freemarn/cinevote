"""CineVote: Flask web app and API.

Talks to:
  * PostgreSQL (database server): users, movies, votes. Reads results; writes only accounts and movies.
  * Redis (same server, Docker network): the vote queue, "already voted" markers, the leaderboard cache.
Flask never writes votes to PostgreSQL itself; the Node.js worker does that.
"""
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
import psycopg2.errors
import psycopg2.extras
import redis
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
# Trust the X-Forwarded-* headers set by Nginx (exactly one proxy in front of us)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

VOTE_QUEUE = "votes"                # Redis LIST the worker pops from
CACHE_TTL_SECONDS = 10              # leaderboard cache lifetime
MARKER_TTL_SECONDS = 8 * 24 * 3600  # "already voted" marker outlives the week

rdb = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=6379,
    decode_responses=True,
    socket_timeout=3,
    socket_connect_timeout=3,
)


# ---------------------------------------------------------------- database

def connect_db():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=5432,
        dbname=os.environ.get("DB_NAME", "votingdb"),
        user=os.environ.get("DB_USER", "votinguser"),
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


def get_db():
    """One PostgreSQL connection per request, closed automatically afterwards."""
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=(), one=False, commit=False):
    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
    if commit:
        conn.commit()
    if one:
        return rows[0] if rows else None
    return rows


def week_key(dt=None):
    """ISO week label such as 2026-W40. A new week starts every Monday 00:00 UTC."""
    dt = dt or datetime.now(timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def ensure_admin():
    """Create the admin account from .env on startup, waiting for PostgreSQL if needed."""
    username = os.environ.get("ADMIN_USERNAME", "admin").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print("ADMIN_PASSWORD not set; skipping admin creation", flush=True)
        return
    for attempt in range(1, 31):
        try:
            conn = connect_db()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (username, password_hash, is_admin) "
                        "VALUES (%s, %s, TRUE) ON CONFLICT (username) DO NOTHING",
                        (username, generate_password_hash(password)),
                    )
            finally:
                conn.close()
            print(f"Admin account '{username}' is ready", flush=True)
            return
        except psycopg2.OperationalError as exc:
            print(f"PostgreSQL not reachable yet (attempt {attempt}/30): {exc}", flush=True)
            time.sleep(2)
    raise RuntimeError("Could not reach PostgreSQL: check DB_HOST, DB_PASSWORD and the security groups")


ensure_admin()


# ---------------------------------------------------------------- auth + CSRF

@app.before_request
def load_user_and_check_csrf():
    g.user = None
    if request.endpoint in ("health", "static", "api_leaderboard"):
        return
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
            abort(400, description="Form expired. Go back, refresh the page and try again.")
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = query("SELECT id, username, is_admin FROM users WHERE id = %s", (user_id,), one=True)
        if g.user is None:
            session.clear()


@app.context_processor
def template_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return {"user": g.get("user"), "csrf_token": session["csrf_token"], "week": week_key()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in to vote.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- data helpers

def get_leaderboard(week):
    """Read from the Redis cache; on a miss, query PostgreSQL and cache for 10 seconds."""
    key = f"leaderboard:{week}"
    try:
        cached = rdb.get(key)
        if cached:
            return json.loads(cached), True
    except redis.RedisError:
        pass  # Redis down: fall through to PostgreSQL
    rows = query(
        """
        SELECT m.id, m.title, m.year, m.genre, m.blurb, COUNT(v.id) AS votes
        FROM movies m
        LEFT JOIN votes v ON v.movie_id = m.id AND v.week = %s
        WHERE m.active
        GROUP BY m.id
        ORDER BY votes DESC, m.title ASC
        """,
        (week,),
    )
    board = [dict(r) for r in rows]
    try:
        rdb.setex(key, CACHE_TTL_SECONDS, json.dumps(board))
    except redis.RedisError:
        pass
    return board, False


def past_winners(limit=6):
    """The top movie of each finished week, newest first."""
    return query(
        """
        SELECT DISTINCT ON (v.week) v.week, m.title, m.year, m.genre, COUNT(*) AS votes
        FROM votes v JOIN movies m ON m.id = v.movie_id
        WHERE v.week < %s
        GROUP BY v.week, m.id
        ORDER BY v.week DESC, votes DESC, m.title
        LIMIT %s
        """,
        (week_key(), limit),
    )


# ---------------------------------------------------------------- pages

@app.route("/")
@login_required
def index():
    week = week_key()
    board, from_cache = get_leaderboard(week)
    total = sum(m["votes"] for m in board)
    max_votes = max((m["votes"] for m in board), default=0)

    # Has this user voted? PostgreSQL first, then the Redis marker
    # (covers the moment while the vote is still waiting in the queue).
    row = query("SELECT movie_id FROM votes WHERE user_id = %s AND week = %s",
                (g.user["id"], week), one=True)
    voted_movie = row["movie_id"] if row else None
    if voted_movie is None:
        try:
            marker = rdb.get(f"voted:{week}:{g.user['id']}")
            voted_movie = int(marker) if marker else None
        except redis.RedisError:
            pass

    winners = past_winners()
    return render_template(
        "vote.html", board=board, from_cache=from_cache, total=total, max_votes=max_votes,
        voted_movie=voted_movie, recommendation=winners[0] if winners else None, winners=winners,
    )


@app.route("/vote", methods=["POST"])
@login_required
def vote():
    week = week_key()
    try:
        movie_id = int(request.form.get("movie_id", ""))
    except ValueError:
        abort(400)
    movie = query("SELECT id, title FROM movies WHERE id = %s AND active", (movie_id,), one=True)
    if movie is None:
        abort(404)

    # Check 1: a vote already saved in PostgreSQL
    if query("SELECT 1 FROM votes WHERE user_id = %s AND week = %s", (g.user["id"], week), one=True):
        flash("You have already voted this week. Come back on Monday!", "warning")
        return redirect(url_for("index"))

    marker = f"voted:{week}:{g.user['id']}"
    try:
        # Check 2: SET ... NX only succeeds if the key does not exist yet.
        # It is atomic, so double-clicks or two tabs cannot both get through.
        if not rdb.set(marker, movie_id, nx=True, ex=MARKER_TTL_SECONDS):
            flash("You have already voted this week. Come back on Monday!", "warning")
            return redirect(url_for("index"))
        # Hand the vote to the worker through the Redis list (the queue)
        rdb.lpush(VOTE_QUEUE, json.dumps({
            "user_id": g.user["id"], "movie_id": movie_id, "week": week, "ts": time.time(),
        }))
    except redis.RedisError:
        try:
            rdb.delete(marker)
        except redis.RedisError:
            pass
        flash("Voting is briefly unavailable. Please try again in a minute.", "error")
        return redirect(url_for("index"))

    flash(f"Vote cast for {movie['title']}! The leaderboard updates within seconds.", "voted")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not (3 <= len(username) <= 40) or not username.replace("_", "").isalnum():
            flash("Username must be 3 to 40 letters, numbers or underscores.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            try:
                row = query(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                    (username, generate_password_hash(password)), one=True, commit=True,
                )
            except psycopg2.errors.UniqueViolation:
                get_db().rollback()
                flash("That username is taken. Try another.", "error")
            else:
                session.clear()
                session.permanent = True
                session["user_id"] = row["id"]
                flash(f"Welcome, {username}! Cast your vote for this week.", "success")
                return redirect(url_for("index"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = query("SELECT id, password_hash FROM users WHERE username = %s", (username,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("index")
            return redirect(target)
        flash("Wrong username or password.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You are logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin():
    week = week_key()
    board, _ = get_leaderboard(week)
    total = sum(m["votes"] for m in board)
    max_votes = max((m["votes"] for m in board), default=0)
    stats = query(
        """
        SELECT (SELECT COUNT(*) FROM users)                  AS users,
               (SELECT COUNT(*) FROM votes WHERE week = %s)  AS week_votes,
               (SELECT COUNT(*) FROM votes)                  AS all_votes
        """,
        (week,), one=True,
    )
    turnout = round(100 * stats["week_votes"] / stats["users"], 1) if stats["users"] else 0
    weekly = list(reversed(query(
        "SELECT week, COUNT(*) AS votes FROM votes GROUP BY week ORDER BY week DESC LIMIT 8")))
    max_week = max((w["votes"] for w in weekly), default=0)
    recent = query(
        """
        SELECT u.username, m.title, v.week, v.created_at
        FROM votes v JOIN users u ON u.id = v.user_id JOIN movies m ON m.id = v.movie_id
        ORDER BY v.created_at DESC LIMIT 10
        """
    )
    movies = query("SELECT id, title, active FROM movies ORDER BY title")
    try:
        queue_len = rdb.llen(VOTE_QUEUE)
    except redis.RedisError:
        queue_len = None
    return render_template(
        "admin.html", board=board, total=total, max_votes=max_votes, stats=stats, turnout=turnout,
        weekly=weekly, max_week=max_week, recent=recent, movies=movies, queue_len=queue_len,
    )


@app.route("/admin/movies", methods=["POST"])
@admin_required
def add_movie():
    title = request.form.get("title", "").strip()[:120]
    year = request.form.get("year", "").strip()
    genre = request.form.get("genre", "").strip()[:60] or None
    blurb = request.form.get("blurb", "").strip()[:300] or None
    if not title:
        flash("A title is required.", "error")
        return redirect(url_for("admin"))
    query("INSERT INTO movies (title, year, genre, blurb) VALUES (%s, %s, %s, %s)",
          (title, int(year) if year.isdigit() else None, genre, blurb), commit=True)
    rdb.delete(f"leaderboard:{week_key()}")  # show the new movie immediately
    flash(f"Added {title} to the ballot.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/movies/<int:movie_id>/toggle", methods=["POST"])
@admin_required
def toggle_movie(movie_id):
    query("UPDATE movies SET active = NOT active WHERE id = %s", (movie_id,), commit=True)
    rdb.delete(f"leaderboard:{week_key()}")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------- API + health

@app.route("/api/leaderboard")
def api_leaderboard():
    """JSON used by the page to refresh the leaderboard every 10 seconds."""
    board, from_cache = get_leaderboard(week_key())
    return jsonify({
        "week": week_key(),
        "cached": from_cache,
        "total": sum(m["votes"] for m in board),
        "results": [{"id": m["id"], "title": m["title"], "votes": m["votes"]} for m in board],
    })


@app.route("/health")
def health():
    """Used by the Docker healthcheck: 200 only if Redis AND PostgreSQL answer."""
    status, code = {"redis": "ok", "postgres": "ok"}, 200
    try:
        rdb.ping()
    except Exception:
        status["redis"], code = "down", 503
    try:
        query("SELECT 1")
    except Exception:
        status["postgres"], code = "down", 503
    return jsonify(status), code
