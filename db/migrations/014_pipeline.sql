-- 014: app layer support. Upload store, background job queue, login, and two read
-- views for the UI. Plan A (001-013) is unchanged; everything here is additive.
--
-- The pipeline: the API stores an upload and calls sp_enqueue_job as the user. A worker
-- (connected as rc_api) claims jobs with FOR UPDATE SKIP LOCKED, runs each one as the
-- user who enqueued it, then records the outcome with sp_finish_job / sp_fail_job.

-- ---------------------------------------------------------------------------------
-- Uploads: the raw file as received, one row per (period, kind, file content).
-- ---------------------------------------------------------------------------------
CREATE TABLE upload (
    upload_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id   bigint NOT NULL REFERENCES return_period(period_id),
    kind        text NOT NULL CHECK (kind IN ('GSTR2B', 'BOOKS')),
    filename    text NOT NULL,
    content     text NOT NULL,
    sha256      char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    uploaded_by bigint NOT NULL REFERENCES app_user(user_id),
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT upload_once UNIQUE (period_id, kind, sha256)
);

-- ---------------------------------------------------------------------------------
-- Job queue. App roles may only read it; every write goes through a procedure.
-- ---------------------------------------------------------------------------------
CREATE TABLE job_queue (
    job_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type     text NOT NULL
                 CHECK (job_type IN ('IMPORT_2B', 'IMPORT_BOOKS', 'RECONCILE', 'REFRESH_SCORE')),
    period_id    bigint NOT NULL REFERENCES return_period(period_id),
    upload_id    bigint REFERENCES upload(upload_id),
    import_id    bigint REFERENCES gstr2b_import(import_id),
    params       jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'QUEUED'
                 CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
    attempts     int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts int NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    run_after    timestamptz NOT NULL DEFAULT now(),
    lease_until  timestamptz,
    last_error   text,
    result       jsonb,
    created_by   bigint NOT NULL REFERENCES app_user(user_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz,
    CHECK (status <> 'RUNNING' OR lease_until IS NOT NULL)
);
CREATE INDEX job_queue_ready ON job_queue (status, run_after);
CREATE INDEX job_queue_period ON job_queue (period_id, job_id);

-- RLS: ENABLE (not FORCE). App roles are filtered by tenant; the table owner, which
-- runs the SECURITY DEFINER queue procedures, must see every job to claim it.
ALTER TABLE upload ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;

CREATE POLICY upload_read ON upload FOR SELECT
    USING (can_see_client(client_of_period(period_id)));
CREATE POLICY upload_insert ON upload FOR INSERT
    WITH CHECK (can_see_client(client_of_period(period_id))
                AND uploaded_by = current_app_user());
CREATE POLICY job_queue_read ON job_queue FOR SELECT
    USING (can_see_client(client_of_period(period_id)));

GRANT SELECT ON upload, job_queue TO rc_staff, rc_partner;
GRANT INSERT (period_id, kind, filename, content, sha256, uploaded_by) ON upload
    TO rc_staff, rc_partner;
GRANT USAGE ON SEQUENCE upload_upload_id_seq TO rc_staff, rc_partner;

-- ---------------------------------------------------------------------------------
-- sp_enqueue_job: called by the API (and by the worker for chained steps) as the user.
-- ---------------------------------------------------------------------------------
CREATE FUNCTION sp_enqueue_job(p_type text, p_period_id bigint,
                               p_upload_id bigint DEFAULT NULL,
                               p_import_id bigint DEFAULT NULL,
                               p_params jsonb DEFAULT '{}')
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_id bigint;
BEGIN
    PERFORM _guard_period(p_period_id);      -- 'not your client' / 'period is closed'
    IF p_upload_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM upload WHERE upload_id = p_upload_id AND period_id = p_period_id) THEN
        RAISE EXCEPTION 'upload does not belong to this period';
    END IF;
    IF p_import_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM gstr2b_import WHERE import_id = p_import_id AND period_id = p_period_id) THEN
        RAISE EXCEPTION 'import does not belong to this period';
    END IF;
    INSERT INTO job_queue(job_type, period_id, upload_id, import_id, params, created_by)
    VALUES (p_type, p_period_id, p_upload_id, p_import_id, coalesce(p_params, '{}'),
            app_user_id())
    RETURNING job_id INTO v_id;
    RETURN v_id;
END;
$$;

-- ---------------------------------------------------------------------------------
-- sp_claim_job: the worker takes one ready job. FOR UPDATE SKIP LOCKED means two
-- workers polling at once each get a different row (or none) and never block.
-- A RUNNING job whose lease has expired belongs to a worker that died: it is ready
-- again, unless that was its last attempt, in which case it is marked FAILED.
-- ---------------------------------------------------------------------------------
CREATE FUNCTION sp_claim_job(p_lease interval DEFAULT '5 minutes')
RETURNS SETOF job_queue
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE job_queue j
       SET status = 'FAILED', lease_until = NULL, finished_at = now(),
           last_error = coalesce(j.last_error || '; ', '') ||
                        'worker stopped while running the final attempt'
     WHERE j.job_id IN (SELECT job_id FROM job_queue
                         WHERE status = 'RUNNING' AND lease_until < now()
                           AND attempts >= max_attempts
                         FOR UPDATE SKIP LOCKED);

    RETURN QUERY
    UPDATE job_queue j
       SET status = 'RUNNING', attempts = j.attempts + 1, started_at = now(),
           lease_until = now() + p_lease
      FROM (SELECT job_id FROM job_queue
             WHERE (status = 'QUEUED' AND run_after <= now())
                OR (status = 'RUNNING' AND lease_until < now())
             ORDER BY run_after, job_id
             FOR UPDATE SKIP LOCKED
             LIMIT 1) c
     WHERE j.job_id = c.job_id
    RETURNING j.*;
