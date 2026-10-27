"""
ContinuFlow tables -> Business Central: invoice posting.

AP_DataConversions.INVOICE_TYPE picks the path (run_batch):
  in AP_INVOICE_TYPES_PO   process_po_record - header + item lines (no PO link)
  anything else            process_record    - non-PO, header + Account lines

    AP_DataConversions   CLIENT_ID / COMPANY_ID in scope,
                         STATUS = 'APPROVED', ERP_POST = 'N'
        -> AP_DC_InvoiceHeader   (one header per RECORD_ID)
        -> AP_DC_GL_Details      (every row is an invoice line;
                                  COST_CENTER and DIVISION are dimensions)
            -> BC purchaseInvoices + purchaseInvoiceLines
               + dimensionSetLines for COST CENTER / DIVISION
        -> ERP_POST = 'P'

process_record is the non-PO code as it was, plus one call that sets the
posting description (client's custom API) before posting.

Retries are safe. Every BC identifier is written to bc.InvoicePostLog the
moment it exists, so a retry resumes the half-built invoice instead of
creating a second one.
"""

import json
import logging
import re
from decimal import Decimal

import requests

import uat_config as cfg
from bc_http import BcError, bc_get, bc_request

log = logging.getLogger("invoice_poster")

ZERO = Decimal("0")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class Held(Exception):
    """Needs a person. Not retried - ERP_POST is left pending and flagged."""


def _dec(v, default=ZERO) -> Decimal:
    if v is None or v == "":
        return default
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _rows(cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def is_po_invoice(dc: dict) -> bool:
    """PO if INVOICE_TYPE is in AP_INVOICE_TYPES_PO; anything else is non-PO."""
    t = str(dc.get("INVOICE_TYPE") or "").strip().upper()
    return t in cfg.INVOICE_TYPES_PO


def pending_records(conn, limit=None) -> list[dict]:
    limit = limit or cfg.BATCH_SIZE

    # PO invoices stay out of the timer's batch until AP_POST_PO_ENABLED,
    # so they can't take the batch slots that non-PO invoices need.
    po_filter, po_params = "", ()
    if not cfg.PO_POSTING_ENABLED and cfg.INVOICE_TYPES_PO:
        po_filter = (
            "AND (dc.INVOICE_TYPE IS NULL OR UPPER(LTRIM(RTRIM("
            "dc.INVOICE_TYPE))) NOT IN ("
            + ", ".join("%s" for _ in cfg.INVOICE_TYPES_PO) + "))")
        po_params = tuple(cfg.INVOICE_TYPES_PO)

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT TOP (%s) dc.RECORD_ID, dc.CLIENT_ID, dc.COMPANY_ID,
                        dc.DOC_NO, dc.INVOICE_TYPE, dc.STATUS, dc.ERP_POST
        FROM dbo.AP_DataConversions dc
        WHERE {cfg.company_scope_sql('dc')}
          AND dc.STATUS   = %s
          AND dc.ERP_POST = %s
          {po_filter}
        ORDER BY dc.RECORD_ID
        """,
        (limit, cfg.DC_STATUS, cfg.DC_ERP_POST_PENDING, *po_params),
    )
    return _rows(cur)


def load_purchase_order(conn, company_id, po_number) -> dict | None:
    """The PO from AP_DC_PurchaseOrder, for this company only."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 POnumber, POInternalID, vendorNumber, DocumentStatus
        FROM dbo.AP_DC_PurchaseOrder
        WHERE CompanyCode = %s AND POnumber = %s
        """,
        (company_id, po_number),
    )
    rows = _rows(cur)
    return rows[0] if rows else None


def load_header(conn, record_id) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 *
        FROM dbo.AP_DC_InvoiceHeader
        WHERE RECORD_ID = %s
        ORDER BY ID DESC
        """,
        (record_id,),
    )
    rows = _rows(cur)
    return rows[0] if rows else None


def load_gl_lines(conn, record_id) -> list[dict]:
    """
    Every AP_DC_GL_Details row for the record becomes one invoice line - no
    filtering. COST_CENTER and DIVISION are read from the same row.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM dbo.AP_DC_GL_Details
        WHERE RECORD_ID = %s
        ORDER BY SERIAL_NO, ID
        """,
        (record_id,),
    )
    return _rows(cur)


