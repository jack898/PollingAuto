#!/usr/bin/env python3
"""
Boston parking ticket scraper (hybrid date + valid VID).

Features:
- Scans CHUNK_SIZE VIDs per run.
- Tracks both last_date (latest kept ticket date) and last_valid_vid (most recent kept Boston ticket VID).
- Repeats each range PASS_LIMIT times before advancing.
- If gaps exceed GAP_THRESHOLD, rollback to last_valid_vid (preferred) or newest_vid.
- If tickets found with a newer date, advance cursor; otherwise probe forward cautiously.
- Persists state across runs (last_vid, last_date, last_valid_vid, pass_count, gap_count, seen_vids).
- Deduplicates tickets persistently via seen_vids.txt.
- Quits early if 5 consecutive 403s.
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
STATE_VALID = "last_valid_vid.txt"
STATE_PASS = "pass_count.txt"
STATE_GAP = "gap_count.txt"
SEEN_FILE = "seen_vids.txt"

# Parameters
START_VID = 831394104
CHUNK_SIZE = 1000
PASS_LIMIT = 3
GAP_THRESHOLD = 10000
FORWARD_BUFFER = 3000
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
    "bike or bus lane",
    "over posted limit",
    "double parking",
    "no parking"
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
    last_date_str = load_str(STATE_DATE, "")
    last_date = parse_date(last_date_str)
    last_valid_vid = load_int(STATE_VALID, START_VID)
    pass_count = load_int(STATE_PASS, 0)
    consecutive_gaps = load_int(STATE_GAP, 0)
    seen = load_seen()

    end_vid = current_vid + CHUNK_SIZE
    collected = []
    newest_date = last_date
    newest_vid = None
    count_403 = 0
    restart_count = 0

    print(f"Pass {pass_count+1}/{PASS_LIMIT}: scanning {current_vid} → {end_vid-1}, last_date={last_date_str}, last_valid_vid={last_valid_vid}, seen={len(seen)}")

    while current_vid < end_vid:
        status, payload = fetch_search(current_vid)

        if status == "ok":
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                consecutive_gaps += 1
            else:
                top = data[0]
                dt = parse_date(top.get("date_utc") or top.get("date"))
                if passes_filters(top) and str(current_vid) not in seen:
                    consecutive_gaps = 0
                    row = extract_row(current_vid, top)
                    collected.append(row)
                    seen.add(str(current_vid))
                    last_valid_vid = current_vid
                    print(f"[KEEP] {current_vid} {row['address']} {row['description']}")
                    if dt and (newest_date is None or dt > newest_date):
                        newest_date = dt
                        newest_vid = current_vid
                else:
                  consecutive_gaps += 1

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
            current_vid += 1

        if consecutive_gaps >= GAP_THRESHOLD:
            restart_count += 1
            print(f"[!] Hit {GAP_THRESHOLD} gaps")
            consecutive_gaps = 0
            if last_valid_vid:
                current_vid = last_valid_vid
                print(f"Rolling back to last_valid_vid={last_valid_vid}")
            elif newest_vid:
                current_vid = newest_vid
                print(f"Rolling back to newest_vid={newest_vid}")
            else:
                current_vid = START_VID
                print(f"Rolling back to START_VID={START_VID}")
            if MAX_RESTARTS and restart_count >= MAX_RESTARTS:
                break

        if len(collected) >= 10:
            write_rows(collected)
            collected = []

    if collected:
        write_rows(collected)

    save_seen(seen)
    save_int(STATE_VALID, last_valid_vid)
    save_int(STATE_GAP, consecutive_gaps)

    # Pass management
    pass_count += 1
    if pass_count >= PASS_LIMIT:
        if newest_date and (last_date is None or newest_date > last_date):
            save_str(STATE_DATE, newest_date.isoformat())
            if newest_vid:
                save_int(STATE_VID, newest_vid)
                print(f"Updated last_date → {newest_date.isoformat()}, advancing to newest_vid={newest_vid}")
            else:
                save_int(STATE_VID, end_vid + FORWARD_BUFFER)
                print(f"Updated last_date → {newest_date.isoformat()}, probing forward to {end_vid + FORWARD_BUFFER}")
        else:
            save_int(STATE_VID, end_vid + FORWARD_BUFFER)
            print(f"No newer dates, probing forward to {end_vid + FORWARD_BUFFER}")
        save_int(STATE_PASS, 0)
    else:
        save_int(STATE_VID, load_int(STATE_VID, START_VID))
        save_int(STATE_PASS, pass_count)
        print(f"Repeating pass {pass_count}/{PASS_LIMIT} around VID={load_int(STATE_VID, START_VID)}")

    print(f"Done. consecutive_gaps={consecutive_gaps}, next start={load_int(STATE_VID)}, last_valid_vid={last_valid_vid}, last_date={load_str(STATE_DATE)}")

if __name__ == "__main__":
    main()



