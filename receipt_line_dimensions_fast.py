"""
Business Central -> CSV: COST CENTER and DIVISION for purchase receipt lines.

Pulls every receipt line of every company, with its dimensions, in large
pages (tens of calls instead of one call per receipt):

    GET companies({company_id})/purchaseReceiptLines
        ?$select=id,documentId&$expand=dimensionSetLines

and writes ONE CSV with: company_name, company_id, lineId, documentId,
costCenter, division.
"""

import csv
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

REQUEST_TIMEOUT = 120


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
        for _ in range(5):
            resp = self.http.get(
                url,
                headers=self.headers(),
                timeout=REQUEST_TIMEOUT,
                verify=VERIFY_SSL,
            )

            if resp.status_code == 401:
                print("[Auth] Token rejected - refreshing and retrying.")
                self.token = get_access_token()
                continue

            if resp.status_code in (429, 503):
                wait = int(resp.headers.get("Retry-After", 10))
                print(f"  [throttled] waiting {wait}s")
                time.sleep(wait)
                continue

            return resp

        return resp


# ============================================================
# FETCH
# ============================================================

def fetch_company(session, company_name, company_id):
    """Every receipt line of one company with its two dimension values."""

    url = (f"{BASE_URL}/companies({company_id})/purchaseReceiptLines"
           f"?$select=id,documentId&$expand=dimensionSetLines")
    rows = []

    while url:
        resp = session.get(url)
        resp.raise_for_status()
        data = resp.json()

        for line in data.get("value", []):
            values = {d["code"]: d["valueCode"]
                      for d in line.get("dimensionSetLines", [])}
            rows.append([company_name, company_id, line["id"],
                         line["documentId"],
                         values.get(COST_CENTER_CODE, ""),
                         values.get(DIVISION_CODE, "")])

        print(f"  {company_name}: {len(rows)} lines")
        url = data.get("@odata.nextLink")

    return rows


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

    all_rows = []
    failed = False

    for company_name, company_id in COMPANIES.items():
        print(f"\nCompany: {company_name}")
        try:
            all_rows.extend(fetch_company(session, company_name, company_id))
        except requests.exceptions.RequestException as e:
            failed = True
            print(f"  [error] {company_name}: {e}")
            if getattr(e, "response", None) is not None:
                print(f"  {e.response.text}")

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([COMPANY_NAME_COLUMN, COMPANY_ID_COLUMN,
                         "lineId", "documentId", "costCenter", "division"])
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows -> {OUTPUT_CSV}")
    if failed:
        sys.exit(1)
