# Reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Reconcile, a multi-client web app for a CA firm that reconciles each client's purchase register against GSTR-2B. Every business rule is enforced in PostgreSQL.

**Architecture:** Numbered SQL migrations define the schema, domains, procedures, triggers, views, roles and RLS. A thin FastAPI layer (psycopg 3, raw SQL, no ORM) sets the DB role and user per request and calls procedures and views. A React (Vite, TypeScript) frontend renders the workflow. pytest runs behaviour tests against a real PostgreSQL 16 instance.

**Tech Stack:** PostgreSQL 16 (`fuzzystrmatch`, `pg_trgm`, `pgcrypto`), Python 3.12, FastAPI, psycopg[binary] 3, pytest, React 18 + Vite + TypeScript, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-27-reconcile-design.md`

## Global Constraints

- PostgreSQL **16** minimum. No PG17/18-only syntax (`WITHOUT OVERLAPS`, `uuidv7()`, `RETURNING OLD/NEW`).
- **No ORM.** All SQL is in `db/migrations/*.sql` or in named string constants in `backend/app/queries.py`.
- Migrations are append-only. Never edit a merged migration; add a new one.
- Enum codes are exactly as in spec §7. UI copy uses "Only in books" and "Only in 2B", never "Missing in…".
- Tolerance defaults: `rounding_tol` = 1.00, `review_tol` = 10.00, `date_window_days` = 10, `fuzzy_max_dist` = 2, `lookback_months` = 12, `r37_interest_pct` = 18.00.
- GSTR-2B sections supported: `B2B`, `B2BA`, `CDNR` only. Dates in 2B JSON are `DD-MM-YYYY` text. `rtnprd` is `MMYYYY`.
- Money is `numeric(14,2)`. Never use float.
- Every legal constant that the spec calls a setting is stored in `recon_settings`. Each carries a comment saying "verify on gst.gov.in".
- Reconcile never calls GSTN or GSP APIs.
- Commit messages follow `feat|fix|test|docs|chore(scope): message`.

## Review Focus

1. **Re-running reconciliation after staff decisions.** Decided rows and `MANUAL` links must survive a re-run unchanged. Test: `test_rerun_preserves_decisions_and_manual` (Task 7).
2. **Credit notes carry negative values on both sides.** A CRN must match a CRN, never an INV with the same number. Test: `test_credit_note_matches_only_credit_note` (Task 6).
3. **The same invoice number from two different suppliers** must never be paired across GSTINs, except by the PAN pass when the PAN matches. Test: `test_same_number_different_supplier_not_matched` (Task 6).
4. **A 2B JSON with a GSTIN that fails the checksum** (special registrations, sample data) imports with `gstin_valid = false`; it is not rejected. Test: `test_import_accepts_invalid_checksum_supplier` (Task 5).
5. **An empty period** (no 2B, or no books) still produces a run and a summary of all `ONLY_IN_…` rows with zero errors. Test: `test_reconcile_with_empty_side` (Task 6).

---

## File Structure

```
docker-compose.yml                 Postgres 16 for dev and test
.env.example                       DATABASE_URL, TEST_DATABASE_URL, API_SECRET
db/migrate.py                      applies db/migrations/*.sql in order; schema_migrations table
db/migrations/
  001_extensions_roles.sql         extensions, group roles, rc_api
  002_reference_gstin.sql          state_code, gstin_checksum_ok(), DOMAIN gstin
  003_tenancy.sql                  enums, app_user, client, client_gstin, assignments, settings, return_period, supplier
  004_documents.sql                norm_doc_no(), pr_import_line, pr_document, payment, gstr2b_raw, g2b_document
  005_imports.sql                  sp_import_gstr2b(), sp_import_purchase_register()
  006_matching_core.sql            match_rule, recon_run, recon_match, match_difference, sp_reconcile() passes 0,1,2,7
  007_matching_fuzzy.sql           passes 3,4,5,6 (CREATE OR REPLACE sp_reconcile)
  008_workflow.sql                 decision constraints, sp_manual_link/sp_delink/sp_decide, audit + freeze + lock triggers, sp_close_period
  009_security.sql                 GRANTs, column grants, RLS policies, helper period_client()
  010_views.sql                    v_match_detail, v_period_summary, v_itc_at_risk, v_rule37_ageing, mv_vendor_scorecard, v_deadline_queue, v_ims_action_plan, v_naive_match, status_label_map, vendor_followup
  011_accuracy.sql                 synth_truth, v_accuracy
db/seed/demo_users.sql             partner, 2 staff, 1 client_viewer, 3 clients, assignments
db/tests/conftest.py               fresh DB per test session + per-test transaction rollback; helpers
db/tests/test_*.py                 one file per migration area
tools/synth.py                     synthetic books CSV + 2B JSON + truth CSV generator
tools/explain_demo.sql             naive vs multi-pass + EXPLAIN ANALYZE script
backend/app/main.py                FastAPI app, routers
backend/app/db.py                  connection pool; with_role(user) context manager
backend/app/auth.py                login, signed session token
backend/app/queries.py             all SQL strings
backend/app/routes/*.py            clients, periods, imports, matches, reports
backend/tests/test_api.py          API tests (httpx TestClient)
frontend/                          Vite React app (pages per spec §12)
docs/er-diagram.md                 Mermaid ER diagram
docs/normalisation.md              FDs + 3NF argument per table
docs/viva-demo.md                  scripted demo (spec §14) + viva answers
README.md                          setup, run, deploy
```

---

### Task 1: Dev environment and migration runner

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `db/migrate.py`, `db/migrations/001_extensions_roles.sql`, `db/tests/conftest.py`, `db/tests/test_migrate.py`, `requirements-dev.txt`

**Interfaces:**
- Produces: `python db/migrate.py --url <DATABASE_URL>` applies pending files and prints `applied 00X_name.sql`. Table `schema_migrations(filename text PK, applied_at timestamptz)`.
- Produces (pytest fixtures): `db` gives a psycopg connection inside a transaction that is rolled back after each test, with autocommit off. `as_user(db, user_id, role)` is a helper that runs `SET LOCAL ROLE rc_<role>` and `set_config('app.user_id', ..., true)`. The session-scoped fixture drops and recreates database `reconcile_test`, then runs all migrations once.

- [ ] **Step 1: Write the failing tests** in `db/tests/test_migrate.py`:
  - `test_migrations_recorded`: `SELECT count(*) FROM schema_migrations` equals the number of files in `db/migrations`.
  - `test_extensions_present`: `pg_extension` contains `fuzzystrmatch`, `pg_trgm` and `pgcrypto`.
  - `test_group_roles_exist`: `pg_roles` contains `rc_partner`, `rc_staff`, `rc_client` and `rc_api`.
- [ ] **Step 2: Run** `docker compose up -d db && pytest db/tests/test_migrate.py -v`. Expected: FAIL, because migrate.py doesn't exist yet.
- [ ] **Step 3: Implement.**
  - `docker-compose.yml`: service `db` with `postgres:16`, port 5432, user and password `reconcile`.
  - `migrate.py`: sorted glob, one transaction per file, skips files already recorded.
  - `001`: `CREATE EXTENSION IF NOT EXISTS` for the three extensions. Create the group roles with `NOLOGIN`, guarded by `DO $$ … IF NOT EXISTS`, because roles are cluster-wide. Create `rc_api` with `LOGIN NOINHERIT` and grant it all three group roles.
- [ ] **Step 4: Run tests.** Expected: PASS. Running `python db/migrate.py` a second time prints nothing.
- [ ] **Step 5: Commit** `chore(db): docker postgres, migration runner, roles`

### Task 2: GSTIN domain and state codes

**Files:**
- Create: `db/migrations/002_reference_gstin.sql`, `db/tests/test_gstin.py`

**Interfaces:**
- Produces: `gstin_checksum_ok(text) RETURNS boolean IMMUTABLE STRICT`, DOMAIN `gstin AS char(15)`, table `state_code(code char(2) PRIMARY KEY, name text NOT NULL)` seeded with codes 01–38, 97 and 99.

- [ ] **Step 1: Write the failing tests:**
  - `test_checksum_valid`: `gstin_checksum_ok('27AAPFU0939F1ZV')` returns true.
  - `test_checksum_invalid`: `gstin_checksum_ok('27AAPFU0939F1ZO')` returns false.
  - `test_checksum_vector_3`: for `'09AAACT2727Q1Z'`, the computed check character is `U`, so `gstin_checksum_ok('09AAACT2727Q1ZU')` returns true.
  - `test_domain_rejects_bad_pattern`: `SELECT 'ABC'::gstin` raises `CheckViolation`.
  - `test_domain_rejects_bad_checksum`: `SELECT '27AAPFU0939F1ZO'::gstin` raises `CheckViolation`.
  - `test_state_codes_seeded`: `count(*)` equals 40, and code `'07'` is Delhi.
- [ ] **Step 2: Run** `pytest db/tests/test_gstin.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** in `002` using this algorithm:
  ```
  chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'; sum = 0
  for i in 1..14: v = position(char_i in chars)-1; f = (i odd ? 1 : 2)
                  p = v*f; sum += p/36 + p%36
  check = chars[(36 - sum%36) % 36]   -- compare with char 15
  ```
  The domain CHECK combines the regex from spec §6 with `gstin_checksum_ok(VALUE)`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): GSTIN domain with mod-36 checksum and state codes`

### Task 3: Tenancy schema

**Files:**
- Create: `db/migrations/003_tenancy.sql`, `db/seed/demo_users.sql`, `db/tests/test_tenancy.py`, `db/tests/factories.py`

**Interfaces:**
- Produces: enums `user_role('partner','staff','client_viewer')`, `period_status('OPEN','CLOSED')`. Tables as in spec §6 "Reference and tenancy". Function `fiscal_year_of(d date) RETURNS char(7)`; for example, 2026-04-01 returns `'2026-27'` and 2026-03-31 returns `'2025-26'`. Function `rtnprd_to_date(char(6)) RETURNS date`, which gives the first day of the month.
- Produces (`factories.py`): `make_client(db, pan='AAPFU0939F') -> client_id`, `make_gstin(db, client_id, state='27') -> gstin` (returns a checksum-valid GSTIN for the PAN), `make_period(db, gstin, rtnprd='092026') -> period_id`, `make_user(db, role) -> user_id`, `assign(db, user_id, client_id)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_gstin_state_must_exist`: inserting a client_gstin with state `'00'` raises `ForeignKeyViolation` or `CheckViolation`.
  - `test_gstin_pan_must_match_client`: a valid GSTIN whose PAN differs from `client.pan` raises, with the message containing `PAN mismatch`.
  - `test_settings_defaults`: inserting `recon_settings(client_id)` gives `rounding_tol = 1.00`, `review_tol = 10.00` and `date_window_days = 10`.
  - `test_period_unique`: a second `return_period` with the same `(gstin, rtnprd)` raises `UniqueViolation`.
  - `test_fiscal_year_of`: `fiscal_year_of('2026-04-01')` returns `'2026-27'` and `fiscal_year_of('2026-03-31')` returns `'2025-26'`.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement.**
  - `003`: the tenancy tables. `recon_settings` gets a row per client through an `AFTER INSERT ON client` trigger. Add `trg_gstin_pan`.
  - `demo_users.sql`: 1 partner, 2 staff, 1 client_viewer, and 3 clients. Staff A is assigned to clients 1 and 2; staff B to client 3. Passwords are hashed with `crypt('demo123', gen_salt('bf'))`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): tenancy schema, settings, fiscal year helpers`

### Task 4: Document tables and invoice-number normalisation

**Files:**
- Create: `db/migrations/004_documents.sql`, `db/tests/test_documents.py`

**Interfaces:**
- Produces:
  - `norm_doc_no(text) RETURNS text IMMUTABLE`. It uppercases; removes FY tokens matching `(20)?\d{2}[-/](20)?\d{2}` when they appear as a whole segment; strips all non-alphanumerics; and strips leading zeros. If the result is empty it returns `'0'`.
  - Enums `doc_type('INV','CRN','DBN')` and `g2b_section('B2B','B2BA','CDNR')`.
  - Tables `pr_import_line`, `pr_document`, `payment`, `gstr2b_raw` and `g2b_document`, per spec §6. Both document tables have the generated columns `doc_no_norm`, `total_tax = igst+cgst+sgst+cess` and `fiscal_year` (via `fiscal_year_of(doc_date)`; the function is IMMUTABLE).
  - CHECK on both document tables: `doc_type='CRN'` implies `taxable_value <= 0`, and `doc_type<>'CRN'` implies `taxable_value >= 0`.
  - Indexes on both tables:
    - `(period_id, supplier_gstin, doc_type, doc_no_norm)`
    - GIN `doc_no_norm gin_trgm_ops`
    - for g2b only, GIN `supplier_name gin_trgm_ops`.

- [ ] **Step 1: Write the failing tests:**
  - `test_norm_doc_no` parametrised: `'DP-0089'→'DP89'`; `'dp/89'→'DP89'`; `'00089'→'89'`; `'INV/2026-27/0045'→'INV45'`; `'INV/26-27/45'→'INV45'`; `'---'→'0'`.
  - `test_generated_columns`: insert an INV with igst 18.00 and cgst 0 → `total_tax = 18.00` and `fiscal_year = '2026-27'` for an invoice dated 2026-09-10.
  - `test_credit_note_sign_check`: a CRN with `taxable_value = 100` raises `CheckViolation`.
  - `test_payment_positive`: a payment with `amount = 0` raises `CheckViolation`.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `004`.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): document tables, norm_doc_no, indexes`

### Task 5: Import procedures

**Files:**
- Create: `db/migrations/005_imports.sql`, `db/tests/test_imports.py`, `db/tests/fixtures/gstr2b_sample.json`, `db/tests/fixtures/pr_sample.csv`

**Interfaces:**
- Consumes: the tables from Task 4.
- Produces:
  - `sp_import_gstr2b(p_period_id bigint, p_payload jsonb, p_user_id bigint) RETURNS bigint /*raw_id*/`
    - Inserts `gstr2b_raw`, then shreds `data.docdata.b2b[].inv[]`, `b2ba[].inv[]` and `cdnr[].nt[]` with `jsonb_array_elements`.
    - Parses dates with `to_date(dt,'DD-MM-YYYY')`.
    - Sums tax from `items[]` if present, otherwise uses document-level `txval/igst/cgst/sgst/cess`.
    - For CDNR, `ntty='C'` becomes `CRN` with negated amounts, and `'D'` becomes `DBN`.
    - Sets `gstin_valid = gstin_checksum_ok(ctin)` and stores `oinum` into `orig_doc_no` for B2BA.
    - Raises `period rtnprd mismatch` if `data.rtnprd` differs from the period's `rtnprd`.
  - `sp_import_purchase_register(p_period_id bigint, p_user_id bigint) RETURNS int /*documents created*/`
    - Aggregates `pr_import_line` rows for the period by `(supplier_gstin, doc_type, doc_no, doc_date)` into `pr_document`, negating CRN amounts.
    - Upserts `supplier` and deletes the staged lines.
  - The CSV header that the API maps into `pr_import_line`: `GSTIN of Supplier,Supplier Name,Document Type,Invoice Number,Invoice date,Rate,Taxable Value,Integrated Tax Paid,Central Tax Paid,State/UT Tax Paid,Cess Paid,Eligible for ITC`

- [ ] **Step 1: Write the failing tests:**
  - `test_import_b2b_counts`: the fixture has 3 B2B invoices, 1 B2BA and 1 CDNR credit note, so 5 `g2b_document` rows are created, and the CRN row has `taxable_value < 0`.
  - `test_import_dates_parsed`: an invoice with `dt '05-09-2026'` gets `doc_date = 2026-09-05`.
  - `test_import_rtnprd_mismatch`: a payload with `rtnprd '082026'` against period `092026` raises with `rtnprd mismatch`.
  - `test_import_accepts_invalid_checksum_supplier`: a `ctin` of `01AABCE2207R1Z5` imports with `gstin_valid = false`. *(Review Focus 4)*
  - `test_pr_lines_aggregate`: two rate lines for the same invoice (5% and 18%) produce one `pr_document` with summed taxes, and `pr_import_line` is empty afterwards.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `005` and the fixtures.** Build the fixture JSON following the structure in spec §6 and the research notes: `data{gstin,rtnprd,docdata{b2b[{ctin,trdnm,supfildt,inv[{inum,dt,val,pos,rev,itcavl,rsn,items[{txval,igst,cgst,sgst,cess}]}]}],b2ba,cdnr[{ctin,trdnm,nt[{ntnum,ntty,dt,...}]}]}}`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): GSTR-2B JSON and purchase register import procedures`

