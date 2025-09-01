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

# Check if current page is a CAPTCHA page
def is_captcha_page(driver, show_name="unknown"):
    try:
        # Quick check: Cloudflare interstitial
        if "Just a moment" in driver.title or "cf-challenge" in driver.page_source:
            print(f"⚠️ CAPTCHA/Cloudflare detected immediately for '{show_name}'")
            print("ℹ️ Page title:", driver.title)
            print("ℹ️ First 500 chars of HTML:", driver.page_source[:500])
            save_debug(driver, show_name, "captcha")
            return True

        # Try to wait for explicit recaptcha/challenge elements
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//iframe[contains(@src,'recaptcha')] | "
                 "//div[contains(@class,'cf-challenge')] | "
                 "//div[contains(@class,'g-recaptcha')]")
            )
        )
        print(f"⚠️ CAPTCHA elements detected for '{show_name}'")
        print("ℹ️ Page title:", driver.title)
        print("ℹ️ First 500 chars of HTML:", driver.page_source[:500])
        save_debug(driver, show_name, "captcha")
        return True

    except TimeoutException:
        print(f"ℹ️ No CAPTCHA detected for '{show_name}'")
        return False

def save_debug(driver, show_name, suffix):
    safe_name = show_name.replace(" ", "_").replace("/", "_")
    os.makedirs("screenshots", exist_ok=True)
    path = f"screenshots/{safe_name}_{suffix}_{int(time.time())}.png"
    driver.save_screenshot(path)
    print(f"📸 Screenshot saved: {path}")

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
def solve_captcha(site_url, site_key):
    """
    Uses CapSolver to solve reCAPTCHA v2 and return the token.
    """
    print("🧩 Solving CAPTCHA via CapSolver...")

    data = {
        "clientKey": CAPSOLVER_API_KEY,
        "task": {
            "type": "NoCaptchaTaskProxyless",
            "websiteURL": site_url,
            "websiteKey": site_key
        }
    }

    # 1️⃣ Create Task
    create_task = requests.post("https://api.capsolver.com/createTask", json=data).json()
    if create_task.get("errorId") != 0:
        raise Exception(f"CapSolver createTask error: {create_task}")

    task_id = create_task["taskId"]

    # 2️⃣ Poll result
    for _ in range(30):  # 30 attempts ~ 60s
        time.sleep(2)
        result = requests.post("https://api.capsolver.com/getTaskResult", json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}).json()
        if result.get("status") == "ready":
            token = result["solution"]["gRecaptchaResponse"]
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

# Extract show data from the current page
def extract_shows(driver):
    shows = []
    # Wait until at least one show is present
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a.show"))
    )
    
    show_elements = driver.find_elements(By.CSS_SELECTOR, "a.show")
    
    for el in show_elements:
        try:
            show = {}
            show['url'] = el.get_attribute("href")
            show['name'] = el.find_element(By.CSS_SELECTOR, "h2").text.strip()
            show['hall'] = el.find_element(By.CSS_SELECTOR, ".theater_container").text.strip()

            raw_date = el.find_element(By.CSS_SELECTOR, ".date_container").text.strip()
            if raw_date:
                show['date'] = parse_hebrew_date(raw_date)
            else:
                show['date'] = ""

            raw_time = el.find_element(By.CSS_SELECTOR, ".time_container").text.strip()
            # Remove any non-digit/colon prefix
            import re
            time_match = re.search(r"(\d{1,2}:\d{2})", raw_time)
            show['time'] = time_match.group(1) if time_match else ""

            # Thumbnail image
            img_el = el.find_element(By.CSS_SELECTOR, ".pic img")
            show['thumbnail'] = img_el.get_attribute("src")
            
            shows.append(show)
        except Exception as e:
            print(f"⚠️ Error extracting a show: {e}")
            continue
    
    print(f"✅ Extracted {len(shows)} shows from page")
    return shows

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
                time.sleep(2)  # wait a bit for page to start loading

                is_captcha = is_captcha_page(driver, name)

                if is_captcha:
                    driver.save_screenshot("captcha.png")

                    # Even though you have an API key, skip solving until you have a balance
                    print(f"⚠️ CAPTCHA detected on {sheet_tab}, skipping until API key has funds")
                    print("ℹ️ First 500 chars of HTML after detection:", driver.page_source[:500])
                    continue  # skip this search term

                    site_key = get_recaptcha_site_key(driver)
                    if site_key:
                        token = solve_captcha(driver.current_url, site_key)
                        # Inject token into page
                        driver.execute_script(f'document.getElementById("g-recaptcha-response").innerHTML = "{token}";')
                        driver.execute_script('___grecaptcha_cfg.clients[0].callback("{token}");')  # trigger callback if needed
                        time.sleep(2)
                    else:
                        print("❌ Could not detect site key, skipping CAPTCHA")
                        return
                
                print(f"✅ Finished search for: {name}")
                print("🌍 Current URL:", driver.current_url)

                all_shows = extract_shows(driver)
                print(f"ℹ️ Extracted {len(all_shows)} shows for {name}")
                for s in all_shows:
                    print(s)


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