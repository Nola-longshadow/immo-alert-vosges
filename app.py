import imaplib
import email
import re
import os
import json
import time
import hashlib
import requests
import threading
import logging
from datetime import datetime
from email.header import decode_header
from flask import Flask, render_template, jsonify
from bs4 import BeautifulSoup

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Configuration (variables d'environnement) ─────────────────────────────────
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")       # Mot de passe d'application Google
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", "120"))  # secondes (2 min par défaut)

# ─── Critères de filtrage ──────────────────────────────────────────────────────
CRITERES = {
    "loyer_max": 750,
    "pieces_min": 3,
    "chambres_min": 2,
    "dpe_max": "D",          # A B C D sont acceptés, E F G refusés
    "zones_ok": [
        "saint-étienne-lès-remiremont", "saint etienne les remiremont",
        "remiremont", "vagney", "saulxures", "rupt-sur-moselle",
        "cleurie", "saint-amé", "dommartin", "raon-aux-bois",
        "vecoux", "jarménil", "eloyes", "archettes", "épinal",
        "golbey", "pouxeux", "ferdrupt", "thiéfosse",
        "vosges", "88"
    ],
    "chauffage_ok": ["électrique", "electrique", "pellet", "granulé", "poêle", "pompe à chaleur", "pac"],
    "bonus": ["terrain", "cour", "jardin", "parking", "garage", "cave"],
    "malus": ["combles", "mansardé", "mansarde"],
}

DPE_ORDRE = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7}

# ─── Stockage des annonces déjà vues ───────────────────────────────────────────
seen_file = "seen_ids.json"

def load_seen():
    if os.path.exists(seen_file):
        with open(seen_file) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(seen_file, "w") as f:
        json.dump(list(seen), f)

seen_ids = load_seen()

# ─── Historique des alertes envoyées ───────────────────────────────────────────
alertes_history = []

# ─── Envoi Telegram ────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("✅ Telegram envoyé")
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")

