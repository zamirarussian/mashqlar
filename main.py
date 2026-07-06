import os
import threading
import logging
import json
import hmac
import hashlib
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import asyncio

from flask import (
    Flask, send_from_directory, jsonify, request, abort,
    session, redirect, url_for
)
from werkzeug.security import generate_password_hash, check_password_hash
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
    "link_teacher": "zamira_russian",
    "link_ai_bot": "zamira_suhbatdosh_bot",
    "link_admin": "zamira_russian",
    "text_invite": "Men «Zamira Russian» ilovasida ruscha o'rganyapman 🇷🇺 Sen ham qo'shil!",
    "text_share": "",
    "bot_welcome": "Salom, {ism}! 👋\n\n*Zamira Russian* — ruscha gapirish kursi.\n\nKursga kirish uchun o'qituvchiga murojaat qiling va shu ID ni yuboring:\n🆔 `{id}`",
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

def upload_pdf_to_r2(data, level, day):
    import time
    key = f"pdfs/{level}/{day}.pdf"
    get_r2_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType="application/pdf")
    return f"{R2_PUBLIC_URL}/{key}?v={int(time.time())}"

def upload_material_to_r2(data, level, day, section):
    import time
    key = f"materials/{level}/{day}/{section}.pdf"
    get_r2_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType="application/pdf")
    return f"{R2_PUBLIC_URL}/{key}?v={int(time.time())}"

def save_lesson_file(level, day, filename, url):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO lesson_files (level, day, filename, url, uploaded_at)
        VALUES (%s,%s,%s,%s, NOW())
        ON CONFLICT (level, day) DO UPDATE SET filename=EXCLUDED.filename, url=EXCLUDED.url, uploaded_at=NOW()""",
        (level, day, filename, url))
    conn.commit(); cur.close(); conn.close()

def get_lesson_files():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT level, day, filename, url, uploaded_at FROM lesson_files ORDER BY level, day")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def delete_lesson_file(level, day):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM lesson_files WHERE level=%s AND day=%s", (level, int(day)))
    conn.commit(); cur.close(); conn.close()

def save_material(level, day, section, filename, url):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO materials (level, day, section, filename, url, uploaded_at)
        VALUES (%s,%s,%s,%s,%s, NOW())
        ON CONFLICT (level, day, section) DO UPDATE SET filename=EXCLUDED.filename, url=EXCLUDED.url, uploaded_at=NOW()""",
        (level, int(day), section, filename, url))
    conn.commit(); cur.close(); conn.close()

def get_materials(level, day):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT section, filename, url FROM materials WHERE level=%s AND day=%s", (level, int(day)))
    rows = cur.fetchall(); cur.close(); conn.close()
    return {r["section"]: {"url": r["url"], "filename": r["filename"]} for r in rows}

def delete_material(level, day, section):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM materials WHERE level=%s AND day=%s AND section=%s", (level, int(day), section))
    conn.commit(); cur.close(); conn.close()

def get_videos(section=None):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    if section:
        cur.execute("SELECT * FROM video_lessons WHERE section=%s ORDER BY sort_order, id", (section,))
    else:
        cur.execute("SELECT * FROM video_lessons ORDER BY section, sort_order, id")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def add_video(section):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM video_lessons WHERE section=%s", (section,))
    so = cur.fetchone()[0]
    cur.execute("INSERT INTO video_lessons (section, title, sort_order) VALUES (%s,%s,%s) RETURNING id",
                (section, "Yangi dars", so))
    vid = cur.fetchone()[0]; conn.commit(); cur.close(); conn.close(); return vid

def save_videos(lessons):
    conn = get_conn(); cur = conn.cursor()
    for i, l in enumerate(lessons):
        try:
            vid = int(l.get("id"))
        except Exception:
            continue
        cur.execute("""UPDATE video_lessons SET title=%s, descr=%s, youtube_url=%s, sort_order=%s WHERE id=%s""",
                    (l.get("title"), l.get("descr"), l.get("youtube_url"), i, vid))
    conn.commit(); cur.close(); conn.close()

def delete_video(vid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM video_lessons WHERE id=%s", (vid,))
    conn.commit(); cur.close(); conn.close()

def set_video_task(vid, url):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE video_lessons SET task_pdf_url=%s WHERE id=%s", (url, vid))
    conn.commit(); cur.close(); conn.close()

def upload_video_task_to_r2(data, vid):
    import time
    key = f"vidtasks/{vid}.pdf"
    get_r2_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType="application/pdf")
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
    "Formulalar uchun ham 2 tadan misol (ex1ru/ex1uz/ex2ru/ex2uz) o'zing tuz. "
    "writing_tasks — PDF dagi HAR BIR yozma topshiriq (ЗАДАНИЕ / Письмо bo'limi) uchun alohida obyekt; PDF da nechta topshiriq bo'lsa shuncha (bittasini emas, hammasini ol). "
    "Har biri: ru — topshiriq matni RUS tilida (PDF dagidek, tarjima qilma), uz — o'sha topshiriqning o'zbekcha tarjimasi, "
    "sample — shu topshiriqqa darajaga mos namuna javob (rus tilida; PDF da tayyor javob/образец bo'lsa o'shani ol, bo'lmasa o'zing yoz). "
    "speaking_questions: title — PDF dagi savol (rus tilida); answer — PDF da shu savolga berilgan namunaviy javob (rus tilida); "
    "agar PDF da tayyor javob bo'lsa o'shani ol, bo'lmasa darajaga mos qisqa (2-3 gap) namuna javob yoz; desc — kerak bo'lsa qisqa izoh (uz), aks holda bo'sh. "
    "audio_questions — audio matniga oid 7 ta tushunish savoli; har biriga 3-4 ta variant (options) va correct (to'g'ri variantning tartib raqami, 1 dan boshlab). "
    "formulas — darsga mos ANIQ 10 ta tayyor ibora/formula ber. Agar PDF da kam bo'lsa, o'sha dars mavzusiga mos qo'shimcha iboralar o'ylab topib 10 taga yetkaz (kam ham, ko'p ham emas — roppa-rosa 10 ta). "
    "reading_tasks — o'qish matniga oid 4-6 ta Правда/Не правда gapi (rus tilida); har biriga answer: '1' = Правда, '0' = Не правда. "
    "dialog — mavzu bo'yicha qisqa tabiiy suhbat (6-10 qator); har qator: sp — 'A' yoki 'B', ru — ruscha gap, uz — o'zbekcha tarjima. "
    "sin/ant/nsv/sv mos kelmasa bo'sh qoldir. Bo'lim umuman bo'lmasa bo'sh ro'yxat [] qoldir. "
    "Agar daraja B1 bo'lsa, har lug'at so'ziga kontekst mashqi qo'sh: kx_q — so'z ishlatilgan tabiiy rus gapi, "
    "lekin o'sha so'z o'rniga ___ (uchta pastki chiziq) qo'yilgan; kx_a — bo'sh joyga to'g'ri keladigan so'z (gapdagi shakli); "
    "kx — 3 ta yaqin, lekin noto'g'ri variant (rus tilida, bir xil turkum). A1 uchun kx_q/kx_a ni bo'sh, kx ni [] qoldir. JSON sxemasi:\n"
    '{"title":"dars nomi","shadowing_ru":"","shadowing_uz":"","razgovor_start":"",'
    '"vocab":[{"ru":"","stress":"","uz":"","ex1ru":"","ex1uz":"","ex2ru":"","ex2uz":"","sin":"","ant":"","nsv":"","sv":"","kx_q":"","kx_a":"","kx":[]}],'
    '"formulas":[{"ru":"","uz":"","ex1ru":"","ex1uz":"","ex2ru":"","ex2uz":""}],'
    '"reading_texts":[{"level":"","ru":"","uz":""}],'
    '"grammar":[{"title":"","sub":"","base":"","res":"","example":""}],'
    '"speaking_questions":[{"title":"","desc":"","format":"","answer":""}],'
    '"audio_questions":[{"q":"","options":["",""],"correct":"1"}],'
    '"writing_tasks":[{"ru":"","uz":"","sample":""}],'
    '"reading_tasks":[{"q":"","answer":"1"}],'
    '"dialog":[{"sp":"A","ru":"","uz":""}]}'
)

def extract_pdf_text(file_storage):
    import io
    from pypdf import PdfReader
    data = file_storage.read()
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def _extract_json(text):
    import re
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*", "", t).rsplit("```", 1)[0].strip()
    a = t.find("{"); z = t.rfind("}")
    if a >= 0 and z > a:
        t = t[a:z + 1]
    return t

def _try_json(text):
    import re
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    fixed = re.sub(r",(\s*[}\]])", r"\1", text)  # ortiqcha vergullar
    try:
        return json.loads(fixed)
    except Exception:
        return None

def ai_generate_lesson(pdf_text, level, day):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=AI_MODEL, max_tokens=16000, system=AI_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Daraja: {level}, {day}-kun. FAQAT to'liq va to'g'ri JSON qaytar "
                              f"(ortiqcha vergulsiz, ``` belgisisiz, izohsiz). "
                              f"Quyidagi matndan darsni tayyorla:\n\n{pdf_text[:60000]}"}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    raw = _extract_json(text)
    data = _try_json(raw)
    if data is not None:
        return data
    # Buzuq JSON — AI ga to'g'rilashni so'raymiz (bir marta)
    rep = client.messages.create(
        model=AI_MODEL, max_tokens=16000,
        system="Sen JSON tuzatuvchisan. Faqat to'g'ri, to'liq JSON qaytar. Izoh yoki ``` yozma.",
        messages=[{"role": "user", "content": "Quyidagi JSON buzuq yoki to'liq emas. To'g'rila va FAQAT to'g'ri JSON qaytar:\n\n" + raw}]
    )
    rtext = "".join(getattr(b, "text", "") for b in rep.content).strip()
    data = _try_json(_extract_json(rtext))
    if data is not None:
        return data
    raise ValueError("AI to'g'ri JSON qaytarmadi. Qayta urinib ko'ring (yoki PDF ni soddalashtiring).")

def get_stored_pdf_text(level, day):
    if not r2_configured():
        return None
    import io
    from pypdf import PdfReader
    key = f"pdfs/{level}/{int(day)}.pdf"
    try:
        obj = get_r2_client().get_object(Bucket=R2_BUCKET, Key=key)
        data = obj["Body"].read()
        reader = PdfReader(io.BytesIO(data))
        txt = "\n".join((p.extract_text() or "") for p in reader.pages)
        return txt if txt.strip() else None
    except Exception:
        return None

AI_TOPUP_SYSTEM = (
    "Sen rus tili darsligini boyituvchisan. Berilgan PDF matnidan IKKI narsani ALOHIDA ajratib JSON qaytar.\n"
    "1) dialog — PDF dagi «Диалог» jadvali (Реплика/Перевод ustunlari) yoki suhbat qatorlari. "
    "HAR bir replika alohida qator bo'lsin. sp — navbatma-navbat 'A' va 'B' (1-qator A, 2-qator B, 3-qator A...). "
    "ru — replikaning ruscha matni (boshidagi «—» chiziqchasiz), uz — shu qatorning o'zbekcha tarjimasi. "
    "Suhbat/dialog qatorlarini FAQAT shu 'dialog' ro'yxatiga qo'y — formulaga MUTLAQO qo'yma.\n"
    "2) formulas — dars mavzusiga oid tayyor IBORALAR / nutq formulalari, ANIQ 10 ta. "
    "Bular suhbat replikasi EMAS — alohida, qayta ishlatiladigan foydali iboralar. "
    "Har biri: ru (ruscha ibora), uz (o'zbekcha tarjima), ex1ru (qisqa misol gap), ex1uz (misol tarjimasi). "
    "PDF da 10 ta formula bo'lmasa, mavzuga mos iboralar o'ylab topib roppa-rosa 10 taga yetkaz.\n"
    "Faqat to'g'ri JSON qaytar (``` yoki izohsiz). Sxema:\n"
    '{"dialog":[{"sp":"A","ru":"","uz":""}],"formulas":[{"ru":"","uz":"","ex1ru":"","ex1uz":""}]}'
)
def ai_topup(pdf_text, level, day):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=AI_MODEL, max_tokens=4000, system=AI_TOPUP_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Daraja: {level}, {day}-kun. Shu matndan 10 ta formula va dialog tayyorla. FAQAT JSON:\n\n{pdf_text[:40000]}"}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    data = _try_json(_extract_json(text))
    if data is None:
        raise ValueError("AI to'g'ri JSON qaytarmadi.")
    return data

# --- DATABASE (connection pool) ---
_db_pool = None
def _get_pool():
    global _db_pool
    if _db_pool is None:
        from psycopg2.pool import ThreadedConnectionPool
        _db_pool = ThreadedConnectionPool(1, 15, DATABASE_URL, sslmode="require")
    return _db_pool

class _PooledConn:
    """get_conn()/conn.close() eski kod o'zgarmasin uchun:
    close() ulanishni YOPMAYDI, balki pulga QAYTARADI."""
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._returned = False
    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)
    def commit(self):
        return self._conn.commit()
    def rollback(self):
        return self._conn.rollback()
    def close(self):
        if self._returned:
            return
        self._returned = True
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
    def __getattr__(self, name):
        return getattr(self._conn, name)

def get_conn():
    pool = _get_pool()
    return _PooledConn(pool, pool.getconn())

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
        CREATE TABLE IF NOT EXISTS admins (
            username TEXT PRIMARY KEY,
            pwd_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'teacher',
            created_at TIMESTAMP DEFAULT NOW()
        );
        ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_start TIMESTAMP;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_sections TEXT DEFAULT '';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_until TIMESTAMP;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS permanent BOOLEAN DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen DATE;
        CREATE TABLE IF NOT EXISTS lesson_files (
            level TEXT NOT NULL, day INTEGER NOT NULL,
            filename TEXT, url TEXT, uploaded_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (level, day)
        );
        CREATE TABLE IF NOT EXISTS materials (
            level TEXT NOT NULL, day INTEGER NOT NULL, section TEXT NOT NULL,
            filename TEXT, url TEXT, uploaded_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (level, day, section)
        );
        CREATE TABLE IF NOT EXISTS user_activity (
            user_id BIGINT NOT NULL, d DATE NOT NULL,
            PRIMARY KEY (user_id, d)
        );
        CREATE TABLE IF NOT EXISTS user_completions (
            user_id BIGINT NOT NULL, level TEXT NOT NULL, day INTEGER NOT NULL,
            done_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, level, day)
        );
        CREATE TABLE IF NOT EXISTS video_lessons (
            id SERIAL PRIMARY KEY,
            section TEXT NOT NULL,
            title TEXT, descr TEXT,
            youtube_url TEXT, task_pdf_url TEXT, task_text TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id SERIAL PRIMARY KEY,
            text TEXT, btns TEXT,
            send_at TIMESTAMP, sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    # Yangi kontent ustunlari (mavjud bazaga ham xavfsiz qo'shiladi)
    for col in ["title TEXT DEFAULT ''", "formulas JSONB DEFAULT '[]'",
                "grammar JSONB DEFAULT '[]'", "reading_texts JSONB DEFAULT '[]'",
                "audio_questions JSONB DEFAULT '[]'", "speaking_questions JSONB DEFAULT '[]'",
                "enabled_tasks JSONB", "grammar_video TEXT DEFAULT ''",
                "writing_task TEXT DEFAULT ''", "writing_sample TEXT DEFAULT ''",
                "taqlid_video TEXT DEFAULT ''", "taqlid_type TEXT DEFAULT ''",
                "writing_tasks JSONB DEFAULT '[]'", "reading_tasks JSONB DEFAULT '[]'",
                "dialog JSONB DEFAULT '[]'"]:
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
    cur.execute("SELECT COUNT(*) FROM video_lessons")
    if cur.fetchone()[0] == 0:
        seed = [
            ("talaffuz", "1-dars — Unlilar", "а, е, ё, и, о, у, ы, э, ю, я"),
            ("talaffuz", "2-dars — Undoshlar", "Qiyin undoshlar: ж, ш, щ, ч, ц"),
            ("talaffuz", "3-dars — Urg'u", "So'zlarda urg'u qoidalari"),
            ("talaffuz", "4-dars — Intonatsiya", "Gap intonatsiyasi"),
            ("fundament", "Rod (jinsi)", "Erkak, ayol va o'rta jins"),
            ("fundament", "Zamonlar", "O'tgan, hozirgi, kelasi zamon"),
            ("fundament", "Padejlar", "6 ta padej — asosiy tushuncha"),
            ("fundament", "Kelishiklar", "So'z o'zgarishi qoidalari"),
            ("fundament", "Fe'l tuslanishi", "1-chi va 2-chi tuslash"),
            ("fundament", "Olmoshlar", "Men, sen, u, biz, siz, ular"),
            ("fundament", "Sifat moslashuvi", "Sifatning jins va kelishikka moslashuvi"),
        ]
        for i, (sec, t, d) in enumerate(seed):
            cur.execute("INSERT INTO video_lessons (section, title, descr, sort_order) VALUES (%s,%s,%s,%s)",
                        (sec, t, d, i))
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

def get_broadcast_users():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE COALESCE(bot_blocked, FALSE)=FALSE ORDER BY joined_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def mark_bot_blocked(uid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET bot_blocked=TRUE WHERE user_id=%s", (uid,))
    conn.commit(); cur.close(); conn.close()

def get_stats():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM users"); total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as active FROM users WHERE last_active > NOW() - INTERVAL '1 day'")
    active = cur.fetchone()["active"]
    cur.close(); conn.close()
    return {"total": total, "active_today": active}

SECTIONS = ["talaffuz", "fundament", "a1", "b1"]

def trial_days():
    try:
        return int(get_setting("trial_days") or 3)
    except Exception:
        return 3

def get_user(uid):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

def ensure_user_trial(uid, first_name, username):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE user_id=%s", (uid,))
    created = cur.fetchone() is None
    cur.execute("""INSERT INTO users (user_id, first_name, username)
        VALUES (%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET last_active=NOW(),
            first_name=COALESCE(EXCLUDED.first_name, users.first_name),
            username=COALESCE(EXCLUDED.username, users.username)""",
        (uid, first_name, username))
    conn.commit(); cur.close(); conn.close()
    return created

def bump_streak(uid):
    today = datetime.now(pytz.timezone("Asia/Tashkent")).date()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT streak, last_seen FROM users WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    streak = (row[0] or 0) if row else 0
    last = row[1] if row else None
    if last == today:
        pass
    elif last == today - timedelta(days=1):
        streak += 1
    else:
        streak = 1
    cur.execute("UPDATE users SET streak=%s, last_seen=%s WHERE user_id=%s", (streak, today, uid))
    cur.execute("INSERT INTO user_activity (user_id, d) VALUES (%s,%s) ON CONFLICT DO NOTHING", (uid, today))
    conn.commit(); cur.close(); conn.close()
    return streak

def mark_day_complete(uid, level, day):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO user_completions (user_id, level, day) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (uid, level, int(day)))
    conn.commit(); cur.close(); conn.close()

