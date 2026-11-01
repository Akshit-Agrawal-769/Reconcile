-- 007_matching_more.sql: matching passes 3-6 (FUZZY_NO, VALUE, PAN, CROSS_PERIOD) and the
-- naive FULL OUTER JOIN baseline. sp_reconcile is replaced with all passes in spec §6 order;
-- passes 0, 1, 2 and 7 are unchanged from 006.
-- Every pass keys on the DOCUMENT financial year: fin_year_of(e.invoice_date) = b.fin_year
-- (gstr2b_entry.fin_year is the return period's FY, not the document's).

CREATE OR REPLACE FUNCTION sp_reconcile(p_import_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
    v_period   return_period%ROWTYPE;
    v_start    date;
    v_allowed  boolean;
    v_tol      numeric(14,2);
    v_window   int;
    v_maxdist  int;
    v_n        int;
    v_total    int := 0;
BEGIN
    -- Read period and tolerances once: period -> gstin_reg -> client.
    SELECT p.* INTO v_period
    FROM gstr2b_import i JOIN return_period p ON p.period_id = i.period_id
    WHERE i.import_id = p_import_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown import %', p_import_id;
    END IF;
    v_start := period_start(v_period.period);
    SELECT t.itc_allowed, c.rounding_tol, c.date_window_days, c.fuzzy_max_dist
      INTO v_allowed, v_tol, v_window, v_maxdist
    FROM gstin_reg g
    JOIN client c ON c.client_id = g.client_id
    JOIN taxpayer_type t ON t.code = g.taxpayer_type
    WHERE g.gstin = v_period.gstin;
    IF NOT v_allowed THEN
        RAISE EXCEPTION 'composition dealers cannot claim ITC';
    END IF;

    DELETE FROM recon_result WHERE import_id = p_import_id;

    -- ===== Pass 0 PREFLIGHT: later entries sharing a natural key are DUPLICATE =====
    INSERT INTO recon_result(import_id, entry_id, entry_fy, status, matched_by, diff_tax)
    SELECT p_import_id, d.entry_id, d.fin_year, 'DUPLICATE', 'PREFLIGHT', NULL
    FROM (
        SELECT e.entry_id, e.fin_year,
               row_number() OVER (PARTITION BY e.party_gstin, e.doc_type, e.invoice_no_norm,
                                               fin_year_of(e.invoice_date)
                                  ORDER BY e.entry_id) AS rn
        FROM gstr2b_entry e WHERE e.import_id = p_import_id
    ) d
    WHERE d.rn > 1
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 1 EXACT: same key, same date, every head equal =====
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT DISTINCT ON (e.entry_id)
           p_import_id, b.invoice_id, b.fin_year, e.entry_id, e.fin_year,
           'EXACT', 'EXACT', 0
    FROM v_books_candidate b
    JOIN gstr2b_entry e ON e.import_id = p_import_id
                       AND e.party_gstin = b.party_gstin AND e.doc_type = b.doc_type
                       AND e.invoice_no_norm = b.invoice_no_norm
                       AND fin_year_of(e.invoice_date) = b.fin_year
                       AND e.invoice_date = b.invoice_date
                       AND e.igst = b.igst AND e.cgst = b.cgst
                       AND e.sgst = b.sgst AND e.cess = b.cess
    WHERE b.period_id = v_period.period_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = b.invoice_id)
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ORDER BY e.entry_id, b.invoice_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 2 ROUNDING: same key, date within window, every head within tolerance =====
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT DISTINCT ON (e.entry_id)
           p_import_id, b.invoice_id, b.fin_year, e.entry_id, e.fin_year,
           'ROUNDING', 'ROUNDING',
           b.total_tax - (e.igst + e.cgst + e.sgst + e.cess)
    FROM v_books_candidate b
    JOIN gstr2b_entry e ON e.import_id = p_import_id
                       AND e.party_gstin = b.party_gstin AND e.doc_type = b.doc_type
                       AND e.invoice_no_norm = b.invoice_no_norm
                       AND fin_year_of(e.invoice_date) = b.fin_year
                       AND abs(e.invoice_date - b.invoice_date) <= v_window
                       AND abs(b.igst - e.igst) <= v_tol AND abs(b.cgst - e.cgst) <= v_tol
                       AND abs(b.sgst - e.sgst) <= v_tol AND abs(b.cess - e.cess) <= v_tol
    WHERE b.period_id = v_period.period_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = b.invoice_id)
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ORDER BY e.entry_id, b.invoice_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 3 FUZZY_NO: near-identical numbers, date window, heads within tolerance =====
    -- dist = levenshtein_less_equal capped at fuzzy_max_dist + 1; a pair qualifying only by
    -- containment (one normalised number inside the other) therefore ranks after every
    -- edit-distance pair. Only mutual-best pairs (rank 1 from both sides) are kept, so the
    -- result is one-to-one by construction.
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT p_import_id, c.invoice_id, c.invoice_fy, c.entry_id, c.entry_fy,
           'PROBABLE', 'FUZZY_NO', c.diff
    FROM (
        SELECT f.*,
               row_number() OVER (PARTITION BY f.invoice_id
                                  ORDER BY f.dist, abs(f.diff), f.entry_id) AS rn_b,
               row_number() OVER (PARTITION BY f.entry_id
                                  ORDER BY f.dist, abs(f.diff), f.invoice_id) AS rn_e
        FROM (
            SELECT b.invoice_id, b.fin_year AS invoice_fy, e.entry_id, e.fin_year AS entry_fy,
                   least(levenshtein_less_equal(b.invoice_no_norm, e.invoice_no_norm,
                                                v_maxdist), v_maxdist + 1) AS dist,
                   b.total_tax - (e.igst + e.cgst + e.sgst + e.cess) AS diff
            FROM v_books_candidate b
            JOIN gstr2b_entry e ON e.import_id = p_import_id
                               AND e.party_gstin = b.party_gstin AND e.doc_type = b.doc_type
                               AND fin_year_of(e.invoice_date) = b.fin_year
                               AND abs(e.invoice_date - b.invoice_date) <= v_window
                               AND abs(b.igst - e.igst) <= v_tol
                               AND abs(b.cgst - e.cgst) <= v_tol
                               AND abs(b.sgst - e.sgst) <= v_tol
                               AND abs(b.cess - e.cess) <= v_tol
                               AND (levenshtein_less_equal(b.invoice_no_norm, e.invoice_no_norm,
                                                           v_maxdist) <= v_maxdist
                                    OR strpos(b.invoice_no_norm, e.invoice_no_norm) > 0
                                    OR strpos(e.invoice_no_norm, b.invoice_no_norm) > 0)
            WHERE b.period_id = v_period.period_id
              AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                              AND r.invoice_id = b.invoice_id)
              AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                              AND r.entry_id = e.entry_id)
        ) f
    ) c
    WHERE c.rn_b = 1 AND c.rn_e = 1
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 4 VALUE: same key, some head beyond tolerance, no date window =====
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT DISTINCT ON (e.entry_id)
           p_import_id, b.invoice_id, b.fin_year, e.entry_id, e.fin_year,
           'MISMATCH', 'VALUE',
           b.total_tax - (e.igst + e.cgst + e.sgst + e.cess)
    FROM v_books_candidate b
    JOIN gstr2b_entry e ON e.import_id = p_import_id
                       AND e.party_gstin = b.party_gstin AND e.doc_type = b.doc_type
                       AND e.invoice_no_norm = b.invoice_no_norm
                       AND fin_year_of(e.invoice_date) = b.fin_year
                       AND (abs(b.igst - e.igst) > v_tol OR abs(b.cgst - e.cgst) > v_tol
                            OR abs(b.sgst - e.sgst) > v_tol OR abs(b.cess - e.cess) > v_tol)
    WHERE b.period_id = v_period.period_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = b.invoice_id)
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ORDER BY e.entry_id, b.invoice_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 5 PAN: same PAN, different GSTIN, same number, total tax within tolerance =====
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT DISTINCT ON (e.entry_id)
           p_import_id, b.invoice_id, b.fin_year, e.entry_id, e.fin_year,
           'PROBABLE', 'PAN',
           b.total_tax - (e.igst + e.cgst + e.sgst + e.cess)
    FROM v_books_candidate b
    JOIN gstr2b_entry e ON e.import_id = p_import_id
                       AND substr(e.party_gstin, 3, 10) = substr(b.party_gstin, 3, 10)
                       AND e.party_gstin <> b.party_gstin
                       AND e.doc_type = b.doc_type
                       AND e.invoice_no_norm = b.invoice_no_norm
                       AND fin_year_of(e.invoice_date) = b.fin_year
                       AND abs(b.total_tax - (e.igst + e.cgst + e.sgst + e.cess)) <= v_tol
    WHERE b.period_id = v_period.period_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = b.invoice_id)
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ORDER BY e.entry_id, b.invoice_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 6 CROSS_PERIOD: earlier-dated books invoices (up to 18 months back) with no
    -- active match in the current import of any period of this GSTIN; any date difference.
    -- ITC exclusions mirror v_books_candidate / v_excluded_itc (RCM, import, all-blocked).
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, entry_id, entry_fy,
                             status, matched_by, diff_tax)
    SELECT DISTINCT ON (e.entry_id)
           p_import_id, i.invoice_id, i.fin_year, e.entry_id, e.fin_year,
           'PERIOD_MISMATCH', 'CROSS_PERIOD',
           t.total_tax - (e.igst + e.cgst + e.sgst + e.cess)
    FROM invoice i
    JOIN v_invoice_total t ON t.invoice_id = i.invoice_id AND t.fin_year = i.fin_year
    JOIN gstr2b_entry e ON e.import_id = p_import_id
                       AND e.party_gstin = i.party_gstin AND e.doc_type = i.doc_type
                       AND e.invoice_no_norm = i.invoice_no_norm
                       AND fin_year_of(e.invoice_date) = i.fin_year
                       AND abs(t.igst - e.igst) <= v_tol AND abs(t.cgst - e.cgst) <= v_tol
                       AND abs(t.sgst - e.sgst) <= v_tol AND abs(t.cess - e.cess) <= v_tol
    WHERE i.gstin = v_period.gstin AND i.direction = 'INWARD'
      AND i.invoice_date >= v_start - interval '18 months'
      AND i.invoice_date <  v_start
      AND NOT i.is_rcm AND NOT i.is_import
      AND EXISTS (SELECT 1 FROM invoice_line l
                  WHERE l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year
                    AND l.itc_category <> 'BLOCKED_17_5')
      AND NOT EXISTS (
          SELECT 1
          FROM recon_result r
          JOIN gstr2b_import gi ON gi.import_id = r.import_id AND gi.is_current
          JOIN return_period rp ON rp.period_id = gi.period_id AND rp.gstin = v_period.gstin
          WHERE r.invoice_id = i.invoice_id AND r.invoice_fy = i.fin_year
            AND r.status IN ('EXACT', 'ROUNDING', 'PROBABLE', 'MISMATCH', 'PERIOD_MISMATCH'))
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = i.invoice_id)
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ORDER BY e.entry_id, i.invoice_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 7 RESIDUE: books side =====
    INSERT INTO recon_result(import_id, invoice_id, invoice_fy, status, matched_by, diff_tax)
    SELECT p_import_id, b.invoice_id, b.fin_year, 'ONLY_IN_BOOKS', 'RESIDUE', b.total_tax
    FROM v_books_candidate b
    WHERE b.period_id = v_period.period_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.invoice_id = b.invoice_id)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    -- ===== Pass 7 RESIDUE: portal side (duplicates already have a row) =====
    INSERT INTO recon_result(import_id, entry_id, entry_fy, status, matched_by, diff_tax)
    SELECT p_import_id, e.entry_id, e.fin_year, 'ONLY_IN_2B', 'RESIDUE',
           -(e.igst + e.cgst + e.sgst + e.cess)
    FROM gstr2b_entry e
    WHERE e.import_id = p_import_id
      AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import_id
                      AND r.entry_id = e.entry_id)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;

    RETURN v_total;
END;
$$;

-- Naive baseline for the viva: one FULL OUTER JOIN on (party_gstin, raw invoice_no) between
-- the import period's books candidates and the import's entries. No normalisation, FY,
-- date or amount logic.
CREATE VIEW v_naive_match AS
WITH b AS (
    SELECT gi.import_id, bc.party_gstin, bc.invoice_no
    FROM gstr2b_import gi
    JOIN v_books_candidate bc ON bc.period_id = gi.period_id
), e AS (
    SELECT ge.import_id, ge.party_gstin, ge.invoice_no
    FROM gstr2b_entry ge
)
SELECT coalesce(b.import_id, e.import_id) AS import_id,
       b.invoice_no AS books_no,
       e.invoice_no AS portal_no,
       (b.invoice_no IS NOT NULL AND e.invoice_no IS NOT NULL) AS paired
FROM b
FULL OUTER JOIN e ON e.import_id = b.import_id
                 AND e.party_gstin = b.party_gstin
                 AND e.invoice_no = b.invoice_no;
