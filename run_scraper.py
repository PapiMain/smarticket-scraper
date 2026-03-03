# from selenium import webdriver
import random
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
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

SITES = {
    "friends": {
        "base_url": "https://friends.smarticket.co.il/",
        "sheet_tab": "Friends"
    },
    "papi": {
        "base_url": "https://papi.smarticket.co.il/",
        "sheet_tab": "Papi"
    },}

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

CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY")  # store your CapSolver API key in env variable

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
        "היכל התרבות מודיעין מכבים רעות": "https://www.shows.org.il/",
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
    all_short_names = [p.get("שם מקוצר") for p in productions if p.get("שם מקוצר")]

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

    return all_short_names, hall_targets

# 2. Update get_driver to use SeleniumBase UC Mode
def get_driver():
    return Driver(
        browser="chrome",
        uc=True,
        # IMPORTANT: Set headless to False or remove it. 
        # Xvfb makes it "headed" but invisible to you.
        headless=False, 
        xvfb=True,       # Tells SeleniumBase to use the virtual display on Linux
        no_sandbox=True,
        disable_gpu=True,
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

# Check if current page is a CAPTCHA page
def is_captcha_page(driver, show_name="unknown"):
    html = driver.page_source.lower()
    title = driver.title.lower()

    # Detect real captcha indicators
    if ("iframe" in html and "recaptcha" in html) or \
       "g-recaptcha" in html or \
       "cf-challenge" in html or \
       "verifying" in html:
        print(f"⚠️ CAPTCHA elements detected for '{show_name}'")
        # print("ℹ️ Page title:", title)
        # print("ℹ️ First 500 chars of HTML:", html[:500])
        # save_debug(driver, show_name, "captcha")
        return True

    # Quick check: Cloudflare interstitial
    if "just a moment" in title:
        print(f"⏳ Cloudflare interstitial detected (not necessarily captcha) for '{show_name}'")
        save_debug(driver, show_name, "cf_interstitial")
        return False

    print(f"✅ No CAPTCHA detected for '{show_name}'")
    return False

# Parse Hebrew date string
def parse_hebrew_date(date_str):
    """
    Convert Hebrew date strings like:
    'ביום שבת, 15 בנובמבר 2025' → '15/11/2025'
    'יום רביעי, 17 ספטמבר 2025' → '17/09/2025'
    """
    try:
        # Remove leading 'ביום' or 'יום' and any commas
        clean = re.sub(r"ביום\s+|יום\s+|,", "", date_str).strip()

        # Extract numeric day, month (with optional prefix ב), and year
        match = re.search(r"(\d{1,2})\s+ב?([א-ת]+)\s+(\d{4})", clean)
        if not match:
            raise ValueError(f"Cannot parse Hebrew date: {date_str}")

        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))

        month = HEBREW_MONTHS.get(month_name)
        if not month:
            raise ValueError(f"Unknown Hebrew month: {month_name}")

        return datetime(year, month, day).strftime("%d/%m/%Y")

    except Exception as e:
        print(f"⚠️ Failed to parse date '{date_str}': {e}")
        return date_str

# Step 1: Get all show URLs from the search results
def get_show_urls(driver):
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a.show"))
        )
        show_elements = driver.find_elements(By.CSS_SELECTOR, "a.show")
        urls = [el.get_attribute("href") for el in show_elements if el.get_attribute("href")]
        print(f"✅ Found {len(urls)} show URLs")
        return urls
    except TimeoutException:
        print("ℹ️ No shows found for this search.")
        return []

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