def get_completed_days(uid, level):
    lv = "B1" if str(level).upper().startswith("B") else "A1"
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT day FROM user_completions WHERE user_id=%s AND level=%s ORDER BY day", (uid, lv))
    days = [int(r[0]) for r in cur.fetchall()]
    cur.close(); conn.close()
    return days

def get_profile_stats(uid):
    tz = pytz.timezone("Asia/Tashkent")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_completions WHERE user_id=%s", (uid,))
    completed_days = cur.fetchone()[0] or 0
    cur.execute("""SELECT COALESCE(SUM(COALESCE(jsonb_array_length(c.vocab),0)),0)
        FROM user_completions uc JOIN content c ON c.level=uc.level AND c.day=uc.day
        WHERE uc.user_id=%s""", (uid,))
    learned_words = int(cur.fetchone()[0] or 0)
    cur.execute("SELECT joined_at FROM users WHERE user_id=%s", (uid,))
    r = cur.fetchone()
    total_days = 1
    if r and r[0]:
        try:
            total_days = (datetime.now(tz).date() - r[0].date()).days + 1
        except Exception:
            total_days = 1
    cur.execute("SELECT d FROM user_activity WHERE user_id=%s ORDER BY d", (uid,))
    active_dates = [str(x[0]) for x in cur.fetchall()]
    cur.execute("SELECT level, COUNT(*) FROM user_completions WHERE user_id=%s GROUP BY level", (uid,))
    per = {row[0]: row[1] for row in cur.fetchall()}
    cur.close(); conn.close()
    return {"completed_days": completed_days, "learned_words": learned_words,
            "total_days": max(total_days, 1), "active_dates": active_dates,
            "completed_a1": per.get("A1", 0), "completed_b1": per.get("B1", 0)}

_BOT_USERNAME = None
def get_bot_username():
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        try:
            import urllib.request, json as _j
            with urllib.request.urlopen("https://api.telegram.org/bot" + BOT_TOKEN + "/getMe", timeout=5) as resp:
                _BOT_USERNAME = _j.load(resp)["result"]["username"]
        except Exception:
            _BOT_USERNAME = ""
    return _BOT_USERNAME

def send_welcome(uid, first_name):
    try:
        import urllib.request, urllib.parse, json as _j
        ui = get_ui()
        tpl = ui.get("bot_welcome") or DEFAULT_UI["bot_welcome"]
        adm = (ui.get("link_admin") or "zamira_russian").lstrip("@").strip()
        text = tpl.replace("{ism}", first_name or "").replace("{id}", str(uid))
        kb = {"inline_keyboard": [[{"text": "\u270D\uFE0F Adminga yozish", "url": "https://t.me/" + adm}]]}
        data = urllib.parse.urlencode({"chat_id": uid, "text": text, "parse_mode": "Markdown",
                                       "reply_markup": _j.dumps(kb)}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage", data=data, timeout=5)
    except Exception as e:
        print("send_welcome error:", e)

def get_available_days(level):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT day, title FROM content WHERE level=%s AND (
        COALESCE(jsonb_array_length(vocab),0)>0 OR
        COALESCE(jsonb_array_length(grammar),0)>0 OR
        COALESCE(jsonb_array_length(reading_texts),0)>0 OR
        COALESCE(jsonb_array_length(formulas),0)>0 OR
        COALESCE(jsonb_array_length(speaking_questions),0)>0 OR
        COALESCE(jsonb_array_length(audio_questions),0)>0 OR
        COALESCE(title,'')<>'' OR COALESCE(shadowing_ru,'')<>''
    ) ORDER BY day""", (level,))
    days = {r["day"]: (r["title"] or "") for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT day FROM audios WHERE level=%s", (level,))
    for r in cur.fetchall():
        days.setdefault(r["day"], "")
    cur.close(); conn.close()
    return [{"day": d, "title": days[d]} for d in sorted(days)]

def compute_access(row):
    if get_setting("access_enabled") == "0":
        return {"sections": list(SECTIONS), "status": "open", "days_left": None}
    now = datetime.utcnow()
    if not row:
        return {"sections": [], "status": "expired", "days_left": 0}
    if row.get("blocked"):
        return {"sections": [], "status": "blocked", "days_left": 0}
    ts = row.get("trial_start")
    if ts:
        end = ts + timedelta(days=trial_days())
        if now < end:
            return {"sections": list(SECTIONS), "status": "trial",
                    "days_left": (end - now).days + 1}
    permanent = bool(row.get("permanent"))
    su = row.get("sub_until")
    sub_active = permanent or (su and now < su)
    allowed = [s for s in (row.get("allowed_sections") or "").split(",") if s]
    if sub_active and allowed:
        return {"sections": allowed,
                "status": "permanent" if permanent else "paid",
                "days_left": None if permanent else ((su - now).days + 1)}
    return {"sections": [], "status": "expired", "days_left": 0}

def verify_init_data(init_data):
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv = parsed.pop("hash", None)
    if not recv:
        return None
    dcs = "\n".join("%s=%s" % (k, parsed[k]) for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None
    user = {}
    if parsed.get("user"):
        try:
            user = json.loads(parsed["user"])
        except Exception:
            user = {}
    return user

def set_user_access(uid, sections, mode, until):
    secs = ",".join(s for s in sections if s in SECTIONS)
    conn = get_conn(); cur = conn.cursor()
    if mode == "permanent":
        cur.execute("UPDATE users SET allowed_sections=%s, permanent=TRUE, sub_until=NULL, trial_start=NULL, blocked=FALSE WHERE user_id=%s", (secs, uid))
    elif mode == "dated":
        cur.execute("UPDATE users SET allowed_sections=%s, permanent=FALSE, sub_until=%s, trial_start=NULL, blocked=FALSE WHERE user_id=%s", (secs, until, uid))
    elif mode == "none":
        cur.execute("UPDATE users SET allowed_sections='', permanent=FALSE, sub_until=NULL, trial_start=NULL, blocked=FALSE WHERE user_id=%s", (uid,))
    else:
        cur.execute("UPDATE users SET allowed_sections=%s, permanent=FALSE, sub_until=NULL, trial_start=NOW(), blocked=FALSE WHERE user_id=%s", (secs, uid))
    conn.commit(); cur.close(); conn.close()

def set_user_blocked(uid, blocked):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET blocked=%s WHERE user_id=%s", (bool(blocked), uid))
    conn.commit(); cur.close(); conn.close()

def extend_trial(uid, days):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET trial_start = NOW() - (%s || ' days')::interval, blocked=FALSE WHERE user_id=%s",
                (str(trial_days() - int(days)), uid))
    conn.commit(); cur.close(); conn.close()

def get_content(level, day):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM content WHERE level=%s AND day=%s", (level, int(day)))
    row = cur.fetchone(); cur.close(); conn.close(); return row

def get_all_content():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT level, day, title FROM content ORDER BY level, day")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def get_lessons_status():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT level, day, title,
        COALESCE(jsonb_array_length(vocab),0) as nvocab,
        COALESCE(jsonb_array_length(grammar),0) as ngrammar
        FROM content ORDER BY level, day""")
    rows = cur.fetchall()
    cur.execute("SELECT DISTINCT level, day FROM audios")
    auds = set((r["level"], r["day"]) for r in cur.fetchall())
    cur.close(); conn.close()
    for r in rows:
        r["has_audio"] = (r["level"], r["day"]) in auds
    return rows

def save_content_full(level, day, d):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO content (level, day, title, shadowing_ru, shadowing_uz, razgovor_start,
            vocab, formulas, grammar, reading_texts, audio_questions, speaking_questions, enabled_tasks,
            grammar_video, writing_task, writing_sample, taqlid_video, taqlid_type, writing_tasks, reading_tasks, dialog)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
        ON CONFLICT (level, day) DO UPDATE SET
            title=EXCLUDED.title, shadowing_ru=EXCLUDED.shadowing_ru, shadowing_uz=EXCLUDED.shadowing_uz,
            razgovor_start=EXCLUDED.razgovor_start, vocab=EXCLUDED.vocab, formulas=EXCLUDED.formulas,
            grammar=EXCLUDED.grammar, reading_texts=EXCLUDED.reading_texts,
            audio_questions=EXCLUDED.audio_questions, speaking_questions=EXCLUDED.speaking_questions,
            enabled_tasks=EXCLUDED.enabled_tasks, grammar_video=EXCLUDED.grammar_video,
            writing_task=EXCLUDED.writing_task, writing_sample=EXCLUDED.writing_sample,
            taqlid_video=EXCLUDED.taqlid_video, taqlid_type=EXCLUDED.taqlid_type,
            writing_tasks=EXCLUDED.writing_tasks, reading_tasks=EXCLUDED.reading_tasks, dialog=EXCLUDED.dialog;
    """, (level, int(day), d.get("title", ""), d.get("shadowing_ru", ""), d.get("shadowing_uz", ""),
          d.get("razgovor_start", ""),
          json.dumps(d.get("vocab", []), ensure_ascii=False),
          json.dumps(d.get("formulas", []), ensure_ascii=False),
          json.dumps(d.get("grammar", []), ensure_ascii=False),
          json.dumps(d.get("reading_texts", []), ensure_ascii=False),
          json.dumps(d.get("audio_questions", []), ensure_ascii=False),
          json.dumps(d.get("speaking_questions", []), ensure_ascii=False),
          json.dumps(d.get("enabled_tasks"), ensure_ascii=False),
          d.get("grammar_video", "") or "", d.get("writing_task", "") or "", d.get("writing_sample", "") or "",
          d.get("taqlid_video", "") or "", d.get("taqlid_type", "") or "",
          json.dumps(d.get("writing_tasks", []), ensure_ascii=False),
          json.dumps(d.get("reading_tasks", []), ensure_ascii=False),
          json.dumps(d.get("dialog", []), ensure_ascii=False)))
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

def get_admins():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT username, role, created_at FROM admins ORDER BY created_at")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def add_admin_db(username, password, role):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO admins (username, pwd_hash, role) VALUES (%s,%s,%s)
        ON CONFLICT (username) DO UPDATE SET pwd_hash=EXCLUDED.pwd_hash, role=EXCLUDED.role""",
        (username, generate_password_hash(password), role))
    conn.commit(); cur.close(); conn.close()

def delete_admin_db(username):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE username=%s", (username,))
    conn.commit(); cur.close(); conn.close()

def find_admin(username):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT pwd_hash, role FROM admins WHERE username=%s", (username,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row  # (pwd_hash, role) yoki None

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

def check_owner():
    if not session.get("admin") or session.get("role", "owner") != "owner":
        abort(403)

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
        # 1) Egasi (env orqali)
        if hmac.compare_digest(u, ADMIN_USER) and hmac.compare_digest(p, ADMIN_PASSWORD):
            session["admin"] = True; session["role"] = "owner"; session["uname"] = u
            return redirect(url_for("admin"))
        # 2) Qo'shilgan adminlar (ustozlar) — bazadan
        row = find_admin(u)
        if row and check_password_hash(row[0], p):
            session["admin"] = True; session["role"] = row[1]; session["uname"] = u
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

def _vocab_source_day(day):
    # Lug'at avtomatik takror: takror kuni qaysi yangi mavzu kunidan lug'at oladi.
    # (day-1)%7: 0=Dush(yangi),1=Sesh,2=Chor,3=Pay(yangi),4=Juma,5=Shan,6=Yaksh
    r = (int(day) - 1) % 7
    if r in (1, 2):
        return int(day) - r          # Sesh/Chor -> Dushanba
    if r in (4, 5):
        return int(day) - (r - 3)    # Juma/Shan -> Payshanba
    return int(day)                  # Dush/Pay/Yaksh -> o'zi


@flask_app.route("/api/content")
def api_content():
    level = request.args.get("level", "A1"); day = int(request.args.get("day", 1))
    # Admin (editor) — to'g'ridan-to'g'ri ruxsat. Aks holda — dostup tekshiriladi.
    if not session.get("admin"):
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        user = verify_init_data(init_data)
        if user is None or not user.get("id"):
            return jsonify({"error": "auth"}), 401
        acc = compute_access(get_user(int(user["id"])))
        sec = "b1" if str(level).upper().startswith("B1") else "a1"
        if sec not in acc["sections"]:
            return jsonify({"error": "locked"}), 403
    row = get_content(level, day); audios = get_audios_for(level, day)
    mats = get_materials(level, day)
    d = dict(row) if row else {"level": level, "day": day}
    d["audios"] = audios; d["materials"] = mats
    # Lug'at avtomatik takror — faqat foydalanuvchi ilovasi uchun (admin editor emas).
    # Takror kunda (Sesh/Chor, Juma/Shan) o'z lug'ati bo'sh bo'lsa, manba kundan (Dush/Pay) tortib beramiz.
    if not session.get("admin"):
        src = _vocab_source_day(day)
        if src != day:
            srow = get_content(level, src)
            if srow and srow.get("vocab"):
                d["vocab"] = srow["vocab"]
                et = d.get("enabled_tasks")
                if et is not None and "vocab" not in et:
                    d["enabled_tasks"] = list(et) + ["vocab"]
            d["vocab_review"] = True
            d["vocab_source_day"] = src
    return jsonify(d)

@flask_app.route("/api/access", methods=["POST"])
def api_access():
    d = request.json or {}
    user = verify_init_data(d.get("initData") or "")
    if user is None or not user.get("id"):
        # Test rejimi: adminkaga kirgan brauzerda to'liq dostup
        if session.get("admin"):
            return jsonify({"ok": True, "sections": list(SECTIONS), "status": "test",
                            "days_left": None, "user_id": "test", "streak": 0})
        return jsonify({"ok": False, "error": "auth"}), 401
    uid = int(user["id"])
    created = ensure_user_trial(uid, user.get("first_name"), user.get("username"))
    if created:
        threading.Thread(target=send_welcome, args=(uid, user.get("first_name")), daemon=True).start()
    acc = compute_access(get_user(uid))
    acc["ok"] = True; acc["user_id"] = uid
    acc["streak"] = bump_streak(uid)
    acc.update(get_profile_stats(uid))
    try:
        acc["avail_a1"] = len(get_available_days("A1"))
        acc["avail_b1"] = len(get_available_days("B1"))
    except Exception:
        acc["avail_a1"] = 0; acc["avail_b1"] = 0
    acc["bot_username"] = get_bot_username()
    return jsonify(acc)

