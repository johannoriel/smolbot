"""Journal des messages Telegram (Postgres/Neon), réglages par chat et outils LLM.

Le bot ne peut pas relire l'historique d'un chat via l'API Telegram : il tient donc
son propre journal. Chaque message vu (et chaque réponse du bot) est inséré ici ;
les messages plus vieux que la durée réglée pour le chat sont ignorés puis purgés.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import asyncpg

log = logging.getLogger("smolbot")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DEFAULT_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "1"))
DEFAULT_RECENT_N = int(os.environ.get("HISTORY_RECENT_N", "10"))
MAX_RETENTION_DAYS = 365
MAX_RECENT_N = 50
SEARCH_MAX = 20
MSG_MAX_CHARS = int(os.environ.get("HISTORY_MSG_MAX_CHARS", "600"))  # troncature à l'affichage
DB_TIMEOUT_S = float(os.environ.get("DB_TIMEOUT_S", "15"))  # laisse le temps à Neon de se réveiller
PURGE_EVERY_S = 600
BOT_NAME = "smolbot"
TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Paris"))

HISTORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_recent_messages",
            "description": (
                "Récupère les n derniers messages de la conversation Telegram en cours "
                "(hors message actuel), du plus ancien au plus récent. A utiliser pour "
                "remonter plus loin que le contexte déjà fourni."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": f"Nombre de messages (1-{MAX_RECENT_N})",
                        "minimum": 1,
                        "maximum": MAX_RECENT_N,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": (
                "Cherche par mots-clés (tous les mots doivent apparaître) dans l'historique "
                "de la conversation en cours, sur la durée conservée. Retourne les messages "
                "correspondants avec date et auteur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Mots-clés recherchés"},
                    "limit": {
                        "type": "integer",
                        "description": f"Nombre max de résultats (1-{SEARCH_MAX}, défaut 10)",
                        "minimum": 1,
                        "maximum": SEARCH_MAX,
                    },
                },
                "required": ["query"],
            },
        },
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        BIGINT      NOT NULL,
    message_id     BIGINT      NOT NULL,
    user_id        BIGINT,
    author         TEXT        NOT NULL,
    from_assistant BOOLEAN     NOT NULL DEFAULT FALSE,
    content        TEXT        NOT NULL,
    reply_to_id    BIGINT,
    sent_at        TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_chat_msg ON messages (chat_id, message_id);
CREATE INDEX IF NOT EXISTS messages_chat_time ON messages (chat_id, sent_at DESC);
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id        BIGINT PRIMARY KEY,
    retention_days INT,
    recent_n       INT
);
"""

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()
_settings_cache: dict[int, tuple[int, int]] = {}
_last_purge: dict[int, float] = {}


def enabled() -> bool:
    return bool(DATABASE_URL)


# --------------------------------------------------------------------------- connexion