def resolve_account(conn, client_id, company_id, gl_code, bc_company_guid):
    """
    GLCODE (the account number, e.g. 61000) -> BC account id.

    COA_Master_SAP.ACCOUNT_NUMBER holds the BC account id. The same account
    number exists in both companies with a different id in each, so the
    lookup is always filtered on this invoice's CLIENT_ID and COMPANY_ID -
    never on the code alone.

    If COA_Master_SAP has no usable row, BC is asked directly, again in this
    invoice's company. Two different ids for one code in the same company is
    ambiguous and holds the invoice rather than guessing.
    """
    code = str(gl_code or "").strip()
    if not code:
        return None, "no GLCODE on the line"

    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ACCOUNT_NUMBER
        FROM dbo.COA_Master_SAP
        WHERE CLIENT_ID = %s AND COMPANY_ID = %s AND ACCOUNT_CODE = %s
          AND ACCOUNT_NUMBER IS NOT NULL
        """,
        (client_id, company_id, code),
    )
    ids = {str(r[0]).strip() for r in cur.fetchall()
           if r[0] and GUID_RE.match(str(r[0]).strip())}

    if len(ids) == 1:
        return ids.pop(), None
    if len(ids) > 1:
        return None, (f"G/L {code} has {len(ids)} different BC ids in "
                      f"COA_Master_SAP for company {company_id}: "
                      f"{sorted(ids)}")

    url = (f"{cfg.BC_API_BASE}/companies({bc_company_guid})/accounts"
           f"?$filter=number eq '{code.replace(chr(39), chr(39) * 2)}'")
    vals = (bc_get(url, timeout=cfg.BC_HTTP_TIMEOUT) or {}).get("value", [])
    if vals:
        return vals[0]["id"], None

    return None, (f"G/L account {code} not found for company {company_id} "
                  f"in COA_Master_SAP or in BC")


def col(row: dict, name: str):
    """Column value regardless of how the column name is cased in the DB."""
    if name in row:
        return row[name]
    lname = name.lower()
    for k, v in row.items():
        if k.lower() == lname:
            return v
    return None


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def cost_center_value(header: dict, line: dict):
    v = line.get("COST_CENTER")
    v = str(v).strip() if v is not None else ""
    return v or None


def division_value(header: dict, line: dict):
    v = line.get("DIVISION")
    v = str(v).strip() if v is not None else ""
    return v or None


def post_dimension(bc_company_guid, invoice_id, line_id, code, value_code):
    url = (f"{cfg.BC_API_BASE}/companies({bc_company_guid})"
           f"/purchaseInvoices({invoice_id})"
           f"/purchaseInvoiceLines({line_id})/dimensionSetLines")
    return bc_request("POST", url,
                      json_body={"code": code, "valueCode": str(value_code)},
                      timeout=cfg.BC_HTTP_TIMEOUT)


# ---------------------------------------------------------------------------
# Logging / write-back
# ---------------------------------------------------------------------------

def log_step(conn, record_id, attempt, step, outcome, detail=None,
             bc_invoice_id=None, bc_invoice_number=None, client_id=None,
             company_id=None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = str(detail)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bc.InvoicePostLog
            (RecordId, ClientId, CompanyId, Attempt, Step, Outcome, Detail,
             BcInvoiceId, BcInvoiceNumber)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (record_id, client_id, company_id, attempt, step, outcome,
         (detail or "")[:4000], bc_invoice_id, bc_invoice_number),
    )
    conn.commit()


def attempt_number(conn, record_id) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT ISNULL(MAX(Attempt), 0) + 1 FROM bc.InvoicePostLog "
        "WHERE RecordId = %s",
        (record_id,),
    )
    return int(cur.fetchone()[0])


def resume_invoice_id(conn, record_id) -> str | None:
    """
    An invoice this record already created in a previous attempt. Without
    this, a retry after a mid-way failure would create a second invoice in BC.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 BcInvoiceId
        FROM bc.InvoicePostLog
        WHERE RecordId = %s AND Step = 'create_header' AND Outcome = 'ok'
              AND BcInvoiceId IS NOT NULL
        ORDER BY LogId DESC
        """,
        (record_id,),
    )
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


# ---------------------------------------------------------------------------
# CF audit trail (dbo.AP_AuditTrails) - one row per final outcome
# ---------------------------------------------------------------------------

def _latest_invoice_ref(conn, record_id) -> tuple[str | None, str | None]:
    """(BC invoice id, BC invoice number) last logged for this record."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 BcInvoiceId, BcInvoiceNumber
        FROM bc.InvoicePostLog
        WHERE RecordId = %s AND BcInvoiceId IS NOT NULL
        ORDER BY CASE WHEN BcInvoiceNumber IS NULL THEN 1 ELSE 0 END,
                 LogId DESC
        """,
        (record_id,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def write_audit_trail(conn, record_id, comments, changes) -> None:
    """
    One row in the CF team's dbo.AP_AuditTrails, in their format:

      DESCRIPTION  'Invoice Posting Status'
      COMMENTS     'Invoice Posted with refrence # PINV-0180460'  (success)
      CHANGES      'Invoice status updated from pending to posted'
      CREATED_ON   now; APPROVED_BY, CREATED_BY, LASTMODIFIED_BY,
                   LASTMODIFIED_ON, ASSIGNED_TO = NULL
      ID           not sent - IDENTITY

    Never raises: ERP_POST is already committed when this runs, and a failed
    audit insert must not turn a posted invoice into a retry. Failures are
    logged ("Could not write AP_AuditTrails row").
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.AP_AuditTrails
                ([DESCRIPTION], [APPROVED_BY], [COMMENTS], [CREATED_BY],
                 [CREATED_ON], [LASTMODIFIED_BY], [LASTMODIFIED_ON],
                 [RECORD_ID], [ASSIGNED_TO], [CHANGES])
            VALUES (%s, %s, %s, NULL,
                    CAST(SYSDATETIMEOFFSET() AT TIME ZONE
                         'Eastern Standard Time' AS smalldatetime),
                    NULL, NULL, %s, NULL, %s)
            """,
            (cfg.AUDIT_DESCRIPTION[:cfg.AUDIT_DESCRIPTION_MAX],
             cfg.AUDIT_USER_ID,
             str(comments)[:cfg.AUDIT_COMMENTS_MAX] if comments else None,
             record_id,
             str(changes)[:cfg.AUDIT_CHANGES_MAX] if changes else None),
        )
        conn.commit()
    except Exception:
        log.exception("Could not write AP_AuditTrails row for record %s",
                      record_id)
        try:
            conn.rollback()
        except Exception:
            pass


def mark_posted(conn, record_id) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.AP_DataConversions
           SET ERP_POST = %s, ERP_ERROR = NULL
         WHERE RECORD_ID = %s
        """,
        (cfg.DC_ERP_POST_DONE, record_id),
    )
    conn.commit()

    # NEW - CF audit trail
    inv_id, inv_no = _latest_invoice_ref(conn, record_id)
    write_audit_trail(
        conn, record_id,
        cfg.AUDIT_POSTED_COMMENTS.format(invoice_number=inv_no or inv_id or ""),
        cfg.AUDIT_POSTED_CHANGES)


