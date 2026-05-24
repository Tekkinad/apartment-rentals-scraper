"""
rent-scout — flexible Rent.com lead scraper.

User picks any US city, max price, and bed count. Backend hits Rent.com's
direct filtered URL (no clicking / form filling needed — Rent.com supports
filters as URL params). Phone number is on the card itself, no per-listing
visits needed.
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


def build_url(city, state, beds, max_price, page=1):
    state_slug = STATE_SLUGS.get(state.upper(), state.lower().replace(" ", "-"))
    city_slug = city.lower().strip().replace(",", "").replace(" ", "-")
    bed_param = BED_PARAMS.get(int(beds), "1BR")
    page_part = f"/page-{page}" if page > 1 else ""
    return (
        f"https://www.rent.com/{state_slug}/{city_slug}-apartments{page_part}"
        f"?min_price=0&max_price={int(max_price)}&bedrooms={bed_param}"
    )


def scrape_rent(job_id, city, state, beds, max_price, max_pages=None):
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

        log("Opening Rent.com…")

        while True:
            if check_pause_or_stop() == "stop":
                log("Stopped by user.")
                break

            url = build_url(city, state, beds, max_price, page_num)
            log(f"Page {page_num}: loading…")
            try:
                page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            except Exception as e:
                log(f"Page {page_num} failed to load: {str(e)[:80]}")
                if page_num == 1:
                    raise
                break

            # ── SPEED FIX #1: post-load wait 2.5-4s → 1.2-2.0s ──
            time.sleep(random.uniform(1.2, 2.0))
            try:
                page.wait_for_selector('li[data-tid^="srp_card_"]', timeout=15_000)
            except Exception:
                log(f"No cards on page {page_num} — stopping.")
                break

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

            # ── SPEED FIX #2: 20 scroll iterations → 10 with bigger jumps ──
            scroll_js = """
                const c = document.querySelector('._e2885217');
                if (c) c.scrollBy(0, ARG);
                else window.scrollBy(0, ARG);
            """
            for _ in range(10):
                page.evaluate(scroll_js.replace("ARG", str(random.randint(400, 700))))
                page.wait_for_timeout(random.randint(150, 280))
            page.wait_for_timeout(800)

            cards = page.locator('li[data-tid^="srp_card_"]').all()
            log(f"Page {page_num}: {len(cards)} cards found")

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

                    # URL
                    try:
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

                    # Property name (unchanged - your working logic)
                    try:
                        lead["property_name"] = card.locator(
                            'p._01ccfad3'
                        ).first.inner_text(timeout=800).strip()
                    except Exception:
                        pass

                    if not lead["property_name"]:
                        try:
                            lead["property_name"] = card.locator(
                                'a[data-tid="pdp-link"] p'
                            ).first.inner_text(timeout=800).strip()
                        except Exception:
                            pass

                    if not lead["property_name"]:
                        try:
                            for tag in ("h2", "h3", "h4"):
                                el = card.locator(tag).first
                                if el.count() > 0:
                                    candidate = el.inner_text(timeout=600).strip()
                                    if candidate and len(candidate) > 2:
                                        lead["property_name"] = candidate
                                        break
                        except Exception:
                            pass

                    if not lead["property_name"]:
                        try:
                            addr = lead.get("address", "")
                            if addr and "," in addr:
                                lead["property_name"] = addr.split(",")[0].strip()
                        except Exception:
                            pass

                    # ── ADDRESS FIX: multi-strategy, pick the best candidate ──
                    address_candidates = []

                    # Strategy A: all <p> elements with comma + 2-letter state + digit
                    try:
                        ps = card.locator("p").all()
                        for el in ps:
                            try:
                                t = el.inner_text(timeout=1200).strip()
                            except Exception:
                                continue
                            if not t or t == lead["property_name"]:
                                continue
                            if "," in t and re.search(r"\b[A-Z]{2}\b", t) and re.search(r"\d", t):
                                address_candidates.append(t)
                    except Exception:
                        pass

                    # Strategy B: regex on full card text for address pattern
                    if not address_candidates:
                        try:
                            full_text = card.inner_text(timeout=2000)
                            matches = re.findall(
                                r"\d{1,6}\s+[A-Za-z][^\n,]{2,60},\s*[A-Za-z][A-Za-z\s]{2,30},?\s*[A-Z]{2}\s*\d{5}",
                                full_text,
                            )
                            address_candidates.extend(matches)
                            if not address_candidates:
                                looser = re.findall(
                                    r"[A-Za-z0-9][^\n]{5,80}?,\s*[A-Za-z\s]{2,30},\s*[A-Z]{2}\s*\d{5}",
                                    full_text,
                                )
                                address_candidates.extend(looser)
                        except Exception:
                            pass

                    # Strategy C: card aria-label after first comma
                    if not address_candidates:
                        try:
                            label = (card.get_attribute("aria-label") or "").strip()
                            if label and "," in label and re.search(r"\b[A-Z]{2}\b", label):
                                parts = label.split(",", 1)
                                if len(parts) > 1:
                                    address_candidates.append(parts[1].strip())
                        except Exception:
                            pass

                    # Pick the best: longest candidate with digits
                    if address_candidates:
                        best = max(
                            address_candidates,
                            key=lambda a: (1 if re.search(r"\d", a) else 0, len(a)),
                        )
                        lead["address"] = best

                    # Price (unchanged)
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

                    # Phone (unchanged)
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

            if max_pages and page_num >= max_pages:
                log(f"Reached page limit ({max_pages}).")
                break
            if page_num >= total_pages:
                log("Reached last page.")
                break

            page_num += 1
            # ── SPEED FIX #3: inter-page delay 6-10s → 2.5-4s ──
            delay = random.uniform(2.5, 4.0)
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
