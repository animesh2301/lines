"""
Settings for the UAT scope: BC -> ContinuFlow tables, and non-PO invoice
posting back to BC.

The database holds data for many clients and companies. Every statement in
this app is scoped by CLIENT_ID and COMPANY_ID so it can never read or write
another client's rows.
"""

import json
import os


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Required app setting '{name}' is not set")
    return v


# ---------------------------------------------------------------------------
# Tenant scope - the single most important setting in this file
# ---------------------------------------------------------------------------

CLIENT_ID = int(_env("CF_CLIENT_ID", "363"))

# Companies in scope. 889 and 890 both belong to client 363.
COMPANY_IDS: list[int] = json.loads(_env("CF_COMPANY_IDS", "[889, 890]"))


def company_scope_sql(alias: str = "c") -> str:
    """
    Predicate restricting any statement to the configured client/company.
    Inlined rather than parameterised because it is built from ints this
    module validated, and it has to appear inside larger SQL templates.
    """
    ids = ",".join(str(int(i)) for i in COMPANY_IDS)
    return f"{alias}.CLIENT_ID = {int(CLIENT_ID)} AND {alias}.COMPANY_ID IN ({ids})"


# ---------------------------------------------------------------------------
# Business Central
# ---------------------------------------------------------------------------

API_HOST = "https://api.businesscentral.dynamics.com"
BC_ENVIRONMENT = _env("BC_ENVIRONMENT", "D365-BC-QA")
BC_API_BASE = f"{API_HOST}/v2.0/{BC_ENVIRONMENT}/api/v2.0"

# {company-guid: company-name} - the app's existing setting
BC_COMPANY_BY_GUID: dict[str, str] = json.loads(_env("BC_COMPANY_ID", "{}"))
# reverse, for posting
BC_GUID_BY_COMPANY: dict[str, str] = {
    name: guid for guid, name in BC_COMPANY_BY_GUID.items()
}