def short_error(message: str) -> str:
    """
    BC's own error text when the message carries BC's JSON response
    ("POST https://... -> 400 :: {"error": {"code": ..., "message": ...}}"),
    otherwise the message as is. The URL alone would fill a 200-char column.
    """
    text = str(message or "")
    if ":: {" in text:
        prefix, _, body = text.partition(":: ")
        try:
            err = json.loads(body).get("error", {})
            status = prefix.strip().rsplit(" ", 1)[-1]   # e.g. 400
            msg = err.get("message") or ""
            if msg:
                return f"BC {status}: {msg}"
        except (ValueError, AttributeError):
            pass
    return text


def mark_error(conn, record_id, message, attempt) -> None:
    """
    Latest error to ERP_ERROR so it shows in the ContinuFlow UI; full history
    stays in bc.InvoicePostLog. ERP_POST goes to the failed marker once the
    attempt cap is reached so the timer stops retrying.

    Never raises: a failure here (e.g. a column too short) is logged and the
    batch carries on with the next record.
    """
    exhausted = attempt >= cfg.MAX_ATTEMPTS
    new_state = cfg.DC_ERP_POST_FAILED if exhausted \
        else cfg.DC_ERP_POST_PENDING
    short = short_error(message)

    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.AP_DataConversions
               SET ERP_ERROR = %s, ERP_POST = %s
             WHERE RECORD_ID = %s
            """,
            (f"[attempt {attempt}] {short}"[:cfg.ERP_ERROR_MAX],
             new_state, record_id),
        )
        conn.commit()
    except Exception:
        log.exception("Could not write ERP_ERROR for record %s", record_id)
        try:
            conn.rollback()
        except Exception:
            pass

    # NEW - CF audit trail
    write_audit_trail(
        conn, record_id,
        cfg.AUDIT_FAILED_COMMENTS.format(message=short),
        cfg.AUDIT_FAILED_CHANGES
        if new_state != cfg.DC_ERP_POST_PENDING else cfg.AUDIT_RETRY_CHANGES)
    return exhausted


# ---------------------------------------------------------------------------
# Posting description (client's custom API) - used by both paths
# ---------------------------------------------------------------------------

def set_posting_description(bc_guid, invoice_id, step, description) -> None:
    """
    PATCH the mandatory posting description (AP_DC_InvoiceHeader.DESCRIPTION)
    through the client's custom API, before Microsoft.NAV.post. Idempotent,
    so it runs on every attempt and a resumed invoice gets it too. Skipped
    while BC_CUSTOM_API_PATH is not set.
    """
    if not cfg.CUSTOM_API_BASE:
        step("posting_description", "skipped", "BC_CUSTOM_API_PATH not set")
        return
    bc_request(
        "PATCH",
        f"{cfg.CUSTOM_API_BASE}/companies({bc_guid})"
        f"/{cfg.CUSTOM_INVOICE_ENTITY}({invoice_id})",
        json_body={cfg.POSTING_DESC_FIELD: description},
        etag="*", timeout=cfg.BC_HTTP_TIMEOUT)
    step("posting_description", "ok",
         f"{cfg.POSTING_DESC_FIELD} = {description!r}")


def fetch_invoice_pdf(record_id) -> bytes:
    """Invoice file from the ContinuFlow API: login -> signed URL -> file."""
    login = requests.post(cfg.CF_API_LOGIN_URL, timeout=60, json={
        "loginBody": {"Source": "password",
                      "UserName": cfg.CF_API_USERNAME,
                      "Password": cfg.CF_API_PASSWORD}})
    login.raise_for_status()
    body = login.json()
    token = (body.get("token") or body.get("accessToken")
             or (body.get("data") or {}).get("token"))

    r = requests.get(cfg.CF_API_FILE_URL.format(record_id=record_id),
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    signed_url = body if isinstance(body, str) else body.get("data")

    file = requests.get(signed_url, timeout=60)
    file.raise_for_status()
    return file.content


def attach_invoice_pdf(bc_guid, invoice_id, record_id, step) -> None:
    """Attach the invoice PDF to the BC purchase invoice (once)."""
    base = f"{cfg.BC_API_BASE}/companies({bc_guid})/documentAttachments"
    existing = bc_get(f"{base}?$filter=parentId eq {invoice_id}",
                      timeout=cfg.BC_HTTP_TIMEOUT) or {}
    if existing.get("value"):
        step("attachment", "skipped", "already attached")
        return

    content = fetch_invoice_pdf(record_id)
    att = bc_request("POST", base, timeout=cfg.BC_HTTP_TIMEOUT, json_body={
        "fileName": f"{record_id}.pdf",
        "parentType": "Purchase_x0020_Invoice",
        "parentId": invoice_id})
    bc_request("PATCH", f"{base}({att['id']})/attachmentContent",
               data=content, etag="*", timeout=cfg.BC_HTTP_TIMEOUT)
    step("attachment", "ok", f"{record_id}.pdf, {len(content)} bytes")


def posting_description(header: dict) -> str:
    """DESCRIPTION from the invoice header, max 100 chars (BC's field size).
    Mandatory for posting in BC, so an empty one holds the record before
    anything is created in BC."""
    desc = str(header.get("DESCRIPTION") or "").strip()[:100]
    if not desc:
        raise Held("DESCRIPTION is empty on AP_DC_InvoiceHeader")
    return desc


# ---------------------------------------------------------------------------
# One record - non-PO (unchanged apart from the posting description call)
# ---------------------------------------------------------------------------

def process_record(conn, dc: dict) -> dict:
    record_id = dc["RECORD_ID"]
    client_id = dc["CLIENT_ID"]
    company_id = dc["COMPANY_ID"]

    attempt = attempt_number(conn, record_id)

    def step(name, outcome, detail=None, inv_id=None, inv_no=None):
        log_step(conn, record_id, attempt, name, outcome, detail,
                 inv_id, inv_no, client_id, company_id)

    try:
        bc_guid = cfg.bc_company_guid(company_id)

        header = load_header(conn, record_id)
        if header is None:
            raise Held(f"no AP_DC_InvoiceHeader row for RECORD_ID {record_id}")

        post_desc = posting_description(header)

        lines = load_gl_lines(conn, record_id)
        step("load", "ok",
             f"header ID {header.get('ID')}, {len(lines)} line(s) to post")

        if not lines:
            raise Held("no AP_DC_GL_Details rows for this record")

        # ---- resolve every account before creating anything -------------
        resolved = []
        for l in lines:
            account_id, err = resolve_account(
                conn, client_id, company_id, l.get("GLCODE"), bc_guid)
            if err:
                raise Held(f"line SERIAL_NO {l.get('SERIAL_NO')}: {err}")
            resolved.append((l, account_id))
        step("resolve_accounts", "ok", f"{len(resolved)} account(s)")

        # ---- header (resume if a previous attempt made one) -------------
        invoice_id = resume_invoice_id(conn, record_id)
        if invoice_id:
            step("create_header", "skipped",
                 f"resuming invoice {invoice_id} from an earlier attempt",
                 inv_id=invoice_id)
        else:
            body = {"vendorNumber": str(header.get("VEND_CODE") or "").strip()}
            if header.get("INVOICE_NUMBER"):
                body["vendorInvoiceNumber"] = str(header["INVOICE_NUMBER"])
            if header.get("INVOICE_DATE"):
                body["invoiceDate"] = str(header["INVOICE_DATE"])[:10]
            if header.get("POSTING_DATE"):
                body["postingDate"] = str(header["POSTING_DATE"])[:10]
            if header.get("CURRENCY"):
                body["currencyCode"] = str(header["CURRENCY"]).strip()

            if not body["vendorNumber"]:
                raise Held("VEND_CODE is empty on AP_DC_InvoiceHeader")

            created = bc_request(
                "POST",
                f"{cfg.BC_API_BASE}/companies({bc_guid})/purchaseInvoices",
                json_body=body, timeout=cfg.BC_HTTP_TIMEOUT)
            invoice_id = created["id"]
            step("create_header", "ok", str(body),
                 inv_id=invoice_id, inv_no=created.get("number"))

        # ---- lines ------------------------------------------------------
        existing = (bc_get(
            f"{cfg.BC_API_BASE}/companies({bc_guid})"
            f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
            timeout=cfg.BC_HTTP_TIMEOUT) or {}).get("value", [])

        for idx, (l, account_id) in enumerate(resolved):
            if idx < len(existing):
                continue        # created by an earlier attempt

            amount = _dec(l.get("AMOUNT"))
            description = (l.get("DESCRIPTION") or l.get("GL_DESCR") or "")
            description = str(description)[:100]

            line_body = {
                "lineType": "Account",
                "accountId": account_id,
                "description": description,
                "quantity": 1,
                "unitCost": float(amount),
            }
            new_line = bc_request(
                "POST",
                f"{cfg.BC_API_BASE}/companies({bc_guid})"
                f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
                json_body=line_body,
                timeout=cfg.BC_HTTP_TIMEOUT)
            stored = (new_line or {}).get("unitCost")

            # Fallback, off by default: if BC's field validation ever
            # overwrites unitCost from the account again, set
            # AP_UNITCOST_PATCH=true to re-apply it with a second call.
            if cfg.UNITCOST_PATCH:
                patched = bc_request(
                    "PATCH",
                    f"{cfg.BC_API_BASE}/companies({bc_guid})"
                    f"/purchaseInvoiceLines({new_line['id']})",
                    json_body={"unitCost": float(amount)}, etag="*",
                    timeout=cfg.BC_HTTP_TIMEOUT)
                stored = (patched or {}).get("unitCost")

            step(f"line_{l.get('SERIAL_NO') or idx + 1}", "ok",
                 f"GL {l.get('GLCODE')} requested {amount} stored {stored}")

            # ---- dimensions ---------------------------------------------
            for enabled, code, value in (
                (cfg.POST_COST_CENTER, cfg.COST_CENTER_DIMENSION_CODE,
                 cost_center_value(header, l)),
                (cfg.POST_DIVISION, cfg.DIVISION_DIMENSION_CODE,
                 division_value(header, l)),
            ):
                if not enabled:
                    continue
                if not value:
                    step(f"dim_{code}", "skipped", "no value on the line")
                    continue
                try:
                    post_dimension(bc_guid, invoice_id, new_line["id"],
                                   code, value)
                    step(f"dim_{code}", "ok", f"{code} = {value}")
                except BcError as e:
                    step(f"dim_{code}", "error",
                         f"{code} = {value}: {e.body or e}")
                    if cfg.DIMENSION_FAILURE_IS_FATAL:
                        raise Held(
                            f"dimension {code} = {value} rejected by BC: "
                            f"{e.body or e}")

        # ---- invoice PDF (NEW - ContinuFlow file API) --------------------
        attach_invoice_pdf(bc_guid, invoice_id, record_id, step)

        # ---- posting description (NEW - client's custom API) ------------
        set_posting_description(bc_guid, invoice_id, step, post_desc)

        invoice = bc_get(f"{cfg.BC_API_BASE}/companies({bc_guid})"
                         f"/purchaseInvoices({invoice_id})",
                         timeout=cfg.BC_HTTP_TIMEOUT) or {}

        # ---- post, or leave as a draft ----------------------------------
        if cfg.POST_MODE == "post":
            bc_request("POST",
                       f"{cfg.BC_API_BASE}/companies({bc_guid})"
                       f"/purchaseInvoices({invoice_id})/Microsoft.NAV.post",
                       timeout=cfg.BC_HTTP_TIMEOUT)
            step("post_invoice", "ok", invoice.get("number"),
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "posted"
        else:
            step("post_invoice", "skipped",
                 "AP_POST_MODE=draft - a BC user reviews and posts",
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "draft_created"

        mark_posted(conn, record_id)
        step("erp_post", "ok", f"ERP_POST set to {cfg.DC_ERP_POST_DONE}")

        return {
            "recordId": record_id,
            "status": outcome,
            "invoiceId": invoice_id,
            "invoiceNumber": invoice.get("number"),
            "lines": len(resolved),
            "totalAmountExcludingTax": invoice.get("totalAmountExcludingTax"),
        }

    except Held as h:
        step("held", "held", str(h))
        exhausted = mark_error(conn, record_id, f"HELD: {h}", attempt)
        log.warning("Record %s held: %s", record_id, h)
        return {"recordId": record_id, "status": "held", "reason": str(h),
                "attempt": attempt, "retriesExhausted": exhausted}

    except BcError as e:
        detail = f"{e} :: {e.body}"
        step("bc_error", "error", detail)
        exhausted = mark_error(conn, record_id, detail, attempt)
        log.exception("Record %s BC error", record_id)
        return {"recordId": record_id, "status": "bc_error", "error": str(e),
                "bcResponse": e.body, "attempt": attempt,
                "retriesExhausted": exhausted}

    except Exception as e:
        step("error", "error", str(e))
        exhausted = mark_error(conn, record_id, str(e), attempt)
        log.exception("Record %s failed", record_id)
        return {"recordId": record_id, "status": "error", "error": str(e),
                "attempt": attempt, "retriesExhausted": exhausted}


# ---------------------------------------------------------------------------
# One record - PO (NEW): header + item lines, not linked to PO receipts
# ---------------------------------------------------------------------------

def add_po_lines(bc_guid, invoice_id, header, lines, step) -> None:
    """
    One Item line per AP_DC_GL_Details row, straight from the table:
      ITEM -> lineObjectNumber, QUANTITY -> quantity, UNIT_COST -> unitCost,
      UNIT_OF_MEASURE -> unitOfMeasureCode, DESCRIPTION -> description,
      + COST CENTER / DIVISION dimensions.
    Lines an earlier attempt already created are skipped, so a retry never
    duplicates them.
    """
    existing = (bc_get(
        f"{cfg.BC_API_BASE}/companies({bc_guid})"
        f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
        timeout=cfg.BC_HTTP_TIMEOUT) or {}).get("value", [])

    for idx, l in enumerate(lines):
        if idx < len(existing):
            continue        # created by an earlier attempt

        body = {
            "lineType": "Item",
            "lineObjectNumber": str(col(l, "ITEM") or "").strip(),
            "description": str(col(l, "DESCRIPTION") or "")[:100],
            "quantity": float(_dec(col(l, "QUANTITY"))),
            "unitCost": float(_dec(col(l, "UNIT_COST"))),
            "unitOfMeasureCode": str(col(l, "UNIT_OF_MEASURE") or "").strip(),
        }

        new_line = bc_request(
            "POST",
            f"{cfg.BC_API_BASE}/companies({bc_guid})"
            f"/purchaseInvoices({invoice_id})/purchaseInvoiceLines",
            json_body=body, timeout=cfg.BC_HTTP_TIMEOUT)
        step(f"line_{col(l, 'SERIAL_NO') or idx + 1}", "ok", str(body))

        for enabled, code, value in (
            (cfg.POST_COST_CENTER, cfg.COST_CENTER_DIMENSION_CODE,
             cost_center_value(header, l)),
            (cfg.POST_DIVISION, cfg.DIVISION_DIMENSION_CODE,
             division_value(header, l)),
        ):
            if not enabled or not value:
                continue
            try:
                post_dimension(bc_guid, invoice_id, new_line["id"],
                               code, value)
                step(f"dim_{code}", "ok", f"{code} = {value}")
            except BcError as e:
                step(f"dim_{code}", "error",
                     f"{code} = {value}: {e.body or e}")
                if cfg.DIMENSION_FAILURE_IS_FATAL:
                    raise


def process_po_record(conn, dc: dict) -> dict:
    """
    PO invoice, one PO per invoice (AP_DC_InvoiceHeader.PO_NUMBER):

      1. PO checks - the PO exists in AP_DC_PurchaseOrder for this company
         and belongs to the invoice's vendor
      2. header, as for non-PO (vendor from the PO when VEND_CODE is empty)
      3. lines from AP_DC_GL_Details - one Item line per row (ITEM,
         QUANTITY, UNIT_COST, UNIT_OF_MEASURE, DESCRIPTION) + COST CENTER /
         DIVISION
      4. posting description, post or draft, ERP_POST -> done, audit row
    """
    record_id = dc["RECORD_ID"]
    client_id = dc["CLIENT_ID"]
    company_id = dc["COMPANY_ID"]

    attempt = attempt_number(conn, record_id)

    def step(name, outcome, detail=None, inv_id=None, inv_no=None):
        log_step(conn, record_id, attempt, name, outcome, detail,
                 inv_id, inv_no, client_id, company_id)

    try:
        bc_guid = cfg.bc_company_guid(company_id)

        header = load_header(conn, record_id)
        if header is None:
            raise Held(f"no AP_DC_InvoiceHeader row for RECORD_ID {record_id}")

        post_desc = posting_description(header)

        # ---- PO checks --------------------------------------------------
        po_number = str(header.get("PO_NUMBER") or "").strip()
        if not po_number:
            raise Held("PO invoice with no PO_NUMBER on AP_DC_InvoiceHeader")

        po = load_purchase_order(conn, company_id, po_number)
        if po is None:
            raise Held(f"PO {po_number} not found in AP_DC_PurchaseOrder "
                       f"for company {company_id}")

        vendor = str(header.get("VEND_CODE") or "").strip()
        po_vendor = str(po.get("vendorNumber") or "").strip()
        if vendor and po_vendor and vendor.upper() != po_vendor.upper():
            raise Held(f"invoice vendor {vendor} does not match PO "
                       f"{po_number} vendor {po_vendor}")
        vendor = vendor or po_vendor
        if not vendor:
            raise Held("no vendor on the invoice header or on the PO")

        # ---- lines -------------------------------------------------------
        lines = load_gl_lines(conn, record_id)
        if not lines:
            raise Held("no AP_DC_GL_Details rows for this record")

        step("load", "ok",
             f"PO invoice, header ID {header.get('ID')}, PO {po_number}, "
             f"vendor {vendor}, {len(lines)} line(s)")

        # ---- header (resume if a previous attempt made one) -------------
        invoice_id = resume_invoice_id(conn, record_id)
        if invoice_id:
            step("create_header", "skipped",
                 f"resuming invoice {invoice_id} from an earlier attempt",
                 inv_id=invoice_id)
        else:
            body = {"vendorNumber": vendor}
            if header.get("INVOICE_NUMBER"):
                body["vendorInvoiceNumber"] = str(header["INVOICE_NUMBER"])
            if header.get("INVOICE_DATE"):
                body["invoiceDate"] = str(header["INVOICE_DATE"])[:10]
            if header.get("POSTING_DATE"):
                body["postingDate"] = str(header["POSTING_DATE"])[:10]
            if header.get("CURRENCY"):
                body["currencyCode"] = str(header["CURRENCY"]).strip()

            created = bc_request(
                "POST",
                f"{cfg.BC_API_BASE}/companies({bc_guid})/purchaseInvoices",
                json_body=body, timeout=cfg.BC_HTTP_TIMEOUT)
            invoice_id = created["id"]
            step("create_header", "ok", str(body),
                 inv_id=invoice_id, inv_no=created.get("number"))

        # ---- lines + dimensions -----------------------------------------
        add_po_lines(bc_guid, invoice_id, header, lines, step)

        # ---- invoice PDF --------------------------------------------------
        attach_invoice_pdf(bc_guid, invoice_id, record_id, step)

        # ---- posting description ----------------------------------------
        set_posting_description(bc_guid, invoice_id, step, post_desc)

        invoice = bc_get(f"{cfg.BC_API_BASE}/companies({bc_guid})"
                         f"/purchaseInvoices({invoice_id})",
                         timeout=cfg.BC_HTTP_TIMEOUT) or {}

        # ---- post, or leave as a draft ----------------------------------
        if cfg.POST_MODE == "post":
            bc_request("POST",
                       f"{cfg.BC_API_BASE}/companies({bc_guid})"
                       f"/purchaseInvoices({invoice_id})/Microsoft.NAV.post",
                       timeout=cfg.BC_HTTP_TIMEOUT)
            step("post_invoice", "ok", invoice.get("number"),
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "posted"
        else:
            step("post_invoice", "skipped",
                 "AP_POST_MODE=draft - a BC user reviews and posts",
                 inv_id=invoice_id, inv_no=invoice.get("number"))
            outcome = "draft_created"

        mark_posted(conn, record_id)
        step("erp_post", "ok", f"ERP_POST set to {cfg.DC_ERP_POST_DONE}")

        return {
            "recordId": record_id,
            "invoiceType": "PO",
            "status": outcome,
            "invoiceId": invoice_id,
            "invoiceNumber": invoice.get("number"),
            "poNumber": po_number,
            "vendorNumber": vendor,
            "lines": len(lines),
            "totalAmountExcludingTax": invoice.get("totalAmountExcludingTax"),
        }

    except Held as h:
        step("held", "held", str(h))
        exhausted = mark_error(conn, record_id, f"HELD: {h}", attempt)
        log.warning("PO record %s held: %s", record_id, h)
        return {"recordId": record_id, "status": "held", "reason": str(h),
                "attempt": attempt, "retriesExhausted": exhausted}

    except BcError as e:
        detail = f"{e} :: {e.body}"
        step("bc_error", "error", detail)
        exhausted = mark_error(conn, record_id, detail, attempt)
        log.exception("PO record %s BC error", record_id)
        return {"recordId": record_id, "status": "bc_error", "error": str(e),
                "bcResponse": e.body, "attempt": attempt,
                "retriesExhausted": exhausted}

    except Exception as e:
        step("error", "error", str(e))
        exhausted = mark_error(conn, record_id, str(e), attempt)
        log.exception("PO record %s failed", record_id)
        return {"recordId": record_id, "status": "error", "error": str(e),
                "attempt": attempt, "retriesExhausted": exhausted}


def run_batch(conn, limit=None, record_id=None) -> dict:
    """
    record_id given -> just that one, ignoring STATUS/ERP_POST, for UAT.
    Otherwise everything pending, up to the batch size.
    """
    if record_id is not None:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT dc.RECORD_ID, dc.CLIENT_ID, dc.COMPANY_ID, dc.DOC_NO,
                   dc.INVOICE_TYPE, dc.STATUS, dc.ERP_POST
            FROM dbo.AP_DataConversions dc
            WHERE dc.RECORD_ID = %s AND {cfg.company_scope_sql('dc')}
            """,
            (record_id,),
        )
        records = _rows(cur)
        if not records:
            return {"picked": 0,
                    "note": f"RECORD_ID {record_id} not found within "
                            f"CLIENT_ID {cfg.CLIENT_ID} / companies "
                            f"{cfg.COMPANY_IDS}",
                    "results": []}
    else:
        records = pending_records(conn, limit)

    results = []
    for dc in records:
        # One record's failure never stops the others.
        try:
            if is_po_invoice(dc):
                results.append(process_po_record(conn, dc))
            else:
                results.append(process_record(conn, dc))
        except Exception as e:
            log.exception("Record %s failed outside its own error handling",
                          dc.get("RECORD_ID"))
            try:
                conn.rollback()
            except Exception:
                pass
            results.append({"recordId": dc.get("RECORD_ID"),
                            "status": "error", "error": str(e)[:500]})

    return {
        "picked": len(records),
        "mode": cfg.POST_MODE,
        "poPostingEnabled": cfg.PO_POSTING_ENABLED,
        "results": results,
    }
