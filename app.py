import os
import uuid
from datetime import date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash

import tmdb

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "graylore-dev-secret")

# PostgreSQL connection string. libpq defaults apply for anything omitted, so
# on a local Postgres.app / Homebrew install the default below just works.
#   export DATABASE_URL=postgresql://user:password@localhost:5432/graylore
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/graylore")

ADMIN_EMAIL = "admin@graylore.com"
DEFAULT_REGION = os.environ.get("GRAYLORE_REGION", "IN")   # demo assumes the viewer is browsing from India
RATE_PER_MIN = 0.05
KIDS_RATINGS = ("U",)                                         # what a Kids profile is allowed to see

PLAN_PRICES = {"Basic": (149, 1), "Standard": (399, 2), "Premium": (649, 4)}


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# On serverless hosts (Vercel) there is no start command to run init_db.py, so with
# AUTO_INIT_DB=1 the app loads schema + triggers + seed itself the first time it finds
# an empty database. Checked once per process.
_schema_checked = False


def ensure_schema():
    global _schema_checked
    if _schema_checked or os.environ.get("AUTO_INIT_DB") != "1":
        return
    _schema_checked = True
    conn = get_db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.users') AS t")
    if cur.fetchone()["t"] is None:
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("schema.sql", "triggers.sql", "seed_data.sql"):
            with open(os.path.join(here, name), encoding="utf-8") as f:
                cur.execute(f.read())
    cur.close()
    conn.close()


def current_user_id():
    return session.get("user_id")


def current_profile_id():
    return session.get("profile_id")


def is_kids_profile():
    return bool(session.get("is_kids"))


def is_admin():
    return session.get("email") == ADMIN_EMAIL


# ---------- Decorators ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user_id() is None:
            return redirect(url_for("auth"))
        return view(*args, **kwargs)
    return wrapped


