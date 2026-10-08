import base64
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

IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b")
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "")  # ex. "1024x1024", vide = défaut du provider
IMAGE_TIMEOUT_S = float(os.environ.get("IMAGE_TIMEOUT_S", "120"))

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_ROOTS = [
    p.strip().replace("-", "")
    for blob in (os.environ.get("NOTION_ROOT_IDS", ""), os.environ.get("NOTION_ROOTS_IDS", ""))
    for p in blob.replace(",", " ").split()
    if p.strip()
]
NOTION_VERSION = "2022-06-28"
NOTION_TIMEOUT_S = float(os.environ.get("NOTION_TIMEOUT_S", "20"))
NOTION_MAX_CHARS = int(os.environ.get("NOTION_MAX_CHARS", "15000"))
_notion_scope_cache: dict[str, bool] = {}

SYSTEM_PROMPT = """Tu es smolbot, un assistant Telegram personnel.
Réponds en français sauf si l'utilisateur écrit dans une autre langue.
Sois utile, direct et concis.
Tu as accès à la recherche web : utilise web_search quand la question porte
sur des faits récents, l'actualité, ou des infos que tu ne connais pas.
Utilise web_read pour lire le contenu d'une page issue de la recherche
quand tu as besoin de citer ou résumer précisément.
Cite les sources (titre + URL) quand tu utilises le web.
Tu peux générer des images : utilise generate_image quand l'utilisateur
demande explicitement une image, un dessin, une illustration ou une photo.
Ne génère une image que sur demande explicite, pas pour illustrer tes réponses.
Tu as accès à Notion (uniquement les pages partagées sous les racines configurées) :
utilise notion_search pour retrouver une page, notion_read pour lire son contenu,
notion_append pour ajouter du texte à la fin d'une page, notion_create pour créer
une sous-page. N'écris dans Notion que sur demande explicite de l'utilisateur.
Refuse poliment toute lecture/écriture hors des pages autorisées."""

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
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Genere une image a partir d'une description et l'envoie a "
                "l'utilisateur. A utiliser UNIQUEMENT quand l'utilisateur demande "
                "explicitement une image, un dessin, une illustration ou une photo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description detaillee de l'image a generer (en anglais de preference)",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_search",
            "description": (
                "Cherche des pages Notion par mots-cles (uniquement dans les pages "
                "autorisées). Retourne id, titre et URL de chaque page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Mots-clés recherchés"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_read",
            "description": (
                "Lit le contenu d'une page Notion autorisée (titre + blocs, "
                "sous-pages incluses jusqu'à 3 niveaux)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "ID de la page (tiré de notion_search)"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_append",
            "description": (
                "Ajoute du texte à la fin d'une page Notion autorisée. "
                "UNIQUEMENT sur demande explicite d'écriture."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "ID de la page"},
                    "text": {"type": "string", "description": "Texte à ajouter (paragraphes séparés par des lignes vides)"},
                },
                "required": ["page_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_create",
            "description": (
                "Crée une sous-page Notion avec titre et contenu, sous une page "
                "autorisée (ou sous la première racine par défaut). "
                "UNIQUEMENT sur demande explicite de création."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Titre de la nouvelle page"},
                    "content": {"type": "string", "description": "Contenu initial (optionnel)"},
                    "parent_id": {"type": "string", "description": "ID de la page parente (optionnel)"},
                },
                "required": ["title"],
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
    if name == "notion_search":
        q = args.get("query", "")
        log.info("notion_search: %s", q[:120])
        return await notion_search_pages(q)
    if name == "notion_read":
        pid = args.get("page_id", "")
        log.info("notion_read: %s", pid)
        return await notion_read_page(pid)
    if name == "notion_append":
        pid = args.get("page_id", "")
        log.info("notion_append: %s", pid)
        return await notion_append_page(pid, args.get("text", ""))
    if name == "notion_create":
        log.info("notion_create: %s", args.get("title", "")[:120])
        return await notion_create_page(args.get("title", ""), args.get("content", ""), args.get("parent_id", ""))
    return f"Outil inconnu: {name}"


async def generate_image_bytes(prompt: str) -> tuple[str, bytes | str]:
    """Génère une image via FreeLLMAPI. Retourne ("bytes", data) ou ("error", msg)."""
    kwargs: dict = {"model": IMAGE_MODEL, "prompt": prompt}
    if IMAGE_SIZE:
        kwargs["size"] = IMAGE_SIZE
    resp = await llm.images.generate(**kwargs)
    item = resp.data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        return ("bytes", base64.b64decode(b64))
    url = getattr(item, "url", None)
    if url:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url)
            r.raise_for_status()
            return ("bytes", r.content)
    revised = getattr(item, "revised_prompt", "")
    return ("error", f"réponse image vide (revised_prompt={revised!r})")


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _norm_id(pid: str) -> str:
    return pid.strip().replace("-", "").lower()


def _rich_text(rich: list) -> str:
    return "".join(seg.get("plain_text", "") for seg in rich or [])


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rich_text(prop.get("title", [])) or "(sans titre)"
    return "(sans titre)"


async def _notion_get(client: httpx.AsyncClient, path: str, **kw):
    r = await client.get(f"https://api.notion.com/v1{path}", headers=_notion_headers(), **kw)
    r.raise_for_status()
    return r.json()


async def _notion_post(client: httpx.AsyncClient, path: str, payload: dict):
    r = await client.post(f"https://api.notion.com/v1{path}", headers=_notion_headers(), json=payload)
    r.raise_for_status()
    return r.json()


async def _notion_patch(client: httpx.AsyncClient, path: str, payload: dict):
    r = await client.patch(f"https://api.notion.com/v1{path}", headers=_notion_headers(), json=payload)
    r.raise_for_status()
    return r.json()


async def notion_in_scope(client: httpx.AsyncClient, page_id: str) -> bool:
    """Vrai si la page est une racine autorisée ou descend d'une racine (via parent chain)."""
    pid = _norm_id(page_id)
    if pid in _notion_scope_cache:
        return _notion_scope_cache[pid]
    seen: list[str] = []
    cur = pid
    ok = False
    try:
        for _ in range(10):
            if cur in NOTION_ROOTS:
                ok = True
                break
            page = await _notion_get(client, f"/pages/{cur}")
            parent = page.get("parent") or {}
            ptype = parent.get("type")
            if ptype == "page_id":
                seen.append(cur)
                cur = _norm_id(parent.get("page_id", ""))
            else:
                break  # workspace / database / autre : hors scope sauf racine exacte
    except Exception:
        ok = False
    _notion_scope_cache[pid] = ok
    for s in seen:
        _notion_scope_cache.setdefault(s, ok)
    return ok


def _block_text(block: dict) -> str:
    btype = block.get("type", "")
    if btype == "child_page":
        return f"📄 Sous-page : {block.get('child_page', {}).get('title', '')} (id={block.get('id')})"
    data = block.get(btype, {}) if isinstance(block.get(btype), dict) else {}
    text = _rich_text(data.get("rich_text", []))
    if btype.startswith("heading_"):
        return f"## {text}" if text else ""
    if btype == "bulleted_list_item":
        return f"• {text}" if text else ""
    if btype == "numbered_list_item":
        return f"– {text}" if text else ""
    if btype == "to_do":
        mark = "☑" if data.get("checked") else "☐"
        return f"{mark} {text}" if text else ""
    if btype == "quote":
        return f"> {text}" if text else ""
    if btype == "code":
        return f"```\n{text}\n```" if text else ""
    return text


async def _read_blocks(client: httpx.AsyncClient, block_id: str, depth: int, budget: list) -> list[str]:
    if depth > 3 or budget[0] <= 0:
        return []
    out: list[str] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = await _notion_get(client, f"/blocks/{block_id}/children", params=params)
        for b in data.get("results", []):
            t = _block_text(b)
            if t:
                take = min(len(t), budget[0])
                out.append(t[:take])
                budget[0] -= take
                if budget[0] <= 0:
                    return out
            if b.get("has_children") and b.get("type") not in ("child_page",):
                out.extend(await _read_blocks(client, b["id"], depth + 1, budget))
                if budget[0] <= 0:
                    return out
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def _split_paragraphs(text: str, limit: int = 2000) -> list[dict]:
    blocks = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        for i in range(0, len(para), limit):
            chunk = para[i:i + limit]
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})
    return blocks or [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "(vide)"}}]}}]


