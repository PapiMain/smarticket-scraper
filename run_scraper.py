from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from urllib.parse import quote
import requests
from datetime import datetime
import pytz


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

# Load Google Sheets credentials from environment variable
def get_short_names():
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
    client = gspread.authorize(creds)
    

    sheet = client.open("דאטה אפשיט אופיס").worksheet("הפקות")
    short_names = sheet.col_values(2)  # for example, if "שם מקוצר" is column B
    return [name for name in short_names if name and name != "שם מקוצר"]

# Set up Selenium WebDriver
def get_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/chromium-browser"  # 👈 important

    service = Service(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    return driver

# Save screenshot for debugging
def save_debug(driver, show_name, suffix):
    safe_name = show_name.replace(" ", "_").replace("/", "_")
    os.makedirs("screenshots", exist_ok=True)
    path = f"screenshots/{safe_name}_{suffix}_{int(time.time())}.png"
    driver.save_screenshot(path)
    print(f"📸 Screenshot saved: {path}")

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
        print("ℹ️ Page title:", title)
        print("ℹ️ First 500 chars of HTML:", html[:500])
        save_debug(driver, show_name, "captcha")
        return True

    # Quick check: Cloudflare interstitial
    if "just a moment" in title:
        print(f"⏳ Cloudflare interstitial detected (not necessarily captcha) for '{show_name}'")
        save_debug(driver, show_name, "cf_interstitial")
        return False

    print(f"✅ No CAPTCHA detected for '{show_name}'")
    return False

# Detect reCAPTCHA site key
def get_recaptcha_site_key(driver):
    """
    Detects reCAPTCHA v2 site key dynamically from the page.
    Returns the site key string if found, else None.
    """
    try:
        # Wait for the iframe that contains the reCAPTCHA
        iframe = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src,'recaptcha')]"))
        )
        src = iframe.get_attribute("src")
        # The site key is usually in the query string: k=SITE_KEY
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(src)
        query_params = parse_qs(parsed_url.query)
        site_key = query_params.get("k", [None])[0]
        if site_key:
            print(f"🧩 Detected reCAPTCHA site key: {site_key}")
        return site_key
    except TimeoutException:
        return None

# Solve CAPTCHA using CapSolver
def solve_captcha(site_url, site_key, captcha_type="recaptcha"):
    """
    Uses CapSolver to solve reCAPTCHA v2 or Cloudflare Turnstile.
    """
    print(f"🧩 Solving {captcha_type.upper()} CAPTCHA via CapSolver...")

    if captcha_type == "recaptcha":
        task = {
            "type": "NoCaptchaTaskProxyless",   # reCAPTCHA v2
            "websiteURL": site_url,
            "websiteKey": site_key
        }
    elif captcha_type == "turnstile":
        task = {
            "type": "TurnstileTaskProxyless",   # Cloudflare Turnstile
            "websiteURL": site_url,
            "websiteKey": site_key
        }
    else:
        raise ValueError(f"Unsupported captcha_type: {captcha_type}")

    data = {
        "clientKey": CAPSOLVER_API_KEY,
        "task": task
    }

    # 1️⃣ Create Task
    create_task = requests.post("https://api.capsolver.com/createTask", json=data).json()
    if create_task.get("errorId") != 0:
        raise Exception(f"CapSolver createTask error: {create_task}")

    task_id = create_task["taskId"]

    # 2️⃣ Poll result
    for _ in range(30):  # 30 attempts ~ 60s
        time.sleep(2)
        result = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
        ).json()

        if result.get("status") == "ready":
            if captcha_type == "recaptcha":
                token = result["solution"]["gRecaptchaResponse"]
            else:  # turnstile
                token = result["solution"]["token"]

            print("✅ CAPTCHA solved")
            return token

    raise Exception("❌ CAPTCHA solving timed out")

# Parse Hebrew date string
def parse_hebrew_date(date_str):
    """
    Convert Hebrew date string like 'יום רביעי, 17 ספטמבר 2025' into 'dd/mm/yyyy'
    """
    try:
        # Remove the day name and comma
        parts = date_str.split(",")
        if len(parts) == 2:
            date_part = parts[1].strip()  # e.g., "17 ספטמבר 2025"
        else:
            date_part = date_str.strip()

        day, month_name, year = date_part.split()
        day = int(day)
        month = HEBREW_MONTHS.get(month_name)
        year = int(year)

        if month:
            return datetime(year, month, day).strftime("%d/%m/%Y")
        else:
            return date_str  # fallback if month not found
    except Exception as e:
        print(f"⚠️ Failed to parse date '{date_str}': {e}")
        return date_str

