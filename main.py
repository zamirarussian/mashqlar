import os
import threading
import logging
import json
import uuid
import tempfile
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
# --- LOGIN / PAROL ---
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "zamira2024")
SESSION_SECRET = os.environ.get("SESSION_SECRET", ADMIN_SECRET + "_session")
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# --- R2 (Cloudflare object storage) ---
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

# Audio bo'limlari (web app'dagi mashqlar)
AUDIO_SLOTS = {"audirov": "Audirovaniye", "shadowing": "Shadowing", "taqlid": "Taqlid"}
AUDIO_CTYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
                ".oga": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac"}

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
    # ?v= — eski fayl o'rniga yangisi keshlanmasligi uchun
    return f"{R2_PUBLIC_URL}/{key}?v={int(time.time())}"

DEFAULT_REMINDER_TEXT = (
    "📚 Bugungi darsni o'tdingizmi?\n\n"
    "Har kuni 10 daqiqa — va ruscha gaplashishga yaqinlashasiz!"
)

# --- DATABASE ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            level TEXT DEFAULT NULL,
            current_day INTEGER DEFAULT 1,
            joined_at TIMESTAMP DEFAULT NOW(),
            last_active TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS content (
            id SERIAL PRIMARY KEY,
            level TEXT NOT NULL,
            day INTEGER NOT NULL,
            shadowing_ru TEXT NOT NULL,
            shadowing_uz TEXT NOT NULL,
            vocab JSONB NOT NULL,
            razgovor_start TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(level, day)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audios (
            level TEXT NOT NULL,
            day INTEGER NOT NULL,
            slot TEXT NOT NULL,
            url TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (level, day, slot)
        );
    """)
    cur.execute("""
        INSERT INTO settings (key, value) VALUES ('reminder_hour', '9')
        ON CONFLICT (key) DO NOTHING;
    """)
    cur.execute(
        "INSERT INTO settings (key, value) VALUES ('reminder_text', %s) ON CONFLICT (key) DO NOTHING;",
        (DEFAULT_REMINDER_TEXT,)
    )
    cur.execute("""
        INSERT INTO content (level, day, shadowing_ru, shadowing_uz, vocab, razgovor_start)
        VALUES (
            'A1', 1,
            '— Как дела? — Нормально, спасибо!',
            '— Ishlar qanday? — Yaxshi, rahmat!',
            '[{"ru":"привет","uz":"salom","ex":"Привет, как дела?"},{"ru":"спасибо","uz":"rahmat","ex":"Спасибо большое!"},{"ru":"пожалуйста","uz":"iltimos","ex":"Дай, пожалуйста."},{"ru":"да / нет","uz":"ha / yoq","ex":"Да, конечно. Нет."},{"ru":"хорошо","uz":"yaxshi","ex":"Всё хорошо!"}]',
            'Привет! Как тебя зовут?'
        )
        ON CONFLICT DO NOTHING;
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database tayyor")

def save_user(user_id, first_name, username):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, first_name, username)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET last_active=NOW(), first_name=EXCLUDED.first_name;
    """, (user_id, first_name, username))
    conn.commit()
    cur.close()
    conn.close()

def set_user_level(user_id, level):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET level=%s WHERE user_id=%s", (level, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_all_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users ORDER BY joined_at DESC")
    users = cur.fetchall()
    cur.close()
    conn.close()
    return users

def get_stats():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM users")
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as active FROM users WHERE last_active > NOW() - INTERVAL '1 day'")
    active = cur.fetchone()["active"]
    cur.execute("SELECT level, COUNT(*) as cnt FROM users WHERE level IS NOT NULL GROUP BY level")
    levels = cur.fetchall()
    cur.close()
    conn.close()
    return {"total": total, "active_today": active, "by_level": levels}

def get_content(level, day):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content WHERE level=%s AND day=%s", (level, day))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def get_all_content():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content ORDER BY level, day")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def save_content(level, day, shadowing_ru, shadowing_uz, vocab_json, razgovor_start):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO content (level, day, shadowing_ru, shadowing_uz, vocab, razgovor_start)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (level, day) DO UPDATE SET
            shadowing_ru=EXCLUDED.shadowing_ru,
            shadowing_uz=EXCLUDED.shadowing_uz,
            vocab=EXCLUDED.vocab,
            razgovor_start=EXCLUDED.razgovor_start;
    """, (level, day, shadowing_ru, shadowing_uz, vocab_json, razgovor_start))
    conn.commit()
    cur.close()
    conn.close()