BC_HTTP_TIMEOUT = int(_env("BC_HTTP_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_SERVER = _env("SQL_SERVER", required=True)
SQL_DATABASE = _env("SQL_DATABASE", required=True)
SQL_USER = _env("SQL_USER", required=True)
SQL_PASSWORD = _env("SQL_PASSWORD", required=True)


# ---------------------------------------------------------------------------
# Direct load (BC -> ContinuFlow tables)
# ---------------------------------------------------------------------------

# Vendors, locations, accounts, purchase orders (+ lines) and purchase
# receipts (+ lines) are written straight into the dbo tables from the CF
# doc, with no bc.stg_* copy. paymentTerms, paymentMethods and items still
# land in bc.stg_* because the mappings look values up from them.
#
# Rows are updated in place and only inserted when new, so IDENTITY ids
# (Mst_Store.PROFIT_CENTER_ID in particular) never change, and only columns
# sourced from BC are overwritten - anything the CF team fills in the other
# columns is left alone.


def _company_map() -> dict[str, str]:
    # {"889": "<bc-company-guid>", "890": "<bc-company-guid>"}
    return json.loads(_env("CF_BC_COMPANY_MAP", "{}"))


def cf_company_id(bc_company_guid: str) -> int | None:
    """BC company GUID -> ContinuFlow COMPANY_ID, or None if not mapped."""
    for cid, guid in _company_map().items():
        if str(guid).lower() == str(bc_company_guid).lower():
            return int(cid)
    return None


# ---------------------------------------------------------------------------
# Non-PO invoice posting (ContinuFlow tables -> BC)
# ---------------------------------------------------------------------------

# Which AP_DataConversions rows to pick up
DC_STATUS = _env("AP_DC_STATUS", "APPROVED")
DC_ERP_POST_PENDING = _env("AP_DC_ERP_POST_PENDING", "N")
DC_ERP_POST_DONE = _env("AP_DC_ERP_POST_DONE", "P")   # posted (CF team)
# Written to ERP_POST once MAX_ATTEMPTS is exhausted, so the timer stops
# retrying a record that will never succeed.
DC_ERP_POST_FAILED = _env("AP_DC_ERP_POST_FAILED", "E")

# AP_DataConversions.ERP_ERROR is varchar(200); longer text fails the update.
ERP_ERROR_MAX = int(_env("AP_ERP_ERROR_MAX", "200"))

MAX_ATTEMPTS = int(_env("AP_POST_MAX_ATTEMPTS", "3"))
BATCH_SIZE = int(_env("AP_POST_BATCH_SIZE", "10"))

POST_CRON = _env("AP_POST_CRON", "0 */5 * * * *")      # every 5 minutes

# draft - create the invoice, its lines and its dimensions, do NOT post it.
#         ERP_POST is still set to 'P'. A BC user reviews and posts.
# post  - as above, then call Microsoft.NAV.post.
#
# Default is draft. A non-PO invoice has no PO authorising the spend, so
# invoice approval is the only control in that flow, and a posted document in
# BC is immutable.
POST_MODE = _env("AP_POST_MODE", "draft").lower()

# ---------------------------------------------------------------------------
# Invoice type routing (AP_DataConversions.INVOICE_TYPE)
# ---------------------------------------------------------------------------

def _upper_list(name, default):
    return [str(v).strip().upper() for v in json.loads(_env(name, default))]


# INVOICE_TYPE values that mean a PO invoice, compared trimmed and
# case-insensitively. Anything else (including empty) is non-PO, as before.
# Check the real values:
#   SELECT INVOICE_TYPE, COUNT(*) FROM dbo.AP_DataConversions
#   WHERE CLIENT_ID = 363 GROUP BY INVOICE_TYPE;
INVOICE_TYPES_PO = _upper_list("AP_INVOICE_TYPES_PO", '["PO"]')

# PO invoices (header + GL lines). Off: the timer skips them;
# a manual ?recordId= call still posts one so it can be tested.
PO_POSTING_ENABLED = _env("AP_POST_PO_ENABLED", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Posting description (client's custom API on purchaseInvoices)
# ---------------------------------------------------------------------------

# Mandatory in BC before posting, and only writable through the client's
# custom API. Set BC_CUSTOM_API_PATH to "<publisher>/<group>/<version>" from
# the URL they shared (or the whole base URL up to /companies). Unset = the
# PATCH is skipped and logged.
CUSTOM_API_PATH = (_env("BC_CUSTOM_API_PATH", "") or "").strip().strip("/")
CUSTOM_API_BASE = (
    CUSTOM_API_PATH if CUSTOM_API_PATH.startswith("https://")
    else f"{API_HOST}/v2.0/{BC_ENVIRONMENT}/api/{CUSTOM_API_PATH}"
    if CUSTOM_API_PATH else ""
)
CUSTOM_INVOICE_ENTITY = _env("BC_CUSTOM_INVOICE_ENTITY", "purchaseInvoices")
POSTING_DESC_FIELD = _env("BC_POSTING_DESC_FIELD", "postingDescription")
# The value comes from AP_DC_InvoiceHeader.DESCRIPTION (mandatory; an empty
# one holds the record).


# unitCost is sent in the line POST. Set true to also PATCH it afterwards,
# in case BC overwrites it from the account's defaults.
UNITCOST_PATCH = _env("AP_UNITCOST_PATCH", "false").lower() == "true"


# ---------------------------------------------------------------------------
# CF audit trail (dbo.ap_auditTrails)
# ---------------------------------------------------------------------------

# Row format agreed with the CF team (their sample row). {invoice_number}
# and {message} are filled in by the app. "refrence" is spelt as in their
# existing rows on purpose.
AUDIT_DESCRIPTION = _env("AP_AUDIT_DESCRIPTION", "Invoice Posting Status")
AUDIT_POSTED_COMMENTS = _env("AP_AUDIT_POSTED_COMMENTS",
                             "Invoice Posted with refrence # {invoice_number}")
AUDIT_POSTED_CHANGES = _env("AP_AUDIT_POSTED_CHANGES",
                            "Invoice status updated from pending to posted")
AUDIT_FAILED_COMMENTS = _env("AP_AUDIT_FAILED_COMMENTS",
                             "Invoice posting failed: {message}")
# ERP_POST stays pending (will be retried) / moved to the failed marker.
AUDIT_RETRY_CHANGES = _env("AP_AUDIT_RETRY_CHANGES",
                           "Invoice status remains pending")
AUDIT_FAILED_CHANGES = _env("AP_AUDIT_FAILED_CHANGES",
                            "Invoice status updated from pending to failed")

# APPROVED_BY - NULL in the CF sample; set AP_AUDIT_USER_ID to fill it.
_audit_user = (_env("AP_AUDIT_USER_ID", "") or "").strip()
AUDIT_USER_ID = int(_audit_user) if _audit_user else None

# Cut each text to the column's length so an insert never fails on size.
# Set these to the real varchar lengths of dbo.ap_auditTrails.
AUDIT_DESCRIPTION_MAX = int(_env("AP_AUDIT_DESCRIPTION_MAX", "100"))
AUDIT_COMMENTS_MAX = int(_env("AP_AUDIT_COMMENTS_MAX", "500"))
AUDIT_CHANGES_MAX = int(_env("AP_AUDIT_CHANGES_MAX", "4000"))


# ---------------------------------------------------------------------------
# Invoice PDF from ContinuFlow -> BC documentAttachments
# ---------------------------------------------------------------------------

CF_API_LOGIN_URL = _env("CF_API_LOGIN_URL",
                        "https://xportv2uam-dev.qbss.cloud/api/v1/auth/login")
CF_API_FILE_URL = _env(
    "CF_API_FILE_URL",
    "https://xportv2approcessing-dev.qbss.cloud"
    "/api/v1/ap-data-conversion/file-path/{record_id}")
CF_API_USERNAME = _env("CF_API_USERNAME", "")      # Key Vault reference
CF_API_PASSWORD = _env("CF_API_PASSWORD", "")      # Key Vault reference


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

# Posted to purchaseInvoiceLines({id})/dimensionSetLines after each line.
COST_CENTER_DIMENSION_CODE = _env("BC_COST_CENTER_DIM_CODE", "COST CENTER")
DIVISION_DIMENSION_CODE = _env("BC_DIVISION_DIM_CODE", "DIVISION")
# mst_store / mst_divisions are built from the values of the BC dimension
# whose displayName (or code) is this - the CF mapping's
# WHERE d.displayName = 'DIVISION'.
DIVISION_DIMENSION_NAME = _env("BC_DIVISION_DIM_NAME", "DIVISION")

POST_COST_CENTER = _env("AP_POST_COST_CENTER", "true").lower() == "true"

# DIVISION = AP_DC_GL_Details.DIVISION, as COST CENTER = COST_CENTER.
POST_DIVISION = _env("AP_POST_DIVISION", "true").lower() == "true"

# A dimension value BC does not recognise fails the whole line. When this is
# true a rejected dimension is logged and the line is kept; when false the
# invoice is held. True for UAT so one bad cost centre does not block a run.
DIMENSION_FAILURE_IS_FATAL = _env(
    "AP_DIMENSION_FAILURE_FATAL", "false").lower() == "true"


def bc_company_guid(company_id: int) -> str:
    """
    Map a ContinuFlow COMPANY_ID to a BC company GUID.

    CF_BC_COMPANY_MAP is {"889": "<bc-company-guid>", "890": "..."} and is
    required both ways - the DB knows COMPANY_ID, BC knows its own GUID, and
    nothing joins them automatically.
    """
    guid = _company_map().get(str(company_id))
    if not guid:
        raise ValueError(
            f"No CF_BC_COMPANY_MAP entry for COMPANY_ID {company_id}. "
            f"Set it to {{\"889\": \"<bc-guid>\", \"890\": \"<bc-guid>\"}}."
        )
    return guid