### Task 6: Matching core (passes 0, 1, 2, 7)

**Files:**
- Create: `db/migrations/006_matching_core.sql`, `db/tests/test_matching_core.py`

**Interfaces:**
- Consumes: `pr_document`, `g2b_document`, `recon_settings`, `norm_doc_no`.
- Produces:
  - Enums `match_status`, `review_decision`, `ims_action`, `itc_reduction('NONE','FULL','PARTIAL')` and `diff_field`, with the exact codes in spec §7 and §6.
  - Tables `match_rule` (seeded with rows 0–7 of spec §8, rules 3–6 `enabled=false` for now), `recon_run`, `recon_match` (with both partial unique indexes) and `match_difference`.
  - `sp_reconcile(p_period_id bigint, p_user_id bigint) RETURNS bigint`, which returns the run_id and writes `recon_run.stats` as `{"EXACT":n,…}`.
  - Helper `period_client(p_period_id bigint) RETURNS bigint STABLE`.

- [ ] **Step 1: Write the failing tests**, building a small dataset with the factories:
  - `test_exact_match`: identical books and 2B invoice → one active match with `EXACT` and no `match_difference` rows.
  - `test_rounding_match`: igst 1800.00 vs 1800.60 → `ROUNDING`, with a `match_difference` row for field `IGST`.
  - `test_beyond_rounding_not_rounding`: igst 1800 vs 1805 → not `ROUNDING`. In this task it becomes `ONLY_IN_BOOKS` plus `ONLY_IN_2B`; Task 7 changes that.
  - `test_residue`: a books-only invoice gives `ONLY_IN_BOOKS` with `g2b_id` NULL, and a 2B-only invoice gives `ONLY_IN_2B`.
  - `test_duplicate_flagged`: two books rows with the same natural key → the second has `is_duplicate=true` and one `DUPLICATE` match row, and only one row is paired.
  - `test_superseded`: a B2BA row whose `orig_doc_no` equals a B2B row → the B2B row gets `is_superseded=true` and a `SUPERSEDED` match, and the books invoice pairs with the B2BA row.
  - `test_one_to_one_enforced`: inserting a second active match with the same `g2b_id` raises `UniqueViolation`.
  - `test_credit_note_matches_only_credit_note`: a books CRN 'CN5' and a 2B INV '5' from the same supplier are not paired. *(Review Focus 2)*
  - `test_same_number_different_supplier_not_matched`: INV '101' from supplier A in books and INV '101' from supplier B in 2B, with different PANs → both end up in residue. *(Review Focus 3)*
  - `test_reconcile_with_empty_side`: a period with only books rows → all rows `ONLY_IN_BOOKS`, and the run's stats equal the count. *(Review Focus 5)*
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `006`.** Each pass is a set-based `INSERT … SELECT … WHERE NOT EXISTS (active match for pr or g2b) ON CONFLICT DO NOTHING`. Differences are inserted in the same statement through a CTE `RETURNING`. Tolerances are read once into variables from `recon_settings` via `period_client`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): reconciliation procedure core passes with 1:1 constraints`

### Task 7: Fuzzy, value, PAN and cross-period passes; re-run safety

**Files:**
- Create: `db/migrations/007_matching_fuzzy.sql` (enables rules 3–6 and `CREATE OR REPLACE` of `sp_reconcile`), `db/tests/test_matching_fuzzy.py`

**Interfaces:**
- Consumes: the Task 6 tables and procedure signature (unchanged).
- Produces: the same procedure, now running all 8 passes, plus the documented re-run semantics.

- [ ] **Step 1: Write the failing tests:**
  - `test_fuzzy_typo`: books 'INV1234' vs 2B 'INV1243' (distance 2), equal amounts, dates 3 days apart → `PROBABLE` with a diff `DOC_NO`.
  - `test_fuzzy_respects_date_window`: the same pair but dates 15 days apart → not `PROBABLE`.
  - `test_fuzzy_mutual_best`: books 'A100' with 2B candidates 'A101' and 'A100X'; plus books 'A101' → 'A101' pairs with 'A101' (exact) and 'A100' pairs with 'A100X'. No row is paired twice.
  - `test_value_mismatch_with_heads`: same key, igst 1800 vs 2000 → `VALUE_MISMATCH` with a diff `IGST` whose books_value is '1800.00' and portal_value is '2000.00'.
  - `test_head_split`: books cgst+sgst 900+900 vs 2B igst 1800 → `VALUE_MISMATCH` with a diff `HEAD_SPLIT`.
  - `test_pan_fallback`: books GSTIN `make_gstin(pan='ABCDE1234F', state='07')` vs 2B GSTIN `make_gstin(pan='ABCDE1234F', state='27')`, same number, same total → `PROBABLE` with a diff `GSTIN`.
  - `test_cross_period`: books invoice in period 08 is unmatched; the 2B row arrives in period 09 (same FY) → when period 09 is reconciled, the result is `PERIOD_MISMATCH` with a diff `PERIOD`.
  - `test_rerun_preserves_decisions_and_manual`: run the reconciliation, set one match to `ACCEPT_BOOKS_VALUE`, manually link two residue rows, then run again → both rows are still active and unchanged, and no duplicate active rows exist. *(Review Focus 1)*
  - `test_rerun_idempotent`: two runs in a row → active match counts per status are identical.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `007`.**
  - **Pass 3:** rank candidates with `row_number() OVER (PARTITION BY pr_id ORDER BY dist, abs(total_tax_diff))` and, separately, the same partitioned by `g2b_id`. Keep pairs where both ranks equal 1.
  - **Re-run:** set `is_active=false` on prior matches for the period, except where `match_status='MANUAL'` or `review_decision<>'PENDING'`.
  - **Pass 6:** look back to `rtnprd_to_date(rtnprd) - lookback_months`, staying within the same fiscal_year.
- [ ] **Step 4: Run tests.** Expected: PASS. Then run the full `pytest db/tests -v` again.
- [ ] **Step 5: Commit** `feat(db): fuzzy, value, PAN, cross-period passes; safe re-run`

### Task 8: Workflow procedures and triggers

**Files:**
- Create: `db/migrations/008_workflow.sql`, `db/tests/test_workflow.py`

**Interfaces:**
- Consumes: `recon_match`, `return_period`.
- Produces:
  - `sp_decide(p_match_id bigint, p_decision review_decision, p_claim_pct numeric, p_ims ims_action, p_itc_reduction itc_reduction, p_remark text) RETURNS void`
  - `sp_manual_link(p_pr_id bigint, p_g2b_id bigint, p_remark text) RETURNS bigint`. It deactivates the two residue rows and inserts an active `MANUAL` match. It raises if either side is already actively matched to something else.
  - `sp_delink(p_match_id bigint, p_remark text) RETURNS void`. It deactivates the match and re-inserts both sides as residue.
  - `sp_close_period(p_period_id bigint) RETURNS void` (partner only) and `sp_reopen_period(p_period_id bigint)`.
  - Table `recon_action_log`. Triggers `trg_match_audit`, `trg_period_freeze` and `trg_decided_lock` (spec §9).
  - CHECK constraints:
    - `review_decision<>'CLAIM_PARTIAL' OR claim_pct BETWEEN 0 AND 100`
    - the credit-note IMS rules from spec §7, enforced in `sp_decide` because they need `doc_type`. The procedure raises `credit note pending allowed for one period only` or `itc reduction declaration required`.

- [ ] **Step 1: Write the failing tests:**
  - `test_decide_writes_audit`: `sp_decide(…,'ACCEPT_BOOKS_VALUE',…)` writes one log row with `column_name='review_decision'`, `old_value='PENDING'`, `new_value='ACCEPT_BOOKS_VALUE'` and `actor_id` equal to the app user.
  - `test_claim_partial_requires_pct`: `CLAIM_PARTIAL` with pct NULL raises `CheckViolation`.
  - `test_cn_pending_one_period`: a CRN match with `pending_since='072026'` in period 09 raises `one period only`.
  - `test_cn_accept_requires_declaration`: a CRN with `ACCEPTED` and NULL reduction raises `declaration required`.
  - `test_manual_link_and_delink`: link two residue rows → one `MANUAL` active row and the residue rows inactive. Delink → the two residue rows are active again.
  - `test_manual_link_rejects_taken`: linking a g2b row that is already `EXACT`-matched raises.
  - `test_freeze_blocks_staff`: close the period, then `sp_decide` as `rc_staff` raises `period closed`. As `rc_partner` it succeeds.
  - `test_decided_lock`: after a decision, changing `g2b_id` as staff raises. As partner it succeeds.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `008`.** The actor comes from `nullif(current_setting('app.user_id', true),'')::bigint`. The role check is `pg_has_role(current_user,'rc_partner','MEMBER')`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(db): decisions, manual link, audit log, period freeze`

