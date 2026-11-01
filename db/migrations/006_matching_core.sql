-- 006_matching_core.sql: reconciliation results and the set-based matcher (passes 0, 1, 2, 7).
-- Task 7 CREATE OR REPLACEs sp_reconcile to slot passes 3-6 between pass 2 and pass 7.

CREATE TYPE match_status AS ENUM ('EXACT', 'ROUNDING', 'PROBABLE', 'MISMATCH',
    'PERIOD_MISMATCH', 'ONLY_IN_BOOKS', 'ONLY_IN_2B', 'DUPLICATE');

-- Not partitioned. One-to-one matching is enforced by the two UNIQUE constraints.
CREATE TABLE recon_result (
    result_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id  bigint NOT NULL REFERENCES gstr2b_import(import_id),
    invoice_id bigint,
    invoice_fy text,
    entry_id   bigint,
    entry_fy   text,
    status     match_status NOT NULL,
    matched_by text NOT NULL,
    diff_tax   numeric(14,2),
    FOREIGN KEY (invoice_id, invoice_fy) REFERENCES invoice(invoice_id, fin_year),
    FOREIGN KEY (entry_id, entry_fy) REFERENCES gstr2b_entry(entry_id, fin_year),
    CONSTRAINT recon_result_invoice_pair CHECK ((invoice_id IS NULL) = (invoice_fy IS NULL)),
    CONSTRAINT recon_result_entry_pair CHECK ((entry_id IS NULL) = (entry_fy IS NULL)),
    CONSTRAINT recon_result_one_side CHECK (invoice_id IS NOT NULL OR entry_id IS NOT NULL),
    CONSTRAINT recon_result_invoice_once UNIQUE (import_id, invoice_id),
    CONSTRAINT recon_result_entry_once UNIQUE (import_id, entry_id)
);
CREATE INDEX recon_result_status_idx ON recon_result (import_id, status);

-- Books invoices of a period that the matcher must not treat as ITC candidates.
CREATE VIEW v_excluded_itc AS
SELECT p.period_id, i.invoice_id, x.reason
FROM return_period p
JOIN invoice i ON i.gstin = p.gstin AND i.direction = 'INWARD'
              AND i.invoice_date >= period_start(p.period)
              AND i.invoice_date <  period_start(p.period) + interval '1 month'
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN i.is_rcm THEN 'RCM'
        WHEN i.is_import THEN 'IMPORT'
        WHEN NOT EXISTS (SELECT 1 FROM invoice_line l
                         WHERE l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year
                           AND l.itc_category <> 'BLOCKED_17_5')
             AND EXISTS (SELECT 1 FROM invoice_line l
                         WHERE l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year)
        THEN 'BLOCKED_17_5'
    END AS reason
) x
WHERE x.reason IS NOT NULL;

-- Books candidates per period, with totals.
CREATE VIEW v_books_candidate AS
SELECT p.period_id, i.invoice_id, i.fin_year, i.party_gstin, i.doc_type, i.invoice_no,
       i.invoice_no_norm, i.invoice_date,
       t.igst, t.cgst, t.sgst, t.cess, t.total_tax
FROM return_period p
JOIN invoice i ON i.gstin = p.gstin AND i.direction = 'INWARD'
              AND i.invoice_date >= period_start(p.period)
              AND i.invoice_date <  period_start(p.period) + interval '1 month'
              AND NOT i.is_rcm AND NOT i.is_import
JOIN v_invoice_total t ON t.invoice_id = i.invoice_id AND t.fin_year = i.fin_year
WHERE NOT EXISTS (
    SELECT 1 FROM v_excluded_itc x
    WHERE x.period_id = p.period_id AND x.invoice_id = i.invoice_id);

CREATE FUNCTION sp_reconcile(p_import_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
    v_period   return_period%ROWTYPE;
    v_allowed  boolean;
    v_tol      numeric(14,2);
    v_window   int;
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
    SELECT t.itc_allowed, c.rounding_tol, c.date_window_days
      INTO v_allowed, v_tol, v_window
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

    -- ===== Passes 3-6 (FUZZY_NO, VALUE, PAN, CROSS_PERIOD) are added by Task 7 here =====

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
