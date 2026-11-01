-- 013_hardening.sql: fixes from the final whole-branch review (rulings F0-F8).
-- Migrations 001-012 are treated as merged (F0); every change lives here.
--
--   F1 (C2/C3) import/match procedures become SECURITY DEFINER wrappers (tenant + closed-month
--              checks) around INVOKER cores that app roles cannot call; app roles lose DML on
--              derived/imported tables; closed-month triggers for every role.
--   F2 (C1)    editing a decision drops the approval and re-derives the state.
--   F3 (I1)    a review's decision counts only in its last period (v_client_summary).
--   F4 (I2)    v_ims_plan is released only after client approval or a partner override.
--   F5 (I3)    gstr2b_entry.ims_status is a checked code read from the payload.
--   F6 (I4)    FOLLOWUP table.
--   F7 (I5/I6) invoice header fixed once lines exist; client PAN fixed while GSTINs exist.
--   F8         client_of_* hide other tenants; no PUBLIC EXECUTE; app_role_is exact match;
--              password_hash unreadable; sp_approve partner-only; v_rule37 / S16_4_NEAR /
--              pass 6 refinements.
--
-- Definer functions are owned by the migration owner (a superuser, so they bypass RLS); each
-- one checks the caller with can_see_client(), which reads the SET ROLE target and
-- app.user_id (both unchanged inside SECURITY DEFINER code).

-- =======================================================================================
-- F8: identity helpers.

