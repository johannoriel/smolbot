# smolbot

Bot Telegram minimal : répond avec l'heure (fuseau `Europe/Paris` par défaut).

## Variables d'environnement
- `TELEGRAM_BOT_TOKEN` (obligatoire) : token donné par @BotFather
- `BOT_TIMEZONE` (optionnel) : ex. `Europe/Paris`
- `FREELLMAPI_API_KEY` (obligatoire) : clé du proxy FreeLLMAPI
- `FREELLMAPI_BASE_URL` (optionnel) : ex. `http://freellmapi.railway.internal:3001/v1`
- `FREELLMAPI_MODEL` (optionnel) : ex. `auto`
- `TAVILY_API_KEY` (optionnel mais requis pour la recherche web) : clé gratuite sur `tavily.com` (1000 crédits/mois)
- `TAVILY_MAX_RESULTS` (optionnel, défaut `5`), `TAVILY_TIMEOUT_S` (optionnel, défaut `20`)

## Recherche web
Le bot expose 2 outils au LLM via tool-calling OpenAI : `web_search` (recherche Tavily + réponse générée + extraits) et `web_read` (lecture du contenu complet de 1-3 pages pour résumer/citer). Pas de RAG vectoriel : c'est un RAG simple en mémoire (retrieve puis generate), suffisant pour un usage perso.

## Lancer en local
```
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python bot.py
```

## Déploiement
Railway, déploiement depuis ce repo GitHub (commande de démarrage dans `railway.json`).
