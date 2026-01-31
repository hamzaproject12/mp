import time
import json
import requests
import hashlib
import os
import math 
import re   
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
DATA_PATH = "data"
SEEN_FILE = os.path.join(DATA_PATH, "seen_offers_ao.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

URL_AO = "https://www.marchespublics.gov.ma/index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons"

# --- 👤 UTILISATEUR UNIQUE (TOI) ---
SUBSCRIBERS = [
    {
        "name": "Administrateur",
        "id": "1952904877", # Ton ID
        "subscriptions": ["ALL"] # Tu reçois tout ce qui passe les filtres
    }
]

# --- 🎯 WHITELIST ACHETEURS (PRIORITÉ) ---
TARGET_BUYERS = [
    "DIRECTION REGIONALE D'AGRICULTURE",
    "DIRECTEUR REGIONAL D'AGRICULTURE",
    "DIRECTION PROVINCIAL DE L'AGRICULTURE",
    "DIRECTEUR PROVINCIAL DE L'AGRICULTURE",
    "CHAMBRE D'AGRICULTURE",
    "MISE EN VALEUR AGRICOLE",
    "CONSEIL AGRICOLE",
    "ONSSA",
    "OFFICE NATIONAL DE SECURITE SANITAIRE"
]

# --- MOTS-CLÉS ---
KEYWORDS = {
    "Event & Formation": [
        "formation", "session", "atelier", "renforcement de capacité", 
        "organisation", "animation", "événement", "sensibilisation",    
        "réception", "pause-café", "restauration", "traiteur",          
        "impression", "conception", "banderole", "flyer", "support",    
        "enquête", "étude", "conseil", "agri", "réunion"
    ]
}

# --- EXCLUSIONS ---
EXCLUSIONS = [
    "nettoyage", "gardiennage", "construction", "bâtiment", "plomberie",
    "sanitaire", "peinture", "électricité", "jardinage", "espaces verts", 
    "piscine", "vêtement", "habillement", "carburant", "véhicule", 
    "transport", "billet", "aérien", "travaux", "voirie", "topographique",
    "la peche", "secteur de la pêche", "maritime" 
]

def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}")

def send_telegram_to_user(chat_id, message):
    if not TELEGRAM_TOKEN or not chat_id: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True})
    except Exception as e:
        log(f"❌ Erreur envoi Telegram: {e}")

def load_seen():
    if not os.path.exists(DATA_PATH):
        os.makedirs(DATA_PATH, exist_ok=True)
    try:
        with open(SEEN_FILE, "r") as f: return set(json.load(f))
    except: return set()

def save_seen(seen_set):
    with open(SEEN_FILE, "w") as f: json.dump(list(seen_set), f)

def scorer(text, buyer_name):
    text_lower = text.lower()
    buyer_lower = buyer_name.lower()
    
    # 1. Vérification EXCLUSIONS
    for exc in EXCLUSIONS:
        if exc in text_lower: return 0, f"Exclu ({exc})"
        if exc in buyer_lower: return 0, f"Exclu Acheteur ({exc})"

    # 2. CIBLAGE ACHETEUR (Priorité MAX)
    for target in TARGET_BUYERS:
        if target.lower() in buyer_lower:
            return 100, "Agri"

    # 3. Mots-clés
    for cat, mots in KEYWORDS.items():
        if any(mot in text_lower for mot in mots):
            if "impression" in text_lower and not any(t in text_lower for t in ["formation", "atelier", "sensibilisation", "événement"]):
                 return 0, "Exclu (Impression seule)"
            return sum(1 for m in mots if m in text_lower), cat
            
    return 0, "Pas de mots-clés"

