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
from urllib.parse import quote
from urllib.parse import urlparse, parse_qs
import requests
from datetime import datetime
import pytz
import re

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

def get_appsheet_data(table_name):
    """Generic function to read a table from AppSheet, handling nested dictionary response."""
    app_id = os.environ.get("APPSHEET_APP_ID")
    app_key = os.environ.get("APPSHEET_APP_KEY")
    url = f"https://api.appsheet.com/api/v1/apps/{app_id}/tables/{table_name}/Action"
    
    headers = {"ApplicationAccessKey": app_key, "Content-Type": "application/json"}
    body = {
        "Action": "Find",
        "Properties": {"Locale": "en-US"},
        "Rows": []
    }
    
    try:
        response = requests.post(url, json=body, headers=headers, timeout=30)
        
        if response.status_code != 200:
            print(f"❌ AppSheet API Error ({table_name}) HTTP {response.status_code}: {response.text}")
            return []

        data = response.json()

        # Logic Fix: AppSheet usually returns a list of rows, 
        # but sometimes a dict with a 'Rows' or 'RowValues' key.
        if isinstance(data, list):
            return data
        
        if isinstance(data, dict):
            # Check common keys where AppSheet stores the actual data
            rows = data.get("Rows") or data.get("RowValues")
            if rows is not None:
                return rows
            
            # If the dict is empty or success but no rows found
            if data.get("Success") == True:
                print(f"ℹ️ AppSheet connected to '{table_name}', but returned 0 rows.")
                return []

        print(f"⚠️ Unexpected AppSheet format: {data}")
        return []

    except Exception as e:
        print(f"❌ Connection Error fetching {table_name}: {e}")
        return []
    
def get_short_names():
    """Uses the generic AppSheet fetcher to get show names."""
    print("⏳ Fetching show names from AppSheet 'הפקות' table...")
    rows = get_appsheet_data("הפקות")
    
    if not rows:
        print("⚠️ No data returned from AppSheet 'הפקות'.")
        return []
    
    # Standardize loop to ensure we are looking at dictionaries
    short_names = []
    for row in rows:
        if isinstance(row, dict) and row.get("שם מקוצר"):
            short_names.append(row["שם מקוצר"])
    
    print(f"🔎 Found {len(short_names)} names to search.")
    return short_names

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