# Step 1: Get all show URLs from the search results
def get_show_urls(driver):
    WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a.show"))
    )
    show_elements = driver.find_elements(By.CSS_SELECTOR, "a.show")
    urls = [el.get_attribute("href") for el in show_elements if el.get_attribute("href")]
    print(f"✅ Found {len(urls)} show URLs")
    return urls

# Step 2: Extract show details from an individual show page
def extract_show_details(driver, url):
    show = {"url": url}
    try:
        driver.get(url)
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

def update_sheet_with_shows(show, site_tab):
    """Update Google Sheet with available seats for a show."""
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
    client = gspread.authorize(creds)

    sheet = client.open("דאטה אפשיט אופיס").worksheet("כרטיסים")
    data = sheet.get_all_records()
    headers = sheet.row_values(1)

    available_col = headers.index("נמכרו") + 1  # Or whichever column you want to update
    updated_col = headers.index("עודכן לאחרונה") + 1

    scraped_date = datetime.strptime(show["date"], "%d/%m/%Y").date()
    israel_tz = pytz.timezone("Asia/Jerusalem")
    now_israel = datetime.now(israel_tz).strftime('%d/%m/%Y %H:%M:%S')

    # Determine the organization based on site
    org_map = {"Papi": "סמארטיקט", "Friends": "פרינדס"}
    org_value = org_map.get(site_tab, "")

    updated = False

    for i, row in enumerate(data, start=2):  # row 1 = headers
        try:
            row_date = row["תאריך"]
            if isinstance(row_date, str):
                try:
                    row_date = datetime.strptime(row_date, "%d/%m/%Y").date()
                except:
                    continue
            elif isinstance(row_date, datetime):
                row_date = row_date.date()

            # Flexible title matching
            title_match = (show["name"].strip() in row["הפקה"].strip()
                           or row["הפקה"].strip() in show["name"].strip())

            if title_match and row_date == scraped_date and row["ארגון"].strip() == org_value:
                # Update sold or available seats
                sold = int(row.get("קיבלו", 0)) - int(show.get("available_seats", 0))
                sheet.update_cell(i, available_col, sold)
                sheet.update_cell(i, updated_col, now_israel)
                updated = True
                print(f"✅ Updated row {i}: {show['name']} - Sold = {sold}")
                break

        except Exception as e:
            print(f"⚠️ Error parsing row {i}: {e}")

    if not updated:
        print(f"❌ No matching row found for {show['name']} on {show['date']}")

