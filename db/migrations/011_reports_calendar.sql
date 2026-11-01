-- 011_reports_calendar.sql: period reports, client board and summary, IMS plan, ITC ledger,
-- Rule 37 ageing, party score (materialized), compliance calendar and alerts.
--
-- Every report reads a period's CURRENT import through v_result_key / v_queue, and every
-- view is security_invoker, so RLS on the underlying tables decides which rows a caller
-- sees (exception: v_party_score, see below).

-- "Today" for every date-relative view: app.today (tests, demos) or current_date.
CREATE FUNCTION app_today() RETURNS date
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT coalesce(nullif(current_setting('app.today', true), '')::date, current_date)
$$;

-- s.16(4) last date for a document FY: 30 November after the FY ends (verify on gst.gov.in).
CREATE FUNCTION s16_4_date(p_fin_year text) RETURNS date
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT make_date(substr(p_fin_year, 1, 4)::int + 1, 11, 30)
$$;
COMMENT ON FUNCTION s16_4_date(text) IS 's.16(4) time limit (30 Nov after FY end); verify on gst.gov.in';

-- ---------------------------------------------------------------------------------------
-- Period reports.

-- Counts and amounts by status over the period's current import.
CREATE VIEW v_period_summary AS
SELECT k.period_id, k.status,
       count(*)::int           AS doc_count,
       sum(k.books_total_tax)  AS books_tax,
       sum(k.portal_total_tax) AS portal_tax
FROM v_result_key k
WHERE k.is_current
GROUP BY k.period_id, k.status;

-- itc_books   = books tax of results with a books side
-- itc_2b      = portal tax of non-DUPLICATE results with a portal side
-- itc_at_risk = books tax of ONLY_IN_BOOKS + positive diff_tax of MISMATCH / PERIOD_MISMATCH
-- match_rate  = EXACT|ROUNDING|PROBABLE books-side results / all books-side results (%)
CREATE VIEW v_period_totals AS
SELECT p.period_id,
       coalesce(sum(k.books_total_tax), 0) AS itc_books,
       coalesce(sum(k.portal_total_tax) FILTER (WHERE k.status <> 'DUPLICATE'), 0) AS itc_2b,
       coalesce(sum(k.books_total_tax) FILTER (WHERE k.status = 'ONLY_IN_BOOKS'), 0)
         + coalesce(sum(greatest(k.diff_tax, 0))
                    FILTER (WHERE k.status IN ('MISMATCH', 'PERIOD_MISMATCH')), 0)
                                           AS itc_at_risk,
       round(100.0 * count(*) FILTER (WHERE k.books_total_tax IS NOT NULL
                                        AND k.status IN ('EXACT', 'ROUNDING', 'PROBABLE'))
             / nullif(count(*) FILTER (WHERE k.books_total_tax IS NOT NULL), 0), 2)
                                           AS match_rate_pct
FROM return_period p
LEFT JOIN v_result_key k ON k.period_id = p.period_id AND k.is_current
GROUP BY p.period_id;

-- Reviews attached to a period's current results (DUPLICATE rows never attach).
CREATE VIEW v_period_review AS
SELECT k.period_id, k.result_id, k.status, k.books_total_tax, k.party_gstin AS k_party,
       k.invoice_no AS k_invoice_no, rv.*
FROM v_result_key k
JOIN review rv ON rv.gstin = k.gstin AND rv.party_gstin = k.party_gstin
              AND rv.doc_type = k.doc_type AND rv.invoice_no_norm = k.invoice_no_norm
              AND rv.fin_year = k.fin_year
WHERE k.is_current AND k.status <> 'DUPLICATE';

