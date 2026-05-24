# Rent Scout

Mobile-first Rent.com lead scraper. Pick any US city, any max price, any bed count → get a table of leads with property name, address, price, and phone number. Tap "Save CSV" to download.

## Why this one is easier than Maps Scout

Rent.com supports **filters as URL parameters**, so the scraper just hits a direct URL and parses cards. No form filling, no CDP, no real Chrome — headless Playwright handles it fine. That means it deploys to Render free tier with no fuss.

## Stack
- Python + Flask backend
- Playwright (headless Chromium) to load Rent.com SRPs
- BeautifulSoup not needed — Playwright's locators handle all extraction
- 9:16 mobile UI with results table, CSV export, pause/resume/stop

## Run locally
```bash
pip install -r requirements.txt
playwright install chromium
python app.py
# open http://localhost:5000
```

## Deploy to Render (same process as Maps Scout)
1. Push to GitHub
2. Render → New Web Service → connect repo
3. Auto-detects Dockerfile, ~5 min first build
4. Open URL on phone, Add to Home Screen

## Inputs
- **City** — any US city (e.g. "Raleigh", "Baton Rouge", "Fort Worth")
- **State** — 2-letter code (NC, LA, TX, etc.) — full state list in `STATE_SLUGS` in `app.py`
- **Bedrooms** — 1, 2, 3, or 4
- **Max rent** — any positive dollar amount
- **Page limit** — capped to keep scrapes short on free tier (~15s per page)

## What it extracts per listing
- Property name
- Address
- Price (matched to your bed count)
- Phone (from card — no per-listing visit)
- URL (links to Rent.com detail page)
- Scrape timestamp

## Notes
- Rent.com returns ~30 listings per page. Most cities have 1-10 pages.
- ~70-90% of listings have a phone number visible on the card.
- The Apps Script CSV upload workflow from your original system still works — the export CSV format includes `property_name`, `address`, `phone`, `price`, etc. Just map the columns.
- Listings without a phone are still shown in the table but greyed out.
