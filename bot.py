import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
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

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
TAVILY_TIMEOUT_S = float(os.environ.get("TAVILY_TIMEOUT_S", "20"))

SYSTEM_PROMPT = """Tu es smolbot, un assistant Telegram personnel.
Réponds en français sauf si l'utilisateur écrit dans une autre langue.
Sois utile, direct et concis.
Tu as accès à la recherche web : utilise web_search quand la question porte
sur des faits récents, l'actualité, ou des infos que tu ne connais pas.
Utilise web_read pour lire le contenu d'une page issue de la recherche
quand tu as besoin de citer ou résumer précisément.
Cite les sources (titre + URL) quand tu utilises le web."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Recherche sur le web actuel via Tavily. A utiliser pour les faits "
                "recents, l'actualite, ou toute info hors de tes connaissances. "
                "Retourne extraits + URLs + une reponse generee."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Requête de recherche"},
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre de résultats (3-10)",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": (
                "Lit le contenu complet d'une ou plusieurs pages web (URLs issues "
                "de web_search) pour resumer ou citer precisement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1 à 3 URLs à lire",
                        "minItems": 1,
                        "maxItems": 3,
                    },
                    "query": {
                        "type": "string",
                        "description": "Question d'origine, pour reranker les passages",
                    },
                },
                "required": ["urls"],
            },
        },
    },
]


async def tavily_search(query: str, max_results: int = 5) -> str:
    if not TAVILY_API_KEY:
        return "Erreur: recherche web non configurée (TAVILY_API_KEY manquante)."
    max_results = max(1, min(10, max_results or TAVILY_MAX_RESULTS))
    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT_S) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
                json={
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": "advanced",
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Tavily search failed: %s", e)
        return f"Erreur recherche web: {e}"
    lines = []
    if data.get("answer"):
        lines.append(f"Réponse Tavily: {data['answer']}")
    for i, res in enumerate(data.get("results", []), 1):
        title = res.get("title", "sans titre")
        url = res.get("url", "")
        content = (res.get("content") or "")[:1500]
        lines.append(f"[{i}] {title}\n{url}\n{content}")
    return "\n\n".join(lines) or "Aucun résultat."


async def tavily_extract(urls: list[str], query: str = "") -> str:
    if not TAVILY_API_KEY:
        return "Erreur: lecture web non configurée (TAVILY_API_KEY manquante)."
    urls = urls[:3]
    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT_S + 10) as client:
            payload = {"urls": urls, "extract_depth": "basic", "format": "markdown"}
            if query:
                payload["query"] = query
                payload["chunks_per_source"] = 3
            r = await client.post(
                "https://api.tavily.com/extract",
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Tavily extract failed: %s", e)
        return f"Erreur lecture web: {e}"
    lines = []
    for res in data.get("results", []):
        url = res.get("url", "")
        content = (res.get("raw_content") or "")[:6000]
        lines.append(f"Contenu de {url}:\n{content}")
    for fail in data.get("failed_results", []):
        lines.append(f"Echec lecture {fail.get('url')}: {fail.get('error')}")
    return "\n\n".join(lines) or "Aucun contenu extrait."


async def run_tool(name: str, args: dict) -> str:
    if name == "web_search":
        q = args.get("query", "")
        n = args.get("max_results", TAVILY_MAX_RESULTS)
        log.info("web_search: %s", q[:120])
        return await tavily_search(q, int(n) if isinstance(n, int) else TAVILY_MAX_RESULTS)
    if name == "web_read":
        urls = args.get("urls", [])
        q = args.get("query", "")
        log.info("web_read: %s", urls)
        return await tavily_extract(urls, q)
    return f"Outil inconnu: {name}"


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
    user_text = update.message.text
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
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
                result = await run_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            response = await llm.chat.completions.create(
                model=LLM_MODEL, messages=messages, max_tokens=1024)
            answer = response.choices[0].message.content or "Je n'ai pas de réponse."
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_llm))
    log.info("smolbot démarré (long polling), LLM=%s tavily=%s",
             LLM_BASE_URL, "on" if TAVILY_API_KEY else "off")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
