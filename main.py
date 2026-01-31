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

# --- 👤 UTILISATEUR UNIQUE ---
SUBSCRIBERS = [
    {
        "name": "Administrateur",
        "id": "1952904877", 
        "subscriptions": ["ALL"] 
    }
]

# --- 🎯 WHITELIST ACHETEURS ---
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
            
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", date_start)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", date_end)
            page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie", "3") 
            
            log("📝 Formulaire rempli, Clic Rechercher...")
            with page.expect_navigation(timeout=60000):
                page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche")

            try:
                page.wait_for_selector(".table-results", timeout=20000)
                log("✅ Tableau de résultats détecté !")
            except:
                log("⚠️ Pas de tableau de résultats trouvé (Timeout).")
                browser.close()
                return True

            # --- PAGINATION ET LECTURE ---
            try:
                count_text = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
                total_results = int(count_text.strip())
                log(f"📊 Total affiché par le site : {total_results} offres.")
            except:
                total_results = 0
                log("⚠️ Impossible de lire le nombre total.")

            if total_results > 10:
                log("🔄 Passage à l'affichage 500 par page...")
                try:
                    with page.expect_response(lambda response: response.status == 200, timeout=30000):
                        page.select_option("#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500")
                    time.sleep(3) 
                except Exception as e:
                    log(f"⚠️ Erreur changement page size: {e}")

            total_pages = math.ceil(total_results / 500)
            if total_pages == 0: total_pages = 1
            
            log(f"📚 Scan de {total_pages} page(s) prévu.")

            for current_page in range(1, total_pages + 1):
                log(f"📄 Analyse Page {current_page}/{total_pages}...")

                rows = page.locator(".table-results tbody tr")
                count_on_page = rows.count()
                log(f"   🔎 {count_on_page} lignes trouvées sur cette page.")

                if count_on_page == 0:
                    log("   ⚠️ Bizarre : Aucune ligne 'tr' trouvée dans le tableau.")

                for i in range(count_on_page):
                    row = rows.nth(i)
                    if not row.is_visible(): continue

                    try:
                        # Extraction brute pour log
                        full_row_text = row.inner_text()
                        
                        # --- EXTRACTION DES CHAMPS ---
                        objet_el = row.locator("div[id*='_panelBlocObjet']")
                        objet = objet_el.inner_text().replace("Objet\n:", "").replace("Objet :", "").strip() if objet_el.count() > 0 else "N/A"

                        buyer_el = row.locator("div[id*='_panelBlocDenomination']")
                        buyer = buyer_el.inner_text().replace("Acheteur public\n:", "").replace("Acheteur public :", "").strip() if buyer_el.count() > 0 else "N/A"

                        # 🛠️ LOG DE DÉBOGAGE : Affiche chaque offre analysée
                        log(f"   👉 [{i+1}] Acheteur: '{buyer}' | Objet: '{objet[:50]}...'")

                        offer_id = hashlib.md5(full_row_text.encode('utf-8')).hexdigest()
                        if offer_id in seen_ids: 
                            log("      ↳ 💤 Déjà vue (Ignorée)")
                            continue

                        # --- SCORING ---
                        score, matched_category = scorer(objet, buyer)
                        
                        # 🛠️ LOG DU SCORE
                        if score > 0:
                            log(f"      ✅ GARDÉE ! Score: {score} ({matched_category})")
                        else:
                            log(f"      ❌ REJETÉE : {matched_category}") # matched_category contient la raison du rejet (ex: "Exclu")

                        if score > 0:
                            # Extraction du reste si c'est bon
                            deadline_el = row.locator("td[headers='cons_dateEnd'] .cloture-line")
                            deadline = deadline_el.inner_text().replace("\n", " ").strip() if deadline_el.count() > 0 else ""
                            
                            link_el = row.locator("td.actions a").first
                            relative_link = link_el.get_attribute("href")
                            final_link = f"https://www.marchespublics.gov.ma/index.php{relative_link}" if relative_link else URL_AO

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

                    except Exception as e: 
                        log(f"   ⚠️ Erreur lecture ligne {i}: {e}")
                        continue
                
                # Page suivante
                if current_page < total_pages:
                    log("➡️ Clic Page Suivante...")
                    try:
                        page.click("#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2")
                        page.wait_for_load_state("networkidle")
                        time.sleep(3)
                    except Exception as e:
                        log(f"❌ Erreur pagination: {e}")
                        break

        except Exception as e:
            log(f"❌ Erreur technique globale: {e}")
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
        log(f"🚀 {count_sent} alertes envoyées sur Telegram.")
    else:
        log("Ø Aucune offre pertinente trouvée (après filtrage).")

    return True

def run_with_retries():
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"🏁 Démarrage Scan AO (Tentative {attempt}/{MAX_RETRIES})...")
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
    log("🚀 Bot AO Démarré (Mode BAVARD)")
    send_telegram_to_user(SUBSCRIBERS[0]["id"], "🚜 Bot AO connecté. Je t'affiche tout dans les logs maintenant !")
    
    while True:
        run_with_retries()
        log("💤 Pause de 4 heures...")
        time.sleep(14400)