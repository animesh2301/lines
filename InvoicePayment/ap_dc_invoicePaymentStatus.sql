/* ===========================================================================
   dbo.ap_dc_invoicePaymentStatus - run once, BEFORE deploying the new
   function app. Steps 1-3 in order; step 4 any time later as a repair.
   =========================================================================== */

/* 1. Recreate the table from the purchase invoice staging table
      (the first version selected from bc.stg_accounts).
      record_id: change int to the type of dbo.AP_DataConversions.RECORD_ID
      if that isn't int. */
DROP TABLE IF EXISTS dbo.ap_dc_invoicePaymentStatus;

SELECT DISTINCT c.client_id, c.company_id, p.id, p.number, p.orderId,
       p.orderNumber, p.status, p.lastModifiedDateTime,
       CAST(NULL AS int) AS record_id
INTO dbo.ap_dc_invoicePaymentStatus
FROM bc.stg_purchaseInvoices p
JOIN dbo.mst_company c
  ON c.company_name = p.company_name
 AND c.client_id = 363;

/* 2. One row per BC invoice per company. Fails if step 1 produced
      duplicates - check mst_company for repeated company names. */
CREATE UNIQUE INDEX UX_ap_dc_invoicePaymentStatus
    ON dbo.ap_dc_invoicePaymentStatus (company_id, id);

/* 3. Link invoices the integration has already created to their CF record. */
UPDATE s
SET    s.record_id = l.RecordId
FROM   dbo.ap_dc_invoicePaymentStatus s
JOIN  (SELECT BcInvoiceId, MAX(RecordId) AS RecordId
       FROM   bc.InvoicePostLog
       WHERE  Step = 'create_header' AND Outcome = 'ok'
         AND  BcInvoiceId IS NOT NULL
       GROUP BY BcInvoiceId) l
  ON   l.BcInvoiceId = s.id
WHERE  s.record_id IS NULL;

/* 4. Check: rows per company and status, and how many are linked to CF.
      Step 3 can be re-run any time to fill a missing record_id (the
      function app never fails a posting over this table). */
SELECT company_id, status, COUNT(*) AS invoices,
       COUNT(record_id) AS linked_to_cf
FROM   dbo.ap_dc_invoicePaymentStatus
GROUP BY company_id, status
ORDER BY company_id, status;
