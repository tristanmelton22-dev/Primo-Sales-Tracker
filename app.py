from flask import (
    Flask, request, render_template_string, redirect, url_for,
    Response, session, abort, jsonify
)
from datetime import date, timedelta, datetime, timezone
import os
import csv
import io
import json
import time
import hmac
import hashlib
import math

# Python 3.9+ zoneinfo, but some Windows installs can be missing tzdata.
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# Postgres driver (psycopg v3)
# requirements.txt MUST include: psycopg[binary]
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception as e:
    raise RuntimeError(
        "Missing dependency psycopg. Add 'psycopg[binary]' to requirements.txt"
    ) from e

# Optional: requests for Slack posting
try:
    import requests
except Exception:
    requests = None

app = Flask(__name__)

# ---------------- CONFIG ----------------
DEFAULT_WEEKLY_GOAL = 50
APP_VERSION = "V0.9"  # ✅ bumped (GPS removed + admin rep mgmt + daily location)

# IMPORTANT:
# Admin is now stored in DB. We seed a default admin user on first run.
DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "Tristan").strip() or "Tristan"
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Primo1234!").strip() or "Primo1234!"

# Optional seeds for reps (comma-separated)
# e.g. "Tristan,Ricky,Sohaib"
SEED_REPS = os.environ.get("SEED_REPS", "Tristan,Ricky,Sohaib")

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is missing. On Render: Service → Environment → add DATABASE_URL with your Postgres connection string."
    )

# ---------------- SLACK CONFIG ----------------
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()

# Keep these only if you still want to support delete protection mapping (you do).
# Slack user -> rep mapping isn't used to create sales anymore.
SLACK_TRISTAN_ID = os.environ.get("SLACK_TRISTAN_ID", "").strip()
SLACK_RICKY_ID = os.environ.get("SLACK_RICKY_ID", "").strip()
SLACK_SOHAIB_ID = os.environ.get("SLACK_SOHAIB_ID", "").strip()

SLACK_USER_TO_REP = {
    SLACK_TRISTAN_ID: "Tristan",
    SLACK_RICKY_ID: "Ricky",
    SLACK_SOHAIB_ID: "Sohaib",
}
SLACK_USER_TO_REP = {k: v for k, v in SLACK_USER_TO_REP.items() if k}
# ---------------------------------------------


# -------- Timezone (Central, safe fallback) --------
def get_central_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Chicago")
        except Exception:
            pass
    return timezone(timedelta(hours=-6))


TZ = get_central_tz()


def local_today() -> date:
    return datetime.now(TZ).date()


def now_ts() -> float:
    return time.time()


# ---------------- Postgres helpers ----------------
def db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ---------------- Password hashing ----------------
def _pw_salt() -> bytes:
    # A stable salt source; SECRET_KEY must be stable across deploys.
    # (Not ideal cryptography, but MUCH better than plain-text in code.)
    return (app.secret_key or "dev-secret-change-me").encode("utf-8")


def hash_password(password: str) -> str:
    password = (password or "").encode("utf-8")
    dk = hashlib.pbkdf2_hmac("sha256", password, _pw_salt(), 150_000)
    return dk.hex()


def verify_password(password: str, expected_hash_hex: str) -> bool:
    try:
        got = hash_password(password)
        return hmac.compare_digest(got, expected_hash_hex or "")
    except Exception:
        return False


def init_db():
    """
    Creates/updates DB schema safely.
    Adds:
      - reps table (auth + admin role + active flag)
      - rep_day_locations (admin sets "rep location" per day)
    Keeps your existing:
      - stores table
      - weekly_goals table
      - sales_entries table
      - slack tables for delete protection
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # ---------------- Reps ----------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reps (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reps_active ON reps(active);")

            # Daily rep location (manual, set by admin)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rep_day_locations (
                    rep_id BIGINT NOT NULL REFERENCES reps(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    location_text TEXT NOT NULL DEFAULT '',
                    updated_by BIGINT NULL REFERENCES reps(id),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (rep_id, work_date)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rep_day_locations_date ON rep_day_locations(work_date);")

            # ---------------- Stores ----------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stores (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    address TEXT NOT NULL,
                    lat DOUBLE PRECISION NOT NULL,
                    lon DOUBLE PRECISION NOT NULL,
                    radius_m INTEGER NOT NULL DEFAULT 150,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                );
            """)

            # Weekly goal per week_start (Monday)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weekly_goals (
                    week_start DATE PRIMARY KEY,
                    goal_qty INTEGER NOT NULL DEFAULT 50,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # Sales entries (store-aware)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sales_entries (
                    id BIGSERIAL PRIMARY KEY,
                    week_start DATE NOT NULL,
                    rep TEXT NOT NULL,
                    qty INTEGER NOT NULL CHECK (qty > 0),
                    created_at DATE NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    store_id BIGINT NULL REFERENCES stores(id),
                    lat DOUBLE PRECISION NULL,
                    lon DOUBLE PRECISION NULL,
                    accuracy_m DOUBLE PRECISION NULL,
                    slack_channel TEXT NULL,
                    slack_ts TEXT NULL
                );
            """)

            # ✅ track which Slack events were processed (dedupe by event_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS slack_processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # ✅ map Slack message -> sales entry so deletes can remove the right sale
            cur.execute("""
                CREATE TABLE IF NOT EXISTS slack_message_sales (
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    entry_id BIGINT NOT NULL,
                    rep TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (channel_id, message_ts)
                );
            """)

            # Indices
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week ON sales_entries(week_start);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week_rep ON sales_entries(week_start, rep);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week_created ON sales_entries(week_start, created_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_store_week ON sales_entries(store_id, week_start);")

            # Optional unique index for slack mapping columns (safe; IF NOT EXISTS)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_slack_msg
                ON sales_entries (slack_channel, slack_ts)
                WHERE slack_channel IS NOT NULL AND slack_channel <> '' AND slack_ts IS NOT NULL AND slack_ts <> '';
            """)

            # Seed Costco stores (kept; now store selection is manual)
            seed_stores = [
                ("Costco - University City", "8685 Olive Blvd, Saint Louis, MO 63132",
                 38.6762657, -90.3590698, 180),
                ("Costco - St. Peters", "200 Costco Way, Saint Peters, MO 63376",
                 38.7963905, -90.6071090, 180),
                ("Costco - St Louis", "4200 Rusty Rd, Saint Louis, MO 63128",
                 38.5085416, -90.3388793, 180),
                ("Costco - Manchester", "301 Highlands Blvd Drive, Manchester, MO 63011",
                 38.5977985, -90.5071777, 180),
            ]
            for name, address, lat, lon, radius in seed_stores:
                cur.execute("""
                    INSERT INTO stores (name, address, lat, lon, radius_m, active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (name) DO UPDATE SET
                      address = EXCLUDED.address,
                      lat = EXCLUDED.lat,
                      lon = EXCLUDED.lon
                    ;
                """, (name, address, lat, lon, radius))

            # Backfill store_id for legacy rows based on note matching store name
            cur.execute("""
                UPDATE sales_entries se
                SET store_id = s.id
                FROM stores s
                WHERE (se.store_id IS NULL OR se.store_id = 0)
                  AND se.note = s.name;
            """)

            # ---------------- Seed reps ----------------
            # 1) Ensure admin exists
            cur.execute("SELECT id FROM reps WHERE username=%s;", (DEFAULT_ADMIN_USERNAME,))
            admin_row = cur.fetchone()
            if not admin_row:
                cur.execute("""
                    INSERT INTO reps (username, password_hash, is_admin, active)
                    VALUES (%s, %s, TRUE, TRUE);
                """, (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)))

            # 2) Seed additional reps (active, non-admin) if missing
            seed_list = [x.strip() for x in (SEED_REPS or "").split(",") if x.strip()]
            # if seed includes admin, keep admin admin
            for uname in seed_list:
                cur.execute("SELECT id, is_admin FROM reps WHERE username=%s;", (uname,))
                r = cur.fetchone()
                if not r:
                    # default password for seeded reps (change in admin UI)
                    default_pw = "Primo123!"
                    is_admin = (uname == DEFAULT_ADMIN_USERNAME)
                    cur.execute("""
                        INSERT INTO reps (username, password_hash, is_admin, active)
                        VALUES (%s, %s, %s, TRUE);
                    """, (uname, hash_password(DEFAULT_ADMIN_PASSWORD if is_admin else default_pw), bool(is_admin)))
                else:
                    # if it's the admin username but not admin in DB, fix it
                    if uname == DEFAULT_ADMIN_USERNAME and not bool(r["is_admin"]):
                        cur.execute("UPDATE reps SET is_admin=TRUE WHERE username=%s;", (uname,))

        conn.commit()


_db_ready = False


