import random
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
import os
from urllib.parse import quote
from urllib.parse import urlparse, parse_qs
import requests
from datetime import datetime
import pytz
import re
from py_appsheet import AppSheetClient
from seleniumbase import Driver

HEBREW_MONTHS = {
    "ינואר": 1,
    "פברואר": 2,
    "מרץ": 3,
    "אפריל": 4,
    "מאי": 5,
    "יוני": 6,
    "יולי": 7,
    "אוגוסט": 8,
    "ספטמבר": 9,
    "אוקטובר": 10,
    "נובמבר": 11,
    "דצמבר": 12
}

# Helper function to clean URLs from AppSheet, handling both direct strings and HYPERLINK formulas
def clean_url(url_data):
    if not url_data: return ""
    if isinstance(url_data, dict): return url_data.get("Url", "")
    
    # Remove AppSheet's HYPERLINK wrapper if it exists as a string
    str_url = str(url_data)
    if "http" in str_url:
        # Extract everything starting from http until the first " or , or )
        match = re.search(r'https?://[^\s",\)]+', str_url)
        if match:
            return match.group(0)
            # return match.group(0).rstrip('"').rstrip(')')
    return str_url.strip()

# Helper function to fetch data from AppSheet using py-appsheet
def get_appsheet_data(table_name):
    """Uses the py-appsheet library to fetch data with the correct arguments."""
    client = AppSheetClient(
        app_id=os.environ.get("APPSHEET_APP_ID"),
        api_key=os.environ.get("APPSHEET_APP_KEY"),
    )
    
    try:
        # Pass None as the 'item' to fetch all rows without a specific search term
        print(f"⏳ Fetching all rows from table: {table_name}")
        rows = client.find_items(table_name, "")
        
        if rows:
            print(f"✅ Successfully retrieved {len(rows)} rows from {table_name}")
            return rows
        else:
            # If still 0 rows, try the most direct call possible
            print(f"⚠️ No rows found in {table_name}. Checking for server-side filter...")
            return client.find_items(table_name, selector="true")
            
    except Exception as e:
        print(f"❌ py-appsheet error: {e}")
        return []

# Main function to determine which shows to scrape based on AppSheet data and return the optimized list of targets
def get_optimized_targets():
    """
    Returns:
    - global_productions: List of short names to search on Papi/Friends.
    - hall_targets: Dict { "hall_url": ["Short Name 1", ... ] } for specific halls.
    """
    productions = get_appsheet_data("הפקות")
    events = get_appsheet_data("אירועי עתיד")
    halls = get_appsheet_data("אולמות")

    if not events:
        print("⚠️ No future events found in 'אירועי עתיד'.")
        return [], {}
    
   # Key = Hall Name in AppSheet, Value = The correct URL to use
    special_halls_lookup = {
        # "היכל התרבות מודיעין מכבים רעות": "https://www.shows.org.il/",
        "היכל התרבות יבנה": "https://www.htyavne.co.il/",
        "היכל התרבות-בית יד לבנים רעננה": "https://tickets.raanana.muni.il/",
        "תאטרון גבעתיים": "https://t-g.smarticket.co.il/",
        "היכל התרבות אור עקיבא": "https://htorakiva.smarticket.co.il/",
        "מרכז אמנויות הבמה שוהם": "https://shoham.smarticket.co.il/",
        "תאטרון חולון": "https://hth.smarticket.co.il/"
    }

    # 1. Create a mapping of Full Production Name -> Short Name
    # (Since events table likely uses the full name)
    prod_name_to_short = {p.get("שם הפקה מלא"): p.get("שם מקוצר") for p in productions if p.get("שם הפקה מלא")}

    active_production_dates = {} # { "סבא אליעזר": ["14/03/2026", "28/03/2026"] }

    for e in events:
        full_name = e.get("הפקה")
        date = e.get("תאריך") # וודא שזה הפורמט שמופיע באתר (למשל DD/MM/YYYY)
        short = prod_name_to_short.get(full_name)
        if short and date:
            if short not in active_production_dates:
                active_production_dates[short] = []
            active_production_dates[short].append(date)

    all_short_names = list(active_production_dates.keys())
    print(f"🎯 Found {len(all_short_names)} active productions with future events.")

    # 2. Create Hall URL lookup
    hall_url_map = {}    # Map Hall Name -> Clean String URL

    for h in halls:
        raw_url = h.get("אתר")
        hall_name = h.get("שם אולם")
        if raw_url and hall_name:
            hall_url_map[str(hall_name).strip()] = clean_url(raw_url)

    # 3. Build the hall-specific search list
    hall_targets = {}
    print(f"📊 Processing {len(events)} events for hall targeting...")

    for e in events:
        hall_name = str(e.get("אולם", "")).strip()
        full_prod_name = e.get("הפקה")
        short_name = prod_name_to_short.get(full_prod_name)
        
        if not short_name:
            continue

        # First, check if it's one of our hard-coded special halls
        if hall_name in special_halls_lookup:
            url = special_halls_lookup[hall_name]
        else:
            # Otherwise, use the URL from AppSheet
            url = hall_url_map.get(hall_name)

        if url:
            is_special = hall_name in special_halls_lookup
            is_smarticket = "smarticket.co.il" in url
            
            if is_special or is_smarticket:
                # Exclude global hubs
                if "papi.smarticket" not in url and "friends.smarticket" not in url:
                    str_url = url if url.endswith("/") else f"{url}/"
                    
                    if str_url not in hall_targets:
                        hall_targets[str_url] = set()
                    hall_targets[str_url].add(short_name)

    # Convert sets back to lists
    hall_targets = {k: list(v) for k, v in hall_targets.items()}
    print(f"   - Halls to visit: {len(hall_targets)}")

    return all_short_names, hall_targets, active_production_dates