### Task 9: DCL — grants and row-level security

**Files:**
- Create: `db/migrations/009_security.sql`, `db/tests/test_security.py`

**Interfaces:**
- Consumes: all tables, and `as_user` from conftest.
- Produces:
  - `current_app_user() RETURNS bigint STABLE` and `can_see_client(p_client_id bigint) RETURNS boolean STABLE SECURITY DEFINER`. The latter returns true for a partner, an assigned staff member, or a linked client user.
  - RLS is enabled and forced on all tenant tables, with policies that use `can_see_client(client_id)` or `can_see_client(period_client(period_id))`.
  - Grants follow spec §11. On `recon_match`, `rc_staff` gets column-level UPDATE on (`review_decision, claim_pct, ims_action, pending_since, itc_reduction_declared, remark`) only. Procedures are `SECURITY INVOKER`, except the ones that must write `recon_action_log`.

- [ ] **Step 1: Write the failing tests:**
  - `test_staff_sees_only_assigned`: as staff A, `SELECT count(DISTINCT client_id) FROM client` returns 2. As staff B it returns 1. As the partner it returns 3.
  - `test_staff_cannot_read_other_client_docs`: as staff B, selecting `pr_document` rows of client 1 returns 0 rows, with no error.
  - `test_client_viewer_read_only`: as client_viewer, `UPDATE recon_match …` raises `InsufficientPrivilege`, and SELECT from `v_period_summary` works (the view comes in Task 10, so mark this `xfail` until then).
  - `test_audit_log_append_only`: as both staff and partner, `UPDATE recon_action_log` and `DELETE FROM recon_action_log` raise `InsufficientPrivilege`.
  - `test_staff_cannot_update_match_status_column`: as staff, `UPDATE recon_match SET match_status='EXACT'` raises `InsufficientPrivilege`.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `009`.**
