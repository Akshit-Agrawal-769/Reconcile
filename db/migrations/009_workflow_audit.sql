-- 009_workflow_audit.sql: month state machine (transitions as data), guarded close,
-- closed-month read-only rule, and the partitioned audit log.

-- ---------------------------------------------------------------------------------------
-- State machine. Each row is one allowed move; allowed_role NULL = any caller.
-- "staff or partner" is two rows. Role checks use app_role_is() (008), so they see the
-- SET ROLE target even inside SECURITY DEFINER code.
-- No extra rows are needed for the automatic moves: a first import is OPEN -> IN_REVIEW
-- (any), an import into AWAITING_APPROVAL is AWAITING_APPROVAL -> IN_REVIEW (any), and a
-- partner import into a CLOSED month is CLOSED -> IN_REVIEW (partner).
CREATE TABLE period_transition (
    from_state   period_state NOT NULL,
    to_state     period_state NOT NULL,
    allowed_role text CHECK (allowed_role IN ('rc_partner', 'rc_staff', 'rc_client')),
    UNIQUE NULLS NOT DISTINCT (from_state, to_state, allowed_role),
    CHECK (from_state <> to_state)
);
INSERT INTO period_transition(from_state, to_state, allowed_role) VALUES
    ('OPEN',              'IN_REVIEW',         NULL),
    ('IN_REVIEW',         'AWAITING_APPROVAL', 'rc_staff'),
    ('IN_REVIEW',         'AWAITING_APPROVAL', 'rc_partner'),
    ('AWAITING_APPROVAL', 'IN_REVIEW',         NULL),
    ('AWAITING_APPROVAL', 'CLOSED',            'rc_partner'),
    ('CLOSED',            'IN_REVIEW',         'rc_partner');

-- Why the month was last reopened (the override reason is cleared on reopen; history
-- of both lives in audit_log).
ALTER TABLE return_period ADD COLUMN reopen_reason text;

CREATE FUNCTION trg_period_transition() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM period_transition t
                   WHERE t.from_state = OLD.state AND t.to_state = NEW.state
                     AND (t.allowed_role IS NULL OR app_role_is(t.allowed_role))) THEN
        RAISE EXCEPTION 'illegal period transition % -> %', OLD.state, NEW.state;
    END IF;
    -- Reopening a closed month: the approval and close no longer describe the numbers,
    -- so they must be given again. AWAITING_APPROVAL -> IN_REVIEW (a Pass-2 import)
    -- keeps the client's approval.
    IF NEW.state = 'IN_REVIEW' AND OLD.state = 'CLOSED' THEN
        NEW.client_approved_by := NULL;
        NEW.client_approved_at := NULL;
        NEW.override_reason    := NULL;
        NEW.closed_by          := NULL;
        NEW.closed_at          := NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_period_transition BEFORE UPDATE OF state ON return_period
FOR EACH ROW EXECUTE FUNCTION trg_period_transition();

-- ---------------------------------------------------------------------------------------
-- Workflow procedures.

