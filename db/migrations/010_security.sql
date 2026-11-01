-- 010_security.sql: grants (DCL), column privileges, row-level security, tenant guards.
--
-- Model: the API logs in as rc_api, then per request SET LOCAL ROLE rc_<role> and
-- set_config('app.user_id', ...). Policies decide row visibility with can_see_client().
-- The import / matching procedures (sp_open_period, sp_import_gstr2b, sp_import_books,
-- sp_reconcile) are SECURITY INVOKER, so they run under the caller's grants and RLS.
-- The workflow procedures (008/009) are SECURITY DEFINER and bypass RLS; the tenant
-- guard triggers below stop them from writing another client's review or period.
-- Superusers (the test/migration owner) bypass RLS even with FORCE.

-- ---------------------------------------------------------------------------------------
-- Identity helpers.

-- Fail closed for rc_api: with role = 'none' only a superuser session counts as any
-- application role (rc_api is a NOINHERIT member of all three groups, and 'MEMBER' would
-- otherwise say yes before it has chosen one).
CREATE OR REPLACE FUNCTION app_role_is(p_role text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN current_setting('role') = 'none'
                THEN coalesce((SELECT rolsuper FROM pg_catalog.pg_roles
                               WHERE rolname = session_user), false)
                ELSE pg_catalog.pg_has_role(current_setting('role'), p_role, 'MEMBER') END
$$;

-- Like app_user_id() but returns NULL instead of raising when app.user_id is unset.
CREATE FUNCTION current_app_user() RETURNS bigint
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::bigint
$$;

CREATE FUNCTION can_see_client(p_client_id bigint) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_uid bigint := current_app_user();
BEGIN
    IF current_setting('role') = 'none'
       AND coalesce((SELECT rolsuper FROM pg_catalog.pg_roles
                     WHERE rolname = session_user), false) THEN
        RETURN true;
    END IF;
    IF v_uid IS NULL OR p_client_id IS NULL THEN
        RETURN false;
    END IF;
    IF app_role_is('rc_partner') THEN
        RETURN EXISTS (SELECT 1 FROM client c JOIN app_user u ON u.firm_id = c.firm_id
                       WHERE c.client_id = p_client_id AND u.user_id = v_uid
                         AND u.role = 'partner');
    ELSIF app_role_is('rc_staff') THEN
        RETURN EXISTS (SELECT 1 FROM staff_assignment a
                       JOIN app_user u ON u.user_id = a.user_id
                       JOIN client c ON c.client_id = a.client_id AND c.firm_id = u.firm_id
                       WHERE a.user_id = v_uid AND a.client_id = p_client_id
                         AND u.role = 'staff');
    ELSIF app_role_is('rc_client') THEN
        -- the client must also belong to the user's firm (no cross-firm client users)
        RETURN EXISTS (SELECT 1 FROM app_user u
                       JOIN client c ON c.client_id = u.client_id AND c.firm_id = u.firm_id
                       WHERE u.user_id = v_uid AND u.role = 'client'
                         AND u.client_id = p_client_id);
    END IF;
    RETURN false;
END;
$$;

-- The current user's firm (NULL when unset); used by partner write policies.
CREATE FUNCTION current_app_firm() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT firm_id FROM app_user WHERE user_id = current_app_user()
$$;

CREATE FUNCTION client_of_gstin(p_gstin text) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT client_id FROM gstin_reg WHERE gstin = p_gstin
$$;

CREATE FUNCTION client_of_period(p_period_id bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT g.client_id FROM return_period p JOIN gstin_reg g ON g.gstin = p.gstin
    WHERE p.period_id = p_period_id
$$;

CREATE FUNCTION client_of_import(p_import_id bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT g.client_id FROM gstr2b_import i
    JOIN return_period p ON p.period_id = i.period_id
    JOIN gstin_reg g ON g.gstin = p.gstin
    WHERE i.import_id = p_import_id
$$;

-- ---------------------------------------------------------------------------------------
-- Tenant guard for SECURITY DEFINER writers (they bypass RLS). Named trg_0_* so it fires
-- before the other BEFORE triggers on these tables (triggers fire in name order).
CREATE FUNCTION trg_tenant_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT can_see_client(client_of_gstin(NEW.gstin))
       OR (TG_OP = 'UPDATE' AND NOT can_see_client(client_of_gstin(OLD.gstin))) THEN
        RAISE EXCEPTION 'not your client';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_0_tenant_guard BEFORE INSERT OR UPDATE ON review
FOR EACH ROW EXECUTE FUNCTION trg_tenant_guard();
CREATE TRIGGER trg_0_tenant_guard BEFORE INSERT OR UPDATE ON return_period
FOR EACH ROW EXECUTE FUNCTION trg_tenant_guard();

-- ---------------------------------------------------------------------------------------
-- A result row may only point at documents of its own import: the books invoice must be
-- an INWARD invoice of the import's GSTIN (any earlier period: CROSS_PERIOD), and the 2B
-- entry must come from the same import. FK checks ignore RLS, so without this a caller
-- could attach another client's document to their own import and read it back through
-- v_result_key / sp_propose. Incomplete (id, fy) pairs are left to the CHECK constraints.
CREATE FUNCTION trg_result_binding() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_gstin text;
BEGIN
    SELECT p.gstin INTO v_gstin
    FROM gstr2b_import i JOIN return_period p ON p.period_id = i.period_id
    WHERE i.import_id = NEW.import_id;
    IF (NEW.invoice_id IS NOT NULL AND NEW.invoice_fy IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM invoice i
                        WHERE i.invoice_id = NEW.invoice_id AND i.fin_year = NEW.invoice_fy
                          AND i.gstin = v_gstin AND i.direction = 'INWARD'))
       OR (NEW.entry_id IS NOT NULL AND NEW.entry_fy IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM gstr2b_entry e
                           WHERE e.entry_id = NEW.entry_id AND e.fin_year = NEW.entry_fy
                             AND e.import_id = NEW.import_id)) THEN
        RAISE EXCEPTION 'result does not belong to this import';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION trg_result_binding() FROM PUBLIC;

CREATE TRIGGER trg_result_binding BEFORE INSERT OR UPDATE ON recon_result
FOR EACH ROW EXECUTE FUNCTION trg_result_binding();

-- ---------------------------------------------------------------------------------------
-- Month state machine (replaces 009's body). Moves into or out of CLOSED must go through
-- sp_close_period / sp_reopen_period (or the partner-import reopen in trg_closed_readonly)
-- so their guards and reasons cannot be skipped by a direct UPDATE.
-- The function is now SECURITY INVOKER: current_user is then the role running the UPDATE,
-- i.e. the owner inside a SECURITY DEFINER procedure/trigger, or the application role
-- (rc_partner, rc_staff, rc_client, or rc_api with no role set) for a direct statement.
-- Superusers (admin, tests) may still update directly. All other checks are unchanged.
CREATE OR REPLACE FUNCTION trg_period_transition() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;
    IF (NEW.state = 'CLOSED' OR OLD.state = 'CLOSED')
       AND NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles
                         WHERE rolname = current_user), false)
       AND (pg_catalog.pg_has_role(current_user, 'rc_partner', 'MEMBER')
            OR pg_catalog.pg_has_role(current_user, 'rc_staff', 'MEMBER')
            OR pg_catalog.pg_has_role(current_user, 'rc_client', 'MEMBER')) THEN
        RAISE EXCEPTION 'use sp_close_period / sp_reopen_period';
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

