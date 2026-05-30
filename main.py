import os
import threading
import logging
import asyncio
import json

from flask import Flask, send_from_directory, jsonify, request, abort, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
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
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# ─── DATABASE ────────────────────────────────────────────────
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
            joined_at TIMESTAMP DEFAULT NOW(),
            last_active TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS access (
            user_id BIGINT NOT NULL,
            level TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT NOW(),
            granted_by BIGINT,
            expires_at TIMESTAMP DEFAULT NULL,
            active BOOLEAN DEFAULT TRUE,
            PRIMARY KEY (user_id, level)
        );

        CREATE TABLE IF NOT EXISTS content (
            id SERIAL PRIMARY KEY,
            level TEXT NOT NULL,
            day INTEGER NOT NULL,
            shadowing_ru TEXT NOT NULL,
            shadowing_uz TEXT NOT NULL,
            vocab JSONB NOT NULL DEFAULT '[]',
            razgovor_start TEXT NOT NULL DEFAULT 'Привет!',
            audio_file_id TEXT DEFAULT NULL,
            audio_label TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(level, day)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    cur.execute("INSERT INTO settings (key,value) VALUES ('reminder_hour','9') ON CONFLICT DO NOTHING;")
    # Default 1-kun kontent
    cur.execute("""
        INSERT INTO content (level, day, shadowing_ru, shadowing_uz, vocab, razgovor_start)
        VALUES (
            'A1', 1,
            '— Как дела? — Нормально, спасибо!',
            '— Ishlar qanday? — Yaxshi, rahmat!',
            '[{"ru":"привет","uz":"salom","ex":"Привет, как дела?"},{"ru":"спасибо","uz":"rahmat","ex":"Спасибо большое!"},{"ru":"пожалуйста","uz":"iltimos","ex":"Дай, пожалуйста."},{"ru":"да / нет","uz":"ha / yoq","ex":"Да, конечно. Нет."},{"ru":"хорошо","uz":"yaxshi","ex":"Всё хорошо!"}]',
            'Привет! Как тебя зовут?'
        ) ON CONFLICT DO NOTHING;
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("DB tayyor")

def save_user(user_id, first_name, username):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, first_name, username)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET last_active=NOW(), first_name=EXCLUDED.first_name;
    """, (user_id, first_name, username))
    conn.commit(); cur.close(); conn.close()

def get_user_access(user_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT level FROM access
        WHERE user_id=%s AND active=TRUE
        AND (expires_at IS NULL OR expires_at > NOW())
    """, (user_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [r["level"] for r in rows]

def grant_access(user_id, level, granted_by, days=None):
    conn = get_conn()
    cur = conn.cursor()
    if days:
        cur.execute("""
            INSERT INTO access (user_id, level, granted_by, expires_at)
            VALUES (%s, %s, %s, NOW() + INTERVAL '%s days')
            ON CONFLICT (user_id, level) DO UPDATE SET
                active=TRUE, granted_by=EXCLUDED.granted_by,
                expires_at=NOW() + INTERVAL '%s days', granted_at=NOW();
        """, (user_id, level, granted_by, days, days))
    else:
        cur.execute("""
            INSERT INTO access (user_id, level, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, level) DO UPDATE SET
                active=TRUE, granted_by=EXCLUDED.granted_by,
                expires_at=NULL, granted_at=NOW();
        """, (user_id, level, granted_by))
    conn.commit(); cur.close(); conn.close()

def revoke_access(user_id, level):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE access SET active=FALSE WHERE user_id=%s AND level=%s", (user_id, level))
    conn.commit(); cur.close(); conn.close()

def get_all_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT u.*, COALESCE(array_agg(a.level) FILTER (WHERE a.active=TRUE AND (a.expires_at IS NULL OR a.expires_at > NOW())), '{}') as levels
        FROM users u
        LEFT JOIN access a ON u.user_id = a.user_id
        GROUP BY u.user_id ORDER BY u.joined_at DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def get_stats():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM users")
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as active FROM users WHERE last_active > NOW() - INTERVAL '1 day'")
    active = cur.fetchone()["active"]
    cur.execute("SELECT COUNT(DISTINCT user_id) as paid FROM access WHERE active=TRUE")
    paid = cur.fetchone()["paid"]
    cur.close(); conn.close()
    return {"total": total, "active_today": active, "paid": paid}

def get_content(level, day):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content WHERE level=%s AND day=%s", (level, day))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def get_all_content():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content ORDER BY level, day")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def save_content(level, day, shadowing_ru, shadowing_uz, vocab_json, razgovor_start):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO content (level, day, shadowing_ru, shadowing_uz, vocab, razgovor_start)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (level, day) DO UPDATE SET
            shadowing_ru=EXCLUDED.shadowing_ru, shadowing_uz=EXCLUDED.shadowing_uz,
            vocab=EXCLUDED.vocab, razgovor_start=EXCLUDED.razgovor_start;
    """, (level, day, shadowing_ru, shadowing_uz, vocab_json, razgovor_start))
    conn.commit(); cur.close(); conn.close()

def save_audio(level, day, file_id, label):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE content SET audio_file_id=%s, audio_label=%s
        WHERE level=%s AND day=%s
    """, (file_id, label, level, day))
    conn.commit(); cur.close(); conn.close()

def get_setting(key):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))
    conn.commit(); cur.close(); conn.close()

# ─── FLASK ───────────────────────────────────────────────────
flask_app = Flask(__name__, static_folder=".")

@flask_app.route("/")
def index():
    return send_from_directory(".", "index.html")

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

@flask_app.route("/api/access")
def api_access():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return {"levels": []}, 200
    levels = get_user_access(user_id)
    return jsonify({"levels": levels})

@flask_app.route("/api/content")
def api_content():
    level = request.args.get("level", "A1")
    day = int(request.args.get("day", 1))
    row = get_content(level, day)
    if row:
        d = dict(row)
        if d.get("vocab") and isinstance(d["vocab"], str):
            d["vocab"] = json.loads(d["vocab"])
        return jsonify(d)
    return {"error": "not found"}, 404

@flask_app.route("/api/audio/<path:file_id>")
def api_audio_proxy(file_id):
    # Telegram file URL ni olib redirect qilamiz
    import urllib.request
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    with urllib.request.urlopen(url) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        return {"error": "not found"}, 404
    file_path = data["result"]["file_path"]
    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    with urllib.request.urlopen(audio_url) as r:
        audio_data = r.read()
    return Response(audio_data, mimetype="audio/mpeg")

# ─── ADMINKA ─────────────────────────────────────────────────
@flask_app.route("/admin")
def admin():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    stats = get_stats()
    users = get_all_users()
    content = get_all_content()
    reminder_hour = get_setting("reminder_hour") or "9"

    user_rows = ""
    for u in users:
        levels = u["levels"] if u["levels"] else []
        levels_clean = [l for l in levels if l]
        level_badges = " ".join(f"<span class='badge green'>{l}</span>" for l in levels_clean) or "<span style='color:#aaa'>—</span>"
        uid = u["user_id"]
        name = u["first_name"] or "—"
        uname = f"@{u['username']}" if u["username"] else "—"
        user_rows += f"""<tr>
            <td>{uid}</td>
            <td><b>{name}</b><br><span style='color:#888;font-size:11px'>{uname}</span></td>
            <td>{level_badges}</td>
            <td style='color:#888;font-size:11px'>{str(u['last_active'])[:16]}</td>
            <td>
              <select id='lvl_{uid}' style='font-size:12px;padding:4px;background:#1a1a1a;color:#fff;border:0.5px solid #444;border-radius:6px;'>
                <option value='A0'>A0</option>
                <option value='A1'>A1</option>
                <option value='B1'>B1</option>
              </select>
              <button onclick="grantAccess({uid})" style='margin-left:4px;padding:4px 10px;background:#1D9E75;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;'>Access ber</button>
              <button onclick="revokeAccess({uid})" style='margin-left:2px;padding:4px 10px;background:transparent;color:#e24b4a;border:0.5px solid #e24b4a;border-radius:6px;font-size:12px;cursor:pointer;'>Olish</button>
            </td>
        </tr>"""

    content_rows = ""
    for c in content:
        audio = "✓ Audio bor" if c.get("audio_file_id") else "— Yo'q"
        audio_color = "#1D9E75" if c.get("audio_file_id") else "#888"
        content_rows += f"<tr><td><span class='badge blue'>{c['level']}</span></td><td>{c['day']}-kun</td><td style='color:#888'>{c['shadowing_ru'][:35]}...</td><td style='color:{audio_color};font-size:12px'>{audio}</td></tr>"

    html = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Adminka — Rus Tili Har Kuni</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0a;color:#fff;display:flex;min-height:100vh;}
.sidebar{width:200px;background:#111;border-right:0.5px solid #222;padding:20px 0;flex-shrink:0;position:sticky;top:0;height:100vh;}
.logo{padding:0 16px 20px;border-bottom:0.5px solid #222;margin-bottom:12px;}
.logo div{font-size:14px;font-weight:600;}
.logo span{font-size:11px;color:#666;}
.nav-item{display:flex;align-items:center;gap:8px;padding:10px 16px;font-size:13px;color:#888;cursor:pointer;transition:all 0.15s;}
.nav-item:hover,.nav-item.active{background:#1a1a1a;color:#fff;}
.nav-item.active{border-right:2px solid #1D9E75;}
.main{flex:1;padding:24px;overflow-y:auto;}
.page{display:none;}
.page.active{display:block;}
h2{font-size:18px;font-weight:600;margin-bottom:20px;}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px;}
.metric{background:#1a1a1a;border-radius:10px;padding:16px;}
.metric .val{font-size:26px;font-weight:700;color:#1D9E75;}
.metric .lbl{font-size:12px;color:#888;margin-top:4px;}
.card{background:#111;border:0.5px solid #222;border-radius:12px;padding:20px;margin-bottom:16px;}
.card h3{font-size:14px;font-weight:600;margin-bottom:16px;color:#fff;}
label{font-size:12px;color:#aaa;display:block;margin-bottom:5px;}
input,select,textarea{width:100%;background:#1a1a1a;border:0.5px solid #333;border-radius:8px;padding:10px 12px;font-size:13px;color:#fff;margin-bottom:12px;font-family:inherit;}
textarea{height:70px;resize:vertical;}
.btn{padding:10px 18px;border-radius:8px;border:none;font-size:13px;font-weight:600;cursor:pointer;}
.btn-green{background:#1D9E75;color:#fff;width:100%;}
.btn-green:hover{background:#0F6E56;}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:500;margin-right:3px;}
.badge.green{background:rgba(29,158,117,0.2);color:#5DCAA5;}
.badge.blue{background:rgba(55,138,221,0.2);color:#85B7EB;}
.badge.amber{background:rgba(186,117,23,0.2);color:#EF9F27;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:#666;padding:8px;border-bottom:0.5px solid #222;font-weight:400;font-size:12px;}
td{padding:10px 8px;border-bottom:0.5px solid #1a1a1a;vertical-align:middle;}
.msg{background:#0F6E56;color:#fff;padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:13px;display:none;}
</style>
</head>
<body>
<div class="sidebar">
  <div class="logo"><div>Rus Tili Har Kuni</div><span>Admin panel</span></div>
  <div class="nav-item active" onclick="showPage('dashboard',this)">Dashboard</div>
  <div class="nav-item" onclick="showPage('users',this)">Foydalanuvchilar</div>
  <div class="nav-item" onclick="showPage('content',this)">Darslar</div>
  <div class="nav-item" onclick="showPage('notifs',this)">Xabarlar</div>
</div>
<div class="main">

  <div class="page active" id="page-dashboard">
    <h2>Dashboard</h2>
    <div class="metrics">
      <div class="metric"><div class="val">""" + str(stats['total']) + """</div><div class="lbl">Jami foydalanuvchi</div></div>
      <div class="metric"><div class="val">""" + str(stats['active_today']) + """</div><div class="lbl">Bugun faol</div></div>
      <div class="metric"><div class="val">""" + str(stats['paid']) + """</div><div class="lbl">Accessli users</div></div>
    </div>
    <div class="card"><h3>Tizim holati</h3><p style="color:#888;font-size:13px">Bot ishlayapti. Barcha tizimlar normal.</p></div>
  </div>

  <div class="page" id="page-users">
    <h2>Foydalanuvchilar</h2>
    <div id="access-msg" class="msg"></div>
    <div class="card" style="padding:0;overflow:hidden;">
      <table>
        <tr><th>ID</th><th>Ism</th><th>Daraja(lar)</th><th>Oxirgi faollik</th><th>Amal</th></tr>
        """ + user_rows + """
      </table>
    </div>
  </div>

  <div class="page" id="page-content">
    <h2>Darslar</h2>
    <div class="card">
      <h3>Yangi kun qo'shish</h3>
      <div id="content-msg" class="msg"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div>
          <label>Daraja</label>
          <select id="c-level"><option value="A0">A0</option><option value="A1" selected>A1</option><option value="B1">B1</option></select>
        </div>
        <div>
          <label>Kun raqami</label>
          <input type="number" id="c-day" min="1" value="2">
        </div>
      </div>
      <label>Shadowing (ruscha)</label>
      <textarea id="c-sru" placeholder="— Как дела? — Хорошо!"></textarea>
      <label>Shadowing (o'zbekcha)</label>
      <textarea id="c-suz" placeholder="— Ishlar qanday? — Yaxshi!"></textarea>
      <label>Razgovor boshlash gapi</label>
      <input type="text" id="c-raz" placeholder="Привет! Откуда ты?">
      <label>Lug'at (har qatorda: ruscha|o'zbekcha|misol)</label>
      <textarea id="c-voc" placeholder="привет|salom|Привет!&#10;спасибо|rahmat|Спасибо!"></textarea>
      <button class="btn btn-green" onclick="saveContent()">Saqlash</button>
    </div>

    <div class="card">
      <h3>Audio qo'shish (mavjud kunga)</h3>
      <p style="color:#888;font-size:12px;margin-bottom:12px;">Botga audio yuborish uchun: /upload_audio Level Day<br>Masalan: /upload_audio A1 1 — so'ng audio faylni yuboring</p>
      <table>
        <tr><th>Daraja</th><th>Kun</th><th>Shadowing</th><th>Audio</th></tr>
        """ + content_rows + """
      </table>
    </div>
  </div>

  <div class="page" id="page-notifs">
    <h2>Xabarlar</h2>
    <div class="card">
      <h3>Broadcast yuborish</h3>
      <label>Kimga</label>
      <select id="bc-filter">
        <option value="all">Hamma</option>
        <option value="active">Bugun faollar</option>
      </select>
      <label>Xabar matni</label>
      <textarea id="bc-text" placeholder="Bugungi darsni o'tdingizmi? 📚"></textarea>
      <button class="btn btn-green" onclick="sendBroadcast()">Yuborish</button>
    </div>
    <div class="card">
      <h3>Kunlik eslatma vaqti</h3>
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="number" id="rh" min="0" max="23" value=\"""" + reminder_hour + """\" style="width:80px;">
        <span style="color:#888;font-size:13px;">:00 Toshkent</span>
        <button class="btn btn-green" style="width:auto;padding:10px 16px;" onclick="saveReminder()">Saqlash</button>
      </div>
    </div>
  </div>

</div>
<script>
var s=new URLSearchParams(location.search).get('secret');
function showPage(id,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  el.classList.add('active');
}
function showMsg(id,text){
  var m=document.getElementById(id);
  m.textContent=text;m.style.display='block';
  setTimeout(()=>{m.style.display='none';},3000);
}
async function grantAccess(uid){
  var level=document.getElementById('lvl_'+uid).value;
  var r=await fetch('/admin/grant-access?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,level})});
  var d=await r.json();
  if(d.ok){showMsg('access-msg','✓ '+uid+' ga '+level+' daraja berildi');setTimeout(()=>location.reload(),2000);}
}
async function revokeAccess(uid){
  var level=document.getElementById('lvl_'+uid).value;
  if(!confirm(uid+' dan '+level+' darajasini olish?'))return;
  await fetch('/admin/revoke-access?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,level})});
  showMsg('access-msg','Access olindi');
  setTimeout(()=>location.reload(),2000);
}
async function saveContent(){
  var vocab=document.getElementById('c-voc').value.trim().split('\\n').map(l=>{var p=l.split('|');return{ru:p[0]||'',uz:p[1]||'',ex:p[2]||''};});
  var r=await fetch('/admin/add-content?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    level:document.getElementById('c-level').value,
    day:document.getElementById('c-day').value,
    shadowRu:document.getElementById('c-sru').value,
    shadowUz:document.getElementById('c-suz').value,
    razgovor:document.getElementById('c-raz').value,
    vocab
  })});
  var d=await r.json();
  if(d.ok){showMsg('content-msg','✓ Dars saqlandi!');setTimeout(()=>location.reload(),2000);}
}
async function saveReminder(){
  await fetch('/admin/set-reminder?secret='+s+'&hour='+document.getElementById('rh').value,{method:'POST'});
  alert('Saqlandi!');
}
async function sendBroadcast(){
  var text=document.getElementById('bc-text').value.trim();
  if(!text)return alert('Xabar yozing');
  if(!confirm('Yuborilsinmi?'))return;
  await fetch('/admin/broadcast?secret='+s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,filter:document.getElementById('bc-filter').value})});
  alert('Yuborildi!');
}
</script>
</body>
</html>"""
    return html

@flask_app.route("/admin/grant-access", methods=["POST"])
def admin_grant_access():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    data = request.json
    grant_access(data["user_id"], data["level"], granted_by=0)
    # Userga xabar yuborish
    threading.Thread(target=notify_user_access, args=(data["user_id"], data["level"])).start()
    return {"ok": True}

@flask_app.route("/admin/revoke-access", methods=["POST"])
def admin_revoke_access():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    data = request.json
    revoke_access(data["user_id"], data["level"])
    return {"ok": True}

@flask_app.route("/admin/add-content", methods=["POST"])
def admin_add_content():
    if request.args.get("secret") != ADMIN_SECRET:
        abort(403)
    data = request.json
    save_content(data["level"], int(data["day"]), data["shadowRu"], data["shadowUz"],
                 json.dumps(data["vocab"], ensure_ascii=False), data["razgovor"])
    return {"ok": True}

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
    data = request.json
    text = data.get("text", "")
    if text:
        threading.Thread(target=send_broadcast_sync, args=(text,)).start()
    return {"ok": True}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# ─── BOT ────────────────────────────────────────────────────
bot_app = None
pending_audio = {}  # admin_id -> {level, day}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    levels = get_user_access(user.id)
    if not levels:
        await update.message.reply_text(
            f"Salom, {user.first_name}! 👋\n\n"
            f"*Zamira Russian* kursiga xush kelibsiz!\n\n"
            f"Kursga kirish uchun to'lov qiling va adminga murojaat qiling.",
            parse_mode="Markdown"
        )
        return
    keyboard = [[InlineKeyboardButton("📚 Darsni boshlash", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"Salom, {user.first_name}! 👋\n\n"
        f"Sizning darajalaringiz: *{', '.join(levels)}*\n\n"
        f"Har kuni 10 daqiqa — 30 kunda ruscha gaplasha olasiz! 🇷🇺",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def upload_audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Ruxsat yo'q.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Format: /upload_audio LEVEL DAY\nMasalan: /upload_audio A1 1\nSo'ng audio faylni yuboring.")
        return
    level = args[0].upper()
    try:
        day = int(args[1])
    except:
        await update.message.reply_text("Kun raqami noto'g'ri.")
        return
    pending_audio[user.id] = {"level": level, "day": day}
    await update.message.reply_text(f"✓ Tayyor. {level} daraja, {day}-kun uchun audio yuboring.")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    if user.id not in pending_audio:
        await update.message.reply_text("Avval /upload_audio LEVEL DAY buyrug'ini yuboring.")
        return
    audio = update.message.audio or update.message.voice
    if not audio:
        await update.message.reply_text("Audio fayl yuboring.")
        return
    info = pending_audio.pop(user.id)
    file_id = audio.file_id
    label = getattr(audio, "file_name", None) or f"{info['level']} {info['day']}-kun audio"
    save_audio(info["level"], info["day"], file_id, label)
    await update.message.reply_text(f"✓ Audio saqlandi!\nDaraja: {info['level']}, Kun: {info['day']}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 /start — Bosh sahifa\n/upload_audio LEVEL DAY — Audio yuklash (admin)")

# ─── ESLATMALAR ──────────────────────────────────────────────
def notify_user_access(user_id, level):
    if not bot_app:
        return
    async def _send():
        try:
            keyboard = [[InlineKeyboardButton("📚 Darsni boshlash", web_app=WebAppInfo(url=WEBAPP_URL))]]
            await bot_app.bot.send_message(
                chat_id=user_id,
                text=f"🎉 Tabriklaymiz!\n\n*{level}* daraja uchun sizga access berildi.\n\nHar kuni 10 daqiqa mashq qiling!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.warning(f"Access xabari yuborilmadi {user_id}: {e}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_send())
    loop.close()

def send_reminders():
    if not bot_app:
        return
    users = get_all_users()
    text = "📚 Bugungi darsni o'tdingizmi?\n\nHar kuni 10 daqiqa — ruscha gaplashishga yaqinlashasiz!"
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
    bot_app.add_handler(CommandHandler("upload_audio", upload_audio_cmd))
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    logger.info("Bot ishga tushdi...")
    bot_app.run_polling()

# ─── MAIN ────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    hour = int(get_setting("reminder_hour") or 9)
    reschedule_reminder(hour)
    scheduler.start()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server ishga tushdi")
    run_bot()
