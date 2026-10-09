import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import Message, MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import history_tools
import image_tools
import notion_tools
import search_tools

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # les logs INFO de httpx contiennent le token Telegram dans l'URL
log = logging.getLogger("smolbot")
TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Paris"))
LLM_BASE_URL = os.environ.get("FREELLMAPI_BASE_URL", "http://freellmapi.railway.internal:3001/v1")
LLM_API_KEY = os.environ["FREELLMAPI_API_KEY"]
LLM_MODEL = os.environ.get("FREELLMAPI_MODEL", "auto")
llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

# Ids Telegram autorisés à régler /historique et /contexte (séparés par virgules/espaces).
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").replace(",", " ").split() if x.isdigit()
}

SYSTEM_PROMPT = """Tu es smolbot, un assistant Telegram personnel.
Réponds en français sauf si l'utilisateur écrit dans une autre langue.
Sois utile, direct et concis.
Tu participes à des conversations Telegram, parfois à plusieurs : tu ne réponds que
lorsqu'on te mentionne ou qu'on répond à l'un de tes messages. Chaque demande arrive sous
la forme « Message de <auteur> : <texte> », précédée des derniers messages de la
conversation (contexte) : utilise-les pour comprendre de quoi on parle. Pour remonter plus
loin, utilise get_recent_messages (n derniers messages) ou search_messages (recherche par
mots-clés) ; l'historique conservé est limité à quelques jours. N'invente jamais ce qui
n'apparaît pas dans l'historique.
Tu as accès à la recherche web : utilise web_search quand la question porte
sur des faits récents, l'actualité, ou des infos que tu ne connais pas.
Utilise web_read pour lire le contenu d'une page issue de la recherche
quand tu as besoin de citer ou résumer précisément.
Cite les sources (titre + URL) quand tu utilises le web.
Tu peux générer des images : utilise generate_image quand l'utilisateur
demande explicitement une image, un dessin, une illustration ou une photo.
Ne génère une image que sur demande explicite, pas pour illustrer tes réponses.
Tu as accès à Notion (uniquement les pages partagées sous les racines configurées) :
utilise notion_list pour lister les pages connues, notion_search pour retrouver
une page par mots-clés, notion_read pour lire son contenu, notion_append pour
ajouter du texte à la fin d'une page, notion_replace pour remplacer le contenu
d'une page (les sous-pages sont conservées), notion_create pour créer une sous-page.
N'écris dans Notion que sur demande explicite de l'utilisateur.
Refuse poliment toute lecture/écriture hors des pages autorisées."""

TOOLS = (
    search_tools.SEARCH_TOOLS
    + [image_tools.IMAGE_TOOL]
    + notion_tools.NOTION_TOOLS
    + (history_tools.HISTORY_TOOLS if history_tools.enabled() else [])
)


async def run_tool(name: str, args: dict, chat_id: int = 0, current_msg_id: int | None = None) -> str:
    if name == "web_search":
        q = args.get("query", "")
        n = args.get("max_results", search_tools.TAVILY_MAX_RESULTS)
        log.info("web_search: %s", q[:120])
        return await search_tools.tavily_search(q, int(n) if isinstance(n, int) else search_tools.TAVILY_MAX_RESULTS)
    if name == "web_read":
        urls = args.get("urls", [])
        q = args.get("query", "")
        log.info("web_read: %s", urls)
        return await search_tools.tavily_extract(urls, q)
    if name == "notion_list":
        log.info("notion_list")
        return await notion_tools.notion_list_pages()
    if name == "notion_search":
        q = args.get("query", "")
        log.info("notion_search: %s", q[:120])
        return await notion_tools.notion_search_pages(q)
    if name == "notion_read":
        pid = args.get("page_id", "")
        log.info("notion_read: %s", pid)
        return await notion_tools.notion_read_page(pid)
    if name == "notion_append":
        pid = args.get("page_id", "")
        log.info("notion_append: %s", pid)
        return await notion_tools.notion_append_page(pid, args.get("text", ""))
    if name == "notion_replace":
        pid = args.get("page_id", "")
        log.info("notion_replace: %s", pid)
        return await notion_tools.notion_replace_page(pid, args.get("text", ""))
    if name == "notion_create":
        log.info("notion_create: %s", args.get("title", "")[:120])
        return await notion_tools.notion_create_page(args.get("title", ""), args.get("content", ""), args.get("parent_id", ""))
    # Le chat est imposé par le code (jamais par le LLM) : le bot ne lit que la conversation en cours.
    if name == "get_recent_messages":
        log.info("get_recent_messages: %s", args.get("n"))
        return await history_tools.tool_get_recent(chat_id, current_msg_id, args.get("n"))
    if name == "search_messages":
        log.info("search_messages: %s", str(args.get("query", ""))[:120])
        return await history_tools.tool_search(chat_id, current_msg_id, args.get("query", ""), args.get("limit"))
    return f"Outil inconnu: {name}"