# 2. Update get_driver to use SeleniumBase UC Mode
def get_driver():
    return Driver(
        browser="chrome",
        uc=True,
        headless=False,  # Set to False so PyAutoGUI/UC can work
        no_sandbox=True,
        disable_gpu=True,
        incognito=True
    )

# Save screenshot for debugging
def save_debug(driver, show_name, suffix):
    """
    Save screenshot + page HTML + attempt to download inline images for later offline inspection.
    """
    safe_name = show_name.replace(" ", "_").replace("/", "_")
    os.makedirs("screenshots", exist_ok=True)
    ts = int(time.time())

    # 1) screenshot
    png_path = f"screenshots/{safe_name}_{suffix}_{ts}.png"
    try:
        driver.save_screenshot(png_path)
        print(f"📸 Screenshot saved: {png_path}")
    except Exception as e:
        print(f"⚠️ Failed saving screenshot: {e}")

    # 2) save page HTML
    html_path = f"screenshots/{safe_name}_{suffix}_{ts}.html"
    try:
        html = driver.page_source
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"🗂️ HTML saved: {html_path}")
    except Exception as e:
        print(f"⚠️ Failed saving HTML: {e}")

    # 3) attempt to download images referenced in the page (may be blocked by site)
    try:
        asset_dir = f"screenshots/{safe_name}_{suffix}_{ts}_assets"
        os.makedirs(asset_dir, exist_ok=True)
        imgs = driver.find_elements(By.TAG_NAME, "img")
        downloaded = 0
        for i, img in enumerate(imgs):
            try:
                src = img.get_attribute("src")
                if not src:
                    continue
                # make filename from URL
                parsed = urlparse(src)
                filename = os.path.basename(parsed.path) or f"img_{i}.bin"
                # avoid query params in filename
                filename = re.sub(r'[^0-9A-Za-z_.-]', '_', filename)
                dest = os.path.join(asset_dir, filename)
                # avoid re-downloading same file
                if os.path.exists(dest):
                    continue
                # download with requests (honor absolute URLs)
                resp = requests.get(src, timeout=15)
                if resp.status_code == 200:
                    with open(dest, "wb") as fh:
                        fh.write(resp.content)
                    downloaded += 1
            except Exception:
                continue
        if downloaded:
            print(f"🖼️ Downloaded {downloaded} images to {asset_dir}")
        else:
            print(f"🖼️ No images downloaded (site may block requests or images are data-URIs).")
    except Exception as e:
        print(f"⚠️ Error while saving assets: {e}")

# Parse Hebrew date string
def parse_hebrew_date(date_str):
    """
    Convert Hebrew date strings like:
    'ביום שבת, 15 בנובמבר 2025' → '15/11/2025'
    'יום רביעי, 17 ספטמבר 2025' → '17/09/2025'
    """
    if not date_str or not isinstance(date_str, str):
        return ""
    
    try:
        # ניקוי רווחים כפולים ותווים מוזרים
        clean = " ".join(date_str.split())
        clean = re.sub(r"ביום\s+|יום\s+|,", "", clean).strip()

        # Extract numeric day, month (with optional prefix ב), and year
        match = re.search(r"(\d{1,2})\s+ב?([א-ת]+)\s+(\d{4})", clean)
        if not match:
            return "" 

        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))

        month = HEBREW_MONTHS.get(month_name)
        if not month:
            return ""

        return datetime(year, month, day).strftime("%d/%m/%Y")

    except Exception as e:
        print(f"⚠️ Failed to parse date '{date_str}': {e}")
        return ""