- [ ] **Step 4: Run** `pytest db/tests -v`. Expected: all PASS except the one marked `xfail`.
- [ ] **Step 5: Commit** `feat(db): roles, column grants, row-level security`

### Task 10: Views, scorecard, follow-ups

**Files:**
- Create: `db/migrations/010_views.sql`, `db/tests/test_views.py`

**Interfaces:**
- Consumes: everything above.
- Produces (columns are the contract the API and UI rely on):
  - `v_match_detail(match_id, period_id, client_id, match_status, review_decision, ims_action, supplier_gstin, supplier_name, books_doc_no, portal_doc_no, books_date, portal_date, books_tax, portal_tax, tax_diff, needs_review_only bool, differences jsonb, remark)`
  - `v_period_summary(period_id, client_id, status_code, doc_count, books_tax, portal_tax)` plus `v_period_totals(period_id, client_id, itc_books, itc_2b, itc_at_risk, match_rate_pct)`
  - `v_itc_at_risk(client_id, period_id, amount)`
  - `v_rule37_ageing(client_id, pr_id, supplier_gstin, doc_no, doc_date, unpaid_amount, days_outstanding, bucket text /*'0-150','150-180','>180'*/, proposed_reversal numeric, interest_estimate numeric)`
  - `mv_vendor_scorecard(client_id, supplier_gstin, docs, pct_only_in_books, pct_value_mismatch, ims_rejections, avg_filing_delay_days, itc_lost, score numeric /*0–100*/)`, where the `pct_*` columns are fractions from 0 to 1, with `score = 100 - (50*pct_only_in_books + 30*pct_value_mismatch + 20*least(avg_filing_delay_days,30)/30)` and the formula in a `COMMENT ON`. Refreshed with `sp_refresh_scorecard()`.
  - `v_deadline_queue(client_id, period_id, open_items, days_to_14th, days_to_19th, days_to_20th)`
  - `v_ims_action_plan(client_id, period_id, supplier_gstin, portal_doc_no, ims_action, remark)`
  - `v_naive_match`: a single FULL OUTER JOIN on `(supplier_gstin, doc_no)` with raw numbers.
  - Tables `vendor_followup` (with RLS) and `status_label_map`, seeded with the rows from the prior-art report's mapping table.
  - Function `fn_followup_letter(p_client_id bigint, p_supplier_gstin text, p_period_id bigint) RETURNS text`, which lists `ONLY_IN_BOOKS` and `VALUE_MISMATCH` invoices.
  - All views use `security_invoker = true`, so RLS applies.

