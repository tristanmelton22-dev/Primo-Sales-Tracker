from flask import Flask, request, render_template_string
from datetime import date, timedelta
import sqlite3
import json
from pathlib import Path

app = Flask(__name__)

# ---------------- CONFIG ----------------
WEEKLY_GOAL = 100
DB_PATH = Path("sales.db")
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
            CREATE TABLE IF NOT EXISTS weekly_sales (
                week_start TEXT PRIMARY KEY,
                total INTEGER NOT NULL DEFAULT 0,
                history_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.commit()


def get_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday start


def week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)

    def fmt(x: date):
        return f"{x.month}/{x.day}/{str(x.year)[-2:]}"
    return f"{fmt(week_start)}–{fmt(week_end)}"


def load_week(week_start: date):
    init_db()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT total, history_json FROM weekly_sales WHERE week_start = ?",
            (week_start.isoformat(),)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO weekly_sales (week_start, total, history_json) VALUES (?, 0, '[]')",
                (week_start.isoformat(),)
            )
            conn.commit()
            return 0, []

        total = int(row["total"])
        try:
            history = json.loads(row["history_json"] or "[]")
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

        cleaned = []
        for x in history:
            try:
                cleaned.append(int(x))
            except Exception:
                pass

        return total, cleaned


def save_week(week_start: date, total: int, history: list[int]):
    init_db()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO weekly_sales (week_start, total, history_json) VALUES (?, ?, ?) "
            "ON CONFLICT(week_start) DO UPDATE SET total=excluded.total, history_json=excluded.history_json",
            (week_start.isoformat(), int(total), json.dumps(history))
        )
        conn.commit()


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


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
      --card:rgba(255,255,255,.84);
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
      padding:12px 12px 16px;
      background: radial-gradient(circle at 18% 12%, #f7feff 0%, var(--bg1) 38%, var(--bg2) 100%);
    }

    .wrap{ max-width: 1100px; margin: 0 auto; }

    .topbar{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      padding:12px 14px;
      border-radius:16px;
      background: rgba(255,255,255,.82);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }
    .brand{ display:flex; align-items:center; gap:10px; min-width: 210px; }
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
      padding:7px 9px; border-radius:999px;
      background: rgba(15,23,42,.06);
      border: 1px solid rgba(15,23,42,.08);
      color: rgba(15,23,42,.82);
      font-size: 11px; white-space: nowrap;
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

    .kpis{ display:grid; grid-template-columns:1fr; gap:10px; }
    @media (min-width: 650px){ .kpis{ grid-template-columns: 1fr 1fr 1fr; } }

    .kpi{
      padding: 12px;
      border-radius: 16px;
      background: rgba(255,255,255,.78);
      border: 1px solid rgba(255,255,255,.78);
      box-shadow: 0 10px 18px rgba(0,0,0,.08);
      text-align:left;
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

    form{
      margin-top: 12px;
      display:flex;
      flex-wrap:wrap;
      gap:10px;
      align-items:center;
    }
    input{
      padding: 10px 12px;
      width: 180px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.18);
      outline: none;
      font-size: 14px;
      background: rgba(255,255,255,.96);
      font-weight: 700;
    }
    input:focus{
      box-shadow: 0 0 0 4px rgba(37,99,235,.18);
      border-color: rgba(37,99,235,.55);
    }
    button{
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(15,23,42,.14);
      background: rgba(255,255,255,.96);
      cursor:pointer;
      font-weight: 950;
      font-size: 14px;
      transition: transform .08s ease;
    }
    button:hover{ transform: translateY(-1px); }
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

    .jugWrap{ display:flex; flex-direction:column; align-items:center; gap:8px; }

    .jugSvg{
      width: min(370px, 100%);
      height: auto;
      filter: drop-shadow(0 12px 16px rgba(0,0,0,.16));
      user-select:none;
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
        <div class="pill">Today: <b>{{ today }}</b></div>
        <div class="pill">Goal: <b>{{ goal }}</b></div>
      </div>
    </div>

    <div class="grid">
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

              <!-- Plastic sheen overlay -->
              <linearGradient id="sheen" x1="0" x2="1">
                <stop offset="0" stop-color="rgba(255,255,255,0.10)"/>
                <stop offset="0.35" stop-color="rgba(255,255,255,0.03)"/>
                <stop offset="0.60" stop-color="rgba(0,0,0,0.03)"/>
                <stop offset="1" stop-color="rgba(255,255,255,0.08)"/>
              </linearGradient>

              <!-- Text paths -->
              <path id="primoArc" d="M86 198 C122 190 158 190 194 198" />
              <path id="waterArc" d="M98 220 C128 216 152 216 182 220" />

              <!-- Very soft text filter so it feels under plastic -->
              <filter id="textUnderPlastic" x="-20%" y="-20%" width="140%" height="140%">
                <feGaussianBlur in="SourceGraphic" stdDeviation="0.12" result="soft"/>
                <feMerge>
                  <feMergeNode in="soft"/>
                  <feMergeNode in="SourceGraphic"/>
                </feMerge>
              </filter>
            </defs>

            <!-- WATER -->
            <g clip-path="url(#jugClip)">
              <rect x="0" y="{{ water_y }}" width="280" height="{{ water_h }}" fill="url(#waterGrad)"/>
              <rect x="0" y="{{ water_y }}" width="280" height="24" fill="url(#waterEdge)" opacity="0.8"/>
              {% if fill_percentage > 0 %}
              <ellipse cx="140" cy="{{ water_y + 6 }}" rx="150" ry="11" fill="rgba(255,255,255,0.14)" opacity="0.85"/>
              {% endif %}
            </g>

            <!-- JUG BODY -->
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

            <!-- Plastic sheen overlay across jug (helps blend label on empty + filled) -->
            <g clip-path="url(#jugClip)">
              <rect x="0" y="0" width="280" height="420" fill="url(#sheen)" opacity="0.60"/>
            </g>

            <!-- Inner thickness -->
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

            <!-- Subtle ridges -->
            <path d="M80 150 C114 142 166 142 200 150" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="11" opacity="0.35"/>
            <path d="M78 154 C114 146 166 146 202 154" fill="none" stroke="rgba(0,0,0,0.04)" stroke-width="3" opacity="0.33"/>
            <path d="M80 232 C114 228 166 228 202 232" fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="10" opacity="0.28"/>
            <path d="M78 236 C114 232 166 232 204 236" fill="none" stroke="rgba(0,0,0,0.035)" stroke-width="3" opacity="0.30"/>
            <path d="M88 338 C116 336 164 336 192 338" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="12" opacity="0.38"/>
            <path d="M86 344 C116 342 164 342 194 344" fill="none" stroke="rgba(0,0,0,0.04)" stroke-width="4" opacity="0.36"/>

            <!-- PRIMO/WATER ONLY (no band behind it) -->
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
              <!-- tiny highlight line to imply printing under plastic -->
              <path d="M86 206 C120 196 160 196 194 206" fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="2" opacity="0.55"/>
            </g>

            <!-- Reflections -->
            <rect x="96" y="86" width="10" height="290" rx="5" fill="rgba(255,255,255,0.12)" opacity="0.52"/>
            <rect x="112" y="84" width="5" height="300" rx="2.5" fill="rgba(255,255,255,0.08)" opacity="0.52"/>
            <rect x="174" y="94" width="7" height="276" rx="3.5" fill="rgba(255,255,255,0.07)" opacity="0.52"/>

            <!-- BLUE CAP -->
            <g>
              <rect x="106" y="8" width="68" height="36" rx="12" fill="url(#capBlue)" stroke="rgba(0,0,0,0.12)" />
              <rect x="100" y="5" width="80" height="14" rx="7" fill="rgba(96,165,250,0.95)" stroke="rgba(0,0,0,0.10)"/>
              <rect x="110" y="40" width="60" height="5" rx="2.5" fill="rgba(0,0,0,0.12)" opacity="0.32"/>
              <rect x="114" y="12" width="12" height="30" rx="6" fill="rgba(255,255,255,0.18)" opacity="0.85"/>
            </g>

            <!-- Neck ring -->
            <rect x="116" y="62" width="48" height="5" rx="2.5" fill="rgba(255,255,255,0.12)" opacity="0.55"/>
          </svg>

          <div class="bar" aria-hidden="true"><div></div></div>

          {% if message %}
            <div class="flash {{ 'ok' if ok else 'bad' }}">{{ message }}</div>
          {% endif %}
        </div>
      </div>

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
          <input type="number" id="salesInput" name="sales" placeholder="Add sales" min="0" step="1">
          <button type="submit" name="action" value="add" class="btn-primary">Add</button>
          <button type="submit" name="action" value="undo">Undo</button>
          <button type="submit" name="action" value="reset" class="btn-danger"
                  onclick="return confirm('Reset this week\\'s total to 0?');">Reset</button>
        </form>
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
  })();
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    today = date.today()
    wk_start = get_week_start(today)
    weekly_sales, history = load_week(wk_start)

    message = None
    ok = True

    if request.method == "POST":
        action = request.form.get("action", "add")

        if action == "reset":
            weekly_sales = 0
            history = []
            message = "Reset complete. Weekly sales cleared."
            ok = True

        elif action == "undo":
            if history:
                last = int(history.pop())
                weekly_sales = max(0, int(weekly_sales) - last)
                message = f"Undid last add: -{last}."
                ok = True
            else:
                message = "Nothing to undo yet."
                ok = False

        else:  # add
            raw = (request.form.get("sales") or "").strip()
            try:
                if raw == "":
                    raise ValueError("empty")
                added = int(raw)
                if added < 0:
                    raise ValueError("negative")
                if added == 0:
                    message = "Added 0. No changes made."
                    ok = False
                else:
                    weekly_sales = int(weekly_sales) + added
                    history.append(added)
                    message = f"Added {added} sale(s)."
                    ok = True
            except Exception:
                message = "For Add: enter a valid non-negative whole number."
                ok = False

        save_week(wk_start, int(weekly_sales), history)

    fill_percentage = clamp((weekly_sales / WEEKLY_GOAL) * 100 if WEEKLY_GOAL else 0, 0, 100)
    remaining = max(0, WEEKLY_GOAL - weekly_sales)

    # Water fills the whole interior INCLUDING the thick short neck.
    top_y = 46
    bottom_y = 380
    usable_h = bottom_y - top_y
    water_h = int((fill_percentage / 100.0) * usable_h)
    water_y = bottom_y - water_h

    return render_template_string(
        HTML_PAGE,
        weekly_sales=weekly_sales,
        goal=WEEKLY_GOAL,
        fill_percentage=fill_percentage,
        remaining=remaining,
        message=message,
        ok=ok,
        today=today.isoformat(),
        range_label=week_label(wk_start),
        water_h=water_h,
        water_y=water_y,
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000)