# Check if we're on a landing page and navigate to the event page if needed
def ensure_event_page(driver):
    """
    Checks if the current page is a landing page. 
    If so, clicks the first 'Order Now' button to reach the detailed event page.
    """
    if "?id=" not in driver.current_url:
        try:
            # Wait for the table listing dates to appear
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".table-responsive table, a.btn-danger"))
            )
            
            # Find the 'Order Now' button (הזמן עכשיו)
            order_buttons = driver.find_elements(By.CSS_SELECTOR, "a.btn-danger[aria-label='הזמן עכשיו'], a.btn-danger")
            
            if order_buttons:
                print(f"🔗 Landing page detected ({driver.current_url}). Navigating to event ID...")
                driver.execute_script("arguments[0].click();", order_buttons[0])
                
                # Wait for the specific event container to load
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.show_details"))
                )
                return True
        except Exception as e:
            print(f"⚠️ Navigation to event page failed: {e}")
    return False

# Select area if area selection table appears (some shows require selecting an area before showing the seat map)
def select_area(driver):
    """
    Smarticket sometimes loads an AREA SELECTION table before the seat map.
    This function clicks the 'אולם' area (or the first available area),
    so the seat map becomes visible.
    """

    try:
        # Wait up to 5 sec for the areas table (if not found → no area selection)
        rows = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, "table.areas tr.area")
            )
        )
    except:
        # No area table → continue normally
        return

    # Try to click specifically the "אולם"
    for row in rows:
        cols = row.find_elements(By.TAG_NAME, "td")
        if not cols:
            continue

        area_name = cols[0].text.strip()

        if area_name == "אולם":
            button = row.find_element(By.CSS_SELECTOR, "input.button")
            driver.execute_script("arguments[0].click();", button)
            print("🟦 Selected area: אולם")
            return

    # If no “אולם” found → click the first one
    first_button = rows[0].find_element(By.CSS_SELECTOR, "input.button")
    driver.execute_script("arguments[0].click();", first_button)
    print("🟦 Selected first area (fallback)")
    
# Count empty seats in the chair_map table
def count_empty_seats(driver):
    """Count the number of empty seats in the chair_map table."""
    try:
        # Wait until the table is loaded
        WebDriverWait(driver, 10).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "table.chair_map td a.chair.empty")
        )
        empty_seats = driver.find_elements(By.CSS_SELECTOR, "table.chair_map td a.chair.empty")
        return len(empty_seats)
    except Exception as e:
        print(f"❌ Error counting empty seats: {e}")
        return 0