-- ---------------------------------------------------------------------------------------
-- Row-level security. One FOR ALL policy per tenant table (USING also serves as WITH
-- CHECK). Policies on partitioned parents apply to every query through the parent;
-- partitions get no grants, so they cannot be queried directly.

ALTER TABLE client           ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user         ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE gstin_reg        ENABLE ROW LEVEL SECURITY;
ALTER TABLE return_period    ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice          ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_line     ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment          ENABLE ROW LEVEL SECURITY;
ALTER TABLE books_staging    ENABLE ROW LEVEL SECURITY;
ALTER TABLE gstr2b_import    ENABLE ROW LEVEL SECURITY;
ALTER TABLE gstr2b_entry     ENABLE ROW LEVEL SECURITY;
ALTER TABLE recon_result     ENABLE ROW LEVEL SECURITY;
ALTER TABLE review           ENABLE ROW LEVEL SECURITY;

ALTER TABLE client           FORCE ROW LEVEL SECURITY;
ALTER TABLE app_user         FORCE ROW LEVEL SECURITY;
ALTER TABLE staff_assignment FORCE ROW LEVEL SECURITY;
ALTER TABLE gstin_reg        FORCE ROW LEVEL SECURITY;
ALTER TABLE return_period    FORCE ROW LEVEL SECURITY;
ALTER TABLE invoice          FORCE ROW LEVEL SECURITY;
ALTER TABLE invoice_line     FORCE ROW LEVEL SECURITY;
ALTER TABLE payment          FORCE ROW LEVEL SECURITY;
ALTER TABLE books_staging    FORCE ROW LEVEL SECURITY;
ALTER TABLE gstr2b_import    FORCE ROW LEVEL SECURITY;
ALTER TABLE gstr2b_entry     FORCE ROW LEVEL SECURITY;
ALTER TABLE recon_result     FORCE ROW LEVEL SECURITY;
ALTER TABLE review           FORCE ROW LEVEL SECURITY;

