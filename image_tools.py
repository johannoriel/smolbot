"""Génération d'images via FreeLLMAPI (/v1/images/generations)."""

import base64
import logging
import os

import httpx
from openai import AsyncOpenAI

log = logging.getLogger("smolbot")

LLM_BASE_URL = os.environ.get("FREELLMAPI_BASE_URL", "http://freellmapi.railway.internal:3001/v1")
LLM_API_KEY = os.environ.get("FREELLMAPI_API_KEY", "")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b")
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "")  # ex. "1024x1024", vide = défaut du provider

_llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY or "missing")

IMAGE_TOOL = {
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
}


async def generate_image_bytes(prompt: str) -> tuple[str, bytes | str]:
    """Génère une image. Retourne ("bytes", data) ou ("error", msg)."""
    kwargs: dict = {"model": IMAGE_MODEL, "prompt": prompt}
    if IMAGE_SIZE:
        kwargs["size"] = IMAGE_SIZE
    resp = await _llm.images.generate(**kwargs)
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