def profile_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user_id() is None:
            return redirect(url_for("auth"))
        if current_profile_id() is None:
            return redirect(url_for("profiles"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user_id() is None:
            return redirect(url_for("auth"))
        if not is_admin():
            flash("That page is for the Graylore admin account only.")
            return redirect(url_for("home") if current_profile_id() else url_for("profiles"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Device sessions ----------

def open_device_session(cur, user_id):
    """
    Register this browser as a device for the user. Each browser gets a stable
    device_id in its cookie; inserting the Sessions row fires trg_device_limit,
    which force-logs-out the oldest device if the plan's cap is exceeded.
    """
    device_id = session.get("device_id") or str(uuid.uuid4())
    session["device_id"] = device_id
    cur.execute(
        "SELECT session_id FROM Sessions WHERE user_id = %s AND device_id = %s AND is_active",
        (user_id, device_id),
    )
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO Sessions (user_id, device_id) VALUES (%s, %s)",
            (user_id, device_id),
        )


@app.before_request
def enforce_device_limit():
    """
    If trg_device_limit has deactivated this browser's session (another device
    pushed it past the plan cap), sign it out. Runs on every request.
    """
    ensure_schema()
    if request.endpoint in (None, "static", "auth", "login", "register", "logout"):
        return None
    user_id, device_id = current_user_id(), session.get("device_id")
    if not user_id or not device_id:
        return None

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT is_active FROM Sessions WHERE user_id = %s AND device_id = %s "
        "ORDER BY login_time DESC, session_id DESC LIMIT 1",
        (user_id, device_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row is not None and not row["is_active"]:
        session.clear()
        if request.endpoint == "progress":
            return jsonify(ok=False, reason="device_limit"), 401
        flash("You've been signed out: another device signed in and your plan's device limit was reached.")
        return redirect(url_for("auth"))
    return None


# ---------- Auth ----------

@app.route("/")
def index():
    if current_user_id() is None:
        return redirect(url_for("auth"))
    if current_profile_id() is None:
        return redirect(url_for("profiles"))
    return redirect(url_for("home"))


@app.route("/auth")
def auth():
    return render_template("auth.html")


@app.route("/register", methods=["POST"])
def register():
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]
    if not name or not email or len(password) < 6:
        flash("Please fill in your name, email and a password of at least 6 characters.")
        return redirect(url_for("auth"))

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO Users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING user_id",
            (name, email, generate_password_hash(password)),
        )
        user_id = cur.fetchone()["user_id"]
        # Every new user starts on Basic until they pick a plan
        cur.execute(
            "INSERT INTO Subscriptions (user_id, plan_type, device_limit, start_date, status) "
            "VALUES (%s, 'Basic', 1, %s, 'active')",
            (user_id, date.today()),
        )
        device_id = session.get("device_id")          # keep the browser's device identity
        session.clear()
        if device_id:
            session["device_id"] = device_id
        session["user_id"] = user_id
        session["email"] = email
        open_device_session(cur, user_id)
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        flash("An account with that email already exists.")
        return redirect(url_for("auth"))
    finally:
        cur.close()
        conn.close()
    return redirect(url_for("profiles"))


@app.route("/login", methods=["POST"])
def login():
    email = request.form["email"].strip().lower()
    password = request.form["password"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM Users WHERE email = %s", (email,))
    user = cur.fetchone()

    if user and check_password_hash(user["password_hash"], password):
        device_id = session.get("device_id")          # keep the browser's device identity
        session.clear()
        if device_id:
            session["device_id"] = device_id
        session["user_id"] = user["user_id"]
        session["email"] = user["email"]
        open_device_session(cur, user["user_id"])
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for("profiles"))

    cur.close()
    conn.close()
    flash("Invalid email or password.")
    return redirect(url_for("auth"))


@app.route("/logout")
def logout():
    user_id, device_id = current_user_id(), session.get("device_id")
    if user_id and device_id:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE Sessions SET is_active = FALSE WHERE user_id = %s AND device_id = %s",
            (user_id, device_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    session.clear()
    if device_id:
        session["device_id"] = device_id
    return redirect(url_for("auth"))


# ---------- Profiles ----------

@app.route("/profiles", methods=["GET", "POST"])
@login_required
def profiles():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["profile_name"].strip()
        is_kids = request.form.get("is_kids") == "on"
        if name:
            cur.execute(
                "INSERT INTO Profiles (user_id, profile_name, is_kids) VALUES (%s, %s, %s)",
                (current_user_id(), name[:50], is_kids),
            )
            conn.commit()

    cur.execute("SELECT * FROM Profiles WHERE user_id = %s ORDER BY profile_id", (current_user_id(),))
    profile_list = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("profile.html", profiles=profile_list)


@app.route("/select_profile/<int:profile_id>")
@login_required
def select_profile(profile_id):
    conn = get_db()
    cur = conn.cursor()
    # Ownership check — a user can only switch to their own profiles
    cur.execute(
        "SELECT profile_id, profile_name, is_kids FROM Profiles WHERE profile_id = %s AND user_id = %s",
        (profile_id, current_user_id()),
    )
    profile = cur.fetchone()
    cur.close()
    conn.close()
    if not profile:
        flash("That profile doesn't belong to your account.")
        return redirect(url_for("profiles"))
    session["profile_id"] = profile["profile_id"]
    session["profile_name"] = profile["profile_name"]
    session["is_kids"] = profile["is_kids"]
    return redirect(url_for("home"))


# ---------- Home / Catalog ----------

# Reusable fragments: a title is "available" when it has an in-window license
# for the viewer's region, and Kids profiles only see U-rated titles.
AVAILABLE_LICENSE = """
    JOIN Licenses l
      ON l.content_id = c.content_id
     AND l.region = %(region)s
     AND CURRENT_DATE BETWEEN l.license_start AND l.license_end
"""
AVG_RATING = """
    LEFT JOIN (
        SELECT content_id, ROUND(AVG(rating)::numeric, 1) AS avg_rating, COUNT(*) AS rating_count
        FROM Ratings GROUP BY content_id
    ) r ON r.content_id = c.content_id
"""
CONTENT_COLS = """
    c.content_id, c.title, c.genre, c.release_date, c.duration_min, c.age_rating,
    c.overview, c.pitch, c.poster_url, c.backdrop_url, c.trailer_key, r.avg_rating, r.rating_count
"""


def kids_clause(params):
    """Return an extra WHERE fragment (and fill params) for Kids profiles."""
    if is_kids_profile():
        params["kids_ratings"] = KIDS_RATINGS
        return " AND c.age_rating IN %(kids_ratings)s"
    return ""


@app.route("/home")
@profile_required
def home():
    conn = get_db()
    cur = conn.cursor()
    params = {"region": DEFAULT_REGION, "profile_id": current_profile_id()}
    kids = kids_clause(params)

    # Full catalog — only titles currently licensed for the viewer's region
    cur.execute(f"""
        SELECT {CONTENT_COLS}, l.region
        FROM Content c
        {AVAILABLE_LICENSE}
        {AVG_RATING}
        WHERE TRUE {kids}
        ORDER BY c.content_id DESC
    """, params)
    catalog = cur.fetchall()

    # Continue watching — this profile's most recent viewing per title that is
    # still in progress (position > 0; a finished film resets to 0)
    cur.execute(f"""
        SELECT DISTINCT ON (c.content_id)
               {CONTENT_COLS}, w.position_sec, w.watched_at AS last_watched
        FROM WatchHistory w
        JOIN Content c ON c.content_id = w.content_id
        {AVG_RATING}
        WHERE w.profile_id = %(profile_id)s {kids}
        ORDER BY c.content_id, w.watched_at DESC, w.watch_id DESC
    """, params)
    continue_watching = [row for row in cur.fetchall() if row["position_sec"]]
    continue_watching.sort(key=lambda row: row["last_watched"], reverse=True)
    continue_watching = continue_watching[:6]

    # Recommendations — co-watch correlation:
    #   w1: titles this profile watched
    #   w2: other profiles who watched those same titles
    #   w3: what those profiles also watched, that this profile has not
    cur.execute(f"""
        SELECT {CONTENT_COLS}, COUNT(DISTINCT w2.profile_id) AS shared_viewers
        FROM WatchHistory w1
        JOIN WatchHistory w2 ON w2.content_id = w1.content_id AND w2.profile_id <> w1.profile_id
        JOIN WatchHistory w3 ON w3.profile_id = w2.profile_id AND w3.content_id <> w1.content_id
        JOIN Content c ON c.content_id = w3.content_id
        {AVAILABLE_LICENSE}
        {AVG_RATING}
        WHERE w1.profile_id = %(profile_id)s
          AND c.content_id NOT IN (SELECT content_id FROM WatchHistory WHERE profile_id = %(profile_id)s)
          {kids}
        GROUP BY {CONTENT_COLS}
        ORDER BY shared_viewers DESC, r.avg_rating DESC NULLS LAST
        LIMIT 6
    """, params)
    recommendations = cur.fetchall()
    recs_fallback = False
    if not recommendations:
        # New profile with no overlap yet — fall back to the best-rated titles they haven't seen
        recs_fallback = True
        cur.execute(f"""
            SELECT {CONTENT_COLS}, 0 AS shared_viewers
            FROM Content c
            {AVAILABLE_LICENSE}
            {AVG_RATING}
            WHERE c.content_id NOT IN (SELECT content_id FROM WatchHistory WHERE profile_id = %(profile_id)s)
              {kids}
            ORDER BY r.avg_rating DESC NULLS LAST, r.rating_count DESC NULLS LAST, c.release_date DESC
            LIMIT 6
        """, params)
        recommendations = cur.fetchall()

    # Trending — most total watch-minutes across all profiles
    cur.execute(f"""
        SELECT {CONTENT_COLS}, SUM(w.watch_minutes) AS total_minutes
        FROM WatchHistory w
        JOIN Content c ON c.content_id = w.content_id
        {AVAILABLE_LICENSE}
        {AVG_RATING}
        WHERE TRUE {kids}
        GROUP BY {CONTENT_COLS}
        ORDER BY total_minutes DESC
        LIMIT 6
    """, params)
    trending = cur.fetchall()

    # Current plan for the billing modal
    cur.execute(
        "SELECT plan_type, device_limit FROM Subscriptions WHERE user_id = %s AND status = 'active' "
        "ORDER BY start_date DESC, sub_id DESC LIMIT 1",
        (current_user_id(),),
    )
    plan = cur.fetchone()

    # Titles this profile has saved to My List (drives the button state in the detail popup)
    cur.execute("SELECT content_id FROM Watchlist WHERE profile_id = %s", (current_profile_id(),))
    watchlist_ids = [row["content_id"] for row in cur.fetchall()]

    # Moods for the Lore bot's chips
    cur.execute("SELECT slug, label, emoji FROM Moods ORDER BY mood_id")
    moods = cur.fetchall()

    cur.close()
    conn.close()

    bot_intro = bot_greeting(session.get("profile_name", ""), is_kids_profile(), continue_watching, recommendations, recs_fallback)

    # Genre rows (Netflix-style): every genre with at least 3 licensed titles, biggest first
    by_genre = {}
    for row in catalog:
        by_genre.setdefault(row["genre"] or "Other", []).append(row)
    genre_rows = sorted(((g, rows) for g, rows in by_genre.items() if len(rows) >= 3),
                        key=lambda gr: (-len(gr[1]), gr[0]))

    return render_template(
        "home.html",
        catalog=catalog,
        genre_rows=genre_rows,
        watchlist_ids=watchlist_ids,
        moods=moods,
        bot_intro=bot_intro,
        continue_watching=continue_watching,
        recommendations=recommendations,
        recs_fallback=recs_fallback,
        trending=trending,
        region=DEFAULT_REGION,
        profile_name=session.get("profile_name", ""),
        is_kids=is_kids_profile(),
        is_admin=is_admin(),
        current_plan=plan["plan_type"] if plan else "Basic",
        plans=PLAN_PRICES,
    )


# ---------- Lore, the recommendation bot ----------

def bot_greeting(name, kids, continue_watching, recommendations, recs_fallback):
    """
    Build the bot's opening line from what this profile has actually been doing.
    Returns {"text": ..., "pick": {...} | None}. The pick is a title the bot can
    offer a Play button for straight away.
    """
    from datetime import datetime
    hour = datetime.now().hour
    tod = "Morning" if hour < 12 else "Afternoon" if hour < 17 else "Evening" if hour < 22 else "Late night"
    first = (name or "there").split()[0]
    hello = f"{tod}, {first}. " if not kids else f"Hi {first}! "

    if continue_watching:
        cw = continue_watching[0]
        left = max(0, (cw["duration_min"] or 0) - (cw["position_sec"] or 0) // 60)
        text = hello + f"You stopped {cw['title']} with about {left} minutes to go. It's still exactly where you left it."
        return {"text": text, "pick": _bot_pick_dict(cw, cw.get("pitch"), resume=True)}
    if recommendations and not recs_fallback:
        rec = recommendations[0]
        text = hello + f"People who watched what you watched went on to {rec['title']} next. Want the two-line version?"
        return {"text": text, "pick": _bot_pick_dict(rec, rec.get("pitch"))}
    if kids:
        return {"text": hello + "I've got funny things and magical things. Which one first?", "pick": None}
    return {"text": hello + "I'm Lore. Tell me how you want to feel tonight and I'll find the film.", "pick": None}


def _bot_pick_dict(row, pitch=None, resume=False):
    return {
        "id": row["content_id"], "title": row["title"], "genre": row["genre"],
        "year": row["release_date"].year if row.get("release_date") else "",
        "rating": row.get("age_rating") or "", "duration": row.get("duration_min"),
        "poster": row.get("poster_url") or "", "pitch": pitch or row.get("overview") or "",
        "avg": float(row["avg_rating"]) if row.get("avg_rating") else 0, "resume": resume,
    }


@app.route("/bot/pick")
def bot_pick():
    """
    The bot's recommendation query. ?mood=<slug>&exclude=1,2,3
    Picks one licensed (and Kids-safe) title tagged with that mood, preferring
    titles this profile hasn't watched, weighted towards better-rated ones.
    Returns {"blurb": mood blurb, "pick": {...}} or {"pick": null}.
    """
    if current_user_id() is None or current_profile_id() is None:
        return jsonify(pick=None), 401
    mood = request.args.get("mood", "surprise")
    try:
        exclude = [int(x) for x in request.args.get("exclude", "").split(",") if x.strip()]
    except ValueError:
        exclude = []
    params = {"region": DEFAULT_REGION, "profile_id": current_profile_id(), "mood": mood,
              "exclude": tuple(exclude) or (0,)}
    kids = kids_clause(params)
    mood_join = "" if mood == "surprise" else "JOIN ContentMoods cm ON cm.content_id = c.content_id JOIN Moods m ON m.mood_id = cm.mood_id AND m.slug = %(mood)s"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT blurb FROM Moods WHERE slug = %s", (mood,))
    row = cur.fetchone()
    blurb = row["blurb"] if row else "Let me think..."

    def query(with_exclude):
        cur.execute(f"""
            SELECT {CONTENT_COLS}, c.pitch,
                   (c.content_id NOT IN (SELECT content_id FROM WatchHistory WHERE profile_id = %(profile_id)s)) AS unwatched
            FROM Content c
            {mood_join}
            {AVAILABLE_LICENSE}
            {AVG_RATING}
            WHERE TRUE {kids} {"AND c.content_id NOT IN %(exclude)s" if with_exclude else ""}
            ORDER BY unwatched DESC, random() * (1 + COALESCE(r.avg_rating, 3)) DESC
            LIMIT 1
        """, params)
        return cur.fetchone()

    pick = query(True) or query(False)
    cur.close()
    conn.close()
    if not pick:
        return jsonify(blurb=blurb, pick=None)
    d = _bot_pick_dict(pick, pick["pitch"])
    d["unwatched"] = bool(pick["unwatched"])
    return jsonify(blurb=blurb, pick=d)


@app.route("/search")
def search():
    """
    Netflix-style live search, called by the home page as you type.
    Matches title, genre, overview and language (case-insensitive), or a release
    year, limited to titles licensed for the viewer's region (and U-rated for Kids).
    Returns JSON: [{id, title, genre, year, rating, overview, poster, backdrop, region, avg, votes, duration}]
    """
    if current_user_id() is None or current_profile_id() is None:
        return jsonify([]), 401
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    params = {"region": DEFAULT_REGION, "like": f"%{q}%", "year": q if q.isdigit() and len(q) == 4 else None}
    kids = kids_clause(params)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT {CONTENT_COLS}, l.region
        FROM Content c
        {AVAILABLE_LICENSE}
        {AVG_RATING}
        WHERE (c.title ILIKE %(like)s OR c.genre ILIKE %(like)s OR c.overview ILIKE %(like)s
               OR c.language ILIKE %(like)s
               OR (%(year)s IS NOT NULL AND EXTRACT(YEAR FROM c.release_date)::text = %(year)s))
          {kids}
        ORDER BY (c.title ILIKE %(like)s) DESC, r.avg_rating DESC NULLS LAST, c.title
        LIMIT 24
    """, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{
        "id": r["content_id"], "title": r["title"], "genre": r["genre"],
        "year": r["release_date"].year if r["release_date"] else "",
        "rating": r["age_rating"] or "", "overview": r["overview"] or "",
        "poster": r["poster_url"] or "", "backdrop": r["backdrop_url"] or "",
        "region": r["region"], "avg": float(r["avg_rating"]) if r["avg_rating"] else 0,
        "votes": r["rating_count"] or 0, "duration": r["duration_min"],
    } for r in rows])


# ---------- Playback ----------

@app.route("/play/<int:content_id>")
@profile_required
def play(content_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM Content WHERE content_id = %s", (content_id,))
    content = cur.fetchone()
    if not content:
        cur.close()
        conn.close()
        flash("That title doesn't exist.")
        return redirect(url_for("home"))

    # Kids profiles are limited to U-rated titles
    if is_kids_profile() and content["age_rating"] not in KIDS_RATINGS:
        cur.close()
        conn.close()
        flash(f"\"{content['title']}\" is rated {content['age_rating']} and isn't available on a Kids profile.")
        return redirect(url_for("home"))

    # License check — block playback outside licensed region/date window
    cur.execute("""
        SELECT 1 FROM Licenses
        WHERE content_id = %s AND region = %s
          AND CURRENT_DATE BETWEEN license_start AND license_end
    """, (content_id, DEFAULT_REGION))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        flash(f"\"{content['title']}\" isn't licensed for playback in {DEFAULT_REGION} right now.")
        return redirect(url_for("home"))

    # Make sure this browser is registered as a device (trigger enforces the cap)
    open_device_session(cur, current_user_id())

    # Resume point — last position this profile reached on this title
    cur.execute("""
        SELECT position_sec, watched_at FROM WatchHistory
        WHERE profile_id = %s AND content_id = %s
        ORDER BY watched_at DESC, watch_id DESC LIMIT 1
    """, (current_profile_id(), content_id))
    last = cur.fetchone()
    resume_sec = last["position_sec"] if last else 0

    # Open a new watch-history row for this viewing; the player updates it
    # via /progress as playback advances
    cur.execute(
        "INSERT INTO WatchHistory (profile_id, content_id, watch_minutes, position_sec) "
        "VALUES (%s, %s, 0, %s) RETURNING watch_id",
        (current_profile_id(), content_id, resume_sec),
    )
    watch_id = cur.fetchone()["watch_id"]
    conn.commit()
    cur.close()
    conn.close()
    return render_template(
        "player.html",
        content=content,
        watch_id=watch_id,
        resume_sec=resume_sec,
        last_watched=last["watched_at"] if last else None,
    )


@app.route("/progress", methods=["POST"])
def progress():
    """
    Called by the player every few seconds (and on pause / page close).
    Body: {"watch_id": int, "position_sec": int, "watched_sec": int}
    Updates the current viewing's resume position and total minutes watched.
    """
    if current_user_id() is None or current_profile_id() is None:
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    try:
        watch_id = int(data["watch_id"])
        position_sec = max(0, int(data.get("position_sec", 0)))
        watched_sec = max(0, int(data.get("watched_sec", 0)))
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False), 400

    conn = get_db()
    cur = conn.cursor()
    # profile_id in the WHERE clause stops one profile updating another's row
    cur.execute("""
        UPDATE WatchHistory
        SET position_sec = %s,
            watch_minutes = GREATEST(watch_minutes, %s),
            watched_at = NOW()
        WHERE watch_id = %s AND profile_id = %s
    """, (position_sec, watched_sec // 60, watch_id, current_profile_id()))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True)


# ---------- My Space: watchlist, reviews, history ----------

def safe_next(default="home"):
    """Redirect target from the form's `next` field, restricted to local paths."""
    nxt = request.form.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for(default)


@app.route("/me")
@profile_required
def my_space():
    conn = get_db()
    cur = conn.cursor()
    params = {"profile_id": current_profile_id(), "region": DEFAULT_REGION}

    # My List, newest first, with a flag for whether it is playable in this region right now
    cur.execute(f"""
        SELECT {CONTENT_COLS}, w.added_at,
               EXISTS (SELECT 1 FROM Licenses l WHERE l.content_id = c.content_id AND l.region = %(region)s
                       AND CURRENT_DATE BETWEEN l.license_start AND l.license_end) AS available
        FROM Watchlist w
        JOIN Content c ON c.content_id = w.content_id
        {AVG_RATING}
        WHERE w.profile_id = %(profile_id)s
        ORDER BY w.added_at DESC
    """, params)
    watchlist = cur.fetchall()

    # My reviews
    cur.execute("""
        SELECT r.rating_id, r.rating, r.review, c.content_id, c.title, c.genre, c.release_date, c.poster_url
        FROM Ratings r
        JOIN Content c ON c.content_id = r.content_id
        WHERE r.profile_id = %(profile_id)s
        ORDER BY r.rating_id DESC
    """, params)
    reviews = cur.fetchall()

    # Recent viewing, latest 20 sessions
    cur.execute("""
        SELECT w.watch_id, w.watch_minutes, w.position_sec, w.watched_at,
               c.content_id, c.title, c.genre, c.duration_min, c.poster_url
        FROM WatchHistory w
        JOIN Content c ON c.content_id = w.content_id
        WHERE w.profile_id = %(profile_id)s
        ORDER BY w.watched_at DESC, w.watch_id DESC
        LIMIT 20
    """, params)
    history = cur.fetchall()

    # Profile stats
    cur.execute("""
        SELECT COALESCE(SUM(watch_minutes), 0) AS minutes, COUNT(DISTINCT content_id) AS titles
        FROM WatchHistory WHERE profile_id = %(profile_id)s
    """, params)
    stats = cur.fetchone()
    cur.close()
    conn.close()
    return render_template(
        "mypage.html",
        watchlist=watchlist, reviews=reviews, history=history, stats=stats,
        profile_name=session.get("profile_name", ""), is_kids=is_kids_profile(), is_admin=is_admin(),
        region=DEFAULT_REGION,
    )


@app.route("/watchlist/toggle", methods=["POST"])
def watchlist_toggle():
    """Add or remove a title from this profile's My List. JSON in: {content_id}; out: {in_list}."""
    if current_user_id() is None or current_profile_id() is None:
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    try:
        content_id = int(data["content_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM Watchlist WHERE profile_id = %s AND content_id = %s RETURNING watchlist_id",
                (current_profile_id(), content_id))
    if cur.fetchone():
        in_list = False
    else:
        cur.execute("INSERT INTO Watchlist (profile_id, content_id) VALUES (%s, %s)",
                    (current_profile_id(), content_id))
        in_list = True
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True, in_list=in_list)


@app.route("/rate", methods=["POST"])
@profile_required
def rate():
    try:
        content_id = int(request.form["content_id"])
        rating = int(request.form["rating"])
    except (KeyError, ValueError):
        flash("Invalid rating.")
        return redirect(safe_next())
    if not 1 <= rating <= 5:
        flash("Rating must be between 1 and 5 stars.")
        return redirect(safe_next())
    review = request.form.get("review", "").strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO Ratings (profile_id, content_id, rating, review)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (profile_id, content_id)
        DO UPDATE SET rating = EXCLUDED.rating, review = EXCLUDED.review
    """, (current_profile_id(), content_id, rating, review))
    conn.commit()
    cur.close()
    conn.close()
    flash("Thanks — your rating was saved.")
    return redirect(safe_next())


@app.route("/rate/delete", methods=["POST"])
@profile_required
def rate_delete():
    try:
        content_id = int(request.form["content_id"])
    except (KeyError, ValueError):
        return redirect(safe_next("my_space"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM Ratings WHERE profile_id = %s AND content_id = %s",
                (current_profile_id(), content_id))
    conn.commit()
    cur.close()
    conn.close()
    flash("Review deleted.")
    return redirect(safe_next("my_space"))


# ---------- Billing ----------

@app.route("/billing", methods=["POST"])
@login_required
def billing():
    plan = request.form.get("plan_type")
    if plan not in PLAN_PRICES:
        flash("Unknown plan.")
        return redirect(url_for("home"))
    price, device_limit = PLAN_PRICES[plan]

    conn = get_db()
    cur = conn.cursor()
    # trg_single_active_sub auto-cancels the previous active subscription
    cur.execute("""
        INSERT INTO Subscriptions (user_id, plan_type, device_limit, start_date, status)
        VALUES (%s, %s, %s, %s, 'active') RETURNING sub_id
    """, (current_user_id(), plan, device_limit, date.today()))
    sub_id = cur.fetchone()["sub_id"]
    cur.execute("""
        INSERT INTO Payments (user_id, sub_id, amount, payment_date, status)
        VALUES (%s, %s, %s, %s, 'paid')
    """, (current_user_id(), sub_id, price, date.today()))
    conn.commit()
    cur.close()
    conn.close()
    flash(f"You're now on the {plan} plan ({device_limit} device{'s' if device_limit > 1 else ''}). ₹{price} charged.")
    return redirect(url_for("home") if current_profile_id() else url_for("profiles"))


# ---------- Admin ----------

@app.route("/admin")
@admin_required
def admin():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT c.content_id, c.title, c.genre, c.age_rating, c.tmdb_id, c.poster_url, c.video_url,
               STRING_AGG(l.region || ' → ' || TO_CHAR(l.license_end, 'DD Mon YYYY'), ', ' ORDER BY l.region) AS licenses
        FROM Content c
        LEFT JOIN Licenses l ON l.content_id = c.content_id
        GROUP BY c.content_id
        ORDER BY c.content_id DESC
    """)
    content_rows = cur.fetchall()

    # Royalty payout — computed live from WatchHistory
    cur.execute("""
        SELECT c.title,
               COALESCE(SUM(w.watch_minutes), 0) AS total_minutes,
               ROUND(COALESCE(SUM(w.watch_minutes), 0) * %s::numeric, 2) AS payout_amount
        FROM Content c
        LEFT JOIN WatchHistory w ON w.content_id = c.content_id
        GROUP BY c.content_id, c.title
        ORDER BY payout_amount DESC, c.title
    """, (RATE_PER_MIN,))
    payouts = cur.fetchall()

    # Stored payout snapshots (the denormalised Payouts table)
    cur.execute("""
        SELECT p.period, COUNT(*) AS titles, SUM(p.total_minutes) AS total_minutes,
               SUM(p.payout_amount) AS payout_amount, MAX(p.rate_per_min) AS rate_per_min
        FROM Payouts p
        GROUP BY p.period
        ORDER BY p.period DESC
    """)
    snapshots = cur.fetchall()

    # Users with their current plan and active device count
    cur.execute("""
        SELECT u.user_id, u.name, u.email, u.created_at,
               s.plan_type, s.device_limit,
               (SELECT COUNT(*) FROM Profiles p WHERE p.user_id = u.user_id) AS profile_count,
               (SELECT COUNT(*) FROM Sessions se WHERE se.user_id = u.user_id AND se.is_active) AS active_devices,
               (SELECT COALESCE(SUM(amount), 0) FROM Payments pay WHERE pay.user_id = u.user_id) AS lifetime_paid
        FROM Users u
        LEFT JOIN Subscriptions s ON s.user_id = u.user_id AND s.status = 'active'
        ORDER BY u.user_id
    """)
    users = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS n FROM Subscriptions WHERE status = 'active'")
    active_subs = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM Content")
    total_titles = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(SUM(watch_minutes), 0) AS n FROM WatchHistory")
    total_minutes = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM Payments WHERE status = 'paid'")
    revenue = cur.fetchone()["n"]

    cur.close()
    conn.close()
    return render_template(
        "admin.html",
        content_rows=content_rows,
        payouts=payouts,
        snapshots=snapshots,
        users=users,
        active_subs=active_subs,
        total_titles=total_titles,
        total_minutes=total_minutes,
        revenue=revenue,
        rate_per_min=RATE_PER_MIN,
        default_region=DEFAULT_REGION,
        this_period=date.today().strftime("%Y-%m"),
        tmdb_enabled=bool(os.environ.get("TMDB_API_KEY")),
    )


@app.route("/admin/add_content", methods=["POST"])
@admin_required
def admin_add_content():
    title = request.form["title"].strip()
    genre = request.form["genre"].strip()
    region = request.form["region"].strip().upper()
    license_end = request.form["license_end"]
    age_rating = request.form.get("age_rating") or "U"
    video_url = request.form.get("video_url", "").strip() or None
    try:
        duration_min = int(request.form.get("duration_min") or 100)
    except ValueError:
        duration_min = 100
    if not title or not genre or not region or not license_end:
        flash("Title, genre, region and license end date are required.")
        return redirect(url_for("admin"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO Content (title, genre, age_rating, release_date, duration_min, video_url) "
        "VALUES (%s, %s, %s, CURRENT_DATE, %s, %s) RETURNING content_id",
        (title[:150], genre[:50], age_rating, duration_min, video_url),
    )
    content_id = cur.fetchone()["content_id"]
    cur.execute(
        "INSERT INTO Licenses (content_id, region, license_start, license_end) "
        "VALUES (%s, %s, CURRENT_DATE, %s)",
        (content_id, region, license_end),
    )
    conn.commit()
    cur.close()
    conn.close()
    flash(f"Added \"{title}\", licensed for {region} until {license_end}.")
    return redirect(url_for("admin"))


@app.route("/admin/snapshot_payouts", methods=["POST"])
@admin_required
def admin_snapshot_payouts():
    """Call the refresh_payouts() stored function for the given period."""
    period = request.form.get("period", "").strip() or date.today().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT refresh_payouts(%s, %s) AS n", (period, RATE_PER_MIN))
    n = cur.fetchone()["n"]
    conn.commit()
    cur.close()
    conn.close()
    flash(f"Payout snapshot for {period} written: {n} title row(s) in Payouts.")
    return redirect(url_for("admin"))


@app.route("/admin/import_tmdb", methods=["POST"])
@admin_required
def admin_import_tmdb():
    """Search TMDB for a title and import the best match."""
    query = request.form.get("query", "").strip()
    region = (request.form.get("region") or DEFAULT_REGION).strip().upper()
    if not query:
        return redirect(url_for("admin"))

    conn = get_db()
    cur = conn.cursor()
    try:
        hits = tmdb.search_movies(query, limit=1)
        if not hits:
            flash(f"TMDB has no results for \"{query}\".")
        else:
            movie = tmdb.fetch_movie(hits[0]["tmdb_id"])
            tmdb.upsert_content(cur, movie, region=region)
            conn.commit()
            flash(f"Imported \"{movie['title']}\" from TMDB, licensed for {region}.")
    except tmdb.TMDBError as e:
        conn.rollback()
        flash(f"TMDB error: {e}")
    except Exception as e:
        conn.rollback()
        flash(f"Import failed: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5050)))