# Step 2: Extract show details from an individual show page
def extract_show_details(driver, url):
    show = {"url": url}
    try:
        driver.get(url)

        ensure_event_page(driver)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.show_details"))
        )

        container = driver.find_element(By.CSS_SELECTOR, "div.show_details")

        # Title
        show["name"] = container.find_element(By.CSS_SELECTOR, "h1").text.strip()

        # Hall (remove "מפת הגעה")
        hall_text = container.find_element(By.CSS_SELECTOR, ".theater").text.strip()
        show["hall"] = hall_text.replace("(מפת הגעה)", "").strip()

        # Date
        raw_date = container.find_element(By.CSS_SELECTOR, ".event-date").text.strip()
        show["date"] = parse_hebrew_date(raw_date)  # stays only date

        # Time (clean string, keep only time)
        raw_time = container.find_element(By.CSS_SELECTOR, ".event-time").text.strip()
        show["time"] = raw_time.replace("בשעה", "").strip()

        # Price range
        try:
            price_text = container.find_element(By.CSS_SELECTOR, ".price_range").text.strip()
            show["price"] = price_text
        except:
            show["price"] = ""

        print(
            f"🎭 Extracted show: {show['name']} - {show['hall']} "
            f"({show['date']} | {show['time']}) - {show['price']}"
        )        

    except Exception as e:
        print(f"❌ Failed to extract show from {url}: {e}")

    return show

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
    current_rows = get_appsheet_data("כרטיסים")
    
    israel_tz = pytz.timezone("Asia/Jerusalem")
    now_israel = datetime.now(israel_tz).strftime('%d/%m/%Y %H:%M:%S') 

    updates = []
    for show in shows:
        # scraped_date = show["date"] # Assuming format 'DD/MM/YYYY'
        try:
            scraped_date_obj = datetime.strptime(show["date"], "%d/%m/%Y").date()
        except:
            continue

        scraped_name = show["name"].strip()

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
            if not app_date_raw: continue

            # Date Format Guesser: AppSheet might send YYYY-MM-DD or MM/DD/YYYY
            app_date_obj = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    app_date_obj = datetime.strptime(app_date_raw, fmt).date()
                    break
                except: continue

            if not app_date_obj: continue

            # Comparison (Name + Date + Org)
            row_name = row.get("הפקה", "").strip()
            row_org = row.get("ארגון", "").strip()

            # Match by Name, Date, and Organization
            if (scraped_name in row_name or row_name in scraped_name) and \
            app_date_obj == scraped_date_obj and \
            row_org == org_value:
                match = row
                break
        
        if not match:
            print(f"❌ No AppSheet match for: {scraped_name} on {show['date']} ({org_value})")

        if match:
            # Calculate 'נמכרו' (Sold) logic
            try:
                total_capacity = int(match.get("קיבלו", 0))
                available = int(show.get("available_seats", 0))
                sold = total_capacity - available

                # Add to update list - MUST include the 'ID' key
                updates.append({
                    "ID": match["ID"], 
                    "נמכרו": sold,
                    "עודכן לאחרונה": now_israel
                })
                print(f"✅ Prepared update for {scraped_name}: sold-{sold}, ID {match['ID']}")
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
def run_search_logic(driver, base_url, search_term, site_tag):
    """
    Handles the actual search process on a specific website.
    Returns a list of 'show' dictionaries.
    """
    found_shows = []
    
    # 1. Construct and visit the search URL
    search_url = f"{base_url}search?q={quote(search_term)}"
    print(f"🔍 Navigating to: {search_url}")
    
    try:
        # driver.get(search_url)

        time.sleep(random.uniform(2, 5)) # Add a tiny human-like delay
        # UC Mode navigation: This handles the 'Just a moment' challenge automatically
        driver.uc_open_with_reconnect(search_url, reconnect_time=10)

        # If we are still blocked, try one "human" click
        if "Just a moment" in driver.title:
            print("🛡️ Cloudflare detected, attempting internal bypass...")
            driver.uc_gui_click_captcha() # SeleniumBase handles the "no mouse" issue better now
            time.sleep(5)
        
        # Now just check if the content is there
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a.show"))
            )
            print("✅ Success! Page loaded.")
            # Proceed with your scraping...
        except:
            print("❌ Still blocked. Cloudflare won this round.")

        # 3. Get all show URLs from the search results
        urls = get_show_urls(driver)
        
        # 4. Process each individual show found
        for url in urls:
            show_data = extract_show_details(driver, url)
            
            if show_data.get("name"):
                try:
                    # Enter the seat map
                    select_area(driver)
                    # Count the seats
                    available = count_empty_seats(driver)
                    
                    show_data["available_seats"] = available
                    show_data["site_tag"] = site_tag # This tells the updater if it's Papi, Friends, or Hall
                    
                    found_shows.append(show_data)
                    print(f"✅ Scraped: {show_data['name']} on {show_data['date']} | Seats: {available}")
                
                except Exception as e:
                    print(f"❌ Error while checking seats at {url}: {e}")

    except Exception as e:
        print(f"❌ Blocked or Error: {e}")
        save_debug(driver, search_term, "blocked_by_cloudflare")
    # except Exception as e:
    #     print(f"❌ Critical error searching for '{search_term}' at {base_url}: {e}")
    #     save_debug(driver, search_term, "search_crash")

    return found_shows

# Main orchestrator function to scrape all targets and update AppSheet
def scrape_everything():
    short_names, hall_targets = get_optimized_targets()
    driver = get_driver()
    all_results = []

    # --- PART 1: The Main Aggregators (Search EVERYTHING) ---
    main_sites = [
        # {"url": "https://papi.smarticket.co.il/", "tab": "Papi"},
        {"url": "https://friends.smarticket.co.il/", "tab": "Friends"}
    ]     

    for site in main_sites:
        print(f"🌐 Scraping Aggregator: {site['tab']}")
        print(f"🌐 Scraping website: {site['url']}")
        for name in short_names:
            results = run_search_logic(driver, site['url'], name, site['tab'])
            all_results.extend(results)

    # --- PART 2: Individual Halls (Search only relevant shows) ---
    # for url, specific_shows in hall_targets.items():
    #     print(f"🏛️ Scraping Hall: {url}")
    #     for name in specific_shows:
    #         # We pass "Hall" as the tab so the update logic knows it's a specific hall
    #         results = run_search_logic(driver, url, name, "Hall")
    #         all_results.extend(results)

    # --- PART 3: Batch Update ---
    if all_results:
        update_appsheet_batch(all_results)
    
    print("🏁 Scraper finished. Closing browser.")
    driver.quit()

# Main entry point
if __name__ == "__main__":
    scrape_everything()