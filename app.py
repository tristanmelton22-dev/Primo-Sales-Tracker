from flask import Flask, request, render_template_string, redirect, url_for, Response
from datetime import date, timedelta, datetime
import sqlite3
from pathlib import Path
import os
import csv
import io

app = Flask(__name__)

# ---------------- CONFIG ----------------
WEEKLY_GOAL = 100
DB_PATH = Path("sales.db")

# Edit this list to match your team:
REPS = ["Tristan", "Ricky", "Sohaib"]
DEFAULT_REP = REPS[0] if REPS else "Rep"
APP_VERSION = "V.03"
# ---------------------------------------

_db_ready = False


# ---------------- SQLite helpers ----------------
def db_conn():
    if DB_PATH.parent and str(DB_PATH.parent) != ".":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week ON sales_entries(week_start)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_entries_week_rep ON sales_entries(week_start, rep)")
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


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def week_total(week_start: date) -> int:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM sales_entries WHERE week_start = ?",
            (week_start.isoformat(),)
        ).fetchone()
        return int(row["total"] or 0)


def rep_totals(week_start: date):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT rep, COALESCE(SUM(qty), 0) AS total "
            "FROM sales_entries WHERE week_start = ? "
            "GROUP BY rep ORDER BY total DESC, rep ASC",
            (week_start.isoformat(),)
        ).fetchall()
        return [(r["rep"], int(r["total"])) for r in rows]


def recent_entries(week_start: date, limit: int = 10):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, rep, qty, created_at FROM sales_entries "
            "WHERE week_start = ? "
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
            })
        return out


