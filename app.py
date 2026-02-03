from flask import (
    Flask, request, render_template_string, redirect, url_for,
    Response, session
)
from datetime import date, timedelta
import sqlite3
from pathlib import Path
import os
import csv
import io

app = Flask(__name__)

# ---------------- CONFIG ----------------
WEEKLY_GOAL = 50
DB_PATH = Path("sales.db")
APP_VERSION = "V0.5"

REPS = ["Tristan", "Ricky", "Sohaib"]
ADMIN_REP = "Tristan"
DEFAULT_REP = REPS[0] if REPS else "Rep"

STORE_LOCATIONS = [
    "Costco - University City",
    "Costco - Manchester",
    "Costco - St Louis",
    "Costco - St. Peters",
]

USER_PASSWORDS = {
    "Tristan": "Primo1234!",
    "Ricky": "Primo123!",
    "Sohaib": "Primo123!",
}

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# ---------------------------------------

_db_ready = False


# ---------------- SQLite helpers ----------------
def db_conn():
    if DB_PATH.parent and str(DB_PATH.parent) != ".":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_fragment: str):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {c["name"] for c in cols}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment}")


def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sales_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                rep TEXT NOT NULL,
                qty INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        ensure_column(conn, "sales_entries", "note", "note TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week ON sales_entries(week_start)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week_rep ON sales_entries(week_start, rep)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week_created ON sales_entries(week_start, created_at)")
        conn.commit()


def get_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday start


def week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)

    def fmt(x: date):
        return f"{x.month}/{x.day}/{str(x.year)[-2:]}"
    return f"{fmt(week_start)}–{fmt(week_end)}"


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def parse_week_start(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def list_weeks() -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT week_start FROM sales_entries ORDER BY week_start DESC"
        ).fetchall()
    return [r["week_start"] for r in rows]


def week_total(week_start: date) -> int:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM sales_entries WHERE week_start = ?",
            (week_start.isoformat(),)
        ).fetchone()
    return int(row["total"] or 0)


def rep_totals(week_start: date) -> list[tuple[str, int]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT rep, COALESCE(SUM(qty), 0) AS total "
            "FROM sales_entries WHERE week_start = ? "
            "GROUP BY rep ORDER BY total DESC, rep ASC",
            (week_start.isoformat(),)
        ).fetchall()
    return [(r["rep"], int(r["total"])) for r in rows]


def recent_entries(week_start: date, limit: int = 8) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, rep, qty, created_at, COALESCE(note,'') AS note "
            "FROM sales_entries WHERE week_start = ? "
            "ORDER BY id DESC LIMIT ?",
            (week_start.isoformat(), limit)
        ).fetchall()

    out = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "rep": r["rep"],
            "qty": int(r["qty"]),
            "created_at": r["created_at"],
            "store": r["note"] or "",
        })
    return out


def add_entry(week_start: date, rep: str, qty: int, store_location: str):
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be positive")

    rep = (rep or "").strip() or DEFAULT_REP
    if rep not in REPS:
        raise ValueError("invalid rep")

    store_location = (store_location or "").strip()
    if store_location not in STORE_LOCATIONS:
        raise ValueError("invalid store location")

    created_date = date.today().isoformat()  # date only

    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sales_entries (week_start, rep, qty, created_at, note) VALUES (?, ?, ?, ?, ?)",
            (week_start.isoformat(), rep, qty, created_date, store_location)
        )
        conn.commit()


def undo_last_entry(week_start: date):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, rep, qty FROM sales_entries WHERE week_start = ? ORDER BY id DESC LIMIT 1",
            (week_start.isoformat(),)
        ).fetchone()
        if row is None:
            return None
        entry_id = int(row["id"])
        rep = row["rep"]
        qty = int(row["qty"])
        conn.execute("DELETE FROM sales_entries WHERE id = ?", (entry_id,))
        conn.commit()
        return {"id": entry_id, "rep": rep, "qty": qty}


def reset_week(week_start: date):
    with db_conn() as conn:
        conn.execute("DELETE FROM sales_entries WHERE week_start = ?", (week_start.isoformat(),))
        conn.commit()


@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


# ---------------- Auth / Roles ----------------
def is_logged_in() -> bool:
    return bool(session.get("logged_in")) and bool(session.get("rep"))


def current_rep() -> str:
    return session.get("rep") or DEFAULT_REP


def is_admin() -> bool:
    return current_rep() == ADMIN_REP


def require_login():
    if not is_logged_in():
        return redirect(url_for("login", next=request.path))
    return None


