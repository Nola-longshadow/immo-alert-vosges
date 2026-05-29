# 🏠 Alertes Immo Vosges

Système d'alerte automatique pour trouver une location dans les Vosges.

## Fonctionnement
- Scanne Gmail toutes les 2 minutes pour les emails d'alertes immobilières
- Filtre selon tes critères (loyer, pièces, DPE, zone, chauffage)
- Score chaque annonce
- Envoie une alerte Telegram instantanée pour les bonnes annonces
- Dashboard web pour voir toutes les annonces analysées

## Variables d'environnement requises

| Variable | Description |
|---|---|
| `GMAIL_USER` | Ton adresse Gmail (ex: toi@gmail.com) |
| `GMAIL_PASSWORD` | Mot de passe d'application Google (pas ton vrai mdp) |
| `TELEGRAM_TOKEN` | Token du bot Telegram (donné par @BotFather) |
| `TELEGRAM_CHAT_ID` | Ton Chat ID Telegram |
| `SCAN_INTERVAL` | Intervalle en secondes (120 = 2 min) |

## Critères de filtrage
- Zone : 15 min autour de Saint-Étienne-lès-Remiremont
- Loyer ≤ 750 €
- 3 pièces minimum
- 2 chambres minimum  
- DPE ≤ D
- Chauffage électrique ou pellets
- Bonus : terrain, cour, parking, garage
- Malus : combles, mansardé