# Step 3: Update AppSheet with the new availability data using the matched IDs
def update_appsheet_batch(shows):
    """Matches scraped shows to AppSheet rows using ID and updates them."""
    app_id = os.environ.get("APPSHEET_APP_ID")
    app_key = os.environ.get("APPSHEET_APP_KEY")

    # 1. Fetch current data to find the IDs
    print("⏳ Fetching current AppSheet data to match IDs...")
    current_rows = get_appsheet_data("הופעות עתידיות")
    
    israel_tz = pytz.timezone("Asia/Jerusalem")
    now_israel = datetime.now(israel_tz).strftime('%d/%m/%Y %H:%M') 

    exclude_words = ["סוואנה", "אפריקה", "הפקת הענק"]

    updates = []
    for show in shows:
        try:
            scraped_date_obj = datetime.strptime(show["date"], "%d/%m/%Y").date()
        except:
            continue

        scraped_name = show["name"].strip()
        clean_scraped_name = " ".join(scraped_name.replace("–", "-").replace(".", "").split()).lower()
        short_name = show["searched_name"].strip() # נשתמש בשם המקוצר שהעברנו בפונקציה הראשית, כי הוא זה שמופיע באירועים העתידיים

        if "סימבה" in scraped_name and all(x not in scraped_name for x in ["סוואנה", "אפריקה"]): scraped_name = "סימבה מלך"

        tag = show.get("site_tag")
        if tag == "Papi":
            org_value = "סמארטיקט"
        elif tag == "Friends":
            org_value = "פרינדס"
        else:
            org_value = "אולם"

        # 2. Find the ID in AppSheet that matches this show
        match = None
        for row in current_rows:
            app_date_raw = row.get("תאריך")
            app_row_name = row.get("הפקה", "").strip().lower()

            # Date Format Guesser: AppSheet might send YYYY-MM-DD or MM/DD/YYYY
            app_date_obj = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    app_date_obj = datetime.strptime(app_date_raw, fmt).date()
                    break
                except: continue

            if app_date_obj != scraped_date_obj: continue

            # Comparison (Name + Date + Org)
            row_org = row.get("ארגון", "").strip()

            name_match = (short_name.lower() in app_row_name) or \
                         (app_row_name in short_name.lower())
            
            if "סימבה" in clean_scraped_name or "פיטר פן" in clean_scraped_name:
                if any(word in clean_scraped_name for word in exclude_words):
                    name_match = False

            # Match by Name, Date, and Organization
            if (name_match) and \
            app_date_obj == scraped_date_obj and \
            row_org == org_value:
                match = row
                break
        
        if not match:
            print(f"❌ No AppSheet match for: {scraped_name} vs {short_name} on {scraped_date_obj} ({org_value})")

        if match:
            # Calculate 'נמכרו' (Sold) logic
            try:
                total_capacity = int(match.get("קיבלו", 0))
                available = int(show.get("available_seats", 0))
                sold = total_capacity - available

                if sold < 0:
                    print(f"⚠️ Warning: Calculated sold tickets is negative for {scraped_name} - {show['hall']}, {scraped_date_obj} ({org_value}) ({match['ID']}). Setting sold to 0.")
                    sold = 0

                # Add to update list - MUST include the 'ID' key
                updates.append({
                    "ID": match["ID"], 
                    "נמכרו": sold,
                    "עודכן לאחרונה": now_israel
                })
                print(f"✅ Prepared update for {scraped_name}, {show['hall']}, {scraped_date_obj}, ({org_value}) - sold:{sold}, ID {match['ID']}")
            except Exception as e:
                print(f"❌ Calculation error for {scraped_name}: {e}")
    
    # 3. Send Batch Edit to AppSheet
    if updates:
        num_updates = len(updates)

        url = f"https://api.appsheet.com/api/v1/apps/{app_id}/tables/כרטיסים/Action"
        body = {
            "Action": "Edit",
            "Properties": {"Locale": "en-US"},
            "Rows": updates
        }
        resp = requests.post(url, json=body, headers={"ApplicationAccessKey": app_key})
        print(f"🚀 AppSheet Batch Update Status: {resp.status_code}")
        print(f"✅ Successfully updated {num_updates} rows in the 'כרטיסים' table.")
        if resp.status_code != 200:
            print(f"🚀 AppSheet Batch Update Status: {resp.status_code}")
            print(f"❌ AppSheet Update Error: {resp.text}")
    else:
        print("❌ No matching rows found in AppSheet.")

