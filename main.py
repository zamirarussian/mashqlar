import os
import threading
import logging
from datetime import datetime
import asyncio

from flask import Flask, send_from_directory, jsonify, request, abort
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
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

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
    """)
    cur.execute("""
        INSERT INTO settings (key, value) VALUES ('reminder_hour', '9')
        ON CONFLICT (key) DO NOTHING;
    """)
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

# --- FLASK ---
flask_app = Flask(__name__, static_folder=".")

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
    if row:
        return jsonify(dict(row))
    return {"error": "not found"}, 404

@flask_app.route("/admin")
def admin():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    stats = get_stats()
    content = get_all_content()
    reminder_hour = get_setting("reminder_hour") or "9"
    users = get_all_users()

    rows_content = "".join(
        f"<tr><td><span class='badge'>{r['level']}</span></td><td>{r['day']}-kun</td><td style='color:#888'>{r['shadowing_ru'][:40]}...</td></tr>"
        for r in content
    )
    rows_users = "".join(
        f"<tr><td>{u['first_name'] or '-'}</td><td><span class='badge'>{u['level'] or '—'}</span></td><td style='color:#888'>{str(u['last_active'])[:16]}</td></tr>"
        for u in users[:20]
    )

    html = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Zamira Russian — Adminka</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#fff;padding:24px;}
h1{font-size:22px;font-weight:700;margin-bottom:8px;}
.sub{color:#888;font-size:13px;margin-bottom:32px;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:32px;}
.card{background:#1a1a1a;border:0.5px solid #333;border-radius:12px;padding:16px;}
.card .num{font-size:28px;font-weight:700;color:#1D9E75;}
.card .label{font-size:12px;color:#888;margin-top:4px;}
.section{background:#1a1a1a;border:0.5px solid #333;border-radius:12px;padding:20px;margin-bottom:20px;}
.section h2{font-size:16px;font-weight:600;margin-bottom:16px;}
label{font-size:13px;color:#aaa;display:block;margin-bottom:6px;}
input,select,textarea{width:100%;background:#111;border:0.5px solid #444;border-radius:8px;padding:10px 12px;font-size:14px;color:#fff;margin-bottom:12px;font-family:inherit;}
textarea{height:80px;resize:vertical;}
button{background:#1D9E75;border:none;border-radius:8px;padding:12px 20px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;width:100%;}
button:hover{background:#17856a;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:#888;padding:8px 0;border-bottom:0.5px solid #333;}
td{padding:8px 0;border-bottom:0.5px solid #222;color:#ccc;}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;background:rgba(29,158,117,0.2);color:#5DCAA5;}
.msg{background:#1a3a2a;border:0.5px solid #1D9E75;border-radius:8px;padding:12px;font-size:13px;color:#5DCAA5;margin-bottom:12px;display:none;}
</style>
</head>
<body>
<h1>Zamira Russian</h1>
<div class="sub">Adminka — faqat siz ko'rasiz</div>

<div class="cards">
  <div class="card"><div class="num">""" + str(stats['total']) + """</div><div class="label">Jami foydalanuvchi</div></div>
  <div class="card"><div class="num">""" + str(stats['active_today']) + """</div><div class="label">Bugun faol</div></div>
  <div class="card"><div class="num">""" + str(len(content)) + """</div><div class="label">Jami darslar</div></div>
</div>

<div class="section">
  <h2>Eslatma vaqti (Toshkent)</h2>
  <div id="rm" class="msg">Saqlandi!</div>
  <label>Soat (0-23)</label>
  <input type="number" id="rh" min="0" max="23" value=\"""" + reminder_hour + """\" style="max-width:120px;">
  <button onclick="saveReminder()">Saqlash</button>
</div>

<div class="section">
  <h2>Xabar yuborish (hammaga)</h2>
  <textarea id="bt" placeholder="Bugungi darsni o'tdingizmi? 📚"></textarea>
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
  <h2>Mavjud darslar</h2>
  <table><tr><th>Daraja</th><th>Kun</th><th>Shadowing</th></tr>""" + rows_content + """</table>
</div>

<div class="section">
  <h2>Foydalanuvchilar</h2>
  <table><tr><th>Ism</th><th>Daraja</th><th>Oxirgi faollik</th></tr>""" + rows_users + """</table>
</div>

<script>
var s=new URLSearchParams(location.search).get('secret');
async function saveReminder(){
  await fetch('/admin/set-reminder?secret='+s+'&hour='+document.getElementById('rh').value,{method:'POST'});
  var m=document.getElementById('rm');m.style.display='block';setTimeout(()=>m.style.display='none',2000);
}
async function sendBroadcast(){
  var t=document.getElementById('bt').value.trim();
  if(!t)return alert('Xabar yozing');
  if(!confirm('Hamma foydalanuvchiga yuborilsinmi?'))return;
  await fetch('/admin/broadcast?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  alert('Yuborildi!');
}
async function saveContent(){
  var vocab=document.getElementById('cv').value.trim().split('\\n').map(l=>{var p=l.split('|');return{ru:p[0]||'',uz:p[1]||'',ex:p[2]||''};});
  await fetch('/admin/add-content?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
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
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    hour = request.args.get("hour", "9")
    set_setting("reminder_hour", hour)
    reschedule_reminder(int(hour))
    return {"ok": True}

@flask_app.route("/admin/broadcast", methods=["POST"])
def admin_broadcast():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    text = request.json.get("text", "")
    if text:
        threading.Thread(target=send_broadcast_sync, args=(text,)).start()
    return {"ok": True}

@flask_app.route("/admin/add-content", methods=["POST"])
def admin_add_content():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    import json
    data = request.json
    save_content(
        data["level"], int(data["day"]),
        data["shadowRu"], data["shadowUz"],
        json.dumps(data["vocab"], ensure_ascii=False),
        data["razgovor"]
    )
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
    text = "📚 Bugungi darsni o'tdingizmi?\n\nHar kuni 10 daqiqa — va ruscha gaplashishga yaqinlashasiz!"
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
