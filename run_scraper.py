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

SITES = {
    "friends": {
        "base_url": "https://friends.smarticket.co.il/",
        "sheet_tab": "Friends"
    },
    "papi": {
        "base_url": "https://papi.smarticket.co.il/",
        "sheet_tab": "Papi"
    },}

# CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY")  # store your CapSolver API key in env variable

def get_short_names():
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
    client = gspread.authorize(creds)
    

    sheet = client.open("דאטה אפשיט אופיס").worksheet("הפקות")
    short_names = sheet.col_values(2)  # for example, if "שם מקוצר" is column B
    return [name for name in short_names if name and name != "שם מקוצר"]

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

def is_captcha_page(driver):
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src,'recaptcha')] | //div[contains(@class,'cf-challenge')]"))
        )
        print("⚠️ CAPTCHA detected!")
        return True
    except TimeoutException:
        return False

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

                if is_captcha_page(driver):
                    driver.save_screenshot("captcha.png")
                    
                    # Even though you have an API key, skip solving until you have a balance
                    print(f"⚠️ CAPTCHA detected on {sheet_tab}, skipping until API key has funds")
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

                # Wait until at least one show <a> is present
                # WebDriverWait(driver, 10).until(
                #     EC.presence_of_element_located((By.CSS_SELECTOR, "a.show.event"))
                # )

                print(f"✅ Finished search for: {name}")
                print("🌍 Current URL:", driver.current_url)

            except Exception as inner_e:
                print(f"❌ Error on show '{name}': {inner_e}")
                # Save a screenshot with the show name
                safe_name = name.replace(" ", "_").replace("/", "_")
                screenshot_path = f"screenshots/{sheet_tab}_{safe_name}.png"
                os.makedirs("screenshots", exist_ok=True)
                driver.save_screenshot(screenshot_path)
                print(f"📸 Screenshot saved to: {screenshot_path}")

    except Exception as e:
        print(f"❌ Error while scraping {base_url}: {e}")
    finally:
        driver.quit()

# Run daily scrapers
for site in ["friends", "papi"]:
    scrape_site(SITES[site])