-- Exact match on the SET ROLE target. rc_api (a NOINHERIT member of every rc_* group) is
-- therefore none of them, whether it has not SET ROLE yet ('none', not a superuser) or a
-- superuser SET ROLE rc_api.
CREATE OR REPLACE FUNCTION app_role_is(p_role text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN current_setting('role') = 'none'
                THEN coalesce((SELECT rolsuper FROM pg_catalog.pg_roles
                               WHERE rolname = session_user), false)
                ELSE current_setting('role') = p_role END
$$;

-- Ownership lookups answer only for clients the caller may see (NULL otherwise), so they
-- cannot be used to probe which GSTIN / period / import belongs to whom.
CREATE OR REPLACE FUNCTION client_of_gstin(p_gstin text) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.cid FROM (SELECT client_id AS cid FROM gstin_reg WHERE gstin = p_gstin) s
    WHERE can_see_client(s.cid)
$$;

CREATE OR REPLACE FUNCTION client_of_period(p_period_id bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.cid FROM (
        SELECT g.client_id AS cid FROM return_period p JOIN gstin_reg g ON g.gstin = p.gstin
        WHERE p.period_id = p_period_id) s
    WHERE can_see_client(s.cid)
$$;

CREATE OR REPLACE FUNCTION client_of_import(p_import_id bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.cid FROM (
        SELECT g.client_id AS cid FROM gstr2b_import i
        JOIN return_period p ON p.period_id = i.period_id
        JOIN gstin_reg g ON g.gstin = p.gstin
        WHERE i.import_id = p_import_id) s
    WHERE can_see_client(s.cid)
$$;

-- =======================================================================================
-- F5: IMS status codes. The import reads an optional per-document 'ims_status' (one of the
-- codes below) or an 'imsactn' action code A/R/P/N; default NO_ACTION. ASSUMPTION: the
-- official IMS JSON field name is unverified (verify on gst.gov.in). Unknown values are
-- passed through so the CHECK rejects them instead of silently becoming NO_ACTION.
CREATE FUNCTION ims_status_of(d jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT coalesce(
        upper(nullif(btrim(d->>'ims_status'), '')),
        CASE upper(nullif(btrim(d->>'imsactn'), ''))
            WHEN 'A' THEN 'ACCEPTED' WHEN 'R' THEN 'REJECTED'
            WHEN 'P' THEN 'PENDING'  WHEN 'N' THEN 'NO_ACTION'
            ELSE upper(nullif(btrim(d->>'imsactn'), '')) END,
        'NO_ACTION')
$$;
COMMENT ON FUNCTION ims_status_of(jsonb) IS
    'IMS status of a 2B/IMS document: ims_status or imsactn (A/R/P/N); field names unverified, verify on gst.gov.in';

ALTER TABLE gstr2b_entry ADD CONSTRAINT gstr2b_entry_ims_status_chk
    CHECK (ims_status IN ('NO_ACTION', 'ACCEPTED', 'REJECTED', 'PENDING', 'DEEMED'));

-- =======================================================================================
-- F1: import / match procedures. The 005-007 functions become INVOKER cores (only callable
-- by the owner, i.e. from the definer wrappers below); the original names are wrappers.

ALTER FUNCTION sp_open_period(text, char) RENAME TO _sp_open_period_core;
ALTER FUNCTION sp_import_gstr2b(bigint, jsonb, text, bigint) RENAME TO _sp_import_gstr2b_core;
ALTER FUNCTION sp_import_books(bigint, bigint) RENAME TO _sp_import_books_core;
ALTER FUNCTION sp_reconcile(bigint, bigint) RENAME TO _sp_reconcile_core;
ALTER FUNCTION _sp_open_period_core(text, char) SET search_path = public, pg_temp;
ALTER FUNCTION _sp_import_books_core(bigint, bigint) SET search_path = public, pg_temp;

-- 005's import, now also storing ims_status (F5). Otherwise unchanged.
CREATE OR REPLACE FUNCTION _sp_import_gstr2b_core(p_period_id bigint, p_payload jsonb,
                                                  p_source text, p_user_id bigint)
RETURNS bigint
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
DECLARE
    v_period return_period%ROWTYPE;
    v_version int;
    v_import bigint;
    v_dd jsonb := p_payload->'data'->'docdata';
BEGIN
    IF p_source NOT IN ('IMS', 'DRAFT_2B', 'RECOMPUTED_2B') THEN
        RAISE EXCEPTION 'invalid source: %', p_source;
    END IF;
    SELECT * INTO v_period FROM return_period WHERE period_id = p_period_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
    IF p_payload->'data'->>'rtnprd' IS DISTINCT FROM v_period.period THEN
        RAISE EXCEPTION 'rtnprd mismatch: payload % vs period %',
            p_payload->'data'->>'rtnprd', v_period.period;
    END IF;

    SELECT COALESCE(max(version), 0) + 1 INTO v_version
    FROM gstr2b_import WHERE period_id = p_period_id;
    UPDATE gstr2b_import SET is_current = false WHERE period_id = p_period_id AND is_current;
    INSERT INTO gstr2b_import(period_id, version, source, is_current, payload, imported_by)
    VALUES (p_period_id, v_version, p_source, true, p_payload, p_user_id)
    RETURNING import_id INTO v_import;

    -- b2b and b2ba invoices
    INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, party_name, section,
        doc_type, invoice_no, invoice_date, taxable_value, igst, cgst, sgst, cess,
        itc_available, orig_invoice_no, ims_status)
    SELECT v_import, v_period.fin_year, s.c->>'ctin', gstin_checksum_ok(s.c->>'ctin'),
        s.c->>'trdnm', s.sec, 'INV', d->>'inum', to_date(d->>'dt', 'DD-MM-YYYY'),
        gstr2b_amt(d, 'txval'), gstr2b_amt(d, 'igst'), gstr2b_amt(d, 'cgst'),
        gstr2b_amt(d, 'sgst'), gstr2b_amt(d, 'cess'),
        COALESCE(upper(d->>'itcavl'), 'Y') <> 'N',
        CASE WHEN s.sec = 'b2ba' THEN d->>'oinum' END,
        ims_status_of(d)
    FROM (
        SELECT 'b2b' AS sec, c FROM jsonb_array_elements(COALESCE(v_dd->'b2b', '[]'::jsonb)) c
        UNION ALL
        SELECT 'b2ba', c FROM jsonb_array_elements(COALESCE(v_dd->'b2ba', '[]'::jsonb)) c
    ) s
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s.c->'inv', '[]'::jsonb)) d;

    -- credit / debit notes: credit notes are stored negated
    INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, party_name, section,
        doc_type, invoice_no, invoice_date, taxable_value, igst, cgst, sgst, cess, itc_available,
        ims_status)
    SELECT v_import, v_period.fin_year, c->>'ctin', gstin_checksum_ok(c->>'ctin'),
        c->>'trdnm', 'cdnr',
        CASE WHEN upper(d->>'ntty') = 'C' THEN 'CRN'::doc_type ELSE 'DBN'::doc_type END,
        d->>'ntnum', to_date(d->>'dt', 'DD-MM-YYYY'),
        sg.m * gstr2b_amt(d, 'txval'), sg.m * gstr2b_amt(d, 'igst'), sg.m * gstr2b_amt(d, 'cgst'),
        sg.m * gstr2b_amt(d, 'sgst'), sg.m * gstr2b_amt(d, 'cess'),
        COALESCE(upper(d->>'itcavl'), 'Y') <> 'N',
        ims_status_of(d)
    FROM jsonb_array_elements(COALESCE(v_dd->'cdnr', '[]'::jsonb)) c
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(c->'nt', '[]'::jsonb)) d
    CROSS JOIN LATERAL (SELECT CASE WHEN upper(d->>'ntty') = 'C' THEN -1 ELSE 1 END AS m) sg;

    -- only checksum-valid suppliers become party rows
    INSERT INTO party(gstin, name)
    SELECT DISTINCT ON (party_gstin) party_gstin, COALESCE(party_name, party_gstin)
    FROM gstr2b_entry
    WHERE import_id = v_import AND fin_year = v_period.fin_year AND gstin_valid
    ORDER BY party_gstin, entry_id
    ON CONFLICT (gstin) DO NOTHING;

    UPDATE return_period SET state = 'IN_REVIEW'
    WHERE period_id = p_period_id AND state = 'OPEN';

    RETURN v_import;
