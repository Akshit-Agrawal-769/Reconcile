"""Every SQL statement the API runs, by name. Values are always bound parameters."""

LOGIN = "SELECT user_id, role::text, client_id, firm_id FROM fn_login(%s, %s)"

# --- uploads / pipeline ---------------------------------------------------------------
PERIOD_VISIBLE = "SELECT period_id, gstin FROM return_period WHERE period_id = %s"
INSERT_UPLOAD = ("INSERT INTO upload(period_id, kind, filename, content, sha256, uploaded_by) "
                 "VALUES (%s, %s, %s, %s, %s, app_user_id()) RETURNING upload_id")
ENQUEUE = "SELECT sp_enqueue_job(%s, %s, %s, %s, %s::jsonb)"
EXISTING_UPLOAD = ("SELECT u.upload_id, (SELECT j.status FROM job_queue j WHERE j.upload_id = u.upload_id "
                   "ORDER BY j.job_id DESC LIMIT 1) FROM upload u "
                   "WHERE u.period_id = %s AND u.kind = %s AND u.sha256 = %s")
# Lists travel as JSON text so they bind the same way under any driver.
FIRST_BAD_GSTIN = ("SELECT t.ord, t.g FROM jsonb_array_elements_text(%s::jsonb) WITH ORDINALITY "
                   "AS t(g, ord) WHERE NOT gstin_checksum_ok(t.g) ORDER BY t.ord LIMIT 1")
ALREADY_IMPORTED = (
    "SELECT DISTINCT t.i FROM jsonb_to_recordset(%s::jsonb) AS t(i int, sup text, dt text, no text, d text) "
    "JOIN invoice inv ON inv.gstin = %s AND inv.party_gstin = t.sup AND inv.direction = 'INWARD' "
    "AND inv.doc_type = t.dt::doc_type AND inv.invoice_no_norm = norm_doc_no(t.no) "
    "AND inv.fin_year = fin_year_of(to_date(t.d, 'DD-MM-YYYY'))")
CLEAR_STAGING = "DELETE FROM books_staging WHERE period_id = %s"
INSERT_STAGING_PREFIX = (
    "INSERT INTO books_staging(period_id, gstin_of_supplier, supplier_name, document_type, "
    "invoice_number, invoice_date, place_of_supply, reverse_charge, hsn_sac, rate, "
    "taxable_value, integrated_tax_paid, central_tax_paid, state_ut_tax_paid, cess_paid, "
    "eligible_for_itc) VALUES ")

# --- reads ----------------------------------------------------------------------------
CLIENT_BOARD = ("SELECT client_id, client_name, gstin, period, state::text AS state, itc_at_risk, "
                "last_import_at, assignee_names FROM v_client_board ORDER BY client_name, gstin")
CLIENT_VISIBLE = "SELECT client_id FROM client WHERE client_id = %s"
CLIENT_PERIODS = ("SELECT period_id, gstin, period, state::text AS state, fin_year "
                  "FROM v_client_periods WHERE client_id = %s "
                  "ORDER BY to_date(period, 'MMYYYY') DESC, gstin")
PERIOD = ("SELECT p.period_id, p.gstin, p.period, p.state::text AS state, p.fin_year, "
          "p.client_approved_at IS NOT NULL AS client_approved, p.client_approved_at, "
          "p.override_reason, p.closed_at, cp.client_id, cp.client_name "
          "FROM return_period p JOIN v_client_periods cp USING (period_id) "
          "WHERE p.period_id = %s")
PERIOD_TOTALS = ("SELECT itc_books, itc_2b, itc_at_risk, match_rate_pct "
                 "FROM v_period_totals WHERE period_id = %s")
PERIOD_SUMMARY = ("SELECT claimable_now, deferred, lost, hold_payment_parties "
                  "FROM v_client_summary WHERE period_id = %s")
QUEUE = ("SELECT result_id, import_id, status::text AS status, matched_by, party_gstin, "
         "invoice_no, diff_tax, tax_at_risk, review_id, decision::text AS decision, "
         "state::text AS review_state, changed_since_review, carried_from_period, priority "
         "FROM v_queue WHERE period_id = %(pid)s "
         "AND (%(status)s::text IS NULL OR status::text = %(status)s::text) "
         "AND (%(q)s::text IS NULL OR strpos(lower(coalesce(invoice_no, '') || ' ' || "
         "coalesce(party_gstin, '')), lower(%(q)s::text)) > 0) "
         "ORDER BY priority, tax_at_risk DESC NULLS LAST, invoice_no, result_id")
