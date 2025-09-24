#!/usr/bin/env python3
"""
Boston parking ticket scraper.

Features:
- Scans CHUNK_SIZE VIDs per run (default 1000).
- Repeats each range PASS_LIMIT times (default 3).
- After PASS_LIMIT passes, advances start VID to the
  largest valid VID found, or just continues forward if none.
- Persists state across runs (last_vid, pass_count, consecutive_gaps, seen_vids).
- Treats both HTTP 404 and empty data[] as gaps.
- Prevents duplicate rows in CSV using seen_vids.txt.
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
GAP_FILE = "gap_count.txt"
SEEN_FILE = "seen_vids.txt"

# Parameters
START_VID = 831394104
CHUNK_SIZE = 1000
PASS_LIMIT = 3
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
def load_int(path, default=0):
    if os.path.exists(path):
        try:
            return int(open(path).read().strip())
        except Exception:
            return default
    return default

def save_int(path, val):
    with open(path, "w") as f:
        f.write(str(val))

def load_last_vid():
    return load_int(STATE_FILE, START_VID)

def save_last_vid(vid):
    save_int(STATE_FILE, vid)

def load_pass_count():
    return load_int(PASS_FILE, 0)

def save_pass_count(count):
    save_int(PASS_FILE, count)

def load_gap_count():
    return load_int(GAP_FILE, 0)

def save_gap_count(count):
    save_int(GAP_FILE, count)

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        for vid in sorted(seen, key=int):
            f.write(str(vid) + "\n")


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
    consecutive_gaps = load_gap_count()
    seen = load_seen()
    end_vid = current_vid + CHUNK_SIZE

    collected = []
    largest_valid_vid = None
    restart_count = 0

    print(f"Pass {pass_count+1}/{PASS_LIMIT}: scanning {current_vid} → {end_vid-1}, starting gaps={consecutive_gaps}, seen={len(seen)}")

    while current_vid < end_vid:
        status, payload = fetch_search(current_vid)

        if status == "ok":
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                consecutive_gaps += 1
                save_gap_count(consecutive_gaps)
            else:
                consecutive_gaps = 0
                save_gap_count(0)
                top = data[0]
                if passes_filters(top) and str(current_vid) not in seen:
                    row = extract_row(current_vid, top)
                    collected.append(row)
                    seen.add(str(current_vid))
                    largest_valid_vid = max(largest_valid_vid or current_vid, current_vid)
                    print(f"[KEEP] {current_vid} {row['address']} {row['description']}")
            polite_sleep()
            current_vid += 1

        elif status == "404":
            consecutive_gaps += 1
            save_gap_count(consecutive_gaps)
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
            consecutive_gaps = 0
            save_gap_count(0)
            current_vid = START_VID
            if MAX_RESTARTS and restart_count >= MAX_RESTARTS:
                break

        if len(collected) >= 10:
            write_rows(collected)
            collected = []

    if collected:
        write_rows(collected)

    save_seen(seen)

    # Pass management
    pass_count += 1
    if pass_count >= PASS_LIMIT:
        if largest_valid_vid:
            save_last_vid(largest_valid_vid)
            print(f"Advancing start to {largest_valid_vid}")
        else:
            save_last_vid(current_vid)
            print(f"No valid tickets, advancing to {current_vid}")
        save_pass_count(0)
    else:
        # repeat same range
        save_last_vid(load_last_vid())
        save_pass_count(pass_count)

    print(f"Done. consecutive_gaps={consecutive_gaps}, next start={load_last_vid()}, next pass={load_pass_count()}, seen={len(seen)}")


if __name__ == "__main__":
    main()