END;
$$;

-- 007's matcher; pass 6 is now bounded by the s.16(4) date (F8). Otherwise unchanged.
CREATE OR REPLACE FUNCTION _sp_reconcile_core(p_import_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
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

    -- ===== Pass 6 CROSS_PERIOD: earlier-dated books invoices (up to 18 months back, and only
    -- while the document FY's s.16(4) date is on or after this period's start) with no
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
      AND s16_4_date(i.fin_year) >= v_start   -- 013: no ITC once the s.16(4) date has passed
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

-- Shared wrapper guard: the caller may see the period's client, and the period is not
-- CLOSED. An unknown period is 'not your client' for app roles (client_of_period is NULL);
-- a superuser falls through to the core's own 'unknown period' error.
CREATE FUNCTION _guard_period(p_period_id bigint) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT can_see_client(client_of_period(p_period_id)) THEN
        RAISE EXCEPTION 'not your client';
    END IF;
    IF EXISTS (SELECT 1 FROM return_period
               WHERE period_id = p_period_id AND state = 'CLOSED') THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
END;
$$;

CREATE FUNCTION sp_open_period(p_gstin text, p_period char(6)) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT can_see_client(client_of_gstin(p_gstin)) THEN
        RAISE EXCEPTION 'not your client';
    END IF;
    IF EXISTS (SELECT 1 FROM return_period
               WHERE gstin = p_gstin AND period = p_period AND state = 'CLOSED') THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN _sp_open_period_core(p_gstin, p_period);
END;
$$;

CREATE FUNCTION sp_import_gstr2b(p_period_id bigint, p_payload jsonb, p_source text,
                                 p_user_id bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    PERFORM _guard_period(p_period_id);
    RETURN _sp_import_gstr2b_core(p_period_id, p_payload, p_source, p_user_id);
END;
$$;

CREATE FUNCTION sp_import_books(p_period_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    PERFORM _guard_period(p_period_id);
    RETURN _sp_import_books_core(p_period_id, p_user_id);
END;
$$;

CREATE FUNCTION sp_reconcile(p_import_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_pid bigint;
BEGIN
    IF NOT can_see_client(client_of_import(p_import_id)) THEN
        RAISE EXCEPTION 'not your client';
    END IF;
    SELECT period_id INTO v_pid FROM gstr2b_import WHERE import_id = p_import_id;
    IF FOUND THEN
        PERFORM _guard_period(v_pid);
    END IF;
    RETURN _sp_reconcile_core(p_import_id, p_user_id);
END;
$$;

-- Table privileges: imported and derived data is written only by the procedures above.
-- (Table-level REVOKE also removes the 010 column grants on these tables.)
REVOKE INSERT, UPDATE, DELETE ON recon_result, gstr2b_entry, gstr2b_import, invoice,
                                 invoice_line, party
    FROM rc_partner, rc_staff, rc_client;
-- return_period: rows come from sp_open_period, state moves from the workflow procedures.
-- INSERT is revoked as well as UPDATE (sp_open_period is the only intended writer).
REVOKE INSERT, UPDATE ON return_period FROM rc_partner, rc_staff, rc_client;
REVOKE USAGE ON SEQUENCE invoice_invoice_id_seq, invoice_line_line_id_seq,
                         gstr2b_import_import_id_seq, gstr2b_entry_entry_id_seq,
                         recon_result_result_id_seq, return_period_period_id_seq
    FROM rc_partner, rc_staff, rc_client;
-- Kept: SELECT everywhere (RLS), books_staging DML, payment DML, review decision columns,
-- filing INSERT/UPDATE.

-- ---------------------------------------------------------------------------------------
-- Closed-month guards for every role (superusers included): a CLOSED month's 2B data and
-- books are frozen until sp_reopen_period. recon_result is already guarded (009).

-- True when p_gstin has a CLOSED return period for the month of p_date.
CREATE FUNCTION month_is_closed(p_gstin text, p_date date) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM return_period
                   WHERE gstin = p_gstin AND period = to_char(p_date, 'MMYYYY')
                     AND state = 'CLOSED')
$$;

CREATE FUNCTION import_is_closed(p_import_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM gstr2b_import i
                   JOIN return_period p ON p.period_id = i.period_id
                   WHERE i.import_id = p_import_id AND p.state = 'CLOSED')
$$;

CREATE FUNCTION trg_entry_closed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF (TG_OP <> 'DELETE' AND import_is_closed(NEW.import_id))
       OR (TG_OP <> 'INSERT' AND import_is_closed(OLD.import_id)) THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER trg_entry_closed BEFORE INSERT OR UPDATE OR DELETE ON gstr2b_entry
FOR EACH ROW EXECUTE FUNCTION trg_entry_closed();

-- gstr2b_import: INSERT is handled by trg_closed_readonly (009) and the wrappers; an
-- UPDATE (is_current flip) or DELETE of a CLOSED month's import is refused.
CREATE FUNCTION trg_import_closed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM return_period p
               WHERE p.state = 'CLOSED'
                 AND (p.period_id = OLD.period_id
                      OR (TG_OP = 'UPDATE' AND p.period_id = NEW.period_id))) THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER trg_import_closed BEFORE UPDATE OR DELETE ON gstr2b_import
FOR EACH ROW EXECUTE FUNCTION trg_import_closed();

-- invoice / invoice_line: the month of the invoice date, if that GSTIN has a CLOSED period
-- for it.
CREATE FUNCTION trg_invoice_closed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF (TG_OP <> 'DELETE' AND month_is_closed(NEW.gstin, NEW.invoice_date))
       OR (TG_OP <> 'INSERT' AND month_is_closed(OLD.gstin, OLD.invoice_date)) THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER trg_invoice_closed BEFORE INSERT OR UPDATE OR DELETE ON invoice
FOR EACH ROW EXECUTE FUNCTION trg_invoice_closed();

CREATE FUNCTION trg_invoice_line_closed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF (TG_OP <> 'DELETE' AND EXISTS (
            SELECT 1 FROM invoice i
            WHERE i.invoice_id = NEW.invoice_id AND i.fin_year = NEW.fin_year
              AND month_is_closed(i.gstin, i.invoice_date)))
       OR (TG_OP <> 'INSERT' AND EXISTS (
            SELECT 1 FROM invoice i
            WHERE i.invoice_id = OLD.invoice_id AND i.fin_year = OLD.fin_year
              AND month_is_closed(i.gstin, i.invoice_date))) THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER trg_invoice_line_closed BEFORE INSERT OR UPDATE OR DELETE ON invoice_line
FOR EACH ROW EXECUTE FUNCTION trg_invoice_line_closed();

-- =======================================================================================
-- F2: changing what a review decides (decision, ims_action, claim_pct) makes the editor the
-- maker and needs a fresh approval: proposed_by = the app user, approved_by NULL, state
-- re-derived from tax_at_risk vs client.approval_threshold (as sp_propose does, so the
-- sp_propose upsert is unaffected). Applied whatever the old state (APPROVED/DONE per F2;
-- also PROPOSED, so a partner cannot edit a proposal and then approve it as the checker).
CREATE FUNCTION trg_review_reapprove() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_thr numeric(14,2);
BEGIN
    IF (NEW.decision, NEW.ims_action, NEW.claim_pct)
       IS NOT DISTINCT FROM (OLD.decision, OLD.ims_action, OLD.claim_pct) THEN
        RETURN NEW;
    END IF;
    SELECT c.approval_threshold INTO v_thr
    FROM gstin_reg g JOIN client c ON c.client_id = g.client_id
    WHERE g.gstin = NEW.gstin;
    NEW.state       := CASE WHEN coalesce(NEW.tax_at_risk, 0) <= v_thr
                            THEN 'APPROVED' ELSE 'PROPOSED' END::review_state;
    NEW.approved_by := NULL;
    NEW.approved_at := CASE WHEN NEW.state = 'APPROVED' THEN now() END;
    NEW.proposed_by := coalesce(current_app_user(), NEW.proposed_by);
    NEW.proposed_at := now();
    NEW.executed_on := NULL;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_review_reapprove BEFORE UPDATE ON review
FOR EACH ROW EXECUTE FUNCTION trg_review_reapprove();

-- =======================================================================================
-- F3: a review's decision counts only in its last period (review.last_period_id); in any
-- other period with a result for the same key the key is unreviewed. Carried DEFER/CHASE
-- rows (no result in the later month) still count as deferred there, as before.
-- Accepted cost: a closed month's summary changes when a later month re-proposes its key.
CREATE OR REPLACE VIEW v_client_summary WITH (security_invoker = true) AS
WITH cur AS (
    SELECT k.period_id, k.status, k.party_gstin, k.books_total_tax,
           rv.review_id, rv.decision, rv.claim_pct, rv.tax_at_risk
    FROM v_result_key k
    LEFT JOIN review rv ON rv.gstin = k.gstin AND rv.party_gstin = k.party_gstin
                       AND rv.doc_type = k.doc_type AND rv.invoice_no_norm = k.invoice_no_norm
                       AND rv.fin_year = k.fin_year
                       AND rv.last_period_id = k.period_id
    WHERE k.is_current AND k.status <> 'DUPLICATE'
), claim AS (
    SELECT period_id,
           round(sum(CASE
               WHEN review_id IS NULL AND status IN ('EXACT', 'ROUNDING')
                   THEN coalesce(books_total_tax, 0)
               WHEN decision = 'ACCEPT' THEN coalesce(books_total_tax, 0)
               WHEN decision = 'PARTIAL' THEN coalesce(books_total_tax, 0) * claim_pct / 100
               ELSE 0 END), 2) AS claimable_now,
           coalesce(sum(tax_at_risk) FILTER (WHERE decision = 'INELIGIBLE'), 0) AS lost
    FROM cur GROUP BY period_id
), q AS (
    SELECT DISTINCT q.period_id, rv.review_id, rv.decision, rv.party_gstin, rv.tax_at_risk
    FROM v_queue q
    JOIN review rv ON rv.review_id = q.review_id
    WHERE rv.decision IN ('DEFER', 'CHASE')
      AND (q.result_id IS NULL OR rv.last_period_id = q.period_id)
), defer AS (
    SELECT period_id, coalesce(sum(tax_at_risk), 0) AS deferred FROM q GROUP BY period_id
), hold AS (
    SELECT period_id, count(DISTINCT party_gstin)::int AS n FROM (
        SELECT period_id, party_gstin FROM q WHERE decision = 'CHASE'
        UNION
        SELECT period_id, party_gstin FROM cur
        WHERE status = 'ONLY_IN_BOOKS' AND review_id IS NULL
    ) h GROUP BY period_id
)
SELECT p.period_id,
       coalesce(c.claimable_now, 0) AS claimable_now,
       coalesce(d.deferred, 0)      AS deferred,
       coalesce(c.lost, 0)          AS lost,
       coalesce(h.n, 0)             AS hold_payment_parties
FROM return_period p
LEFT JOIN claim c ON c.period_id = p.period_id
LEFT JOIN defer d ON d.period_id = p.period_id
LEFT JOIN hold h  ON h.period_id = p.period_id;

-- =======================================================================================
-- F4: the IMS action plan is released only after the client's approval or a partner
-- override with a reason (spec §4).
CREATE OR REPLACE VIEW v_ims_plan WITH (security_invoker = true) AS
SELECT r.period_id, r.party_gstin, r.invoice_no, r.ims_action, r.remark
FROM v_period_review r
JOIN return_period p ON p.period_id = r.period_id
WHERE r.ims_action <> 'NONE'
  AND (p.client_approved_by IS NOT NULL OR p.override_reason IS NOT NULL);

-- =======================================================================================
-- F6: supplier follow-ups (ER: FOLLOWUP). party_gstin is plain char(15) like review (R3).
CREATE TABLE followup (
    followup_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gstin         gstin NOT NULL REFERENCES gstin_reg(gstin),
    party_gstin   char(15) NOT NULL,
    period_id     bigint NOT NULL REFERENCES return_period(period_id),
    channel       text NOT NULL CHECK (channel IN ('EMAIL', 'WHATSAPP', 'CALL', 'LETTER')),
    sent_by       bigint REFERENCES app_user(user_id) DEFAULT current_app_user(),
    sent_on       date,
    response      text,
    next_chase_on date,
    status        text NOT NULL DEFAULT 'OPEN'
                  CHECK (status IN ('OPEN', 'SENT', 'RESPONDED', 'RESOLVED'))
);
CREATE INDEX followup_gstin_idx ON followup (gstin, party_gstin);
CREATE INDEX followup_period_idx ON followup (period_id);
COMMENT ON TABLE followup IS 'Who chased which supplier, when, how, and what they replied';

-- The period must be one of the follow-up's own GSTIN (FKs ignore RLS).
CREATE FUNCTION trg_followup_period() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM return_period
                   WHERE period_id = NEW.period_id AND gstin = NEW.gstin) THEN
        RAISE EXCEPTION 'period does not belong to this GSTIN';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_followup_period BEFORE INSERT OR UPDATE ON followup
FOR EACH ROW EXECUTE FUNCTION trg_followup_period();
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON followup
FOR EACH ROW EXECUTE FUNCTION trg_audit('followup_id');

ALTER TABLE followup ENABLE ROW LEVEL SECURITY;
ALTER TABLE followup FORCE ROW LEVEL SECURITY;
CREATE POLICY followup_tenant ON followup
    USING (can_see_client(client_of_gstin(gstin)));
GRANT SELECT ON followup TO rc_partner, rc_staff, rc_client;
GRANT INSERT, UPDATE, DELETE ON followup TO rc_staff, rc_partner;

-- =======================================================================================
-- F7: the header fields the line rules depend on are fixed once lines exist.
CREATE FUNCTION trg_invoice_header_fixed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF (NEW.gstin, NEW.party_gstin, NEW.direction, NEW.doc_type, NEW.place_of_supply,
        NEW.is_import, NEW.is_rcm, NEW.invoice_date)
       IS DISTINCT FROM
       (OLD.gstin, OLD.party_gstin, OLD.direction, OLD.doc_type, OLD.place_of_supply,
        OLD.is_import, OLD.is_rcm, OLD.invoice_date)
       AND EXISTS (SELECT 1 FROM invoice_line l
                   WHERE l.invoice_id = OLD.invoice_id AND l.fin_year = OLD.fin_year) THEN
        RAISE EXCEPTION 'invoice header is fixed once lines exist';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_invoice_header_fixed BEFORE UPDATE ON invoice
FOR EACH ROW EXECUTE FUNCTION trg_invoice_header_fixed();

-- A client's GSTINs embed its PAN (trg_gstin_pan checks on insert); keep them consistent.
CREATE FUNCTION trg_client_pan_fixed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.pan IS DISTINCT FROM OLD.pan
       AND EXISTS (SELECT 1 FROM gstin_reg WHERE client_id = OLD.client_id) THEN
        RAISE EXCEPTION 'PAN cannot change while GSTINs are registered';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_client_pan_fixed BEFORE UPDATE ON client
FOR EACH ROW EXECUTE FUNCTION trg_client_pan_fixed();

-- =======================================================================================
-- F8: report refinements.

-- Rule 37 covers only invoices whose ITC is claimed: the v_excluded_itc reasons (RCM,
-- import, every line BLOCKED_17_5) are excluded directly, so invoices of months without a
-- return period are covered too.
CREATE OR REPLACE VIEW v_rule37 WITH (security_invoker = true) AS
SELECT x.client_id, x.invoice_id, x.party_gstin, x.invoice_no, x.invoice_date, x.unpaid,
       x.days_outstanding, x.bucket,
       CASE WHEN x.bucket = '>180'
            THEN round(x.reversal * x.r37_interest_pct / 100
                       * (x.days_outstanding - 180) / 365, 2)
            ELSE 0 END AS interest_estimate,
       CASE WHEN x.bucket = '>180' THEN x.reversal ELSE 0 END AS proposed_reversal
FROM (
    SELECT g.client_id, i.invoice_id, i.party_gstin, i.invoice_no, i.invoice_date,
           u.unpaid, d.days_outstanding,
           CASE WHEN d.days_outstanding <= 150 THEN '0-150'
                WHEN d.days_outstanding <= 180 THEN '150-180'
                ELSE '>180' END AS bucket,
           round(u.unpaid * t.total_tax / nullif(t.taxable_value + t.total_tax, 0), 2)
               AS reversal,
           c.r37_interest_pct
    FROM invoice i
    JOIN gstin_reg g ON g.gstin = i.gstin
    JOIN client c ON c.client_id = g.client_id
    JOIN v_invoice_total t ON t.invoice_id = i.invoice_id AND t.fin_year = i.fin_year
    CROSS JOIN LATERAL (
        SELECT t.taxable_value + t.total_tax
               - coalesce((SELECT sum(pm.amount) FROM payment pm
                           WHERE pm.invoice_id = i.invoice_id
                             AND pm.fin_year = i.fin_year), 0) AS unpaid
    ) u
    CROSS JOIN LATERAL (SELECT app_today() - i.invoice_date AS days_outstanding) d
    WHERE i.direction = 'INWARD'
      AND i.doc_type IN ('INV', 'DBN')   -- supplier credit notes are not bills to pay
      AND NOT i.is_rcm AND NOT i.is_import
      AND EXISTS (SELECT 1 FROM invoice_line l
                  WHERE l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year
                    AND l.itc_category <> 'BLOCKED_17_5')
      AND u.unpaid > 0
      AND EXISTS (SELECT 1 FROM payment pm
                  JOIN invoice pi ON pi.invoice_id = pm.invoice_id
                                 AND pi.fin_year = pm.fin_year
                  JOIN gstin_reg pg ON pg.gstin = pi.gstin
                  WHERE pg.client_id = g.client_id)
) x;

-- S16_4_NEAR only for reviews still waiting on ITC (DEFER / CHASE).
CREATE OR REPLACE VIEW v_alerts WITH (security_invoker = true) AS
SELECT g.client_id, f.gstin,
       CASE WHEN f.due_date < app_today() THEN 'FILING_OVERDUE' ELSE 'FILING_DUE' END AS kind,
       f.return_type || ' ' || f.period AS ref,
       f.due_date AS due_by
FROM filing f
JOIN gstin_reg g ON g.gstin = f.gstin
WHERE f.filed_on IS NULL AND f.status <> 'FILED'
  AND f.due_date <= app_today() + 7

UNION ALL

SELECT g.client_id, rv.gstin, 'S16_4_NEAR', rv.invoice_no, s16_4_date(rv.fin_year)
FROM review rv
JOIN gstin_reg g ON g.gstin = rv.gstin
WHERE rv.state <> 'DONE'
  AND rv.decision IN ('DEFER', 'CHASE')
  AND s16_4_date(rv.fin_year) BETWEEN app_today() AND app_today() + 30;

-- password_hash is never readable by the application roles.
REVOKE SELECT ON app_user FROM rc_partner, rc_staff, rc_client;
GRANT SELECT (user_id, firm_id, client_id, email, role) ON app_user
    TO rc_partner, rc_staff, rc_client;

-- =======================================================================================
-- F8: EXECUTE. Nothing of ours is executable by PUBLIC (extension functions untouched);
-- grants below name the roles that need each function. Trigger functions need no grant.
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                            AND d.deptype = 'e')
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f);
    END LOOP;