@app.before_request
def ensure_db():
    global _db_ready
    if request.path.startswith("/slack/events"):
        return
    if not _db_ready:
        init_db()
        _db_ready = True


# ---------------- Auth / Roles ----------------
def is_logged_in() -> bool:
    return bool(session.get("logged_in")) and bool(session.get("rep_id"))


def current_rep_id() -> int | None:
    rid = session.get("rep_id")
    try:
        return int(rid) if rid is not None else None
    except Exception:
        return None


def current_rep_name() -> str:
    return session.get("rep_name") or ""


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def require_login():
    if not is_logged_in():
        return redirect(url_for("login", next=request.path))
    return None


def get_rep_by_username(username: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, password_hash, is_admin, active
                FROM reps
                WHERE username=%s;
            """, ((username or "").strip(),))
            return cur.fetchone()


def list_reps(active_only=True):
    with db_conn() as conn:
        with conn.cursor() as cur:
            if active_only:
                cur.execute("""
                    SELECT id, username, is_admin, active
                    FROM reps
                    WHERE active=TRUE
                    ORDER BY is_admin DESC, username ASC;
                """)
            else:
                cur.execute("""
                    SELECT id, username, is_admin, active
                    FROM reps
                    ORDER BY is_admin DESC, active DESC, username ASC;
                """)
            return cur.fetchall()


# ---------------- Business logic ----------------
def get_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday start


def week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)

    def fmt(x: date):
        return f"{x.month}/{x.day}/{str(x.year)[-2:]}"
    return f"{fmt(week_start)}–{fmt(week_end)}"


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def parse_week_start(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def list_weeks() -> list[str]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT week_start FROM sales_entries ORDER BY week_start DESC;")
            rows = cur.fetchall()
    return [r["week_start"].isoformat() for r in rows]


def get_week_goal_qty(week_start: date) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT goal_qty FROM weekly_goals WHERE week_start = %s;", (week_start,))
            row = cur.fetchone()
            if row:
                return int(row["goal_qty"])
            # create default row
            cur.execute("""
                INSERT INTO weekly_goals (week_start, goal_qty)
                VALUES (%s, %s)
                ON CONFLICT (week_start) DO NOTHING;
            """, (week_start, int(DEFAULT_WEEKLY_GOAL)))
        conn.commit()
    return int(DEFAULT_WEEKLY_GOAL)


def set_week_goal_qty(week_start: date, goal_qty: int):
    goal_qty = int(goal_qty)
    if goal_qty <= 0:
        raise ValueError("goal must be positive")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO weekly_goals (week_start, goal_qty)
                VALUES (%s, %s)
                ON CONFLICT (week_start) DO UPDATE SET
                  goal_qty = EXCLUDED.goal_qty,
                  updated_at = NOW();
            """, (week_start, goal_qty))
        conn.commit()


def week_total(week_start: date) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(qty), 0) AS total FROM sales_entries WHERE week_start = %s;",
                (week_start,)
            )
            row = cur.fetchone()
    return int(row["total"] or 0)


def rep_totals_with_today(week_start: date, today_central: date) -> list[tuple[str, int, int]]:
    reps = [r["username"] for r in list_reps(active_only=True)]

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rep, COALESCE(SUM(qty), 0) AS total "
                "FROM sales_entries WHERE week_start = %s "
                "GROUP BY rep;",
                (week_start,)
            )
            week_rows = cur.fetchall()

            cur.execute(
                "SELECT rep, COALESCE(SUM(qty), 0) AS total "
                "FROM sales_entries WHERE week_start = %s AND created_at = %s "
                "GROUP BY rep;",
                (week_start, today_central)
            )
            today_rows = cur.fetchall()

    week_map = {r["rep"]: int(r["total"] or 0) for r in week_rows}
    today_map = {r["rep"]: int(r["total"] or 0) for r in today_rows}

    out = []
    for rep in reps:
        out.append((rep, week_map.get(rep, 0), today_map.get(rep, 0)))

    out.sort(key=lambda x: (-x[1], -x[2], x[0].lower()))
    return out


def get_stores(active_only=True):
    with db_conn() as conn:
        with conn.cursor() as cur:
            if active_only:
                cur.execute("SELECT * FROM stores WHERE active = TRUE ORDER BY name ASC;")
            else:
                cur.execute("SELECT * FROM stores ORDER BY name ASC;")
            rows = cur.fetchall()
    return rows


def store_totals_for_week(week_start: date) -> list[tuple[str, int]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.name AS store, COALESCE(SUM(se.qty), 0) AS total
                FROM stores s
                LEFT JOIN sales_entries se
                  ON se.store_id = s.id AND se.week_start = %s
                WHERE s.active = TRUE
                GROUP BY s.name
                ORDER BY total DESC, store ASC;
            """, (week_start,))
            rows = cur.fetchall()
    return [(r["store"], int(r["total"] or 0)) for r in rows]


def recent_entries(week_start: date, limit: int = 12) -> list[dict]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT se.id, se.rep, se.qty, se.created_at,
                       COALESCE(s.name, se.note, '') AS store_label,
                       COALESCE(se.note,'') AS note,
                       se.slack_channel, se.slack_ts
                FROM sales_entries se
                LEFT JOIN stores s ON s.id = se.store_id
                WHERE se.week_start = %s
                ORDER BY se.id DESC
                LIMIT %s;
            """, (week_start, limit))
            rows = cur.fetchall()

    out = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "rep": r["rep"],
            "qty": int(r["qty"]),
            "created_at": r["created_at"].isoformat(),
            "store": (r["store_label"] or ""),
            "note": (r["note"] or ""),
            "slack_channel": (r["slack_channel"] or ""),
            "slack_ts": (r["slack_ts"] or ""),
        })
    return out


# ---------------- Daily Rep Location (Admin manual) ----------------
def get_rep_location_for_day(rep_id: int, work_date: date) -> str:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT location_text
                FROM rep_day_locations
                WHERE rep_id=%s AND work_date=%s;
            """, (int(rep_id), work_date))
            row = cur.fetchone()
            return (row["location_text"] if row else "") or ""


def set_rep_location_for_day(rep_id: int, work_date: date, location_text: str, updated_by: int | None):
    loc = (location_text or "").strip()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rep_day_locations (rep_id, work_date, location_text, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (rep_id, work_date) DO UPDATE SET
                  location_text=EXCLUDED.location_text,
                  updated_by=EXCLUDED.updated_by,
                  updated_at=NOW();
            """, (int(rep_id), work_date, loc, int(updated_by) if updated_by else None))
        conn.commit()


def locations_for_day(work_date: date) -> dict[str, str]:
    """
    Returns {username: location_text} for active reps.
    """
    reps = list_reps(active_only=True)
    rep_ids = [int(r["id"]) for r in reps]
    if not rep_ids:
        return {}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.username, COALESCE(l.location_text,'') AS location_text
                FROM reps r
                LEFT JOIN rep_day_locations l
                  ON l.rep_id = r.id AND l.work_date = %s
                WHERE r.active=TRUE
                ORDER BY r.is_admin DESC, r.username ASC;
            """, (work_date,))
            rows = cur.fetchall()
    return {row["username"]: (row["location_text"] or "") for row in rows}


# ---------------- Slack posting (still supported) ----------------
def slack_post_sale(rep: str, qty: int, store_name: str, week_start: date):
    """
    Posts a standardized message to Slack.
    Returns (channel_id, ts) if successful, else (None, None).
    """
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID or requests is None:
        return None, None

    prefix = f"APP|sale|rep={rep}|qty={qty}|store={store_name}|week={week_start.isoformat()}"
    human = f"*{rep}* logged *{qty}* Primo water sale(s) at *{store_name or 'Unknown store'}* ✅"
    text = f"{human}\n`{prefix}`"

    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {"channel": SLACK_CHANNEL_ID, "text": text}

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        data = r.json()
        if not data.get("ok"):
            return None, None
        return data.get("channel"), data.get("ts")
    except Exception:
        return None, None


# ---------------- CRUD ----------------
def add_entry_manual(week_start: date, rep: str, qty: int, store_id: int | None):
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be positive")

    rep = (rep or "").strip()
    if not rep:
        raise ValueError("rep required")

    # ensure rep is active (or allow historical reps?)
    rep_names = [r["username"] for r in list_reps(active_only=True)]
    if rep not in rep_names and not is_admin():
        raise ValueError("invalid rep")

    store_name = ""
    if store_id is not None:
        store_id = int(store_id)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM stores WHERE id=%s;", (store_id,))
                s = cur.fetchone()
                if not s:
                    raise ValueError("invalid store")
                store_name = s["name"]

    created_date = local_today()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sales_entries (week_start, rep, qty, created_at, note, store_id, lat, lon, accuracy_m)
                VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL)
                RETURNING id;
            """, (week_start, rep, qty, created_date, store_name, store_id))
            row = cur.fetchone()
            entry_id = int(row["id"])
        conn.commit()

    # Slack post + mapping (optional)
    channel_id, ts = slack_post_sale(rep, qty, store_name, week_start)
    if channel_id and ts:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE sales_entries
                    SET slack_channel=%s, slack_ts=%s
                    WHERE id=%s;
                """, (channel_id, ts, entry_id))

                cur.execute("""
                    INSERT INTO slack_message_sales (channel_id, message_ts, entry_id, rep, qty)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING;
                """, (channel_id, ts, entry_id, rep, qty))
            conn.commit()

    return entry_id, bool(channel_id and ts)


def delete_entry(entry_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sales_entries WHERE id = %s;", (int(entry_id),))
        conn.commit()


def update_entry(entry_id: int, qty: int, store_id: int | None):
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be positive")

    if store_id is not None:
        store_id = int(store_id)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM stores WHERE id=%s;", (store_id,))
                s = cur.fetchone()
                if not s:
                    raise ValueError("invalid store")
        store_name = s["name"]
    else:
        store_name = ""

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sales_entries
                SET qty=%s,
                    store_id=%s,
                    note=%s
                WHERE id=%s;
            """, (qty, store_id, store_name, int(entry_id)))
        conn.commit()