@flask_app.route("/api/progress", methods=["POST"])
def api_progress():
    d = request.json or {}
    user = verify_init_data(d.get("initData") or "")
    if user is None or not user.get("id"):
        return jsonify({"ok": True, "skip": True})
    uid = int(user["id"])
    lvl = (d.get("level") or "").strip().upper()
    lvl = "B1" if lvl.startswith("B") else "A1"
    try:
        day = int(d.get("day"))
    except Exception:
        return jsonify({"ok": False, "error": "day"}), 400
    mark_day_complete(uid, lvl, day)
    return jsonify({"ok": True})

@flask_app.route("/api/days")
def api_days():
    level = request.args.get("level", "A1")
    done = []
    if not session.get("admin"):
        user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if user is None or not user.get("id"):
            return jsonify({"error": "auth"}), 401
        acc = compute_access(get_user(int(user["id"])))
        sec = "b1" if str(level).upper().startswith("B1") else "a1"
        if sec not in acc["sections"]:
            return jsonify({"error": "locked"}), 403
        try:
            done = get_completed_days(int(user["id"]), level)
        except Exception:
            done = []
    return jsonify({"days": get_available_days(level), "done": done})

@flask_app.route("/api/leaderboard")
def api_leaderboard():
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user is None or not user.get("id"):
        return jsonify({"error": "auth"}), 401
    uid = int(user["id"])
    metric = (request.args.get("metric") or "streak").strip()
    if metric not in ("streak", "days", "words"):
        metric = "streak"
    conn = get_conn(); cur = conn.cursor()
    if metric == "days":
        cur.execute("""SELECT u.user_id, u.first_name, COUNT(*) AS val
            FROM users u JOIN user_completions uc ON uc.user_id=u.user_id
            WHERE COALESCE(u.blocked,FALSE)=FALSE
            GROUP BY u.user_id, u.first_name
            ORDER BY val DESC, u.user_id""")
    elif metric == "words":
        cur.execute("""SELECT u.user_id, u.first_name,
              COALESCE(SUM(COALESCE(jsonb_array_length(c.vocab),0)),0) AS val
            FROM users u JOIN user_completions uc ON uc.user_id=u.user_id
            JOIN content c ON c.level=uc.level AND c.day=uc.day
            WHERE COALESCE(u.blocked,FALSE)=FALSE
            GROUP BY u.user_id, u.first_name
            ORDER BY val DESC, u.user_id""")
    else:
        cur.execute("""SELECT u.user_id, u.first_name, COALESCE(u.streak,0) AS val
            FROM users u
            WHERE COALESCE(u.blocked,FALSE)=FALSE AND COALESCE(u.streak,0)>0
            ORDER BY val DESC, u.user_id""")
    rows = cur.fetchall(); cur.close(); conn.close()
    top = []; me = None
    for i, r in enumerate(rows):
        rank = i + 1
        entry = {"rank": rank, "name": (r[1] or "—"), "val": int(r[2] or 0), "me": int(r[0]) == uid}
        if len(top) < 20:
            top.append(entry)
        if int(r[0]) == uid:
            me = entry
    return jsonify({"metric": metric, "top": top, "me": me})