# --------------------------------------------------------------------------- journal Telegram

def author_of(msg: Message) -> str:
    if msg.from_user:
        return msg.from_user.full_name
    if msg.sender_chat:
        return msg.sender_chat.title or "Canal"
    return "Inconnu"


async def log_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Journalise tout message texte vu par le bot (handler du groupe -1, avant message_llm)."""
    msg = update.message
    if not msg:
        return
    text = msg.text or msg.caption
    if not text:
        return
    await history_tools.add_message(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        author=author_of(msg),
        text=text,
        sent_at=msg.date,
        user_id=msg.from_user.id if msg.from_user else None,
        reply_to_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
    )


async def log_outgoing(sent: Message, text: str):
    """Telegram ne renvoie pas ses propres messages au bot : on les journalise nous-mêmes."""
    await history_tools.add_message(
        chat_id=sent.chat_id,
        message_id=sent.message_id,
        author=history_tools.BOT_NAME,
        text=text,
        sent_at=sent.date,
        reply_to_id=sent.reply_to_message.message_id if sent.reply_to_message else None,
        from_assistant=True,
    )


async def send_text(msg: Message, text: str):
    """Répond (découpé en blocs de 4000 caractères) et journalise chaque bloc."""
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        sent = await msg.reply_text(chunk)
        await log_outgoing(sent, chunk)


def is_addressed(msg: Message, bot) -> bool:
    """Vrai si le bot doit répondre : chat privé, @mention explicite, ou réponse à un de ses messages."""
    if msg.chat.type == ChatType.PRIVATE:
        return True
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot.id:
        return True
    if bot.username and msg.entities:
        handle = f"@{bot.username}".lower()
        for ent in msg.entities:
            if ent.type == MessageEntity.MENTION and msg.parse_entity(ent).lower() == handle:
                return True
    return False


async def build_user_content(msg: Message, n: int) -> str:
    """Contexte (n derniers messages) + éventuel message cité + message à traiter."""
    parts = []
    recent = await history_tools.context_block(msg.chat_id, n, msg.message_id)
    if recent:
        parts.append("Contexte — derniers messages de la conversation :\n" + recent + "\n---")
    reply = msg.reply_to_message
    reply_text = (reply.text or reply.caption) if reply else None
    if reply_text:
        quoted = " ".join(reply_text.split())[:500]
        parts.append(f"(en réponse à {author_of(reply)} : « {quoted} »)")
    parts.append(f"Message de {author_of(msg)} : {msg.text}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- images

async def handle_image_tool(update: Update, prompt: str) -> str:
    """Génère l'image et l'envoie directement. Retourne le résumé pour le LLM."""
    if not prompt.strip():
        return "Echec: prompt vide, demande une description à l'utilisateur."
    try:
        log.info("generate_image: %s", prompt[:150])
        kind, payload = await image_tools.generate_image_bytes(prompt)
        if kind == "error":
            return f"Echec génération image: {payload}"
        sent = await update.message.reply_photo(photo=payload, caption=prompt[:900])
        await log_outgoing(sent, f"[image générée] {prompt[:300]}")
        return f"Image générée et envoyée à l'utilisateur (prompt: {prompt[:200]}). Confirme brièvement."
    except Exception as e:
        log.warning("Image generation failed: %s", e)
        return f"Echec génération image: {e}"


# --------------------------------------------------------------------------- commandes

def now_text():
    now = datetime.now(TZ)
    return f"Il est {now:%H:%M:%S} ({now:%d/%m/%Y}, {TZ.key})"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salut ! Je suis smolbot. En groupe, mentionne-moi ou réponds à l'un de mes messages.\n"
        "Commandes : /heure, /image <description>, /historique [jours], /contexte [n]."
    )


async def heure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(now_text())


async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt.strip():
        await update.message.reply_text("Usage : /image <description de l'image>")
        return
    await update.message.reply_text("🎨 Génération en cours…")
    try:
        kind, payload = await image_tools.generate_image_bytes(prompt)
    except Exception as e:
        log.warning("Image generation failed: %s", e)
        await update.message.reply_text(f"Désolé, la génération d'image a échoué : {e}")
        return
    if kind == "error":
        await update.message.reply_text(f"Désolé, la génération d'image a échoué : {payload}")
        return
    sent = await update.message.reply_photo(photo=payload, caption=prompt[:900])
    await log_outgoing(sent, f"[image générée] {prompt[:300]}")


def _can_configure(update: Update) -> bool:
    """Admins déclarés ; sans ADMIN_USER_IDS, uniquement en chat privé (réglages propres à ce chat)."""
    user = update.effective_user
    if user and user.id in ADMIN_USER_IDS:
        return True
    return not ADMIN_USER_IDS and update.effective_chat.type == ChatType.PRIVATE


async def _setting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, *, cmd: str, label: str,
                       current: int, max_value: int, setter):
    if not history_tools.enabled():
        await update.message.reply_text("Journal désactivé (DATABASE_URL manquante).")
        return
    if not context.args:
        await update.message.reply_text(f"{label} : {current}. Pour changer : /{cmd} <1-{max_value}>")
        return
    if not _can_configure(update):
        await update.message.reply_text("Réglage réservé à l'administrateur du bot.")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        value = 0
    if not 1 <= value <= max_value:
        await update.message.reply_text(f"Valeur invalide. Usage : /{cmd} <1-{max_value}>")
        return
    if await setter(update.effective_chat.id, value):
        await update.message.reply_text(f"{label} : {value}.")
    else:
        await update.message.reply_text("Echec de l'enregistrement du réglage.")


async def historique_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days, _ = await history_tools.get_settings(update.effective_chat.id)
    await _setting_cmd(update, context, cmd="historique", label="Historique conservé (jours)",
                       current=days, max_value=history_tools.MAX_RETENTION_DAYS,
                       setter=history_tools.set_retention_days)


async def contexte_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, n = await history_tools.get_settings(update.effective_chat.id)
    await _setting_cmd(update, context, cmd="contexte", label="Messages de contexte injectés",
                       current=n, max_value=history_tools.MAX_RECENT_N,
                       setter=history_tools.set_recent_n)


# --------------------------------------------------------------------------- réponse LLM

async def message_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    if not is_addressed(msg, context.bot):
        return
    chat_id = msg.chat_id
    _, n = await history_tools.get_settings(chat_id)
    user_content = await build_user_content(msg, n)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        answer = None
        for _ in range(3):
            response = await llm.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=1024,
            )
            llm_msg = response.choices[0].message
            log.info("LLM response: model=%s finish=%s tool_calls=%s",
                     response.model, response.choices[0].finish_reason,
                     len(llm_msg.tool_calls or []))
            if not llm_msg.tool_calls:
                answer = llm_msg.content or "Je n'ai pas de réponse."
                break
            messages.append({
                "role": "assistant",
                "content": llm_msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments}}
                    for tc in llm_msg.tool_calls
                ],
            })
            for tc in llm_msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name == "generate_image":
                    result = await handle_image_tool(update, args.get("prompt", ""))
                else:
                    result = await run_tool(tc.function.name, args, chat_id, msg.message_id)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            response = await llm.chat.completions.create(
                model=LLM_MODEL, messages=messages, max_tokens=1024)
            answer = response.choices[0].message.content or "Je n'ai pas de réponse."
        await send_text(msg, answer)
    except Exception:
        log.exception("LLM request failed")
        await msg.reply_text("Désolé, je n'arrive pas à joindre le serveur LLM pour le moment.")


# --------------------------------------------------------------------------- démarrage

async def on_startup(app: Application):
    if history_tools.enabled():
        try:
            await history_tools.init()
            log.info("Journal des messages prêt")
        except Exception:
            log.exception("Init du journal échouée (nouvel essai au prochain message)")


async def on_shutdown(app: Application):
    await history_tools.close()


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    # concurrent_updates : l'état est en base, et un message reçu pendant qu'une réponse LLM
    # est en cours doit être journalisé tout de suite.
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["heure", "time"], heure))
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(CommandHandler("historique", historique_cmd))
    app.add_handler(CommandHandler("contexte", contexte_cmd))
    # Groupe -1 : s'exécute avant message_llm sur le même update, et journalise TOUS les messages.
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, log_incoming), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_llm))
    log.info("smolbot démarré (long polling), LLM=%s tavily=%s image=%s notion=%s journal=%s tools=%d",
             LLM_BASE_URL, "on" if search_tools.TAVILY_API_KEY else "off", image_tools.IMAGE_MODEL,
             f"on({len(notion_tools.NOTION_ROOTS)} racines)" if notion_tools.NOTION_TOKEN and notion_tools.NOTION_ROOTS else "off",
             "on" if history_tools.enabled() else "off", len(TOOLS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
