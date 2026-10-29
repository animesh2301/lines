"""
Business Central -> CSV: COST CENTER and DIVISION for purchase receipt lines.

Pulls every receipt line of every company, with its dimensions, in large
pages (tens of calls instead of one call per receipt):

    GET companies({company_id})/purchaseReceiptLines
        ?$select=id,documentId&$expand=dimensionSetLines

and writes ONE CSV with: company_name, company_id, lineId, documentId,
costCenter, division.

Each page is written to the CSV as soon as it arrives, and the position is
saved in a .progress file. If the run stops (throttling, timeout, network),
just run it again: it carries on from the last saved page. Delete the CSV
and the .progress file to start from scratch.
"""

import csv
import json
import os
import sys
import time
import requests
import urllib3


# ============================================================
# AUTH CONFIGURATION
# ============================================================

TENANT_ID = "<tenant-id>"
CLIENT_ID = "<client-id>"
CLIENT_SECRET = "<client-sec>"

API_HOST = "https://api.businesscentral.dynamics.com"
SCOPE = f"{API_HOST}/.default"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"


# ============================================================
# EXTRACT CONFIGURATION
# ============================================================

ENVIRONMENT = "D365-BC-QA"

BASE_URL = f"{API_HOST}/v2.0/{ENVIRONMENT}/api/v2.0"

# Same companies as extract.py
COMPANIES = {
    "Company 1": "<company_1_id>",
    "Company 2": "<company_2_id>",
}

OUTPUT_FOLDER = r"C:\Users\animesh.agarwal.QBSS\Downloads"
OUTPUT_CSV = os.path.join(OUTPUT_FOLDER, "purchaseReceiptLineDimensions.csv")
PROGRESS_FILE = OUTPUT_CSV + ".progress"

# Rows per page asked from BC (BC may return fewer)
PAGE_SIZE = 5000

# Dimension codes as they are set up in BC
COST_CENTER_CODE = "COST CENTER"
DIVISION_CODE = "DIVISION"

# Same column names extract.py stamps on every row
COMPANY_NAME_COLUMN = "company_name"
COMPANY_ID_COLUMN = "company_id"

# Set back to True once the network certificate issue is fixed
VERIFY_SSL = True
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQUEST_TIMEOUT = 300          # a page with dimensions can take a while

# Retries per page for throttling (429/503), BC errors (5xx) and timeouts
MAX_RETRIES = 10


# ============================================================
# AUTH
# ============================================================

def get_access_token() -> str:
    """Request an access token using the client credentials flow."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": SCOPE,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(
        TOKEN_URL,
        data=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        verify=VERIFY_SSL,
    )

    print(f"[Auth] Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"[Auth] Response: {resp.text}")
        resp.raise_for_status()

    token = resp.json()["access_token"]
    print("[Auth] Token acquired successfully.\n")
    return token


class Session:
    """Holds the bearer token, re-acquires it once on a 401, waits on 429."""

    def __init__(self):
        self.token = get_access_token()
        self.http = requests.Session()          # reuse one connection

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Prefer": f"odata.maxpagesize={PAGE_SIZE}",
        }

    def get(self, url):
        for attempt in range(1, MAX_RETRIES + 1):
            wait = min(10 * attempt, 120)
            try:
                resp = self.http.get(
                    url,
                    headers=self.headers(),
                    timeout=REQUEST_TIMEOUT,
                    verify=VERIFY_SSL,
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                print(f"  [retry {attempt}] {type(e).__name__} - waiting {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                print("[Auth] Token rejected - refreshing and retrying.")
                self.token = get_access_token()
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = int(resp.headers.get("Retry-After", wait))
                print(f"  [retry {attempt}] HTTP {resp.status_code} - "
                      f"waiting {wait}s")
                time.sleep(wait)
                continue

            return resp

        raise requests.exceptions.RetryError(
            f"gave up after {MAX_RETRIES} attempts: {url}")


# ============================================================
# PROGRESS (resume after a stop)
# ============================================================

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": [], "company": None, "next_url": None}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f)


# ============================================================
# FETCH
# ============================================================

def fetch_company(session, writer, csv_file, progress,
                  company_name, company_id):
    """Every receipt line of one company, written page by page."""

    if progress["company"] == company_name and progress["next_url"]:
        url = progress["next_url"]
        print("  resuming from the last saved page")
    else:
        url = (f"{BASE_URL}/companies({company_id})/purchaseReceiptLines"
               f"?$select=id,documentId&$expand=dimensionSetLines")
    count = 0

    while url:
        resp = session.get(url)
        resp.raise_for_status()
        data = resp.json()

        for line in data.get("value", []):
            values = {d["code"]: d["valueCode"]
                      for d in line.get("dimensionSetLines", [])}
            writer.writerow([company_name, company_id, line["id"],
                             line["documentId"],
                             values.get(COST_CENTER_CODE, ""),
                             values.get(DIVISION_CODE, "")])
        csv_file.flush()
        count += len(data.get("value", []))

        url = data.get("@odata.nextLink")
        progress.update(company=company_name, next_url=url)
        save_progress(progress)
        print(f"  {company_name}: {count} lines this run")

    progress["done"].append(company_name)
    progress.update(company=None, next_url=None)
    save_progress(progress)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:
        session = Session()
    except requests.exceptions.RequestException as e:
        print("\nCould not acquire token:")
        print(e)
        sys.exit(1)

    progress = load_progress()
    resuming = os.path.exists(OUTPUT_CSV) and (progress["done"]
                                               or progress["company"])
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    with open(OUTPUT_CSV, "a" if resuming else "w",
              newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not resuming:
            writer.writerow([COMPANY_NAME_COLUMN, COMPANY_ID_COLUMN,
                             "lineId", "documentId", "costCenter", "division"])

        for company_name, company_id in COMPANIES.items():
            if company_name in progress["done"]:
                print(f"\nCompany: {company_name} - already done, skipped")
                continue
            print(f"\nCompany: {company_name}")
            try:
                fetch_company(session, writer, f, progress,
                              company_name, company_id)
            except requests.exceptions.RequestException as e:
                print(f"\n  [stopped] {company_name}: {e}")
                if getattr(e, "response", None) is not None:
                    print(f"  {e.response.text[:500]}")
                print("  Everything fetched so far is saved. "
                      "Run the script again to continue.")
                sys.exit(1)

    os.remove(PROGRESS_FILE)
    print(f"\nAll companies done -> {OUTPUT_CSV}")
