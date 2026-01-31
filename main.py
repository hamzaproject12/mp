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
    },
    # {
    #     "name": "Administrateur",
    #     "id": "7879373928", 
    #     "subscriptions": ["ALL"] 
    # },
    # {
    #     "name": "Administrateur",
    #     "id": "8260779046", 
    #     "subscriptions": ["ALL"] 
    # }
]

# --- 🎯 WHITELIST ACHETEURS (STRICT) ---
# --- 🎯 WHITELIST ULTRA-COMPLÈTE (VARIATIONS & FAUTES INCLUSES) ---
TARGET_BUYERS = [
    # --- DIRECTIONS RÉGIONALES (DRA) ---
    "DIRECTION REGIONALE DE L'AGRICULTURE",
    "DIRECTION REGIONALE D'AGRICULTURE",
    "DIRECTEUR REGIONAL DE L'AGRICULTURE",
    "DIRECTEUR REGIONAL D'AGRICULTURE",
    "DIRECTION REGIONALE AGRICULTURE", # Variante courte
    
    # --- DIRECTIONS PROVINCIALES (DPA) - Avec gestion des fautes d'accord ---
    "DIRECTION PROVINCIALE DE L'AGRICULTURE",
    "DIRECTION PROVINCIALE D'AGRICULTURE",
    "DIRECTEUR PROVINCIAL DE L'AGRICULTURE",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE",
    # Cas spécifiques de fautes fréquentes sur le portail :
    "DIRECTEUR PROVINCIALE DE L'AGRICULTURE", 
    "DIRECTEUR PROVINCIALE D'AGRICULTURE",
    "DIRECTION PROVINCIAL DE L'AGRICULTURE",
    
    # --- OFFICES DE MISE EN VALEUR (ORMVA) ---
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE",
    "OFFICE REGIONAL DE LA MISE EN VALEUR AGRICOLE",
    "ORMVA", # L'acronyme est souvent utilisé seul (ex: ORMVA DU GHARB)
    "O.R.M.V.A",
    
    # --- SÉCURITÉ SANITAIRE (ONSSA) ---
    "ONSSA",
    "OFFICE NATIONAL DE SECURITE SANITAIRE",
    "DIRECTION REGIONALE DE L'ONSSA",
    
    # --- CONSEIL AGRICOLE (ONCA) ---
    "ONCA",
    "OFFICE NATIONAL DU CONSEIL AGRICOLE",
    "DIRECTION REGIONALE DU CONSEIL AGRICOLE",
    "CENTRE DU CONSEIL AGRICOLE",
    "CONSEIL AGRICOLE",

    # --- CHAMBRES ---
    "CHAMBRE D'AGRICULTURE",
    "CHAMBRE REGIONALE D'AGRICULTURE",
    
    # --- INSTITUTS & ÉCOLES (FORMATION AGRICOLE) ---
    "ECOLE NATIONALE D'AGRICULTURE", # ENA
    
    # --- AGENCES DE DÉVELOPPEMENT ---
    "AGENCE NATIONALE POUR LE DEVELOPPEMENT DES ZONES OASIENNES" # ANDZOA
]

