-- 012_accuracy.sql: labelled synthetic truth (tools/synth.py truth.csv) and the measured
-- accuracy of sp_reconcile against it. Test data only: synth_truth carries no RLS.

CREATE TABLE synth_truth (
    truth_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gstin             char(15) NOT NULL,
    books_invoice_no  text,                 -- raw number as written in the books CSV
    portal_invoice_no text,                 -- raw number as written in the 2B JSON
    perturbation      text NOT NULL CHECK (perturbation IN (
        'EXACT', 'FORMAT', 'TYPO', 'ROUNDING', 'DATE_SHIFT', 'VALUE', 'OTHER_STATE_GSTIN',
        'BOOKS_ONLY', '2B_ONLY', 'B2BA', 'DUPLICATE')),
    expected_status   match_status NOT NULL,
    CHECK (books_invoice_no IS NOT NULL OR portal_invoice_no IS NOT NULL)
);
CREATE INDEX synth_truth_key_idx ON synth_truth (gstin, books_invoice_no, portal_invoice_no);
COMMENT ON TABLE synth_truth IS 'Expected status per synthetic document (tools/synth.py truth.csv); test data';

-- Confusion matrix of expected vs predicted status over the CURRENT import of every period
-- of each GSTIN that has truth rows. A truth row pairs with a result on
-- (gstin, coalesce(books invoice_no, ''), coalesce(portal invoice_no, '')), one-to-one by
-- row_number() within the key (so a portal duplicate pairs once as EXACT, once as DUPLICATE).
-- predicted_status is NULL for truth rows with no result.
--   precision_pct = 100 * n / (truth-paired results predicted predicted_status)
--   recall_pct    = 100 * n / (truth rows expected expected_status)
-- On the diagonal (expected = predicted) these are the usual precision and recall.
CREATE VIEW v_accuracy WITH (security_invoker = true) AS
WITH res AS (
    SELECT p.gstin::text AS gstin,
           coalesce(i.invoice_no, '') AS bno,
           coalesce(e.invoice_no, '') AS pno,
           r.status,
           row_number() OVER (PARTITION BY p.gstin, coalesce(i.invoice_no, ''),
                                           coalesce(e.invoice_no, '')
                              ORDER BY r.status, r.result_id) AS rn
    FROM recon_result r
    JOIN gstr2b_import gi ON gi.import_id = r.import_id AND gi.is_current
    JOIN return_period p ON p.period_id = gi.period_id
    LEFT JOIN invoice i ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy
    LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id AND e.fin_year = r.entry_fy
    WHERE p.gstin IN (SELECT t.gstin FROM synth_truth t)
), tr AS (
    SELECT t.gstin::text AS gstin,
           coalesce(t.books_invoice_no, '') AS bno,
           coalesce(t.portal_invoice_no, '') AS pno,
           t.expected_status,
           row_number() OVER (PARTITION BY t.gstin, coalesce(t.books_invoice_no, ''),
                                           coalesce(t.portal_invoice_no, '')
                              ORDER BY t.expected_status, t.truth_id) AS rn
    FROM synth_truth t
), paired AS (
    SELECT tr.expected_status, res.status AS predicted_status
    FROM tr
    LEFT JOIN res ON res.gstin = tr.gstin AND res.bno = tr.bno AND res.pno = tr.pno
                 AND res.rn = tr.rn
)
SELECT expected_status, predicted_status, count(*) AS n,
       CASE WHEN predicted_status IS NOT NULL THEN
           round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY predicted_status), 2)
       END AS precision_pct,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY expected_status), 2)
           AS recall_pct
FROM paired
GROUP BY expected_status, predicted_status;

GRANT SELECT ON synth_truth, v_accuracy TO rc_partner;
