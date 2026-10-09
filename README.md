# smolbot

Bot Telegram personnel : LLM gratuit (FreeLLMAPI) + recherche web (Tavily) + images (Flux) + Notion, avec un journal des conversations en base Postgres (Neon).

## Comportement
- En chat privé, le bot répond à tout message.
- En groupe, il ne répond que si on le **mentionne** (`@joriel_lie_bot`) ou si on **répond à un de ses messages**.
- Il journalise **tous** les messages texte qu'il voit (et ses propres réponses), puis injecte les `n` derniers dans le prompt à chaque réponse. Le LLM peut aller plus loin avec `get_recent_messages` et `search_messages`.
- L'API Telegram ne permet pas de relire l'historique : le bot ne connaît que ce qui s'est dit après son arrivée dans le chat.

**À faire dans BotFather pour les groupes** : `/setprivacy` → *Disable*, sinon le bot ne voit que les mentions et les réponses (rien à journaliser). Après changement, retirer puis remettre le bot dans le groupe.

## Commandes
- `/heure` : l'heure exacte
- `/image <description>` : générer une image
- `/historique [jours]` : durée de conservation du journal pour ce chat (sans argument : valeur actuelle)
- `/contexte [n]` : nombre de messages injectés automatiquement dans le prompt (sans argument : valeur actuelle)

Les réglages sont par chat, stockés en base. Les modifier est réservé aux ids listés dans `ADMIN_USER_IDS` (sans cette variable : uniquement en chat privé).

## Structure
- `bot.py` : orchestrateur (commandes, journalisation, déclenchement sur mention, boucle tool-calling)
- `history_tools.py` : journal Postgres, réglages par chat, outils `get_recent_messages` et `search_messages`
- `search_tools.py` : `web_search`, `web_read` (Tavily)
- `image_tools.py` : `generate_image` (FreeLLMAPI `/v1/images/generations`)
- `notion_tools.py` : `notion_list`, `notion_search`, `notion_read`, `notion_append`, `notion_replace`, `notion_create` (scope restreint aux racines)

## Variables d'environnement
- `TELEGRAM_BOT_TOKEN` (obligatoire) : token donné par @BotFather
- `BOT_TIMEZONE` (optionnel) : ex. `Europe/Paris`
- `FREELLMAPI_API_KEY` (obligatoire) : clé du proxy FreeLLMAPI
- `FREELLMAPI_BASE_URL` (optionnel) : ex. `http://freellmapi.railway.internal:3001/v1`
- `FREELLMAPI_MODEL` (optionnel) : ex. `auto`
- `DATABASE_URL` (requis pour le journal) : chaîne de connexion Postgres (Neon). Sans elle, le bot fonctionne mais sans mémoire de conversation.
- `ADMIN_USER_IDS` (recommandé) : ids Telegram autorisés à régler `/historique` et `/contexte` (ex. `123456789` ; id obtenu via @userinfobot)
- `HISTORY_RETENTION_DAYS` (optionnel, défaut `1`) : durée de conservation par défaut
- `HISTORY_RECENT_N` (optionnel, défaut `10`) : nombre de messages injectés par défaut
- `HISTORY_MSG_MAX_CHARS` (optionnel, défaut `600`) : troncature de chaque message dans le contexte
- `DB_TIMEOUT_S` (optionnel, défaut `15`) : délai de connexion/requête (Neon peut mettre quelques secondes à se réveiller)
- `TAVILY_API_KEY` (optionnel mais requis pour la recherche web) : clé gratuite sur `tavily.com` (1000 crédits/mois)
- `TAVILY_MAX_RESULTS` (optionnel, défaut `5`), `TAVILY_TIMEOUT_S` (optionnel, défaut `20`)
- `NOTION_TOKEN`, `NOTION_ROOT_IDS` : voir la section Notion

## Journal des messages
Deux tables créées automatiquement au démarrage : `messages` (chat, auteur, texte, date, message cité) et `chat_settings` (durée de conservation et `n` par chat). Les messages plus vieux que la durée réglée sont ignorés par les requêtes et purgés régulièrement. Le chat consulté par les outils est imposé par le code : le LLM ne peut lire que la conversation en cours.

## Recherche web
Le bot expose 2 outils au LLM via tool-calling OpenAI : `web_search` (recherche Tavily + réponse générée + extraits) et `web_read` (lecture du contenu complet de 1-3 pages pour résumer/citer). Pas de RAG vectoriel : c'est un RAG simple en mémoire (retrieve puis generate), suffisant pour un usage perso.

## Génération d'images
Via FreeLLMAPI `/v1/images/generations` (ex. worker Cloudflare Flux). Le LLM appelle l'outil `generate_image` sur demande explicite, ou commande directe `/image <description>`. Variables :
- `IMAGE_MODEL` (défaut `@cf/black-forest-labs/flux-2-klein-4b`)
- `IMAGE_SIZE` (optionnel, ex. `1024x1024` ; vide = défaut du provider)

## Notion (lecture + écriture, scope restreint)
Intégration interne Notion (token `ntn_...` partagé sur la page racine). 6 outils : `notion_list`, `notion_search`, `notion_read` (page + sous-blocs jusqu'à 3 niveaux), `notion_append` (ajoute du texte en fin de page), `notion_replace` (remplace le contenu, sous-pages conservées), `notion_create` (crée une sous-page). L'écriture n'a lieu que sur demande explicite. Le code refuse toute page hors des racines (`NOTION_ROOT_IDS` ou `NOTION_ROOTS_IDS`, IDs séparés par virgules/espaces, tirets optionnels). Pas de RAG vectoriel : lecture live à chaque question.

## Lancer en local
```
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... FREELLMAPI_API_KEY=... DATABASE_URL=... python bot.py
```

## Déploiement
Railway, déploiement depuis ce repo GitHub (commande de démarrage dans `railway.json`). Ajouter `DATABASE_URL` (et `ADMIN_USER_IDS`) dans les variables du service.