async def notion_search_pages(query: str) -> str:
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            data = await _notion_post(client, "/search", {
                "query": query, "filter": {"property": "object", "value": "page"}, "page_size": 10,
            })
            lines = []
            for res in data.get("results", []):
                pid = res.get("id", "")
                if not await notion_in_scope(client, pid):
                    continue
                title = _page_title(res)
                url = res.get("url", "")
                lines.append(f"• {title}\n  id={pid}\n  {url}")
            if not lines:
                return "Aucune page autorisée trouvée."
            return "\n".join(lines)
    except Exception as e:
        log.warning("Notion search failed: %s", e)
        return f"Erreur recherche Notion: {e}"


async def notion_read_page(page_id: str) -> str:
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            if not await notion_in_scope(client, page_id):
                return "Refusé : cette page est hors des pages autorisées."
            page = await _notion_get(client, f"/pages/{_norm_id(page_id)}")
            title = _page_title(page)
            budget = [NOTION_MAX_CHARS]
            parts = await _read_blocks(client, _norm_id(page_id), 0, budget)
            body = "\n".join(parts) or "(page vide)"
            return f"# {title}\n\n{body}"
    except Exception as e:
        log.warning("Notion read failed: %s", e)
        return f"Erreur lecture Notion: {e}"


async def notion_append_page(page_id: str, text: str) -> str:
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    if not text.strip():
        return "Echec : texte vide."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            if not await notion_in_scope(client, page_id):
                return "Refusé : cette page est hors des pages autorisées."
            await _notion_patch(client, f"/blocks/{_norm_id(page_id)}/children",
                                {"children": _split_paragraphs(text)})
            return "Texte ajouté à la page."
    except Exception as e:
        log.warning("Notion append failed: %s", e)
        return f"Erreur écriture Notion: {e}"


