import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]  # Railway URL, masalan: https://zamira-bot.up.railway.app


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "Do'stim"

    keyboard = [[
        InlineKeyboardButton(
            text="📚 Darsni boshlash",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Salom, {name}! 👋\n\n"
        f"*Zamira Russian* — ruscha gapirish kursi.\n\n"
        f"Har kuni 10 daqiqa mashq qiling va 30 kunda begona bilan ruscha gaplasha olasiz. 🇷🇺\n\n"
        f"Boshlashga tayyormisiz?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *Buyruqlar:*\n\n"
        "/start — Bosh sahifa\n"
        "/help — Yordam\n\n"
        "Savollar bo'lsa: @zamira\\_russian",
        parse_mode="Markdown"
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
