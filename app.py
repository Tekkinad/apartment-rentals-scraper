"""
rent-scout — flexible Rent.com lead scraper.

User picks any US city, max price, and bed count. Backend hits Rent.com's
direct filtered URL (no clicking / form filling needed — Rent.com supports
filters as URL params). Phone number is on the card itself, no per-listing
visits needed.

Compared to the original `rent_scraper.py`:
  - Removes CDP dependency (real Chrome) so it can deploy headless on Render
  - Removes hardcoded MARKETS dict — user supplies city + state per request
  - Removes per-city CSV — results live in-memory and download on demand
  - Adds pause/resume/stop controls (battle-tested pattern from Maps Scout)
"""

import csv
import io
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Rent.com uses lowercase state names in URLs (e.g. "north-carolina").
# This map handles the cases where the slug isn't a simple lowercase.
STATE_SLUGS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina",
    "SD": "south-dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district-of-columbia",
}

BED_PARAMS = {1: "1BR", 2: "2BR", 3: "3BR", 4: "4BR"}


# ---------- Job helpers ----------

def make_job(query_label):
    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "query": query_label,
            "status": "queued",
            "stage": "Waiting to start...",
            "scraped": 0,
            "total": 0,
            "results": [],
            "error": None,
            "started_at": time.time(),
            "paused": False,
            "stopped": False,
        }
    return job_id


def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def format_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw


def build_url_candidates(city, state, beds, max_price, page=1):
    """Rent.com has changed URL formats over time. Return all known URL
    patterns to try in order; the scraper will use whichever one returns
    actual listing cards.

    Known patterns seen in the wild:
      /<state-full>/<city>-apartments          (older, may redirect now)
      /<state-abbrev>/<city>                    (newer)
      /apartments/<state-abbrev>/<city>         (alt newer)
    """
    state_full = STATE_SLUGS.get(state.upper(), state.lower().replace(" ", "-"))
    state_ab = state.lower()
    city_slug = city.lower().strip().replace(",", "").replace(" ", "-")
    bed_param = BED_PARAMS.get(int(beds), "1BR")
    page_part_dash = f"/page-{page}" if page > 1 else ""
    query = f"?min_price=0&max_price={int(max_price)}&bedrooms={bed_param}"

    return [
        # Newer format: state abbreviation, no "-apartments" suffix
        f"https://www.rent.com/{state_ab}/{city_slug}{page_part_dash}{query}",
        # Original format: state full name with "-apartments"
        f"https://www.rent.com/{state_full}/{city_slug}-apartments{page_part_dash}{query}",
        # Alternative: /apartments/ prefix
        f"https://www.rent.com/apartments/{state_ab}/{city_slug}{page_part_dash}{query}",
        # No "-apartments" with full state name
        f"https://www.rent.com/{state_full}/{city_slug}{page_part_dash}{query}",
    ]


def build_url(city, state, beds, max_price, page=1):
    """Backward-compatible single-URL builder (returns the first candidate)."""
    return build_url_candidates(city, state, beds, max_price, page)[0]


# ---------- Scraper core ----------

def scrape_rent(job_id, city, state, beds, max_price, max_pages=None):
    """Wrapper that catches crashes and writes them to the job."""
    try:
        _scrape_rent_inner(job_id, city, state, beds, max_price, max_pages)
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_job(
            job_id,
            status="error",
            stage="Scraper crashed",
            error=f"{type(e).__name__}: {e}",
        )


