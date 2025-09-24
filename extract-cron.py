#!/usr/bin/env python3
"""
Boston parking ticket scraper.

Features:
- Scans in CHUNK_SIZE slices.
- Repeats scanning the same range PASS_LIMIT times
  (to catch tickets that appear a little later).
- After PASS_LIMIT passes, advance start VID to the
  highest valid VID found so far.
- Persists state across runs in last_vid.txt and pass_count.txt.
"""

import requests
import time
import csv
import random
import os

# ---- Configuration ----
BASE_HOST = "bostonma.rmcpay.com"
SEARCH_PATH = "/rmcapi/api/violation_index.php/searchviolation"
QS_TEMPLATE = ("operatorid=1582&violationnumber={vid}&stateid=&lpn=&vin=&plate_type_id="
               "&devicenumber=&payment_plan_id=&immobilization_id=&single_violation=0&omsessiondata=&")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": f"https://{BASE_HOST}/",
}
COOKIES = {}

# State files
STATE_FILE = "last_vid.txt"
PASS_FILE = "pass_count.txt"

# Parameters
START_VID = 831394104 # Start VID at 9/24/25 1pm
CHUNK_SIZE = 1000
PASS_LIMIT = 3       # how many times to rescan same range
GAP_THRESHOLD = 10000
MAX_RESTARTS = 1
REQUEST_DELAY = 0.001  # 1 ms

CSV_OUT = "filtered_boston_tickets.csv"

KEYWORDS = [
    "resident permit only",
    "no stopping or standing",
    "meter fee unpaid",
    "no valid",
    "within 20 feet of intersection",
    "hydrant",
    "driveway",
    "sidewalk",
    "bike or bus lane"
]

MAX_BACKOFF_MULT = 6


# ---- State helpers ----
def load_last_vid():
    if os.path.exists(STATE_FILE):
        try:
            return int(open(STATE_FILE).read().strip())
        except Exception:
            return START_VID
    return START_VID

def save_last_vid(vid):
    with open(STATE_FILE, "w") as f:
        f.write(str(vid))

def load_pass_count():
    if os.path.exists(PASS_FILE):
        try:
            return int(open(PASS_FILE).read().strip())
        except Exception:
            return 0
    return 0

def save_pass_count(count):
    with open(PASS_FILE, "w") as f:
        f.write(str(count))


# ---- HTTP ----
def polite_sleep():
    time.sleep(REQUEST_DELAY)

def build_url(vid):
    return f"https://{BASE_HOST}{SEARCH_PATH}?{QS_TEMPLATE.format(vid=vid)}"

def fetch_search(vid):
    try:
        resp = requests.get(build_url(vid), headers=HEADERS, cookies=COOKIES, timeout=12)
    except Exception as e:
        return ("err", str(e))

    if resp.status_code == 403:
        return ("403", None)
    if resp.status_code == 429:
        return ("429", None)
    if resp.status_code == 404:
        return ("404", None)
    if resp.status_code != 200:
        return ("err", f"status={resp.status_code}")

    try:
        j = resp.json()
    except Exception:
        return ("err", "invalid-json")
    return ("ok", j)


# ---- Filters ----
def passes_filters(top):
    if top.get("userdef1_label") != "Location":
        return False
    if top.get("userdef8_label") != "Street Number":
        return False
    u1 = top.get("userdef1")
    u8 = top.get("userdef8")
    if not u1 or str(u1).strip().lower() in ("", "null"):
        return False
    if not u8 or str(u8).strip().lower() in ("", "null"):
        return False
    desc = str(top.get("description") or "").lower()
    return any(kw in desc for kw in KEYWORDS)

def extract_row(vid, top):
    num = str(top.get("userdef8", "")).strip()
    name = str(top.get("userdef1", "")).strip()
    address = f"{num} {name}".strip()
    if address:
        address += ", Boston, MA"
    return {
        "violation_number": vid,
        "date_utc": top.get("date_utc") or top.get("date", ""),
        "address": address,
        "zonenumber": top.get("zonenumber", ""),
        "lpn": top.get("lpn", ""),
        "description": top.get("description", ""),
    }

def write_rows(rows):
    header = ["violation_number","date_utc","address","zonenumber","lpn","description"]
    need_header = not os.path.exists(CSV_OUT)
    with open(CSV_OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if need_header:
            w.writeheader()
        for r in rows:
            w.writerow(r)


# ---- Main ----
def main():
    current_vid = load_last_vid()
    pass_count = load_pass_count()
    end_vid = current_vid + CHUNK_SIZE

    collected = []
    largest_valid_vid = None
    consecutive_gaps = 0
    restart_count = 0

    print(f"Pass {pass_count+1}/{PASS_LIMIT}: scanning {current_vid} → {end_vid-1}")

    while current_vid < end_vid:
        status, payload = fetch_search(current_vid)

        if status == "ok":
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                consecutive_gaps += 1
            else:
                consecutive_gaps = 0
                top = data[0]
                if passes_filters(top):
                    row = extract_row(current_vid, top)
                    collected.append(row)
                    largest_valid_vid = max(largest_valid_vid or current_vid, current_vid)
                    print(f"[KEEP] {current_vid} {row['address']} {row['description']}")
            polite_sleep()
            current_vid += 1

        elif status == "404":
            consecutive_gaps += 1
            polite_sleep()
            current_vid += 1

        elif status in ("403","429"):
            wait = 1 + random.random()*2
            print(f"[!] {status} backing off {wait:.1f}s")
            time.sleep(wait)
            current_vid += 1

        else:
            current_vid += 1

        if consecutive_gaps >= GAP_THRESHOLD:
            restart_count += 1
            print(f"[!] Hit {GAP_THRESHOLD} gaps, restarting from {START_VID}")
            current_vid = START_VID
            consecutive_gaps = 0
            if MAX_RESTARTS and restart_count >= MAX_RESTARTS:
                break

        if len(collected) >= 10:
            write_rows(collected)
            collected = []

    if collected:
        write_rows(collected)

    # Pass management
    pass_count += 1
    if pass_count >= PASS_LIMIT:
        if largest_valid_vid:
            save_last_vid(largest_valid_vid)
            print(f"Advancing start to {largest_valid_vid}")
        else:
            # no valid found, just continue from where we left off
            save_last_vid(current_vid)
        save_pass_count(0)
    else:
        # same range again
        save_last_vid(load_last_vid())
        save_pass_count(pass_count)

    print("Done.")

if __name__ == "__main__":
    main()
