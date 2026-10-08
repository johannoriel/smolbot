"""Outils Notion (lecture + écriture, scope restreint aux racines configurées)."""

import logging
import os

import httpx

log = logging.getLogger("smolbot")

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
NOTION_LIST_MAX = int(os.environ.get("NOTION_LIST_MAX", "100"))
_notion_scope_cache: dict[str, bool] = {}

NOTION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "notion_list",
            "description": (
                "Liste toutes les pages Notion autorisées (arborescence des racines "
                "configurées, avec id et titre). A utiliser quand l'utilisateur parle "
                "d'une page sans donner son ID."
            ),
            "parameters": {"type": "object", "properties": {}},
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
                    "page_id": {"type": "string", "description": "ID de la page (tiré de notion_list ou notion_search)"},
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
            "name": "notion_replace",
            "description": (
                "Remplace le contenu textuel d'une page Notion autorisée par un nouveau "
                "texte (les sous-pages sont conservées). UNIQUEMENT sur demande "
                "explicite de modification/remplacement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "ID de la page"},
                    "text": {"type": "string", "description": "Nouveau contenu complet"},
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


async def _notion_delete(client: httpx.AsyncClient, path: str):
    r = await client.delete(f"https://api.notion.com/v1{path}", headers=_notion_headers())
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


async def _walk_pages(client: httpx.AsyncClient, block_id: str, depth: int, lines: list[str]) -> None:
    """Ajoute les sous-pages (titre + id) en arborescence. Stop à NOTION_LIST_MAX."""
    if depth > 5 or len(lines) >= NOTION_LIST_MAX:
        return
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = await _notion_get(client, f"/blocks/{block_id}/children", params=params)
        for b in data.get("results", []):
            if len(lines) >= NOTION_LIST_MAX:
                return
            if b.get("type") == "child_page":
                title = b.get("child_page", {}).get("title", "(sans titre)")
                lines.append(f"{'  ' * depth}📄 {title} (id={b.get('id')})")
                await _walk_pages(client, b["id"], depth + 1, lines)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def _split_paragraphs(text: str, limit: int = 2000) -> list[dict]:
    blocks = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        for i in range(0, len(para), limit):
            chunk = para[i:i + limit]
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})
    return blocks or [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "(vide)"}}]}}]


async def notion_list_pages() -> str:
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    if not NOTION_ROOTS:
        return "Erreur : aucune page racine configurée."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            lines: list[str] = []
            for root in NOTION_ROOTS:
                try:
                    page = await _notion_get(client, f"/pages/{root}")
                    title = _page_title(page)
                except Exception:
                    title = "(racine illisible)"
                lines.append(f"📁 {title} (id={root})")
                await _walk_pages(client, root, 1, lines)
            return "\n".join(lines) or "Aucune page."
    except Exception as e:
        log.warning("Notion list failed: %s", e)
        return f"Erreur liste Notion: {e}"


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


async def notion_replace_page(page_id: str, text: str) -> str:
    """Remplace les blocs de texte de la page (sous-pages conservées)."""
    if not NOTION_TOKEN:
        return "Erreur: Notion non configuré (NOTION_TOKEN manquant)."
    if not text.strip():
        return "Echec : texte vide."
    try:
        async with httpx.AsyncClient(timeout=NOTION_TIMEOUT_S) as client:
            if not await notion_in_scope(client, page_id):
                return "Refusé : cette page est hors des pages autorisées."
            pid = _norm_id(page_id)
            cursor = None
            removed = 0
            while True:
                params = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                data = await _notion_get(client, f"/blocks/{pid}/children", params=params)
                for b in data.get("results", []):
                    if b.get("type") == "child_page":
                        continue  # sous-pages conservées
                    await _notion_delete(client, f"/blocks/{b['id']}")
                    removed += 1
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            await _notion_patch(client, f"/blocks/{pid}/children",
                                {"children": _split_paragraphs(text)})
            return f"Page mise à jour ({removed} bloc(s) remplacé(s), sous-pages conservées)."
    except Exception as e:
        log.warning("Notion replace failed: %s", e)
        return f"Erreur modification Notion: {e}"


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