async def notion_create_page(title: str, content: str = "", parent_id: str = "") -> str:
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    if not title.strip():
        return "Echec : titre vide."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            parent = _norm_id(parent_id) if parent_id.strip() else (NOTION_ROOTS[0] if NOTION_ROOTS else "")
            if not parent:
                return "Erreur : aucune page racine configurée."
            if not await notion_in_scope(client, parent):
                return "Refusé : le parent est hors des pages autorisées."
            page = await _notion_post(client, "/pages", {
                "parent": {"page_id": parent},
                "properties": {"title": [{"text": {"content": title[:100]}}]},
            })
            new_id = page.get("id", "")
            if content.strip():
                await _notion_patch(client, f"/blocks/{_norm_id(new_id)}/children",
                                    {"children": _split_paragraphs(content)})
            url = page.get("url", "")
            _notion_scope_cache[_norm_id(new_id)] = True
            return f"Page créée : {title} (id={new_id}) {url}"
    except Exception as e:
        log.warning("Notion create failed: %s", e)
        return f"Erreur création Notion: {e}"


async def handle_image_tool(update: Update, prompt: str) -> str:
    """Génère l'image et l'envoie directement. Retourne le résumé pour le LLM."""
    if not prompt.strip():
        return "Echec: prompt vide, demande une description à l'utilisateur."
    try:
        log.info("generate_image: %s", prompt[:150])
        kind, payload = await generate_image_bytes(prompt)
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
    await update.message.reply_text("Salut ! Je suis smolbot. Envoie-moi un message et je te réponds avec le LLM. Commandes : /heure (l'heure), /image <description> (générer une image).")


async def heure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(now_text())


async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt.strip():
        await update.message.reply_text("Usage : /image <description de l'image>")
        return
    await update.message.reply_text("🎨 Génération en cours…")
    try:
        kind, payload = await generate_image_bytes(prompt)
    except Exception as e:
        log.warning("Image generation failed: %s", e)
        await update.message.reply_text(f"Désolé, la génération d'image a échoué : {e}")
        return
    if kind == "error":
        await update.message.reply_text(f"Désolé, la génération d'image a échoué : {payload}")
        return
    await update.message.reply_photo(photo=payload, caption=prompt[:900])


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
                if tc.function.name == "generate_image":
                    result = await handle_image_tool(update, args.get("prompt", ""))
                else:
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
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_llm))
    log.info("smolbot démarré (long polling), LLM=%s tavily=%s image=%s notion=%s",
             LLM_BASE_URL, "on" if TAVILY_API_KEY else "off", IMAGE_MODEL,
             f"on({len(NOTION_ROOTS)} racines)" if NOTION_TOKEN and NOTION_ROOTS else "off")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