@flask_app.route("/api/videos")
def api_videos():
    section = request.args.get("section", "talaffuz")
    if not session.get("admin"):
        user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if user is None or not user.get("id"):
            return jsonify({"error": "auth"}), 401
        acc = compute_access(get_user(int(user["id"])))
        if section not in acc["sections"]:
            return jsonify({"error": "locked"}), 403
    vids = get_videos(section)
    out = [{"id": v["id"], "title": v["title"], "descr": v["descr"],
            "youtube_url": v["youtube_url"], "task_pdf_url": v["task_pdf_url"],
            "task_text": v["task_text"]} for v in vids]
    return jsonify({"videos": out})

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
    role = session.get("role", "owner")
    access_on = (get_setting("access_enabled") != "0")
    admins = get_admins() if role == "owner" else []
    rows_admins = "".join(
        "<div class='lrow2'><div style='flex:1'><b>%s</b> <span class='muted'>· %s</span></div>"
        "<button class='icon-btn' onclick=\"delAdmin('%s')\">🗑</button></div>"
        % (esc(a['username']), 'Egasi' if a['role'] == 'owner' else 'Ustoz', esc(a['username']))
        for a in admins) or "<div class='muted' style='padding:8px'>Hali ustoz qo'shilmagan</div>"
    files = get_lesson_files()
    n_a1 = sum(1 for x in files if str(x['level']).upper().startswith('A'))
    n_b1 = sum(1 for x in files if str(x['level']).upper().startswith('B'))
    _ftiles = "".join(
        "<div class='ftile frow' data-fl='%s'><div class='ftnum'>%s-kun</div>"
        "<div class='ftname'>%s</div><div class='ftbtns'>"
        "<a class='ftview' href='%s' target='_blank'>Ko'rish</a>"
        "<button class='ftdel' onclick=\"delFile('%s',%s)\">🗑</button></div></div>"
        % (esc(x['level']), x['day'], esc(x.get('filename') or '—'),
           esc(x.get('url') or '#'), esc(x['level']), x['day'])
        for x in files)
    rows_files = ("<div id='filesGrid' class='fgrid'>" + _ftiles + "</div>") if files else "<div class='muted' style='padding:8px;font-size:13px'>Hali fayl yuklanmagan</div>"
    sched = get_scheduled_pending()
    rows_sched = "".join(
        "<div class='lrow2' style='padding:9px 12px'><div style='flex:1'><b>%s</b> · %s</div>"
        "<button class='btn-sm' style='color:#A32D2D' onclick='cancelSched(%s)'>Bekor</button></div>"
        % (str(s["send_at"])[:16], esc((s.get("text") or "")[:40]), s["id"])
        for s in sched) or "<div class='muted' style='padding:8px;font-size:13px'>Rejalashtirilgan xabar yo'q</div>"

    lessons = get_lessons_status()
    def _mini(c):
        m = ("<span class='lmc'>Lug'at %d</span>" % c['nvocab']) if c['nvocab'] else "<span class='lmc off'>Lug'at yo'q</span>"
        m += "<span class='lmc ok'>Audio</span>" if c['has_audio'] else "<span class='lmc off'>Audio yo'q</span>"
        m += ("<span class='lmc'>Gram %d</span>" % c['ngrammar']) if c['ngrammar'] else "<span class='lmc off'>Gram yo'q</span>"
        return m
    def _tile(c):
        title = esc(c.get('title')) or ("%d-kun" % c['day'])
        lk2 = "B1" if str(c['level']).upper().startswith('B') else "A1"
        return ("<div class='ltile' data-day='%d'><div class='ltnum'>%d</div>"
                "<div class='ltt'>%s</div><div class='ltm'>%s</div>"
                "<a class='ltedit' href='/admin/lesson?level=%s&day=%d' onclick=\"lsave(%d,'%s')\">Tahrirlash</a></div>"
                % (c['day'], c['day'], title, _mini(c), c['level'], c['day'], c['day'], lk2))
    _by = {"A1": [], "B1": []}
    for c in lessons:
        lk = "B1" if str(c['level']).upper().startswith('B') else "A1"
        _by.setdefault(lk, []).append(c)
    _blocks = []
    _first = True
    for lk, items in _by.items():
        items.sort(key=lambda x: x['day'])
        if items:
            _weeks = {}
            for c in items:
                _weeks.setdefault((c['day'] - 1) // 7 + 1, []).append(c)
            wk_html = ""
            for w in sorted(_weeks):
                days = _weeks[w]
                wk_html += ("<div class='lweek'><div class='lwh'><span class='lwn'>%d-hafta</span>"
                            "<span class='lwl'></span><span class='lwc'>%d dars</span></div>"
                            "<div class='lgrid'>%s</div></div>"
                            % (w, len(days), "".join(_tile(c) for c in days)))
        else:
            wk_html = "<div class='muted' style='padding:10px'>Bu darajada dars yo'q</div>"
        _blocks.append("<div class='lvl-block' data-level='%s' style='%s'>%s</div>"
                       % (lk, "" if _first else "display:none", wk_html))
        _first = False
    rows_lessons = "".join(_blocks)

    SEC_LABEL = {"talaffuz": "Talaffuz", "fundament": "Fundament", "a1": "A1", "b1": "B1"}
    def _user_row(u):
        acc = compute_access(u)
        uid = u["user_id"]
        name = esc(u.get("first_name")) or "—"
        un = ("@" + esc(u["username"])) if u.get("username") else ""
        granted = [s for s in (u.get("allowed_sections") or "").split(",") if s]
        if u.get("permanent"):
            mode, until = "permanent", ""
        elif u.get("sub_until"):
            mode, until = "dated", str(u["sub_until"])[:10]
        else:
            mode, until = "trial", ""
        st = acc["status"]; dl = acc.get("days_left")
        cm = {
            "trial": ("#E6F1FB", "#0C447C", "Trial" + (" · %d kun" % dl if dl else "")),
            "paid": ("#E1F5EE", "#0F6E56", "To'langan" + (" · %d kun" % dl if dl else "")),
            "permanent": ("#E1F5EE", "#0F6E56", "Doimiy"),
            "expired": ("#FCEBEB", "#A32D2D", "Tugagan"),
            "blocked": ("#F1EFE8", "#5F5E5A", "Bloklangan"),
        }
        bg, fg, lbl = cm.get(st, cm["expired"])
        secchips = "".join("<span class='sec %s'>%s</span>" % ("on" if s in granted else "off", SEC_LABEL[s]) for s in SECTIONS)
        nm = name.replace('"', '&quot;')
        return (
            "<div class='lrow2' data-uid='%s' data-name=\"%s\" data-secs='%s' data-mode='%s' data-until='%s' data-search='%s %s %s'>"
            "<div style='flex:1;min-width:150px'><div style='font-weight:500'>%s <span class='muted' style='font-weight:400'>%s</span></div>"
            "<div class='muted' style='font-size:12px'>ID: %s</div>"
            "<div style='margin-top:6px'>%s</div></div>"
            "<span class='st' style='background:%s;color:%s'>%s</span>"
            "<button class='btn-sm' onclick='openAccess(this)'>⚙️ Dostup</button></div>"
            % (uid, nm, ",".join(granted), mode, until, name.lower(), un.lower(), uid, name, un, uid, secchips, bg, fg, lbl)
        )
    rows_users = "".join(_user_row(u) for u in users[:300]) or "<div class='muted' style='padding:10px'>Foydalanuvchi yo'q</div>"

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
.lrow2{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 16px;margin-bottom:10px;}
.daybadge{width:40px;height:40px;border-radius:10px;background:rgba(29,158,117,.2);color:var(--accs);display:flex;align-items:center;justify-content:center;font-weight:600;flex-shrink:0;}
.chip{font-size:11px;padding:2px 8px;border-radius:7px;margin-right:4px;display:inline-block;margin-top:2px;}
.chip.on{background:rgba(29,158,117,.2);color:var(--accs);}
.chip.off{background:var(--soft);color:var(--muted);}
.seg2{display:inline-flex;border:1px solid var(--border);border-radius:9px;overflow:hidden;margin-bottom:14px;}
.seg2b{border:none;border-radius:0;padding:8px 22px;background:var(--ibg);color:var(--text);cursor:pointer;width:auto;}
.seg2b.on{background:var(--acc);color:#fff;}
.seg2b.on[data-lv="A1"]{background:#1f54e0;}
.seg2b.on[data-lv="B1"]{background:#e42a3b;}
#lessonList{--lacc:#1f54e0;--lacc-d:#16308f;--lacc-soft:#e8eefc;}
.lweek{margin-bottom:22px;}
.lwh{display:flex;align-items:center;gap:10px;margin-bottom:11px;}
.lwn{font-size:12.5px;font-weight:800;color:var(--lacc);letter-spacing:.04em;text-transform:uppercase;}
.lwl{flex:1;height:1px;background:var(--border);}
.lwc{font-size:11px;color:#9aa3b5;font-weight:600;}
.lgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;}
.ltile{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:13px;display:flex;flex-direction:column;min-height:150px;}
.ltnum{width:34px;height:34px;border-radius:9px;background:var(--lacc-soft);color:var(--lacc);font-weight:800;font-size:15px;display:flex;align-items:center;justify-content:center;margin-bottom:9px;}
.ltt{font-size:13px;font-weight:700;line-height:1.32;margin-bottom:9px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:34px;}
.ltm{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:11px;}
.lmc{font-size:10.5px;font-weight:600;border-radius:6px;padding:3px 7px;background:var(--lacc-soft);color:var(--lacc-d);}
.lmc.off{background:var(--ibg);color:#aab2c2;}
.lmc.ok{background:#e7f6ee;color:#1e8a55;}
.ltedit{margin-top:auto;border:none;background:var(--lacc);color:#fff;font-size:12.5px;font-weight:700;padding:8px;border-radius:9px;cursor:pointer;text-align:center;text-decoration:none;display:block;}
.ftab.on-a1{background:#1f54e0;color:#fff;}
.ftab.on-b1{background:#e42a3b;color:#fff;}
#filesGrid{--facc:#1f54e0;--facc-soft:#e8eefc;display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;}
.ftile{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:10px;display:flex;flex-direction:column;}
.ftnum{font-weight:800;font-size:13px;color:var(--facc);margin-bottom:4px;}
.ftname{font-size:11px;color:#9aa3b5;margin-bottom:9px;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:28px;}
.ftbtns{display:flex;gap:6px;margin-top:auto;}
.ftview{flex:1;text-align:center;font-size:11px;font-weight:700;padding:6px;border-radius:8px;background:var(--facc-soft);color:var(--facc);text-decoration:none;}
.ftdel{border:none;background:var(--ibg);color:#c0392b;border-radius:8px;padding:6px 9px;cursor:pointer;font-size:12px;}
.sec{font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;margin-right:4px;display:inline-block;margin-top:2px;}
.sec.on{background:rgba(29,158,117,.2);color:var(--accs);}
.sec.off{background:var(--soft);color:var(--muted);}
.st{font-size:11px;font-weight:600;padding:3px 10px;border-radius:7px;white-space:nowrap;}
.acc-tog{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--soft);}
.acc-r{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.accrow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid var(--soft);font-size:14px;cursor:pointer;}
.accrow.last{border-bottom:none;}
.accbox{width:20px!important;height:20px;flex:none;margin:0;accent-color:var(--acc);cursor:pointer;}
.accdate{width:auto!important;max-width:150px;padding:6px 8px;font-size:13px;margin:0;}
.vcard{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-bottom:12px;}
.vcard input{margin-bottom:8px;}
.vrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;}
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
  <button class="nav-item" data-panel="videos" onclick="nav(this)">🎬 Video kurslar</button>
  <button class="nav-item" data-owner="1" data-panel="users" onclick="nav(this)">👥 Foydalanuvchilar</button>
  <button class="nav-item" data-owner="1" data-panel="broadcast" onclick="nav(this)">✉️ Xabar yuborish</button>
  <button class="nav-item" data-owner="1" data-panel="segments" onclick="nav(this);loadSegments();">📨 Eslatmalar</button>
  <button class="nav-item" data-owner="1" data-panel="ui" onclick="nav(this)">🎨 Interfeys matnlari</button>
  <button class="nav-item" data-owner="1" data-panel="admins" onclick="nav(this)">🛡️ Adminlar</button>
  <button class="nav-item" data-owner="1" data-panel="settings" onclick="nav(this)">⚙️ Sozlamalar</button>
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
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-bottom:16px;">
      <div class="section" style="margin:0;">
        <h2>🤖 AI bilan to'ldirish</h2>""" + ai_warn + """
        <div class="row2"><div><label>Daraja</label><select id="ai_l"><option selected>A1</option><option>B1</option></select></div><div style="max-width:90px"><label>Kun</label><input type="number" id="ai_d" min="1" value="1"></div></div>
        <label>PDF fayl</label><input type="file" id="ai_f" accept="application/pdf">
        <button class="act" id="aiBtn" onclick="aiFill()">🤖 To'ldirish</button>
        <button class="btn-sm" id="aiCancel" onclick="aiCancel()" style="display:none;">Bekor qilish</button>
        <div class="hint" style="margin-top:12px;font-weight:600;">📎 Yuklangan fayllar</div>
        <div style="display:flex;gap:8px;margin:8px 0;">
          <button class="btn-sm ftab" id="ftab-A1" onclick="fileTab('A1')">A1 (""" + str(n_a1) + """)</button>
          <button class="btn-sm ftab" id="ftab-B1" onclick="fileTab('B1')">B1 (""" + str(n_b1) + """)</button>
        </div>
        """ + rows_files + """
      </div>
      <div class="section" style="margin:0;">
        <h2>✏️ Qo'lda qo'shish / tahrirlash</h2>
        <div class="row2"><div><label>Daraja</label><select id="nl"><option selected>A1</option><option>B1</option></select></div><div style="max-width:90px"><label>Kun</label><input type="number" id="nd" min="1" value="1"></div></div>
        <div class="hint">Mavjud bo'lsa ochadi, bo'lmasa yangi yaratadi.</div>
        <button class="act" onclick="openLesson()">Ochish →</button>
      </div>
    </div>
    <div class="seg2"><button class="seg2b on" data-lv="A1" onclick="filterLevel(this)">A1</button><button class="seg2b" data-lv="B1" onclick="filterLevel(this)">B1</button></div>
    <div id="lessonList">""" + rows_lessons + """</div>
  </div>

  <div class="panel" id="p-videos">
    <h1>Video kurslar</h1>
    <div id="vm" class="msg">Saqlandi!</div>
    <div class="seg2"><button class="seg2b on" data-vsec="talaffuz" onclick="vTab(this)">Talaffuz</button><button class="seg2b" data-vsec="fundament" onclick="vTab(this)">Fundament</button></div>
    <div class="hint">YouTube havola va PDF vazifa qo'shing. Tartibni o'zgartirsangiz, <b>Saqlash</b>ni bosing.</div>
    <div id="videoList"></div>
    <button class="act" onclick="vAdd()">+ Yangi dars qo'shish</button>
    <button class="act" style="background:#1D9E75;color:#fff;" onclick="vSave()">💾 Saqlash</button>
  </div>

  <div class="panel" id="p-users">
    <h1>Foydalanuvchilar</h1>
    <div style="display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:9px 12px;margin-bottom:14px;">
      🔍 <input oninput="userSearch(this.value)" placeholder="Ism, @username yoki ID bo'yicha qidirish" style="border:none;flex:1;background:transparent;padding:2px;">
    </div>
    """ + rows_users + """
    <div id="accModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:60;align-items:center;justify-content:center;padding:16px;">
      <div style="background:var(--card);border:1px solid var(--border);border-radius:18px;max-width:430px;width:100%;overflow:hidden;">
        <div style="padding:18px 20px 14px;border-bottom:1px solid var(--soft);">
          <div id="acc-name" style="font-weight:600;font-size:17px;"></div>
          <div id="acc-id" class="muted" style="font-size:12px;margin-top:2px;"></div>
        </div>
        <div style="padding:16px 20px;">
          <div class="muted" style="font-size:11px;letter-spacing:.05em;font-weight:600;margin-bottom:8px;">BO'LIMLAR — qaysi kurslar ochiq</div>
          <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:18px;">
            <label class="accrow"><span>🗣️ Talaffuz</span><input type="checkbox" id="acc-talaffuz" class="accbox"></label>
            <label class="accrow"><span>🏛️ Fundament</span><input type="checkbox" id="acc-fundament" class="accbox"></label>
            <label class="accrow"><span>🟢 A1 — A2</span><input type="checkbox" id="acc-a1" class="accbox"></label>
            <label class="accrow last"><span>🔵 B1 — B2</span><input type="checkbox" id="acc-b1" class="accbox"></label>
          </div>
          <div class="muted" style="font-size:11px;letter-spacing:.05em;font-weight:600;margin-bottom:8px;">MUDDAT</div>
          <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;">
            <label class="accrow"><span>🆓 Trial — """ + str(trial_days()) + """ kun bepul</span><input type="radio" name="accmode" value="trial" class="accbox"></label>
            <label class="accrow"><span style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">📅 Sana gacha <input type="date" id="acc-until" class="accdate" onclick="document.querySelector('input[name=accmode][value=dated]').checked=true;"></span><input type="radio" name="accmode" value="dated" class="accbox"></label>
            <label class="accrow"><span>♾️ Doimiy (muddatsiz)</span><input type="radio" name="accmode" value="permanent" class="accbox"></label>
            <label class="accrow last"><span style="color:#c0392b;font-weight:500;">🔒 Dostup yo'q — hammasi yopiq</span><input type="radio" name="accmode" value="none" class="accbox"></label>
          </div>
        </div>
        <div style="display:flex;gap:8px;padding:0 20px 18px;">
          <button class="act" style="flex:1;margin:0;" onclick="saveAccess()">💾 Saqlash</button>
          <button class="btn-sm" style="border-color:#e4a6a6;color:#c0392b;" onclick="blockUser()">🚫 Blok</button>
          <button class="btn-sm" onclick="closeAccess()">Yopish</button>
        </div>
      </div>
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
      <div id="bc-text">
        <textarea id="bt" placeholder="Bugungi darsni o'tdingizmi? 📚"></textarea>
        <div class="hint" style="font-weight:600;margin-top:8px;">Tugmalar (ixtiyoriy)</div>
        <div style="display:flex;gap:8px;margin-bottom:8px;"><input id="b1label" placeholder="1-tugma matni" style="flex:1;"><select id="b1type" style="max-width:160px;"><option value="app">Darsga kirish</option><option value="link">Havola</option></select></div>
        <input id="b1url" placeholder="1-tugma havolasi (faqat Havola uchun)" style="margin-bottom:10px;">
        <div style="display:flex;gap:8px;margin-bottom:8px;"><input id="b2label" placeholder="2-tugma matni (ixtiyoriy)" style="flex:1;"><select id="b2type" style="max-width:160px;"><option value="app">Darsga kirish</option><option value="link">Havola</option></select></div>
        <input id="b2url" placeholder="2-tugma havolasi" style="margin-bottom:10px;">
        <label class="acc-r" style="margin-top:4px;"><input type="checkbox" id="bc_sched"> Rejalashtirish:</label>
        <input type="datetime-local" id="bc_when" style="max-width:220px;">
      </div>
      <div id="bc-audio" style="display:none;"><label>Audio fayl</label><input type="file" id="baf" accept="audio/*"><label>Izoh (ixtiyoriy)</label><textarea id="bac"></textarea></div>
      <div id="bc-video" style="display:none;"><label>Dumaloq video (kvadrat mp4)</label><input type="file" id="bvf" accept="video/*"><label>Izoh (ixtiyoriy)</label><textarea id="bvc"></textarea></div>
      <button class="act" onclick="sendBroadcast()">Yuborish</button>
    </div>
    <div class="section">
      <h2>Rejalashtirilgan xabarlar</h2>
      """ + rows_sched + """
    </div>
  </div>

  <div class="panel" id="p-segments">
    <style>
    .seg-card{border:1px solid rgba(128,140,170,.28);border-radius:12px;margin-bottom:10px;overflow:hidden;}
    .seg-card-top{display:flex;align-items:center;gap:12px;padding:12px 13px;cursor:pointer;}
    .seg-badge{width:38px;height:38px;border-radius:10px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:13px;background:linear-gradient(145deg,#3f7bff,#1c40b0);}
    .seg-count{font-weight:800;color:#1f54e0;background:rgba(31,84,224,.14);border-radius:9px;padding:5px 11px;flex:0 0 auto;}
    .seg-card-body{padding:0 13px 14px;}
    .seg-lbl{font-size:11px;font-weight:700;opacity:.6;text-transform:uppercase;letter-spacing:.03em;margin:10px 0 6px;}
    .seg-users{display:flex;flex-wrap:wrap;gap:6px;}
    .seg-u{font-size:12px;border:1px solid rgba(128,140,170,.3);border-radius:7px;padding:4px 8px;}
    .seg-ta{width:100%;min-height:64px;resize:vertical;}
    .seg-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;}
    .seg-auto-tag{font-size:10px;font-weight:800;color:#fff;background:#1f54e0;border-radius:6px;padding:2px 7px;}
    .seg-auto-sw{font-size:13px;display:flex;align-items:center;gap:6px;cursor:pointer;}
    .seg-contact-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid rgba(128,140,170,.2);font-size:13px;}
    .seg-contact-row a{color:#1f54e0;font-weight:700;text-decoration:none;}
    </style>
    <h1>Eslatmalar / Segmentlar</h1>
    <div class="section">
      <div class="hint" style="margin-bottom:12px;">Foydalanuvchilarni faollik va streak bo'yicha guruhlab, shablon xabar yuboring. «Avto» yoqilsa shu segmentga har kuni (soat 10:30 da) shablon o'zi yuboriladi.</div>
      <div class="seg">
        <button class="seg-btn active" data-st="in" onclick="segTab('in',this)">😴 Kirmaganlar</button>
        <button class="seg-btn" data-st="st" onclick="segTab('st',this)">🔥 Streak</button>
        <button class="seg-btn" data-st="contact" onclick="segTab('contact',this)">📞 10+ kun (aloqa)</button>
      </div>
      <div id="seg-root"><div class="hint">Yuklanmoqda...</div></div>
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
      <style>textarea[data-ui="bot_welcome"]{min-height:210px;line-height:1.6;font-size:14px;}</style>
      <h2>Bot /start xabari</h2>
      <div class="hint" style="margin-bottom:10px;">Foydalanuvchi botni ochganda yuboriladigan xabar. <b>{ism}</b> — foydalanuvchi ismi, <b>{id}</b> — ID raqami. Markdown ishlaydi: *qalin*, `kod`. Xabar ostida <b>«✍️ Adminga yozish»</b> tugmasi chiqadi.</div>
      """ + ui_field("bot_welcome", "Welcome matni", True) + """
      """ + ui_field("link_admin", "«Adminga yozish» tugmasi — Telegram username (@ belgisisiz)") + """
      <button class="act" onclick="saveUI()" style="margin-top:8px;">💾 Saqlash</button>
    </div>
    <div class="section">
      <h2>Kurs kartasi</h2>
      """ + ui_field("ui_course_label", "Daraja yorlig'i (masalan A1 — A2)") + ui_field("ui_course_desc", "Tavsif (masalan Bilaman lekin gapira olmayman)") + """
    </div>
    <div class="section">
      <h2>Havolalar va tugmalar</h2>
      <div class="hint">O'qituvchi va AI bot havolalarini shu yerdan o'zgartirasiz (kodga tegmasdan). @ belgisisiz, faqat username yozing.</div>
      """ + ui_field("link_teacher", "O'qituvchiga yozish — username (mas: zamira_russian)") + ui_field("link_ai_bot", "AI suhbat bot — username (mas: zamira_suhbatdosh_bot)") + ui_field("text_invite", "Do'stlarni taklif matni", True) + ui_field("text_share", "Profil ulashish matni (bo'sh = avtomatik)", True) + """
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
      <h2>🔓 Dostup tekshiruvi (himoya)</h2>
      <div class="hint">Yoqilgan bo'lsa — faqat trial/obunasi borlar kiradi. <b>Test uchun o'chirib qo'ying</b> — hamma bo'limlar hammaga ochiq bo'ladi. Ishga tushirishdan oldin yana yoqing.</div>
      <div style="display:flex;align-items:center;gap:12px;margin-top:8px;">
        <div id="accStateLbl" style="font-weight:600;"></div>
        <button class="act" id="accToggleBtn" style="margin:0;width:auto;" onclick="toggleAccess()">O'zgartirish</button>
      </div>
    </div>
      <h2>Kunlik avtomatik eslatma</h2>
      <div id="rm" class="msg">Saqlandi!</div>
      <label>Soat (0-23, Toshkent)</label><input type="number" id="rh" min="0" max="23" value=\"""" + rh + """\" style="max-width:120px;">
      <label>Eslatma matni (har kuni shu soatda boradi)</label><textarea id="rt">""" + esc(rt) + """</textarea>
      <button class="act" onclick="saveReminder()">Saqlash</button>
    </div>
    <div class="section">
      <h2>Kirish</h2>
      <div class="hint">Egasi logini/paroli Railway env'larida (ADMIN_USER, ADMIN_PASSWORD). Ustozlarni "Adminlar" bo'limidan qo'shasiz.</div>
    </div>
  </div>
  <div class="panel" id="p-admins">
    <h1>Adminlar</h1>
    <div class="section">
      <h2>Ustoz qo'shish</h2>
      <div id="am" class="msg">Qo'shildi!</div>
      <div class="hint">Ustoz faqat <b>Darslar</b> bo'limini ko'radi va tahrirlaydi. Foydalanuvchilar, xabar va sozlamalarga kira olmaydi.</div>
      <div class="row2">
        <div><label>Login</label><input id="ad_u" placeholder="masalan: ustoz1"></div>
        <div><label>Parol</label><input id="ad_p" type="text" placeholder="parol"></div>
      </div>
      <button class="act" onclick="addAdmin()">+ Ustoz qo'shish</button>
    </div>
    <div class="section">
      <h2>Mavjud adminlar</h2>
      """ + rows_admins + """
    </div>
  </div>
</div>

<script>
function openPanel(p){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.panel===p));
  document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active',x.id==='p-'+p));
  if(p==='videos')vLoad(vSec);
  document.getElementById('sidebar').classList.remove('open');
}
function nav(btn){var p=btn.dataset.panel;location.hash=p;openPanel(p);}
var ROLE=\"""" + role + """\";
var ACCESS_ON=""" + ("1" if access_on else "0") + """;
(function(){var on=(ACCESS_ON==='1'||ACCESS_ON===1);
  var e=document.getElementById('accStateLbl');if(e)e.innerHTML=on?"<span style='color:#1D9E75'>✅ YOQILGAN (himoyalangan)</span>":"<span style='color:#c0504d'>⚠️ O'CHIRILGAN (test rejimi — hamma ochiq)</span>";
  var tb=document.getElementById('accToggleBtn');if(tb)tb.textContent=on?"O'chirish":"Yoqish";
  if(!on){var w=document.createElement('div');w.style.cssText='position:sticky;top:0;z-index:200;background:#c0392b;color:#fff;padding:11px 16px;font-size:13px;font-weight:600;text-align:center;cursor:pointer;';w.textContent="⚠️ Dostup himoyasi O'CHIRILGAN — hamma foydalanuvchiga barcha darslar ochiq. Yoqish uchun shu yerni bosing.";w.onclick=toggleAccess;document.body.insertBefore(w,document.body.firstChild);}
})();
function toggleAccess(){fetch('/admin/toggle-access',{method:'POST'}).then(function(){location.reload();});}
var VALID=(ROLE==='owner')?['dash','lessons','videos','users','broadcast','segments','ui','admins','settings']:['lessons','videos'];
if(ROLE!=='owner'){document.querySelectorAll('[data-owner]').forEach(function(b){b.style.display='none';});var _d=document.querySelector('[data-panel="dash"]');if(_d)_d.style.display='none';}
(function(){var h=(location.hash||'').replace('#','');
  if(VALID.indexOf(h)>=0)openPanel(h);else openPanel(ROLE==='owner'?'dash':'lessons');})();
function addAdmin(){var u=document.getElementById('ad_u').value.trim(),p=document.getElementById('ad_p').value;if(!u||!p)return alert('Login va parol kiriting');
  fetch('/admin/add-admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})}).then(function(r){if(r.ok)location.reload();else alert('Xato — login band bo\\'lishi mumkin');});}
function delAdmin(u){if(!confirm(u+' o\\'chirilsinmi?'))return;
  fetch('/admin/delete-admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})}).then(function(){location.reload();});}
var ACC_UID=null;
var ACC_SECS=['talaffuz','fundament','a1','b1'];
function openAccess(btn){var r=btn.closest('.lrow2');ACC_UID=r.dataset.uid;
  document.getElementById('acc-name').textContent=r.dataset.name;
  document.getElementById('acc-id').textContent='ID: '+r.dataset.uid;
  var secs=(r.dataset.secs||'').split(',');
  ACC_SECS.forEach(function(s){document.getElementById('acc-'+s).checked=secs.indexOf(s)>=0;});
  var mode=r.dataset.mode||'trial';
  document.querySelectorAll('input[name=accmode]').forEach(function(x){x.checked=(x.value===mode);});
  document.getElementById('acc-until').value=r.dataset.until||'';
  document.getElementById('accModal').style.display='flex';}
function closeAccess(){document.getElementById('accModal').style.display='none';}
function saveAccess(){
  var secs=ACC_SECS.filter(function(s){return document.getElementById('acc-'+s).checked;});
  var m=document.querySelector('input[name=accmode]:checked');var mode=m?m.value:'trial';
  var until=document.getElementById('acc-until').value;
  if(mode==='dated'&&!until)return alert('Sana tanlang');
  fetch('/admin/user-access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:ACC_UID,sections:secs,mode:mode,until:until})}).then(function(r){if(r.ok)location.reload();else alert('Xato');});}
function blockUser(){if(!confirm('Bu foydalanuvchi bloklansinmi?'))return;
  fetch('/admin/user-block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:ACC_UID,blocked:true})}).then(function(){location.reload();});}
function userSearch(q){q=(q||'').toLowerCase();document.querySelectorAll('#p-users .lrow2').forEach(function(r){r.style.display=((r.dataset.search||'').indexOf(q)>=0)?'flex':'none';});}
var vSec='talaffuz';
function vTab(btn){document.querySelectorAll('[data-vsec]').forEach(function(b){b.classList.toggle('on',b===btn);});vSec=btn.dataset.vsec;vLoad(vSec);}
function vEsc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function vLoad(sec){sec=sec||'talaffuz';vSec=sec;
  fetch('/api/videos?section='+sec).then(function(r){return r.json();}).then(function(d){vRender(d.videos||[]);}).catch(function(){});}
function vRender(list){var h='';
  list.forEach(function(v){
    h+='<div class="vcard" data-id="'+v.id+'">'+
      '<div style="font-size:12px;color:#999;margin-bottom:3px;">Nom</div><input class="v-title" value="'+vEsc(v.title)+'"/>'+
      '<div style="font-size:12px;color:#999;margin-bottom:3px;">Tavsif</div><input class="v-descr" value="'+vEsc(v.descr)+'"/>'+
      '<div style="font-size:12px;color:#999;margin-bottom:3px;">YouTube havola</div><input class="v-yt" value="'+vEsc(v.youtube_url)+'" placeholder="https://youtu.be/..."/>'+
      '<div class="vrow">'+(v.task_pdf_url?'<a class="btn-sm" href="'+vEsc(v.task_pdf_url)+'" target="_blank">📄 Vazifa PDF</a>':'<span class="muted" style="font-size:12px">Vazifa PDF yo\\'q</span>')+
      '<button class="btn-sm" onclick="vTask('+v.id+')">⬆ PDF yuklash</button>'+
      '<button class="btn-sm" onclick="vMove(this,-1)">↑</button><button class="btn-sm" onclick="vMove(this,1)">↓</button>'+
      '<button class="btn-sm" style="color:#d06b6b" onclick="vDelete('+v.id+')">🗑</button></div></div>';
  });
  document.getElementById('videoList').innerHTML=h||'<div class="muted" style="padding:8px">Dars yo\\'q</div>';}
function vAdd(){fetch('/admin/video-add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section:vSec})}).then(function(r){return r.json();}).then(function(){vLoad(vSec);});}
function vDelete(id){if(!confirm('Dars o\\'chirilsinmi?'))return;fetch('/admin/video-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).then(function(){vLoad(vSec);});}
function vMove(btn,dir){var c=btn.closest('.vcard');var s=dir<0?c.previousElementSibling:c.nextElementSibling;if(s&&s.classList.contains('vcard')){if(dir<0)c.parentNode.insertBefore(c,s);else c.parentNode.insertBefore(s,c);}}
function vTask(id){var inp=document.createElement('input');inp.type='file';inp.accept='application/pdf';inp.onchange=function(){if(!inp.files.length)return;var fd=new FormData();fd.append('id',id);fd.append('file',inp.files[0]);fetch('/admin/video-task',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(j){if(j&&j.ok)vLoad(vSec);else alert('Xato');});};inp.click();}
function vSave(){var cards=document.querySelectorAll('#videoList .vcard');var lessons=[];cards.forEach(function(c){lessons.push({id:c.dataset.id,title:c.querySelector('.v-title').value,descr:c.querySelector('.v-descr').value,youtube_url:c.querySelector('.v-yt').value});});
  fetch('/admin/video-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lessons:lessons})}).then(function(r){if(r.ok){var m=document.getElementById('vm');m.style.display='block';setTimeout(function(){m.style.display='none';},1500);}else alert('Xato');});}
function applyTheme(t){var b=document.getElementById('themeBtn');if(t==='light'){document.body.classList.add('light');b.textContent='☀️ Oq fon';}else{document.body.classList.remove('light');b.textContent='🌙 Qora fon';}}
function toggleTheme(){var t=document.body.classList.contains('light')?'dark':'light';try{localStorage.setItem('admin_theme',t);}catch(e){}applyTheme(t);}
(function(){var t='dark';try{t=localStorage.getItem('admin_theme')||'dark';}catch(e){}applyTheme(t);})();

function openLesson(){location.href='/admin/lesson?level='+document.getElementById('nl').value+'&day='+document.getElementById('nd').value;}
function filterLevel(btn){document.querySelectorAll('#p-lessons .seg2b[data-lv]').forEach(function(b){b.classList.toggle('on',b===btn);});var lv=btn.dataset.lv;document.querySelectorAll('#lessonList .lvl-block').forEach(function(bl){bl.style.display=(bl.dataset.level===lv)?'block':'none';});var ll=document.getElementById('lessonList');if(lv==='B1'){ll.style.setProperty('--lacc','#e42a3b');ll.style.setProperty('--lacc-d','#a3141f');ll.style.setProperty('--lacc-soft','#fde8ea');}else{ll.style.setProperty('--lacc','#1f54e0');ll.style.setProperty('--lacc-d','#16308f');ll.style.setProperty('--lacc-soft','#e8eefc');}}
function lsave(day,lv){try{sessionStorage.setItem('lret',JSON.stringify({lv:lv,day:day}));}catch(e){}}
(function(){
  var lret=null;try{lret=JSON.parse(sessionStorage.getItem('lret')||'null');sessionStorage.removeItem('lret');}catch(e){}
  var onL=document.getElementById('p-lessons').classList.contains('active');
  if(lret&&lret.lv&&onL){
    var b=document.querySelector('#p-lessons .seg2b[data-lv="'+lret.lv+'"]');
    if(b){filterLevel(b);setTimeout(function(){var t=document.querySelector('#lessonList .lvl-block[data-level="'+lret.lv+'"] .ltile[data-day="'+lret.day+'"]');if(t)t.scrollIntoView({block:'center'});},80);return;}
  }
  var f=document.querySelector('#p-lessons .seg2b.on[data-lv]');if(f)filterLevel(f);
})();
var aiAbort=null;
async function aiFill(){
  var f=document.getElementById("ai_f");if(!f.files.length)return alert("PDF tanlang");
  var lvl=document.getElementById("ai_l").value,day=document.getElementById("ai_d").value;
  var fname=f.files[0].name;
  if(!confirm(lvl+" · "+day+"-kun — "+fname+" dan AI to'ldirsinmi? Avval ko'rib chiqasiz, keyin saqlaysiz."))return;
  var fd=new FormData();fd.append("level",lvl);fd.append("day",day);fd.append("file",f.files[0]);
  var b=document.getElementById("aiBtn");b.disabled=true;b.textContent="🤖 AI ishlayapti...";
  document.getElementById("aiCancel").style.display="inline-block";
  aiAbort=new AbortController();
  try{
    var r=await fetch("/admin/ai-fill",{method:"POST",body:fd,signal:aiAbort.signal});
    var j=await r.json().catch(function(){return {};});
    if(r.ok&&j.ok){try{sessionStorage.setItem("ai_draft_"+lvl+"_"+day,JSON.stringify(j.data));}catch(e){}location.href="/admin/lesson?level="+lvl+"&day="+day+"&draft=1";}
    else{alert("Xato: "+(j.error||r.status));resetAi();}
  }catch(e){if(e.name!=="AbortError")alert("Xato: "+e.message);resetAi();}
}
function resetAi(){var b=document.getElementById("aiBtn");if(b){b.disabled=false;b.textContent="🤖 To'ldirish";}var c=document.getElementById("aiCancel");if(c)c.style.display="none";aiAbort=null;}
function aiCancel(){if(aiAbort)aiAbort.abort();resetAi();}

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
  if(bcType==='text'){
    var t=document.getElementById('bt').value.trim();if(!t)return alert('Matn yozing');
    var btns=[];
    [['b1label','b1type','b1url'],['b2label','b2type','b2url']].forEach(function(ids){
      var lbl=document.getElementById(ids[0]).value.trim();if(!lbl)return;
      btns.push({label:lbl,type:document.getElementById(ids[1]).value,url:document.getElementById(ids[2]).value.trim()});
    });
    var sched=document.getElementById('bc_sched').checked,send_at='';
    if(sched){send_at=document.getElementById('bc_when').value;if(!send_at)return alert('Vaqtni tanlang');}
    if(!sched&&!confirm('Hammaga hozir yuborilsinmi?'))return;
    var r=await fetch('/admin/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,buttons:btns,send_at:send_at})});
    if(r.ok){var j=await r.json();if(j.scheduled){alert('Rejalashtirildi ✓');location.reload();return;}var m=document.getElementById('bm');m.style.display='block';setTimeout(function(){m.style.display='none';},2500);}
    else alert('Xato');
  }else{
    if(!confirm('Hammaga yuborilsinmi?'))return;
    var fe=(bcType==='audio')?document.getElementById('baf'):document.getElementById('bvf');
    var ce=(bcType==='audio')?document.getElementById('bac'):document.getElementById('bvc');
    if(!fe.files.length)return alert('Fayl tanlang');
    var fd=new FormData();fd.append('type',bcType);fd.append('caption',ce.value.trim());fd.append('file',fe.files[0]);
    var b=event.target;b.disabled=true;b.textContent='Yuborilyapti...';
    await fetch('/admin/broadcast-media',{method:'POST',body:fd});b.disabled=false;b.textContent='Yuborish';
    var m=document.getElementById('bm');m.style.display='block';setTimeout(function(){m.style.display='none';},2500);
  }
}
function cancelSched(id){if(!confirm('Bu rejalashtirilgan xabar bekor qilinsinmi?'))return;
  fetch('/admin/schedule-cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).then(function(){location.reload();});}
var SEG_DATA=null, SEG_TAB='in';
function segEsc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function loadSegments(){
  var root=document.getElementById('seg-root'); if(!root)return;
  root.innerHTML='<div class="hint">Yuklanmoqda...</div>';
  try{ var r=await fetch('/admin/segments'); SEG_DATA=await r.json(); }
  catch(e){ root.innerHTML='<div class="hint">Xato yuz berdi</div>'; return; }
  segRender();
}
function segTab(t,btn){SEG_TAB=t;document.querySelectorAll('#p-segments .seg-btn').forEach(function(b){b.classList.toggle('active',b===btn);});segRender();}
function segRender(){
  var root=document.getElementById('seg-root'); if(!root||!SEG_DATA)return;
  if(SEG_TAB==='contact'){root.innerHTML=segContact();return;}
  var list=(SEG_TAB==='in')?SEG_DATA.inactive:SEG_DATA.streak;
  var lbl=(SEG_TAB==='in')?'kun kirmaganlar':'kun ketma-ket';
  var h='';
  list.forEach(function(s){
    var uid='seg-'+SEG_TAB+'-'+s.key;
    var chips='';
    s.users.forEach(function(u){chips+='<span class="seg-u">'+segEsc(u.name)+(u.username?' @'+segEsc(u.username):'')+'</span>';});
    if(s.count>s.users.length)chips+='<span class="seg-u">+'+(s.count-s.users.length)+' ta</span>';
    if(!chips)chips='<span class="hint">Hozircha mavjud emas</span>';
    h+='<div class="seg-card"><div class="seg-card-top" onclick="segToggle('+s.key+')">'+
       '<div class="seg-badge">'+s.key+'</div>'+
       '<div style="flex:1"><b>'+s.key+' '+lbl+'</b>'+(s.auto?' <span class="seg-auto-tag">AVTO</span>':'')+'</div>'+
       '<div class="seg-count">'+s.count+'</div></div>'+
       '<div class="seg-card-body" id="'+uid+'" style="display:none">'+
       '<div class="seg-lbl">Foydalanuvchilar</div><div class="seg-users">'+chips+'</div>'+
       '<div class="seg-lbl">Shablon xabar</div>'+
       '<textarea class="seg-ta" id="'+uid+'-tpl">'+segEsc(s.tpl)+'</textarea>'+
       '<div class="seg-actions">'+
       '<button class="act" style="margin:0;width:auto" onclick="segSend('+s.key+',this)">'+s.count+' kishiga yuborish</button>'+
       '<button class="btn-sm" onclick="segMedia('+s.key+',1)">🖼 Rasm</button>'+
       '<button class="btn-sm" onclick="segMedia('+s.key+',2)">🎬 Video</button>'+
       '<button class="btn-sm" onclick="segMedia('+s.key+',3)">🎧 Audio</button>'+
       '<label class="seg-auto-sw"><input type="checkbox" '+(s.auto?'checked':'')+' onchange="segAuto('+s.key+',this.checked)"> Avto</label>'+
       '</div></div></div>';
  });
  root.innerHTML=h;
}
function segContact(){
  var d=SEG_DATA;var h='<div class="seg-card"><div class="seg-card-top" style="cursor:default">'+
    '<div class="seg-badge" style="background:linear-gradient(145deg,#ffb14a,#e88a1a)">'+d.contact_min+'+</div>'+
    '<div style="flex:1"><b>'+d.contact_min+' kundan oshganlar</b><div class="hint">Shaxsan aloqaga chiqish uchun</div></div>'+
    '<div class="seg-count">'+d.contact_count+'</div></div><div class="seg-card-body" style="display:block">';
  if(!d.contact.length)h+='<div class="hint">Hozircha mavjud emas</div>';
  d.contact.forEach(function(u){
    var un=u.username?'<a href="https://t.me/'+segEsc(u.username)+'" target="_blank">@'+segEsc(u.username)+'</a>':'<span class="hint">username topilmadi</span>';
    h+='<div class="seg-contact-row"><span><b>'+segEsc(u.name)+'</b> · ID '+u.id+' · '+u.gap+' kun</span>'+un+'</div>';
  });
  h+='</div></div>';return h;
}
function segToggle(key){var e=document.getElementById('seg-'+SEG_TAB+'-'+key);if(e)e.style.display=(e.style.display==='none')?'block':'none';}
async function segSend(key,btn){
  var tpl=document.getElementById('seg-'+SEG_TAB+'-'+key+'-tpl').value.trim();
  if(!tpl)return alert('Shablon matni kiriting');
  if(!confirm('Shu segmentga hozir yuborilsinmi?'))return;
  btn.disabled=true;var ot=btn.textContent;btn.textContent='Yuborilyapti...';
  try{var r=await fetch('/admin/segment-send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:SEG_TAB,key:key,text:tpl})});var j=await r.json();alert(j.ok?(j.count+' kishiga yuborildi'):'Xato yuz berdi');}
  catch(e){alert('Xato yuz berdi');}
  btn.disabled=false;btn.textContent=ot;
}
async function segAuto(key,on){
  try{await fetch('/admin/segment-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:SEG_TAB,key:key,auto:on})});}catch(e){}
}
function segMedia(key,code){
  var mtype=['','photo','video','audio'][code];
  var tpl=document.getElementById('seg-'+SEG_TAB+'-'+key+'-tpl').value.trim();
  var kind=SEG_TAB;
  var inp=document.createElement('input');inp.type='file';
  inp.accept=(code===1)?'image/*':(code===2)?'video/*':'audio/*';
  inp.onchange=async function(){
    if(!inp.files.length)return;
    if(!confirm('Media shu segmentga yuborilsinmi?'))return;
    var fd=new FormData();fd.append('kind',kind);fd.append('key',key);fd.append('mtype',mtype);fd.append('caption',tpl);fd.append('file',inp.files[0]);
    try{var r=await fetch('/admin/segment-send-media',{method:'POST',body:fd});var j=await r.json();alert(j.ok?(j.count+' kishiga yuborildi'):'Xato yuz berdi');}
    catch(e){alert('Xato yuz berdi');}
  };
  inp.click();
}
function fileTab(lvl){
  document.querySelectorAll('.ftab').forEach(function(b){b.classList.remove('on-a1','on-b1');});
  var act=document.getElementById('ftab-'+lvl);if(act)act.classList.add(lvl==='B1'?'on-b1':'on-a1');
  var g=document.getElementById('filesGrid');
  if(g){if(lvl==='B1'){g.style.setProperty('--facc','#e42a3b');g.style.setProperty('--facc-soft','#fde8ea');}else{g.style.setProperty('--facc','#1f54e0');g.style.setProperty('--facc-soft','#e8eefc');}}
  document.querySelectorAll('.frow').forEach(function(r){r.style.display=(r.getAttribute('data-fl')===lvl)?'':'none';});
}
async function delFile(level,day){
  if(!confirm(level+" "+day+"-kun faylini o'chirasizmi? (faqat fayl o'chadi, dars saqlanadi)"))return;
  var r=await fetch('/admin/delete-file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:level,day:day})});
  if(r.ok){location.reload();}else{alert('Xato');}
}
fileTab('A1');
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
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:18px;max-width:780px;margin:0 auto;}
a.back,.backbtn{color:var(--muted);text-decoration:none;font-size:14px;background:none;border:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-family:inherit;padding:0;margin-bottom:10px;}
h1{font-size:22px;font-weight:700;margin:6px 0 2px;}
h2{font-size:18px;font-weight:600;margin:6px 0 14px;}
.tag{color:var(--muted);font-size:13px;margin-bottom:16px;}
.screen{display:none;}.screen.on{display:block;}
.scard{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:12px;cursor:pointer;}
.ic{width:44px;height:44px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;}
.snm{font-size:16px;font-weight:500;}.ssub{font-size:13px;color:var(--muted);margin-top:2px;}
.chev{color:var(--muted);margin-left:auto;font-size:20px;}
label.flbl{font-size:12px;color:var(--muted);display:block;margin:2px 0 4px;}
input,textarea,select{width:100%;background:var(--ibg);border:1px solid var(--ib);border-radius:8px;padding:9px 11px;font-size:14px;color:var(--text);font-family:inherit;}
input.etask{width:18px;height:18px;flex:none;margin:0;padding:0;}
textarea{min-height:96px;resize:vertical;line-height:1.55;}
.fixed{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:14px;}
.ecard{position:relative;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px 16px;margin-bottom:12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}
.ecard .efield{display:flex;flex-direction:column;min-width:0;}
.ecard .efield.full{grid-column:1/-1;}
.cdelbtn{justify-self:end;grid-column:1/-1;background:transparent;border:none;color:#d06b6b;cursor:pointer;font-size:13px;font-family:inherit;}
.tg-sin{color:#0a5a40;font-weight:500;}.tg-ant{color:#a32d2d;font-weight:500;}.tg-nsv{color:#185fa5;font-weight:500;}.tg-sv{color:#854f0b;font-weight:500;}
.add{background:var(--soft);color:var(--text);border:1px dashed var(--ib);border-radius:10px;padding:11px;font-size:14px;cursor:pointer;width:100%;margin-top:2px;}
.save{background:var(--acc);color:#fff;border:none;border-radius:10px;padding:14px;font-size:15px;font-weight:700;cursor:pointer;width:100%;margin-top:8px;position:sticky;bottom:10px;}
.save:hover{background:var(--acch);}
.hint{font-size:12px;color:var(--muted);margin:4px 0 10px;}.hint.warn{color:#e0a800;}
.aud{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--soft);flex-wrap:wrap;}
.aud audio{height:34px;max-width:180px;}
.up{background:var(--acc);color:#fff;border:none;border-radius:7px;padding:7px 12px;font-size:12px;cursor:pointer;}
.msg{background:rgba(29,158,117,.15);border:1px solid var(--acc);border-radius:8px;padding:12px;color:var(--accs);font-size:13px;margin-bottom:12px;display:none;}
.subt{font-size:12px;color:var(--muted);letter-spacing:.05em;margin:8px 0 6px;}
</style></head><body>
<div id="msg" class="msg">Saqlandi!</div>

<div class="screen on" id="sc-menu">
  <a class="back" href="/admin#lessons">← Darslar</a>
  <h1 id="lt">Dars tahrirlash</h1>
  <div class="tag" id="ltag"></div>
  <div id="draftBanner" style="display:none;background:rgba(29,158,117,.15);border:1px solid var(--acc);border-radius:10px;padding:11px 14px;font-size:13px;color:var(--accs);margin-bottom:12px;">🤖 AI tayyorladi — ko'rib chiqing va <b>Saqlash</b> bossangiz yoziladi. Saqlamaguncha hech narsa o'zgarmaydi.</div>
  <div class="fixed"><label class="flbl">Dars nomi</label><input id="f_title" placeholder="masalan: Tanishuv"></div>
  <div class="scard" onclick="openSec('vocab')"><div class="ic" style="background:#E1F5EE;color:#085041;">🔤</div><div><div class="snm">Lug'at</div><div class="ssub" id="cnt-vocab">0 ta so'z</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('formulas')"><div class="ic" style="background:#EEEDFE;color:#3C3489;">💬</div><div><div class="snm">Nutq formulalari</div><div class="ssub" id="cnt-formulas">0 ta</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('audio')"><div class="ic" style="background:#E1F5EE;color:#085041;">🎧</div><div><div class="snm">Audio mashq</div><div class="ssub">Audio + test savollari</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('speaking')"><div class="ic" style="background:#E6F1FB;color:#0C447C;">🎤</div><div><div class="snm">Gapirish</div><div class="ssub" id="cnt-speaking">0 ta savol</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('grammar')"><div class="ic" style="background:#FAEEDA;color:#633806;">📘</div><div><div class="snm">Grammatika</div><div class="ssub" id="cnt-grammar">0 ta konstruksiya</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('writing')"><div class="ic" style="background:#E6F1FB;color:#0C447C;">✍️</div><div><div class="snm">Yozish mashqi</div><div class="ssub" id="cnt-writing">Topshiriq + namuna</div></div><span class="chev">›</span></div>
  <div class="scard" onclick="openSec('reading')"><div class="ic" style="background:#F1EFE8;color:#444441;">📖</div><div><div class="snm">O'qish matnlari</div><div class="ssub" id="cnt-reading_texts">0 ta matn</div></div><span class="chev">›</span></div>
  <div style="background:var(--card,#fff);border:1px solid var(--bd,#e5e5df);border-radius:14px;padding:14px 16px;margin-top:8px;">
    <div style="font-size:14px;font-weight:600;margin-bottom:3px;">Ilovada ko'rsatiladigan mashqlar</div>
    <div style="font-size:12px;color:var(--mut,#888);margin-bottom:10px;">Belgilanmagan mashq shu kunda ilovada ko'rinmaydi. Hammasi belgili bo'lsa — barchasi ko'rinadi.</div>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="vocab" checked> 🔤 Lug'at</label>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="formula" checked> 💬 Nutq formulalari</label>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="audio" checked> 🎧 Audio mashq</label>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="speaking" checked> 🎤 Gapirish</label>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="grammar" checked> 📚 Grammatika</label>
    <label style="display:flex;align-items:center;gap:9px;font-size:14px;padding:7px 0;cursor:pointer;"><input type="checkbox" class="etask" data-t="writing" checked> ✍️ Yozish mashqi</label>
  </div>
  <button class="save" onclick="saveLesson()">💾 Saqlash</button>
  <div style="text-align:center;margin-top:14px;"><button class="backbtn" style="color:#d06b6b;" onclick="delLesson()">🗑 Darsni o'chirish</button></div>
</div>

<div class="screen" id="sc-vocab">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>🔤 Lug'at</h2>
  <div id="L_vocab"></div><button class="add" onclick="addCard('vocab')">+ So'z qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-formulas">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>💬 Nutq formulalari</h2>
  <button id="topupBtn" onclick="aiTopup()" style="width:100%;padding:13px;border-radius:12px;border:none;background:#6b4ef0;color:#fff;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;margin-bottom:8px;">🤖 AI: 10 formula + dialogni PDF'dan to'ldir</button>
  <div class="hint" style="margin-bottom:12px;">Bu kun uchun saqlangan PDF'dan AI formula (10 ta) va dialog tayyorlaydi. Lug'at va boshqa mazmunga tegmaydi — faqat formula va dialog. Ko'rib chiqib «Saqlash» bosing.</div>
  <div id="L_formulas"></div><button class="add" onclick="addCard('formulas')">+ Formula qo'shish</button>
  <div class="subt" style="margin-top:16px;">DIALOG (mavzu bo'yicha suhbat)</div>
  <div class="hint" style="margin-bottom:8px;">Har qator: <b>A</b> yoki <b>B</b> (kim gapiryapti) + ruscha + o'zbekcha. Ketma-ket qo'shing.</div>
  <div id="L_dialog"></div><button class="add" onclick="addCard('dialog')">+ Dialog qatori qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-audio">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>🎧 Audio mashq</h2>
  <div class="fixed">
    <div class="subt">SHADOWING (tinglab takrorlash matni)</div>
    <label class="flbl">Ruscha</label><textarea id="f_shadowing_ru"></textarea>
    <label class="flbl">O'zbekcha</label><textarea id="f_shadowing_uz"></textarea>
  </div>
  <div class="fixed">
    <div class="subt">AUDIO FAYLLAR</div>__R2WARN__
    <div id="audioBox"></div>
  </div>
  <div class="subt">AUDIROVANIYE TEST SAVOLLARI</div>
  <div id="L_audio_questions"></div><button class="add" onclick="addCard('audio_questions')">+ Savol qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-speaking">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>🎤 Gapirish</h2>
  <div class="fixed"><label class="flbl">Razgovor (AI suhbat boshlash gapi)</label><input id="f_razgovor_start"></div>
  <div class="subt">SAVOLLAR</div>
  <div id="L_speaking_questions"></div><button class="add" onclick="addCard('speaking_questions')">+ Savol qo'shish</button>
  <div class="fixed" style="margin-top:14px;">
    <div class="subt">🎯 TAQLID MASHQI</div>
    <label class="flbl">Taqlid turi (shu kun uchun)</label>
    <select id="f_taqlid_type" style="width:100%;padding:11px;border-radius:9px;border:1px solid #ccc;font-family:inherit;font-size:14px;">
      <option value="audio">🎧 Audio</option>
      <option value="video">🎬 Video (YouTube)</option>
    </select>
    <label class="flbl" style="margin-top:10px;">Video (YouTube URL) — «Video» tanlansa</label>
    <input id="f_taqlid_video" type="text" placeholder="https://youtu.be/..." style="width:100%;padding:11px;border-radius:9px;border:1px solid #ccc;font-family:inherit;font-size:14px;">
    <label class="flbl" style="margin-top:10px;">Audio fayl — «Audio» tanlansa</label>
    <div id="taqlidAudioBox"></div>
  </div>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-grammar">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>📘 Grammatika</h2>
  <div class="fixed">
    <label class="flbl" style="display:flex;align-items:center;gap:8px;cursor:pointer;"><input type="checkbox" id="gv_on" class="etask-x" style="width:18px;height:18px;flex:none;margin:0;" onchange="document.getElementById('gv_url').disabled=!this.checked;"> 🎬 Videodars yoqilgan</label>
    <input id="gv_url" placeholder="YouTube link (masalan https://youtu.be/...)" style="margin-top:8px;">
    <div class="flbl" style="margin-top:6px;">Yoqilsa — ilovada grammatika ekrani tepasida video chiqadi.</div>
  </div>
  <div id="L_grammar"></div><button class="add" onclick="addCard('grammar')">+ Konstruksiya qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-writing">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>✍️ Yozish mashqi</h2>
  <div class="hint" style="margin-bottom:10px;">Har zadaniya uchun: <b>Ruscha matn</b> (topshiriq), <b>O'zbekcha matn</b> (tarjima), <b>Misol/Namuna javob</b>. Bir nechta zadaniya qo'shsa bo'ladi.</div>
  <div id="L_writing_tasks"></div>
  <button class="add" onclick="addCard('writing_tasks')">+ Zadaniya qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>
<div class="screen" id="sc-reading">
  <button class="backbtn" onclick="showMenu()">← Menyu</button><h2>📖 O'qish matnlari</h2>
  <div id="L_reading_texts"></div><button class="add" onclick="addCard('reading_texts')">+ Matn qo'shish</button>
  <div class="subt" style="margin-top:16px;">TOPSHIRIQLAR — Правда / Не правда</div>
  <div class="hint" style="margin-bottom:8px;">Matnga oid gaplar. Javob: <b>1</b> = Правда (to'g'ri), <b>0</b> = Не правда (noto'g'ri).</div>
  <div id="L_reading_tasks"></div><button class="add" onclick="addCard('reading_tasks')">+ Gap qo'shish</button>
  <button class="save" onclick="saveLesson(1)">💾 Saqlash</button>
</div>

<script>
(function(){var t='dark';try{t=localStorage.getItem('admin_theme')||'dark';}catch(e){}if(t==='light')document.body.classList.add('light');})();
var LEVEL="__LEVEL__",DAY="__DAY__";
document.getElementById('ltag').textContent=LEVEL+' · '+DAY+'-kun';

var SCHEMA={
  vocab:[{k:'stress',l:"So'z — urg'u bilan",big:1,full:1},{k:'ru',l:"So'z (urg'usiz)"},{k:'uz',l:'Tarjima'},{k:'ex1ru',l:'1-misol (rus)'},{k:'ex1uz',l:'1-misol (uz)'},{k:'ex2ru',l:'2-misol (rus)'},{k:'ex2uz',l:'2-misol (uz)'},{k:'sin',l:'Sinonim',cls:'tg-sin'},{k:'ant',l:'Antonim',cls:'tg-ant'},{k:'nsv',l:'НСВ',cls:'tg-nsv'},{k:'sv',l:'СВ',cls:'tg-sv'},{k:'kx_q',l:"Kontekst gap (B1) — so'z o'rniga ___",a:1,full:1},{k:'kx_a',l:"Kontekst — to'g'ri so'z"},{k:'kx',l:"Kontekst — 3 yanglish variant (har qatorda bitta)",lines:1,full:1}],
  formulas:[{k:'ru',l:'Formula (rus)'},{k:'uz',l:'Tarjima (uz)'},{k:'ex1ru',l:'1-misol (rus)'},{k:'ex1uz',l:'1-misol (uz)'},{k:'ex2ru',l:'2-misol (rus)'},{k:'ex2uz',l:'2-misol (uz)'}],
  reading_texts:[{k:'level',l:'Sarlavha',full:1},{k:'ru',l:'Ruscha matn',a:1},{k:'uz',l:"O'zbekcha tarjima",a:1}],
  grammar:[{k:'title',l:'Mavzu',full:1},{k:'sub',l:'Izoh',full:1},{k:'base',l:'Asos shakl'},{k:'res',l:'Natija shakl'},{k:'example',l:'Misol',a:1}],
  speaking_questions:[{k:'title',l:'Savol (rus)',full:1},{k:'desc',l:'Izoh (uz)'},{k:'format',l:'Format'},{k:'answer',l:'Namuna javob (rus)',a:1,full:1}],
  audio_questions:[{k:'q',l:'Savol',full:1},{k:'options',l:'Variantlar (har qatorda bitta)',lines:1,full:1},{k:'correct',l:"To'g'ri variant raqami"}],
  writing_tasks:[{k:'ru',l:'Ruscha matn (topshiriq)',a:1,full:1},{k:'uz',l:"O'zbekcha matn (tarjima)",a:1,full:1},{k:'sample',l:'Misol / Namuna javob (rus)',a:1,full:1}],
  reading_tasks:[{k:'q',l:'Gap (rus)',full:1},{k:'answer',l:'Javob: 1 = Правда, 0 = Не правда'}],
  dialog:[{k:'sp',l:'Kim gapiryapti (A yoki B)'},{k:'ru',l:'Ruscha',a:1,full:1},{k:'uz',l:"O'zbekcha",a:1,full:1}]
};
var LISTS=['vocab','formulas','reading_texts','grammar','speaking_questions','audio_questions','writing_tasks','reading_tasks','dialog'];

function addCard(name,it){
  it=it||{};var sc=SCHEMA[name];var c=document.getElementById('L_'+name);
  var card=document.createElement('div');card.className='ecard';
  var db=document.createElement('button');db.type='button';db.className='cdelbtn';db.textContent="🗑 o'chirish";
  db.onclick=function(){card.remove();updateCounts();};card.appendChild(db);
  sc.forEach(function(f){
    var w=document.createElement('div');w.className='efield'+((f.full||f.a||f.lines)?' full':'');
    var lab=document.createElement('label');lab.className='flbl'+(f.cls?' '+f.cls:'');lab.textContent=f.l;
    var el=(f.a||f.lines)?document.createElement('textarea'):document.createElement('input');
    el.className='lfield';el.dataset.k=f.k;el.dataset.lines=f.lines?1:'';
    if(f.big){el.style.fontSize='18px';el.style.fontWeight='500';}
    var v=it[f.k];if(f.lines&&Array.isArray(v))v=v.join('\\n');el.value=(v==null?'':v);
    w.appendChild(lab);w.appendChild(el);card.appendChild(w);
  });
  c.appendChild(card);updateCounts();
}
function collect(name){
  var out=[];document.querySelectorAll('#L_'+name+' .ecard').forEach(function(card){
    var o={},empty=true;
    card.querySelectorAll('.lfield').forEach(function(el){
      var v=el.value;if(el.dataset.lines){o[el.dataset.k]=v.split('\\n').map(function(s){return s.trim();}).filter(Boolean);}else{o[el.dataset.k]=v;}
      if(v&&v.trim())empty=false;
    });
    if(!empty)out.push(o);
  });
  return out;
}
function cntLen(name){return document.querySelectorAll('#L_'+name+' .ecard').length;}
function updateCounts(){
  var m={vocab:" ta so'z",formulas:" ta",speaking_questions:" ta savol",grammar:" ta konstruksiya",reading_texts:" ta matn",writing_tasks:" ta zadaniya"};
  for(var k in m){var e=document.getElementById('cnt-'+k);if(e)e.textContent=cntLen(k)+m[k];}
}
function openSec(s){document.querySelectorAll('.screen').forEach(function(x){x.classList.remove('on');});document.getElementById('sc-'+s).classList.add('on');window.scrollTo(0,0);}
function showMenu(){updateCounts();openSec('menu');}
async function aiTopup(){
  var btn=document.getElementById('topupBtn');if(!btn)return;
  if(!confirm("Bu kun uchun saqlangan PDF'dan formula va dialog AI bilan to'ldiriladi. Mavjud formula/dialog almashadi (lug'at va boshqasi saqlanadi). Davom etamizmi?"))return;
  btn.disabled=true;var ot=btn.textContent;btn.textContent='⏳ AI ishlayapti (30-60 soniya)...';
  try{
    var r=await fetch('/admin/ai-topup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:LEVEL,day:DAY})});
    var j=await r.json();
    if(!j.ok){alert(j.error||'Xato');btn.disabled=false;btn.textContent=ot;return;}
    if(j.formulas&&j.formulas.length){document.getElementById('L_formulas').innerHTML='';j.formulas.forEach(function(it){addCard('formulas',it);});}
    if(j.dialog&&j.dialog.length){document.getElementById('L_dialog').innerHTML='';j.dialog.forEach(function(it){addCard('dialog',it);});}
    updateCounts();
    alert("AI to'ldirdi: "+((j.formulas||[]).length)+" formula, "+((j.dialog||[]).length)+" dialog qatori. Ko'rib chiqing va pastdagi Saqlash ni bosing.");
  }catch(e){alert('Xato: '+e);}
  btn.disabled=false;btn.textContent=ot;
}

function renderAudio(audios){
  audios=audios||{};var box=document.getElementById('audioBox');box.innerHTML='';
  var slots={audirov:'Audirovaniye'};
  Object.keys(slots).forEach(function(slot){
    var div=document.createElement('div');div.className='aud';
    var h='<b style="min-width:110px">'+slots[slot]+'</b>';
    if(audios[slot])h+='<audio controls preload="none" src="'+audios[slot]+'"></audio>';else h+='<span class="hint">yuklanmagan</span>';
    div.innerHTML=h;
    var inp=document.createElement('input');inp.type='file';inp.accept='audio/*';inp.style.maxWidth='190px';
    var btn=document.createElement('button');btn.className='up';btn.textContent='Yuklash';
    btn.onclick=function(){uploadAudio(slot,inp,btn);};
    div.appendChild(inp);div.appendChild(btn);box.appendChild(div);
  });
}
function renderTaqlidAudio(audios){
  audios=audios||{};var box=document.getElementById('taqlidAudioBox');if(!box)return;box.innerHTML='';
  var div=document.createElement('div');div.className='aud';
  var h='<b style="min-width:110px">Taqlid audio</b>';
  if(audios.taqlid)h+='<audio controls preload="none" src="'+audios.taqlid+'"></audio>';else h+='<span class="hint">yuklanmagan</span>';
  div.innerHTML=h;
  var inp=document.createElement('input');inp.type='file';inp.accept='audio/*';inp.style.maxWidth='190px';
  var btn=document.createElement('button');btn.className='up';btn.textContent='Yuklash';
  btn.onclick=function(){uploadAudio('taqlid',inp,btn);};
  div.appendChild(inp);div.appendChild(btn);box.appendChild(div);
}
async function uploadAudio(slot,inp,btn){
  if(!inp.files.length)return alert('Fayl tanlang');
  var fd=new FormData();fd.append('level',LEVEL);fd.append('day',DAY);fd.append('slot',slot);fd.append('file',inp.files[0]);
  btn.disabled=true;btn.textContent='...';
  var r=await fetch('/admin/upload-audio',{method:'POST',body:fd});btn.disabled=false;btn.textContent='Yuklash';
  if(r.ok){load();}else{var j=await r.json().catch(function(){return{};});alert('Xato: '+(j.error||r.status));}
}
async function load(){
  var d=null,isDraft=/[?&]draft=1/.test(location.search);
  if(isDraft){try{var raw=sessionStorage.getItem('ai_draft_'+LEVEL+'_'+DAY);if(raw)d=JSON.parse(raw);}catch(e){}}
  if(d){var bn=document.getElementById('draftBanner');if(bn)bn.style.display='block';
    try{var ar=await fetch('/api/content?level='+LEVEL+'&day='+DAY);var ad=await ar.json();d.audios=ad.audios||{};d.materials=ad.materials||{};}catch(e){d.audios={};d.materials={};}}
  else{var r=await fetch('/api/content?level='+LEVEL+'&day='+DAY);d=await r.json();}
  document.getElementById('f_title').value=d.title||'';
  document.getElementById('f_shadowing_ru').value=d.shadowing_ru||'';
  document.getElementById('f_shadowing_uz').value=d.shadowing_uz||'';
  document.getElementById('f_razgovor_start').value=d.razgovor_start||'';
  var gv=d.grammar_video||'';document.getElementById('gv_on').checked=!!gv;var gu=document.getElementById('gv_url');gu.value=gv;gu.disabled=!gv;
  document.getElementById('f_taqlid_type').value=d.taqlid_type||'audio';
  document.getElementById('f_taqlid_video').value=d.taqlid_video||'';
  if(!d.writing_tasks||!d.writing_tasks.length){if(d.writing_task||d.writing_sample){d.writing_tasks=[{ru:'',uz:d.writing_task||'',sample:d.writing_sample||''}];}else if(d.writing&&(d.writing.task||d.writing.sample)){d.writing_tasks=[{ru:'',uz:d.writing.task||'',sample:d.writing.sample||''}];}}
  LISTS.forEach(function(n){document.getElementById('L_'+n).innerHTML='';(d[n]||[]).forEach(function(it){addCard(n,it);});});
  var et=d.enabled_tasks;document.querySelectorAll('.etask').forEach(function(c){c.checked=(!et)?true:(et.indexOf(c.dataset.t)!==-1);});
  MATERIALS=d.materials||{};setupMaterials();
  renderAudio(d.audios);renderTaqlidAudio(d.audios);updateCounts();
}
async function saveLesson(toMenu){
  var et=[];document.querySelectorAll('.etask').forEach(function(c){if(c.checked)et.push(c.dataset.t);});
  var body={level:LEVEL,day:DAY,
    title:document.getElementById('f_title').value,
    shadowing_ru:document.getElementById('f_shadowing_ru').value,
    shadowing_uz:document.getElementById('f_shadowing_uz').value,
    razgovor_start:document.getElementById('f_razgovor_start').value,
    grammar_video:(document.getElementById('gv_on').checked?document.getElementById('gv_url').value.trim():''),
    taqlid_type:document.getElementById('f_taqlid_type').value,
    taqlid_video:document.getElementById('f_taqlid_video').value.trim(),
    enabled_tasks:(et.length===6)?null:et,
    vocab:collect('vocab'),formulas:collect('formulas'),reading_texts:collect('reading_texts'),
    grammar:collect('grammar'),speaking_questions:collect('speaking_questions'),audio_questions:collect('audio_questions'),
    writing_tasks:collect('writing_tasks'),reading_tasks:collect('reading_tasks'),dialog:collect('dialog')};
  var r=await fetch('/admin/save-lesson',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){var m=document.getElementById('msg');m.style.display='block';window.scrollTo(0,0);setTimeout(function(){m.style.display='none';},1800);
    try{sessionStorage.removeItem('ai_draft_'+LEVEL+'_'+DAY);}catch(e){}var bn=document.getElementById('draftBanner');if(bn)bn.style.display='none';
    if(toMenu)showMenu();}
  else alert('Saqlashda xato');
}
async function delLesson(){
  if(!confirm(LEVEL+' '+DAY+'-kun butunlay o\\'chirilsinmi?'))return;
  await fetch('/admin/delete-lesson',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:LEVEL,day:DAY})});
  location.href='/admin#lessons';
}
var MATERIALS={};
var MAT_SECS={vocab:'sc-vocab',formula:'sc-formulas',audio:'sc-audio',speaking:'sc-speaking',grammar:'sc-grammar',writing:'sc-writing'};
function setupMaterials(){
  for(var sec in MAT_SECS){
    var screen=document.getElementById(MAT_SECS[sec]);if(!screen)continue;
    var box=screen.querySelector('.matbox');
    if(!box){box=document.createElement('div');box.className='fixed matbox';box.dataset.sec=sec;
      var save=screen.querySelector('.save');if(save)screen.insertBefore(box,save);else screen.appendChild(box);}
    renderMaterial(sec);
  }
}
function renderMaterial(sec){
  var screen=document.getElementById(MAT_SECS[sec]);if(!screen)return;
  var box=screen.querySelector('.matbox');if(!box)return;
  var m=MATERIALS[sec];box.innerHTML='';
  var t=document.createElement('div');t.className='subt';t.textContent="\\uD83D\\uDCCE QO'SHIMCHA MATERIAL (PDF)";box.appendChild(t);
  if(m&&m.url){
    var row=document.createElement('div');row.className='aud';
    var a=document.createElement('a');a.className='btn-sm';a.href=m.url;a.target='_blank';a.textContent="Ko'rish";
    var fn=document.createElement('span');fn.className='hint';fn.style.flex='1';fn.textContent=m.filename||'';
    var del=document.createElement('button');del.className='up';del.style.background='#c0392b';del.textContent="O'chirish";del.onclick=function(){delMaterial(sec);};
    row.appendChild(a);row.appendChild(fn);row.appendChild(del);box.appendChild(row);
  } else {
    var hint=document.createElement('div');hint.className='hint';hint.style.marginBottom='6px';hint.textContent="Hozircha material yo'q";box.appendChild(hint);
  }
  var row2=document.createElement('div');row2.className='aud';
  var inp=document.createElement('input');inp.type='file';inp.accept='application/pdf';inp.className='matf';inp.style.maxWidth='190px';
  var btn=document.createElement('button');btn.className='up';btn.textContent='Yuklash';btn.onclick=function(){uploadMaterial(sec,btn,inp);};
  row2.appendChild(inp);row2.appendChild(btn);box.appendChild(row2);
}
async function uploadMaterial(sec,btn,inp){
  if(!inp.files.length)return alert('PDF tanlang');
  var fd=new FormData();fd.append('level',LEVEL);fd.append('day',DAY);fd.append('section',sec);fd.append('file',inp.files[0]);
  btn.disabled=true;btn.textContent='...';
  var r=await fetch('/admin/upload-material',{method:'POST',body:fd});btn.disabled=false;btn.textContent='Yuklash';
  if(r.ok){var j=await r.json();MATERIALS[sec]={url:j.url,filename:j.filename};renderMaterial(sec);}
  else{var j=await r.json().catch(function(){return{};});alert('Xato: '+(j.error||r.status));}
}
async function delMaterial(sec){
  if(!confirm("Material o'chirilsinmi?"))return;
  var r=await fetch('/admin/delete-material',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:LEVEL,day:DAY,section:sec})});
  if(r.ok){delete MATERIALS[sec];renderMaterial(sec);}else alert('Xato');
}
load();
</script></body></html>
"""

# --- ADMIN API endpoints ---
@flask_app.route("/admin/set-reminder", methods=["POST"])
def admin_set_reminder():
    check_owner()
    data = request.json or {}
    hour = str(data.get("hour", "9")); text = data.get("text", "").strip()
    set_setting("reminder_hour", hour)
    if text: set_setting("reminder_text", text)
    reschedule_reminder(int(hour))
    return {"ok": True}

@flask_app.route("/admin/add-admin", methods=["POST"])
@require_admin
def admin_add_admin():
    check_owner()
    d = request.json or {}
    u = (d.get("username") or "").strip(); p = d.get("password") or ""
    if not u or not p:
        return {"ok": False, "error": "login/parol yo'q"}, 400
    if u == ADMIN_USER:
        return {"ok": False, "error": "bu login band"}, 400
    add_admin_db(u, p, "teacher")
    return {"ok": True}

@flask_app.route("/admin/delete-admin", methods=["POST"])
@require_admin
def admin_delete_admin():
    check_owner()
    d = request.json or {}
    delete_admin_db((d.get("username") or "").strip())
    return {"ok": True}

@flask_app.route("/admin/user-access", methods=["POST"])
@require_admin
def admin_user_access():
    check_owner()
    d = request.json or {}
    try:
        uid = int(d.get("user_id"))
    except Exception:
        return {"ok": False, "error": "user_id"}, 400
    sections = d.get("sections") or []
    mode = d.get("mode") or "trial"
    until = None
    if mode == "dated":
        u = (d.get("until") or "").strip()
        if not u:
            return {"ok": False, "error": "until yo'q"}, 400
        until = u + " 23:59:59"
    set_user_access(uid, sections, mode, until)
    return {"ok": True}

@flask_app.route("/admin/user-block", methods=["POST"])
@require_admin
def admin_user_block():
    check_owner()
    d = request.json or {}
    try:
        uid = int(d.get("user_id"))
    except Exception:
        return {"ok": False, "error": "user_id"}, 400
    set_user_blocked(uid, bool(d.get("blocked", True)))
    return {"ok": True}

@flask_app.route("/admin/toggle-access", methods=["POST"])
@require_admin
def admin_toggle_access():
    check_owner()
    cur = "0" if get_setting("access_enabled") != "0" else "1"
    set_setting("access_enabled", cur)
    return {"ok": True, "access_enabled": cur}

@flask_app.route("/admin/video-add", methods=["POST"])
@require_admin
def admin_video_add():
    check_api_auth()
    d = request.json or {}
    sec = d.get("section") or "talaffuz"
    vid = add_video(sec)
    return {"ok": True, "id": vid}

@flask_app.route("/admin/video-save", methods=["POST"])
@require_admin
def admin_video_save():
    check_api_auth()
    d = request.json or {}
    save_videos(d.get("lessons") or [])
    return {"ok": True}

@flask_app.route("/admin/video-delete", methods=["POST"])
@require_admin
def admin_video_delete():
    check_api_auth()
    d = request.json or {}
    try:
        delete_video(int(d.get("id")))
    except Exception:
        return {"ok": False, "error": "id"}, 400
    return {"ok": True}

@flask_app.route("/admin/video-task", methods=["POST"])
@require_admin
def admin_video_task():
    check_api_auth()
    vid = request.form.get("id"); f = request.files.get("file")
    if not (vid and f):
        return {"ok": False, "error": "ma'lumot to'liq emas"}, 400
    if not r2_configured():
        return {"ok": False, "error": "R2 sozlanmagan"}, 400
    try:
        url = upload_video_task_to_r2(f.read(), int(vid))
        set_video_task(int(vid), url)
    except Exception as e:
        logger.exception("Video task upload xato")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "url": url}

@flask_app.route("/admin/save-ui", methods=["POST"])
def admin_save_ui():
    check_owner()
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

@flask_app.route("/admin/ai-topup", methods=["POST"])
def admin_ai_topup():
    check_api_auth()
    if not ai_configured():
        return {"ok": False, "error": "AI sozlanmagan (ANTHROPIC_API_KEY yo'q)"}, 400
    d = request.json or {}
    level = d.get("level"); day = d.get("day")
    if not (level and day):
        return {"ok": False, "error": "ma'lumot to'liq emas"}, 400
    txt = get_stored_pdf_text(level, int(day))
    if not txt:
        return {"ok": False, "error": "Bu kun uchun saqlangan PDF topilmadi. Avval «AI bilan to'ldirish» orqali PDF yuklang."}, 400
    try:
        res = ai_topup(txt, level, int(day))
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "formulas": res.get("formulas", []), "dialog": res.get("dialog", [])}

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
        import io
        from pypdf import PdfReader
        data_bytes = f.read()
        reader = PdfReader(io.BytesIO(data_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        if not text.strip():
            return {"ok": False, "error": "PDF dan matn topilmadi (skaner/rasm bo'lishi mumkin)"}, 400
        # PDF ni saqlash (chalkashmaslik uchun ko'rinib turadi)
        if r2_configured():
            try:
                url = upload_pdf_to_r2(data_bytes, level, int(day))
                save_lesson_file(level, int(day), f.filename or "fayl.pdf", url)
            except Exception:
                logger.exception("PDF saqlash xato (davom etamiz)")
        data = ai_generate_lesson(text, level, int(day))
    except Exception as e:
        logger.exception("AI fill xato")
        return {"ok": False, "error": str(e)}, 500
    # DIQQAT: darrov saqlamaymiz — admin ko'rib chiqib, editorda "Saqlash" bossa yoziladi
    return {"ok": True, "data": data}

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
    check_owner()
    d = request.json or {}
    text = (d.get("text") or "").strip()
    btns = d.get("buttons") or []
    send_at = (d.get("send_at") or "").strip()
    if not text:
        return {"ok": False, "error": "matn yo'q"}, 400
    if send_at:
        send_at = send_at.replace("T", " ")
        save_scheduled(text, btns, send_at)
        return {"ok": True, "scheduled": True}
    threading.Thread(target=send_broadcast_sync, args=(text, btns)).start()
    return {"ok": True}

@flask_app.route("/admin/segments")
@require_admin
def admin_segments():
    check_owner()
    def pack(kind, key, users):
        ku = kind + ":" + str(key)
        return {"key": key, "count": len(users),
                "users": [{"id": u["user_id"], "name": u.get("first_name") or "—",
                           "username": u.get("username") or "", "gap": u.get("gap")} for u in users[:80]],
                "tpl": _seg_tpl(ku), "auto": _seg_auto(ku)}
    inactive = [pack("in", d, seg_inactive_users(d)) for d in INACTIVE_SEGMENTS]
    streak = [pack("st", n, seg_streak_users(n)) for n in STREAK_SEGMENTS]
    cu = seg_inactive_users(CONTACT_MIN_DAYS, ge=True)
    contact = [{"id": u["user_id"], "name": u.get("first_name") or "—",
                "username": u.get("username") or "", "gap": u.get("gap")} for u in cu[:200]]
    return jsonify({"inactive": inactive, "streak": streak, "contact": contact,
                    "contact_count": len(cu), "contact_min": CONTACT_MIN_DAYS})

@flask_app.route("/admin/segment-save", methods=["POST"])
@require_admin
def admin_segment_save():
    check_owner()
    d = request.json or {}
    kind = (d.get("kind") or "").strip(); key = str(d.get("key") or "").strip()
    if kind not in ("in", "st") or not key:
        return {"ok": False, "error": "segment"}, 400
    ku = "seg:" + kind + ":" + key
    if "tpl" in d: set_setting(ku + ":tpl", d.get("tpl") or "")
    if "auto" in d: set_setting(ku + ":auto", "1" if d.get("auto") else "0")
    return {"ok": True}

@flask_app.route("/admin/segment-send", methods=["POST"])
@require_admin
def admin_segment_send():
    check_owner()
    d = request.json or {}
    kind = (d.get("kind") or "").strip(); key = str(d.get("key") or "").strip()
    text = (d.get("text") or "").strip()
    if kind not in ("in", "st") or not key:
        return {"ok": False, "error": "segment"}, 400
    if not text:
        return {"ok": False, "error": "matn yo'q"}, 400
    set_setting("seg:" + kind + ":" + key + ":tpl", text)
    ids = _seg_user_ids(kind, key)
    if not ids:
        return {"ok": True, "count": 0}
    threading.Thread(target=send_segment_sync, args=(ids, text)).start()
    return {"ok": True, "count": len(ids)}

@flask_app.route("/admin/segment-send-media", methods=["POST"])
@require_admin
def admin_segment_send_media():
    check_owner()
    import tempfile
    kind = (request.form.get("kind") or "").strip(); key = str(request.form.get("key") or "").strip()
    mtype = (request.form.get("mtype") or "").strip()
    caption = (request.form.get("caption") or "").strip()
    f = request.files.get("file")
    if kind not in ("in", "st") or not key:
        return {"ok": False, "error": "segment"}, 400
    if not f or mtype not in ("photo", "video", "audio"):
        return {"ok": False, "error": "fayl"}, 400
    ext = os.path.splitext(f.filename or "")[1] or ""
    fd, tmp = tempfile.mkstemp(suffix=ext); os.close(fd); f.save(tmp)
    if caption:
        set_setting("seg:" + kind + ":" + key + ":tpl", caption)
    ids = _seg_user_ids(kind, key)
    if not ids:
        try: os.remove(tmp)
        except Exception: pass
        return {"ok": True, "count": 0}
    threading.Thread(target=send_segment_sync, args=(ids, caption, mtype, tmp)).start()
    return {"ok": True, "count": len(ids)}

@flask_app.route("/admin/schedule-cancel", methods=["POST"])
@require_admin
def admin_schedule_cancel():
    check_owner()
    d = request.json or {}
    try:
        cancel_scheduled(int(d.get("id")))
    except Exception:
        return {"ok": False, "error": "id"}, 400
    return {"ok": True}

@flask_app.route("/admin/delete-file", methods=["POST"])
@require_admin
def admin_delete_file():
    check_owner()
    d = request.json or {}
    level = (d.get("level") or "").strip()
    try:
        day = int(d.get("day"))
    except Exception:
        return {"ok": False, "error": "day"}, 400
    # R2 dan PDF ni o'chirish (best-effort), dars kontenti saqlanadi
    try:
        if r2_configured():
            get_r2_client().delete_object(Bucket=R2_BUCKET, Key=f"pdfs/{level}/{day}.pdf")
    except Exception as e:
        print("R2 delete-file error:", e)
    delete_lesson_file(level, day)
    return {"ok": True}

@flask_app.route("/admin/upload-material", methods=["POST"])
@require_admin
def admin_upload_material():
    level = (request.form.get("level") or "").strip()
    section = (request.form.get("section") or "").strip()
    try:
        day = int(request.form.get("day"))
    except Exception:
        return {"ok": False, "error": "day"}, 400
    f = request.files.get("file")
    if not f or not section:
        return {"ok": False, "error": "fayl/bo'lim"}, 400
    if not r2_configured():
        return {"ok": False, "error": "R2 sozlanmagan"}, 400
    try:
        url = upload_material_to_r2(f.read(), level, day, section)
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    fname = f.filename or "material.pdf"
    save_material(level, day, section, fname, url)
    return {"ok": True, "url": url, "filename": fname}

@flask_app.route("/admin/delete-material", methods=["POST"])
@require_admin
def admin_delete_material():
    d = request.json or {}
    level = (d.get("level") or "").strip()
    section = (d.get("section") or "").strip()
    try:
        day = int(d.get("day"))
    except Exception:
        return {"ok": False, "error": "day"}, 400
    try:
        if r2_configured():
            get_r2_client().delete_object(Bucket=R2_BUCKET, Key=f"materials/{level}/{day}/{section}.pdf")
    except Exception as e:
        print("R2 delete-material error:", e)
    delete_material(level, day, section)
    return {"ok": True}

@flask_app.route("/admin/broadcast-media", methods=["POST"])
def admin_broadcast_media():
    check_owner()
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
    try:
        from waitress import serve
        logger.info("Waitress server ishga tushdi (threads=8)")
        serve(flask_app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        logger.warning("waitress topilmadi — Flask dev server bilan ishlayapti")
        flask_app.run(host="0.0.0.0", port=port)

# --- BOT ---
bot_app = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    ensure_user_trial(user.id, user.first_name, user.username)
    kb = [[InlineKeyboardButton("✍️ Adminga yozish", url="https://t.me/" + (get_ui().get("link_admin") or "zamira_russian").lstrip("@").strip())]]
    tpl = get_ui().get("bot_welcome") or DEFAULT_UI["bot_welcome"]
    text = tpl.replace("{ism}", user.first_name or "").replace("{id}", str(user.id))
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 /start — Bosh sahifa")

def send_reminders():
    if not bot_app: return
    from telegram.error import Forbidden, RetryAfter
    users = get_broadcast_users(); text = get_setting("reminder_text") or DEFAULT_REMINDER_TEXT
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Darsga kirish", web_app=WebAppInfo(url=WEBAPP_URL))]])
    async def _s():
        for u in users:
            uid = u["user_id"]
            try:
                await bot_app.bot.send_message(chat_id=uid, text=text, reply_markup=kb)
            except RetryAfter as e:
                await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                try:
                    await bot_app.bot.send_message(chat_id=uid, text=text, reply_markup=kb)
                except Exception as e2:
                    logger.warning(f"Eslatma {uid}: {e2}")
            except Forbidden:
                mark_bot_blocked(uid)
            except Exception as e:
                logger.warning(f"Eslatma {uid}: {e}")
            await asyncio.sleep(0.04)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(_s()); loop.close()

def check_scheduled():
    if not bot_app: return
    now = datetime.now(pytz.timezone("Asia/Tashkent")).replace(tzinfo=None)
    try:
        due = get_scheduled_due(now)
    except Exception:
        logger.exception("Scheduled tekshirish xato"); return
    for m in due:
        try:
            btns = json.loads(m.get("btns") or "[]")
            send_broadcast_sync(m["text"], btns)
            mark_scheduled_sent(m["id"])
        except Exception:
            logger.exception("Scheduled yuborish xato")

def send_broadcast_sync(text, btns=None):
    if not bot_app: return
    from telegram.error import Forbidden, RetryAfter
    users = get_broadcast_users()
    markup = build_markup(btns)
    async def _s():
        for u in users:
            uid = u["user_id"]
            try:
                await bot_app.bot.send_message(chat_id=uid, text=text, reply_markup=markup)
            except RetryAfter as e:
                await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                try:
                    await bot_app.bot.send_message(chat_id=uid, text=text, reply_markup=markup)
                except Exception as e2:
                    logger.warning(f"Xabar {uid}: {e2}")
            except Forbidden:
                mark_bot_blocked(uid)
            except Exception as e:
                logger.warning(f"Xabar {uid}: {e}")
            await asyncio.sleep(0.04)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(_s()); loop.close()

def build_markup(btns):
    if not btns: return None
    rows = []
    for b in btns:
        label = (b.get("label") or "").strip()
        if not label: continue
        if b.get("type") == "app":
            rows.append([InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL))])
        else:
            url = (b.get("url") or "").strip()
            if url: rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None

def save_scheduled(text, btns, send_at):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO scheduled_messages (text, btns, send_at, sent) VALUES (%s,%s,%s,FALSE)",
                (text, json.dumps(btns or []), send_at))
    conn.commit(); cur.close(); conn.close()

def get_scheduled_pending():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM scheduled_messages WHERE sent=FALSE ORDER BY send_at")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def get_scheduled_due(now_naive):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM scheduled_messages WHERE sent=FALSE AND send_at <= %s", (now_naive,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def mark_scheduled_sent(mid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE scheduled_messages SET sent=TRUE WHERE id=%s", (mid,))
    conn.commit(); cur.close(); conn.close()

def cancel_scheduled(mid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM scheduled_messages WHERE id=%s", (mid,))
    conn.commit(); cur.close(); conn.close()

def send_media_broadcast_sync(mtype, path, caption):
    if not bot_app: return
    from telegram.error import Forbidden, RetryAfter
    users = get_broadcast_users()
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
            except RetryAfter as e:
                await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
            except Forbidden:
                mark_bot_blocked(uid)
            except Exception as e:
                logger.warning(f"Media {uid}: {e}")
            await asyncio.sleep(0.05)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_s())
    finally:
        loop.close()
        try:
            os.remove(path)
        except Exception:
            pass

# ===================== SEGMENTLI ESLATMALAR =====================
INACTIVE_SEGMENTS = [1, 3, 5]
STREAK_SEGMENTS = [5, 10, 20, 30, 40, 50, 60]
CONTACT_MIN_DAYS = 10

DEFAULT_SEG_TEMPLATES = {
    "in:1": "Salom! Bugun darsni o'tkazib yubordingiz. Bir oz vaqt ajrating, 10 daqiqa ham yetadi. Davom etamizmi?",
    "in:3": "3 kundan beri ko'rinmadingiz. Rus tilini unutmaslik uchun bugun qaytib keling!",
    "in:5": "5 kun bo'ldi. Darslar sizni kutyapti. Keling, qaytadan boshlaymiz!",
    "st:5": "Zo'r! 5 kun ketma-ket o'qidingiz. Shu sur'atni davom ettiring!",
    "st:10": "10 kun ketma-ket! Ajoyib natija, siz bilan faxrlanamiz!",
    "st:20": "20 kun! Bu jiddiy intizom. Davom eting!",
    "st:30": "30 kun ketma-ket, bir oy! Siz haqiqiy yutuqchisiz!",
    "st:40": "40 kun! To'xtamang, natija ko'rinib turibdi!",
    "st:50": "50 kun ketma-ket! Ajoyib, manzilga yaqinsiz!",
    "st:60": "60 kun! Mukammal intizom. Tabriklaymiz!",
}

def _seg_tpl(ku):
    return get_setting("seg:" + ku + ":tpl") or DEFAULT_SEG_TEMPLATES.get(ku, "")

def _seg_auto(ku):
    return get_setting("seg:" + ku + ":auto") == "1"

def seg_inactive_users(days, ge=False):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    op = ">=" if ge else "="
    cur.execute(
        "SELECT user_id, first_name, username, streak, "
        "(CURRENT_DATE - last_active::date) AS gap FROM users "
        "WHERE COALESCE(bot_blocked,FALSE)=FALSE AND COALESCE(blocked,FALSE)=FALSE "
        "AND last_active IS NOT NULL AND (CURRENT_DATE - last_active::date) " + op + " %s "
        "ORDER BY gap DESC, first_name", (days,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def seg_streak_users(n):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT user_id, first_name, username, streak FROM users "
        "WHERE COALESCE(bot_blocked,FALSE)=FALSE AND COALESCE(blocked,FALSE)=FALSE "
        "AND streak=%s ORDER BY first_name", (n,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

def _seg_user_ids(kind, key):
    us = seg_inactive_users(int(key)) if kind == "in" else seg_streak_users(int(key))
    return [u["user_id"] for u in us]

def send_segment_sync(user_ids, text, mtype=None, path=None):
    if not bot_app or not user_ids:
        if path:
            try: os.remove(path)
            except Exception: pass
        return
    from telegram.error import Forbidden, RetryAfter
    cap = text or None
    async def _s():
        fid = None
        for uid in user_ids:
            try:
                if mtype and path:
                    if mtype == "photo":
                        if fid is None:
                            with open(path, "rb") as fh: m = await bot_app.bot.send_photo(chat_id=uid, photo=fh, caption=cap)
                            fid = m.photo[-1].file_id
                        else: await bot_app.bot.send_photo(chat_id=uid, photo=fid, caption=cap)
                    elif mtype == "video":
                        if fid is None:
                            with open(path, "rb") as fh: m = await bot_app.bot.send_video(chat_id=uid, video=fh, caption=cap)
                            fid = m.video.file_id
                        else: await bot_app.bot.send_video(chat_id=uid, video=fid, caption=cap)
                    else:
                        if fid is None:
                            with open(path, "rb") as fh: m = await bot_app.bot.send_audio(chat_id=uid, audio=fh, caption=cap)
                            fid = m.audio.file_id
                        else: await bot_app.bot.send_audio(chat_id=uid, audio=fid, caption=cap)
                else:
                    await bot_app.bot.send_message(chat_id=uid, text=text)
            except RetryAfter as e:
                await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
            except Forbidden:
                mark_bot_blocked(uid)
            except Exception as e:
                logger.warning(f"Segment {uid}: {e}")
            await asyncio.sleep(0.04)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_s())
    finally:
        loop.close()
        if path:
            try: os.remove(path)
            except Exception: pass

def send_auto_segments():
    if not bot_app: return
    for d in INACTIVE_SEGMENTS:
        if _seg_auto("in:" + str(d)):
            try:
                us = seg_inactive_users(d); tpl = _seg_tpl("in:" + str(d))
                if us and tpl: send_segment_sync([u["user_id"] for u in us], tpl)
            except Exception: logger.exception("auto seg in")
    for n in STREAK_SEGMENTS:
        if _seg_auto("st:" + str(n)):
            try:
                us = seg_streak_users(n); tpl = _seg_tpl("st:" + str(n))
                if us and tpl: send_segment_sync([u["user_id"] for u in us], tpl)
            except Exception: logger.exception("auto seg st")


scheduler = BackgroundScheduler(timezone=TASHKENT_TZ)

def reschedule_reminder(hour):
    try: scheduler.remove_job("reminder")
    except Exception: pass
    scheduler.add_job(send_reminders, "cron", hour=hour, minute=0, id="reminder", replace_existing=True)
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
    scheduler.add_job(check_scheduled, "interval", minutes=1, id="sched_check", replace_existing=True)
    try:
        _sah = int(get_setting("seg_auto_hour") or "10")
    except Exception:
        _sah = 10
    scheduler.add_job(send_auto_segments, "cron", hour=_sah, minute=30, id="seg_auto", replace_existing=True)
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask ishga tushdi")
    run_bot()
