# smolbot

Bot Telegram minimal : répond avec l'heure (fuseau `Europe/Paris` par défaut).

## Variables d'environnement
- `TELEGRAM_BOT_TOKEN` (obligatoire) : token donné par @BotFather
- `BOT_TIMEZONE` (optionnel) : ex. `Europe/Paris`

## Lancer en local
```
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python bot.py
```

## Déploiement
Railway, déploiement depuis ce repo GitHub (commande de démarrage dans `railway.json`).