def remove_sale_from_slack(channel_id: str, message_ts: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id FROM slack_message_sales WHERE channel_id = %s AND message_ts = %s;",
                (channel_id, message_ts)
            )
            row = cur.fetchone()
            if not row:
                return False
            entry_id = int(row["entry_id"])

            cur.execute("DELETE FROM sales_entries WHERE id = %s;", (entry_id,))
            cur.execute(
                "DELETE FROM slack_message_sales WHERE channel_id = %s AND message_ts = %s;",
                (channel_id, message_ts)
            )
        conn.commit()
    return True


def slack_verify_request(req) -> bool:
    if not SLACK_SIGNING_SECRET:
        return False

    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False

    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > 60 * 5:
        return False

    body = req.get_data(as_text=True)
    base = f"v0:{ts}:{body}".encode("utf-8")
    my_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_sig, sig)


def slack_event_already_processed(event_id: str) -> bool:
    if not event_id:
        return False
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM slack_processed_events WHERE event_id = %s;", (event_id,))
            return cur.fetchone() is not None


def mark_slack_event_processed(event_id: str):
    if not event_id:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slack_processed_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                (event_id,)
            )
        conn.commit()


# ---------------- UI ----------------
LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Login • Primo Sales Tracker</title>
  <style>
    :root{
      --bgA:#ecfbff; --bgB:#cfefff;
      --text:#0f172a; --muted:#475569;
      --card:rgba(255,255,255,.92);
      --border:rgba(15,23,42,.10);
      --shadow:0 14px 34px rgba(0,0,0,.12);
      --primary:#2563eb;
      --focus: rgba(37,99,235,.22);
    }
    *{ box-sizing:border-box; }
    body{
      margin:0; padding:14px;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color: var(--text);
      background: radial-gradient(circle at 18% 12%, #ffffff 0%, var(--bgA) 40%, var(--bgB) 100%);
      min-height: 100vh;
      display:flex; align-items:center; justify-content:center;
    }
    .card{
      width: min(520px, 100%);
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 18px;
      backdrop-filter: blur(10px);
    }
    .head{
      display:flex; align-items:center; gap:10px;
      margin-bottom: 8px;
    }
    .mark{
      width:34px; height:34px; border-radius:12px;
      background: linear-gradient(135deg, var(--primary), #06b6d4);
      box-shadow: 0 10px 16px rgba(37,99,235,.16);
    }
    h1{ margin: 0; font-size: 18px; font-weight: 950; line-height:1.1; }
    p{ margin: 6px 0 0; color: var(--muted); font-weight: 700; font-size: 12px; }
    label{
      font-weight: 900; font-size: 12px;
      color: rgba(15,23,42,.70);
      display:block; margin: 14px 0 6px;
    }
    .fieldRow{
      display:flex;
      gap:10px;
      align-items: center;
    }
    .input, .eyeBtn{
      height: 46px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.18);
      background: rgba(255,255,255,.98);
    }
    .input{
      width: 100%;
      padding: 0 12px;
      outline: none;
      font-weight: 850;
      font-size: 14px;
    }
    .input:focus{
      box-shadow: 0 0 0 4px var(--focus);
      border-color: rgba(37,99,235,.55);
    }
    .eyeBtn{
      width: 46px;
      flex: 0 0 auto;
      cursor: pointer;
      display:flex;
      align-items:center;
      justify-content:center;
      padding: 0;
    }
    .eyeBtn:focus{
      outline: none;
      box-shadow: 0 0 0 4px var(--focus);
      border-color: rgba(37,99,235,.55);
    }
    button.primary{
      width: 100%;
      margin-top: 14px;
      height: 46px;
      border-radius: 12px;
      border: 1px solid rgba(29,78,216,.25);
      background: linear-gradient(180deg, rgba(37,99,235,.95), rgba(29,78,216,.95));
      color: white;
      font-weight: 950;
      font-size: 14px;
      cursor:pointer;
      box-shadow: 0 10px 18px rgba(37,99,235,.16);
    }
    .err{
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(239,68,68,.28);
      background: rgba(239,68,68,.10);
      font-weight: 850;
      color: rgba(127,29,29,.95);
      font-size: 13px;
    }
    footer{
      margin-top: 12px;
      text-align:center;
      color: rgba(15,23,42,.55);
      font-weight: 900;
      font-size: 12px;
    }
    @media (max-width: 420px){
      .card{ padding: 16px; }
      button.primary{ font-size: 16px; }
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="head">
      <div class="mark" aria-hidden="true"></div>
      <div>
        <h1>Primo Sales Tracker</h1>
        <p>Login with your username + password.</p>
      </div>
    </div>

    <form method="POST" autocomplete="off">
      <label>Username</label>
      <input class="input" type="text" name="username" required>

      <label>Password</label>
      <div class="fieldRow">
        <input class="input" id="pw" type="password" name="password" required>
        <button type="button" class="eyeBtn" id="togglePw" aria-label="Show password">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" stroke="rgba(15,23,42,.75)" stroke-width="2"/>
            <circle cx="12" cy="12" r="3" stroke="rgba(15,23,42,.75)" stroke-width="2"/>
          </svg>
        </button>
      </div>

      <input type="hidden" name="next" value="{{ next_url }}">
      <button class="primary" type="submit">Login</button>
    </form>

    {% if error %}
      <div class="err">{{ error }}</div>
    {% endif %}

    <footer>{{ version }}</footer>
  </div>

  <script>
    (function(){
      const pw = document.getElementById('pw');
      const btn = document.getElementById('togglePw');
      let shown = false;
      btn.addEventListener('click', () => {
        shown = !shown;
        pw.type = shown ? 'text' : 'password';
        btn.setAttribute('aria-label', shown ? 'Hide password' : 'Show password');
      });
    })();
  </script>
</body>
</html>
"""


SLACK_SVG = """
<svg viewBox="0 0 24 24" aria-hidden="true">
  <path fill="#E01E5A" d="M6.1 13.6a1.9 1.9 0 1 1-1.9-1.9h1.9v1.9Z"/>
  <path fill="#E01E5A" d="M7.1 13.6a1.9 1.9 0 1 1 1.9-1.9v1.9H7.1Z"/>

  <path fill="#36C5F0" d="M10.4 6.1a1.9 1.9 0 1 1 1.9-1.9v1.9h-1.9Z"/>
  <path fill="#36C5F0" d="M10.4 7.1a1.9 1.9 0 1 1-1.9 1.9V7.1h1.9Z"/>

  <path fill="#2EB67D" d="M17.9 10.4a1.9 1.9 0 1 1 1.9 1.9h-1.9v-1.9Z"/>
  <path fill="#2EB67D" d="M16.9 10.4a1.9 1.9 0 1 1-1.9-1.9h1.9v1.9Z"/>

  <path fill="#ECB22E" d="M13.6 17.9a1.9 1.9 0 1 1-1.9 1.9v-1.9h1.9Z"/>
  <path fill="#ECB22E" d="M13.6 16.9a1.9 1.9 0 1 1 1.9-1.9v1.9h-1.9Z"/>
</svg>
"""

HTML_PAGE = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Primo Sales Tracker</title>
  <style>
    :root{{
      --bgA:#ecfbff; --bgB:#cfefff;
      --text:#0f172a; --muted:#475569;
      --card:rgba(255,255,255,.92);
      --border:rgba(15,23,42,.10);
      --shadow:0 14px 34px rgba(0,0,0,.12);
      --primary:#2563eb; --danger:#ef4444;
      --focus: rgba(37,99,235,.20);
      --ok: rgba(34,197,94,.12);
      --warn: rgba(245,158,11,.14);
    }}
    *{{ box-sizing:border-box; }}
    body{{
      margin:0; padding: 12px;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color:var(--text);
      background: radial-gradient(circle at 18% 12%, #ffffff 0%, var(--bgA) 40%, var(--bgB) 100%);
    }}
    .wrap{{ max-width: 1100px; margin: 0 auto; }}
    .topbar{{
      display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px;
      padding:10px 12px; border-radius:16px; background: rgba(255,255,255,.92);
      border: 1px solid var(--border); box-shadow: var(--shadow); backdrop-filter: blur(10px);
    }}
    .brand{{ display:flex; align-items:center; gap:10px; min-width: 220px; }}
    .logo{{
      width:32px; height:32px; border-radius:12px;
      background: linear-gradient(135deg, var(--primary), #06b6d4);
      box-shadow: 0 10px 16px rgba(37,99,235,.16);
      flex: 0 0 auto;
    }}
    .brand h1{{ font-size:14px; margin:0; font-weight:950; line-height:1.1; }}
    .brand .sub{{ margin:2px 0 0; font-size:11px; color: var(--muted); font-weight:850; }}
    .topActions{{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
    .pill{{
      display:inline-flex; align-items:center; gap:8px;
      padding:6px 10px; border-radius:999px; background: rgba(15,23,42,.06);
      border: 1px solid rgba(15,23,42,.08); color: rgba(15,23,42,.82);
      font-size: 11px; white-space: nowrap; font-weight: 900;
    }}
    .pill.ok{{ background: var(--ok); border-color: rgba(34,197,94,.22); }}
    .pill.warn{{ background: var(--warn); border-color: rgba(245,158,11,.25); }}
    .logout{{
      text-decoration:none; font-weight: 950; font-size: 12px; color: rgba(15,23,42,.72);
      padding: 6px 10px; border-radius: 999px; border: 1px solid rgba(15,23,42,.10);
      background: rgba(255,255,255,.90);
    }}
    .grid{{ display:grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; align-items:start; }}
    @media (min-width: 980px){{ .grid{{ grid-template-columns: 420px 1fr; }} }}
    .card{{
      background: var(--card); border: 1px solid var(--border); border-radius: 18px;
      box-shadow: var(--shadow); backdrop-filter: blur(10px); padding: 12px;
    }}
    .jugPanel{{ border-radius: 16px; background: rgba(255,255,255,.88); border: 1px solid rgba(15,23,42,.08); padding: 10px; }}
    .jugWrap{{ display:flex; flex-direction:column; align-items:center; gap:10px; }}
    .jugSvg{{ width: min(320px, 100%); height:auto; user-select:none; filter: drop-shadow(0 14px 18px rgba(0,0,0,.16)); }}
    .kpis{{ display:grid; grid-template-columns: 1fr 1fr 1fr; gap:8px; width: 100%; }}
    .kpi{{ padding: 10px; border-radius: 14px; background: rgba(255,255,255,.92); border: 1px solid rgba(15,23,42,.08); box-shadow: 0 10px 16px rgba(0,0,0,.06); }}
    .kpi .label{{ font-size:10px; color: var(--muted); margin-bottom:4px; font-weight:950; text-transform: uppercase; letter-spacing: .06em; }}
    .kpi .value{{ font-size:18px; font-weight:950; margin:0; }}
    .flash{{ width:100%; padding: 10px 12px; border-radius: 14px; border: 1px solid rgba(15,23,42,.12); background: rgba(255,255,255,.92); font-weight: 850; font-size: 13px; }}
    .flash.ok{{ border-color: rgba(34,197,94,.25); background: rgba(34,197,94,.10); }}
    .flash.bad{{ border-color: rgba(239,68,68,.28); background: rgba(239,68,68,.10); }}
    .sectionHead{{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 10px 10px; border-radius: 14px;
      background: rgba(15,23,42,.05); border: 1px solid rgba(15,23,42,.08);
      font-weight: 950; font-size: 12px; color: rgba(15,23,42,.78);
      text-transform: uppercase; letter-spacing: .04em; margin-bottom: 10px; }}
    .weekRow{{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom: 12px; align-items:center; }}
    .weekRow > select{{ flex: 1 1 280px; }}
    form.controls{{ display:grid; grid-template-columns: 1fr; gap:10px; padding: 10px; border-radius: 16px;
      background: rgba(255,255,255,.88); border: 1px solid rgba(15,23,42,.08);
      box-shadow: 0 10px 16px rgba(0,0,0,.06); margin-bottom: 12px; }}
    .formGrid{{ display:grid; grid-template-columns: 1fr; gap:10px; }}
    @media (min-width: 760px){{ .formGrid{{ grid-template-columns: 1fr 1fr; }} .formGrid .span2{{ grid-column: span 2; }} }}
    .btnRow{{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; }}
    .btnRow .span2{{ grid-column: span 2; }}
    @media (max-width: 420px){{ .btnRow{{ grid-template-columns: 1fr; }} .btnRow .span2{{ grid-column: auto; }} }}
    input, select{{ height: 44px; padding: 0 12px; border-radius: 12px; border: 1px solid rgba(15,23,42,.18); outline: none;
      font-size: 14px; background: rgba(255,255,255,.98); font-weight: 850; width: 100%; min-width: 0; }}
    input:focus, select:focus{{ box-shadow: 0 0 0 4px var(--focus); border-color: rgba(37,99,235,.55); }}
    button, a.btn{{ height: 44px; padding: 0 12px; border-radius: 12px; border: 1px solid rgba(15,23,42,.14); background: rgba(255,255,255,.96);
      cursor:pointer; font-weight: 950; font-size: 14px; transition: transform .08s ease; text-decoration:none; color: inherit;
      display:flex; align-items:center; justify-content:center; gap:8px; white-space: nowrap; width: 100%; }}
    button:hover, a.btn:hover{{ transform: translateY(-1px); }}
    button:disabled{{ opacity: .55; cursor: not-allowed; transform:none; }}
    .btn-primary{{ background: linear-gradient(180deg, rgba(37,99,235,.95), rgba(29,78,216,.95)); color: white;
      border-color: rgba(29,78,216,.25); box-shadow: 0 10px 16px rgba(37,99,235,.14); }}
    .btn-danger{{ background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.25); color: rgba(127,29,29,.95); }}
    .btn-ghost{{ background: rgba(15,23,42,.06); border-color: rgba(15,23,42,.10); color: rgba(15,23,42,.85); width:auto; padding: 0 14px; }}
    .tables{{ display:grid; grid-template-columns: 1fr; gap: 12px; }}
    @media (min-width: 980px){{ .tables{{ grid-template-columns: 1fr 1fr; }} }}
    .tableCard{{ border-radius: 16px; background: rgba(255,255,255,.88); border: 1px solid rgba(15,23,42,.08); overflow:hidden; }}
    .tableTitle{{ padding: 10px 12px; font-weight: 950; font-size: 12px; color: rgba(15,23,42,.78);
      background: rgba(15,23,42,.05); border-bottom: 1px solid rgba(15,23,42,.08);
      text-transform: uppercase; letter-spacing: .04em;
      display:flex; align-items:center; justify-content:space-between; gap:10px; }}
    .slackIcon{{ display:inline-flex; align-items:center; justify-content:center; width: 24px; height: 24px; border-radius: 8px;
      background: rgba(255,255,255,.96); border: 1px solid rgba(15,23,42,.10); box-shadow: 0 8px 12px rgba(0,0,0,.06); }}
    .slackIcon svg{{ width: 16px; height: 16px; display:block; }}
    .tableWrap{{ max-height: 220px; overflow:auto; }}
    @media (min-width: 980px){{ .tableWrap{{ max-height: none; overflow: visible; }} }}
    table{{ width:100%; border-collapse: collapse; min-width: 420px; }}
    th, td{{ padding: 10px 10px; font-size: 13px; text-align:left; border-bottom: 1px solid rgba(15,23,42,.08);
      white-space: nowrap; vertical-align: top; background: rgba(255,255,255,.94); }}
    th{{ position: sticky; top: 0; z-index: 1; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: rgba(15,23,42,.65);
      background: rgba(255,255,255,.98); }}
    table.storeTable{{ min-width: 0 !important; width: 100% !important; table-layout: fixed; }}
    table.storeTable th, table.storeTable td{{ white-space: normal !important; }}
    table.storeTable th:last-child, table.storeTable td:last-child{{ text-align: right; width: 90px; }}
    details.manageDetails{{ margin-top: 12px; border-radius: 16px; background: rgba(255,255,255,.88); border: 1px solid rgba(15,23,42,.08); overflow:hidden; }}
    details.manageDetails > summary{{ list-style: none; cursor: pointer; padding: 10px 12px; font-weight: 950; font-size: 12px;
      color: rgba(15,23,42,.78); background: rgba(15,23,42,.05); border-bottom: 1px solid rgba(15,23,42,.08);
      text-transform: uppercase; letter-spacing: .04em; user-select:none; display:flex; align-items:center; justify-content:space-between; gap:10px; }}
    details.manageDetails > summary::-webkit-details-marker{{ display:none; }}
    .chev{{ font-size: 12px; color: rgba(15,23,42,.55); font-weight: 950; }}
    .manageWrap{{ max-height: 320px; overflow: auto; }}
    @media (max-width: 520px){{ .manageWrap{{ max-height: 380px; }} }}
    table.manageTable{{ min-width: 0 !important; width: 100% !important; table-layout: fixed; }}
    table.manageTable td, table.manageTable th{{ white-space: normal !important; }}
    .mini{{ height: 38px !important; font-size: 12px !important; font-weight: 850 !important; }}
    .btnSmall{{ height: 38px !important; font-size: 12px !important; font-weight: 950 !important; padding: 0 10px !important; width:auto !important; }}
    .rowActions{{ display:flex; gap:8px; flex-wrap:wrap; }}
    footer{{ margin-top: 10px; text-align:center; color: rgba(15,23,42,.55); font-weight: 900; font-size: 12px; padding: 6px 0 2px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <div class="logo" aria-hidden="true"></div>
        <div>
          <h1>Primo Sales Tracker</h1>
          <div class="sub">Logged in as <b>{{{{ user_rep }}}}</b>{{% if not admin %}} (rep){{% endif %}}</div>
        </div>
      </div>

      <div class="topActions">
        <div class="pill {{{{ 'ok' if today_location else 'warn' }}}}">Today location: <b>{{{{ today_location if today_location else 'Not set' }}}}</b></div>
        <div class="pill">Week: <b>{{{{ range_label }}}}</b></div>
        <div class="pill">Total: <b>{{{{ weekly_sales }}}}</b></div>
        <div class="pill">Goal: <b>{{{{ goal }}}}</b></div>
        <a class="logout" href="{{{{ url_for('logout') }}}}">Logout</a>
      </div>
    </div>

    <div class="grid">
      <!-- LEFT -->
      <div class="card">
        <div class="jugPanel">
          <div class="jugWrap">
            <svg class="jugSvg" viewBox="0 0 280 420" role="img" aria-label="Jug fill shows weekly progress">
              <defs>
                <clipPath id="jugClip">
                  <path d="
                    M112 46
                    C112 36 168 36 168 46
                    L168 64
                    C168 74 190 80 206 92
                    C220 102 226 116 226 132
                    C226 146 222 156 220 170
                    C218 186 224 206 228 226
                    C232 248 232 272 228 290
                    C224 310 226 328 228 342
                    C230 360 218 374 200 380
                    C172 388 108 388 80 380
                    C62 374 50 360 52 342
                    C54 328 56 310 52 290
                    C48 272 48 248 52 226
                    C56 206 62 186 60 170
                    C58 156 54 146 54 132
                    C54 116 60 102 74 92
                    C90 80 112 74 112 64
                    Z
                  " />
                </clipPath>

                <linearGradient id="plastic" x1="0" x2="1">
                  <stop offset="0" stop-color="rgba(255,255,255,0.22)" />
                  <stop offset="0.28" stop-color="rgba(255,255,255,0.12)" />
                  <stop offset="0.55" stop-color="rgba(0,0,0,0.08)" />
                  <stop offset="1" stop-color="rgba(255,255,255,0.22)" />
                </linearGradient>

                <linearGradient id="waterGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stop-color="rgba(190,237,255,0.96)"/>
                  <stop offset="0.62" stop-color="rgba(100,207,250,0.95)"/>
                  <stop offset="1" stop-color="rgba(2,132,199,0.96)"/>
                </linearGradient>

                <linearGradient id="waterEdge" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stop-color="rgba(255,255,255,0.20)"/>
                  <stop offset="1" stop-color="rgba(255,255,255,0.00)"/>
                </linearGradient>

                <linearGradient id="capBlue" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0" stop-color="rgba(59,130,246,0.98)"/>
                  <stop offset="0.6" stop-color="rgba(37,99,235,0.98)"/>
                  <stop offset="1" stop-color="rgba(30,64,175,0.98)"/>
                </linearGradient>

                <linearGradient id="sheen" x1="0" x2="1">
                  <stop offset="0" stop-color="rgba(255,255,255,0.12)"/>
                  <stop offset="0.35" stop-color="rgba(255,255,255,0.04)" />
                  <stop offset="0.60" stop-color="rgba(0,0,0,0.04)" />
                  <stop offset="1" stop-color="rgba(255,255,255,0.10)" />
                </linearGradient>

                <path id="primoArc" d="M86 198 C122 190 158 190 194 198" />
                <path id="waterArc" d="M98 220 C128 216 152 216 182 220" />
              </defs>

              <!-- Water -->
              <g clip-path="url(#jugClip)">
                <rect x="0" y="{{{{ water_y }}}}" width="280" height="{{{{ water_h }}}}" fill="url(#waterGrad)"/>
                {{% if fill_percentage > 0 %}}
                  <rect x="0" y="{{{{ water_y }}}}" width="280" height="22" fill="url(#waterEdge)" opacity="0.8"/>
                  <ellipse cx="140" cy="{{{{ water_y + 6 }}}}" rx="150" ry="10" fill="rgba(255,255,255,0.14)" opacity="0.85"/>
                {{% endif %}}
              </g>

              <!-- Jug body -->
              <path d="
                M112 46
                C112 36 168 36 168 46
                L168 64
                C168 74 190 80 206 92
                C220 102 226 116 226 132
                C226 146 222 156 220 170
                C218 186 224 206 228 226
                C232 248 232 272 228 290
                C224 310 226 328 228 342
                C230 360 218 374 200 380
                C172 388 108 388 80 380
                C62 374 50 360 52 342
                C54 328 56 310 52 290
                C48 272 48 248 52 226
                C56 206 62 186 60 170
                C58 156 54 146 54 132
                C54 116 60 102 74 92
                C90 80 112 74 112 64
                Z
              " fill="url(#plastic)" stroke="rgba(15,23,42,0.18)" stroke-width="2.3"/>

              <g clip-path="url(#jugClip)">
                <rect x="0" y="0" width="280" height="420" fill="url(#sheen)" opacity="0.58"/>
              </g>

              <!-- Branding -->
              <g opacity="0.84">
                <circle cx="96" cy="210" r="11" fill="none" stroke="rgba(37,99,235,0.36)" stroke-width="3" opacity="0.78"/>
                <text font-size="21" font-weight="900"
                      fill="rgba(15,23,42,0.58)"
                      stroke="rgba(255,255,255,0.10)" stroke-width="0.6"
                      style="letter-spacing:1px;">
                  <textPath href="#primoArc" startOffset="50%" text-anchor="middle">PRIMO</textPath>
                </text>
                <text font-size="13" font-weight="850"
                      fill="rgba(15,23,42,0.48)"
                      stroke="rgba(255,255,255,0.08)" stroke-width="0.5"
                      style="letter-spacing:0.8px;">
                  <textPath href="#waterArc" startOffset="50%" text-anchor="middle">WATER</textPath>
                </text>
              </g>

              <!-- Cap -->
              <g>
                <rect x="106" y="8" width="68" height="36" rx="12" fill="url(#capBlue)" stroke="rgba(0,0,0,0.12)" />
                <rect x="100" y="5" width="80" height="14" rx="7" fill="rgba(96,165,250,0.95)" stroke="rgba(0,0,0,0.10)"/>
                <rect x="110" y="40" width="60" height="5" rx="2.5" fill="rgba(0,0,0,0.12)" opacity="0.32"/>
                <rect x="114" y="12" width="12" height="30" rx="6" fill="rgba(255,255,255,0.18)" opacity="0.85"/>
              </g>
              <rect x="116" y="62" width="48" height="5" rx="2.5" fill="rgba(255,255,255,0.12)" opacity="0.55"/>
            </svg>

            <div class="kpis">
              <div class="kpi"><div class="label">Sold</div><p class="value">{{{{ weekly_sales }}}}</p></div>
              <div class="kpi"><div class="label">Remaining</div><p class="value">{{{{ remaining }}}}</p></div>
              <div class="kpi"><div class="label">Complete</div><p class="value">{{{{ fill_percentage | round(0) }}}}%</p></div>
            </div>

            {{% if message %}}
              <div class="flash {{{{ 'ok' if ok else 'bad' }}}}">{{{{ message }}}}</div>
            {{% endif %}}
          </div>
        </div>
      </div>

      <!-- RIGHT -->
      <div class="card">
        <div class="sectionHead">Sales</div>

        <div class="weekRow">
          <form method="GET" action="{{{{ url_for('index') }}}}" style="margin:0; display:flex; gap:10px; flex-wrap:wrap; width:100%;">
            <select name="week">
              <option value="{{{{ selected_week_start }}}}" selected>Viewing: {{{{ range_label }}}}</option>
              <option value="{{{{ current_week_start }}}}">Current Week ({{{{ current_range_label }}}})</option>
              {{% for wk in weeks %}}
                {{% if wk != selected_week_start and wk != current_week_start %}}
                  <option value="{{{{ wk }}}}">{{{{ wk }}}}</option>
                {{% endif %}}
              {{% endfor %}}
            </select>
            <button class="btn-ghost" type="submit" style="flex:0 0 auto;">View</button>
          </form>
        </div>

        <!-- Rep form (manual store selection, no GPS) -->
        <form method="POST" id="salesForm" class="controls" autocomplete="off">
          <input type="hidden" name="week" value="{{{{ selected_week_start }}}}">
          <div class="formGrid">
            {{% if admin %}}
              <div>
                <select name="rep">
                  {{% for r in reps %}}
                    <option value="{{{{ r.username }}}}" {{% if r.username == user_rep %}}selected{{% endif %}}>{{{{ r.username }}}}</option>
                  {{% endfor %}}
                </select>
              </div>
            {{% else %}}
              <input type="hidden" name="rep" value="{{{{ user_rep }}}}">
              <div class="span2">
                <div class="pill warn" style="width:100%; justify-content:space-between;">
                  <span>Store is manual now:</span>
                  <span style="font-weight:950;">Select store below</span>
                </div>
              </div>
            {{% endif %}}

            <div class="span2">
              <select name="store_id" required>
                <option value="" selected>Select store…</option>
                {{% for s in stores %}}
                  {{% if s.active %}}
                    <option value="{{{{ s.id }}}}">{{{{ s.name }}}}</option>
                  {{% endif %}}
                {{% endfor %}}
              </select>
            </div>

            <div class="span2">
              <input type="number" id="salesInput" name="sales" placeholder="Quantity" min="1" step="1" required>
            </div>
          </div>

          <div class="btnRow">
            <button id="addBtn" type="submit" name="action" value="add" class="btn-primary span2">Add Sale</button>

            {{% if admin %}}
              <button type="submit" name="action" value="reset" class="btn-danger"
                      onclick="return confirm('Reset this week\\'s total to 0?');">Reset</button>
              <a class="btn span2" href="{{{{ url_for('export_csv', week=selected_week_start) }}}}">Export CSV</a>
            {{% else %}}
              <a class="btn span2" href="{{{{ url_for('export_csv', week=selected_week_start) }}}}">Export CSV</a>
            {{% endif %}}
          </div>
        </form>

        {{% if admin %}}
          <form method="POST" action="{{{{ url_for('admin_goal') }}}}" class="controls" style="margin-top:12px;">
            <div class="sectionHead" style="margin:0 0 8px;">Admin — weekly goal</div>
            <input type="hidden" name="week" value="{{{{ selected_week_start }}}}">
            <div class="formGrid">
              <div class="span2">
                <input type="number" name="goal_qty" min="1" step="1" value="{{{{ goal }}}}" required>
              </div>
            </div>
            <div class="btnRow">
              <button class="btn-primary span2" type="submit">Save Goal</button>
            </div>
          </form>

          <form method="POST" action="{{{{ url_for('admin_set_location') }}}}" class="controls" style="margin-top:12px;">
            <div class="sectionHead" style="margin:0 0 8px;">Admin — set rep location for today</div>
            <div class="formGrid">
              <div>
                <select name="rep_id" required>
                  {{% for r in reps %}}
                    <option value="{{{{ r.id }}}}">{{{{ r.username }}}}</option>
                  {{% endfor %}}
                </select>
              </div>
              <div>
                <input type="text" name="location_text" placeholder="e.g. University City Costco" required>
              </div>
              <div class="span2">
                <button class="btn-primary" type="submit">Save Today Location</button>
              </div>
            </div>
            <div style="font-weight:850; color: rgba(15,23,42,.62); font-size:12px;">
              Current (today): {{% for name, loc in today_locations.items() %}}
                <span style="display:inline-block; margin-right:10px;"><b>{{{{ name }}}}:</b> {{{{ loc if loc else '—' }}}}</span>
              {{% endfor %}}
            </div>
          </form>

          <details class="manageDetails">
            <summary>
              Admin — manage reps (add/remove/reset password)
              <span class="chev">▼</span>
            </summary>

            <div class="manageWrap" style="padding: 10px;">
              <form method="POST" action="{{{{ url_for('admin_add_rep') }}}}" class="controls" style="margin:0;">
                <div class="sectionHead" style="margin:0 0 8px;">Add rep</div>
                <div class="formGrid">
                  <div>
                    <input type="text" name="username" placeholder="Username (e.g. NewRep)" required>
                  </div>
                  <div>
                    <input type="text" name="password" placeholder="Temporary password" required>
                  </div>
                  <div class="span2">
                    <label style="display:flex; gap:10px; align-items:center; margin:0; font-weight:900; font-size:12px; color:rgba(15,23,42,.70);">
                      <input type="checkbox" name="is_admin" value="1" style="width:18px; height:18px;">
                      Make admin
                    </label>
                  </div>
                  <div class="span2">
                    <button class="btn-primary" type="submit">Add Rep</button>
                  </div>
                </div>
              </form>

              <div style="height:10px;"></div>

              <table class="manageTable">
                <thead>
                  <tr>
                    <th>Rep</th>
                    <th style="width:120px;">Role</th>
                    <th style="width:120px;">Status</th>
                    <th style="width:260px;">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {{% for r in reps_all %}}
                    <tr>
                      <td><b>{{{{ r.username }}}}</b></td>
                      <td>{{{{ 'Admin' if r.is_admin else 'Rep' }}}}</td>
                      <td>{{{{ 'Active' if r.active else 'Inactive' }}}}</td>
                      <td>
                        <div class="rowActions">
                          <form method="POST" action="{{{{ url_for('admin_toggle_rep') }}}}" style="margin:0;">
                            <input type="hidden" name="rep_id" value="{{{{ r.id }}}}">
                            <input type="hidden" name="set_active" value="{{{{ '0' if r.active else '1' }}}}">
                            <button class="btnSmall {{{{ 'btn-danger' if r.active else '' }}}}" type="submit"
                              onclick="return confirm('{{{{ 'Deactivate' if r.active else 'Reactivate' }}}} {{{{ r.username }}}}?');">
                              {{{{ 'Deactivate' if r.active else 'Reactivate' }}}}
                            </button>
                          </form>

                          <form method="POST" action="{{{{ url_for('admin_reset_password') }}}}" style="margin:0; display:flex; gap:8px; align-items:center;">
                            <input type="hidden" name="rep_id" value="{{{{ r.id }}}}">
                            <input class="mini" type="text" name="new_password" placeholder="New password" required style="max-width: 160px;">
                            <button class="btnSmall btn-primary" type="submit"
                              onclick="return confirm('Reset password for {{{{ r.username }}}}?');">
                              Reset PW
                            </button>
                          </form>
                        </div>
                        {{% if r.username == user_rep %}}
                          <div style="font-size:12px; font-weight:850; color: rgba(15,23,42,.60); margin-top:6px;">
                            (This is you)
                          </div>
                        {{% endif %}}
                      </td>
                    </tr>
                  {{% endfor %}}
                </tbody>
              </table>
            </div>
          </details>

          <details class="manageDetails">
            <summary>
              Admin — manage entries (edit/delete)
              <span class="chev">▼</span>
            </summary>

            <div class="manageWrap">
              <table class="manageTable">
                <thead>
                  <tr>
                    <th style="width: 70px;">ID</th>
                    <th style="width: 120px;">Rep</th>
                    <th style="width: 90px;">Qty</th>
                    <th>Store</th>
                    <th style="width: 120px;">Date</th>
                    <th style="width: 170px;">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {{% if recent %}}
                    {{% for e in recent %}}
                      <tr>
                        <td>{{{{ e.id }}}}</td>
                        <td>{{{{ e.rep }}}}</td>
                        <td>
                          <form method="POST" action="{{{{ url_for('admin_update') }}}}" style="display:flex; gap:8px; align-items:center; margin:0;">
                            <input type="hidden" name="week" value="{{{{ selected_week_start }}}}">
                            <input type="hidden" name="entry_id" value="{{{{ e.id }}}}">
                            <input class="mini" type="number" name="qty" value="{{{{ e.qty }}}}" min="1" step="1" style="max-width: 90px;">
                        </td>
                        <td>
                            <select class="mini" name="store_id" style="max-width: 320px;">
                              <option value="">(none)</option>
                              {{% for s in stores %}}
                                <option value="{{{{ s.id }}}}" {{% if e.store == s.name %}}selected{{% endif %}}>{{{{ s.name }}}}</option>
                              {{% endfor %}}
                            </select>
                        </td>
                        <td>{{{{ e.created_at }}}}</td>
                        <td>
                            <button class="btnSmall btn-primary" type="submit">Save</button>
                          </form>
                          <form method="POST" action="{{{{ url_for('admin_delete') }}}}" style="display:inline; margin:0;">
                            <input type="hidden" name="week" value="{{{{ selected_week_start }}}}">
                            <input type="hidden" name="entry_id" value="{{{{ e.id }}}}">
                            <button class="btnSmall btn-danger" type="submit"
                                    onclick="return confirm('Delete entry #{{{{ e.id }}}}?');">Delete</button>
                          </form>
                        </td>
                      </tr>
                    {{% endfor %}}
                  {{% else %}}
                    <tr><td colspan="6" style="color: rgba(15,23,42,.60); font-weight: 900;">No entries yet</td></tr>
                  {{% endif %}}
                </tbody>
              </table>
            </div>
          </details>

          <details class="manageDetails">
            <summary>
              Admin — store geofence radius (legacy; GPS feature removed)
              <span class="chev">▼</span>
            </summary>
            <div class="manageWrap">
              <table class="manageTable">
                <thead><tr><th>Store</th><th style="width:140px;">Radius (m)</th><th style="width:170px;">Action</th></tr></thead>
                <tbody>
                  {{% for s in stores %}}
                    <tr>
                      <td>{{{{ s.name }}}}<div style="color:rgba(15,23,42,.55); font-weight:850; font-size:12px;">{{{{ s.address }}}}</div></td>
                      <td>
                        <form method="POST" action="{{{{ url_for('admin_store_radius') }}}}" style="margin:0; display:flex; gap:8px; align-items:center;">
                          <input type="hidden" name="store_id" value="{{{{ s.id }}}}">
                          <input class="mini" type="number" name="radius_m" min="50" step="10" value="{{{{ s.radius_m }}}}">
                      </td>
                      <td>
                          <button class="btnSmall btn-primary" type="submit">Save</button>
                        </form>
                      </td>
                    </tr>
                  {{% endfor %}}
                </tbody>
              </table>
            </div>
          </details>
        {{% endif %}}

        <div class="tables" style="margin-top: 12px;">
          <div class="tableCard">
            <div class="tableTitle">
              <div>Leaderboard</div>
              <div class="slackIcon" title="Slack posts are generated by the app" aria-label="Slack">{SLACK_SVG}</div>
            </div>
            <div class="tableWrap">
              <table>
                <thead><tr><th>Rep</th><th>Total</th><th>Today location</th></tr></thead>
                <tbody>
                  {{% if rep_rows %}}
                    {{% for rep, total, today_total in rep_rows %}}
                      <tr>
                        <td>{{{{ rep }}}}</td>
                        <td>
                          <b>{{{{ total }}}}</b>
                          <span style="color: rgba(15,23,42,.62); font-weight: 900;">
                            &nbsp;&nbsp;+{{{{ today_total }}}}
                          </span>
                        </td>
                        <td style="color: rgba(15,23,42,.75); font-weight: 900;">
                          {{{{ today_locations.get(rep, '') if today_locations else '' }}}}
                        </td>
                      </tr>
                    {{% endfor %}}
                  {{% else %}}
                    <tr><td colspan="3" style="color: rgba(15,23,42,.60); font-weight: 900;">No entries yet</td></tr>
                  {{% endif %}}
                </tbody>
              </table>
            </div>
          </div>

          <div class="tableCard">
            <div class="tableTitle">
              <div>Store production</div>
              <div class="slackIcon" title="Store is selected manually now" aria-label="Slack">{SLACK_SVG}</div>
            </div>
            <div class="tableWrap">
              <table class="storeTable">
                <thead><tr><th>Store</th><th>Week</th></tr></thead>
                <tbody>
                  {{% if store_rows %}}
                    {{% for store, total in store_rows %}}
                      <tr><td>{{{{ store }}}}</td><td><b>{{{{ total }}}}</b></td></tr>
                    {{% endfor %}}
                  {{% else %}}
                    <tr><td colspan="2" style="color: rgba(15,23,42,.60); font-weight: 900;">No entries yet</td></tr>
                  {{% endif %}}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <footer>{{{{ version }}}}</footer>
      </div>
    </div>
  </div>
</body>
</html>
"""


# ---------------- Routes ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    error = None

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        rep = get_rep_by_username(username)
        if not rep or not bool(rep.get("active")):
            error = "Invalid username or account inactive."
        else:
            if not verify_password(password, rep.get("password_hash") or ""):
                error = "Incorrect password."
            else:
                session.clear()
                session["logged_in"] = True
                session["rep_id"] = int(rep["id"])
                session["rep_name"] = rep["username"]
                session["is_admin"] = bool(rep["is_admin"])
                return redirect(next_url)

    return render_template_string(
        LOGIN_PAGE,
        error=error,
        next_url=next_url,
        version=APP_VERSION
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
def index():
    gate = require_login()
    if gate:
        return gate

    today = local_today()
    current_wk_start = get_week_start(today)

    requested = parse_week_start(request.args.get("week") or request.form.get("week"))
    selected_wk_start = requested or current_wk_start

    message = request.args.get("msg")
    ok = (request.args.get("ok", "1") == "1")

    user_rep = current_rep_name()
    admin = is_admin()

    # Today rep location pill
    my_loc = ""
    rid = current_rep_id()
    if rid:
        my_loc = get_rep_location_for_day(rid, today)

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "reset":
            if not admin:
                return redirect(url_for("index", week=selected_wk_start.isoformat(), msg="Admins only.", ok="0"))
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM sales_entries WHERE week_start = %s;", (selected_wk_start,))
                conn.commit()
            return redirect(url_for("index", week=selected_wk_start.isoformat(), msg="Reset complete.", ok="1"))

        if action == "add":
            rep = (request.form.get("rep") or "").strip() or user_rep
            raw = (request.form.get("sales") or "").strip()
            store_id_raw = (request.form.get("store_id") or "").strip()
            try:
                qty = int(raw)
                if qty <= 0:
                    raise ValueError()

                store_id = int(store_id_raw) if store_id_raw else None
                entry_id, slack_ok = add_entry_manual(selected_wk_start, rep, qty, store_id)

                # Store label for message
                store_label = ""
                if store_id:
                    for s in get_stores(active_only=False):
                        if int(s["id"]) == int(store_id):
                            store_label = s["name"]
                            break

                msg = f"Added {qty} sale(s) for {rep}" + (f" at {store_label}." if store_label else ".")
                if not slack_ok:
                    msg += " (Slack post not sent — check SLACK_BOT_TOKEN / SLACK_CHANNEL_ID.)"
                return redirect(url_for("index", week=selected_wk_start.isoformat(), msg=msg, ok="1"))
            except Exception:
                return redirect(url_for("index", week=selected_wk_start.isoformat(),
                                        msg="Could not add sale. Qty must be >0 and you must select a store.", ok="0"))

        return redirect(url_for("index", week=selected_wk_start.isoformat(), msg="Unknown action.", ok="0"))

    goal_qty = get_week_goal_qty(selected_wk_start)
    weekly_sales = week_total(selected_wk_start)
    fill_percentage = clamp((weekly_sales / goal_qty) * 100 if goal_qty else 0, 0, 100)
    remaining = max(0, goal_qty - weekly_sales)

    # Water fill mapping
    top_y = 64
    bottom_y = 388
    usable_h = bottom_y - top_y
    water_h = (fill_percentage / 100.0) * usable_h
    if fill_percentage <= 0:
        water_h = 0
        water_y = bottom_y
    else:
        water_y = bottom_y - water_h

    water_h = int(round(max(0, water_h)))
    water_y = int(round(water_y))

    rep_rows = rep_totals_with_today(selected_wk_start, today)
    store_rows = store_totals_for_week(selected_wk_start)
    weeks = list_weeks()
    recent = recent_entries(selected_wk_start, limit=12) if admin else []
    stores = get_stores(active_only=False)

    reps_active = list_reps(active_only=True)
    reps_all = list_reps(active_only=False) if admin else []

    today_locations = locations_for_day(today)  # {username: location}

    return render_template_string(
        HTML_PAGE,
        user_rep=user_rep,
        admin=admin,
        reps=reps_active,
        reps_all=reps_all,
        weekly_sales=weekly_sales,
        goal=goal_qty,
        fill_percentage=fill_percentage,
        remaining=remaining,
        water_h=water_h,
        water_y=water_y,
        range_label=week_label(selected_wk_start),
        current_range_label=week_label(current_wk_start),
        current_week_start=current_wk_start.isoformat(),
        selected_week_start=selected_wk_start.isoformat(),
        weeks=weeks,
        message=message,
        ok=ok,
        rep_rows=rep_rows,
        store_rows=store_rows,
        recent=recent,
        version=APP_VERSION,
        stores=stores,
        today_location=my_loc,
        today_locations=today_locations
    )


@app.route("/admin/goal", methods=["POST"])
def admin_goal():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    wk = parse_week_start(request.form.get("week"))
    week_start = wk or get_week_start(local_today())
    goal_qty = request.form.get("goal_qty") or ""

    try:
        set_week_goal_qty(week_start, int(goal_qty))
        return redirect(url_for("index", week=week_start.isoformat(), msg="Weekly goal saved.", ok="1"))
    except Exception:
        return redirect(url_for("index", week=week_start.isoformat(), msg="Goal must be a whole number > 0.", ok="0"))


@app.route("/admin/set-location", methods=["POST"])
def admin_set_location():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    rep_id = request.form.get("rep_id") or ""
    loc = request.form.get("location_text") or ""
    try:
        rep_id_int = int(rep_id)
        set_rep_location_for_day(rep_id_int, local_today(), loc, current_rep_id())
        return redirect(url_for("index", msg="Today location saved.", ok="1"))
    except Exception:
        return redirect(url_for("index", msg="Could not save location.", ok="0"))


@app.route("/admin/reps/add", methods=["POST"])
def admin_add_rep():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    is_admin_flag = (request.form.get("is_admin") or "").strip() == "1"

    if not username or not password:
        return redirect(url_for("index", msg="Username + password required.", ok="0"))

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM reps WHERE username=%s;", (username,))
                if cur.fetchone():
                    return redirect(url_for("index", msg="That username already exists.", ok="0"))
                cur.execute("""
                    INSERT INTO reps (username, password_hash, is_admin, active)
                    VALUES (%s, %s, %s, TRUE);
                """, (username, hash_password(password), bool(is_admin_flag)))
            conn.commit()
        return redirect(url_for("index", msg=f"Added rep {username}.", ok="1"))
    except Exception:
        return redirect(url_for("index", msg="Could not add rep.", ok="0"))


@app.route("/admin/reps/toggle", methods=["POST"])
def admin_toggle_rep():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    rep_id = request.form.get("rep_id") or ""
    set_active = request.form.get("set_active") or ""
    try:
        rep_id_int = int(rep_id)
        active_val = True if str(set_active).strip() == "1" else False

        # Prevent deactivating the last admin
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, username, is_admin FROM reps WHERE id=%s;", (rep_id_int,))
                r = cur.fetchone()
                if not r:
                    return redirect(url_for("index", msg="Rep not found.", ok="0"))

                if bool(r["is_admin"]) and not active_val:
                    cur.execute("SELECT COUNT(*) AS c FROM reps WHERE is_admin=TRUE AND active=TRUE;")
                    c = int(cur.fetchone()["c"])
                    if c <= 1:
                        return redirect(url_for("index", msg="Cannot deactivate the last active admin.", ok="0"))

                cur.execute("""
                    UPDATE reps
                    SET active=%s, updated_at=NOW()
                    WHERE id=%s;
                """, (active_val, rep_id_int))
            conn.commit()

        return redirect(url_for("index", msg="Rep status updated.", ok="1"))
    except Exception:
        return redirect(url_for("index", msg="Could not update rep.", ok="0"))


@app.route("/admin/reps/reset-password", methods=["POST"])
def admin_reset_password():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    rep_id = request.form.get("rep_id") or ""
    new_pw = (request.form.get("new_password") or "").strip()
    if not new_pw:
        return redirect(url_for("index", msg="New password required.", ok="0"))

    try:
        rep_id_int = int(rep_id)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE reps
                    SET password_hash=%s, updated_at=NOW()
                    WHERE id=%s;
                """, (hash_password(new_pw), rep_id_int))
            conn.commit()
        return redirect(url_for("index", msg="Password reset.", ok="1"))
    except Exception:
        return redirect(url_for("index", msg="Could not reset password.", ok="0"))


@app.route("/admin/store-radius", methods=["POST"])
def admin_store_radius():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    store_id = request.form.get("store_id") or ""
    radius_m = request.form.get("radius_m") or ""
    try:
        store_id = int(store_id)
        radius_m = int(radius_m)
        if radius_m < 50 or radius_m > 1000:
            raise ValueError()

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE stores SET radius_m=%s WHERE id=%s;", (radius_m, store_id))
            conn.commit()
        return redirect(url_for("index", msg="Store radius saved.", ok="1"))
    except Exception:
        return redirect(url_for("index", msg="Radius must be between 50 and 1000 meters.", ok="0"))


@app.route("/admin/update", methods=["POST"])
def admin_update():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    wk = parse_week_start(request.form.get("week"))
    week_start = wk or get_week_start(local_today())

    entry_id = request.form.get("entry_id") or ""
    qty = request.form.get("qty") or ""
    store_id = request.form.get("store_id") or ""

    try:
        sid = int(store_id) if store_id.strip() else None
        update_entry(int(entry_id), int(qty), sid)
        msg = "Saved changes."
        okv = "1"
    except Exception:
        msg = "Could not save. Qty must be > 0 and Store must be valid."
        okv = "0"

    return redirect(url_for("index", week=week_start.isoformat(), msg=msg, ok=okv))


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    wk = parse_week_start(request.form.get("week"))
    week_start = wk or get_week_start(local_today())

    entry_id = request.form.get("entry_id") or ""
    try:
        delete_entry(int(entry_id))
        msg = f"Deleted entry #{entry_id}."
        okv = "1"
    except Exception:
        msg = "Could not delete that entry."
        okv = "0"

    return redirect(url_for("index", week=week_start.isoformat(), msg=msg, ok=okv))


@app.route("/export.csv")
def export_csv():
    gate = require_login()
    if gate:
        return gate

    today = local_today()
    current_wk_start = get_week_start(today)
    wk = parse_week_start(request.args.get("week"))
    week_start = wk or current_wk_start

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT se.week_start, se.rep, se.qty, COALESCE(s.name, se.note, '') AS store,
                       se.created_at, se.lat, se.lon, se.accuracy_m
                FROM sales_entries se
                LEFT JOIN stores s ON s.id = se.store_id
                WHERE se.week_start = %s
                ORDER BY se.id ASC;
            """, (week_start,))
            rows = cur.fetchall()

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["week_start", "rep", "qty", "store", "date", "lat", "lon", "accuracy_m"])
    for r in rows:
        w.writerow([
            r["week_start"].isoformat(),
            r["rep"],
            int(r["qty"]),
            r["store"],
            r["created_at"].isoformat(),
            r["lat"],
            r["lon"],
            r["accuracy_m"],
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"primo_sales_{week_start.isoformat()}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ---------------- SLACK EVENTS (delete protection) ----------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    payload = request.get_json(silent=True) or {}

    # Slack URL verification
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    # Verify signature
    if not slack_verify_request(request):
        return Response("invalid signature", status=403)

    # Ensure DB ready
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True

    event_id = payload.get("event_id", "")
    if slack_event_already_processed(event_id):
        return Response("ok", status=200)

    event = payload.get("event", {}) or {}
    channel_id = (event.get("channel") or "").strip()

    # Only watch the configured channel (if set)
    if SLACK_CHANNEL_ID and channel_id != SLACK_CHANNEL_ID:
        mark_slack_event_processed(event_id)
        return Response("ok", status=200)

    subtype = event.get("subtype")

    # Ignore edits
    if subtype == "message_changed":
        mark_slack_event_processed(event_id)
        return Response("ok", status=200)

    # If a Slack message was deleted, remove the sale linked to that Slack message
    if subtype == "message_deleted":
        deleted_ts = (event.get("deleted_ts") or "").strip()
        if not deleted_ts:
            prev = event.get("previous_message") or {}
            deleted_ts = (prev.get("ts") or "").strip()

        if deleted_ts:
            remove_sale_from_slack(channel_id, deleted_ts)

        mark_slack_event_processed(event_id)
        return Response("ok", status=200)

    mark_slack_event_processed(event_id)
    return Response("ok", status=200)


@app.route("/db-status")
def db_status():
    gate = require_login()
    if gate:
        return gate
    if not is_admin():
        abort(403)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM sales_entries;")
            row = cur.fetchone()
            count = int(row["c"])
            cur.execute("SELECT current_database() AS db, current_user AS u;")
            row2 = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS c FROM reps;")
            reps_count = int(cur.fetchone()["c"])

    return {
        "ok": True,
        "database": row2["db"],
        "user": row2["u"],
        "rows_in_sales_entries": int(count),
        "rows_in_reps": reps_count,
        "central_today": local_today().isoformat(),
        "version": APP_VERSION,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    init_db()
    app.run(host="0.0.0.0", port=port)