def _clean_dsn(url: str) -> str:
    """Neon ajoute parfois channel_binding=..., que asyncpg ne comprend pas."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            pool = await asyncpg.create_pool(
                _clean_dsn(DATABASE_URL),
                min_size=0,
                max_size=4,
                timeout=DB_TIMEOUT_S,
                command_timeout=DB_TIMEOUT_S,
                max_inactive_connection_lifetime=60,  # Neon coupe les connexions inactives
                statement_cache_size=0,  # compatible avec le pooler (pgbouncer) de Neon
            )
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA)
            _pool = pool
    return _pool


async def init() -> None:
    """Ouvre le pool et crée les tables si besoin."""
    await _get_pool()


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _run(fn):
    """Exécute fn(conn) ; un seul nouvel essai si la connexion est morte (Neon endormi)."""
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            pool = await _get_pool()
            async with pool.acquire() as conn:
                return await fn(conn)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError) as e:
            last = e
            log.warning("Base de données injoignable (essai %d/2): %s", attempt, e)
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------------- réglages

async def get_settings(chat_id: int) -> tuple[int, int]:
    """(jours d'historique, n derniers messages injectés) pour ce chat."""
    defaults = (DEFAULT_RETENTION_DAYS, DEFAULT_RECENT_N)
    if not enabled():
        return defaults
    if chat_id in _settings_cache:
        return _settings_cache[chat_id]
    try:
        async def op(conn):
            return await conn.fetchrow(
                "SELECT retention_days, recent_n FROM chat_settings WHERE chat_id = $1", chat_id)
        row = await _run(op)
    except Exception as e:
        log.warning("Lecture réglages échouée: %s", e)
        return defaults
    result = (
        (row["retention_days"] if row and row["retention_days"] else DEFAULT_RETENTION_DAYS),
        (row["recent_n"] if row and row["recent_n"] else DEFAULT_RECENT_N),
    )
    _settings_cache[chat_id] = result
    return result


async def _set_setting(chat_id: int, column: str, value: int) -> bool:
    if not enabled() or column not in ("retention_days", "recent_n"):
        return False
    try:
        async def op(conn):
            await conn.execute(
                f"INSERT INTO chat_settings (chat_id, {column}) VALUES ($1, $2) "
                f"ON CONFLICT (chat_id) DO UPDATE SET {column} = EXCLUDED.{column}",
                chat_id, value)
        await _run(op)
    except Exception as e:
        log.warning("Ecriture réglage échouée: %s", e)
        return False
    _settings_cache.pop(chat_id, None)
    return True


async def set_retention_days(chat_id: int, days: int) -> bool:
    ok = await _set_setting(chat_id, "retention_days", days)
    if ok:
        await purge(chat_id)  # si on réduit la durée, on nettoie tout de suite
    return ok


async def set_recent_n(chat_id: int, n: int) -> bool:
    return await _set_setting(chat_id, "recent_n", n)


# --------------------------------------------------------------------------- journal

async def purge(chat_id: int) -> None:
    days, _ = await get_settings(chat_id)
    try:
        async def op(conn):
            await conn.execute(
                "DELETE FROM messages WHERE chat_id = $1 "
                "AND sent_at < now() - make_interval(days => $2)", chat_id, days)
        await _run(op)
        _last_purge[chat_id] = time.monotonic()
    except Exception as e:
        log.warning("Purge échouée: %s", e)


async def add_message(
    chat_id: int,
    message_id: int,
    author: str,
    text: str,
    sent_at: datetime,
    user_id: int | None = None,
    reply_to_id: int | None = None,
    from_assistant: bool = False,
) -> None:
    """Journalise un message (ne lève jamais : le bot doit continuer sans base)."""
    if not enabled() or not text or not text.strip():
        return
    try:
        async def op(conn):
            await conn.execute(
                "INSERT INTO messages (chat_id, message_id, user_id, author, from_assistant, "
                "content, reply_to_id, sent_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (chat_id, message_id) DO NOTHING",
                chat_id, message_id, user_id, author, from_assistant, text, reply_to_id, sent_at)
        await _run(op)
        last = _last_purge.get(chat_id)
        if last is None or time.monotonic() - last > PURGE_EVERY_S:
            await purge(chat_id)
    except Exception as e:
        log.warning("Journalisation échouée: %s", e)


async def recent_messages(chat_id: int, n: int, exclude_message_id: int | None = None) -> list:
    """Les n derniers messages (dans la durée conservée), du plus ancien au plus récent."""
    days, _ = await get_settings(chat_id)
    n = max(1, min(MAX_RECENT_N, n))

    async def op(conn):
        return await conn.fetch(
            "SELECT sent_at, author, content FROM messages "
            "WHERE chat_id = $1 AND sent_at > now() - make_interval(days => $2) "
            "AND ($3::bigint IS NULL OR message_id <> $3) "
            "ORDER BY sent_at DESC, message_id DESC LIMIT $4",
            chat_id, days, exclude_message_id, n)

    rows = await _run(op)
    return list(reversed(rows))


async def find_messages(
    chat_id: int, query: str, limit: int = 10, exclude_message_id: int | None = None
) -> list:
    """Messages contenant tous les mots de la requête (insensible à la casse), du plus ancien au plus récent."""
    words = query.split()[:6]
    if not words:
        return []
    days, _ = await get_settings(chat_id)
    limit = max(1, min(SEARCH_MAX, limit))
    patterns = ["%" + re.sub(r"([\\%_])", r"\\\1", w) + "%" for w in words]
    conds = "".join(f" AND content ILIKE ${i}" for i in range(5, 5 + len(patterns)))
    sql = (
        "SELECT sent_at, author, content FROM messages "
        "WHERE chat_id = $1 AND sent_at > now() - make_interval(days => $2) "
        "AND ($3::bigint IS NULL OR message_id <> $3)" + conds +
        " ORDER BY sent_at DESC, message_id DESC LIMIT $4"
    )

    async def op(conn):
        return await conn.fetch(sql, chat_id, days, exclude_message_id, limit, *patterns)

    rows = await _run(op)
    return list(reversed(rows))


def format_rows(rows: list) -> str:
    lines = []
    for r in rows:
        ts = r["sent_at"].astimezone(TZ).strftime("%d/%m %H:%M")
        text = " ".join(r["content"].split())  # une ligne par message
        if len(text) > MSG_MAX_CHARS:
            text = text[:MSG_MAX_CHARS] + "…"
        lines.append(f"[{ts}] {r['author']} : {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- côté bot / LLM

async def context_block(chat_id: int, n: int, exclude_message_id: int | None = None) -> str:
    """Texte des n derniers messages, prêt à injecter dans le prompt ('' si rien ou erreur)."""
    if not enabled():
        return ""
    try:
        return format_rows(await recent_messages(chat_id, n, exclude_message_id))
    except Exception as e:
        log.warning("Lecture du contexte échouée: %s", e)
        return ""


async def tool_get_recent(chat_id: int, current_msg_id: int | None, n) -> str:
    if not enabled():
        return "Erreur: journal des messages non configuré (DATABASE_URL manquante)."
    try:
        n = int(n) if n else DEFAULT_RECENT_N
        rows = await recent_messages(chat_id, n, current_msg_id)
    except Exception as e:
        log.warning("get_recent_messages failed: %s", e)
        return f"Erreur lecture historique: {e}"
    return format_rows(rows) or "Aucun message dans l'historique conservé."


async def tool_search(chat_id: int, current_msg_id: int | None, query: str, limit) -> str:
    if not enabled():
        return "Erreur: journal des messages non configuré (DATABASE_URL manquante)."
    if not (query or "").strip():
        return "Echec : requête vide."
    try:
        rows = await find_messages(chat_id, query, int(limit) if limit else 10, current_msg_id)
    except Exception as e:
        log.warning("search_messages failed: %s", e)
        return f"Erreur recherche historique: {e}"
    return format_rows(rows) or "Aucun message correspondant dans l'historique conservé."