def find_turnstile_sitekey(driver, verbose=True):
    """
    Try multiple strategies to discover a Cloudflare Turnstile sitekey on the page.
    Returns sitekey string if found, else None.
    Strategies:
      - iframe[src*="turnstile"] query param k=
      - elements with data-sitekey attribute
      - search page_source for Turnstile-like keys (start with '0x')
      - inspect inline <script> tags text for '0x...' occurrences
      - check common window variables via execute_script
    """
    try:
        # 1) iframe[src*="turnstile"] -> ?k=SITEKEY
        try:
            iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='turnstile']")
            src = iframe.get_attribute("src") or ""
            if "k=" in src:
                parsed = urlparse(src)
                q = parse_qs(parsed.query)
                site_key = q.get("k", [None])[0]
                if site_key:
                    if verbose: print(f"🧩 sitekey from turnstile iframe src: {site_key}")
                    return site_key
        except Exception:
            pass

        # 2) any element with data-sitekey
        try:
            el = driver.find_element(By.CSS_SELECTOR, "[data-sitekey]")
            site_key = el.get_attribute("data-sitekey")
            if site_key:
                if verbose: print(f"🧩 sitekey from data-sitekey attribute: {site_key}")
                return site_key
        except Exception:
            pass

        # 3) search whole page_source for a Turnstile-looking key (Turnstile keys often start with "0x")
        try:
            src = driver.page_source
            # pattern: 0x followed by at least 10 chars (alphanumeric)
            m = re.search(r"(0x[a-zA-Z0-9_\-]{8,60})", src)
            if m:
                site_key = m.group(1)
                if verbose: print(f"🧩 sitekey found in page_source (regex): {site_key}")
                return site_key
        except Exception:
            pass

        # 4) search inline script tags text
        try:
            scripts = driver.find_elements(By.TAG_NAME, "script")
            for s in scripts:
                try:
                    txt = s.get_attribute("innerText") or ""
                    m = re.search(r"(0x[a-zA-Z0-9_\-]{8,60})", txt)
                    if m:
                        site_key = m.group(1)
                        if verbose: print(f"🧩 sitekey found inside <script>: {site_key}")
                        return site_key
                except Exception:
                    continue
        except Exception:
            pass

        # 5) attempt some common JS variables (Friends or sites sometimes expose it)
        try:
            js_try = """
                return window.turnstileSiteKey
                    || window.turnstile_site_key
                    || window.__cf_turnstile_sitekey
                    || window.Turnstile?.siteKey
                    || (document.querySelector('[data-sitekey]') && document.querySelector('[data-sitekey]').getAttribute('data-sitekey'))
                    || null;
            """
            site_key = driver.execute_script(js_try)
            if site_key:
                if verbose: print(f"🧩 sitekey obtained from window var: {site_key}")
                return site_key
        except Exception:
            pass

        # nothing found
        if verbose: print("⚠️ No sitekey found with heuristics")
        return None

    except Exception as e:
        if verbose: print(f"⚠️ find_turnstile_sitekey error: {e}")
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
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": site_url,
            "websiteKey": site_key
        }
        chosen = "AntiTurnstileTaskProxyLess (Cloudflare Turnstile with sitekey)"
    elif captcha_type == "cloudflare_challenge": # <--- NEW BLOCK
        task = {
            "type": "AntiCloudflareTaskProxyLess", # <--- Use the general challenge bypass task
            "websiteURL": site_url
            # NO websiteKey required for this task type
        }
        chosen = "AntiCloudflareTaskProxyLess (general Cloudflare Challenge)"
    elif captcha_type == "anti_turnstile":
        # task = {
        #     "type": "AntiTurnstileTaskProxyLess",
        #     "websiteURL": site_url
        # }
        raise Exception("❌ The 'anti_turnstile' task type requires a websiteKey in the current CapSolver version.")
    else:
        raise Exception("❌ No valid captcha_type or site_key detected for CapSolver")


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
    Improved handler:
      - Wait for Turnstile iframe or data-sitekey
      - Try many heuristics to extract a Turnstile sitekey (find_turnstile_sitekey)
      - If a sitekey is found -> call solve_captcha(..., captcha_type='turnstile')
      - If not found -> save debug (html + images) and optionally ask for manual solve
    """
    if not is_captcha:
        return False

    try:
        site_url = driver.current_url

        # 1) quick wait for any sign of Turnstile or captcha
        try:
            WebDriverWait(driver, 8).until(
                lambda d: d.find_element(By.CSS_SELECTOR, "iframe[src*='turnstile'], [data-sitekey], iframe[src*='recaptcha'], .g-recaptcha"), 
            )
        except Exception:
            # we continue — page might still have dynamic JS that sets sitekey, so proceed
            pass

        # 2) try to extract sitekey using helper (search scripts, DOM, window vars)
        site_key = find_turnstile_sitekey(driver, verbose=True)

        # Determine the best solving strategy
        page_title = driver.title.lower()

        if "just a moment..." in page_title:
            # This is the full-page challenge, which often does not need a sitekey
            captcha_type = "cloudflare_challenge"
            site_key = None # Explicitly discard any potential false positive sitekey
            print("✅ Full-page 'just a moment...' detected. Using general CapSolver Cloudflare Challenge task.")
            
        elif site_key:
            # If a sitekey was reliably found (e.g., not the regex false positive)
            captcha_type = "turnstile"
            print("✅ Turnstile sitekey found. Using Turnstile task with sitekey.")
        else:
            # Fallback to anti_turnstile if no reliable key was found
            captcha_type = "cloudflare_challenge"
            print("⚠️ No sitekey found, falling back to general Cloudflare Challenge task.")

        # 3) call solve_captcha. If site_key is None and solver expects a websiteKey, solve_captcha may raise.
        token = solve_captcha(site_url, site_key, captcha_type=captcha_type)
        print("✅ Got CAPTCHA token:", token[:40], "...")

        # 4) inject token (Turnstile uses cf-turnstile-response)
        try:
            driver.execute_script("""
                (function(token){
                    var el = document.querySelector('input[name="cf-turnstile-response"]');
                    if (!el) {
                        el = document.createElement('input');
                        el.type = 'hidden';
                        el.name = 'cf-turnstile-response';
                        // attach to first form or body
                        var f = document.forms[0] || document.body;
                        f.appendChild(el);
                    }
                    el.value = token;
                    el.dispatchEvent(new Event("input", {bubbles:true}));
                    el.dispatchEvent(new Event("change", {bubbles:true}));
                })(arguments[0]);
            """, token)
        except Exception as e:
            print("⚠️ Failed to inject token via cf-turnstile-response:", e)

        save_debug(driver, name, "after_inject")
        time.sleep(5)
        return True

    except Exception as e:
        print("❌ Captcha handling failed:", str(e))
        save_debug(driver, name, "captcha_fail")
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

def update_appsheet_batch(shows, site_tab):
    """Matches scraped shows to AppSheet rows using ID and updates them."""
    app_id = os.environ.get("APPSHEET_APP_ID")
    app_key = os.environ.get("APPSHEET_APP_KEY")
    
    # 1. Fetch current data to find the IDs
    print("⏳ Fetching current AppSheet data to match IDs...")
    current_rows = get_appsheet_data("כרטיסים")
    
    israel_tz = pytz.timezone("Asia/Jerusalem")
    now_israel = datetime.now(israel_tz).strftime('%d/%m/%Y %H:%M')
    org_value = "סמארטיקט" if site_tab == "Papi" else "פרינדס"

    updates = []

    for show in shows:
        try:
            scraped_date_obj = datetime.strptime(show["date"], "%d/%m/%Y").date()
        except:
            continue
        # scraped_date = show["date"] # Assuming format 'DD/MM/YYYY'
        scraped_name = show["name"].strip()

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

            # Match by Name, Date, and Organization
            if (scraped_name in row_name or row_name in scraped_name) and \
               app_date_obj == scraped_date_obj and \
               row.get("ארגון") == org_value:
                match = row
                break
        
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
        url = f"https://api.appsheet.com/api/v1/apps/{app_id}/tables/כרטיסים/Action"
        body = {
            "Action": "Edit",
            "Properties": {"Locale": "en-US"},
            "Rows": updates
        }
        resp = requests.post(url, json=body, headers={"ApplicationAccessKey": app_key})
        print(f"🚀 AppSheet Batch Update Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"❌ AppSheet Update Error: {resp.text}")
    else:
        print("❌ No matching rows found in AppSheet.")


def scrape_site(site_config):
    base_url = site_config["base_url"]
    sheet_tab = site_config["sheet_tab"]
    print(f"🌐 Starting scraper for site: {sheet_tab} ({base_url})")

    driver = get_driver()
    
    # 1. This list must be outside the loops to collect EVERYTHING
    all_shows_to_update = []

    try:
        # Load show names from Google Sheets
        short_names = get_short_names()
        print(f"🔎 Loaded {len(short_names)} short names")
        
        for name in short_names:
            print(f"➡️ Searching for: {name}")

            # Encode the show name for the URL
            search_url = f"{base_url}search?q={quote(name)}"

            try:
                driver.get(search_url)

                # Check for CAPTCHA
                is_captcha = is_captcha_page(driver, name)
                if is_captcha:
                    solved = handle_captcha(driver, name, True)
                    if not solved:
                        print(f"⚠️ Skipping '{name}' because CAPTCHA could not be solved.")
                        continue 
                else:
                    print(f"ℹ️ No CAPTCHA detected for '{name}'")

                print(f"✅ Finished search for: {name}")
                print("🌍 Current URL:", driver.current_url)
                
                # 2. Get URLs with a safety check so it doesn't crash if 0 shows are found
                urls = get_show_urls(driver)
                
                # 3. Process each URL found for this specific search name
                for url in urls:
                    show = extract_show_details(driver, url)
                    if not show.get("name"): # Skip if extraction failed
                        continue

                    try:
                        select_area(driver)
                        available = count_empty_seats(driver)
                        show["available_seats"] = available
                        
                        # Add the individual show results to our master list
                        all_shows_to_update.append(show)
                        print(f"🎫 Available seats for {show['name']} on {show['date']}: {available}")
                        
                    except Exception as seat_e:
                        print(f"❌ Error counting seats for {show.get('name','?')}: {seat_e}")
                        show["available_seats"] = None

            except Exception as inner_e:
                print(f"❌ Error during search/process for '{name}': {inner_e}")
                save_debug(driver, name, "error_loop")

        # 4. CRITICAL: The update happens AFTER all searches are finished
        if all_shows_to_update:
            print(f"🚀 Found {len(all_shows_to_update)} total shows. Starting Batch Update to Google Sheets...")
            update_appsheet_batch(all_shows_to_update, sheet_tab)
        else:
            print("❌ No show data collected. Nothing to update.")

    finally:
        print("🏁 Scraper finished. Closing browser.")
        driver.quit()

# Run daily scrapers
for site in [ "papi"]:
    scrape_site(SITES[site])