- [ ] **Step 1: Write the failing tests:**
  - `test_period_totals`: with fixtures for 1 EXACT (tax 1800), 1 ONLY_IN_BOOKS (tax 500) and 1 VALUE_MISMATCH (books 1000, portal 800): `itc_books=3300`, `itc_2b=2600`, `itc_at_risk=700`, `match_rate_pct=33.33`.
  - `test_rule37_buckets`: unpaid invoices aged 100, 170 and 200 days (relative to a `now()` override via the setting `app.today`) fall in buckets `'0-150'`, `'150-180'` and `'>180'`. Only the last has `proposed_reversal` greater than 0. A partially paid invoice shows `unpaid_amount = total - paid`.
  - `test_scorecard_formula`: a supplier with 2 of 4 docs only in books, no value mismatches and 0 filing delay → `score = 75.00`.
  - `test_views_respect_rls`: as staff B, `v_match_detail` has no rows for client 1.
  - `test_naive_vs_multipass`: with the 'DP-0089' vs '89' fixture, the naive view shows 2 unmatched rows and multi-pass shows 1 `PROBABLE` or `EXACT`.
  - `test_followup_letter_lists_invoices`: the letter text contains both residue invoice numbers.
  - Remove `xfail` from `test_client_viewer_read_only`.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement `010`.** "Today" is `coalesce(nullif(current_setting('app.today',true),'')::date, current_date)`, so the tests are deterministic.