# Main function to run the search logic for a given site and search term, returning found shows with availability
def run_search_logic(driver, base_url, search_term, site_tag, active_dates_map):
    """
    Handles the actual search process on a specific website.
    Returns a list of 'show' dictionaries.
    """
    found_shows = []
    
    # 1. Construct and visit the search URL
    search_url = f"{base_url}search?q={quote(search_term)}"
    print(f"🔍 Navigating to: {search_url}")
    
    try:

        time.sleep(random.uniform(2, 4)) # Add a tiny human-like delay
        # UC Mode navigation: This handles the 'Just a moment' challenge automatically
        driver.uc_open_with_reconnect(search_url, reconnect_time=10)

        # If we are still blocked, try one "human" click
        if "Just a moment" in driver.title:
            print("🛡️ Cloudflare detected, attempting internal bypass...")
            driver.uc_gui_click_captcha() # SeleniumBase handles the "no mouse" issue better now
            time.sleep(4)
        
        print("📜 Scrolling to load all cards...")
        for _ in range(4): # 4 גלילות קטנות
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(1)

        try:
            WebDriverWait(driver, 7).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a.show"))
            )
        except TimeoutException:
            print(f"ℹ️ No results for '{search_term}'")
            return []
        
        valid_dates = active_dates_map.get(search_term, [])
        normalized_valid_dates = []
        for d in valid_dates:
            try:
                # אם התאריך הגיע כ-MM/DD/YYYY, נהפוך אותו ל-DD/MM/YYYY
                dt = datetime.strptime(d, "%m/%d/%Y")
                normalized_valid_dates.append(dt.strftime("%d/%m/%Y"))
            except:
                normalized_valid_dates.append(d)

        print(f"🎯 Target dates for '{search_term}': {normalized_valid_dates}")
        
        show_cards = driver.find_elements(By.CSS_SELECTOR, "a.show")
        total = len(show_cards)
        print(f"🔍 Found {total} show cards for '{search_term}' before date filtering.")

        # שמירת לינקים ונתונים שצריך לבדוק מושבים עבורם
        to_process = []
        
        for i, card in enumerate(show_cards):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
                time.sleep(0.2)

                # חילוץ נתונים ישירות מהכרטיסייה (ה-HTML ששלחת)
                raw_date = driver.execute_script("return arguments[0].querySelector('.date_container').innerText;", card).strip()
                full_name = driver.execute_script("return arguments[0].querySelector('h2').innerText;", card).strip()
                
                parsed_date = parse_hebrew_date(raw_date)

                print(f"   [{i+1}/{total}] Card Name: '{full_name}' | Date: '{parsed_date}'")

                if not parsed_date or not full_name:
                    # אם עדיין ריק, ננסה גלילה קטנה לאלמנט הספציפי
                    driver.execute_script("arguments[0].scrollIntoView();", card)
                    raw_date = driver.execute_script("return arguments[0].querySelector('.date_container').innerText;", card).strip()
                    full_name = driver.execute_script("return arguments[0].querySelector('h2').innerText;", card).strip()
                    parsed_date = parse_hebrew_date(raw_date)
                
                # אם התאריך לא ברשימה שלנו - מדלגים מיד בלי להיכנס ללינק!
                if normalized_valid_dates and parsed_date not in normalized_valid_dates:
                    print(f"⏩ Skipping {parsed_date} (Not in targets)")
                    continue
                else:
                     print(f"🎯 Date {parsed_date} is a target! Processing this show.")
                    
                # אם עברנו את הסינון, נאסוף את שאר הנתונים מהכרטיסייה
                hall = driver.execute_script("return arguments[0].querySelector('.theater_container').innerText;", card).strip().replace("(מפת הגעה)", "")
                time_val = driver.execute_script("return arguments[0].querySelector('.time_container').innerText;", card).replace("בשעה", "").strip()

                show_info = {
                    "url": card.get_attribute("href"),
                    "name": full_name,
                    "hall": hall,
                    "date": parsed_date,
                    "time": time_val,
                    "site_tag": site_tag,
                    "searched_name": search_term
                }
                to_process.append(show_info)
                print(f"⭐ Match found: {show_info['name']} - {show_info['hall']} on {parsed_date} - {show_info['time']}. Adding to queue.")

            except Exception as e:
                print(f"⚠️ Error processing a show card for {search_term}: {e}")
                print(f"   [{i+1}/{total}] ⚠️ Error reading card: {str(e)[:50]}")
                continue
        
        for show_data in to_process:
            try:
                print(f"⭐ Processing match: {show_data['name']} on {show_data['date']}")
                driver.get(show_data["url"])
                ensure_event_page(driver) # הפונקציה שלך שמטפלת בדפי נחיתה
                
                select_area(driver)
                available = count_empty_seats(driver)
                
                show_data["available_seats"] = available
                found_shows.append(show_data)
                print(f"✅ Scraped Seats: {show_data['name']} | Date: {show_data['date']} | Seats: {available}")
                
            except Exception as e:
                print(f"❌ Error extracting seats for {show_data['url']}: {e}")

    except Exception as e:
        print(f"❌ Critical error searching for '{search_term}' at {base_url}: {e}")
        save_debug(driver, search_term, "search_crash")

    return found_shows

# Main orchestrator function to scrape all targets and update AppSheet
def scrape_everything():
    short_names, hall_targets, active_dates_map = get_optimized_targets()
    driver = get_driver()
    all_results = []

    # --- PART 1: The Main Aggregators (Search EVERYTHING) ---
    main_sites = [
        {"url": "https://papi.smarticket.co.il/", "tab": "Papi"},
        {"url": "https://friends.smarticket.co.il/", "tab": "Friends"}
    ]     

    for site in main_sites:
        print(f"🌐 Scraping Aggregator: {site['tab']}")
        print(f"🌐 Scraping website: {site['url']}")
        for name in short_names:
            results = run_search_logic(driver, site['url'], name, site['tab'], active_dates_map)
            all_results.extend(results)

    # --- PART 2: Individual Halls (Search only relevant shows) ---
    for url, specific_shows in hall_targets.items():
        print(f"🏛️ Scraping Hall: {url}")
        for name in specific_shows:
            # We pass "Hall" as the tab so the update logic knows it's a specific hall
            results = run_search_logic(driver, url, name, "Hall", active_dates_map)
            all_results.extend(results)

    # --- PART 3: Batch Update ---
    if all_results:
        update_appsheet_batch(all_results)
    
    print("🏁 Scraper finished. Closing browser.")
    driver.quit()

# Main entry point
if __name__ == "__main__":
    scrape_everything()