END $$;
-- Functions created later by the migration owner start without PUBLIC EXECUTE too.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Cores: owner only (also revoked explicitly from the app roles, in case of earlier grants).
REVOKE EXECUTE ON FUNCTION _sp_open_period_core(text, char),
                           _sp_import_gstr2b_core(bigint, jsonb, text, bigint),
                           _sp_import_books_core(bigint, bigint),
                           _sp_reconcile_core(bigint, bigint),
                           _guard_period(bigint)
    FROM rc_partner, rc_staff, rc_client;

-- Helpers used in RLS policies, column defaults, domain checks and invoker views.
GRANT EXECUTE ON FUNCTION gstin_checksum_ok(text), rate_on(text, date), norm_doc_no(text),
                          fin_year_of(date), period_start(char), app_role_is(text),
                          app_user_id(), current_app_user(), can_see_client(bigint),
                          current_app_firm(), client_of_gstin(text), client_of_period(bigint),
                          client_of_import(bigint), app_today(), s16_4_date(text)
    TO rc_partner, rc_staff, rc_client;
GRANT EXECUTE ON FUNCTION result_fingerprint(bigint) TO rc_partner, rc_staff;
-- rc_api may ask which role it holds (always false until it SETs ROLE).
GRANT EXECUTE ON FUNCTION app_role_is(text) TO rc_api;

-- Import / match wrappers: staff and partners.
GRANT EXECUTE ON FUNCTION sp_open_period(text, char),
                          sp_import_gstr2b(bigint, jsonb, text, bigint),
                          sp_import_books(bigint, bigint),
                          sp_reconcile(bigint, bigint)
    TO rc_staff, rc_partner;

-- Approval is a partner action (maker-checker).
REVOKE EXECUTE ON FUNCTION sp_approve(bigint) FROM rc_staff;