def _scrape_rent_inner(job_id, city, state, beds, max_price, max_pages):
    def log(msg):
        update_job(job_id, stage=msg)
        print(f"[{job_id}] {msg}")

    def check_pause_or_stop():
        """Return 'stop' to break out, 'continue' to keep going."""
        j = get_job(job_id) or {}
        if j.get("stopped"):
            return "stop"
        if j.get("paused"):
            update_job(job_id, status="paused", stage="Paused — tap Resume to continue")
            while True:
                time.sleep(1)
                j = get_job(job_id) or {}
                if j.get("stopped"):
                    return "stop"
                if not j.get("paused"):
                    update_job(job_id, status="running")
                    log("Resuming…")
                    return "continue"
        return "continue"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        # Block heavy assets to save RAM on Render free tier.
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )
        page = context.new_page()
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        results = []
        seen_urls = set()
        page_num = 1
        total_pages = 1
        url_pattern_idx = None  # which build_url_candidates index worked

        log("Opening Rent.com…")

        while True:
            if check_pause_or_stop() == "stop":
                log("Stopped by user.")
                break

            # Build candidate URLs for this page. On page 1 we try ALL patterns;
            # on subsequent pages we use only the one that worked on page 1.
            candidates = build_url_candidates(city, state, beds, max_price, page_num)
            if url_pattern_idx is not None:
                candidates = [candidates[url_pattern_idx]]

            page_loaded_url = None
            for idx, candidate_url in enumerate(candidates):
                log(f"Page {page_num}: trying URL #{idx + 1}…")
                try:
                    page.goto(candidate_url, timeout=45_000, wait_until="domcontentloaded")
                except Exception as e:
                    log(f"  URL #{idx + 1} failed to load: {str(e)[:60]}")
                    continue

                # Did Rent.com redirect us off the city page to a state landing?
                final_url = page.url
                if final_url.rstrip("/") in (
                    f"https://www.rent.com/{state.lower()}",
                    f"https://www.rent.com/{STATE_SLUGS.get(state.upper(), '')}",
                ):
                    log(f"  URL #{idx + 1} redirected to state landing — bad city slug")
                    continue
                if final_url == "https://www.rent.com/" or final_url == "https://www.rent.com":
                    log(f"  URL #{idx + 1} redirected to homepage")
                    continue

                # Looks promising — see if cards appear
                page_loaded_url = candidate_url
                if page_num == 1:
                    url_pattern_idx = idx
                    log(f"  URL #{idx + 1} accepted: {final_url[:80]}")
                break

            if not page_loaded_url:
                log(
                    f"All URL patterns failed for {city}, {state.upper()}. "
                    f"Rent.com may not list this city, or the URL format has changed again."
                )
                if page_num == 1:
                    update_job(
                        job_id,
                        error=f"Could not find a working Rent.com URL for {city}, {state.upper()}. Try a bigger city name or check spelling.",
                    )
                break

            url = page_loaded_url

            # Wait for cards. Rent.com markup changes — try several known selectors.
            time.sleep(random.uniform(1.5, 2.5))

            # Multiple selectors Rent.com has used / may be using now
            CARD_SELECTORS = [
                'li[data-tid^="srp_card_"]',         # original
                'div[data-tid^="srp_card_"]',        # if they switched tag
                '[data-testid^="srp-card"]',         # alt naming
                'article[data-tid*="card"]',         # tag-shift
                'a[data-tid="pdp-link"]',            # the link inside every card
                '[data-tid="listing-card"]',         # generic name
                'div[data-component="ListingCard"]', # React component name
            ]

            working_selector = None
            for sel in CARD_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=4000)
                    count = page.locator(sel).count()
                    if count > 0:
                        working_selector = sel
                        log(f"Found {count} cards using selector: {sel}")
                        break
                except Exception:
                    continue

            if not working_selector:
                # Diagnostic: figure out WHY there are no cards
                try:
                    title = page.title() or ""
                    url_now = page.url
                    body_sample = (page.inner_text("body")[:500] if page else "")[:500]

                    # Check for explicit blocks
                    block_terms = [
                        "press & hold", "verify you are human", "captcha",
                        "access denied", "blocked", "are you a robot",
                        "checking your browser",
                    ]
                    body_lc = body_sample.lower()
                    blocked = any(term in body_lc for term in block_terms)

                    # Check for empty-results message
                    empty_terms = ["no results", "no listings", "couldn't find", "0 results"]
                    is_empty = any(term in body_lc for term in empty_terms)

                    if blocked:
                        log(f"BLOCKED by Rent.com (title='{title[:60]}'). May need to retry or use a different IP.")
                        update_job(
                            job_id,
                            error=f"Rent.com blocked the request. Title: {title[:80]}",
                        )
                    elif is_empty:
                        log(f"No listings match for {city}, {state} {beds}BR ≤ ${max_price}. Try raising max rent or different city.")
                    else:
                        # Capture a hint of what's actually on the page so we can debug
                        log(f"Cards not found. Page title: '{title[:60]}'. URL: {url_now[:80]}")
                        log(f"Page sample: {body_sample[:200]!r}")
                except Exception as diag_err:
                    log(f"Diagnostic failed: {diag_err}")
                break

            # Determine total pages once, from page 1.
            if page_num == 1:
                try:
                    body_text = page.inner_text("body")
                    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d,]+)", body_text)
                    if m:
                        per_page = int(m.group(2)) - int(m.group(1)) + 1
                        total = int(m.group(3).replace(",", ""))
                        total_pages = max(1, -(-total // per_page))
                        log(f"Found {total} listings across {total_pages} pages")
                        update_job(job_id, total=total)
                except Exception:
                    pass

            # Scroll the custom scroll container Rent.com uses (NOT window).
            # 8 iterations is enough — all cards lazy-load within ~3 seconds.
            scroll_js = """
                const c = document.querySelector('._e2885217');
                if (c) c.scrollBy(0, ARG);
                else window.scrollBy(0, ARG);
            """
            for _ in range(8):
                page.evaluate(scroll_js.replace("ARG", str(random.randint(400, 700))))
                page.wait_for_timeout(random.randint(150, 280))
            page.wait_for_timeout(600)

            cards = page.locator(working_selector).all()
            log(f"Page {page_num}: {len(cards)} cards found")

            # If the working selector matched an inner anchor (not the outer card),
            # we need to climb up to a card-like container so child queries work.
            # We do this by checking the selector name.
            needs_climb = working_selector in (
                'a[data-tid="pdp-link"]',
            )

            for card in cards:
                if check_pause_or_stop() == "stop":
                    break

                try:
                    lead = {
                        "platform": "rent.com",
                        "city": city,
                        "state": state.upper(),
                        "beds": f"{beds} Bed{'s' if int(beds) > 1 else ''}",
                        "max_price": max_price,
                        "property_type": "Apartment",
                        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "url": "",
                        "property_name": "",
                        "address": "",
                        "price": "",
                        "phone": "",
                    }

                    # URL — if we matched the pdp-link anchor directly, the card
                    # IS the anchor; otherwise the anchor lives inside the card.
                    try:
                        if needs_climb:
                            href = card.get_attribute("href") or ""
                        else:
                            href = (
                                card.locator('a[data-tid="pdp-link"]').first.get_attribute("href")
                                or ""
                            )
                        lead["url"] = (
                            "https://www.rent.com" + href if href.startswith("/") else href
                        )
                    except Exception:
                        pass

                    if not lead["url"] or lead["url"] in seen_urls:
                        continue

                    # ── PRIMARY EXTRACTION: JSON-LD structured data ──
                    # Rent.com embeds <script type="application/ld+json"> in each
                    # card for Google's SEO. This is the most reliable source —
                    # it won't change with their CSS class names.
                    json_ld_data = None
                    try:
                        scripts = card.locator(
                            'script[type="application/ld+json"]'
                        ).all()
                        for s in scripts:
                            try:
                                import json as _json
                                txt = s.inner_text(timeout=600).strip()
                                if not txt:
                                    txt = s.evaluate("el => el.textContent") or ""
                                parsed = _json.loads(txt)
                                if isinstance(parsed, list):
                                    parsed = parsed[0] if parsed else {}
                                if isinstance(parsed, dict):
                                    json_ld_data = parsed
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if json_ld_data:
                        # Pull name from JSON-LD
                        if not lead["property_name"]:
                            n = json_ld_data.get("name", "")
                            if n:
                                lead["property_name"] = str(n).strip()

                        # Pull address from JSON-LD — it's often a nested object
                        addr_obj = json_ld_data.get("address", {})
                        if isinstance(addr_obj, dict):
                            parts = [
                                addr_obj.get("streetAddress", ""),
                                addr_obj.get("addressLocality", ""),
                                addr_obj.get("addressRegion", ""),
                                addr_obj.get("postalCode", ""),
                            ]
                            assembled = ", ".join(p for p in parts if p)
                            if assembled:
                                lead["address"] = assembled
                        elif isinstance(addr_obj, str) and addr_obj:
                            lead["address"] = addr_obj

                    # ── FALLBACK CHAIN for property name ──
                    if not lead["property_name"]:
                        try:
                            lead["property_name"] = card.locator(
                                'p._01ccfad3'
                            ).first.inner_text(timeout=600).strip()
                        except Exception:
                            pass

                    if not lead["property_name"]:
                        # Card title is consistently the first <p> inside the pdp-link
                        try:
                            lead["property_name"] = card.locator(
                                'a[data-tid="pdp-link"] p'
                            ).first.inner_text(timeout=600).strip()
                        except Exception:
                            pass

                    if not lead["property_name"]:
                        # Any heading tag
                        for tag in ("h2", "h3", "h4"):
                            try:
                                el = card.locator(tag).first
                                if el.count() > 0:
                                    candidate = el.inner_text(timeout=500).strip()
                                    if candidate and len(candidate) > 2:
                                        lead["property_name"] = candidate
                                        break
                            except Exception:
                                continue

                    if not lead["property_name"]:
                        # Last resort: extract from the URL slug
                        # e.g. /apartments/raleigh-nc/the-timbers/ -> "The Timbers"
                        try:
                            m = re.search(r"/apartments/[^/]+/([^/?]+)", lead["url"])
                            if m:
                                slug = m.group(1).replace("-", " ").strip()
                                if slug:
                                    lead["property_name"] = slug.title()
                        except Exception:
                            pass

                    # ── FALLBACK CHAIN for address ──
                    if not lead["address"]:
                        try:
                            ps = card.locator("p").all()
                            for el in ps:
                                try:
                                    t = el.inner_text(timeout=400).strip()
                                except Exception:
                                    continue
                                if (
                                    t
                                    and t != lead["property_name"]
                                    and "," in t
                                    and re.search(r"\b[A-Z]{2}\b", t)
                                ):
                                    lead["address"] = t
                                    break
                        except Exception:
                            pass

                    # Price — first try the per-bed row matching our bed count
                    try:
                        bed_label = {1: "1 bd", 2: "2 bd", 3: "3 bd", 4: "4 bd"}.get(
                            int(beds), ""
                        )
                        bed_rows = card.locator(
                            'div[data-tid="bed-count-details"] p'
                        ).all()
                        for row in bed_rows:
                            try:
                                txt = row.inner_text(timeout=800).strip().lower()
                            except Exception:
                                continue
                            if bed_label in txt:
                                spans = row.locator("span").all()
                                if len(spans) >= 2:
                                    lead["price"] = spans[1].inner_text(
                                        timeout=800
                                    ).strip()
                                break
                    except Exception:
                        pass
                    if not lead["price"]:
                        try:
                            lead["price"] = card.locator(
                                'span[data-tid="listing-price-text"]'
                            ).first.inner_text(timeout=1000).strip()
                        except Exception:
                            pass

                    # Phone (on the card — no per-listing visit needed)
                    try:
                        phone_el = card.locator('div[data-tid="cta-phone"]').first
                        if phone_el.count() > 0:
                            raw = phone_el.inner_text(timeout=1500).strip()
                            lead["phone"] = format_phone(raw) if raw else ""
                    except Exception:
                        pass

                    seen_urls.add(lead["url"])
                    results.append(lead)
                    update_job(
                        job_id, scraped=len(results), results=results.copy()
                    )

                except Exception as e:
                    print(f"  card error: {e}")
                    continue

            # Pagination
            if max_pages and page_num >= max_pages:
                log(f"Reached page limit ({max_pages}).")
                break
            if page_num >= total_pages:
                log("Reached last page.")
                break

            page_num += 1
            delay = random.uniform(3, 5)
            log(f"Waiting {delay:.0f}s before page {page_num}…")
            time.sleep(delay)

        browser.close()

        stopped = (get_job(job_id) or {}).get("stopped")
        final_msg = (
            f"Stopped — {len(results)} leads collected."
            if stopped
            else f"Done — {len(results)} leads from {page_num} page{'s' if page_num > 1 else ''}."
        )
        update_job(job_id, status="done", stage=final_msg, results=results)


# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    try:
        data = request.get_json(silent=True) or {}
        city = (data.get("city") or "").strip()
        state = (data.get("state") or "").strip()
        beds = data.get("beds")
        max_price = data.get("max_price")
        max_pages = data.get("max_pages")

        if not city or not state:
            return jsonify({"error": "City and state are required"}), 400
        try:
            beds = int(beds)
            assert 1 <= beds <= 4
        except (TypeError, ValueError, AssertionError):
            return jsonify({"error": "Beds must be 1-4"}), 400
        try:
            max_price = int(max_price)
            assert max_price > 0
        except (TypeError, ValueError, AssertionError):
            return jsonify({"error": "Max price must be a positive number"}), 400
        try:
            max_pages = int(max_pages) if max_pages else None
        except (TypeError, ValueError):
            max_pages = None

        # Reject if a scrape is already running (free tier can only fit one Chromium)
        with JOBS_LOCK:
            running = [
                j for j in JOBS.values()
                if j.get("status") in ("queued", "running", "paused")
            ]
        if running:
            return jsonify({
                "error": "Another scrape is already running.",
                "running_job_id": running[0]["id"],
            }), 409

        label = f"{city}, {state.upper()} — {beds}BR up to ${max_price}"
        job_id = make_job(label)
        t = threading.Thread(
            target=scrape_rent,
            args=(job_id, city, state, beds, max_price, max_pages),
            daemon=True,
        )
        t.start()
        update_job(job_id, status="running")
        return jsonify({"job_id": job_id, "label": label})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/control/<job_id>", methods=["POST"])
def api_control(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower()
    if action == "pause":
        update_job(job_id, paused=True)
    elif action == "resume":
        update_job(job_id, paused=False)
    elif action == "stop":
        update_job(job_id, stopped=True, paused=False)
    else:
        return jsonify({"error": "Unknown action"}), 400
    return jsonify({"ok": True, "action": action})


@app.route("/api/export/<job_id>")
def api_export(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    rows = job.get("results", [])

    # If the user has been swiping and only wants their saved set,
    # the frontend sends ?only=url1,url2,url3
    only_param = request.args.get("only", "").strip()
    if only_param:
        only = set(u for u in only_param.split(",") if u)
        rows = [r for r in rows if r.get("url") in only]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Property Name", "Address", "City", "State", "Beds", "Price",
        "Phone", "URL", "Scraped At", "Platform",
    ])
    for r in rows:
        writer.writerow([
            r.get("property_name", ""),
            r.get("address", ""),
            r.get("city", ""),
            r.get("state", ""),
            r.get("beds", ""),
            r.get("price", ""),
            r.get("phone", ""),
            r.get("url", ""),
            r.get("scraped_at", ""),
            r.get("platform", ""),
        ])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    safe = re.sub(r"[^a-z0-9]+", "-", job["query"].lower()).strip("-")
    suffix = "-saved" if only_param else ""
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"rent-{safe}{suffix}-{job_id}.csv",
    )


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "Server error (500)."}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