def scrape_site(site_config):
    base_url = site_config["base_url"]
    sheet_tab = site_config["sheet_tab"]
    print(f"🌐 Starting scraper for site: {sheet_tab} ({base_url})")

    driver = get_driver()

    try:
        # Load show names from Google Sheets
        short_names = get_short_names()
        print(f"🔎 Loaded {len(short_names)} short names")

        for name in short_names[:5]:  # first 5 for testing
            print(f"➡️ Searching for: {name}")

            # Encode the show name for the URL
            search_url = f"{base_url}search?q={quote(name)}"

            try:
                driver.get(search_url)

                is_captcha = is_captcha_page(driver, name)

                if is_captcha:
                    # helpful local helper names
                    safe_name = name.replace(" ", "_").replace("/", "_")
                    timestamp = int(time.time())

                    # Save a "before" screenshot and HTML snapshot
                    try:
                        save_debug(driver, name, "before_captcha")
                        html_path = f"screenshots/{safe_name}_before_{timestamp}.html"
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(driver.page_source)
                        print(f"💾 Saved HTML snapshot: {html_path}")
                    except Exception as e:
                        print(f"⚠️ Failed to save before-snapshot: {e}")

                    # Try to detect reCAPTCHA sitekey (your existing helper)
                    recaptcha_site_key = get_recaptcha_site_key(driver)
                    print(f"🔍 reCAPTCHA site key detection result: {recaptcha_site_key}")

                    # Try to detect Turnstile (Cloudflare) sitekey
                    try:
                        turnstile_site_key = driver.execute_script(
                            "var el = document.querySelector('[data-sitekey]');"
                            "return el ? el.getAttribute('data-sitekey') : null;"
                        )
                    except Exception as e:
                        print(f"⚠️ Error extracting Turnstile sitekey via JS: {e}")
                        turnstile_site_key = None
                    print(f"🔍 Turnstile site key detection result: {turnstile_site_key}")

                    # If neither was found, dump helpful hints and skip this term
                    if not recaptcha_site_key and not turnstile_site_key:
                        page_snippet = driver.page_source[:2000].lower()
                        if "turnstile" in page_snippet:
                            print("ℹ️ Page contains 'turnstile' - likely Cloudflare Turnstile.")
                        if "hcaptcha" in page_snippet or "h-captcha" in page_snippet:
                            print("ℹ️ Page contains 'hcaptcha' - could be hCaptcha.")
                        print("❌ Could not detect a known CAPTCHA site key - saving snapshot and skipping this term.")
                        try:
                            save_debug(driver, name, "no_site_key")
                            with open(f"screenshots/{safe_name}_no_site_key_{timestamp}.html", "w", encoding="utf-8") as f:
                                f.write(driver.page_source)
                        except Exception:
                            pass
                        continue

                    # Choose which type to solve
                    if recaptcha_site_key:
                        captcha_type = "recaptcha"
                        site_key = recaptcha_site_key
                    else:
                        captcha_type = "turnstile"
                        site_key = turnstile_site_key

                    try:
                        # Solve captcha — pass captcha_type so your solver can choose the right task
                        # (Update solve_captcha to handle 'turnstile' if it doesn't yet.)
                        token = solve_captcha(driver.current_url, site_key, captcha_type=captcha_type)
                        print(f"🧩 Received token (start, type={captcha_type}): {token[:40]}...")

                        # Take screenshot immediately before injection (extra safety)
                        save_debug(driver, name, "before_injection")

                        # Inject token into the correct input
                        if captcha_type == "recaptcha":
                            # Ensure g-recaptcha-response exists
                            try:
                                exists = driver.execute_script('return !!document.getElementById("g-recaptcha-response");')
                                if not exists:
                                    driver.execute_script(
                                        'var t=document.createElement("textarea");'
                                        't.id="g-recaptcha-response"; t.style.display="none";'
                                        'document.body.appendChild(t);'
                                    )
                                    print("ℹ️ Created missing g-recaptcha-response textarea in DOM.")
                            except Exception as e:
                                print(f"⚠️ Could not check/create g-recaptcha-response element: {e}")

                            # Inject token safely (escape quotes)
                            safe_token = token.replace('"', '\\"')
                            try:
                                driver.execute_script('document.getElementById("g-recaptcha-response").style.display = "block";')
                                driver.execute_script(f'document.getElementById("g-recaptcha-response").value = "{safe_token}";')
                                print("📝 Injected token into g-recaptcha-response element.")
                            except Exception as e:
                                print(f"❌ Failed injecting token into g-recaptcha-response: {e}")
                                save_debug(driver, name, "inject_failed")

                            # Try to trigger any grecaptcha callbacks if present
                            try:
                                driver.execute_script(
                                    "if(window.grecaptcha && typeof window.grecaptcha.getResponse === 'function'){"
                                    "/* noop */ }"
                                )
                            except Exception:
                                pass

                        else:  # turnstile
                            # Find hidden input for Turnstile response (common name: cf-turnstile-response)
                            try:
                                # Try common selector first; fallback to finding by id/name
                                exists = driver.execute_script(
                                    "return !!(document.querySelector('input[name=\"cf-turnstile-response\"]') || document.querySelector('[id^=\"cf-chl-widget\"]'))"
                                )
                            except Exception as e:
                                print(f"⚠️ Could not check Turnstile response element: {e}")
                                exists = False

                            # Insert hidden input if missing (some pages include empty input already)
                            try:
                                driver.execute_script(
                                    'if(!document.querySelector(\'input[name="cf-turnstile-response"]\')){'
                                    'var t=document.createElement("input"); t.type="hidden"; '
                                    't.name="cf-turnstile-response"; t.id="cf-turnstile-response"; '
                                    'document.body.appendChild(t); }'
                                )
                            except Exception as e:
                                print(f"⚠️ Could not create cf-turnstile-response input: {e}")

                            safe_token = token.replace('"', '\\"')
                            try:
                                driver.execute_script(
                                    f'var el = document.querySelector(\'input[name="cf-turnstile-response"]\') || document.getElementById("cf-chl-widget-78zb3_response") || document.getElementById("cf-turnstile-response");'
                                    f'if(el) el.value = "{safe_token}";'
                                )
                                print("📝 Injected token into cf-turnstile-response (Turnstile).")
                            except Exception as e:
                                print(f"❌ Failed injecting token into cf-turnstile-response: {e}")
                                save_debug(driver, name, "inject_failed")

                            # Optionally call any Turnstile callback if present (best-effort)
                            try:
                                driver.execute_script('if(window.turnstile && window.turnstile.execute) { /* noop */ }')
                            except Exception:
                                pass

                        # Verify the token got set (best-effort)
                        try:
                            if captcha_type == "recaptcha":
                                set_val = driver.execute_script('return document.getElementById("g-recaptcha-response").value;')
                            else:
                                set_val = driver.execute_script(
                                    'var el = document.querySelector(\'input[name="cf-turnstile-response"]\'); return el ? el.value : null;'
                                )
                            print(f"✅ Token field now set (start): {set_val[:40] if set_val else 'EMPTY'}")
                        except Exception as e:
                            print(f"⚠️ Could not read token value after injection: {e}")

                        # Try to submit a form or refresh so server validates token
                        try:
                            form_exists = driver.execute_script('return !!document.getElementById("captcha-form");')
                            print(f"📂 captcha-form exists: {form_exists}")
                            if form_exists:
                                try:
                                    driver.execute_script('document.getElementById("captcha-form").submit();')
                                    print("📤 Submitted captcha-form.")
                                except Exception as e:
                                    print(f"⚠️ Failed to submit captcha-form: {e}")
                            else:
                                # fallback: reload page and hope server sees token
                                try:
                                    driver.refresh()
                                    print("🔁 Page refreshed after injection.")
                                except Exception as e:
                                    print(f"⚠️ Failed to refresh page after injection: {e}")
                        except Exception as e:
                            print(f"⚠️ Error when checking/submitting captcha-form: {e}")

                        # Wait a short time for the site to react (Cloudflare may redirect)
                        time.sleep(4)

                        # Save "after" screenshot + HTML snapshot
                        try:
                            save_debug(driver, name, "after_captcha")
                            with open(f"screenshots/{safe_name}_after_{timestamp}.html", "w", encoding="utf-8") as f:
                                f.write(driver.page_source)
                            print("💾 Saved after-injection HTML snapshot.")
                        except Exception as e:
                            print(f"⚠️ Failed to save after-snapshot: {e}")

                        print("🌍 Current URL after CAPTCHA handling:", driver.current_url)
                        print("✅ Injected CAPTCHA solution")
                    except Exception as e:
                        # Save debugging artifacts if something fails during solving/injection
                        print(f"❌ Failed to solve/inject CAPTCHA for {sheet_tab}: {e}")
                        try:
                            save_debug(driver, name, "captcha_error")
                            with open(f"screenshots/{safe_name}_error_{timestamp}.html", "w", encoding="utf-8") as f:
                                f.write(driver.page_source)
                        except:
                            pass
                        continue  # skip this show (or you could retry depending on your strategy)
                else:
                    print("❌ Could not detect site key, skipping CAPTCHA")
                    continue

                
                print(f"✅ Finished search for: {name}")
                print("🌍 Current URL:", driver.current_url)

                urls = get_show_urls(driver)
                
                for url in urls:
                    show = extract_show_details(driver, url)

                    try:
                        available = count_empty_seats(driver)
                        show["available_seats"] = available
                        print(f"🎫 Available seats for {show['name']} on {show['date']}: {available}")
                        # Update Google Sheet
                        update_sheet_with_shows(show, sheet_tab)
                    except Exception as seat_e:
                        print(f"❌ Error counting seats for {show.get('name','?')}: {seat_e}")
                        show["available_seats"] = None

            except Exception as inner_e:
                print(f"❌ Error on show '{name}': {inner_e}")
                # Save a screenshot with the show name
                save_debug(driver, name, "captcha")

    except Exception as e:
        print(f"❌ Error while scraping {base_url}: {e}")
    finally:
        driver.quit()

# Run daily scrapers
for site in ["friends", "papi"]:
    scrape_site(SITES[site])