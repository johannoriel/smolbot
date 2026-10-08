"""Outils de recherche web via Tavily (search + extract)."""

import logging
import os

import httpx

log = logging.getLogger("smolbot")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
TAVILY_TIMEOUT_S = float(os.environ.get("TAVILY_TIMEOUT_S", "20"))

SEARCH_TOOLS = [
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