-- The four numbers the client approves.
--   claimable_now = books tax of unreviewed EXACT/ROUNDING + ACCEPT + PARTIAL x claim_pct
--   deferred      = tax_at_risk of DEFER/CHASE reviews in the period's queue (incl. carried)
--   lost          = tax_at_risk of INELIGIBLE reviews attached in the period
--   hold_payment_parties = parties with a CHASE review, or an unreviewed ONLY_IN_BOOKS row
CREATE VIEW v_client_summary AS
WITH cur AS (
    SELECT k.period_id, k.status, k.party_gstin, k.books_total_tax,
           rv.review_id, rv.decision, rv.claim_pct, rv.tax_at_risk
    FROM v_result_key k
    LEFT JOIN review rv ON rv.gstin = k.gstin AND rv.party_gstin = k.party_gstin
                       AND rv.doc_type = k.doc_type AND rv.invoice_no_norm = k.invoice_no_norm
                       AND rv.fin_year = k.fin_year
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

-- IMS actions to take on the portal (CSV export).
CREATE VIEW v_ims_plan AS
SELECT period_id, party_gstin, invoice_no, ims_action, remark
FROM v_period_review
WHERE ims_action <> 'NONE';

-- Derived ITC ledger per period (replaces a stored ledger table).
-- blocked = books tax of the period's invoices excluded as BLOCKED_17_5. invoice_id comes
-- from the parent's single identity, so it is unique across fin_year partitions.
CREATE VIEW v_itc_ledger AS
SELECT p.gstin, p.period,
       t.itc_2b          AS available,
       s.claimable_now   AS claimed,
       s.deferred        AS deferred,
       coalesce(b.blocked, 0) AS blocked
FROM return_period p
JOIN v_period_totals t  ON t.period_id = p.period_id
JOIN v_client_summary s ON s.period_id = p.period_id
LEFT JOIN (
    SELECT x.period_id, sum(it.total_tax) AS blocked
    FROM v_excluded_itc x
    JOIN v_invoice_total it ON it.invoice_id = x.invoice_id
    WHERE x.reason = 'BLOCKED_17_5'
    GROUP BY x.period_id
) b ON b.period_id = p.period_id;

-- ---------------------------------------------------------------------------------------
-- Client board: one row per client registration, showing its latest period.

-- Assigned staff of a client (app_user RLS would otherwise hide colleagues from staff).
CREATE FUNCTION assignee_names(p_client_id bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT string_agg(u.email, ', ' ORDER BY u.email)
    FROM staff_assignment a JOIN app_user u ON u.user_id = a.user_id
    WHERE a.client_id = p_client_id AND u.role = 'staff' AND can_see_client(p_client_id)
$$;
COMMENT ON FUNCTION assignee_names(bigint) IS
    'app_user has no name column; assigned staff are listed by email';

CREATE VIEW v_client_board AS
SELECT c.client_id, c.name AS client_name, g.gstin, lp.period, lp.state,
       t.itc_at_risk, li.last_import_at, assignee_names(c.client_id) AS assignee_names
FROM client c
LEFT JOIN gstin_reg g ON g.client_id = c.client_id
LEFT JOIN LATERAL (
    SELECT p.period_id, p.period, p.state FROM return_period p
    WHERE p.gstin = g.gstin
    ORDER BY period_start(p.period) DESC LIMIT 1
) lp ON true
LEFT JOIN v_period_totals t ON t.period_id = lp.period_id
LEFT JOIN LATERAL (
    SELECT max(i.imported_at) AS last_import_at FROM gstr2b_import i
    WHERE i.period_id = lp.period_id
) li ON true;

-- ---------------------------------------------------------------------------------------
-- Rule 37: unpaid inward invoices by age, only for clients that record payments.
-- unpaid = invoice value (taxable + tax) - payments. For '>180' rows the tax portion of the
-- unpaid amount is the proposed reversal, with interest at client.r37_interest_pct for the
-- days beyond 180 (verify on gst.gov.in).
CREATE VIEW v_rule37 AS
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
      AND u.unpaid > 0
      AND EXISTS (SELECT 1 FROM payment pm
                  JOIN invoice pi ON pi.invoice_id = pm.invoice_id
                                 AND pi.fin_year = pm.fin_year
                  JOIN gstin_reg pg ON pg.gstin = pi.gstin
                  WHERE pg.client_id = g.client_id)
) x;

-- ---------------------------------------------------------------------------------------
-- Party score (materialized). One row per (party, client) over the non-DUPLICATE results
-- of every current import. gstr2b_entry carries no supplier filing date, so
-- avg_filing_delay_days is 0 until such a field exists.
CREATE MATERIALIZED VIEW mv_party_score AS
SELECT b.party_gstin, b.client_id, b.docs, b.pct_only_in_books, b.pct_mismatch,
       b.avg_filing_delay_days,
       round(100 - (50 * b.pct_only_in_books + 30 * b.pct_mismatch
                    + 20 * least(b.avg_filing_delay_days, 30) / 30), 2) AS score
FROM (
    SELECT k.party_gstin, g.client_id,
           count(*)::int AS docs,
           round(count(*) FILTER (WHERE k.status = 'ONLY_IN_BOOKS')::numeric / count(*), 4)
               AS pct_only_in_books,
           round(count(*) FILTER (WHERE k.status = 'MISMATCH')::numeric / count(*), 4)
               AS pct_mismatch,
           0::numeric AS avg_filing_delay_days
    FROM v_result_key k
    JOIN gstin_reg g ON g.gstin = k.gstin
    WHERE k.is_current AND k.status <> 'DUPLICATE'
    GROUP BY k.party_gstin, g.client_id
) b;
CREATE UNIQUE INDEX mv_party_score_uq ON mv_party_score (party_gstin, client_id);
COMMENT ON MATERIALIZED VIEW mv_party_score IS
    'score = 100 - (50*pct_only_in_books + 30*pct_mismatch + 20*least(avg_filing_delay_days,30)/30); '
    'pct values are fractions 0..1 over non-DUPLICATE results of current imports; '
    'avg_filing_delay_days is 0 (no supplier filing date in gstr2b_entry). '
    'Read through v_party_score; refresh with sp_refresh_party_score().';

-- Materialized views cannot carry RLS, so the MV gets no grants and is read through this
-- filtering view. It is deliberately NOT security_invoker: an invoker view would need
-- SELECT on the MV itself (which would expose every client's rows). The owner reads the
-- MV and can_see_client(), which looks at the caller's SET ROLE and app.user_id, filters.
-- security_barrier: can_see_client is not leakproof, so without the barrier the planner
-- may evaluate a caller's cheaper predicate (e.g. a pg_temp function) on every MV row
-- before the tenant filter, leaking other clients' rows.
CREATE VIEW v_party_score WITH (security_barrier = true) AS
SELECT * FROM mv_party_score WHERE can_see_client(client_id);

CREATE FUNCTION sp_refresh_party_score() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    REFRESH MATERIALIZED VIEW mv_party_score;
END;
$$;

-- ---------------------------------------------------------------------------------------
-- Compliance calendar.
CREATE TABLE filing (
    filing_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gstin       gstin NOT NULL REFERENCES gstin_reg(gstin),
    return_type text NOT NULL,
    period      char(6) NOT NULL CHECK (period ~ '^(0[1-9]|1[0-2])[0-9]{4}$'),
    due_date    date NOT NULL,
    filed_on    date,
    status      text NOT NULL DEFAULT 'DUE' CHECK (status IN ('DUE', 'FILED', 'LATE')),
    late_fee    numeric(14,2) NOT NULL DEFAULT 0,
    UNIQUE (gstin, return_type, period)
);
COMMENT ON TABLE filing IS 'Due dates derived from filing_rule; verify on gst.gov.in';

-- Inserts the FILING rows for p_gstin's taxpayer_type and filing_freq over the months of
-- [p_from, p_to] (month granularity). Due date = due_day of the month AFTER:
--   MONTHLY   the tax period month          (period = that month)
--   QUARTERLY the quarter's last month      (Jun/Sep/Dec/Mar; period = that month)
--   ANNUAL    March, the FY's last month    (period = '03YYYY'; due in April)
-- due_day is clamped to the month's length. Returns the number of rows inserted.
CREATE FUNCTION sp_generate_filings(p_gstin text, p_from date, p_to date) RETURNS int
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
    v_n int;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM gstin_reg WHERE gstin = p_gstin) THEN
        RAISE EXCEPTION 'unknown GSTIN %', p_gstin;
    END IF;
    IF p_from IS NULL OR p_to IS NULL OR p_to < p_from THEN
        RAISE EXCEPTION 'invalid range % .. %', p_from, p_to;
    END IF;

    INSERT INTO filing(gstin, return_type, period, due_date)
    SELECT g.gstin, fr.return_type, to_char(m.mon, 'MMYYYY'),
           make_date(extract(year FROM nx.due_month)::int,
                     extract(month FROM nx.due_month)::int,
                     least(fr.due_day,
                           extract(day FROM nx.due_month + interval '1 month'
                                            - interval '1 day')::int))
    FROM gstin_reg g
    JOIN filing_rule fr ON fr.taxpayer_type = g.taxpayer_type
                       AND fr.filing_freq = g.filing_freq
    CROSS JOIN LATERAL generate_series(date_trunc('month', p_from),
                                       date_trunc('month', p_to),
                                       interval '1 month') AS m(mon)
    CROSS JOIN LATERAL (SELECT (m.mon + interval '1 month')::date AS due_month) nx
    WHERE g.gstin = p_gstin
      AND (fr.frequency = 'MONTHLY'
           OR (fr.frequency = 'QUARTERLY' AND extract(month FROM m.mon) IN (3, 6, 9, 12))
           OR (fr.frequency = 'ANNUAL' AND extract(month FROM m.mon) = 3))
    ON CONFLICT (gstin, return_type, period) DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END;
$$;

-- Alerts: unfiled returns due within 7 days or overdue, and open reviews whose s.16(4)
-- date is within 30 days.
CREATE VIEW v_alerts AS
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
  AND s16_4_date(rv.fin_year) BETWEEN app_today() AND app_today() + 30;

-- ---------------------------------------------------------------------------------------
-- Security: RLS on filing (Task 10 pattern), invoker views, grants.
ALTER TABLE filing ENABLE ROW LEVEL SECURITY;
ALTER TABLE filing FORCE ROW LEVEL SECURITY;
CREATE POLICY filing_tenant ON filing
    USING (can_see_client(client_of_gstin(gstin)));

ALTER VIEW v_period_summary SET (security_invoker = true);
ALTER VIEW v_period_totals  SET (security_invoker = true);
ALTER VIEW v_period_review  SET (security_invoker = true);
ALTER VIEW v_client_summary SET (security_invoker = true);
ALTER VIEW v_ims_plan       SET (security_invoker = true);
ALTER VIEW v_itc_ledger     SET (security_invoker = true);
ALTER VIEW v_client_board   SET (security_invoker = true);
ALTER VIEW v_rule37         SET (security_invoker = true);
ALTER VIEW v_alerts         SET (security_invoker = true);

REVOKE ALL ON mv_party_score FROM PUBLIC, rc_partner, rc_staff, rc_client;

GRANT SELECT ON v_period_summary, v_period_totals, v_period_review, v_client_summary,
                v_ims_plan, v_itc_ledger, v_client_board, v_rule37, v_party_score, v_alerts,
                filing
    TO rc_partner, rc_staff, rc_client;
GRANT INSERT, UPDATE ON filing TO rc_staff, rc_partner;
GRANT USAGE ON SEQUENCE filing_filing_id_seq TO rc_staff, rc_partner;

REVOKE EXECUTE ON FUNCTION assignee_names(bigint), sp_refresh_party_score(),
                           sp_generate_filings(text, date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION assignee_names(bigint) TO rc_partner, rc_staff, rc_client;
GRANT EXECUTE ON FUNCTION sp_refresh_party_score(),
                          sp_generate_filings(text, date, date) TO rc_staff, rc_partner;