# --- EXCLUSIONS (SÉCURITÉ) ---
EXCLUSIONS = [
    "nettoyage", "gardiennage"
    , "construction", "bâtiment", "plomberie",
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

    # 2. VÉRIFICATION STRICTE DE L'ACHETEUR
    is_target_buyer = False
    for target in TARGET_BUYERS:
        if target.lower() in buyer_lower:
            is_target_buyer = True
            break
    
    if is_target_buyer:
        return 100, "Agri"
    else:
        return 0, "Acheteur Non-Cible"

def scan_ao_attempt():
    seen_ids = load_seen()
    new_ids = set()
    pending_alerts = [] 

    # --- CALCUL DES DATES (CORRECTION) ---
    today = datetime.now()
    
    # 1. Date de Mise en ligne : Entre "6 mois avant" et "Aujourd'hui"
    date_pub_start = (today - timedelta(days=180)).strftime("%d/%m/%Y")
    date_pub_end = today.strftime("%d/%m/%Y")

    # 2. Date limite de remise des plis : Entre "Aujourd'hui" et "6 mois après"
    date_deadline_start = today.strftime("%d/%m/%Y")
    date_deadline_end = (today + timedelta(days=180)).strftime("%d/%m/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        log(f"🌍 Connexion AO...")
        log(f"📅 Filtre Publication : {date_pub_start} -> {date_pub_end}")
        log(f"📅 Filtre Deadline    : {date_deadline_start} -> {date_deadline_end}")
        
        try:
            page.goto(URL_AO, timeout=90000)
            
            # --- REMPLISSAGE DU FORMULAIRE ---
            # 1. Mode de passation : AO Ouvert (1)
            page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_procedureType", "1")
            
            # 2. Catégorie : Services (3)
            page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie", "3")
            
            # 3. Date limite de remise des plis (Champs dateMiseEnLigneStart/End sur ce site)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", date_deadline_start)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", date_deadline_end)

            # 4. Date de mise en ligne (Champs dateMiseEnLigneCalculeStart/End)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeStart", date_pub_start)
            page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeEnd", date_pub_end)
            
            log("📝 Clic sur Rechercher...")
            with page.expect_navigation(timeout=60000):
                page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche")

            try:
                page.wait_for_selector(".table-results", timeout=20000)
                log("✅ Résultats affichés.")
            except:
                log("⚠️ Aucun résultat ou Timeout.")
                browser.close()
                return True

            # --- GESTION AFFICHAGE 500 ---
            try:
                count_text = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
                total_results = int(count_text.strip())
                log(f"📊 Total trouvé : {total_results} offres.")
            except:
                total_results = 0

            if total_results > 10:
                log("🔄 Passage en mode 500 résultats...")
                try:
                    with page.expect_response(lambda response: response.status == 200, timeout=60000):
                        page.select_option("#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500")
                    time.sleep(3) 
                except Exception as e:
                    log(f"⚠️ Erreur changement affichage: {e}")

            total_pages = math.ceil(total_results / 500)
            if total_pages == 0: total_pages = 1
            
            # --- BOUCLE PAGES ---
            for current_page in range(1, total_pages + 1):
                log(f"📄 Lecture Page {current_page}/{total_pages}...")
                
                rows = page.locator(".table-results tbody tr")
                count_on_page = rows.count()
                
                for i in range(count_on_page):
                    row = rows.nth(i)
                    if not row.is_visible(): continue

                    try:
                        full_row_text = row.inner_text()
                        
                        # --- EXTRACTION ---
                        buyer_el = row.locator("div[id*='_panelBlocDenomination']")
                        buyer = buyer_el.inner_text().replace("Acheteur public\n:", "").replace("Acheteur public :", "").strip() if buyer_el.count() > 0 else "N/A"

                        objet_el = row.locator("div[id*='_panelBlocObjet']")
                        objet = objet_el.inner_text().replace("Objet\n:", "").replace("Objet :", "").strip() if objet_el.count() > 0 else "N/A"

                        log(f"   👉 [{i+1}] Acheteur: '{buyer}'")

                        offer_id = hashlib.md5(full_row_text.encode('utf-8')).hexdigest()
                        if offer_id in seen_ids: 
                            log("      ↳ 💤 Déjà vue (Ignorée)")
                            continue

                        # --- SCORING ---
                        score, matched_reason = scorer(objet, buyer)

                        if score > 0:
                            log(f"      ✅ VALIDÉE ! ({matched_reason})")
                            
                            # Extraction date (Correction V7)
                            deadline_cells = row.locator("td[headers='cons_dateEnd'] .cloture-line")
                            if deadline_cells.count() > 0:
                                deadline = deadline_cells.first.inner_text().replace("\n", " ").strip()
                            else:
                                deadline = ""
                            
                            link_el = row.locator("td.actions a").first
                            relative_link = link_el.get_attribute("href")
                            final_link = f"https://www.marchespublics.gov.ma/index.php{relative_link}" if relative_link else URL_AO

                            msg_text = (
                                f"🚜 **OFFRE AGRI CIBLÉE** 🚜\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"🏛️ *Acheteur :* {buyer}\n"
                                f"📅 *Limite :* `{deadline}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{objet}\n\n"
                                f"🔗 [VOIR L'OFFRE]({final_link})"
                            )

                            pending_alerts.append({
                                'score': score,
                                'msg': msg_text,
                                'id': offer_id
                            })
                        else:
                             log(f"      ❌ REJETÉE : {matched_reason}")

                    except Exception as e: 
                        log(f"   ⚠️ Erreur lecture ligne {i}: {e}")
                        continue
                
                # Suivant
                if current_page < total_pages:
                    log("➡️ Clic Page Suivante...")
                    try:
                        page.click("#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2")
                        page.wait_for_load_state("networkidle")
                        time.sleep(3)
                    except: break

        except Exception as e:
            log(f"❌ Erreur: {e}")
            return False

        browser.close()

    if pending_alerts:
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
        log("Ø Aucune offre cible trouvée.")

    return True

def run_with_retries():
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"🏁 Scan AO Strict (Tentative {attempt})...")
            success = scan_ao_attempt()
            if success: return 
        except Exception as e:
            log(f"⚠️ Erreur {e}")
            time.sleep(60)

if __name__ == "__main__":
    log("🚀 Bot AO Démarré (FILTRE DATES + ACHETEURS)")
    send_telegram_to_user(SUBSCRIBERS[0]["id"], "🚜 Bot AO (Strict & Dates) : Je surveille les délais !")
    
    while True:
        run_with_retries()
        log("💤 Pause de 4 heures...")
        time.sleep(14400)