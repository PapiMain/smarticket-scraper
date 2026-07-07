"""
Standalone smoke test for the DataImpulse Israeli residential proxy, routed
through the local authenticating relay (see proxy_relay.py).

Why the relay: SeleniumBase's authenticated-proxy support uses an MV3 service
worker that UC mode races, so Chrome shows a native auth popup and pages hang.
The relay presents Chrome an *unauthenticated* 127.0.0.1 proxy and injects the
upstream credentials itself — no popup, UC-mode safe.

Checks:
  1. Opens https://ipinfo.io/ip through the relay and prints the exit IP +
     confirms it's Israeli (via ipinfo.io/json country == "IL").
  2. Navigates to the Cloudflare-protected hall (tickets.friends-hist.co.il) and
     runs run_scraper's clear_cloudflare() logic — confirms the challenge clears
     from the residential IP and that block_images didn't break the solve.

Run locally (Windows):
    set PYTHONUTF8=1
    python test_proxy.py

Requires PROXY_USERNAME / PROXY_PASSWORD in your local .env.
"""

import json

from dotenv import load_dotenv
from seleniumbase import Driver
from selenium.webdriver.common.by import By

from proxy_relay import start_proxy_relay
# Reuse the real scraper's Cloudflare helpers so we test the real code path.
from run_scraper import is_cloudflare_challenge, clear_cloudflare

load_dotenv()

CLOUDFLARE_TEST_URL = "https://tickets.friends-hist.co.il/"


def get_proxied_driver(proxy_address):
    """
    Mirror the proxied-driver config that will live in run_scraper.get_driver():
      - proxy points at the LOCAL relay (no auth) → no native popup.
      - incognito=False so nothing extension-related is stripped.
      - block_images=True to cut residential-proxy data usage.
    """
    return Driver(
        browser="chrome",
        uc=True,
        headless=False,
        no_sandbox=True,
        disable_gpu=True,
        incognito=False,
        proxy=proxy_address,
        block_images=True,
    )


def check_exit_ip(driver):
    print("\n=== CHECK 1: proxy exit IP ===")
    driver.get("https://ipinfo.io/json")
    body = driver.find_element(By.TAG_NAME, "body").text.strip()
    try:
        info = json.loads(body)
    except json.JSONDecodeError:
        print("❌ Could not parse ipinfo.io response. Raw body:")
        print(repr(body[:500]))
        return False

    print(f"   IP:      {info.get('ip', '?')}")
    print(f"   Country: {info.get('country', '?')}")
    print(f"   City:    {info.get('city', '?')}")
    print(f"   Org:     {info.get('org', '?')}")
    if info.get("country") == "IL":
        print("✅ Exit IP is Israeli (IL).")
        return True
    print(f"⚠️ Exit country is '{info.get('country')}', expected 'IL'.")
    return False


def check_cloudflare(driver):
    print("\n=== CHECK 2: Cloudflare challenge on blocked hall ===")
    print(f"   Navigating to {CLOUDFLARE_TEST_URL}")
    driver.uc_open_with_reconnect(CLOUDFLARE_TEST_URL, reconnect_time=10)

    if is_cloudflare_challenge(driver):
        print("   Cloudflare interstitial detected — attempting bypass...")
    else:
        print("   No interstitial on first load.")

    cleared = clear_cloudflare(driver)
    try:
        title = driver.title
    except Exception:
        title = "?"
    src_len = len(driver.page_source or "")
    print(f"   Final page title: {title!r}  (page_source len={src_len})")

    # A real cleared page has substantial content; a blank/stalled page does not.
    if cleared and not is_cloudflare_challenge(driver) and src_len > 500:
        print("✅ Cloudflare cleared and real content loaded.")
        return True
    print("❌ Page did not load real content (still challenged or blank).")
    return False


def main():
    proxy_address = start_proxy_relay()
    if not proxy_address:
        print("❌ PROXY_USERNAME / PROXY_PASSWORD not set. Add them to .env and retry.")
        return
    print(f"🔌 Local relay listening at {proxy_address} → forwarding to DataImpulse")

    driver = get_proxied_driver(proxy_address)
    try:
        ip_ok = check_exit_ip(driver)
        cf_ok = check_cloudflare(driver)
    finally:
        print("\n🏁 Closing browser.")
        driver.quit()

    print("\n=== RESULT ===")
    print(f"   Proxy IP is Israeli:  {'✅' if ip_ok else '❌'}")
    print(f"   Cloudflare cleared:   {'✅' if cf_ok else '❌'}")
    if ip_ok and cf_ok:
        print("🎉 All good — safe to wire the relay into run_scraper.py.")
    else:
        print("⚠️ Fix the failing check before integrating.")


if __name__ == "__main__":
    main()