-- client: visible per can_see_client; a partner may add clients to their own firm.
CREATE POLICY client_read ON client FOR SELECT USING (can_see_client(client_id));
CREATE POLICY client_insert ON client FOR INSERT
    WITH CHECK (app_role_is('rc_partner') AND firm_id = current_app_firm());
CREATE POLICY client_update ON client FOR UPDATE
    USING (can_see_client(client_id))
    WITH CHECK (app_role_is('rc_partner') AND firm_id = current_app_firm());

-- app_user: everyone sees themselves; a partner sees and manages the users of the firm.
CREATE POLICY app_user_tenant ON app_user
    USING (user_id = current_app_user()
           OR (app_role_is('rc_partner') AND firm_id = current_app_firm()))
    WITH CHECK (app_role_is('rc_partner') AND firm_id = current_app_firm()
                AND (client_id IS NULL OR can_see_client(client_id)));

CREATE POLICY staff_assignment_tenant ON staff_assignment
    USING (can_see_client(client_id));
CREATE POLICY gstin_reg_tenant ON gstin_reg
    USING (can_see_client(client_id));
CREATE POLICY return_period_tenant ON return_period
    USING (can_see_client(client_of_gstin(gstin)));
CREATE POLICY invoice_tenant ON invoice
    USING (can_see_client(client_of_gstin(gstin)));
-- Children reach the client through their (itself RLS-filtered) parent row.
CREATE POLICY invoice_line_tenant ON invoice_line
    USING (EXISTS (SELECT 1 FROM invoice i
                   WHERE i.invoice_id = invoice_line.invoice_id
                     AND i.fin_year = invoice_line.fin_year));
CREATE POLICY payment_tenant ON payment
    USING (EXISTS (SELECT 1 FROM invoice i
                   WHERE i.invoice_id = payment.invoice_id
                     AND i.fin_year = payment.fin_year));
CREATE POLICY books_staging_tenant ON books_staging
    USING (can_see_client(client_of_period(period_id)));
CREATE POLICY gstr2b_import_tenant ON gstr2b_import
    USING (can_see_client(client_of_period(period_id)));
CREATE POLICY gstr2b_entry_tenant ON gstr2b_entry
    USING (can_see_client(client_of_import(import_id)));
CREATE POLICY recon_result_tenant ON recon_result
    USING (can_see_client(client_of_import(import_id)));
CREATE POLICY review_tenant ON review
    USING (can_see_client(client_of_gstin(gstin)));