def get_setting(key):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, (key, value))
    conn.commit()
    cur.close()
    conn.close()

def save_audio(level, day, slot, url):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO audios (level, day, slot, url, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (level, day, slot) DO UPDATE SET url=EXCLUDED.url, updated_at=NOW()
    """, (level, int(day), slot, url))
    conn.commit()
    cur.close()
    conn.close()

def delete_audio(level, day, slot):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM audios WHERE level=%s AND day=%s AND slot=%s",
                (level, int(day), slot))
    conn.commit()
    cur.close()
    conn.close()

def get_audios_for(level, day):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT slot, url FROM audios WHERE level=%s AND day=%s", (level, int(day)))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r["slot"]: r["url"] for r in rows}

def get_all_audios():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT level, day, slot, url, updated_at FROM audios ORDER BY level, day, slot")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def esc(text):
    """HTML-escape qilish (textarea ichiga xavfsiz qo'yish uchun)."""
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# --- FLASK ---
flask_app = Flask(__name__, static_folder=".")
flask_app.secret_key = SESSION_SECRET

# --- AUTH ---
def require_admin(fn):
    """Sahifalar uchun: kirilmagan bo'lsa login'ga yo'naltiradi."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def check_api_auth():
    """API uchun: kirilmagan bo'lsa 401 qaytaradi."""
    if not session.get("admin"):
        abort(401)

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Adminka — Kirish</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:linear-gradient(170deg,#0a4a35,#1D9E75);min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:24px;}
.box{background:#fff;border-radius:20px;padding:32px 28px;width:100%;max-width:360px;
  box-shadow:0 20px 60px rgba(0,0,0,0.3);}
h1{font-size:22px;font-weight:800;color:#0a4a35;margin-bottom:4px;}
.sub{font-size:13px;color:#999;margin-bottom:24px;}
label{font-size:13px;color:#666;display:block;margin-bottom:6px;font-weight:500;}
input{width:100%;border:1.5px solid #e5e5df;border-radius:10px;padding:12px 14px;
  font-size:15px;margin-bottom:16px;font-family:inherit;color:#1a1a1a;}
input:focus{outline:none;border-color:#1D9E75;}
button{width:100%;background:#1D9E75;border:none;border-radius:12px;padding:14px;
  color:#fff;font-size:15px;font-weight:700;cursor:pointer;}
button:active{transform:scale(0.98);}
.err{background:#fde8e8;color:#b91c1c;border-radius:10px;padding:10px 12px;
  font-size:13px;margin-bottom:16px;{ERR_DISPLAY}}
</style>
</head>
<body>
<form class="box" method="POST" action="/admin/login">
  <h1>Zamira Russian</h1>
  <div class="sub">Adminka — kirish</div>
  <div class="err">Login yoki parol noto'g'ri</div>
  <label>Login</label>
  <input type="text" name="username" placeholder="Login" autocomplete="username" required>
  <label>Parol</label>
  <input type="password" name="password" placeholder="Parol" autocomplete="current-password" required>
  <button type="submit">Kirish</button>
</form>
</body>
</html>"""

@flask_app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        ok = (hmac.compare_digest(u, ADMIN_USER) and
              hmac.compare_digest(p, ADMIN_PASSWORD))
        if ok:
            session["admin"] = True
            return redirect(url_for("admin"))
        return LOGIN_PAGE.replace("{ERR_DISPLAY}", "display:block;"), 401
    if session.get("admin"):
        return redirect(url_for("admin"))
    return LOGIN_PAGE.replace("{ERR_DISPLAY}", "display:none;")

@flask_app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@flask_app.route("/")
def index():
    return send_from_directory(".", "index.html")

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

@flask_app.route("/api/set-level", methods=["POST"])
def api_set_level():
    data = request.json
    user_id = data.get("user_id")
    level = data.get("level")
    if user_id and level:
        set_user_level(user_id, level)
        return {"ok": True}
    return {"ok": False}, 400

@flask_app.route("/api/content")
def api_content():
    level = request.args.get("level", "A1")
    day = int(request.args.get("day", 1))
    row = get_content(level, day)
    audios = get_audios_for(level, day)
    if row:
        d = dict(row)
        d["audios"] = audios
        return jsonify(d)
    # Kontent matni bo'lmasa ham audiolarni qaytaramiz
    return jsonify({"level": level, "day": day, "audios": audios})

@flask_app.route("/admin")
@require_admin
def admin():
    stats = get_stats()
    content = get_all_content()
    reminder_hour = get_setting("reminder_hour") or "9"
    reminder_text = get_setting("reminder_text") or DEFAULT_REMINDER_TEXT
    users = get_all_users()

    rows_content = "".join(
        f"<tr><td><span class='badge'>{r['level']}</span></td><td>{r['day']}-kun</td><td class='muted-td'>{esc(r['shadowing_ru'][:40])}...</td></tr>"
        for r in content
    )
    rows_users = "".join(
        f"<tr><td>{esc(u['first_name']) or '-'}</td><td><span class='badge'>{u['level'] or '—'}</span></td><td class='muted-td'>{str(u['last_active'])[:16]}</td></tr>"
        for u in users[:20]
    )

    audios_list = get_all_audios()
    rows_audios = "".join(
        f"<tr><td><span class='badge'>{a['level']}</span></td><td>{a['day']}</td>"
        f"<td>{AUDIO_SLOTS.get(a['slot'], a['slot'])}</td>"
        f"<td><audio controls preload='none' src=\"{esc(a['url'])}\" style='height:34px;max-width:170px;'></audio></td>"
        f"<td><button class='icon-btn' onclick=\"delAudio('{a['level']}',{a['day']},'{a['slot']}')\">🗑</button></td></tr>"
        for a in audios_list
    ) or "<tr><td colspan='5' class='muted-td'>Hali audio yuklanmagan</td></tr>"
    r2_warn = ("" if r2_configured() else
        "<div class='hint' style='color:#e0a800;'>⚠️ R2 hali sozlanmagan. Railway'da quyidagi env'larni qo'shing: "
        "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL.</div>")

    html = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Zamira Russian — Adminka</title>
<style>
:root{
  --bg:#0f0f0f; --card:#1a1a1a; --border:#333; --border-soft:#222;
  --text:#fff; --muted:#888; --input-bg:#111; --input-border:#444;
  --accent:#1D9E75; --accent-hover:#17856a; --accent-soft:#5DCAA5;
}
body.light{
  --bg:#f5f5f0; --card:#fff; --border:#e5e5df; --border-soft:#eee;
  --text:#1a1a1a; --muted:#888; --input-bg:#fff; --input-border:#d5d5cf;
  --accent:#1D9E75; --accent-hover:#17856a; --accent-soft:#0a5a40;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);padding:24px;transition:background .2s,color .2s;}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:8px;}
h1{font-size:22px;font-weight:700;}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px;}
.top-actions{display:flex;gap:8px;flex-shrink:0;}
.icon-btn{background:var(--card);border:0.5px solid var(--border);border-radius:10px;
  padding:8px 12px;color:var(--text);font-size:13px;cursor:pointer;width:auto;font-weight:500;}
.icon-btn:hover{background:var(--border-soft);}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:32px;}
.card{background:var(--card);border:0.5px solid var(--border);border-radius:12px;padding:16px;}
.card .num{font-size:28px;font-weight:700;color:var(--accent);}
.card .label{font-size:12px;color:var(--muted);margin-top:4px;}
.section{background:var(--card);border:0.5px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px;}
.section h2{font-size:16px;font-weight:600;margin-bottom:16px;}
label{font-size:13px;color:var(--muted);display:block;margin-bottom:6px;}
input,select,textarea{width:100%;background:var(--input-bg);border:0.5px solid var(--input-border);
  border-radius:8px;padding:10px 12px;font-size:14px;color:var(--text);margin-bottom:12px;font-family:inherit;}
input[type=file]{padding:8px;}
textarea{height:80px;resize:vertical;}
button{background:var(--accent);border:none;border-radius:8px;padding:12px 20px;color:#fff;
  font-size:14px;font-weight:600;cursor:pointer;width:100%;}
button:hover{background:var(--accent-hover);}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:var(--muted);padding:8px 0;border-bottom:0.5px solid var(--border);}
td{padding:8px 0;border-bottom:0.5px solid var(--border-soft);color:var(--text);}
.muted-td{color:var(--muted)!important;}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;background:rgba(29,158,117,0.2);color:var(--accent-soft);}
.msg{background:rgba(29,158,117,0.12);border:0.5px solid var(--accent);border-radius:8px;padding:12px;font-size:13px;color:var(--accent-soft);margin-bottom:12px;display:none;}
.seg{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;}
.seg-btn{flex:1;min-width:90px;background:var(--input-bg);border:0.5px solid var(--input-border);
  border-radius:8px;padding:10px;font-size:13px;color:var(--text);cursor:pointer;font-weight:500;width:auto;}
.seg-btn.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.hint{font-size:12px;color:var(--muted);margin:-4px 0 12px;line-height:1.5;}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>Zamira Russian</h1>
    <div class="sub">Adminka — faqat siz ko'rasiz</div>
  </div>
  <div class="top-actions">
    <button class="icon-btn" id="themeBtn" onclick="toggleTheme()">🌙 Qora</button>
    <button class="icon-btn" onclick="location.href='/admin/logout'">Chiqish</button>
  </div>
</div>

<div class="cards">
  <div class="card"><div class="num">""" + str(stats['total']) + """</div><div class="label">Jami foydalanuvchi</div></div>
  <div class="card"><div class="num">""" + str(stats['active_today']) + """</div><div class="label">Bugun faol</div></div>
  <div class="card"><div class="num">""" + str(len(content)) + """</div><div class="label">Jami darslar</div></div>
</div>

<div class="section">
  <h2>Kunlik avtomatik eslatma</h2>
  <div id="rm" class="msg">Saqlandi!</div>
  <label>Soat (0-23, Toshkent vaqti)</label>
  <input type="number" id="rh" min="0" max="23" value=\"""" + reminder_hour + """\" style="max-width:120px;">
  <label>Eslatma matni (har kuni shu soatda avtomatik boradi)</label>
  <textarea id="rt" placeholder="Bugungi darsni o'tdingizmi? 📚">""" + esc(reminder_text) + """</textarea>
  <button onclick="saveReminder()">Saqlash</button>
</div>

<div class="section">
  <h2>Xabar yuborish (hammaga)</h2>
  <div id="bm" class="msg">Yuborildi!</div>
  <div class="seg">
    <button class="seg-btn active" data-type="text" onclick="setBcType('text')">📝 Matn</button>
    <button class="seg-btn" data-type="audio" onclick="setBcType('audio')">🎵 Audio</button>
    <button class="seg-btn" data-type="video_note" onclick="setBcType('video_note')">⭕ Dumaloq video</button>
  </div>

  <div id="bc-text">
    <textarea id="bt" placeholder="Bugungi darsni o'tdingizmi? 📚"></textarea>
  </div>

  <div id="bc-audio" style="display:none;">
    <label>Audio fayl (mp3, m4a, ogg)</label>
    <input type="file" id="baf" accept="audio/*">
    <label>Izoh (ixtiyoriy)</label>
    <textarea id="bac" placeholder="Audio bilan birga matn (ixtiyoriy)"></textarea>
  </div>

  <div id="bc-video" style="display:none;">
    <label>Dumaloq video fayl (mp4)</label>
    <input type="file" id="bvf" accept="video/mp4,video/*">
    <div class="hint">Telegram dumaloq video uchun <b>kvadrat (1:1)</b> mp4 va ~1 daqiqagacha bo'lishi kerak. Aks holda video kesilib ko'rinishi mumkin.</div>
    <label>Izoh (ixtiyoriy — alohida xabar bo'lib boradi)</label>
    <textarea id="bvc" placeholder="Video tagidagi matn (ixtiyoriy)"></textarea>
  </div>

  <button onclick="sendBroadcast()">Yuborish</button>
</div>

<div class="section">
  <h2>Yangi dars qo'shish</h2>
  <div id="cm" class="msg">Qo'shildi!</div>
  <label>Daraja</label>
  <select id="cl"><option value="A0">A0</option><option value="A1" selected>A1</option><option value="B1">B1</option></select>
  <label>Kun raqami</label>
  <input type="number" id="cd" min="1" value="2">
  <label>Shadowing (ruscha)</label>
  <textarea id="csr" placeholder="— Как дела? — Хорошо!"></textarea>
  <label>Shadowing (o'zbekcha)</label>
  <textarea id="csu" placeholder="— Ishlar qanday? — Yaxshi!"></textarea>
  <label>Razgovor boshlash gapi</label>
  <input type="text" id="cr" placeholder="Привет! Откуда ты?">
  <label>Lug'at (har qatorda: ruscha|o'zbekcha|misol)</label>
  <textarea id="cv" placeholder="привет|salom|Привет!&#10;спасибо|rahmat|Спасибо!"></textarea>
  <button onclick="saveContent()">Qo'shish</button>
</div>

<div class="section">
  <h2>🎧 Audio yuklash (mashqlar uchun)</h2>
  <div id="am" class="msg">Yuklandi!</div>
  """ + r2_warn + """
  <label>Daraja</label>
  <select id="al"><option value="A0">A0</option><option value="A1" selected>A1</option><option value="B1">B1</option></select>
  <label>Kun</label>
  <input type="number" id="ad" min="1" value="1" style="max-width:120px;">
  <label>Bo'lim</label>
  <select id="aslot"><option value="audirov">Audirovaniye</option><option value="shadowing">Shadowing</option><option value="taqlid">Taqlid</option></select>
  <label>Audio fayl (mp3, m4a, ogg)</label>
  <input type="file" id="af" accept="audio/*">
  <button onclick="uploadAudio()">Yuklash</button>
</div>

<div class="section">
  <h2>Yuklangan audiolar</h2>
  <table><tr><th>Daraja</th><th>Kun</th><th>Bo'lim</th><th>Audio</th><th></th></tr>""" + rows_audios + """</table>
</div>

<div class="section">
  <h2>Mavjud darslar</h2>
  <table><tr><th>Daraja</th><th>Kun</th><th>Shadowing</th></tr>""" + rows_content + """</table>
</div>

<div class="section">
  <h2>Foydalanuvchilar</h2>
  <table><tr><th>Ism</th><th>Daraja</th><th>Oxirgi faollik</th></tr>""" + rows_users + """</table>
</div>

<script>
// --- THEME (qora/oq fon) ---
function applyTheme(t){
  var btn=document.getElementById('themeBtn');
  if(t==='light'){document.body.classList.add('light');btn.textContent='☀️ Oq';}
  else{document.body.classList.remove('light');btn.textContent='🌙 Qora';}
}
function toggleTheme(){
  var t=document.body.classList.contains('light')?'dark':'light';
  try{localStorage.setItem('admin_theme',t);}catch(e){}
  applyTheme(t);
}
(function(){var t='dark';try{t=localStorage.getItem('admin_theme')||'dark';}catch(e){}applyTheme(t);})();

// --- ESLATMA ---
async function saveReminder(){
  await fetch('/admin/set-reminder',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hour:document.getElementById('rh').value,text:document.getElementById('rt').value})});
  var m=document.getElementById('rm');m.style.display='block';setTimeout(()=>m.style.display='none',2000);
}

// --- BROADCAST ---
var bcType='text';
function setBcType(t){
  bcType=t;
  document.querySelectorAll('.seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.type===t));
  document.getElementById('bc-text').style.display=(t==='text')?'block':'none';
  document.getElementById('bc-audio').style.display=(t==='audio')?'block':'none';
  document.getElementById('bc-video').style.display=(t==='video_note')?'block':'none';
}
async function sendBroadcast(){
  if(!confirm('Hamma foydalanuvchiga yuborilsinmi?'))return;
  var m=document.getElementById('bm');
  if(bcType==='text'){
    var t=document.getElementById('bt').value.trim();
    if(!t)return alert('Xabar yozing');
    await fetch('/admin/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  }else{
    var fileEl=(bcType==='audio')?document.getElementById('baf'):document.getElementById('bvf');
    var capEl=(bcType==='audio')?document.getElementById('bac'):document.getElementById('bvc');
    if(!fileEl.files.length)return alert('Fayl tanlang');
    var fd=new FormData();
    fd.append('type',bcType);
    fd.append('caption',capEl.value.trim());
    fd.append('file',fileEl.files[0]);
    var btn=event.target;btn.disabled=true;btn.textContent='Yuborilyapti...';
    await fetch('/admin/broadcast-media',{method:'POST',body:fd});
    btn.disabled=false;btn.textContent='Yuborish';
  }
  m.style.display='block';setTimeout(()=>m.style.display='none',2500);
}

// --- AUDIO YUKLASH ---
async function uploadAudio(){
  var f=document.getElementById('af');
  if(!f.files.length)return alert('Fayl tanlang');
  var fd=new FormData();
  fd.append('level',document.getElementById('al').value);
  fd.append('day',document.getElementById('ad').value);
  fd.append('slot',document.getElementById('aslot').value);
  fd.append('file',f.files[0]);
  var btn=event.target;btn.disabled=true;btn.textContent='Yuklanyapti...';
  var r=await fetch('/admin/upload-audio',{method:'POST',body:fd});
  btn.disabled=false;btn.textContent='Yuklash';
  if(r.ok){var m=document.getElementById('am');m.style.display='block';setTimeout(()=>location.reload(),1200);}
  else{var j=await r.json().catch(()=>({}));alert('Xato: '+(j.error||r.status));}
}
async function delAudio(level,day,slot){
  if(!confirm(level+' '+day+'-kun audiosi o\\'chirilsinmi?'))return;
  await fetch('/admin/delete-audio',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({level:level,day:day,slot:slot})});
  location.reload();
}

// --- DARS ---
async function saveContent(){
  var vocab=document.getElementById('cv').value.trim().split('\\n').map(l=>{var p=l.split('|');return{ru:p[0]||'',uz:p[1]||'',ex:p[2]||''};});
  await fetch('/admin/add-content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    level:document.getElementById('cl').value,
    day:document.getElementById('cd').value,
    shadowRu:document.getElementById('csr').value,
    shadowUz:document.getElementById('csu').value,
    razgovor:document.getElementById('cr').value,
    vocab
  })});
  var m=document.getElementById('cm');m.style.display='block';setTimeout(()=>location.reload(),1500);
}
</script>
</body>
</html>"""
    return html

@flask_app.route("/admin/set-reminder", methods=["POST"])
def admin_set_reminder():
    check_api_auth()
    data = request.json or {}
    hour = str(data.get("hour", "9"))
    text = data.get("text", "").strip()
    set_setting("reminder_hour", hour)
    if text:
        set_setting("reminder_text", text)
    reschedule_reminder(int(hour))
    return {"ok": True}

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
    media_type = request.form.get("type", "")
    caption = request.form.get("caption", "").strip()
    f = request.files.get("file")
    if media_type not in ("audio", "video_note") or not f:
        return {"ok": False, "error": "fayl yoki tur noto'g'ri"}, 400
    ext = os.path.splitext(f.filename or "")[1] or (".mp4" if media_type == "video_note" else ".mp3")
    tmp_path = os.path.join(tempfile.gettempdir(), f"bcast_{uuid.uuid4().hex}{ext}")
    f.save(tmp_path)
    threading.Thread(target=send_media_broadcast_sync, args=(media_type, tmp_path, caption)).start()
    return {"ok": True}

@flask_app.route("/admin/add-content", methods=["POST"])
def admin_add_content():
    check_api_auth()
    data = request.json
    save_content(
        data["level"], int(data["day"]),
        data["shadowRu"], data["shadowUz"],
        json.dumps(data["vocab"], ensure_ascii=False),
        data["razgovor"]
    )
    return {"ok": True}

@flask_app.route("/admin/upload-audio", methods=["POST"])
def admin_upload_audio():
    check_api_auth()
    if not r2_configured():
        return {"ok": False, "error": "R2 sozlanmagan (env kalitlar yo'q)"}, 400
    level = request.form.get("level")
    day = request.form.get("day")
    slot = request.form.get("slot")
    f = request.files.get("file")
    if not (level and day and slot and f):
        return {"ok": False, "error": "ma'lumot to'liq emas"}, 400
    if slot not in AUDIO_SLOTS:
        return {"ok": False, "error": "bo'lim noto'g'ri"}, 400
    try:
        url = upload_audio_to_r2(f, level, int(day), slot)
        save_audio(level, int(day), slot, url)
    except Exception as e:
        logger.exception("R2 yuklashda xato")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "url": url}

@flask_app.route("/admin/delete-audio", methods=["POST"])
def admin_delete_audio():
    check_api_auth()
    data = request.json or {}
    if data.get("level") and data.get("day") is not None and data.get("slot"):
        delete_audio(data["level"], data["day"], data["slot"])
    return {"ok": True}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# --- BOT ---
bot_app = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    keyboard = [[InlineKeyboardButton("📚 Darsni boshlash", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"Salom, {user.first_name}! 👋\n\n"
        f"*Zamira Russian* — ruscha gapirish kursi.\n\n"
        f"Har kuni 10 daqiqa — 30 kunda begona bilan ruscha gaplasha olasiz. 🇷🇺",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 /start — Bosh sahifa")

# --- ESLATMALAR ---
def send_reminders():
    if not bot_app:
        return
    users = get_all_users()
    text = get_setting("reminder_text") or DEFAULT_REMINDER_TEXT
    async def _send():
        for user in users:
            try:
                await bot_app.bot.send_message(chat_id=user["user_id"], text=text)
            except Exception as e:
                logger.warning(f"Eslatma yuborilmadi {user['user_id']}: {e}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_send())
    loop.close()

def send_broadcast_sync(text):
    if not bot_app:
        return
    users = get_all_users()
    async def _send():
        for user in users:
            try:
                await bot_app.bot.send_message(chat_id=user["user_id"], text=text)
            except Exception as e:
                logger.warning(f"Xabar yuborilmadi {user['user_id']}: {e}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_send())
    loop.close()

def send_media_broadcast_sync(media_type, file_path, caption):
    """Audio yoki dumaloq video (video note) hammaga yuboradi.
    Birinchi yuborishdan file_id olinadi va qolganlarga shu file_id ishlatiladi (tezroq)."""
    if not bot_app:
        return
    users = get_all_users()
    async def _send():
        file_id = None
        for user in users:
            uid = user["user_id"]
            try:
                if media_type == "video_note":
                    if file_id is None:
                        with open(file_path, "rb") as fh:
                            msg = await bot_app.bot.send_video_note(chat_id=uid, video_note=fh)
                        file_id = msg.video_note.file_id
                    else:
                        await bot_app.bot.send_video_note(chat_id=uid, video_note=file_id)
                    if caption:
                        await bot_app.bot.send_message(chat_id=uid, text=caption)
                elif media_type == "audio":
                    if file_id is None:
                        with open(file_path, "rb") as fh:
                            msg = await bot_app.bot.send_audio(chat_id=uid, audio=fh,
                                                               caption=caption or None)
                        file_id = msg.audio.file_id
                    else:
                        await bot_app.bot.send_audio(chat_id=uid, audio=file_id,
                                                     caption=caption or None)
            except Exception as e:
                logger.warning(f"Media yuborilmadi {uid}: {e}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_send())
    finally:
        loop.close()
        try:
            os.remove(file_path)
        except Exception:
            pass

scheduler = BackgroundScheduler(timezone=TASHKENT_TZ)

def reschedule_reminder(hour):
    scheduler.remove_all_jobs()
    scheduler.add_job(send_reminders, "cron", hour=hour, minute=0)
    logger.info(f"Eslatma: soat {hour}:00 Toshkent")

def run_bot():
    global bot_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    logger.info("Bot ishga tushdi...")
    bot_app.run_polling()

if __name__ == "__main__":
    init_db()
    hour = int(get_setting("reminder_hour") or 9)
    reschedule_reminder(hour)
    scheduler.start()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server ishga tushdi")
    run_bot()
