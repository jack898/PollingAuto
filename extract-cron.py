#!/usr/bin/env python3
"""
Boston parking ticket scraper.

Features:
- Scans violation IDs in chunks (CHUNK_SIZE per run).
- Resumes from where it left off (using last_vid.txt).
- Treats both HTTP 404 and empty `data: []` as gaps.
- If GAP_THRESHOLD consecutive gaps occur, reset to START_VID (up to MAX_RESTARTS).
- Writes matching tickets to filtered_boston_tickets.csv.
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
COOKIES = {
    # e.g. "PHPSESSID": "..."
}

# Starting VID (used only if last_vid.txt is missing)
START_VID = 831394098

# How many VIDs to scan per GitHub Action run
CHUNK_SIZE = 1000

# Gap threshold (counting both HTTP 404 and empty data[] as gaps)
GAP_THRESHOLD = 15000

# Maximum restarts after hitting GAP_THRESHOLD
# Set to None for infinite restarts
MAX_RESTARTS = 1

# Keywords to look for in description
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

# File paths
CSV_OUT = "filtered_boston_tickets.csv"
STATE_FILE = "last_vid.txt"

# Timing
REQUEST_DELAY = 0.001  # 1 ms
MAX_BACKOFF_MULT = 6


# ---- State handling ----
def load_last_vid():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return int(f.read().strip())
        except Exception:
            return START_VID
    return START_VID


def save_last_vid(vid):
    with open(STATE_FILE, "w") as f:
        f.write(str(vid))


# ---- HTTP helpers ----
def polite_sleep():
    """Fixed tiny delay between requests (1 ms)."""
    time.sleep(REQUEST_DELAY)


def build_url(vid):
    qs = QS_TEMPLATE.format(vid=vid)
    return f"https://{BASE_HOST}{SEARCH_PATH}?{qs}"


def fetch_search(vid):
    url = build_url(vid)
    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=12)
    except Exception as e:
        return ("err", str(e))

    if resp.status_code == 403:
        return ("403", resp.text)
    if resp.status_code == 429:
        return ("429", resp.text)
    if resp.status_code == 404:
        return ("404", None)
    if resp.status_code != 200:
        return ("err", f"status={resp.status_code}")

    try:
        j = resp.json()
    except Exception:
        return ("err", "invalid-json")
    return ("ok", j)


# ---- Filtering ----
def passes_filters(top):
    if top.get("userdef1_label") != "Location":
        return False
    if top.get("userdef8_label") != "Street Number":
        return False

    u1 = top.get("userdef1")
    u8 = top.get("userdef8")
    if not u1 or str(u1).strip() == "" or str(u1).strip().lower() == "null":
        return False
    if not u8 or str(u8).strip() == "" or str(u8).strip().lower() == "null":
        return False

    desc = str(top.get("description") or "").lower()
    if not any(kw in desc for kw in KEYWORDS):
        return False
    return True


def extract_row(vid, top):
    num = str(top.get("userdef8", "")).strip()
    name = str(top.get("userdef1", "")).strip()
    address = f"{num} {name}".strip()
    if address:
        address = address + ", Boston, MA"
    return {
        "violation_number": vid,
        "date_utc": top.get("date_utc") or top.get("date", ""),
        "address": address,
        "zonenumber": top.get("zonenumber", ""),
        "lpn": top.get("lpn", ""),
        "description": top.get("description", ""),
    }


def write_rows(rows, path=CSV_OUT):
    header = ["violation_number", "date_utc", "address", "zonenumber", "lpn", "description"]
    need_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if need_header:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


# ---- Main ----
def main():
    collected = []
    seen = set()
    backoff_count = 0
    restart_count = 0

    current_vid = load_last_vid()
    end_vid = current_vid + CHUNK_SIZE
    consecutive_gaps = 0

    print(f"Scanning {CHUNK_SIZE} VIDs: {current_vid} → {end_vid-1}")

    while current_vid < end_vid:
        status, payload = fetch_search(current_vid)

        if status == "ok":
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data or len(data) == 0:
                # Treat empty data as a gap
                consecutive_gaps += 1
                print(f"[EMPTY] VID={current_vid} (consecutive_gaps={consecutive_gaps})")
            else:
                consecutive_gaps = 0
                top = data[0]
                if passes_filters(top) and current_vid not in seen:
                    seen.add(current_vid)
                    row = extract_row(current_vid, top)
                    collected.append(row)
                    print(f"[KEEP] VID={current_vid} {row['address']} {row['description']}")
            polite_sleep()
            current_vid += 1

        elif status == "404":
            consecutive_gaps += 1
            print(f"[404] VID={current_vid} (consecutive_gaps={consecutive_gaps})")
            polite_sleep()
            current_vid += 1

        elif status in ("403", "429"):
            backoff_count += 1
            mult = min(MAX_BACKOFF_MULT, 2 ** backoff_count)
            wait = REQUEST_DELAY * mult + random.random() * 2.0 + 0.05
            print(f"[!] {status} at VID={current_vid}, backing off {wait:.2f}s")
            time.sleep(wait)
            current_vid += 1

        else:
            print(f"[ERR] VID={current_vid} => {payload}")
            time.sleep(0.1)
            current_vid += 1

        # Restart logic
        if consecutive_gaps >= GAP_THRESHOLD:
            restart_count += 1
            print(f"[!] Hit {GAP_THRESHOLD} consecutive gaps. Restarting from {START_VID}.")
            consecutive_gaps = 0
            current_vid = START_VID
            if MAX_RESTARTS is not None and restart_count >= MAX_RESTARTS:
                print("[!] Reached MAX_RESTARTS, ending early.")
                break

        # Flush CSV periodically
        if len(collected) >= 10:
            write_rows(collected)
            collected = []

    if collected:
        write_rows(collected)

    save_last_vid(current_vid)
    print(f"Done. Next start VID = {current_vid}")


if __name__ == "__main__":
    main()

