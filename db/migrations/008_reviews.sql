-- 008_reviews.sql: reviews keyed by the real invoice, maker-checker approval, carry-forward,
-- and the work queue. A review is one row per (gstin, party_gstin, doc_type, invoice_no_norm,
-- fin_year); results of any import (re-imports, later months) find it by joining on that key.

CREATE TYPE review_decision AS ENUM ('ACCEPT', 'CHASE', 'DEFER', 'PARTIAL', 'INELIGIBLE');
CREATE TYPE ims_action AS ENUM ('ACCEPT', 'REJECT', 'PENDING', 'NONE');
CREATE TYPE review_state AS ENUM ('PROPOSED', 'APPROVED', 'DONE');

CREATE TABLE review (
    review_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gstin           gstin NOT NULL REFERENCES gstin_reg(gstin),
    party_gstin     char(15) NOT NULL,              -- R3: plain, no FK to party
    doc_type        doc_type NOT NULL,
    invoice_no_norm text NOT NULL,
    fin_year        text NOT NULL,                  -- the document's FY
    invoice_no      text NOT NULL,                  -- as displayed when proposed
    decision        review_decision NOT NULL,
    ims_action      ims_action NOT NULL DEFAULT 'NONE',
    claim_pct       numeric(5,2),
    remark          text,
    state           review_state NOT NULL DEFAULT 'PROPOSED',
    tax_at_risk     numeric(14,2),                  -- abs(result.diff_tax) at proposal
    is_rcm          boolean NOT NULL DEFAULT false,
    fingerprint     text,                           -- result_fingerprint() at proposal
    last_period_id  bigint REFERENCES return_period(period_id),
    proposed_by     bigint NOT NULL REFERENCES app_user(user_id),
    proposed_at     timestamptz NOT NULL DEFAULT now(),
    approved_by     bigint REFERENCES app_user(user_id),   -- NULL = auto-approved / pending
    approved_at     timestamptz,
    executed_on     date,
    CONSTRAINT review_natural_key UNIQUE (gstin, party_gstin, doc_type, invoice_no_norm, fin_year),
    CONSTRAINT review_maker_checker CHECK (approved_by IS NULL OR approved_by <> proposed_by),
    -- coalesce: a NULL claim_pct must fail the check, not pass it as UNKNOWN.
    CONSTRAINT review_partial_pct
        CHECK (decision <> 'PARTIAL' OR coalesce(claim_pct BETWEEN 0 AND 100, false)),
    CONSTRAINT review_rcm_ims_none CHECK (NOT is_rcm OR ims_action = 'NONE')
);
CREATE INDEX review_last_period_idx ON review (last_period_id);

-- One row per recon_result with its review key: BOOKS side when present, else PORTAL side.
-- Document FY = invoice.fin_year (books) or fin_year_of(entry.invoice_date) (portal).
CREATE VIEW v_result_key AS
SELECT r.result_id, r.import_id, gi.period_id, gi.is_current, p.gstin,
       r.status, r.matched_by, r.diff_tax,
       coalesce(i.party_gstin::char(15), e.party_gstin)      AS party_gstin,
       coalesce(i.doc_type, e.doc_type)                      AS doc_type,
       coalesce(i.invoice_no_norm, e.invoice_no_norm)        AS invoice_no_norm,
       coalesce(i.fin_year, fin_year_of(e.invoice_date))     AS fin_year,
       coalesce(i.invoice_no, e.invoice_no)                  AS invoice_no,
       coalesce(i.is_rcm, false)                             AS is_rcm,
       t.total_tax                                           AS books_total_tax,
       e.igst + e.cgst + e.sgst + e.cess                     AS portal_total_tax,
       e.entry_id, e.ims_status,
       md5(r.status::text || ':' || coalesce(t.total_tax, 0) || ':'
           || coalesce(e.igst + e.cgst + e.sgst + e.cess, 0)) AS fingerprint
FROM recon_result r
JOIN gstr2b_import gi ON gi.import_id = r.import_id
JOIN return_period p ON p.period_id = gi.period_id
LEFT JOIN invoice i ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy
LEFT JOIN v_invoice_total t ON t.invoice_id = r.invoice_id AND t.fin_year = r.invoice_fy
LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id AND e.fin_year = r.entry_fy;

CREATE FUNCTION result_fingerprint(p_result_id bigint) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT fingerprint FROM v_result_key WHERE result_id = p_result_id
$$;

