from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import time
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

SITES = {
    "friends": {
        "base_url": "https://friends.smarticket.co.il/",
        "sheet_tab": "Friends"
    },
    "papi": {
        "base_url": "https://papi.smarticket.co.il/",
        "sheet_tab": "Papi"
    },}


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
    options.add_argument("--headless=new")  # ✅ use headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def scrape_site(site_config):
    base_url = site_config["base_url"]
    driver = get_driver()
    driver.get(base_url + "search")  # directly open the search page

    try:
        # Wait for search link and click it (backup if direct URL fails)
        # search_link = WebDriverWait(driver, 10).until(
        #     EC.element_to_be_clickable((By.ID, "search-link"))
        # )
        # search_link.click()

        # Load show names from Google Sheets
        short_names = get_short_names()
        print(f"🔎 Loaded {len(short_names)} short names")

        for name in short_names[:5]:  # just test first 5 for now
            print(f"➡️ Searching for: {name}")

            # Find the input
            search_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "q"))
            )
            # Clear old value and type new one
            search_input.clear()
            search_input.send_keys(name)

            # Submit by pressing Enter
            search_input.send_keys(Keys.RETURN)

            # Wait for results to load (TODO: find result container)
            time.sleep(2)

            print(f"✅ Finished search for: {name}")
            print("🌍 Current URL:", driver.current_url)

            # Optionally, navigate back to search page for next query
            driver.get(base_url + "search")

    except Exception as e:
        print(f"❌ Error while scraping {base_url}: {e}")
    finally:
        driver.quit()

# Run daily scrapers
for site in ["friends", "papi"]:
    scrape_site(SITES[site])