"""
Business Central -> CSV: COST CENTER and DIVISION for purchase receipt lines.

Reads the purchaseReceiptLines.csv written by extract.py (it has the
company_name / company_id columns), calls BC once per receipt:

    GET companies({company_id})/purchaseReceipts({documentId})
        /purchaseReceiptLines?$expand=dimensionSetLines

and writes ONE CSV with: company_name, company_id, lineId, documentId,
costCenter, division.
"""

import csv
import os
import sys
import time
import requests


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

OUTPUT_FOLDER = r"C:\Users\animesh.agarwal.QBSS\Downloads"

# Written by extract.py with "purchaseReceiptLines" in ENTITIES
INPUT_CSV = os.path.join(OUTPUT_FOLDER, "purchaseReceiptLines.csv")
OUTPUT_CSV = os.path.join(OUTPUT_FOLDER, "purchaseReceiptLineDimensions.csv")

# Dimension codes as they are set up in BC
COST_CENTER_CODE = "COST CENTER"
DIVISION_CODE = "DIVISION"

# Same column names extract.py stamps on every row
COMPANY_NAME_COLUMN = "company_name"
COMPANY_ID_COLUMN = "company_id"

VERIFY_SSL = True

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

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def get(self, url):
        for _ in range(5):
            resp = requests.get(
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

def fetch_receipt_dimensions(session, company_id, receipt_id):
    """{line id: (cost center, division)} for every line of one receipt."""

    url = (f"{BASE_URL}/companies({company_id})/purchaseReceipts({receipt_id})"
           f"/purchaseReceiptLines?$expand=dimensionSetLines")
    dims = {}

    while url:
        resp = session.get(url)
        resp.raise_for_status()

        data = resp.json()
        for line in data.get("value", []):
            values = {d["code"]: d["valueCode"]
                      for d in line.get("dimensionSetLines", [])}
            dims[line["id"]] = (values.get(COST_CENTER_CODE, ""),
                                values.get(DIVISION_CODE, ""))

        url = data.get("@odata.nextLink")

    return dims


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        lines = list(csv.DictReader(f))

    receipts = sorted({(row[COMPANY_NAME_COLUMN], row[COMPANY_ID_COLUMN],
                        row["documentId"]) for row in lines})
    print(f"{len(lines)} receipt lines in {len(receipts)} receipts\n")

    try:
        session = Session()
    except requests.exceptions.RequestException as e:
        print("\nCould not acquire token:")
        print(e)
        sys.exit(1)

    dims = {}          # (company_id, line id) -> (cost center, division)
    failed = 0

    for n, (company_name, company_id, receipt_id) in enumerate(receipts, 1):
        try:
            for line_id, values in fetch_receipt_dimensions(
                    session, company_id, receipt_id).items():
                dims[(company_id, line_id)] = values

        except requests.exceptions.RequestException as e:
            failed += 1
            print(f"  [error] {company_name} receipt {receipt_id}: {e}")
            if getattr(e, "response", None) is not None:
                print(f"  {e.response.text}")
            continue

        print(f"  {n}/{len(receipts)} {company_name} receipt {receipt_id}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([COMPANY_NAME_COLUMN, COMPANY_ID_COLUMN,
                         "lineId", "documentId", "costCenter", "division"])
        for row in lines:
            cost_center, division = dims.get(
                (row[COMPANY_ID_COLUMN], row["id"]), ("", ""))
            writer.writerow([row[COMPANY_NAME_COLUMN], row[COMPANY_ID_COLUMN],
                             row["id"], row["documentId"],
                             cost_center, division])

    print(f"\nWrote {len(lines)} rows -> {OUTPUT_CSV}")
    if failed:
        print(f"{failed} receipt(s) failed - their lines have empty dimensions")
        sys.exit(1)
