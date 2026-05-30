import threading
import os
import logging
from flask import Flask, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]

# --- Flask app ---
flask_app = Flask(__name__, static_folder="static")

@flask_app.route("/")
def index():
    return send_from_directory("static", "index.html")

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# --- Telegram bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "Do'stim"
    keyboard = [[
        InlineKeyboardButton(
            text="📚 Darsni boshlash",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    await update.message.reply_text(
        f"Salom, {name}! 👋\n\n"
        f"*Zamira Russian* — ruscha gapirish kursi.\n\n"
        f"Har kuni 10 daqiqa — 30 kunda begona bilan ruscha gaplasha olasiz. 🇷🇺",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *Buyruqlar:*\n\n/start — Bosh sahifa\n/help — Yordam",
        parse_mode="Markdown"
    )

def run_bot():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    logger.info("Bot ishga tushdi...")
    app.run_polling()

# --- Main ---
if __name__ == "__main__":
    # Flask ni background threadda ishlatamiz
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server ishga tushdi")
    # Bot main threadda
    run_bot()
