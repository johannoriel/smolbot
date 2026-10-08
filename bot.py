import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("smolbot")
TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Paris"))
LLM_BASE_URL = os.environ.get("FREELLMAPI_BASE_URL", "http://freellmapi.railway.internal:3001/v1")
LLM_API_KEY = os.environ["FREELLMAPI_API_KEY"]
LLM_MODEL = os.environ.get("FREELLMAPI_MODEL", "auto")
llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
SYSTEM_PROMPT = """Tu es smolbot, un assistant Telegram personnel.
Réponds en français sauf si l'utilisateur écrit dans une autre langue.
Sois utile, direct et concis. Tu peux converser normalement.
Tu n'as pas accès au web ni aux outils externes dans cette première version."""

def now_text():
    now = datetime.now(TZ)
    return f"Il est {now:%H:%M:%S} ({now:%d/%m/%Y}, {TZ.key})"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salut ! Je suis smolbot. Envoie-moi un message et je te réponds avec le LLM. La commande /heure donne l'heure exacte.")

async def heure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(now_text())

async def message_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    try:
        response = await llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": update.message.text}],
            max_tokens=512,
        )
        answer = response.choices[0].message.content or "Je n'ai pas de réponse."
        log.info("LLM response routed via %s", response.headers.get("x-routed-via", "unknown"))
        await update.message.reply_text(answer)
    except Exception:
        log.exception("LLM request failed")
        await update.message.reply_text("Désolé, je n'arrive pas à joindre le serveur LLM pour le moment.")

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["heure", "time"], heure))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_llm))
    log.info("smolbot démarré (long polling), LLM=%s", LLM_BASE_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