-- audit_log has no RLS: only partners may read it (all firms' rows; single-firm demo).

-- ---------------------------------------------------------------------------------------
-- Views run with the caller's privileges and RLS.
ALTER VIEW v_invoice_total        SET (security_invoker = true);
ALTER VIEW v_rate_warnings        SET (security_invoker = true);
ALTER VIEW v_excluded_itc         SET (security_invoker = true);
ALTER VIEW v_books_candidate      SET (security_invoker = true);
ALTER VIEW v_naive_match          SET (security_invoker = true);
ALTER VIEW v_result_key           SET (security_invoker = true);
ALTER VIEW v_queue                SET (security_invoker = true);
ALTER VIEW v_ims_plan_vs_observed SET (security_invoker = true);

-- ---------------------------------------------------------------------------------------
-- Grants. Start from nothing for the application roles.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, rc_partner, rc_staff, rc_client;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, rc_partner, rc_staff, rc_client;

-- Global reference data: read-only for everyone.
GRANT SELECT ON taxpayer_type, hsn_sac, tax_rate, filing_rule, period_transition
    TO rc_partner, rc_staff, rc_client;
-- party is global; the imports upsert it.
GRANT SELECT ON party TO rc_client;
GRANT SELECT, INSERT, UPDATE ON party TO rc_staff, rc_partner;

-- Tenant tables: read for all three roles (RLS limits rows) ...
GRANT SELECT ON firm, client, app_user, staff_assignment, gstin_reg, return_period,
                invoice, invoice_line, payment, books_staging, gstr2b_import, gstr2b_entry,
                recon_result, review
    TO rc_partner, rc_staff, rc_client;
GRANT SELECT ON v_invoice_total, v_rate_warnings, v_excluded_itc, v_books_candidate,
                v_naive_match, v_result_key, v_queue, v_ims_plan_vs_observed
    TO rc_partner, rc_staff, rc_client;

-- ... and the DML the invoker procedures need, for staff and partners.
GRANT INSERT, UPDATE, DELETE ON invoice, invoice_line, payment, books_staging,
                                gstr2b_entry, recon_result
    TO rc_staff, rc_partner;
-- 2B imports are versioned history: add a version, flip is_current; never rewrite.
GRANT INSERT ON gstr2b_import TO rc_staff, rc_partner;
GRANT UPDATE (is_current) ON gstr2b_import TO rc_staff, rc_partner;
-- sp_open_period inserts (state takes its default); imports move state, which
-- trg_period_transition guards. Everything else is written by definer procedures.
GRANT INSERT (gstin, period, fin_year) ON return_period TO rc_staff, rc_partner;
GRANT UPDATE (state) ON return_period TO rc_staff, rc_partner;
-- Reviews are created by sp_propose (definer); staff may edit only the decision columns.
GRANT UPDATE (decision, ims_action, claim_pct, remark) ON review TO rc_staff, rc_partner;

-- Partners manage clients, registrations, users and assignments of their firm.
GRANT INSERT, UPDATE ON client, gstin_reg, app_user TO rc_partner;
GRANT INSERT, DELETE ON staff_assignment TO rc_partner;

-- Identity sequences of the tables the roles insert into. (Identity columns do not
-- check sequence privileges on INSERT; granted for explicit nextval/currval use.)
GRANT USAGE ON SEQUENCE invoice_invoice_id_seq, invoice_line_line_id_seq,
                        payment_payment_id_seq, books_staging_staging_id_seq,
                        gstr2b_import_import_id_seq, gstr2b_entry_entry_id_seq,
                        recon_result_result_id_seq, return_period_period_id_seq
    TO rc_staff, rc_partner;
GRANT USAGE ON SEQUENCE client_client_id_seq, app_user_user_id_seq TO rc_partner;

-- audit_log: append-only via trg_audit (definer); partners may read; nobody else.
GRANT SELECT ON audit_log TO rc_partner;

-- rc_client: no DML anywhere; its one write is EXECUTE on sp_client_approve (009).