- [ ] **Step 4: Run** `pytest db/tests -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat(db): reporting views, Rule 37 ageing, vendor scorecard, follow-ups`

### Task 11: Synthetic data and measured accuracy

**Files:**
- Create: `tools/synth.py`, `db/migrations/011_accuracy.sql`, `tools/explain_demo.sql`, `db/tests/test_accuracy.py`

**Interfaces:**
- Produces:
  - `python tools/synth.py --clients 3 --docs 500 --seed 42 --out data/synth/` writes `books_<gstin>_<rtnprd>.csv` (the Task 5 header), `gstr2b_<gstin>_<rtnprd>.json`, and `truth.csv(client_gstin, rtnprd, books_doc_no, portal_doc_no, perturbation, expected_status)`.
  - `generate_gstin(state: str, pan: str) -> str` in `synth.py` (checksum-valid; a Python port of Task 2).
  - Table `synth_truth`, mirroring `truth.csv`.
  - `v_accuracy(expected_status, predicted_status, n, precision_pct, recall_pct)`.
- Perturbation mix (fractions of documents):
  - EXACT 0.50
  - FORMAT (slash, hyphen, leading zeros) 0.12
  - TYPO (one edit) 0.06
  - ROUNDING (±₹0.10–0.90) 0.08
  - DATE_SHIFT (1–5 days) 0.04
  - VALUE (tax off by ₹50–500) 0.05
  - PAN_GSTIN 0.02
  - BOOKS_ONLY 0.05
  - 2B_ONLY 0.04
  - B2BA 0.02
  - DUPLICATE 0.02
  - Each document is also a credit note with a 10% chance.

