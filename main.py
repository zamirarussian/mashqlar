import os
import threading
import logging
import json
import hmac
from functools import wraps
from datetime import datetime
import asyncio

from flask import (
    Flask, send_from_directory, jsonify, request, abort,
    session, redirect, url_for
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import psycopg2
from psycopg2.extras import RealDictCursor
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "zamira2024")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "zamira2024")
SESSION_SECRET = os.environ.get("SESSION_SECRET", ADMIN_SECRET + "_session")
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

DEFAULT_REMINDER_TEXT = (
    "📚 Bugungi darsni o'tdingizmi?\n\n"
    "Har kuni 10 daqiqa — va ruscha gaplashishga yaqinlashasiz!"
)

# --- R2 (Cloudflare object storage) ---
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

AUDIO_SLOTS = {"audirov": "Audirovaniye", "shadowing": "Shadowing", "taqlid": "Taqlid"}
AUDIO_CTYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
                ".oga": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac"}

# --- Interfeys matnlari (default qiymatlar) ---
DEFAULT_UI = {
    "ui_welcome_title": "Rus tili",
    "ui_welcome_sub": "har kuni",
    "ui_welcome_desc": "Har kuni 30 daqiqa shug'ullanib 1 oyda begona odam bilan ruscha gapirib boshlang",
    "ui_course_label": "A1 — A2",
    "ui_course_desc": "Bilaman lekin gapira olmayman",
    "label_vocab": "Lug'at",
    "label_audio": "Audio mashq",
    "label_speaking": "Gapirish",
    "label_grammar": "Grammatika",
    "label_text": "Matn",
    "label_formula": "Nutq formulalari",
}

def r2_configured():
    return all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL])

_r2_client = None
def get_r2_client():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _r2_client

def upload_audio_to_r2(file_storage, level, day, slot):
    import time
    ext = os.path.splitext(file_storage.filename or "")[1].lower() or ".mp3"
    ctype = AUDIO_CTYPES.get(ext, "application/octet-stream")
    key = f"audio/{level}/{day}/{slot}{ext}"
    data = file_storage.read()
    get_r2_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data,
                               ContentType=ctype, CacheControl="public, max-age=31536000")
    return f"{R2_PUBLIC_URL}/{key}?v={int(time.time())}"

# --- AI (Claude API) bilan dars to'ldirish ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-6")

def ai_configured():
    return bool(ANTHROPIC_API_KEY)

AI_SYSTEM = (
    "Sen rus tili kursi uchun dars kontentini tayyorlovchi yordamchisan. "
    "Foydalanuvchi bergan PDF matnidan dars mazmunini ajratib, QAT'IY faqat JSON qaytar. "
    "Hech qanday izoh, markdown belgisi yoki ortiqcha matn YO'Q. uz maydonlari — o'zbekcha tarjima. "
    "So'zlar va matnlarni PDF dan ol. Lekin lug'at uchun urg'u (stress, masalan приве́т), "
    "sinonim (sin), antonim (ant) va fe'l aspektlarini (НСВ/СВ) o'zing rus tili bilimingga tayanib to'ldir. "
    "Agar PDF da misol bo'lmasa, sodda tabiiy misollar (ex1/ex2) o'zing yoz. "
    "sin/ant/nsv/sv mos kelmasa bo'sh qoldir. Bo'lim umuman bo'lmasa bo'sh ro'yxat [] qoldir. JSON sxemasi:\n"
    '{"title":"dars nomi","shadowing_ru":"","shadowing_uz":"","razgovor_start":"",'
    '"vocab":[{"ru":"","stress":"","uz":"","ex1ru":"","ex1uz":"","ex2ru":"","ex2uz":"","sin":"","ant":"","nsv":"","sv":""}],'
    '"formulas":[{"ru":"","uz":""}],'
    '"reading_texts":[{"level":"","ru":"","uz":""}],'
    '"grammar":[{"title":"","sub":"","base":"","res":"","example":""}],'
    '"speaking_questions":[{"title":"","desc":"","format":""}],'
    '"audio_questions":[{"q":"","options":["",""],"correct":"1"}]}'
)