def add_entry(week_start: date, rep: str, qty: int):
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be positive")
    rep = (rep or "").strip() or DEFAULT_REP
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sales_entries (week_start, rep, qty, created_at) VALUES (?, ?, ?, ?)",
            (week_start.isoformat(), rep, qty, now_iso())
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


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Primo Sales Tracker</title>
  <style>
    :root{
      --bg1:#ecfbff;
      --bg2:#cfefff;
      --text:#0f172a;
      --muted:#475569;
      --card:rgba(255,255,255,.86);
      --border:rgba(255,255,255,.72);
      --shadow:0 16px 40px rgba(0,0,0,.14);
      --primary:#2563eb;
      --danger:#ef4444;
    }
    *{ box-sizing:border-box; }

    body{
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color:var(--text);
      margin:0;
      padding: 12px;
      background: radial-gradient(circle at 18% 12%, #f7feff 0%, var(--bg1) 38%, var(--bg2) 100%);
    }

    .wrap{ max-width: 1100px; margin: 0 auto; }

    .topbar{
      display:flex;
      flex-wrap:wrap;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:12px 14px;
      border-radius:16px;
      background: rgba(255,255,255,.85);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }

    .brand{ display:flex; align-items:center; gap:10px; min-width: 220px; }
    .logo{
      width:34px; height:34px; border-radius:12px;
      background: linear-gradient(135deg, var(--primary) 0%, #06b6d4 100%);
      box-shadow: 0 10px 18px rgba(37,99,235,.18);
      position:relative; flex: 0 0 auto;
    }
    .logo:before{ content:""; position:absolute; inset:8px; border-radius:10px; background: rgba(255,255,255,.22); }
    .brand h1{ font-size:15px; margin:0; font-weight:950; line-height:1.1; }
    .brand .sub{ margin:2px 0 0; font-size:11px; color: var(--muted); font-weight:700; }

    .meta{ display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; align-items:center; }
    .pill{
      display:inline-flex; align-items:center; gap:8px;
      padding:7px 10px; border-radius:999px;
      background: rgba(15,23,42,.06);
      border: 1px solid rgba(15,23,42,.08);
      color: rgba(15,23,42,.82);
      font-size: 11px;
      white-space: nowrap;
    }
    .pill b{ font-weight: 950; }

    .grid{
      display:grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-top: 12px;
      align-items:start;
    }
    @media (min-width: 950px){
      .grid{ grid-template-columns: 440px 1fr; }
    }

    .card{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      padding: 14px;
    }

    .kpis{ display:grid; grid-template-columns: 1fr; gap:10px; }
    @media (min-width: 650px){ .kpis{ grid-template-columns: 1fr 1fr 1fr; } }

    .kpi{
      padding: 12px;
      border-radius: 16px;
      background: rgba(255,255,255,.82);
      border: 1px solid rgba(255,255,255,.78);
      box-shadow: 0 10px 18px rgba(0,0,0,.08);
      text-align:left;
      min-width: 0;
    }
    .kpi .label{ font-size:11px; color: var(--muted); margin-bottom:6px; font-weight:900; }
    .kpi .value{ font-size:20px; font-weight:950; margin:0; }
    .kpi .hint{ margin:6px 0 0; font-size:11px; color: rgba(15,23,42,.70); font-weight:650; }

    .bar{
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: rgba(15,23,42,.10);
      overflow:hidden;
      box-shadow: inset 0 1px 2px rgba(0,0,0,.10);
      margin: 10px 0 0;
    }
    .bar > div{
      height:100%;
      width: {{ fill_percentage }}%;
      background: linear-gradient(90deg, rgba(56,189,248,.95), rgba(2,132,199,.95));
      transition: width 650ms cubic-bezier(.2,.9,.2,1);
    }

    .jugWrap{ display:flex; flex-direction:column; align-items:center; gap:8px; }
    .jugSvg{
      width: min(360px, 100%);
      height: auto;
      filter: drop-shadow(0 12px 16px rgba(0,0,0,.16));
      user-select:none;
    }
    @media (max-width: 480px){
      .jugSvg{ width: min(320px, 100%); }
    }

    form{
      margin-top: 12px;
      display:flex;
      flex-wrap:wrap;
      gap:10px;
      align-items:center;
    }

    input, select{
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.18);
      outline: none;
      font-size: 14px;
      background: rgba(255,255,255,.98);
      font-weight: 750;
      min-width: 0;
    }
    input{ width: 160px; }
    select{ width: 210px; }

    input:focus, select:focus{
      box-shadow: 0 0 0 4px rgba(37,99,235,.18);
      border-color: rgba(37,99,235,.55);
    }

    button, a.btn{
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.14);
      background: rgba(255,255,255,.96);
      cursor:pointer;
      font-weight: 950;
      font-size: 14px;
      transition: transform .08s ease;
      text-decoration:none;
      color: inherit;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:8px;
      min-width: 92px;
    }
    button:hover, a.btn:hover{ transform: translateY(-1px); }

    .btn-primary{
      background: linear-gradient(180deg, rgba(37,99,235,.95), rgba(29,78,216,.95));
      color: white;
      border-color: rgba(29,78,216,.25);
      box-shadow: 0 10px 18px rgba(37,99,235,.16);
    }
    .btn-danger{
      background: rgba(239,68,68,.12);
      border-color: rgba(239,68,68,.25);
      color: rgba(127,29,29,.95);
    }

    /* Mobile form: stack cleanly and go full width */
    @media (max-width: 640px){
      form{
        display:grid;
        grid-template-columns: 1fr 1fr;
        gap:10px;
        align-items:stretch;
      }
      select{ width: 100%; grid-column: 1 / -1; }
      input{ width: 100%; grid-column: 1 / -1; }
      button, a.btn{
        width: 100%;
        min-width: 0;
      }
      .btn-primary{ grid-column: 1 / -1; }
    }

    .flash{
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.12);
      background: rgba(255,255,255,.82);
      display: inline-block;
      max-width: 720px;
      text-align:left;
      font-weight: 750;
    }
    .flash.ok{ border-color: rgba(34,197,94,.25); background: rgba(34,197,94,.10); }
    .flash.bad{ border-color: rgba(239,68,68,.28); background: rgba(239,68,68,.10); }

    .split{
      display:grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-top: 12px;
    }
    @media (min-width: 650px){
      .split{ grid-template-columns: 1fr 1fr; }
    }

    .muted{ color: rgba(15,23,42,.62); font-weight: 700; }

    .tableWrap{
      width: 100%;
      overflow-x: auto;
      border-radius: 14px;
      border: 1px solid rgba(15,23,42,.10);
      background: rgba(255,255,255,.72);
    }
    .table{
      width:100%;
      border-collapse: collapse;
      min-width: 360px; /* keeps columns readable on small screens */
    }
    .table th, .table td{
      padding: 10px 10px;
      font-size: 13px;
      text-align:left;
      border-bottom: 1px solid rgba(15,23,42,.08);
      white-space: nowrap;
    }
    .table th{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: rgba(15,23,42,.65);
    }

    footer{
      margin-top: 14px;
      text-align:center;
      color: rgba(15,23,42,.50);
      font-weight: 800;
      font-size: 12px;
      padding: 8px 0 2px;
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
          <div class="sub">Internal dashboard • Weekly bottle goal tracking</div>
        </div>
      </div>

      <div class="meta">
        <div class="pill">Week: <b>{{ range_label }}</b></div>
        <div class="pill">Total: <b>{{ weekly_sales }}</b></div>
        <div class="pill">Goal: <b>{{ goal }}</b></div>
      </div>
    </div>

    <div class="grid">
      <!-- LEFT: Jug -->
      <div class="card">
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
                <stop offset="0.28" stop-color="rgba(255,255,255,0.10)" />
                <stop offset="0.55" stop-color="rgba(0,0,0,0.07)" />
                <stop offset="1" stop-color="rgba(255,255,255,0.20)" />
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
                <stop offset="0" stop-color="rgba(255,255,255,0.10)"/>
                <stop offset="0.35" stop-color="rgba(255,255,255,0.03)"/>
                <stop offset="0.60" stop-color="rgba(0,0,0,0.03)"/>
                <stop offset="1" stop-color="rgba(255,255,255,0.08)"/>
              </linearGradient>

              <path id="primoArc" d="M86 198 C122 190 158 190 194 198" />
              <path id="waterArc" d="M98 220 C128 216 152 216 182 220" />

              <filter id="textUnderPlastic" x="-20%" y="-20%" width="140%" height="140%">
                <feGaussianBlur in="SourceGraphic" stdDeviation="0.12" result="soft"/>
                <feMerge>
                  <feMergeNode in="soft"/>
                  <feMergeNode in="SourceGraphic"/>
                </feMerge>
              </filter>
            </defs>

            <g clip-path="url(#jugClip)">
              <rect x="0" y="{{ water_y }}" width="280" height="{{ water_h }}" fill="url(#waterGrad)"/>
              <rect x="0" y="{{ water_y }}" width="280" height="24" fill="url(#waterEdge)" opacity="0.8"/>
              {% if fill_percentage > 0 %}
              <ellipse cx="140" cy="{{ water_y + 6 }}" rx="150" ry="11" fill="rgba(255,255,255,0.14)" opacity="0.85"/>
              {% endif %}
            </g>

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
            " fill="url(#plastic)" stroke="rgba(255,255,255,0.20)" stroke-width="2"/>

            <g clip-path="url(#jugClip)">
              <rect x="0" y="0" width="280" height="420" fill="url(#sheen)" opacity="0.60"/>
            </g>

            <path d="
              M118 52
              C118 46 162 46 162 52
              L162 64
              C162 82 180 86 194 96
              C204 104 210 116 210 130
              C210 144 208 154 206 170
              C204 186 210 206 214 226
              C218 248 218 272 214 290
              C210 310 212 328 214 344
              C216 356 210 362 196 368
              C174 376 106 376 84 368
              C70 362 64 356 66 344
              C68 328 70 310 66 290
              C62 272 62 248 66 226
              C70 206 76 186 74 170
              C72 154 70 144 70 130
              C70 116 76 104 86 96
              C100 86 118 82 118 64
              Z
            " fill="none" stroke="rgba(15,23,42,0.10)" stroke-width="2"/>

            <path d="M80 150 C114 142 166 142 200 150" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="11" opacity="0.35"/>
            <path d="M78 154 C114 146 166 146 202 154" fill="none" stroke="rgba(0,0,0,0.04)" stroke-width="3" opacity="0.33"/>
            <path d="M80 232 C114 228 166 228 202 232" fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="10" opacity="0.28"/>
            <path d="M78 236 C114 232 166 232 204 236" fill="none" stroke="rgba(0,0,0,0.035)" stroke-width="3" opacity="0.30"/>
            <path d="M88 338 C116 336 164 336 192 338" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="12" opacity="0.38"/>
            <path d="M86 344 C116 342 164 342 194 344" fill="none" stroke="rgba(0,0,0,0.04)" stroke-width="4" opacity="0.36"/>

            <g opacity="0.86" filter="url(#textUnderPlastic)">
              <circle cx="96" cy="210" r="11" fill="none" stroke="rgba(37,99,235,0.38)" stroke-width="3" opacity="0.78"/>
              <text font-size="21" font-weight="900"
                    fill="rgba(15,23,42,0.60)"
                    stroke="rgba(255,255,255,0.10)" stroke-width="0.6"
                    style="letter-spacing:1px;">
                <textPath href="#primoArc" startOffset="50%" text-anchor="middle">PRIMO</textPath>
              </text>
              <text font-size="13" font-weight="850"
                    fill="rgba(15,23,42,0.50)"
                    stroke="rgba(255,255,255,0.09)" stroke-width="0.5"
                    style="letter-spacing:0.8px;">
                <textPath href="#waterArc" startOffset="50%" text-anchor="middle">WATER</textPath>
              </text>
              <path d="M86 206 C120 196 160 196 194 206" fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="2" opacity="0.55"/>
            </g>

            <rect x="96" y="86" width="10" height="290" rx="5" fill="rgba(255,255,255,0.12)" opacity="0.52"/>
            <rect x="112" y="84" width="5" height="300" rx="2.5" fill="rgba(255,255,255,0.08)" opacity="0.52"/>
            <rect x="174" y="94" width="7" height="276" rx="3.5" fill="rgba(255,255,255,0.07)" opacity="0.52"/>

            <g>
              <rect x="106" y="8" width="68" height="36" rx="12" fill="url(#capBlue)" stroke="rgba(0,0,0,0.12)" />
              <rect x="100" y="5" width="80" height="14" rx="7" fill="rgba(96,165,250,0.95)" stroke="rgba(0,0,0,0.10)"/>
              <rect x="110" y="40" width="60" height="5" rx="2.5" fill="rgba(0,0,0,0.12)" opacity="0.32"/>
              <rect x="114" y="12" width="12" height="30" rx="6" fill="rgba(255,255,255,0.18)" opacity="0.85"/>
            </g>

            <rect x="116" y="62" width="48" height="5" rx="2.5" fill="rgba(255,255,255,0.12)" opacity="0.55"/>
          </svg>

          <div class="bar" aria-hidden="true"><div></div></div>

          {% if message %}
            <div class="flash {{ 'ok' if ok else 'bad' }}">{{ message }}</div>
          {% endif %}
        </div>
      </div>

      <!-- RIGHT: KPIs + Controls + Reports -->
      <div class="card">
        <div class="kpis">
          <div class="kpi">
            <div class="label">Bottles Sold</div>
            <p class="value">{{ weekly_sales }}</p>
            <p class="hint">Total logged this week</p>
          </div>
          <div class="kpi">
            <div class="label">Remaining</div>
            <p class="value">{{ remaining }}</p>
            <p class="hint">To reach the weekly goal</p>
          </div>
          <div class="kpi">
            <div class="label">Completion</div>
            <p class="value">{{ fill_percentage | round(1) }}%</p>
            <p class="hint">Fill is capped at 100% visually</p>
          </div>
        </div>

        <form method="POST" id="salesForm" autocomplete="off">
          <select name="rep" id="repSelect">
            {% for r in reps %}
              <option value="{{ r }}" {% if r == selected_rep %}selected{% endif %}>{{ r }}</option>
            {% endfor %}
          </select>

          <input type="number" id="salesInput" name="sales" placeholder="Add sales" min="1" step="1">

          <button type="submit" name="action" value="add" class="btn-primary">Add</button>
          <button type="submit" name="action" value="undo">Undo</button>
          <button type="submit" name="action" value="reset" class="btn-danger"
                  onclick="return confirm('Reset this week\\'s total to 0?');">Reset</button>

          <a class="btn" href="{{ url_for('export_csv') }}">Export CSV</a>
        </form>

        <div class="split">
          <div>
            <div class="muted" style="margin:4px 0 8px;">Leaderboard</div>
            <div class="tableWrap">
              <table class="table">
                <thead>
                  <tr><th>Rep</th><th>Total</th></tr>
                </thead>
                <tbody>
                  {% if rep_rows %}
                    {% for rep, total in rep_rows %}
                      <tr><td>{{ rep }}</td><td><b>{{ total }}</b></td></tr>
                    {% endfor %}
                  {% else %}
                    <tr><td colspan="2" class="muted">No entries yet</td></tr>
                  {% endif %}
                </tbody>
              </table>
            </div>
          </div>

          <div>
            <div class="muted" style="margin:4px 0 8px;">Recent Activity</div>
            <div class="tableWrap">
              <table class="table">
                <thead>
                  <tr><th>Rep</th><th>Qty</th><th>Time</th></tr>
                </thead>
                <tbody>
                  {% if recent %}
                    {% for e in recent %}
                      <tr>
                        <td>{{ e.rep }}</td>
                        <td><b>+{{ e.qty }}</b></td>
                        <td class="muted">{{ e.created_at }}</td>
                      </tr>
                    {% endfor %}
                  {% else %}
                    <tr><td colspan="3" class="muted">No entries yet</td></tr>
                  {% endif %}
                </tbody>
              </table>
            </div>
          </div>
        </div>

      </div>
    </div>

    <footer>{{ version }}</footer>
  </div>

  <script>
  (function(){
    const form = document.getElementById('salesForm');
    const input = document.getElementById('salesInput');

    // Only require the number for Add (Undo/Reset/Export don't need it)
    form.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      input.required = (btn.value === 'add');
      if (btn.value === 'add') input.focus();
    });

    // Default: not required until they click Add
    input.required = false;
  })();
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    today = date.today()
    wk_start = get_week_start(today)

    message = None
    ok = True

    if request.method == "POST":
        action = request.form.get("action", "add")
        selected_rep = (request.form.get("rep") or DEFAULT_REP).strip() or DEFAULT_REP

        if action == "reset":
            reset_week(wk_start)
            message = "Reset complete. Weekly entries cleared."
            ok = True

        elif action == "undo":
            undone = undo_last_entry(wk_start)
            if undone:
                message = f"Undid last entry: {undone['rep']} -{undone['qty']}."
                ok = True
            else:
                message = "Nothing to undo yet."
                ok = False

        else:  # add
            raw = (request.form.get("sales") or "").strip()
            try:
                qty = int(raw)
                if qty <= 0:
                    raise ValueError("qty must be positive")
                add_entry(wk_start, selected_rep, qty)
                message = f"Added {qty} sale(s) for {selected_rep}."
                ok = True
            except Exception:
                message = "For Add: enter a valid whole number greater than 0."
                ok = False

        return redirect(url_for("index", msg=message, ok=("1" if ok else "0"), rep=selected_rep))

    weekly_sales = week_total(wk_start)
    fill_percentage = clamp((weekly_sales / WEEKLY_GOAL) * 100 if WEEKLY_GOAL else 0, 0, 100)
    remaining = max(0, WEEKLY_GOAL - weekly_sales)

    top_y = 46
    bottom_y = 380
    usable_h = bottom_y - top_y
    water_h = int((fill_percentage / 100.0) * usable_h)
    water_y = bottom_y - water_h

    selected_rep = request.args.get("rep") or DEFAULT_REP
    message = request.args.get("msg")
    ok = (request.args.get("ok", "1") == "1")

    rep_rows = rep_totals(wk_start)
    recent = recent_entries(wk_start, limit=10)

    return render_template_string(
        HTML_PAGE,
        weekly_sales=weekly_sales,
        goal=WEEKLY_GOAL,
        fill_percentage=fill_percentage,
        remaining=remaining,
        water_h=water_h,
        water_y=water_y,
        range_label=week_label(wk_start),
        message=message,
        ok=ok,
        reps=REPS,
        selected_rep=selected_rep,
        rep_rows=rep_rows,
        recent=recent,
        version=APP_VERSION,
    )


@app.route("/export.csv")
def export_csv():
    today = date.today()
    wk_start = get_week_start(today)

    with db_conn() as conn:
        rows = conn.execute(
            "SELECT week_start, rep, qty, created_at FROM sales_entries "
            "WHERE week_start = ? ORDER BY id ASC",
            (wk_start.isoformat(),)
        ).fetchall()

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["week_start", "rep", "qty", "created_at"])
    for r in rows:
        w.writerow([r["week_start"], r["rep"], r["qty"], r["created_at"]])

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"primo_sales_{wk_start.isoformat()}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
