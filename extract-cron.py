#!/usr/bin/env python3
"""
Boston parking ticket scraper (hybrid VID + UTC-date logic).

Features:
- Scans CHUNK_SIZE VIDs starting from last_vid.txt.
- Tracks both last_vid (scan cursor) and last_date (latest UTC date seen).
- Never advances past a chunk if it found a ticket newer than last_date.
- Deduplicates tickets persistently via seen_vids.txt.
- Persists state across runs (last_vid, last_date, consecutive_gaps, seen_vids).
"""

import requests
import time
import csv
import random
import os
from datetime import datetime

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
STATE_VID = "last_vid.txt"
STATE_DATE = "last_date.txt"
STATE_GAP = "gap_count.txt"
SEEN_FILE = "seen_vids.txt"

# Parameters
START_VID = 831399742
CHUNK_SIZE = 1000
GAP_THRESHOLD = 15000
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

# ---- Helpers ----
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

def load_str(path, default=""):
    if os.path.exists(path):
        return open(path).read().strip()
    return default

def save_str(path, val):
    with open(path, "w") as f:
        f.write(str(val))

def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

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
    current_vid = load_int(STATE_VID, START_VID)
    consecutive_gaps = load_int(STATE_GAP, 0)
    last_date_str = load_str(STATE_DATE, "")
    last_date = parse_date(last_date_str)
    seen = load_seen()

    end_vid = current_vid + CHUNK_SIZE
    collected = []
    restart_count = 0
    newest_date = last_date
    count_403 = 0

    print(f"Scanning {current_vid} → {end_vid-1}, last_date={last_date_str}, seen={len(seen)}")

    while current_vid < end_vid:
        status, payload = fetch_search(current_vid)
        if status == "ok":
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                consecutive_gaps += 1
            else:
                consecutive_gaps = 0
                top = data[0]
                dt = parse_date(top.get("date_utc") or top.get("date"))
                if dt and (newest_date is None or dt > newest_date):
                    newest_date = dt
                if passes_filters(top) and str(current_vid) not in seen:
                    row = extract_row(current_vid, top)
                    collected.append(row)
                    seen.add(str(current_vid))
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
            if status == "403":
                  count_403 += 1
                  if count_403 >= 5:
                      print("[!] Received 5 consecutive 403s, ending run early.")
                      break
            current_vid += 1

        else:
            count_403 = 0
            current_vid += 1

        # reset gaps if threshold exceeded
        if consecutive_gaps >= GAP_THRESHOLD:
            restart_count += 1
            print(f"[!] Hit {GAP_THRESHOLD} gaps, restarting from {START_VID}")
            consecutive_gaps = 0
            current_vid = START_VID
            if MAX_RESTARTS and restart_count >= MAX_RESTARTS:
                break

        if len(collected) >= 10:
            write_rows(collected)
            collected = []

    if collected:
        write_rows(collected)

    save_seen(seen)
    save_int(STATE_GAP, consecutive_gaps)

    # Decide how to advance VID
    if newest_date and (last_date is None or newest_date > last_date):
        # Found newer tickets → update last_date, but don't advance VID much
        save_str(STATE_DATE, newest_date.isoformat())
        save_int(STATE_VID, load_int(STATE_VID, START_VID))  # stay in same range
        print(f"Updated last_date → {newest_date.isoformat()}, staying around VID={load_int(STATE_VID)}")
    else:
        # No newer dates → safe to advance cursor
        save_int(STATE_VID, end_vid)
        print(f"No newer dates found, advancing to VID={end_vid}")

    print(f"Done. consecutive_gaps={consecutive_gaps}, next start={load_int(STATE_VID)}, last_date={load_str(STATE_DATE)}")

if __name__ == "__main__":
    main()