CREATE FUNCTION sp_submit_period(p_period_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT (app_role_is('rc_staff') OR app_role_is('rc_partner')) THEN
        RAISE EXCEPTION 'only staff or a partner can submit';
    END IF;
    PERFORM app_user_id();  -- raises if unset: the audit row must name who submitted
    UPDATE return_period SET state = 'AWAITING_APPROVAL' WHERE period_id = p_period_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
END;
$$;

CREATE FUNCTION sp_client_approve(p_period_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_uid bigint := app_user_id();
    v_state period_state;
BEGIN
    SELECT p.state INTO v_state
    FROM return_period p
    JOIN gstin_reg g ON g.gstin = p.gstin
    JOIN app_user u ON u.client_id = g.client_id AND u.role = 'client' AND u.user_id = v_uid
    WHERE p.period_id = p_period_id
    FOR UPDATE OF p;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only the client can approve this period';
    END IF;
    IF v_state <> 'AWAITING_APPROVAL' THEN
        RAISE EXCEPTION 'period is not awaiting approval';
    END IF;
    UPDATE return_period SET client_approved_by = v_uid, client_approved_at = now()
    WHERE period_id = p_period_id;
END;
$$;

-- Partner closes without the client's approval; client_approved_by stays NULL and the
-- close guard accepts the stored reason instead.
CREATE FUNCTION sp_partner_override(p_period_id bigint, p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_state period_state;
BEGIN
    IF NOT app_role_is('rc_partner') THEN
        RAISE EXCEPTION 'only a partner can override';
    END IF;
    IF coalesce(btrim(p_reason), '') = '' THEN
        RAISE EXCEPTION 'override reason required';
    END IF;
    SELECT state INTO v_state FROM return_period WHERE period_id = p_period_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
    IF v_state <> 'AWAITING_APPROVAL' THEN
        RAISE EXCEPTION 'period is not awaiting approval';
    END IF;
    UPDATE return_period SET override_reason = btrim(p_reason) WHERE period_id = p_period_id;
END;
$$;

CREATE FUNCTION sp_close_period(p_period_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_uid bigint;
    p return_period%ROWTYPE;
BEGIN
    IF NOT app_role_is('rc_partner') THEN
        RAISE EXCEPTION 'only a partner can close';
    END IF;
    v_uid := app_user_id();
    SELECT * INTO p FROM return_period WHERE period_id = p_period_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
    IF p.client_approved_by IS NULL AND p.override_reason IS NULL THEN
        RAISE EXCEPTION 'client approval missing';
    END IF;
    IF EXISTS (SELECT 1 FROM v_queue q WHERE q.period_id = p_period_id
                                         AND q.state = 'PROPOSED') THEN
        RAISE EXCEPTION 'reviews awaiting approval';
    END IF;
    IF EXISTS (SELECT 1 FROM v_queue q WHERE q.period_id = p_period_id
                                         AND q.changed_since_review) THEN
        RAISE EXCEPTION 'changed items not reviewed';
    END IF;
    IF p.state = 'CLOSED' THEN  -- same-state updates bypass the transition trigger
        RAISE EXCEPTION 'illegal period transition CLOSED -> CLOSED';
    END IF;
    -- trg_period_transition enforces AWAITING_APPROVAL -> CLOSED
    UPDATE return_period SET state = 'CLOSED', closed_by = v_uid, closed_at = now()
    WHERE period_id = p_period_id;
END;
$$;

CREATE FUNCTION sp_reopen_period(p_period_id bigint, p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_state period_state;
BEGIN
    IF NOT app_role_is('rc_partner') THEN
        RAISE EXCEPTION 'only a partner can reopen';
    END IF;
    IF coalesce(btrim(p_reason), '') = '' THEN
        RAISE EXCEPTION 'reopen reason required';
    END IF;
    SELECT state INTO v_state FROM return_period WHERE period_id = p_period_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
    IF v_state <> 'CLOSED' THEN
        RAISE EXCEPTION 'illegal period transition % -> IN_REVIEW', v_state;
    END IF;
    UPDATE return_period SET state = 'IN_REVIEW', reopen_reason = btrim(p_reason)
    WHERE period_id = p_period_id;
END;
$$;

-- ---------------------------------------------------------------------------------------
-- Closed month is read-only for non-partners.
-- review: the period is NEW.last_period_id. Strict on INSERT or when last_period_id
--   changes. On an UPDATE that keeps last_period_id (sp_mark_done, a carried review
--   worked in a later month), a CLOSED period is allowed only if the GSTIN has a
--   chronologically later month that is not CLOSED; otherwise it raises.
-- gstr2b_import / books_staging: the period is NEW.period_id. A new import (2B or books)
--   by a partner reopens a CLOSED month, and any new import sends an AWAITING_APPROVAL
--   month back to IN_REVIEW; both moves go through trg_period_transition.
CREATE FUNCTION trg_closed_readonly() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_pid bigint;
    p return_period%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'review' THEN
        v_pid := NEW.last_period_id;
    ELSE
        v_pid := NEW.period_id;
    END IF;
    SELECT * INTO p FROM return_period WHERE period_id = v_pid;
    IF p.state = 'CLOSED' AND NOT app_role_is('rc_partner') THEN
        -- nested IFs: NEW.last_period_id must not be evaluated for the import tables
        IF TG_TABLE_NAME = 'review' AND TG_OP = 'UPDATE' THEN
            IF NEW.last_period_id IS NOT DISTINCT FROM OLD.last_period_id
               AND EXISTS (SELECT 1 FROM return_period l
                           WHERE l.gstin = NEW.gstin AND l.state <> 'CLOSED'
                             AND period_start(l.period) > period_start(p.period)) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION 'period is closed';
    END IF;
    IF TG_TABLE_NAME <> 'review' AND p.state IN ('CLOSED', 'AWAITING_APPROVAL') THEN
        UPDATE return_period SET state = 'IN_REVIEW' WHERE period_id = v_pid;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_closed_readonly BEFORE INSERT OR UPDATE ON review
FOR EACH ROW EXECUTE FUNCTION trg_closed_readonly();
CREATE TRIGGER trg_closed_readonly BEFORE INSERT ON gstr2b_import
FOR EACH ROW EXECUTE FUNCTION trg_closed_readonly();
CREATE TRIGGER trg_closed_readonly BEFORE INSERT ON books_staging
FOR EACH ROW EXECUTE FUNCTION trg_closed_readonly();

-- Results of a CLOSED month are read-only for every role (spec §8): a partner must
-- reopen it, or import into it (which reopens), before re-running sp_reconcile.
CREATE FUNCTION trg_results_closed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_import bigint := CASE WHEN TG_OP = 'DELETE' THEN OLD.import_id ELSE NEW.import_id END;
BEGIN
    IF EXISTS (SELECT 1 FROM gstr2b_import gi
               JOIN return_period p ON p.period_id = gi.period_id
               WHERE gi.import_id = v_import AND p.state = 'CLOSED')
       OR (TG_OP = 'UPDATE' AND EXISTS (
               SELECT 1 FROM gstr2b_import gi
               JOIN return_period p ON p.period_id = gi.period_id
               WHERE gi.import_id = OLD.import_id AND p.state = 'CLOSED')) THEN
        RAISE EXCEPTION 'period is closed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER trg_results_closed BEFORE INSERT OR UPDATE OR DELETE ON recon_result
FOR EACH ROW EXECUTE FUNCTION trg_results_closed();

-- Carry-forward: a refreshed review now belongs to the month whose import changed it
-- (like sp_propose), so later work on it is checked against that month. Otherwise
-- identical to 008.
CREATE OR REPLACE FUNCTION sp_refresh_reviews(p_import_id bigint) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_n int;
    v_pid bigint;
BEGIN
    SELECT period_id INTO v_pid FROM gstr2b_import WHERE import_id = p_import_id;
    UPDATE review rv SET state = 'PROPOSED', approved_by = NULL, approved_at = NULL,
                         last_period_id = v_pid
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

-- ---------------------------------------------------------------------------------------
-- Audit log, partitioned by the FY in which the event happened (R2: plain column).
CREATE TABLE audit_log (
    log_id     bigint GENERATED ALWAYS AS IDENTITY,
    fin_year   text NOT NULL,
    user_id    bigint,                     -- app.user_id at the time; NULL if unset
    table_name text NOT NULL,
    row_id     bigint NOT NULL,
    action     text NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    old_value  jsonb,
    new_value  jsonb,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (log_id, fin_year)
) PARTITION BY LIST (fin_year);

CREATE TABLE audit_log_2025_26 PARTITION OF audit_log FOR VALUES IN ('2025-26');
CREATE TABLE audit_log_2026_27 PARTITION OF audit_log FOR VALUES IN ('2026-27');
CREATE TABLE audit_log_2027_28 PARTITION OF audit_log FOR VALUES IN ('2027-28');
CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT;

CREATE INDEX audit_log_row_idx ON audit_log (table_name, row_id);

-- TG_ARGV[0] = primary-key column. gstr2b_import.payload is dropped to keep rows small.
CREATE FUNCTION trg_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_old jsonb;
    v_new jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD) - 'payload'; END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW) - 'payload'; END IF;
    INSERT INTO audit_log(fin_year, user_id, table_name, row_id, action, old_value, new_value)
    VALUES (fin_year_of(current_date),
            nullif(current_setting('app.user_id', true), '')::bigint,
            TG_TABLE_NAME,
            (coalesce(v_new, v_old) ->> TG_ARGV[0])::bigint,
            TG_OP, v_old, v_new);
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON review
FOR EACH ROW EXECUTE FUNCTION trg_audit('review_id');
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON return_period
FOR EACH ROW EXECUTE FUNCTION trg_audit('period_id');
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON gstr2b_import
FOR EACH ROW EXECUTE FUNCTION trg_audit('import_id');

-- ---------------------------------------------------------------------------------------
-- Procedures run as owner; table grants and RLS arrive in Task 10.
REVOKE EXECUTE ON FUNCTION sp_submit_period(bigint), sp_client_approve(bigint),
                           sp_partner_override(bigint, text), sp_close_period(bigint),
                           sp_reopen_period(bigint, text),
                           trg_period_transition(), trg_closed_readonly(), trg_results_closed(),
                           trg_audit()
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sp_submit_period(bigint) TO rc_staff, rc_partner;
GRANT EXECUTE ON FUNCTION sp_client_approve(bigint) TO rc_client;
GRANT EXECUTE ON FUNCTION sp_partner_override(bigint, text), sp_close_period(bigint),
                          sp_reopen_period(bigint, text) TO rc_partner;