- [ ] **Step 1: Write the failing tests:**
  - `test_generate_gstin_valid`: 100 generated GSTINs all satisfy `gstin_checksum_ok`.
  - `test_synth_mix`: with a seed of 42 and 500 docs, each perturbation count is within ±3 of the expected value.
  - `test_accuracy_targets`: load the generated set through the import procedures, run `sp_reconcile` for each period, and read `v_accuracy` → `precision_pct ≥ 95` for each of `EXACT`, `ROUNDING` and `PROBABLE`.
- [ ] **Step 2: Run** and confirm the tests FAIL.
- [ ] **Step 3: Implement.** `explain_demo.sql` runs `EXPLAIN ANALYZE` on the pass-1 join with and without the composite index (`DROP INDEX … ; … ; ROLLBACK`).
- [ ] **Step 4: Run tests.** Expected: PASS. Record the actual precision and recall figures in `docs/viva-demo.md`.
- [ ] **Step 5: Commit** `feat(tools): synthetic data generator and accuracy measurement`

### Task 12: Backend API

**Files:**
- Create: `backend/app/{main.py,db.py,auth.py,queries.py}`, `backend/app/routes/{clients.py,periods.py,imports.py,matches.py,reports.py}`, `backend/tests/test_api.py`, `backend/requirements.txt`

**Interfaces:**
- Consumes: every procedure and view above.
- Produces:
  - `db.with_role(user: CurrentUser) -> ContextManager[psycopg.Connection]`. It opens a transaction, runs `SET LOCAL ROLE rc_{user.role_db}`, and sets `set_config('app.user_id', str(user.id), true)`. The `role_db` mapping is partner→`partner`, staff→`staff`, client_viewer→`client`.
  - `POST /auth/login {email,password} → {token, user:{id,name,role}}`. Verified with `crypt(password, password_hash) = password_hash`. The token is HMAC-signed with `API_SECRET` and expires after 8 hours.
  - Endpoints (JSON; errors are `{detail}` with 4xx; a DB `InsufficientPrivilege` maps to 403, and a `RaiseException` maps to 409 with the DB message):
    - `GET /clients`
    - `GET /clients/{id}/periods`
    - `POST /periods {gstin, rtnprd}`
    - `POST /periods/{id}/import/gstr2b` (multipart JSON file)
    - `POST /periods/{id}/import/books` (multipart CSV; header mapping per Task 5)
    - `POST /periods/{id}/reconcile → {run_id, stats}`
    - `GET /periods/{id}/summary → {totals, by_status[]}`
    - `GET /periods/{id}/matches?status=&q=&page=`
    - `GET /matches/{id} → {detail, history[]}`
    - `POST /matches/{id}/decision`
    - `POST /matches/link {pr_id,g2b_id,remark}`
    - `POST /matches/{id}/delink`
    - `POST /periods/{id}/close`
    - `GET /periods/{id}/ims-plan.csv`
    - `GET /clients/{id}/scorecard`
    - `GET /clients/{id}/rule37`
    - `GET /clients/{id}/followups/{gstin}/letter?period_id=`
    - `GET /debug/last-sql` (returns the last SQL text and `EXPLAIN` for the "under the hood" panel; only when `DEBUG_SQL=1`)