def extract_pdf_text(file_storage):
    import io
    from pypdf import PdfReader
    data = file_storage.read()
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def ai_generate_lesson(pdf_text, level, day):
    import anthropic, re
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=AI_MODEL, max_tokens=8000, system=AI_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Daraja: {level}, {day}-kun. Quyidagi matndan darsni tayyorla:\n\n{pdf_text[:60000]}"}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text).rsplit("```", 1)[0].strip()
    a = text.find("{"); z = text.rfind("}")
    if a >= 0 and z > a:
        text = text[a:z + 1]
    return json.loads(text)

# --- DATABASE ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT, username TEXT,
            level TEXT DEFAULT NULL, current_day INTEGER DEFAULT 1,
            joined_at TIMESTAMP DEFAULT NOW(), last_active TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS content (
            id SERIAL PRIMARY KEY,
            level TEXT NOT NULL, day INTEGER NOT NULL,
            shadowing_ru TEXT NOT NULL DEFAULT '', shadowing_uz TEXT NOT NULL DEFAULT '',
            vocab JSONB NOT NULL DEFAULT '[]', razgovor_start TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(), UNIQUE(level, day)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audios (
            level TEXT NOT NULL, day INTEGER NOT NULL, slot TEXT NOT NULL,
            url TEXT NOT NULL, updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (level, day, slot)
        );
    """)
    # Yangi kontent ustunlari (mavjud bazaga ham xavfsiz qo'shiladi)
    for col in ["title TEXT DEFAULT ''", "formulas JSONB DEFAULT '[]'",
                "grammar JSONB DEFAULT '[]'", "reading_texts JSONB DEFAULT '[]'",
                "audio_questions JSONB DEFAULT '[]'", "speaking_questions JSONB DEFAULT '[]'"]:
        cur.execute(f"ALTER TABLE content ADD COLUMN IF NOT EXISTS {col};")
    cur.execute("INSERT INTO settings (key, value) VALUES ('reminder_hour','9') ON CONFLICT (key) DO NOTHING;")
    cur.execute("INSERT INTO settings (key, value) VALUES ('reminder_text',%s) ON CONFLICT (key) DO NOTHING;",
                (DEFAULT_REMINDER_TEXT,))
    cur.execute("""
        INSERT INTO content (level, day, title, shadowing_ru, shadowing_uz, vocab, razgovor_start)
        VALUES ('A1', 1, 'Tanishuv',
            '— Как дела? — Нормально, спасибо!', '— Ishlar qanday? — Yaxshi, rahmat!',
            '[{"ru":"привет","uz":"salom","ex":"Привет, как дела?"},{"ru":"спасибо","uz":"rahmat","ex":"Спасибо большое!"}]',
            'Привет! Как тебя зовут?')
        ON CONFLICT DO NOTHING;
    """)
    conn.commit(); cur.close(); conn.close()
    logger.info("Database tayyor")

def save_user(user_id, first_name, username):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, first_name, username) VALUES (%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET last_active=NOW(), first_name=EXCLUDED.first_name;""",
        (user_id, first_name, username))
    conn.commit(); cur.close(); conn.close()

def set_user_level(user_id, level):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET level=%s WHERE user_id=%s", (level, user_id))
    conn.commit(); cur.close(); conn.close()

def get_all_users():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users ORDER BY joined_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def get_stats():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM users"); total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as active FROM users WHERE last_active > NOW() - INTERVAL '1 day'")
    active = cur.fetchone()["active"]
    cur.close(); conn.close()
    return {"total": total, "active_today": active}

def get_content(level, day):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content WHERE level=%s AND day=%s", (level, int(day)))
    row = cur.fetchone(); cur.close(); conn.close(); return row

def get_all_content():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT level, day, title FROM content ORDER BY level, day")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def save_content_full(level, day, d):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO content (level, day, title, shadowing_ru, shadowing_uz, razgovor_start,
            vocab, formulas, grammar, reading_texts, audio_questions, speaking_questions)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
        ON CONFLICT (level, day) DO UPDATE SET
            title=EXCLUDED.title, shadowing_ru=EXCLUDED.shadowing_ru, shadowing_uz=EXCLUDED.shadowing_uz,
            razgovor_start=EXCLUDED.razgovor_start, vocab=EXCLUDED.vocab, formulas=EXCLUDED.formulas,
            grammar=EXCLUDED.grammar, reading_texts=EXCLUDED.reading_texts,
            audio_questions=EXCLUDED.audio_questions, speaking_questions=EXCLUDED.speaking_questions;
    """, (level, int(day), d.get("title", ""), d.get("shadowing_ru", ""), d.get("shadowing_uz", ""),
          d.get("razgovor_start", ""),
          json.dumps(d.get("vocab", []), ensure_ascii=False),
          json.dumps(d.get("formulas", []), ensure_ascii=False),
          json.dumps(d.get("grammar", []), ensure_ascii=False),
          json.dumps(d.get("reading_texts", []), ensure_ascii=False),
          json.dumps(d.get("audio_questions", []), ensure_ascii=False),
          json.dumps(d.get("speaking_questions", []), ensure_ascii=False)))
    conn.commit(); cur.close(); conn.close()

def delete_content(level, day):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM content WHERE level=%s AND day=%s", (level, int(day)))
    cur.execute("DELETE FROM audios WHERE level=%s AND day=%s", (level, int(day)))
    conn.commit(); cur.close(); conn.close()

def get_setting(key):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO settings (key, value) VALUES (%s,%s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, value))
    conn.commit(); cur.close(); conn.close()

def get_ui():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings WHERE key = ANY(%s)", (list(DEFAULT_UI.keys()),))
    overrides = dict(cur.fetchall()); cur.close(); conn.close()
    out = dict(DEFAULT_UI); out.update(overrides); return out

def save_audio(level, day, slot, url):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO audios (level, day, slot, url, updated_at) VALUES (%s,%s,%s,%s,NOW())
        ON CONFLICT (level, day, slot) DO UPDATE SET url=EXCLUDED.url, updated_at=NOW()""",
        (level, int(day), slot, url))
    conn.commit(); cur.close(); conn.close()

def delete_audio(level, day, slot):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM audios WHERE level=%s AND day=%s AND slot=%s", (level, int(day), slot))
    conn.commit(); cur.close(); conn.close()

def get_audios_for(level, day):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT slot, url FROM audios WHERE level=%s AND day=%s", (level, int(day)))
    rows = cur.fetchall(); cur.close(); conn.close()
    return {r["slot"]: r["url"] for r in rows}

def esc(t):
    if t is None: return ""
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

# --- FLASK ---
flask_app = Flask(__name__, static_folder=".")
flask_app.secret_key = SESSION_SECRET

def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **k)
    return wrapper

def check_api_auth():
    if not session.get("admin"):
        abort(401)

LOGIN_PAGE = """<!DOCTYPE html><html lang="uz"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Adminka — Kirish</title>
<style>*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(170deg,#0a4a35,#1D9E75);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
.box{background:#fff;border-radius:20px;padding:32px 28px;width:100%;max-width:360px;box-shadow:0 20px 60px rgba(0,0,0,.3);}
h1{font-size:22px;font-weight:800;color:#0a4a35;margin-bottom:4px;}.sub{font-size:13px;color:#999;margin-bottom:24px;}
label{font-size:13px;color:#666;display:block;margin-bottom:6px;font-weight:500;}
input{width:100%;border:1.5px solid #e5e5df;border-radius:10px;padding:12px 14px;font-size:15px;margin-bottom:16px;color:#1a1a1a;}
input:focus{outline:none;border-color:#1D9E75;}
button{width:100%;background:#1D9E75;border:none;border-radius:12px;padding:14px;color:#fff;font-size:15px;font-weight:700;cursor:pointer;}
.err{background:#fde8e8;color:#b91c1c;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:16px;{ERR}}</style></head>
<body><form class="box" method="POST" action="/admin/login"><h1>Zamira Russian</h1><div class="sub">Adminka — kirish</div>
<div class="err">Login yoki parol noto'g'ri</div>
<label>Login</label><input type="text" name="username" required>
<label>Parol</label><input type="password" name="password" required>
<button type="submit">Kirish</button></form></body></html>"""

@flask_app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", ""); p = request.form.get("password", "")
        if hmac.compare_digest(u, ADMIN_USER) and hmac.compare_digest(p, ADMIN_PASSWORD):
            session["admin"] = True
            return redirect(url_for("admin"))
        return LOGIN_PAGE.replace("{ERR}", "display:block;"), 401
    if session.get("admin"):
        return redirect(url_for("admin"))
    return LOGIN_PAGE.replace("{ERR}", "display:none;")

@flask_app.route("/admin/logout")
def admin_logout():
    session.clear(); return redirect(url_for("admin_login"))

@flask_app.route("/")
def index():
    return send_from_directory(".", "index.html")

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

@flask_app.route("/api/set-level", methods=["POST"])
def api_set_level():
    data = request.json; uid = data.get("user_id"); lvl = data.get("level")
    if uid and lvl:
        set_user_level(uid, lvl); return {"ok": True}
    return {"ok": False}, 400

@flask_app.route("/api/ui")
def api_ui():
    return jsonify(get_ui())

@flask_app.route("/api/content")
def api_content():
    level = request.args.get("level", "A1"); day = int(request.args.get("day", 1))
    row = get_content(level, day); audios = get_audios_for(level, day)
    if row:
        d = dict(row); d["audios"] = audios; return jsonify(d)
    return jsonify({"level": level, "day": day, "audios": audios})

# ============ ADMINKA (sidebar) ============
@flask_app.route("/admin")
@require_admin
def admin():
    stats = get_stats(); content = get_all_content(); users = get_all_users()
    rh = get_setting("reminder_hour") or "9"
    rt = get_setting("reminder_text") or DEFAULT_REMINDER_TEXT
    ui = get_ui()
    r2warn = ("" if r2_configured() else
        "<div class='hint warn'>⚠️ R2 sozlanmagan — audio yuklash ishlamaydi. Railway env'larni qo'shing.</div>")
    ai_warn = ("" if ai_configured() else
        "<div class='hint warn'>⚠️ AI sozlanmagan. Railway'ga ANTHROPIC_API_KEY qo'shing.</div>")

    rows_lessons = "".join(
        f"<tr><td><span class='badge'>{c['level']}</span></td><td>{c['day']}</td>"
        f"<td>{esc(c.get('title')) or '—'}</td>"
        f"<td><a class='btn-sm' href='/admin/lesson?level={c['level']}&day={c['day']}'>Tahrirlash</a></td></tr>"
        for c in content) or "<tr><td colspan='4' class='muted'>Dars yo'q</td></tr>"

    rows_users = "".join(
        f"<tr><td>{esc(u['first_name']) or '-'}</td><td><span class='badge'>{u['level'] or '—'}</span></td>"
        f"<td class='muted'>{str(u['last_active'])[:16]}</td></tr>"
        for u in users[:50]) or "<tr><td colspan='3' class='muted'>Foydalanuvchi yo'q</td></tr>"

    def ui_field(key, label, area=False):
        v = esc(ui.get(key, ""))
        if area:
            return f"<label>{label}</label><textarea data-ui='{key}'>{v}</textarea>"
        return f"<label>{label}</label><input data-ui='{key}' value=\"{v}\">"

    html = """<!DOCTYPE html><html lang="uz"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Zamira Russian — Adminka</title>
<style>
:root{--bg:#0f0f0f;--card:#1a1a1a;--border:#333;--soft:#222;--text:#fff;--muted:#888;--ibg:#111;--ib:#444;--acc:#1D9E75;--acch:#17856a;--accs:#5DCAA5;--side:#161616;}
body.light{--bg:#f5f5f0;--card:#fff;--border:#e5e5df;--soft:#eee;--text:#1a1a1a;--muted:#888;--ibg:#fff;--ib:#d5d5cf;--acc:#1D9E75;--acch:#17856a;--accs:#0a5a40;--side:#fff;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh;}
/* SIDEBAR */
.sidebar{width:240px;background:var(--side);border-right:1px solid var(--border);padding:20px 14px;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto;}
.brand{font-size:18px;font-weight:800;margin-bottom:2px;}
.brand-sub{font-size:11px;color:var(--muted);margin-bottom:20px;}
.nav-item{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:10px;font-size:14px;color:var(--text);cursor:pointer;margin-bottom:3px;border:none;background:none;width:100%;text-align:left;font-family:inherit;}
.nav-item:hover{background:var(--soft);}
.nav-item.active{background:var(--acc);color:#fff;font-weight:600;}
.side-bottom{margin-top:18px;border-top:1px solid var(--border);padding-top:14px;}
/* MAIN */
.main{flex:1;padding:28px 32px;max-width:920px;}
.topbar{display:none;}
.panel{display:none;}.panel.active{display:block;}
h1{font-size:22px;font-weight:700;margin-bottom:18px;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:8px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;}
.card .num{font-size:28px;font-weight:700;color:var(--acc);}.card .label{font-size:12px;color:var(--muted);margin-top:4px;}
.section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:18px;}
.section h2{font-size:16px;font-weight:600;margin-bottom:16px;}
label{font-size:13px;color:var(--muted);display:block;margin-bottom:6px;margin-top:8px;}
input,select,textarea{width:100%;background:var(--ibg);border:1px solid var(--ib);border-radius:8px;padding:10px 12px;font-size:14px;color:var(--text);margin-bottom:6px;font-family:inherit;}
input[type=file]{padding:8px;}textarea{min-height:70px;resize:vertical;}
button.act{background:var(--acc);border:none;border-radius:8px;padding:12px 20px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;width:100%;margin-top:10px;}
button.act:hover{background:var(--acch);}
.icon-btn{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:13px;cursor:pointer;width:auto;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:var(--muted);padding:8px 0;border-bottom:1px solid var(--border);}
td{padding:8px 0;border-bottom:1px solid var(--soft);}
.muted{color:var(--muted);}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;background:rgba(29,158,117,.2);color:var(--accs);}
.btn-sm{display:inline-block;padding:5px 12px;border-radius:7px;background:var(--acc);color:#fff;font-size:12px;text-decoration:none;}
.msg{background:rgba(29,158,117,.12);border:1px solid var(--acc);border-radius:8px;padding:12px;font-size:13px;color:var(--accs);margin-bottom:12px;display:none;}
.hint{font-size:12px;color:var(--muted);margin:4px 0 10px;line-height:1.5;}
.hint.warn{color:#e0a800;}
.seg{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;}
.seg-btn{flex:1;min-width:90px;background:var(--ibg);border:1px solid var(--ib);border-radius:8px;padding:10px;font-size:13px;color:var(--text);cursor:pointer;width:auto;}
.seg-btn.active{background:var(--acc);color:#fff;border-color:var(--acc);}
.row2{display:flex;gap:10px;}.row2>div{flex:1;}
.burger{display:none;}
@media(max-width:760px){
  body{flex-direction:column;}
  .sidebar{position:fixed;left:-260px;top:0;z-index:50;transition:left .2s;box-shadow:0 0 40px rgba(0,0,0,.4);}
  .sidebar.open{left:0;}
  .main{padding:16px;max-width:100%;}
  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
  .burger{display:block;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:18px;cursor:pointer;width:auto;}
}
</style></head><body>

<div class="sidebar" id="sidebar">
  <div class="brand">Zamira Russian</div>
  <div class="brand-sub">Adminka</div>
  <button class="nav-item active" data-panel="dash" onclick="nav(this)">📊 Bosh sahifa</button>
  <button class="nav-item" data-panel="lessons" onclick="nav(this)">📚 Darslar</button>
  <button class="nav-item" data-panel="users" onclick="nav(this)">👥 Foydalanuvchilar</button>
  <button class="nav-item" data-panel="broadcast" onclick="nav(this)">✉️ Xabar yuborish</button>
  <button class="nav-item" data-panel="ui" onclick="nav(this)">🎨 Interfeys matnlari</button>
  <button class="nav-item" data-panel="settings" onclick="nav(this)">⚙️ Sozlamalar</button>
  <div class="side-bottom">
    <button class="nav-item" onclick="toggleTheme()" id="themeBtn">🌙 Qora fon</button>
    <button class="nav-item" onclick="location.href='/admin/logout'">🚪 Chiqish</button>
  </div>
</div>

<div class="main">
  <div class="topbar"><button class="burger" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button><b>Zamira Russian</b></div>

  <div class="panel active" id="p-dash">
    <h1>Bosh sahifa</h1>
    <div class="cards">
      <div class="card"><div class="num">""" + str(stats['total']) + """</div><div class="label">Jami foydalanuvchi</div></div>
      <div class="card"><div class="num">""" + str(stats['active_today']) + """</div><div class="label">Bugun faol</div></div>
      <div class="card"><div class="num">""" + str(len(content)) + """</div><div class="label">Jami darslar</div></div>
    </div>
  </div>

  <div class="panel" id="p-lessons">
    <h1>Darslar</h1>
    <div class="section">
      <h2>🤖 AI bilan to'ldirish (PDF dan)</h2>
      """ + ai_warn + """
      <div class="hint">PDF yuklang — Claude undan lug'at, matn, grammatika, savollarni ajratib darsni to'ldiradi. Keyin tahrirlash oynasida ko'rib chiqasiz. ⚠️ Bu o'sha kunning matn kontentini almashtiradi (audio tegmaydi).</div>
      <div class="row2">
        <div><label>Daraja</label><select id="ai_l"><option>A0</option><option selected>A1</option><option>B1</option></select></div>
        <div><label>Kun</label><input type="number" id="ai_d" min="1" value="1"></div>
      </div>
      <label>PDF fayl</label><input type="file" id="ai_f" accept="application/pdf">
      <button class="act" onclick="aiFill()">🤖 AI bilan to'ldirish</button>
    </div>
    <div class="section">
      <h2>Yangi dars ochish / tahrirlash</h2>
      <div class="row2">
        <div><label>Daraja</label><select id="nl"><option>A0</option><option selected>A1</option><option>B1</option></select></div>
        <div><label>Kun</label><input type="number" id="nd" min="1" value="1"></div>
      </div>
      <button class="act" onclick="openLesson()">Ochish →</button>
    </div>
    <div class="section">
      <h2>Mavjud darslar</h2>
      <table><tr><th>Daraja</th><th>Kun</th><th>Nomi</th><th></th></tr>""" + rows_lessons + """</table>
    </div>
  </div>

  <div class="panel" id="p-users">
    <h1>Foydalanuvchilar</h1>
    <div class="section">
      <table><tr><th>Ism</th><th>Daraja</th><th>Oxirgi faollik</th></tr>""" + rows_users + """</table>
    </div>
  </div>

  <div class="panel" id="p-broadcast">
    <h1>Xabar yuborish (hammaga)</h1>
    <div class="section">
      <div id="bm" class="msg">Yuborildi!</div>
      <div class="seg">
        <button class="seg-btn active" data-t="text" onclick="setBc('text')">📝 Matn</button>
        <button class="seg-btn" data-t="audio" onclick="setBc('audio')">🎵 Audio</button>
        <button class="seg-btn" data-t="video_note" onclick="setBc('video_note')">⭕ Dumaloq video</button>
      </div>
      <div id="bc-text"><textarea id="bt" placeholder="Bugungi darsni o'tdingizmi? 📚"></textarea></div>
      <div id="bc-audio" style="display:none;"><label>Audio fayl</label><input type="file" id="baf" accept="audio/*"><label>Izoh (ixtiyoriy)</label><textarea id="bac"></textarea></div>
      <div id="bc-video" style="display:none;"><label>Dumaloq video (kvadrat mp4)</label><input type="file" id="bvf" accept="video/*"><label>Izoh (ixtiyoriy)</label><textarea id="bvc"></textarea></div>
      <button class="act" onclick="sendBroadcast()">Yuborish</button>
    </div>
  </div>

  <div class="panel" id="p-ui">
    <h1>Interfeys matnlari</h1>
    <div id="uim" class="msg">Saqlandi!</div>
    <div class="section">
      <h2>Welcome ekran</h2>
      """ + ui_field("ui_welcome_title", "Katta sarlavha") + ui_field("ui_welcome_sub", "Pastki sarlavha") + ui_field("ui_welcome_desc", "Tavsif", True) + """
    </div>
    <div class="section">
      <h2>Kurs kartasi</h2>
      """ + ui_field("ui_course_label", "Daraja yorlig'i (masalan A1 — A2)") + ui_field("ui_course_desc", "Tavsif (masalan Bilaman lekin gapira olmayman)") + """
    </div>
    <div class="section">
      <h2>Bo'lim nomlari</h2>
      """ + ui_field("label_vocab", "Lug'at bo'limi") + ui_field("label_formula", "Nutq formulalari bo'limi") + ui_field("label_audio", "Audio mashq bo'limi") + ui_field("label_speaking", "Gapirish bo'limi") + ui_field("label_grammar", "Grammatika bo'limi") + ui_field("label_text", "Matn bo'limi") + """
      <button class="act" onclick="saveUI()">Hammasini saqlash</button>
    </div>
  </div>

  <div class="panel" id="p-settings">
    <h1>Sozlamalar</h1>
    <div class="section">
      <h2>Kunlik avtomatik eslatma</h2>
      <div id="rm" class="msg">Saqlandi!</div>
      <label>Soat (0-23, Toshkent)</label><input type="number" id="rh" min="0" max="23" value=\"""" + rh + """\" style="max-width:120px;">
      <label>Eslatma matni (har kuni shu soatda boradi)</label><textarea id="rt">""" + esc(rt) + """</textarea>
      <button class="act" onclick="saveReminder()">Saqlash</button>
    </div>
    <div class="section">
      <h2>Kirish</h2>
      <div class="hint">Login va parol Railway env'larida (ADMIN_USER, ADMIN_PASSWORD) o'rnatiladi.</div>
    </div>
  </div>
</div>

<script>
function openPanel(p){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.panel===p));
  document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active',x.id==='p-'+p));
  document.getElementById('sidebar').classList.remove('open');
}
function nav(btn){var p=btn.dataset.panel;location.hash=p;openPanel(p);}
(function(){var h=(location.hash||'').replace('#','');
  if(['dash','lessons','users','broadcast','ui','settings'].indexOf(h)>=0)openPanel(h);})();
function applyTheme(t){var b=document.getElementById('themeBtn');if(t==='light'){document.body.classList.add('light');b.textContent='☀️ Oq fon';}else{document.body.classList.remove('light');b.textContent='🌙 Qora fon';}}
function toggleTheme(){var t=document.body.classList.contains('light')?'dark':'light';try{localStorage.setItem('admin_theme',t);}catch(e){}applyTheme(t);}
(function(){var t='dark';try{t=localStorage.getItem('admin_theme')||'dark';}catch(e){}applyTheme(t);})();

function openLesson(){location.href='/admin/lesson?level='+document.getElementById('nl').value+'&day='+document.getElementById('nd').value;}
async function aiFill(){
  var f=document.getElementById('ai_f');if(!f.files.length)return alert('PDF tanlang');
  var lvl=document.getElementById('ai_l').value,day=document.getElementById('ai_d').value;
  var fd=new FormData();fd.append('level',lvl);fd.append('day',day);fd.append('file',f.files[0]);
  var b=event.target;b.disabled=true;b.textContent='🤖 AI ishlayapti... (30s gacha)';
  var r=await fetch('/admin/ai-fill',{method:'POST',body:fd});
  if(r.ok){location.href='/admin/lesson?level='+lvl+'&day='+day;}
  else{b.disabled=false;b.textContent='🤖 AI bilan to\\'ldirish';var j=await r.json().catch(function(){return {};});alert('Xato: '+(j.error||r.status));}
}

async function saveReminder(){
  await fetch('/admin/set-reminder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hour:document.getElementById('rh').value,text:document.getElementById('rt').value})});
  var m=document.getElementById('rm');m.style.display='block';setTimeout(()=>m.style.display='none',2000);
}
async function saveUI(){
  var o={};document.querySelectorAll('[data-ui]').forEach(el=>{o[el.dataset.ui]=el.value;});
  await fetch('/admin/save-ui',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});
  var m=document.getElementById('uim');m.style.display='block';setTimeout(()=>m.style.display='none',2000);
}
var bcType='text';
function setBc(t){bcType=t;document.querySelectorAll('.seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.t===t));
  document.getElementById('bc-text').style.display=(t==='text')?'block':'none';
  document.getElementById('bc-audio').style.display=(t==='audio')?'block':'none';
  document.getElementById('bc-video').style.display=(t==='video_note')?'block':'none';}
async function sendBroadcast(){
  if(!confirm('Hammaga yuborilsinmi?'))return;
  if(bcType==='text'){var t=document.getElementById('bt').value.trim();if(!t)return alert('Matn yozing');
    await fetch('/admin/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});}
  else{var fe=(bcType==='audio')?document.getElementById('baf'):document.getElementById('bvf');
    var ce=(bcType==='audio')?document.getElementById('bac'):document.getElementById('bvc');
    if(!fe.files.length)return alert('Fayl tanlang');
    var fd=new FormData();fd.append('type',bcType);fd.append('caption',ce.value.trim());fd.append('file',fe.files[0]);
    var b=event.target;b.disabled=true;b.textContent='Yuborilyapti...';
    await fetch('/admin/broadcast-media',{method:'POST',body:fd});b.disabled=false;b.textContent='Yuborish';}
  var m=document.getElementById('bm');m.style.display='block';setTimeout(()=>m.style.display='none',2500);
}
</script></body></html>"""
    return html

# ============ DARS TAHRIRLASH SAHIFASI ============
@flask_app.route("/admin/lesson")
@require_admin
def admin_lesson():
    level = request.args.get("level", "A1")
    day = request.args.get("day", "1")
    r2warn = ("" if r2_configured() else "<div class='hint warn'>⚠️ R2 sozlanmagan — audio yuklash ishlamaydi.</div>")
    return LESSON_EDITOR.replace("__LEVEL__", esc(level)).replace("__DAY__", esc(str(day))).replace("__R2WARN__", r2warn)

LESSON_EDITOR = """<!DOCTYPE html><html lang="uz"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Dars tahrirlash</title>
<style>
:root{--bg:#0f0f0f;--card:#1a1a1a;--border:#333;--soft:#222;--text:#fff;--muted:#888;--ibg:#111;--ib:#444;--acc:#1D9E75;--acch:#17856a;--accs:#5DCAA5;}
body.light{--bg:#f5f5f0;--card:#fff;--border:#e5e5df;--soft:#eee;--text:#1a1a1a;--muted:#888;--ibg:#fff;--ib:#d5d5cf;--accs:#0a5a40;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:840px;margin:0 auto;}
a.back{color:var(--muted);text-decoration:none;font-size:13px;}
h1{font-size:22px;font-weight:700;margin:8px 0 4px;}
.tag{color:var(--muted);font-size:13px;margin-bottom:18px;}
.section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px;}
.section h2{font-size:15px;font-weight:600;margin-bottom:12px;}
label{font-size:13px;color:var(--muted);display:block;margin:8px 0 4px;}
input,textarea,select{width:100%;background:var(--ibg);border:1px solid var(--ib);border-radius:8px;padding:9px 11px;font-size:14px;color:var(--text);font-family:inherit;}
textarea{min-height:60px;resize:vertical;}
.lrow{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:flex-start;border:1px dashed var(--ib);border-radius:8px;padding:8px;}
.lrow .lfield{flex:1 1 140px;min-width:130px;}
.ldel{background:#5a2020;color:#fff;border:none;border-radius:7px;width:34px;flex-shrink:0;cursor:pointer;font-size:14px;align-self:stretch;}
.add{background:var(--soft);color:var(--text);border:1px dashed var(--ib);border-radius:8px;padding:8px;font-size:13px;cursor:pointer;width:100%;margin-top:4px;}
.save{background:var(--acc);color:#fff;border:none;border-radius:10px;padding:14px;font-size:15px;font-weight:700;cursor:pointer;width:100%;position:sticky;bottom:12px;}
.save:hover{background:var(--acch);}
.hint{font-size:12px;color:var(--muted);margin:4px 0;}.hint.warn{color:#e0a800;}
.aud{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap;}
.aud audio{height:34px;}
.msg{background:rgba(29,158,117,.15);border:1px solid var(--acc);border-radius:8px;padding:12px;color:var(--accs);font-size:13px;margin-bottom:12px;display:none;}
.up{background:var(--acc);color:#fff;border:none;border-radius:7px;padding:8px 12px;font-size:12px;cursor:pointer;}
</style></head><body>
<a class="back" href="/admin#lessons">← Darslar</a>
<h1 id="lt">Dars tahrirlash</h1>
<div class="tag" id="ltag"></div>
<div id="msg" class="msg">Saqlandi!</div>

<div class="section"><h2>Dars nomi</h2><input id="f_title" placeholder="masalan: Tanishuv"></div>

<div class="section"><h2>🔤 Lug'at</h2>
  <div class="hint">Har bir so'z: ruscha, o'zbekcha, misol. Pastdagi tugma bilan qo'shing, ✕ bilan o'chiring.</div>
  <div id="L_vocab"></div><button class="add" onclick="addItem('vocab')">+ So'z qo'shish</button></div>

<div class="section"><h2>💬 Nutq formulalari</h2>
  <div id="L_formulas"></div><button class="add" onclick="addItem('formulas')">+ Formula qo'shish</button></div>

<div class="section"><h2>🗣 Shadowing & razgovor</h2>
  <label>Shadowing (ruscha)</label><textarea id="f_shadowing_ru"></textarea>
  <label>Shadowing (o'zbekcha)</label><textarea id="f_shadowing_uz"></textarea>
  <label>Razgovor boshlash gapi</label><input id="f_razgovor_start"></div>

<div class="section"><h2>📖 O'qish matnlari</h2>
  <div id="L_reading_texts"></div><button class="add" onclick="addItem('reading_texts')">+ Matn qo'shish</button></div>

<div class="section"><h2>📘 Grammatika</h2>
  <div id="L_grammar"></div><button class="add" onclick="addItem('grammar')">+ Grammatika bloki</button></div>

<div class="section"><h2>🎤 Gapirish savollari</h2>
  <div id="L_speaking_questions"></div><button class="add" onclick="addItem('speaking_questions')">+ Savol qo'shish</button></div>

<div class="section"><h2>🎧 Audirovaniye test savollari</h2>
  <div id="L_audio_questions"></div><button class="add" onclick="addItem('audio_questions')">+ Savol qo'shish</button></div>

<div class="section"><h2>🎵 Audio fayllar</h2>
  __R2WARN__
  <div id="audioBox"></div></div>

<button class="save" onclick="saveLesson()">💾 Saqlash</button>

<script>
(function(){var t='dark';try{t=localStorage.getItem('admin_theme')||'dark';}catch(e){}if(t==='light')document.body.classList.add('light');})();
var LEVEL="__LEVEL__",DAY="__DAY__";
document.getElementById('ltag').textContent=LEVEL+' · '+DAY+'-kun';

var SCHEMA={
  vocab:[{k:'ru',l:"So'z (rus)"},{k:'stress',l:"Urg'u bilan (приве́т)"},{k:'uz',l:'Tarjima'},{k:'ex1ru',l:'1-misol (rus)'},{k:'ex1uz',l:'1-misol (uz)'},{k:'ex2ru',l:'2-misol (rus)'},{k:'ex2uz',l:'2-misol (uz)'},{k:'sin',l:'Sinonim'},{k:'ant',l:'Antonim'},{k:'nsv',l:'НСВ (feʼl)'},{k:'sv',l:'СВ (feʼl)'}],
  formulas:[{k:'ru',l:'Ruscha'},{k:'uz',l:"O'zbekcha"}],
  reading_texts:[{k:'level',l:'Sarlavha (A1 daraja)'},{k:'ru',l:'Ruscha matn',a:1},{k:'uz',l:"O'zbekcha tarjima",a:1}],
  grammar:[{k:'title',l:'Mavzu'},{k:'sub',l:'Izoh'},{k:'base',l:'Asos shakl'},{k:'res',l:'Natija shakl'},{k:'example',l:'Misol',a:1}],
  speaking_questions:[{k:'title',l:'Savol (ruscha)'},{k:'desc',l:'Izoh'},{k:'format',l:'Format (🎤 30 soniya)'}],
  audio_questions:[{k:'q',l:'Savol'},{k:'options',l:'Variantlar (har qatorda bitta)',lines:1},{k:'correct',l:"To'g'ri variant raqami (1,2,..)"}]
};
function addItem(name,it){
  it=it||{};var c=document.getElementById('L_'+name);var sc=SCHEMA[name];
  var row=document.createElement('div');row.className='lrow';
  sc.forEach(function(f){
    var el=(f.a||f.lines)?document.createElement('textarea'):document.createElement('input');
    el.className='lfield';el.dataset.k=f.k;el.dataset.lines=f.lines?1:'';el.placeholder=f.l;
    var v=it[f.k];if(f.lines&&Array.isArray(v))v=v.join('\\n');el.value=(v==null?'':v);
    row.appendChild(el);
  });
  var d=document.createElement('button');d.className='ldel';d.type='button';d.textContent='✕';d.onclick=function(){row.remove();};
  row.appendChild(d);c.appendChild(row);
}
function collect(name){
  var out=[];document.querySelectorAll('#L_'+name+' .lrow').forEach(function(row){
    var o={},empty=true;
    row.querySelectorAll('.lfield').forEach(function(el){
      var v=el.value;if(el.dataset.lines){o[el.dataset.k]=v.split('\\n').map(s=>s.trim()).filter(Boolean);}else{o[el.dataset.k]=v;}
      if(v&&v.trim())empty=false;
    });
    if(!empty)out.push(o);
  });
  return out;
}
function renderAudio(audios){
  audios=audios||{};var box=document.getElementById('audioBox');box.innerHTML='';
  var slots={audirov:'Audirovaniye',shadowing:'Shadowing',taqlid:'Taqlid'};
  Object.keys(slots).forEach(function(slot){
    var div=document.createElement('div');div.className='aud';
    var html='<b style="min-width:110px">'+slots[slot]+'</b>';
    if(audios[slot])html+='<audio controls preload="none" src="'+audios[slot]+'"></audio>';
    else html+='<span class="hint">yo\\'q</span>';
    div.innerHTML=html;
    var inp=document.createElement('input');inp.type='file';inp.accept='audio/*';inp.style.maxWidth='200px';
    var btn=document.createElement('button');btn.className='up';btn.textContent='Yuklash';
    btn.onclick=function(){uploadAudio(slot,inp,btn);};
    div.appendChild(inp);div.appendChild(btn);box.appendChild(div);
  });
}
async function uploadAudio(slot,inp,btn){
  if(!inp.files.length)return alert('Fayl tanlang');
  var fd=new FormData();fd.append('level',LEVEL);fd.append('day',DAY);fd.append('slot',slot);fd.append('file',inp.files[0]);
  btn.disabled=true;btn.textContent='...';
  var r=await fetch('/admin/upload-audio',{method:'POST',body:fd});
  btn.disabled=false;btn.textContent='Yuklash';
  if(r.ok){load();}else{var j=await r.json().catch(()=>({}));alert('Xato: '+(j.error||r.status));}
}
async function load(){
  var r=await fetch('/api/content?level='+LEVEL+'&day='+DAY);var d=await r.json();
  document.getElementById('f_title').value=d.title||'';
  document.getElementById('f_shadowing_ru').value=d.shadowing_ru||'';
  document.getElementById('f_shadowing_uz').value=d.shadowing_uz||'';
  document.getElementById('f_razgovor_start').value=d.razgovor_start||'';
  ['vocab','formulas','reading_texts','grammar','speaking_questions','audio_questions'].forEach(function(n){
    document.getElementById('L_'+n).innerHTML='';
    (d[n]||[]).forEach(function(it){addItem(n,it);});
  });
  renderAudio(d.audios);
}
async function saveLesson(){
  var body={level:LEVEL,day:DAY,
    title:document.getElementById('f_title').value,
    shadowing_ru:document.getElementById('f_shadowing_ru').value,
    shadowing_uz:document.getElementById('f_shadowing_uz').value,
    razgovor_start:document.getElementById('f_razgovor_start').value,
    vocab:collect('vocab'),formulas:collect('formulas'),reading_texts:collect('reading_texts'),
    grammar:collect('grammar'),speaking_questions:collect('speaking_questions'),audio_questions:collect('audio_questions')};
  var r=await fetch('/admin/save-lesson',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){var m=document.getElementById('msg');m.style.display='block';window.scrollTo(0,0);setTimeout(()=>m.style.display='none',2000);}
  else alert('Saqlashda xato');
}
load();
</script></body></html>"""

# --- ADMIN API endpoints ---
@flask_app.route("/admin/set-reminder", methods=["POST"])
def admin_set_reminder():
    check_api_auth()
    data = request.json or {}
    hour = str(data.get("hour", "9")); text = data.get("text", "").strip()
    set_setting("reminder_hour", hour)
    if text: set_setting("reminder_text", text)
    reschedule_reminder(int(hour))
    return {"ok": True}

@flask_app.route("/admin/save-ui", methods=["POST"])
def admin_save_ui():
    check_api_auth()
    data = request.json or {}
    for k, v in data.items():
        if k in DEFAULT_UI:
            set_setting(k, v)
    return {"ok": True}

@flask_app.route("/admin/save-lesson", methods=["POST"])
def admin_save_lesson():
    check_api_auth()
    d = request.json or {}
    if not d.get("level") or d.get("day") is None:
        return {"ok": False, "error": "level/day yo'q"}, 400
    save_content_full(d["level"], int(d["day"]), d)
    return {"ok": True}

@flask_app.route("/admin/delete-lesson", methods=["POST"])
def admin_delete_lesson():
    check_api_auth()
    d = request.json or {}
    delete_content(d["level"], int(d["day"]))
    return {"ok": True}

@flask_app.route("/admin/ai-fill", methods=["POST"])
def admin_ai_fill():
    check_api_auth()
    if not ai_configured():
        return {"ok": False, "error": "AI sozlanmagan (ANTHROPIC_API_KEY yo'q)"}, 400
    level = request.form.get("level"); day = request.form.get("day")
    f = request.files.get("file")
    if not (level and day and f):
        return {"ok": False, "error": "ma'lumot to'liq emas"}, 400
    try:
        text = extract_pdf_text(f)
        if not text.strip():
            return {"ok": False, "error": "PDF dan matn topilmadi (skaner/rasm bo'lishi mumkin)"}, 400
        data = ai_generate_lesson(text, level, int(day))
        save_content_full(level, int(day), data)
    except Exception as e:
        logger.exception("AI fill xato")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True}

@flask_app.route("/admin/upload-audio", methods=["POST"])
def admin_upload_audio():
    check_api_auth()
    if not r2_configured():
        return {"ok": False, "error": "R2 sozlanmagan"}, 400
    level = request.form.get("level"); day = request.form.get("day")
    slot = request.form.get("slot"); f = request.files.get("file")
    if not (level and day and slot and f):
        return {"ok": False, "error": "ma'lumot to'liq emas"}, 400
    if slot not in AUDIO_SLOTS:
        return {"ok": False, "error": "bo'lim noto'g'ri"}, 400
    try:
        url = upload_audio_to_r2(f, level, int(day), slot)
        save_audio(level, int(day), slot, url)
    except Exception as e:
        logger.exception("R2 yuklash xato")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "url": url}

@flask_app.route("/admin/broadcast", methods=["POST"])
def admin_broadcast():
    check_api_auth()
    text = (request.json or {}).get("text", "")
    if text:
        threading.Thread(target=send_broadcast_sync, args=(text,)).start()
    return {"ok": True}

@flask_app.route("/admin/broadcast-media", methods=["POST"])
def admin_broadcast_media():
    check_api_auth()
    import uuid, tempfile
    mtype = request.form.get("type", ""); caption = request.form.get("caption", "").strip()
    f = request.files.get("file")
    if mtype not in ("audio", "video_note") or not f:
        return {"ok": False}, 400
    ext = os.path.splitext(f.filename or "")[1] or (".mp4" if mtype == "video_note" else ".mp3")
    tmp = os.path.join(tempfile.gettempdir(), f"bc_{uuid.uuid4().hex}{ext}")
    f.save(tmp)
    threading.Thread(target=send_media_broadcast_sync, args=(mtype, tmp, caption)).start()
    return {"ok": True}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# --- BOT ---
bot_app = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    kb = [[InlineKeyboardButton("📚 Darsni boshlash", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"Salom, {user.first_name}! 👋\n\n*Zamira Russian* — ruscha gapirish kursi.\n\n"
        f"Har kuni 10 daqiqa — 30 kunda begona bilan ruscha gaplasha olasiz. 🇷🇺",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 /start — Bosh sahifa")

def send_reminders():
    if not bot_app: return
    users = get_all_users(); text = get_setting("reminder_text") or DEFAULT_REMINDER_TEXT
    async def _s():
        for u in users:
            try: await bot_app.bot.send_message(chat_id=u["user_id"], text=text)
            except Exception as e: logger.warning(f"Eslatma {u['user_id']}: {e}")
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(_s()); loop.close()

def send_broadcast_sync(text):
    if not bot_app: return
    users = get_all_users()
    async def _s():
        for u in users:
            try: await bot_app.bot.send_message(chat_id=u["user_id"], text=text)
            except Exception as e: logger.warning(f"Xabar {u['user_id']}: {e}")
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(_s()); loop.close()

def send_media_broadcast_sync(mtype, path, caption):
    if not bot_app: return
    users = get_all_users()
    async def _s():
        fid = None
        for u in users:
            uid = u["user_id"]
            try:
                if mtype == "video_note":
                    if fid is None:
                        with open(path, "rb") as fh:
                            m = await bot_app.bot.send_video_note(chat_id=uid, video_note=fh)
                        fid = m.video_note.file_id
                    else:
                        await bot_app.bot.send_video_note(chat_id=uid, video_note=fid)
                    if caption: await bot_app.bot.send_message(chat_id=uid, text=caption)
                else:
                    if fid is None:
                        with open(path, "rb") as fh:
                            m = await bot_app.bot.send_audio(chat_id=uid, audio=fh, caption=caption or None)
                        fid = m.audio.file_id
                    else:
                        await bot_app.bot.send_audio(chat_id=uid, audio=fid, caption=caption or None)
            except Exception as e:
                logger.warning(f"Media {uid}: {e}")
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try: loop.run_until_complete(_s())
    finally:
        loop.close()
        try: os.remove(path)
        except Exception: pass

scheduler = BackgroundScheduler(timezone=TASHKENT_TZ)

def reschedule_reminder(hour):
    scheduler.remove_all_jobs()
    scheduler.add_job(send_reminders, "cron", hour=hour, minute=0)
    logger.info(f"Eslatma: soat {hour}:00")

def run_bot():
    global bot_app
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    logger.info("Bot ishga tushdi...")
    bot_app.run_polling()

if __name__ == "__main__":
    init_db()
    hour = int(get_setting("reminder_hour") or 9)
    reschedule_reminder(hour); scheduler.start()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask ishga tushdi")
    run_bot()