RESULT_DETAIL = ("SELECT result_id, import_id, period_id, status::text AS status, matched_by, "
                 "diff_tax, books_invoice_no, books_date, books_taxable, books_tax, "
                 "portal_invoice_no, portal_date, portal_taxable, portal_tax, party_gstin, "
                 "party_name FROM v_result_detail WHERE result_id = %s")
RESULT_REVIEW = ("SELECT r.review_id, r.decision::text AS decision, r.ims_action::text AS ims_action, "
                 "r.claim_pct, r.remark, r.state::text AS state, r.proposed_by, r.approved_by, "
                 "r.executed_on, q.changed_since_review "
                 "FROM v_queue q JOIN review r ON r.review_id = q.review_id "
                 "WHERE q.result_id = %s")
JOB_COLS = ("job_id, job_type, status, attempts, last_error, result, created_at, finished_at")
JOB = f"SELECT {JOB_COLS} FROM job_queue WHERE job_id = %s"
PERIOD_JOBS = f"SELECT {JOB_COLS} FROM job_queue WHERE period_id = %s ORDER BY job_id DESC LIMIT 50"
IMS_PLAN = ("SELECT party_gstin, invoice_no, ims_action::text, remark FROM v_ims_plan "
            "WHERE period_id = %s ORDER BY party_gstin, invoice_no")
RULE37 = ("SELECT client_id, invoice_id, party_gstin, invoice_no, invoice_date, unpaid, "
          "days_outstanding, bucket, interest_estimate, proposed_reversal FROM v_rule37 "
          "WHERE client_id = %s ORDER BY days_outstanding DESC, invoice_no")
PARTY_SCORES = ("SELECT party_gstin, client_id, docs, pct_only_in_books, pct_mismatch, "
                "avg_filing_delay_days, score FROM v_party_score WHERE client_id = %s "
                "ORDER BY score, party_gstin")
FILINGS = ("SELECT f.filing_id, f.gstin, f.return_type, f.period, f.due_date, f.filed_on, "
           "f.status::text AS status, f.late_fee FROM filing f "
           "JOIN gstin_reg g ON g.gstin = f.gstin WHERE g.client_id = %s "
           "ORDER BY f.due_date, f.return_type")
ALERTS = ("SELECT client_id, gstin, kind, ref, due_by FROM v_alerts WHERE client_id = %s "
          "ORDER BY due_by NULLS LAST, kind")

# --- writes ---------------------------------------------------------------------------
OPEN_PERIOD = "SELECT sp_open_period(%s, %s)"
# ::numeric — psycopg 3 sends a Python float as double precision, which has no
# implicit cast to sp_propose's numeric parameter.
PROPOSE = "SELECT sp_propose(%s, %s::review_decision, %s::ims_action, %s::numeric, %s)"
APPROVE = "SELECT sp_approve(%s)"
MARK_DONE = "SELECT sp_mark_done(%s, %s)"
SUBMIT = "SELECT sp_submit_period(%s)"
CLIENT_APPROVE = "SELECT sp_client_approve(%s)"
OVERRIDE = "SELECT sp_partner_override(%s, %s)"
CLOSE = "SELECT sp_close_period(%s)"
REOPEN = "SELECT sp_reopen_period(%s, %s)"
CLIENT_GSTINS = "SELECT gstin FROM gstin_reg WHERE client_id = %s ORDER BY gstin"
GENERATE_FILINGS = "SELECT sp_generate_filings(%s, %s, %s)"
FOLLOWUPS = ("SELECT followup_id, gstin, party_gstin, period_id, channel, sent_by, sent_on, "
             "response, next_chase_on, status FROM followup WHERE period_id = %s "
             "ORDER BY followup_id DESC")
INSERT_FOLLOWUP = ("INSERT INTO followup(gstin, party_gstin, period_id, channel, sent_on, "
                   "response, next_chase_on, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                   "RETURNING followup_id")
