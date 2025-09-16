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
    options.add_argument("--disable-blink-features=AutomationControlled")

    service = Service(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)

    # Now you can safely inject the stealth JS
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined})
        """
    })

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
        # save_debug(driver, show_name, "captcha")
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
def solve_captcha(site_url, site_key=None, captcha_type="recaptcha"):
    """
    Uses CapSolver to solve reCAPTCHA v2, Cloudflare Turnstile, or fall back to
    AntiTurnstileTask when no site_key is available (Cloudflare managed challenge).

    Args:
        site_url (str): URL of the page where the captcha appears.
        site_key (str|None): sitekey if known (for recaptcha/turnstile). May be None.
        captcha_type (str): "recaptcha", "turnstile" or "auto". If "auto", we will
                            prefer recaptcha/turnstile when site_key present, else AntiTurnstileTask.

    Returns:
        str: solved token string.

    Raises:
        Exception on createTask error or timeout.
    """
    print(f"🧩 Starting CAPTCHA solve (type={captcha_type}, has_site_key={bool(site_key)}) via CapSolver...")

    # Normalize captcha_type
    captcha_type = (captcha_type or "recaptcha").lower()

    # Decide which CapSolver task to use
    if captcha_type == "recaptcha" and site_key:
        task = {
            "type": "NoCaptchaTaskProxyless",
            "websiteURL": site_url,
            "websiteKey": site_key
        }
        chosen = "NoCaptchaTaskProxyless (reCAPTCHA v2)"
    elif captcha_type == "turnstile" and site_key:
        task = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": site_url,
            "websiteKey": site_key
        }
        chosen = "TurnstileTaskProxyless (Cloudflare Turnstile)"
    else:
        # Fallback: use AntiTurnstileTask when no sitekey is present or when we couldn't detect
        # This handles Cloudflare managed "just a moment..." challenges.
        task = {
            "type": "AntiTurnstileTask",
            "websiteURL": site_url,
            "websiteKey": site_key if site_key else "no-sitekey"
        }
        chosen = "AntiTurnstileTask (Anti-Cloudflare fallback)"

    print(f"🔧 Creating CapSolver task: {chosen}")

    # Retry loop for robustness
    max_retries = 3
    for attempt_retry in range(1, max_retries + 1):
        try:
            print(f"🚀 Attempt {attempt_retry}/{max_retries} to create task...")
            data = {"clientKey": CAPSOLVER_API_KEY, "task": task}
            create_task_resp = requests.post("https://api.capsolver.com/createTask", json=data, timeout=30)
            create_task = create_task_resp.json()
        except Exception as e:
            print(f"⚠️ CreateTask request failed: {e}")
            if attempt_retry < max_retries:
                time.sleep(5)
                continue
            raise

        if create_task.get("errorId") != 0:
            print(f"❌ CapSolver createTask error: {create_task}")
            if attempt_retry < max_retries:
                time.sleep(5)
                continue
            raise Exception(f"CapSolver createTask error after retries: {create_task}")

        task_id = create_task.get("taskId")
        if not task_id:
            if attempt_retry < max_retries:
                print("⚠️ No taskId returned, retrying...")
                time.sleep(5)
                continue
            raise Exception(f"CapSolver returned no taskId after retries: {create_task}")

        # 2️⃣ Poll result
        max_attempts = 45   # ~90s
        for attempt in range(max_attempts):
            time.sleep(2)
            try:
                result = requests.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
                    timeout=30
                ).json()
            except Exception as e:
                print(f"⚠️ Polling attempt {attempt+1} failed: {e}")
                continue

            status = result.get("status")
            if status == "ready":
                solution = result.get("solution", {})
                token = None
                if "gRecaptchaResponse" in solution:
                    token = solution.get("gRecaptchaResponse")
                elif "token" in solution:
                    token = solution.get("token")
                elif "cfTurnstileResponse" in solution:
                    token = solution.get("cfTurnstileResponse")
                else:
                    for v in solution.values():
                        if isinstance(v, str) and len(v) > 50:
                            token = v
                            break

                if not token:
                    raise Exception(f"CapSolver returned ready but no recognizable token: {solution}")

                print("✅ CAPTCHA solved successfully")
                return token

            if attempt % 5 == 0:
                print(f"⏳ Waiting for solution... attempt {attempt+1}/{max_attempts}")

        # Polling timed out
        print("⌛ Timed out waiting for captcha solution")
        if attempt_retry < max_retries:
            time.sleep(5)
            continue
        raise Exception("❌ CAPTCHA solving timed out after retries")

def handle_captcha(driver, name, is_captcha):
    """
    Detects captcha type (reCAPTCHA / Turnstile / Managed Cloudflare), solves it via
    solve_captcha(), injects the returned token into the appropriate DOM input,
    triggers events, and attempts submission/refresh.

    Keeps the original `if is_captcha:` semantics inside the function as requested.

    Args:
        driver: Selenium WebDriver instance.
        name: show/search name (used for filenames and logs).
        is_captcha: boolean flag (if True, handle captcha; otherwise function returns immediately).

    Returns:
        True  -> CAPTCHA was handled and site progressed (likely solved).
        False -> No captcha handling required OR captcha handling failed (caller should skip or retry).
    """
    if not is_captcha:
        # Caller indicated no captcha to handle; return False to mean "no handling done".
        return False

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

    if "Verifying you are human" in driver.page_source:
        print("⏳ Waiting for Cloudflare verification...")
        time.sleep(6)  # or longer if needed
        try:
            driver.refresh()  # optional
        except Exception as e:
            print(f"⚠️ Refresh failed while waiting for verification: {e}")

    # Try to detect reCAPTCHA sitekey (your existing helper)
    recaptcha_site_key = None
    try:
        recaptcha_site_key = get_recaptcha_site_key(driver)
    except Exception as e:
        print(f"⚠️ get_recaptcha_site_key() error: {e}")
    print(f"🔍 reCAPTCHA site key detection result: {recaptcha_site_key}")

    # Try to detect Turnstile (Cloudflare) sitekey via data-sitekey or script src
    turnstile_site_key = None
    try:
        turnstile_site_key = driver.execute_script(
            "var el = document.querySelector('[data-sitekey]'); return el ? el.getAttribute('data-sitekey') : null;"
        )
        if not turnstile_site_key:
            try:
                scripts = driver.find_elements(By.TAG_NAME, "script")
                for s in scripts:
                    src = s.get_attribute("src") or ""
                    if "sitekey=" in src:
                        turnstile_site_key = src.split("sitekey=")[1].split("&")[0]
                        break
            except Exception as e:
                print(f"⚠️ Error scanning script tags for sitekey: {e}")
    except Exception as e:
        print(f"⚠️ Error extracting Turnstile sitekey via JS: {e}")
        turnstile_site_key = None

    print(f"🔍 Turnstile site key detection result: {turnstile_site_key}")

    # If neither was found, don't immediately skip — we'll attempt AntiTurnstile fallback.
    if not recaptcha_site_key and not turnstile_site_key:
        page_snippet = driver.page_source[:2000].lower()
        if "turnstile" in page_snippet:
            print("ℹ️ Page contains 'turnstile' - likely Cloudflare Turnstile (managed).")
        if "hcaptcha" in page_snippet or "h-captcha" in page_snippet:
            print("ℹ️ Page contains 'hcaptcha' - could be hCaptcha.")
        print("⚠️ No explicit sitekey found — will attempt AntiTurnstileTask fallback (solver).")

    # Determine captcha_type and site_key to pass to solver
    if recaptcha_site_key:
        captcha_type = "recaptcha"
        site_key = recaptcha_site_key
    elif turnstile_site_key:
        captcha_type = "turnstile"
        site_key = turnstile_site_key
    else:
        captcha_type = "auto"   # our solve_captcha() should treat this as falling back to AntiTurnstileTask
        site_key = None

    # If no sitekey found, wait a few seconds for Cloudflare JS to complete
    if not recaptcha_site_key and not turnstile_site_key:
        print("⚠️ No site key found — using AntiTurnstileTask fallback.")

        # Give Cloudflare a chance to clear itself
        time.sleep(10)
        if "just a moment" in driver.title.lower():
            print("⏳ Still stuck on Cloudflare interstitial, sending to solver...")
    
        captcha_type = "auto"
        site_key = None


    # Solve captcha via external solver (solve_captcha must support site_key=None / captcha_type="auto")
    try:
        token = solve_captcha(driver.current_url, site_key, captcha_type=captcha_type)
        if not token:
            raise Exception("solve_captcha returned empty token")
        print(f"🧩 Received token (type={captcha_type}): {token[:80]}...")
    except Exception as e:
        print(f"❌ solve_captcha() failed: {e}")
        try:
            save_debug(driver, name, "captcha_solver_error")
            with open(f"screenshots/{safe_name}_solver_error_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass
        # Return False — caller should treat this as an unsolved captcha (skip/retry)
        return False

    # Snapshot right before injection
    try:
        save_debug(driver, name, "before_injection")
    except Exception as e:
        print(f"⚠️ Failed to save before_injection snapshot: {e}")

    safe_token = token.replace('"', '\\"')

    # Inject token into appropriate input and dispatch events
    try:
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

            try:
                driver.execute_script('document.getElementById("g-recaptcha-response").style.display = "block";')
                driver.execute_script(f'document.getElementById("g-recaptcha-response").value = "{safe_token}";')
                driver.execute_script(
                    'var el = document.getElementById("g-recaptcha-response");'
                    'el.dispatchEvent(new Event("input", { bubbles: true }));'
                    'el.dispatchEvent(new Event("change", { bubbles: true }));'
                )
                print("📝 Injected token into g-recaptcha-response element.")
            except Exception as e:
                print(f"❌ Failed injecting token into g-recaptcha-response: {e}")
                save_debug(driver, name, "inject_failed")
                # injection failed — treat as failure
                return False

            # Try to trigger any grecaptcha callbacks if present (best-effort)
            try:
                driver.execute_script(
                    "if(window.grecaptcha && typeof window.grecaptcha.getResponse === 'function'){ /* noop */ }"
                )
            except Exception:
                pass

        else:
            # Turnstile / AntiTurnstile injection — ensure a fallback hidden input exists
            try:
                driver.execute_script(
                    'if(!document.querySelector(\'input[name="cf-turnstile-response"]\')){'
                    '  var t=document.createElement("input"); t.type="hidden"; '
                    '  t.name="cf-turnstile-response"; t.id="cf-turnstile-response"; '
                    '  document.body.appendChild(t); }'
                )
            except Exception as e:
                print(f"⚠️ Could not ensure cf-turnstile-response input exists: {e}")

            # Attempt to set the token into common Turnstile locations and dispatch events
            try:
                driver.execute_script(
                    'var token = arguments[0];'
                    'var el = document.querySelector(\'input[name="cf-turnstile-response"]\') || '
                    '         document.getElementById("cf-turnstile-response") || '
                    '         (function(){'
                    '            var els = document.querySelectorAll("[id$=\"_response\"]");'
                    '            for(var i=0;i<els.length;i++){ if(els[i].id.indexOf("cf-chl-widget")!==-1) return els[i]; }'
                    '            return null;'
                    '         })();'
                    'if(el) { el.value = token; el.dispatchEvent(new Event("input",{bubbles:true})); el.dispatchEvent(new Event("change",{bubbles:true})); }',
                    safe_token
                )
                print("📝 Injected token into Turnstile response field (attempted multiple selectors).")
            except Exception as e:
                print(f"❌ Failed injecting token into Turnstile response fields: {e}")
                save_debug(driver, name, "inject_failed")
                return False

            # Optionally attempt to trigger Turnstile callback (best-effort)
            try:
                driver.execute_script('if(window.turnstile && window.turnstile.execute) { /* noop */ }')
            except Exception:
                pass

    except Exception as e:
        print(f"❌ Unexpected error during injection: {e}")
        save_debug(driver, name, "inject_exception")
        return False

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
                try:
                    driver.refresh()
                    print("🔁 Page refreshed after failed submit.")
                except Exception as e2:
                    print(f"⚠️ Failed to refresh after submit failure: {e2}")
        else:
            # fallback: reload page so server side can validate the injected token
            try:
                driver.refresh()
                print("🔁 Page refreshed after injection.")
            except Exception as e:
                print(f"⚠️ Failed to refresh page after injection: {e}")
    except Exception as e:
        print(f"⚠️ Error when checking/submitting captcha-form or refreshing: {e}")

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

    # FINAL VERIFICATION: check if captcha indicators are still present
    try:
        still_captcha = is_captcha_page(driver, name)
        if still_captcha:
            print("❌ CAPTCHA still present after handling — treating as failure.")
            return False
        else:
            print("✅ CAPTCHA appears cleared / site progressed.")
            return True
    except Exception as e:
        print(f"⚠️ Error during final captcha verification: {e}")
        # If the verification itself failed, be conservative and return False
        return False

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
                    solved = handle_captcha(driver, name, True)
                    if not solved:
                        print(f"⚠️ Skipping '{name}' because CAPTCHA could not be solved.")
                        continue  # skip this show
                else:
                    print(f"ℹ️ No CAPTCHA detected for '{name}'")

                
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