END;
$$;

-- p_attempt is the attempt number the worker claimed. If the lease expired and another
-- worker has since claimed the job, this worker's late outcome is refused.
CREATE FUNCTION sp_finish_job(p_job_id bigint, p_result jsonb, p_attempt int DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE job_queue
       SET status = 'DONE', result = p_result, finished_at = now(), lease_until = NULL
     WHERE job_id = p_job_id AND status = 'RUNNING'
       AND (p_attempt IS NULL OR attempts = p_attempt);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'job % is no longer running for this worker', p_job_id;
    END IF;
END;
$$;

-- Retry with linear backoff (30 s per attempt so far), then FAILED.
CREATE FUNCTION sp_fail_job(p_job_id bigint, p_error text, p_attempt int DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE job_queue
       SET last_error = p_error,
           lease_until = NULL,
           status = CASE WHEN attempts < max_attempts THEN 'QUEUED' ELSE 'FAILED' END,
           run_after = CASE WHEN attempts < max_attempts
                            THEN now() + attempts * interval '30 seconds' ELSE run_after END,
           finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END
     WHERE job_id = p_job_id AND status = 'RUNNING'
       AND (p_attempt IS NULL OR attempts = p_attempt);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'job % is no longer running for this worker', p_job_id;
    END IF;
END;
$$;

-- Who a claimed job runs as. The worker connects as rc_api, which has no table grants,
-- so it asks here for the enqueuer's identity, then SET ROLEs to it. Only RUNNING jobs
-- answer, so a job is never run as anyone before it has been claimed.
CREATE FUNCTION fn_job_runner(p_job_id bigint)
RETURNS TABLE(user_id bigint, role user_role, client_id bigint, firm_id bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT u.user_id, u.role, u.client_id, u.firm_id
      FROM job_queue j JOIN app_user u ON u.user_id = j.created_by
     WHERE j.job_id = p_job_id AND j.status = 'RUNNING'
$$;
REVOKE EXECUTE ON FUNCTION fn_job_runner(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_job_runner(bigint) TO rc_api;

-- ---------------------------------------------------------------------------------
-- Login. App roles cannot read password_hash, so the check runs as the owner and
-- returns only who the user is. Called by rc_api before any SET ROLE.
-- ---------------------------------------------------------------------------------
CREATE FUNCTION fn_login(p_email text, p_password text)
RETURNS TABLE(user_id bigint, role user_role, client_id bigint, firm_id bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT u.user_id, u.role, u.client_id, u.firm_id
      FROM app_user u
     WHERE lower(u.email) = lower(p_email)
       -- Only bcrypt hashes are checked; anything else (a disabled account) never matches
       -- and never reaches crypt(), which would raise on an invalid salt.
       AND CASE WHEN u.password_hash ~ '^\$2[abxy]\$'
                THEN u.password_hash = crypt(p_password, u.password_hash)
                ELSE false END
$$;

REVOKE EXECUTE ON FUNCTION sp_enqueue_job(text, bigint, bigint, bigint, jsonb),
                           sp_claim_job(interval), sp_finish_job(bigint, jsonb, int),
                           sp_fail_job(bigint, text, int), fn_login(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sp_enqueue_job(text, bigint, bigint, bigint, jsonb)
    TO rc_staff, rc_partner;
-- rc_api is NOINHERIT, so the worker's procedures are granted to it directly.
GRANT EXECUTE ON FUNCTION sp_claim_job(interval), sp_finish_job(bigint, jsonb, int),
                          sp_fail_job(bigint, text, int), fn_login(text, text) TO rc_api;

-- The API and worker log in as rc_api. Its password is set at deploy time, never here.
ALTER ROLE rc_api LOGIN;
GRANT rc_partner, rc_staff, rc_client TO rc_api;

-- ---------------------------------------------------------------------------------
-- UI read views (security_invoker: RLS of the caller applies to every base table).
-- ---------------------------------------------------------------------------------
CREATE VIEW v_client_periods WITH (security_invoker = true) AS
SELECT c.client_id, c.name AS client_name, p.gstin, p.period_id, p.period, p.state,
       p.fin_year
  FROM return_period p
  JOIN gstin_reg g ON g.gstin = p.gstin
  JOIN client c ON c.client_id = g.client_id;

CREATE VIEW v_result_detail WITH (security_invoker = true) AS
SELECT r.result_id, r.import_id, gi.period_id, r.status, r.matched_by, r.diff_tax,
       i.invoice_no AS books_invoice_no, i.invoice_date AS books_date,
       bl.taxable AS books_taxable, bl.tax AS books_tax,
       e.invoice_no AS portal_invoice_no, e.invoice_date AS portal_date,
       e.taxable_value AS portal_taxable,
       e.igst + e.cgst + e.sgst + e.cess AS portal_tax,
       coalesce(i.party_gstin, e.party_gstin) AS party_gstin,
       coalesce(pt.name, e.party_name) AS party_name
  FROM recon_result r
  JOIN gstr2b_import gi ON gi.import_id = r.import_id
  LEFT JOIN invoice i ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy
  LEFT JOIN LATERAL (
        SELECT sum(l.taxable_value) AS taxable,
               sum(l.igst + l.cgst + l.sgst + l.cess) AS tax
          FROM invoice_line l
         WHERE l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year) bl ON true
  LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id AND e.fin_year = r.entry_fy
  LEFT JOIN party pt ON pt.gstin = coalesce(i.party_gstin, e.party_gstin);

GRANT SELECT ON v_client_periods TO rc_partner, rc_staff, rc_client;
GRANT SELECT ON v_result_detail TO rc_partner, rc_staff;
