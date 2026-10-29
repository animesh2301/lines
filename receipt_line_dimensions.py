"""
Add COST CENTER and DIVISION to purchase receipt lines, per company.

Reads the purchaseReceiptLines CSV (columns `company`, `id`, `documentId`),
calls BC once per receipt:

    GET companies({company})/purchaseReceipts({documentId})
        /purchaseReceiptLines?$expand=dimensionSetLines

and writes: company, lineId, documentId, costCenter, division.

`company` in the CSV can be the BC company id or its name. Names are turned
into ids with BC_COMPANY_ID - the same setting the function app uses:
    BC_COMPANY_ID = {"<company-guid>": "<company name>", ...}
If the CSV has no `company` column, BC_COMPANY_ID must hold one company.

Other settings (environment variables, never in the script):
    BC_TENANT_ID, BC_CLIENT_ID, BC_CLIENT_SECRET, BC_ENVIRONMENT

Usage:
    python receipt_line_dimensions.py receipt_lines.csv receipt_line_dims.csv
"""

import csv
import json
import os
import sys
import time

import msal
import requests

COST_CENTER = "COST CENTER"
DIVISION = "DIVISION"

BASE = (f"https://api.businesscentral.dynamics.com/v2.0/"
        f"{os.environ['BC_TENANT_ID']}/{os.environ['BC_ENVIRONMENT']}"
        f"/api/v2.0")

COMPANIES = json.loads(os.environ["BC_COMPANY_ID"])          # guid -> name
COMPANY_GUID = {name: guid for guid, name in COMPANIES.items()}

app = msal.ConfidentialClientApplication(
    os.environ["BC_CLIENT_ID"],
    client_credential=os.environ["BC_CLIENT_SECRET"],
    authority=f"https://login.microsoftonline.com/{os.environ['BC_TENANT_ID']}")


def token():
    result = app.acquire_token_for_client(
        scopes=["https://api.businesscentral.dynamics.com/.default"])
    return result["access_token"]


def get(url):
    """GET with a wait-and-retry when BC throttles (429/503)."""
    for _ in range(5):
        r = requests.get(url, headers={"Authorization": f"Bearer {token()}"},
                         timeout=60)
        if r.status_code in (429, 503):
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def company_guid(row):
    """BC company id for a CSV row: its `company` value (id or name)."""
    value = (row.get("company") or "").strip()
    if not value:
        if len(COMPANIES) != 1:
            sys.exit("CSV has no company column - BC_COMPANY_ID must hold "
                     "exactly one company")
        return next(iter(COMPANIES))
    if value in COMPANIES:
        return value
    if value in COMPANY_GUID:
        return COMPANY_GUID[value]
    sys.exit(f"Company '{value}' is not in BC_COMPANY_ID")


def main(in_csv, out_csv):
    with open(in_csv, newline="", encoding="utf-8-sig") as f:
        lines = list(csv.DictReader(f))
    receipts = sorted({(company_guid(row), row["documentId"]) for row in lines})
    print(f"{len(lines)} lines in {len(receipts)} receipts")

    dims = {}                    # (company, line id) -> (cost center, division)
    for n, (company, receipt_id) in enumerate(receipts, 1):
        url = (f"{BASE}/companies({company})/purchaseReceipts({receipt_id})"
               f"/purchaseReceiptLines?$expand=dimensionSetLines")
        while url:
            page = get(url)
            for line in page["value"]:
                values = {d["code"]: d["valueCode"]
                          for d in line.get("dimensionSetLines", [])}
                dims[(company, line["id"])] = (values.get(COST_CENTER, ""),
                                               values.get(DIVISION, ""))
            url = page.get("@odata.nextLink")
        print(f"  {n}/{len(receipts)} receipts done")

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "lineId", "documentId",
                         "costCenter", "division"])
        for row in lines:
            company = company_guid(row)
            cost_center, division = dims.get((company, row["id"]), ("", ""))
            writer.writerow([row.get("company") or COMPANIES[company],
                             row["id"], row["documentId"],
                             cost_center, division])
    print(f"Written {out_csv}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