def scan_ao_attempt():
    seen_ids = load_seen()
    new_ids = set()
    pending_alerts = [] 

    # Dates : 30 derniers jours à Aujourd'hui
    today = datetime.now()
    past = today - timedelta(days=30)
    date_start = past.strftime("%d/%m/%Y")
    date_end = today.strftime("%d/%m/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        log(f"🌍 Connexion AO : {date_start} -> {date_end}")
        
        try:
            page.goto(URL_AO, timeout=90000)
            
            # --- REMPLISSAGE FORMULAIRE ---
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", date_start)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", date_end)
            page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie", "3") # Services
            
            log("📝 Clic sur Rechercher...")
            with page.expect_navigation(timeout=60000):
                page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche")

            # Attente du tableau
            try:
                page.wait_for_selector(".table-results", timeout=20000)
            except:
                log("⚠️ Pas de résultats ou timeout.")
                browser.close()
                return True

            # --- GESTION DU NOMBRE DE RÉSULTATS & PAGINATION ---
            # 1. Lire le nombre total
            try:
                count_text = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
                total_results = int(count_text.strip())
                log(f"📊 Total trouvé : {total_results} offres.")
            except:
                total_results = 0
                log("⚠️ Impossible de lire le nombre total.")

            # 2. Passer à 500 résultats si nécessaire
            if total_results > 10:
                log("🔄 Passage à l'affichage 500 par page...")
                try:
                    # On sélectionne "500" et on attend que le site recharge
                    with page.expect_response(lambda response: response.status == 200, timeout=30000):
                        page.select_option("#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500")
                    # Petite pause de sécurité pour le rendu DOM
                    time.sleep(3) 
                except Exception as e:
                    log(f"⚠️ Erreur changement page size: {e}")

            # 3. Calcul des pages
            # Si on affiche 500 par page, le nombre de pages est faible
            total_pages = math.ceil(total_results / 500)
            if total_pages == 0: total_pages = 1
            
            log(f"📚 Scan de {total_pages} page(s) prévu.")

            # --- BOUCLE SUR LES PAGES ---
            for current_page in range(1, total_pages + 1):
                log(f"📄 Analyse Page {current_page}...")

                # Récupération des lignes
                rows = page.locator(".table-results tbody tr")
                count_on_page = rows.count()
                log(f"   🔎 {count_on_page} lignes sur cette page.")

                for i in range(count_on_page):
                    row = rows.nth(i)
                    if not row.is_visible(): continue

                    try:
                        full_row_text = row.inner_text()
                        offer_id = hashlib.md5(full_row_text.encode('utf-8')).hexdigest()
                        
                        if offer_id in seen_ids: continue
                        
                        # Extraction
                        ref_el = row.locator("span.ref")
                        ref = ref_el.inner_text().strip() if ref_el.count() > 0 else "N/A"

                        objet_el = row.locator("div[id*='_panelBlocObjet']")
                        objet = objet_el.inner_text().replace("Objet\n:", "").replace("Objet :", "").strip() if objet_el.count() > 0 else ""

                        buyer_el = row.locator("div[id*='_panelBlocDenomination']")
                        buyer = buyer_el.inner_text().replace("Acheteur public\n:", "").replace("Acheteur public :", "").strip() if buyer_el.count() > 0 else ""

                        deadline_el = row.locator("td[headers='cons_dateEnd'] .cloture-line")
                        deadline = deadline_el.inner_text().replace("\n", " ").strip() if deadline_el.count() > 0 else ""

                        link_el = row.locator("td.actions a").first
                        relative_link = link_el.get_attribute("href")
                        final_link = f"https://www.marchespublics.gov.ma/index.php{relative_link}" if relative_link else URL_AO

                        # Scoring
                        score, matched_category = scorer(objet, buyer)

                        if score > 0:
                            log(f"      ✅ Trouvé : {buyer[:30]}...")
                            is_agri_special = matched_category == "Agri"
                            
                            if is_agri_special:
                                msg_text = (
                                    f"🚜 **URGENT AGRI (AO)** 🚜\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🏛️ *Acheteur :* {buyer}\n"
                                    f"📅 *Limite :* `{deadline}`\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"{objet}\n\n"
                                    f"🔗 [VOIR L'APPEL D'OFFRE]({final_link})"
                                )
                            else:
                                msg_text = (
                                    f"🚨 **ALERTE AO - {matched_category}**\n"
                                    f"🏛️ {buyer}\n"
                                    f"⏳ *{deadline}* | 🎯 Score: *{score}*\n\n"
                                    f"{objet}\n\n"
                                    f"🔗 [Voir l'offre]({final_link})"
                                )

                            pending_alerts.append({
                                'score': score + (500 if is_agri_special else 0),
                                'msg': msg_text,
                                'id': offer_id
                            })

                    except Exception as e: continue
                
                # Passer à la page suivante si ce n'est pas la dernière
                if current_page < total_pages:
                    log("➡️ Page suivante...")
                    try:
                        # Clic sur la flèche "Suivant" (ID identifié dans ton HTML)
                        page.click("#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2")
                        # Attente chargement
                        page.wait_for_load_state("networkidle")
                        time.sleep(3)
                    except Exception as e:
                        log(f"❌ Erreur pagination: {e}")
                        break # Stop si on ne peut pas changer de page

        except Exception as e:
            log(f"❌ Erreur technique: {e}")
            return False

        browser.close()

    if pending_alerts:
        pending_alerts.sort(key=lambda x: x['score'], reverse=True)
        count_sent = 0
        admin_id = SUBSCRIBERS[0]["id"]
        
        for item in pending_alerts:
            new_ids.add(item['id'])
            send_telegram_to_user(admin_id, item['msg'])
            count_sent += 1
        
        seen_ids.update(new_ids)
        save_seen(seen_ids)
        log(f"🚀 {count_sent} alertes envoyées.")
    else:
        log("Ø Rien de nouveau (AO).")

    return True

def run_with_retries():
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"🏁 Démarrage Scan AO Solo (Tentative {attempt}/{MAX_RETRIES})...")
            success = scan_ao_attempt()
            if success: return 
        except Exception as e:
            log(f"⚠️ ERREUR TENTATIVE {attempt} : {e}")
            if attempt < MAX_RETRIES:
                time.sleep(60)
            else:
                log("❌ ECHEC TOTAL.")
                send_telegram_to_user(SUBSCRIBERS[0]["id"], f"❌ Crash Bot AO: {e}")

if __name__ == "__main__":
    log("🚀 Bot AO Solo Démarré")
    send_telegram_to_user(SUBSCRIBERS[0]["id"], "🚜 Bot AO (Solo) connecté. Pagination et Filtres actifs !")
    
    while True:
        run_with_retries()
        log("💤 Pause de 4 heures...")
        time.sleep(14400)