# ---------------- UI ----------------
LOGIN_PAGE = """
<!DOCTYPE html>
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
    .fieldRow{ display:flex; gap:10px; align-items: center; }
    .input, .eyeBtn{
      height: 46px; border-radius: 12px;
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
  </style>
</head>
<body>
  <div class="card">
    <div class="head">
      <div class="mark" aria-hidden="true"></div>
      <div>
        <h1>Primo Sales Tracker</h1>
        <p>Login with your name + password.</p>
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


# ✅ SLEEK + NO FULL-PAGE SCROLL:
# - compact header
# - modest jug size
# - entry UI in a tidy card
# - tables are "peek" height with internal scroll (ONLY if needed)
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Primo Sales Tracker</title>
  <style>
    :root{
      --bgA:#ecfbff; --bgB:#cfefff;
      --text:#0f172a; --muted:#475569;
      --card:rgba(255,255,255,.92);
      --border:rgba(15,23,42,.10);
      --shadow:0 14px 34px rgba(0,0,0,.12);
      --primary:#2563eb; --danger:#ef4444;
      --focus: rgba(37,99,235,.20);
    }
    *{ box-sizing:border-box; }
    body{
      margin:0;
      padding: 12px;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color:var(--text);
      background: radial-gradient(circle at 18% 12%, #ffffff 0%, var(--bgA) 40%, var(--bgB) 100%);
    }

    /* Centered, not huge */
    .wrap{
      max-width: 1100px;
      margin: 0 auto;
    }

    /* Compact header */
    .topbar{
      display:flex;
      flex-wrap:wrap;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:10px 12px;
      border-radius:16px;
      background: rgba(255,255,255,.92);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }
    .brand{ display:flex; align-items:center; gap:10px; min-width: 220px; }
    .logo{
      width:32px; height:32px; border-radius:12px;
      background: linear-gradient(135deg, var(--primary), #06b6d4);
      box-shadow: 0 10px 16px rgba(37,99,235,.16);
      flex: 0 0 auto;
    }
    .brand h1{ font-size:14px; margin:0; font-weight:950; line-height:1.1; }
    .brand .sub{ margin:2px 0 0; font-size:11px; color: var(--muted); font-weight:850; }

    .topActions{
      display:flex;
      gap:8px;
      align-items:center;
      flex-wrap:wrap;
      justify-content:flex-end;
    }
    .pill{
      display:inline-flex; align-items:center; gap:8px;
      padding:6px 10px; border-radius:999px;
      background: rgba(15,23,42,.06);
      border: 1px solid rgba(15,23,42,.08);
      color: rgba(15,23,42,.82);
      font-size: 11px;
      white-space: nowrap;
      font-weight: 900;
    }
    .logout{
      text-decoration:none;
      font-weight: 950;
      font-size: 12px;
      color: rgba(15,23,42,.72);
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(15,23,42,.10);
      background: rgba(255,255,255,.90);
    }

    /* Main layout: balanced, not giant */
    .grid{
      display:grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-top: 12px;
      align-items:start;
    }
    @media (min-width: 980px){
      .grid{ grid-template-columns: 420px 1fr; }
    }

    .card{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      padding: 12px;
    }

    /* Jug: smaller + clean */
    .jugPanel{
      border-radius: 16px;
      background: rgba(255,255,255,.88);
      border: 1px solid rgba(15,23,42,.08);
      padding: 10px;
    }
    .jugWrap{ display:flex; flex-direction:column; align-items:center; gap:10px; }
    .jugSvg{
      width: min(320px, 100%);
      height:auto;
      user-select:none;
      filter: drop-shadow(0 14px 18px rgba(0,0,0,.16));
    }

    .kpis{
      display:grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap:8px;
      width: 100%;
    }
    .kpi{
      padding: 10px;
      border-radius: 14px;
      background: rgba(255,255,255,.92);
      border: 1px solid rgba(15,23,42,.08);
      box-shadow: 0 10px 16px rgba(0,0,0,.06);
    }
    .kpi .label{ font-size:10px; color: var(--muted); margin-bottom:4px; font-weight:950; text-transform: uppercase; letter-spacing: .06em; }
    .kpi .value{ font-size:18px; font-weight:950; margin:0; }

    .flash{
      width:100%;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(15,23,42,.12);
      background: rgba(255,255,255,.92);
      font-weight: 850;
      font-size: 13px;
    }
    .flash.ok{ border-color: rgba(34,197,94,.25); background: rgba(34,197,94,.10); }
    .flash.bad{ border-color: rgba(239,68,68,.28); background: rgba(239,68,68,.10); }

    /* Right side sections */
    .sectionHead{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding: 10px 10px;
      border-radius: 14px;
      background: rgba(15,23,42,.05);
      border: 1px solid rgba(15,23,42,.08);
      font-weight: 950;
      font-size: 12px;
      color: rgba(15,23,42,.78);
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 10px;
    }

    /* Week select compact row */
    .weekRow{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      margin-bottom: 12px;
      align-items:center;
    }
    .weekRow > select{ flex: 1 1 280px; }

    /* Entry card: vertical but not tall */
    form.controls{
      display:grid;
      grid-template-columns: 1fr;
      gap:10px;
      padding: 10px;
      border-radius: 16px;
      background: rgba(255,255,255,.88);
      border: 1px solid rgba(15,23,42,.08);
      box-shadow: 0 10px 16px rgba(0,0,0,.06);
      margin-bottom: 12px;
    }
    .formGrid{
      display:grid;
      grid-template-columns: 1fr;
      gap:10px;
    }
    @media (min-width: 760px){
      .formGrid{ grid-template-columns: 1fr 1fr; }
      .formGrid .span2{ grid-column: span 2; }
    }

    .btnRow{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:10px;
    }
    .btnRow .span2{ grid-column: span 2; }
    @media (max-width: 420px){
      .btnRow{ grid-template-columns: 1fr; }
      .btnRow .span2{ grid-column: auto; }
    }

    input, select{
      height: 44px;
      padding: 0 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.18);
      outline: none;
      font-size: 14px;
      background: rgba(255,255,255,.98);
      font-weight: 850;
      width: 100%;
      min-width: 0;
    }
    input:focus, select:focus{
      box-shadow: 0 0 0 4px var(--focus);
      border-color: rgba(37,99,235,.55);
    }

    button, a.btn{
      height: 44px;
      padding: 0 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.14);
      background: rgba(255,255,255,.96);
      cursor:pointer;
      font-weight: 950;
      font-size: 14px;
      transition: transform .08s ease;
      text-decoration:none;
      color: inherit;
      display:flex;
      align-items:center;
      justify-content:center;
      gap:8px;
      white-space: nowrap;
      width: 100%;
    }
    button:hover, a.btn:hover{ transform: translateY(-1px); }

    .btn-primary{
      background: linear-gradient(180deg, rgba(37,99,235,.95), rgba(29,78,216,.95));
      color: white;
      border-color: rgba(29,78,216,.25);
      box-shadow: 0 10px 16px rgba(37,99,235,.14);
    }
    .btn-danger{
      background: rgba(239,68,68,.12);
      border-color: rgba(239,68,68,.25);
      color: rgba(127,29,29,.95);
    }
    .btn-ghost{
      background: rgba(15,23,42,.06);
      border-color: rgba(15,23,42,.10);
      color: rgba(15,23,42,.85);
      width:auto;
      padding: 0 14px;
    }

    /* Tables: compact "peek" height so page doesn't scroll */
    .tables{
      display:grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }
    @media (min-width: 980px){
      .tables{ grid-template-columns: 1fr 1fr; }
    }

    .tableCard{
      border-radius: 16px;
      background: rgba(255,255,255,.88);
      border: 1px solid rgba(15,23,42,.08);
      overflow:hidden;
    }
    .tableTitle{
      padding: 10px 12px;
      font-weight: 950;
      font-size: 12px;
      color: rgba(15,23,42,.78);
      background: rgba(15,23,42,.05);
      border-bottom: 1px solid rgba(15,23,42,.08);
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .tableWrap{
      max-height: 220px; /* ✅ prevents full page scroll */
      overflow:auto;
    }
    table{
      width:100%;
      border-collapse: collapse;
      min-width: 420px;
    }
    th, td{
      padding: 10px 10px;
      font-size: 13px;
      text-align:left;
      border-bottom: 1px solid rgba(15,23,42,.08);
      white-space: nowrap;
      vertical-align: top;
      background: rgba(255,255,255,.94);
    }
    th{
      position: sticky;
      top: 0;
      z-index: 1;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: rgba(15,23,42,.65);
      background: rgba(255,255,255,.98);
    }

    footer{
      margin-top: 10px;
      text-align:center;
      color: rgba(15,23,42,.55);
      font-weight: 900;
      font-size: 12px;
      padding: 6px 0 2px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <div class="logo" aria-hidden="true"></div>
        <div>
          <h1>Primo Sales Tracker</h1>
          <div class="sub">Logged in as <b>{{ user_rep }}</b>{% if not admin %} (rep){% endif %}</div>
        </div>
      </div>

      <div class="topActions">
        <div class="pill">Week: <b>{{ range_label }}</b></div>
        <div class="pill">Total: <b>{{ weekly_sales }}</b></div>
        <div class="pill">Goal: <b>{{ goal }}</b></div>
        <a class="logout" href="{{ url_for('logout') }}">Logout</a>
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
                  <stop offset="0.35" stop-color="rgba(255,255,255,0.04)"/>
                  <stop offset="0.60" stop-color="rgba(0,0,0,0.04)"/>
                  <stop offset="1" stop-color="rgba(255,255,255,0.10)"/>
                </linearGradient>

                <path id="primoArc" d="M86 198 C122 190 158 190 194 198" />
                <path id="waterArc" d="M98 220 C128 216 152 216 182 220" />
              </defs>

              <!-- Water -->
              <g clip-path="url(#jugClip)">
                <rect x="0" y="{{ water_y }}" width="280" height="{{ water_h }}" fill="url(#waterGrad)"/>
                {% if fill_percentage > 0 %}
                  <rect x="0" y="{{ water_y }}" width="280" height="22" fill="url(#waterEdge)" opacity="0.8"/>
                  <ellipse cx="140" cy="{{ water_y + 6 }}" rx="150" ry="10" fill="rgba(255,255,255,0.14)" opacity="0.85"/>
                {% endif %}
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
              <div class="kpi">
                <div class="label">Sold</div>
                <p class="value">{{ weekly_sales }}</p>
              </div>
              <div class="kpi">
                <div class="label">Remaining</div>
                <p class="value">{{ remaining }}</p>
              </div>
              <div class="kpi">
                <div class="label">Complete</div>
                <p class="value">{{ fill_percentage | round(0) }}%</p>
              </div>
            </div>

            {% if message %}
              <div class="flash {{ 'ok' if ok else 'bad' }}">{{ message }}</div>
            {% endif %}
          </div>
        </div>
      </div>

      <!-- RIGHT -->
      <div class="card">
        <div class="sectionHead">Sales entry</div>

        <div class="weekRow">
          <form method="GET" action="{{ url_for('index') }}" style="margin:0; display:flex; gap:10px; flex-wrap:wrap; width:100%;">
            <select name="week">
              <option value="{{ selected_week_start }}" selected>Viewing: {{ range_label }}</option>
              <option value="{{ current_week_start }}">Current Week ({{ current_range_label }})</option>
              {% for wk in weeks %}
                {% if wk != selected_week_start and wk != current_week_start %}
                  <option value="{{ wk }}">{{ wk }}</option>
                {% endif %}
              {% endfor %}
            </select>
            <button class="btn-ghost" type="submit" style="flex:0 0 auto;">View</button>
          </form>
        </div>

        <form method="POST" id="salesForm" class="controls" autocomplete="off">
          <input type="hidden" name="week" value="{{ selected_week_start }}">

          <div class="formGrid">
            <div>
              <select name="rep" disabled>
                <option value="{{ user_rep }}" selected>{{ user_rep }}</option>
              </select>
              <input type="hidden" name="rep" value="{{ user_rep }}">
            </div>

            <div>
              <input type="number" id="salesInput" name="sales" placeholder="Quantity" min="1" step="1">
            </div>

            <div class="span2">
              <select name="store_location" required>
                {% for s in stores %}
                  <option value="{{ s }}">{{ s }}</option>
                {% endfor %}
              </select>
            </div>
          </div>

          <div class="btnRow">
            <button type="submit" name="action" value="add" class="btn-primary span2">Add Sale</button>

            {% if admin %}
              <button type="submit" name="action" value="undo">Undo</button>
              <button type="submit" name="action" value="reset" class="btn-danger"
                      onclick="return confirm('Reset this week\\'s total to 0?');">Reset</button>
            {% endif %}

            <a class="btn span2" href="{{ url_for('export_csv', week=selected_week_start) }}">Export CSV</a>
          </div>
        </form>

        <div class="tables">
          <div class="tableCard">
            <div class="tableTitle">Leaderboard</div>
            <div class="tableWrap">
              <table>
                <thead>
                  <tr><th>Rep</th><th>Total</th></tr>
                </thead>
                <tbody>
                  {% if rep_rows %}
                    {% for rep, total in rep_rows %}
                      <tr><td>{{ rep }}</td><td><b>{{ total }}</b></td></tr>
                    {% endfor %}
                  {% else %}
                    <tr><td colspan="2" style="color: rgba(15,23,42,.60); font-weight: 900;">No entries yet</td></tr>
                  {% endif %}
                </tbody>
              </table>
            </div>
          </div>

          <div class="tableCard">
            <div class="tableTitle">Recent activity</div>
            <div class="tableWrap">
              <table>
                <thead>
                  <tr><th>Rep</th><th>Qty</th><th>Store</th><th>Date</th></tr>
                </thead>
                <tbody>
                  {% if recent %}
                    {% for e in recent %}
                      <tr>
                        <td>{{ e.rep }}</td>
                        <td><b>+{{ e.qty }}</b></td>
                        <td style="color: rgba(15,23,42,.62); font-weight: 900;">{{ e.store }}</td>
                        <td style="color: rgba(15,23,42,.62); font-weight: 900;">{{ e.created_at }}</td>
                      </tr>
                    {% endfor %}
                  {% else %}
                    <tr><td colspan="4" style="color: rgba(15,23,42,.60); font-weight: 900;">No entries yet</td></tr>
                  {% endif %}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <footer>{{ version }}</footer>
      </div>
    </div>
  </div>

  <script>
  (function(){
    const form = document.getElementById('salesForm');
    const input = document.getElementById('salesInput');

    form.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      input.required = (btn.value === 'add');
      if (btn.value === 'add') input.focus();
    });

    input.required = false;
  })();
  </script>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    error = None

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if username not in REPS:
            error = "Username must be Tristan, Ricky, or Sohaib."
        else:
            expected = USER_PASSWORDS.get(username, "")
            if password != expected:
                error = "Incorrect password."
            else:
                session["logged_in"] = True
                session["rep"] = username
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

    today = date.today()
    current_wk_start = get_week_start(today)

    requested = parse_week_start(request.args.get("week") or request.form.get("week"))
    selected_wk_start = requested or current_wk_start

    message = request.args.get("msg")
    ok = (request.args.get("ok", "1") == "1")

    user_rep = current_rep()
    admin = is_admin()

    if request.method == "POST":
        action = request.form.get("action", "add")

        if action in ("undo", "reset") and not admin:
            message = "Permission denied."
            ok = False
            return redirect(url_for("index", week=selected_wk_start.isoformat(), msg=message, ok="0"))

        if action == "reset":
            reset_week(selected_wk_start)
            message = "Reset complete. Weekly entries cleared."
            ok = True

        elif action == "undo":
            undone = undo_last_entry(selected_wk_start)
            if undone:
                message = f"Undid last entry: {undone['rep']} -{undone['qty']}."
                ok = True
            else:
                message = "Nothing to undo yet."
                ok = False

        else:  # add
            raw = (request.form.get("sales") or "").strip()
            store_location = (request.form.get("store_location") or "").strip()
            try:
                qty = int(raw)
                if qty <= 0:
                    raise ValueError("qty must be positive")
                add_entry(selected_wk_start, user_rep, qty, store_location)
                message = f"Added {qty} sale(s) for {user_rep}."
                ok = True
            except Exception:
                message = "For Add: enter a valid whole number > 0 and choose a store location."
                ok = False

        return redirect(url_for("index", week=selected_wk_start.isoformat(), msg=message, ok=("1" if ok else "0")))

    weekly_sales = week_total(selected_wk_start)
    fill_percentage = clamp((weekly_sales / WEEKLY_GOAL) * 100 if WEEKLY_GOAL else 0, 0, 100)
    remaining = max(0, WEEKLY_GOAL - weekly_sales)

    # Water fill mapping (fits the jug better at the bottom)
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

    rep_rows = rep_totals(selected_wk_start)
    recent = recent_entries(selected_wk_start, limit=8)  # ✅ smaller to reduce height
    weeks = list_weeks()

    return render_template_string(
        HTML_PAGE,
        user_rep=user_rep,
        admin=admin,
        weekly_sales=weekly_sales,
        goal=WEEKLY_GOAL,
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
        recent=recent,
        version=APP_VERSION,
        stores=STORE_LOCATIONS
    )


@app.route("/export.csv")
def export_csv():
    gate = require_login()
    if gate:
        return gate

    today = date.today()
    current_wk_start = get_week_start(today)
    wk = parse_week_start(request.args.get("week"))
    week_start = wk or current_wk_start

    with db_conn() as conn:
        rows = conn.execute(
            "SELECT week_start, rep, qty, COALESCE(note,'') AS store_location, created_at "
            "FROM sales_entries WHERE week_start = ? ORDER BY id ASC",
            (week_start.isoformat(),)
        ).fetchall()

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["week_start", "rep", "qty", "store_location", "date"])
    for r in rows:
        w.writerow([r["week_start"], r["rep"], r["qty"], r["store_location"], r["created_at"]])

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"primo_sales_{week_start.isoformat()}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

