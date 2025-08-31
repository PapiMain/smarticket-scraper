from scraper.smarticket_scraper import scrape_site
from scraper.config import SITES

# Run daily scrapers
for site in ["friends", "papi"]:
    scrape_site(SITES[site])