- [ ] **Step 1: Write the failing tests** (TestClient against the test DB with the demo seed):
  - `test_login_ok_and_bad`
  - `test_staff_client_list_filtered`: staff A sees 2 clients.
  - `test_end_to_end_reconcile`: import the fixtures, reconcile, and check the summary counts equal the DB counts.
  - `test_closed_period_returns_409_for_staff`
  - `test_client_viewer_decision_403`
  - `test_ims_plan_csv_header`: the first line is `supplier_gstin,portal_doc_no,ims_action,remark`.
- [ ] **Step 2: Run** `pytest backend/tests -v` and confirm the tests FAIL.
- [ ] **Step 3: Implement.** Route handlers stay thin: the SQL constant from `queries.py`, then execute, then return.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): FastAPI layer with per-request DB role and RLS`

### Task 13: Frontend

**Files:**
- Create: `frontend/` (Vite React TS), `frontend/src/api.ts`, `frontend/src/pages/{Login,Clients,PeriodDashboard,Imports,Scorecard,Rule37}.tsx`, `frontend/src/components/{MatchGrid,MatchDrawer,StatusChip,UnderTheHood}.tsx`, `frontend/src/__tests__/*.test.tsx`

**Interfaces:**
- Consumes: the Task 12 endpoints.
- Produces:
  - Status chip colours: success for `EXACT`/`ROUNDING`, accent for `PROBABLE`/`MANUAL`, warning for `VALUE_MISMATCH`/`PERIOD_MISMATCH`/`ONLY_IN_2B`, danger for `ONLY_IN_BOOKS`/`DUPLICATE`, neutral for `SUPERSEDED`.
  - Labels: "Exact", "Rounding", "Probable", "Value mismatch", "Period mismatch", "Only in books", "Only in 2B", "Duplicate", "Manual", "Superseded".

- [ ] **Step 1: Write the failing tests** (Vitest + Testing Library, with the API mocked):
  - `StatusChip` renders "Only in books" for `ONLY_IN_BOOKS`.
  - `MatchDrawer` disables Save until a decision is chosen. For `CLAIM_PARTIAL` it shows a % input and blocks values outside 0–100 with the inline error "Enter a percentage from 0 to 100".
  - `PeriodDashboard` shows the four metric cards (ITC in books, ITC in 2B, ITC at risk, Match rate) from the mocked summary.
  - A 409 from the API surfaces the DB message in a toast.
- [ ] **Step 2: Run** `npm test` and confirm the tests FAIL.
- [ ] **Step 3: Implement the pages from spec §12.** The layout follows the dashboard mockup: header, metric cards, grid, scorecard. The token is kept in memory with a sessionStorage fallback wrapped in try/catch.
- [ ] **Step 4: Run** `npm test` (PASS), then a manual smoke test: `npm run dev` with the API running, and walk through spec §14 steps 1–5.
- [ ] **Step 5: Commit** `feat(web): React UI for dashboard, matches, imports, reports`

### Task 14: Documentation, viva pack, deployment

**Files:**
- Create: `docs/er-diagram.md`, `docs/normalisation.md`, `docs/viva-demo.md`, `README.md` (replace), `render.yaml` or `Procfile`

**Interfaces:**
- Consumes: the final schema.

- [ ] **Step 1: Write a check script** `tools/check_docs.py`. It fails if any table in `information_schema.tables` (schema `public`, base tables) is missing from `docs/er-diagram.md` or `docs/normalisation.md`.
- [ ] **Step 2: Run** `python tools/check_docs.py` and confirm it FAILS.
- [ ] **Step 3: Write the docs.**
  - `er-diagram.md`: a Mermaid `erDiagram` showing every table and FK.
  - `normalisation.md`: for each table, the key, the FDs, and why it is in 3NF/BCNF. Explicitly cover why `pr_document` and `g2b_document` are separate, why `match_difference` exists, and the accepted denormalisation in `mv_vendor_scorecard`.
  - `viva-demo.md`: the 7 steps from spec §14 with exact clicks and psql commands, the measured accuracy table, and the four rehearsed answers from the prior-art report.
  - `README.md`: prerequisites, `docker compose up`, `python db/migrate.py`, `psql -f db/seed/demo_users.sql`, `python tools/synth.py`, running the API and web app, running the tests, and deploying (Neon + Render + Vercel).
- [ ] **Step 4: Run** `python tools/check_docs.py` (PASS). Then do a full clean setup from the README on a fresh clone, run `pytest db/tests backend/tests` and `npm test`, and confirm everything passes.
- [ ] **Step 5: Commit** `docs: ER diagram, normalisation, viva demo, README, deploy config`

---

## Suggested schedule (4 weeks, 4 people)

| Week | DB core (A) | Matching (B) | API (C) | Web + docs (D) |
|---|---|---|---|---|
| 1 | Tasks 1–3 | Task 4 | Task 5 | Scaffold Vite, mock API, ER draft |
| 2 | Task 8 | Tasks 6–7 | Task 12 (auth, imports) | Task 13 (login, dashboard) |
| 3 | Task 9 | Task 11 | Task 12 (rest) | Task 13 (drawer, reports) |
| 4 | Task 10 | Accuracy tuning | Deployment | Task 14, demo rehearsal |

Tasks 6–7 depend on 4–5, Task 9 depends on 8, and Task 12 needs 5–10 to be stable. Frontend work can start early against a mocked `api.ts`.
