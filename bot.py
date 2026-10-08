import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import image_tools
import notion_tools
import search_tools

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("smolbot")
TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Paris"))
LLM_BASE_URL = os.environ.get("FREELLMAPI_BASE_URL", "http://freellmapi.railway.internal:3001/v1")
LLM_API_KEY = os.environ["FREELLMAPI_API_KEY"]
LLM_MODEL = os.environ.get("FREELLMAPI_MODEL", "auto")
llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

HISTORY_MAX_ENTRIES = int(os.environ.get("HISTORY_MAX_ENTRIES", "30"))
_history: dict[int, list[dict]] = {}
_no_history: set[int] = set()

SYSTEM_PROMPT = """Tu es smolbot, un assistant Telegram personnel.
Réponds en français sauf si l'utilisateur écrit dans une autre langue.
Sois utile, direct et concis. Tu as la mémoire de la conversation en cours.
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

TOOLS = search_tools.SEARCH_TOOLS + [image_tools.IMAGE_TOOL] + notion_tools.NOTION_TOOLS


async def run_tool(name: str, args: dict) -> str:
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
    return f"Outil inconnu: {name}"


async def handle_image_tool(update: Update, prompt: str) -> str:
    """Génère l'image et l'envoie directement. Retourne le résumé pour le LLM."""
    if not prompt.strip():
        return "Echec: prompt vide, demande une description à l'utilisateur."
    try:
        log.info("generate_image: %s", prompt[:150])
        kind, payload = await image_tools.generate_image_bytes(prompt)
        if kind == "error":
            return f"Echec génération image: {payload}"
        await update.message.reply_photo(photo=payload, caption=prompt[:900])
        return f"Image générée et envoyée à l'utilisateur (prompt: {prompt[:200]}). Confirme brièvement."
    except Exception as e:
        log.warning("Image generation failed: %s", e)
        return f"Echec génération image: {e}"


def now_text():
    now = datetime.now(TZ)
    return f"Il est {now:%H:%M:%S} ({now:%d/%m/%Y}, {TZ.key})"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salut ! Je suis smolbot, avec mémoire de conversation.\n"
        "Commandes : /heure, /image <description>, /new (nouvelle conversation), /test (mode sans historique)."
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
    await update.message.reply_photo(photo=payload, caption=prompt[:900])


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _history.pop(chat_id, None)
    _no_history.discard(chat_id)
    await update.message.reply_text("Nouvelle conversation (historique activé).")


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _no_history.add(chat_id)
    await update.message.reply_text("Mode sans historique : je ne retiens rien jusqu'au prochain /new.")


async def message_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_text = update.message.text
    past = [] if chat_id in _no_history else _history.get(chat_id, [])
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, *past, {"role": "user", "content": user_text}]
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
            msg = response.choices[0].message
            log.info("LLM response: model=%s finish=%s tool_calls=%s",
                     response.model, response.choices[0].finish_reason,
                     len(msg.tool_calls or []))
            if not msg.tool_calls:
                answer = msg.content or "Je n'ai pas de réponse."
                break
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name == "generate_image":
                    result = await handle_image_tool(update, args.get("prompt", ""))
                else:
                    result = await run_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            response = await llm.chat.completions.create(
                model=LLM_MODEL, messages=messages, max_tokens=1024)
            answer = response.choices[0].message.content or "Je n'ai pas de réponse."
        if chat_id not in _no_history:
            _history[chat_id] = [*past, {"role": "user", "content": user_text},
                                 {"role": "assistant", "content": answer}][-HISTORY_MAX_ENTRIES:]
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i:i + 4000])
    except Exception:
        log.exception("LLM request failed")
        await update.message.reply_text("Désolé, je n'arrive pas à joindre le serveur LLM pour le moment.")


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["heure", "time"], heure))
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_llm))
    log.info("smolbot démarré (long polling), LLM=%s tavily=%s image=%s notion=%s tools=%d",
             LLM_BASE_URL, "on" if search_tools.TAVILY_API_KEY else "off", image_tools.IMAGE_MODEL,
             f"on({len(notion_tools.NOTION_ROOTS)} racines)" if notion_tools.NOTION_TOKEN and notion_tools.NOTION_ROOTS else "off",
             len(TOOLS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