# ─── Extraction du texte d'un email ────────────────────────────────────────────
def get_email_text(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except:
                    pass
            elif ctype == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(html, "html.parser")
                    body += soup.get_text(separator=" ")
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except:
            pass
    return body.lower()

def get_email_links(msg):
    links = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(html, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if any(site in href for site in ["leboncoin", "seloger", "pap.fr", "bienici", "logic-immo", "maison-foret"]):
                            links.append(href)
                except:
                    pass
    return list(set(links))

# ─── Extraction des données de l'annonce ───────────────────────────────────────
def extraire_loyer(texte):
    patterns = [
        r"(\d[\d\s]*)\s*€\s*(?:/\s*mois|par mois|mensuel)",
        r"loyer[^\d]*(\d[\d\s]*)\s*€",
        r"(\d[\d\s]*)\s*euros?\s*(?:/\s*mois|par mois)",
        r"(\d{3,4})\s*€",
    ]
    for p in patterns:
        m = re.search(p, texte)
        if m:
            val = int(re.sub(r"\s", "", m.group(1)))
            if 200 < val < 5000:
                return val
    return None

def extraire_pieces(texte):
    patterns = [
        r"(\d)\s*pièces?",
        r"(\d)\s*p\b",
        r"t(\d)\b",
        r"f(\d)\b",
        r"(\d)\s*rooms?",
    ]
    for p in patterns:
        m = re.search(p, texte)
        if m:
            return int(m.group(1))
    return None

def extraire_chambres(texte):
    m = re.search(r"(\d)\s*chambre", texte)
    return int(m.group(1)) if m else None

def extraire_dpe(texte):
    m = re.search(r"dpe\s*[:\-]?\s*([a-g])", texte)
    if m:
        return m.group(1).upper()
    m = re.search(r"classe\s+énergie\s*[:\-]?\s*([a-g])", texte)
    if m:
        return m.group(1).upper()
    return None

def extraire_surface(texte):
    m = re.search(r"(\d+)\s*m²", texte)
    return int(m.group(1)) if m else None

# ─── Scoring de l'annonce ──────────────────────────────────────────────────────
def scorer_annonce(texte, loyer, pieces, chambres, dpe, surface):
    score = 0
    raisons_ok = []
    raisons_ko = []

    # Loyer
    if loyer is not None:
        if loyer <= CRITERES["loyer_max"]:
            score += 30
            raisons_ok.append(f"💶 Loyer {loyer}€ ≤ {CRITERES['loyer_max']}€")
        else:
            score -= 50
            raisons_ko.append(f"💸 Loyer {loyer}€ trop élevé (max {CRITERES['loyer_max']}€)")

    # Pièces
    if pieces is not None:
        if pieces >= CRITERES["pieces_min"]:
            score += 20
            raisons_ok.append(f"🏠 {pieces} pièces ✓")
        else:
            score -= 30
            raisons_ko.append(f"📦 Seulement {pieces} pièce(s) (min {CRITERES['pieces_min']})")

    # Chambres
    if chambres is not None:
        if chambres >= CRITERES["chambres_min"]:
            score += 15
            raisons_ok.append(f"🛏️ {chambres} chambres ✓")
        else:
            score -= 20
            raisons_ko.append(f"🛏️ Seulement {chambres} chambre(s) (min {CRITERES['chambres_min']})")

    # DPE
    if dpe is not None:
        if DPE_ORDRE.get(dpe, 9) <= DPE_ORDRE.get(CRITERES["dpe_max"], 4):
            score += 15
            raisons_ok.append(f"🌿 DPE {dpe} ✓")
        else:
            score -= 25
            raisons_ko.append(f"🔥 DPE {dpe} trop mauvais (max {CRITERES['dpe_max']})")

    # Zone
    zone_trouvee = any(z in texte for z in CRITERES["zones_ok"])
    if zone_trouvee:
        score += 10
        raisons_ok.append("📍 Zone correcte ✓")

    # Chauffage
    chauff_ok = any(c in texte for c in CRITERES["chauffage_ok"])
    if chauff_ok:
        score += 10
        raisons_ok.append("🔥 Chauffage adapté ✓")

    # Bonus
    for b in CRITERES["bonus"]:
        if b in texte:
            score += 5
            raisons_ok.append(f"⭐ Bonus : {b}")

    # Malus
    for m in CRITERES["malus"]:
        if m in texte:
            score -= 15
            raisons_ko.append(f"⚠️ Attention : {m}")

    return score, raisons_ok, raisons_ko

# ─── Analyse d'un email ────────────────────────────────────────────────────────
def analyser_email(msg):
    texte = get_email_text(msg)
    links = get_email_links(msg)

    loyer    = extraire_loyer(texte)
    pieces   = extraire_pieces(texte)
    chambres = extraire_chambres(texte)
    dpe      = extraire_dpe(texte)
    surface  = extraire_surface(texte)

    score, raisons_ok, raisons_ko = scorer_annonce(texte, loyer, pieces, chambres, dpe, surface)

    # Détermine la source
    source = "Inconnu"
    for site in ["leboncoin", "seloger", "pap", "bienici", "logic-immo"]:
        if site in texte:
            source = site.capitalize()
            break

    return {
        "score": score,
        "loyer": loyer,
        "pieces": pieces,
        "chambres": chambres,
        "dpe": dpe,
        "surface": surface,
        "source": source,
        "links": links,
        "raisons_ok": raisons_ok,
        "raisons_ko": raisons_ko,
        "texte_extrait": texte[:300]
    }

# ─── Scan de la boîte Gmail ────────────────────────────────────────────────────
def scan_gmail():
    global seen_ids, alertes_history

    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.warning("Gmail non configuré")
        return

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        mail.select("inbox")

        # Cherche les emails non lus des sites immo
        senders = [
            "leboncoin", "seloger", "pap.fr", "bienici",
            "logic-immo", "laforet", "century21", "orpi"
        ]

        for sender in senders:
            _, data = mail.search(None, f'(UNSEEN FROM "{sender}")')
            ids = data[0].split()

            for eid in ids:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                # ID unique basé sur le contenu
                uid = hashlib.md5(raw[:500]).hexdigest()
                if uid in seen_ids:
                    continue

                # Analyse
                result = analyser_email(msg)
                result["uid"] = uid
                result["date"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                result["sender"] = sender

                # Ajoute à l'historique
                alertes_history.insert(0, result)
                if len(alertes_history) > 50:
                    alertes_history = alertes_history[:50]

                seen_ids.add(uid)
                save_seen(seen_ids)

                # Décide d'envoyer ou non
                if result["score"] >= 0:
                    envoyer_alerte(result)
                else:
                    log.info(f"Annonce ignorée (score {result['score']}): {sender}")

        mail.close()
        mail.logout()

    except Exception as e:
        log.error(f"Erreur Gmail : {e}")

# ─── Formatage et envoi de l'alerte ───────────────────────────────────────────
def envoyer_alerte(r):
    emoji_score = "🔥🔥🔥" if r["score"] >= 80 else "🔥🔥" if r["score"] >= 50 else "🔥"

    lignes = [
        f"{emoji_score} <b>NOUVELLE ANNONCE — {r['source'].upper()}</b>",
        f"⏰ {r['date']}",
        "",
        f"📊 Score : <b>{r['score']}/100</b>",
    ]

    if r["loyer"]:    lignes.append(f"💶 Loyer : <b>{r['loyer']} €/mois</b>")
    if r["pieces"]:   lignes.append(f"🏠 Pièces : {r['pieces']}")
    if r["chambres"]: lignes.append(f"🛏️ Chambres : {r['chambres']}")
    if r["surface"]:  lignes.append(f"📐 Surface : {r['surface']} m²")
    if r["dpe"]:      lignes.append(f"🌿 DPE : {r['dpe']}")

    if r["raisons_ok"]:
        lignes.append("\n✅ Points positifs :")
        lignes += [f"  {x}" for x in r["raisons_ok"]]

    if r["raisons_ko"]:
        lignes.append("\n❌ Points négatifs :")
        lignes += [f"  {x}" for x in r["raisons_ko"]]

    if r["links"]:
        lignes.append(f"\n🔗 <a href='{r['links'][0]}'>Voir l'annonce</a>")

    send_telegram("\n".join(lignes))

# ─── Boucle de scan en arrière-plan ────────────────────────────────────────────
def scan_loop():
    while True:
        log.info("🔍 Scan en cours...")
        scan_gmail()
        log.info(f"💤 Prochain scan dans {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)

# ─── Routes Flask ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", alertes=alertes_history, criteres=CRITERES)

@app.route("/api/alertes")
def api_alertes():
    return jsonify(alertes_history)

@app.route("/api/scan")
def api_scan():
    scan_gmail()
    return jsonify({"status": "ok", "message": "Scan effectué"})

@app.route("/api/test-telegram")
def api_test_telegram():
    send_telegram("✅ Test — Ton système d'alerte immobilière est opérationnel !")
    return jsonify({"status": "ok"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "seen": len(seen_ids), "alertes": len(alertes_history)})

# ─── Démarrage ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Lance le scan en arrière-plan
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