-- Role check usable inside SECURITY DEFINER functions (where current_user is the owner):
-- the effective application role is the SET ROLE target, else the session user.
CREATE FUNCTION app_role_is(p_role text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN current_setting('role') <> 'none'
                THEN pg_has_role(current_setting('role'), p_role, 'MEMBER')
                ELSE pg_has_role(session_user, p_role, 'MEMBER') END
$$;

CREATE FUNCTION app_user_id() RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v text := nullif(current_setting('app.user_id', true), '');
BEGIN
    IF v IS NULL THEN
        RAISE EXCEPTION 'app.user_id not set';
    END IF;
    RETURN v::bigint;
END;
$$;

CREATE FUNCTION sp_propose(p_result_id bigint, p_decision review_decision, p_ims ims_action,
                           p_claim_pct numeric, p_remark text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_uid   bigint := app_user_id();
    k       v_result_key%ROWTYPE;
    v_thr   numeric(14,2);
    v_risk  numeric(14,2);
    v_state review_state;
    v_id    bigint;
BEGIN
    SELECT * INTO k FROM v_result_key WHERE result_id = p_result_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown result %', p_result_id;
    END IF;
    -- A DUPLICATE shares the real invoice's key; it must never overwrite its review.
    IF k.status = 'DUPLICATE' THEN
        RAISE EXCEPTION 'cannot review a duplicate entry';
    END IF;
    SELECT c.approval_threshold INTO v_thr
    FROM gstin_reg g JOIN client c ON c.client_id = g.client_id
    WHERE g.gstin = k.gstin;

    v_risk  := coalesce(abs(k.diff_tax), 0);
    v_state := CASE WHEN v_risk <= v_thr THEN 'APPROVED' ELSE 'PROPOSED' END;

    INSERT INTO review(gstin, party_gstin, doc_type, invoice_no_norm, fin_year, invoice_no,
                       decision, ims_action, claim_pct, remark, state, tax_at_risk, is_rcm,
                       fingerprint, last_period_id, proposed_by, approved_at)
    VALUES (k.gstin, k.party_gstin, k.doc_type, k.invoice_no_norm, k.fin_year, k.invoice_no,
            p_decision, p_ims, p_claim_pct, p_remark, v_state, v_risk, k.is_rcm,
            k.fingerprint, k.period_id, v_uid,
            CASE WHEN v_state = 'APPROVED' THEN now() END)
    ON CONFLICT ON CONSTRAINT review_natural_key DO UPDATE
    SET invoice_no     = EXCLUDED.invoice_no,
        decision       = EXCLUDED.decision,
        ims_action     = EXCLUDED.ims_action,
        claim_pct      = EXCLUDED.claim_pct,
        remark         = EXCLUDED.remark,
        state          = EXCLUDED.state,
        tax_at_risk    = EXCLUDED.tax_at_risk,
        is_rcm         = EXCLUDED.is_rcm,
        fingerprint    = EXCLUDED.fingerprint,
        last_period_id = EXCLUDED.last_period_id,
        proposed_by    = EXCLUDED.proposed_by,
        proposed_at    = now(),
        approved_by    = NULL,
        approved_at    = EXCLUDED.approved_at,
        executed_on    = NULL
    RETURNING review_id INTO v_id;
    RETURN v_id;
END;
$$;

CREATE FUNCTION sp_approve(p_review_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_uid bigint;
BEGIN
    IF NOT app_role_is('rc_partner') THEN
        RAISE EXCEPTION 'only a partner can approve';
    END IF;
    v_uid := app_user_id();
    -- maker-checker: review_maker_checker raises check_violation if v_uid proposed it
    UPDATE review SET state = 'APPROVED', approved_by = v_uid, approved_at = now()
    WHERE review_id = p_review_id AND state = 'PROPOSED';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'review % is not awaiting approval', p_review_id;
    END IF;
END;
$$;

CREATE FUNCTION sp_mark_done(p_review_id bigint, p_executed_on date) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_state review_state;
BEGIN
    SELECT state INTO v_state FROM review WHERE review_id = p_review_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown review %', p_review_id;
    END IF;
    IF v_state <> 'APPROVED' THEN
        RAISE EXCEPTION 'review must be approved first';
    END IF;
    UPDATE review SET state = 'DONE', executed_on = p_executed_on
    WHERE review_id = p_review_id;
END;
$$;

-- Called by the API after sp_reconcile: reviews whose result changed go back to PROPOSED.
CREATE FUNCTION sp_refresh_reviews(p_import_id bigint) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_n int;
BEGIN
    UPDATE review rv SET state = 'PROPOSED', approved_by = NULL, approved_at = NULL
    WHERE rv.state = 'APPROVED'
      AND EXISTS (SELECT 1 FROM v_result_key k
                  WHERE k.import_id = p_import_id AND k.status <> 'DUPLICATE'
                    AND k.gstin = rv.gstin AND k.party_gstin = rv.party_gstin
                    AND k.doc_type = rv.doc_type AND k.invoice_no_norm = rv.invoice_no_norm
                    AND k.fin_year = rv.fin_year
                    AND rv.fingerprint IS DISTINCT FROM k.fingerprint);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END;
$$;

-- Work queue: every result of each period's current import (with its review, if any; a
-- DUPLICATE row shares the real invoice's key but never attaches to its review), plus
-- open DEFER/CHASE reviews carried from earlier periods of the same GSTIN until the
-- s.16(4) date (30 Nov after the document's FY ends; verify on gst.gov.in).
CREATE VIEW v_queue AS
SELECT * FROM (
    SELECT k.period_id, k.import_id, k.result_id, k.status, k.matched_by,
           k.party_gstin, k.invoice_no, k.diff_tax,
           abs(k.diff_tax)                                        AS tax_at_risk,
           rv.review_id, rv.decision, rv.state,
           (rv.review_id IS NOT NULL
            AND rv.fingerprint IS DISTINCT FROM k.fingerprint)    AS changed_since_review,
           NULL::text                                             AS carried_from_period,
           CASE WHEN k.status IN ('EXACT', 'ROUNDING') THEN 1 ELSE 0 END AS priority
    FROM v_result_key k
    LEFT JOIN review rv ON k.status <> 'DUPLICATE'
                       AND rv.gstin = k.gstin AND rv.party_gstin = k.party_gstin
                       AND rv.doc_type = k.doc_type AND rv.invoice_no_norm = k.invoice_no_norm
                       AND rv.fin_year = k.fin_year
    WHERE k.is_current

    UNION ALL

    SELECT p.period_id, NULL, NULL, NULL, NULL,
           rv.party_gstin, rv.invoice_no, NULL,
           rv.tax_at_risk,
           rv.review_id, rv.decision, rv.state,
           false,
           lp.period::text,
           0
    FROM return_period p
    JOIN review rv ON rv.gstin = p.gstin
    JOIN return_period lp ON lp.period_id = rv.last_period_id
    WHERE rv.state <> 'DONE'
      AND rv.decision IN ('DEFER', 'CHASE')
      AND period_start(lp.period) < period_start(p.period)
      AND period_start(p.period) <= make_date(substr(rv.fin_year, 1, 4)::int + 1, 11, 30)
      AND NOT EXISTS (
          SELECT 1 FROM v_result_key k
          WHERE k.period_id = p.period_id AND k.is_current AND k.status <> 'DUPLICATE'
            AND k.gstin = rv.gstin AND k.party_gstin = rv.party_gstin
            AND k.doc_type = rv.doc_type AND k.invoice_no_norm = rv.invoice_no_norm
            AND k.fin_year = rv.fin_year)
) q
ORDER BY period_id, priority, tax_at_risk DESC NULLS LAST;

-- Planned IMS action (review) vs the status observed on the portal (current import).
CREATE VIEW v_ims_plan_vs_observed AS
SELECT k.period_id, rv.review_id, rv.ims_action AS planned, k.ims_status AS observed,
       CASE rv.ims_action
           WHEN 'NONE'    THEN false
           WHEN 'ACCEPT'  THEN k.ims_status IS DISTINCT FROM 'ACCEPTED'
           WHEN 'REJECT'  THEN k.ims_status IS DISTINCT FROM 'REJECTED'
           WHEN 'PENDING' THEN k.ims_status IS DISTINCT FROM 'PENDING'
       END AS mismatch
FROM v_result_key k
JOIN review rv ON rv.gstin = k.gstin AND rv.party_gstin = k.party_gstin
              AND rv.doc_type = k.doc_type AND rv.invoice_no_norm = k.invoice_no_norm
              AND rv.fin_year = k.fin_year
WHERE k.is_current AND k.entry_id IS NOT NULL AND k.status <> 'DUPLICATE';

-- Workflow procedures run as owner; table grants and RLS arrive in Task 10.
GRANT USAGE ON SCHEMA public TO rc_partner, rc_staff, rc_client;
REVOKE EXECUTE ON FUNCTION sp_propose(bigint, review_decision, ims_action, numeric, text),
                           sp_approve(bigint), sp_mark_done(bigint, date),
                           sp_refresh_reviews(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sp_propose(bigint, review_decision, ims_action, numeric, text),
                          sp_approve(bigint), sp_mark_done(bigint, date),
                          sp_refresh_reviews(bigint) TO rc_partner, rc_staff;
