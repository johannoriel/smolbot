import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("smolbot")

TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Paris"))


def now_text() -> str:
    now = datetime.now(TZ)
    return f"Il est {now:%H:%M:%S} ({now:%d/%m/%Y}, {TZ.key})"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Salut ! Envoie-moi n'importe quel message (ou /heure) et je te donne l'heure."
    )


async def heure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(now_text())


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["heure", "time"], heure))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, heure))
    log.info("smolbot démarré (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
