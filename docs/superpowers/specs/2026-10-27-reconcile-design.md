# Reconcile — Design Spec

**Date:** 2026-10-27
**Status:** Draft for team review
**Course:** DBMS Lab project, NSUT (schema design, DDL, DML, DCL, triggers, views, procedures)

> **Assumptions to confirm before building** (each one changes the schema):
> 1. **PostgreSQL is allowed** in the lab. If the lab requires MySQL, row-level security, DOMAINs, generated `STORED` columns on functions, partial unique indexes and `fuzzystrmatch` all need workarounds — stop and re-plan.
> 2. **The user is a CA firm managing many client firms** (multi-tenant), not a single business.
> 3. Team of 3–4, roughly 4 weeks.

---

## 1. Problem

Every month, an Indian business may claim Input Tax Credit (ITC) only on purchase invoices its suppliers have reported, which appear in the buyer's **GSTR-2B** statement on the GST portal. Before filing **GSTR-3B** (due on the 20th), the firm's accountant must reconcile the **purchase register (books)** with **GSTR-2B (portal)**. Claiming more than 2B shows triggers a **DRC-01C** intimation (Rule 88D): pay or explain within 7 days, and GSTR-1 stays blocked until the firm responds.

CA firms do this for dozens of clients. Practitioners report 3–5 hours of manual matching per medium client per month. Their cycle is: 2B is generated on the 14th, differences are sorted by the 17th, IMS actions are taken by the 19th, and 3B is filed on the 20th.

**Reconcile** is a web app for a CA firm. It imports each client's purchase register and GSTR-2B JSON, matches them in PostgreSQL, and gives staff a workflow to resolve differences, chase suppliers and track ITC at risk.

## 2. Positioning: why build it if tools exist

Commercial tools (ClearTax, Tally, Zoho Books, Masters India) and open-source ones (India Compliance and aerele/reconciler for ERPNext, a 2026 Prisma tracker) already exist. **In every one we studied, the critical rules live in application code.**

Reconcile's thesis is that **the database is the source of truth**:
- One-to-one matching is enforced by UNIQUE partial indexes.
- The audit log is written by a trigger and is append-only through REVOKE.
- Staff and client isolation uses row-level security.
- Matching rules are rows in a table, executed by a stored procedure.

It also adds features the reviewed products don't document:
- Rule 37 180-day ageing.
- A `DUPLICATE` status.
- A vendor scorecard with a visible formula.
- Measured matching accuracy on labelled synthetic data.

See `docs/research/gst-reconciler-prior-art.md` for evidence.

## 3. Users and roles

| Role | Who | Can |
|---|---|---|
| `partner` | CA firm partner | Everything; close or reopen periods; override frozen rows; manage users and assignments |
| `staff` | Article assistant / accountant | Only **assigned** clients: import, run reconciliation, decide, link and delink, log follow-ups |
| `client_viewer` | A client firm's owner | Read-only view of **their own** firm's summaries and matches |

Roles are enforced **in the database** (PostgreSQL roles, GRANT/REVOKE and RLS), not only in the API.

## 4. Scope

**In scope**
- Clients, GSTINs, suppliers and return periods. Per-client tolerance settings.
- Import of GSTR-2B JSON (sections **B2B, B2BA, CDNR**) as downloaded from the portal.
- Import of a purchase-register CSV with Tally-style columns; rate-wise lines are aggregated to documents.
- The multi-pass matching procedure (§6).
- Three-axis status per match (§7), manual link and delink, remarks.
- IMS action planning, exported as CSV. Reconcile never pushes anything to GSTN.
- Audit log, period freeze, and append-only history.
- Supplier payments and Rule 37 ageing.
- A vendor follow-up log with a generated letter text.
- Dashboards: period summary, ITC at risk, deadline queue, vendor scorecard.
- A synthetic data generator with labelled perturbations, plus an accuracy report.
- A web frontend over all of the above.

**Out of scope** (say so in the viva)
- Live GSP/GSTN APIs, which need a GSP licence.
- ISD, IMPG, IMPGSEZ, ECOM sections, and GSTR-2A / GSTR-1 reconciliation.
- Item- and rate-wise matching. API v4 removed the rate-wise break-up from 2B.
- Many-to-many matches. These are handled by a manual link plus a remark, and listed as a limitation.
- OCR, email/WhatsApp sending, ML matching, Tally connectors, and PDF reports.

**Legal values stored as settings, not hard-coded facts:** the DRC-01C threshold, Rule 37 interest rate, Section 16(4) cutoff, and the IMS deemed-acceptance day. Each is labelled "verify on gst.gov.in".

## 5. Architecture

```
React (Vite, TS) ──HTTP/JSON──> FastAPI (Python 3.12, psycopg 3, raw SQL only)
                                      │  per request: BEGIN; SET LOCAL ROLE rc_<role>;
                                      │  SELECT set_config('app.user_id', ...);
                                      ▼
                              PostgreSQL 16+  ← all business rules live here
                              (procedures, triggers, views, RLS)
```

- **No ORM.** The API calls stored procedures and selects from views. Every SQL statement lives in a `.sql` file or a named constant, so the examiner can read it.
- **Migrations** are numbered `.sql` files in `db/migrations/`, applied in order by a 40-line Python runner that records them in `schema_migrations`.
- **PostgreSQL 16 minimum.** Do not use PG18-only features (`WITHOUT OVERLAPS`, `uuidv7()`), so the lab machine and the Neon free tier both work.
- **Extensions:** `fuzzystrmatch` (Levenshtein) and `pg_trgm` (supplier-name search). Both are trusted extensions.
- **Local development:** `docker compose` with `postgres:16`. **Deployment:** Neon free tier for the database, Render or Railway for the API, Vercel for the frontend. Avoid Render's free Postgres, which expires after 30 days.

## 6. Data model (3NF)

Natural key for a document: `(client_id, supplier_gstin, doc_type, doc_no_norm, fiscal_year)`. Invoice numbers are unique per supplier per financial year.

**Reference and tenancy**
- `state_code(code char(2) PK, name)`: 01–38, 97, 99.
- `gstin` **DOMAIN**: `char(15)` with the regex `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$` and `gstin_checksum_ok(VALUE)`, a mod-36 check.
- `app_user(user_id, full_name, email UNIQUE, role user_role, password_hash, active)`
- `client(client_id, legal_name, pan char(10))`
- `client_gstin(gstin gstin PK, client_id FK, state_code FK)`, with a CHECK that `substr(gstin,1,2) = state_code` and `substr(gstin,3,10) = client.pan`. The PAN check is enforced by a trigger.
- `staff_assignment(user_id FK, client_id FK, PK(both))`
- `client_user(user_id FK, client_id FK, PK(both))`
- `recon_settings(client_id PK FK, rounding_tol numeric(8,2) DEFAULT 1, review_tol numeric(8,2) DEFAULT 10, date_window_days int DEFAULT 10, fuzzy_max_dist int DEFAULT 2, lookback_months int DEFAULT 12, r37_interest_pct numeric(5,2) DEFAULT 18)`
- `return_period(period_id, gstin FK, rtnprd char(6) /*MMYYYY*/, fiscal_year char(7) /*2026-27*/ , status period_status DEFAULT 'OPEN', UNIQUE(gstin, rtnprd))`
- `supplier(client_id FK, supplier_gstin text, trade_name, PK(client_id, supplier_gstin))`

**Books side**
- `pr_import_line`: a staging table with raw CSV columns, one row per rate line.
- `pr_document(pr_id, period_id FK, supplier_gstin gstin, doc_type doc_type /*INV,CRN,DBN*/, doc_no, doc_no_norm GENERATED ALWAYS AS (norm_doc_no(doc_no)) STORED, doc_date, taxable_value, igst, cgst, sgst, cess, total_tax GENERATED, is_duplicate bool DEFAULT false)`. Credit notes are stored with negative signs.
- `payment(payment_id, pr_id FK, paid_on date, amount numeric(14,2) CHECK (amount > 0))`

**Portal side**
- `gstr2b_raw(raw_id, period_id FK, payload jsonb, imported_at, imported_by)`
- `g2b_document(g2b_id, period_id FK, raw_id FK, section g2b_section /*B2B,B2BA,CDNR*/, supplier_gstin text, gstin_valid bool, supplier_name, doc_type, doc_no, doc_no_norm GENERATED, doc_date, taxable_value, igst, cgst, sgst, cess, total_tax GENERATED, itc_available bool, itc_reason, supplier_filed_on date, orig_doc_no, is_superseded bool DEFAULT false, is_duplicate bool DEFAULT false)`. `supplier_gstin` is not the domain type, because special registrations break the checksum, so the result is stored as a flag.

**Reconciliation**
- `match_rule(rule_seq int PK, code text UNIQUE, description, enabled bool)`: one row per pass in §8.
- `recon_run(run_id, period_id FK, started_by FK, started_at, finished_at, stats jsonb)`
- `recon_match(match_id, run_id FK, period_id FK, pr_id FK NULL, g2b_id FK NULL, match_status, rule_seq FK, is_active bool DEFAULT true, review_decision DEFAULT 'PENDING', claim_pct numeric(5,2), ims_action DEFAULT 'NO_ACTION', pending_since char(6), itc_reduction_declared itc_reduction, remark, decided_by, decided_at)`
  - CHECK: at least one of `pr_id`, `g2b_id` is not null.
  - **Partial UNIQUE indexes:** `(pr_id) WHERE is_active AND pr_id IS NOT NULL`, and the same for `g2b_id`.
- `match_difference(match_id FK, field diff_field /*DOC_NO,DATE,GSTIN,TAXABLE,IGST,CGST,SGST,CESS,PERIOD,HEAD_SPLIT*/, books_value text, portal_value text, PK(match_id, field))`. This is normalised; it is not a comma-joined string, which was aerele's mistake.
- `recon_action_log(log_id, match_id, actor_id, acted_at, column_name, old_value, new_value, remark)`. It is append-only: UPDATE and DELETE are revoked from every app role.
- `vendor_followup(followup_id, client_id, supplier_gstin, period_id, status followup_status /*OPEN,SENT,RESPONDED,RESOLVED*/, letter_text, created_by, created_at)`
- `status_label_map(code, tool, label)`: maps our codes to ClearTax, Tally, Zoho, India Compliance and GSTN labels, for the viva.

ER diagram: `docs/er-diagram.md` (Mermaid). Normalisation argument per table: `docs/normalisation.md`.

## 7. Three status axes (never collapsed)

1. `match_status` (set **only** by the procedure or a manual link): `EXACT, ROUNDING, PROBABLE, VALUE_MISMATCH, PERIOD_MISMATCH, ONLY_IN_BOOKS, ONLY_IN_2B, DUPLICATE, MANUAL, SUPERSEDED`
2. `review_decision` (staff): `PENDING, ACCEPT_BOOKS_VALUE, ACCEPT_PORTAL_VALUE, CARRY_FORWARD, CHASE_VENDOR, IGNORE, CLAIM_PARTIAL, INELIGIBLE, REVERSE_R37`. `CLAIM_PARTIAL` requires `claim_pct BETWEEN 0 AND 100`.
3. `ims_action` (the planned portal action): `NO_ACTION, ACCEPTED, REJECTED, PENDING, DEEMED_ACCEPTED`
   - `PENDING` on a credit note is allowed for one period only: `pending_since` must equal the match's period or the period before it.
   - `ACCEPTED` on a credit note requires `itc_reduction_declared IN ('NONE','FULL','PARTIAL')`.

The wording "Only in…" is used instead of "Missing in…".

## 8. Matching procedure `sp_reconcile(p_period_id bigint, p_user_id bigint) RETURNS bigint /*run_id*/`

The procedure is set-based, with one `INSERT INTO recon_match … SELECT … ON CONFLICT DO NOTHING` per pass. It reads `match_rule` in `rule_seq` order and skips disabled rules. Before each run it deactivates prior *system* matches for the period, but keeps `MANUAL` matches and any decided rows.

| Seq | Code | Condition | Tolerance | Status / reason |
|---|---|---|---|---|
| 0 | `PREFLIGHT` | Mark superseded originals (B2BA `orig_doc_no`); flag duplicates on the natural key within each side | n/a | `SUPERSEDED`, `DUPLICATE` rows |
| 1 | `EXACT` | Same GSTIN, doc_type, doc_no_norm, FY, and date | every head diff = 0 | `EXACT` |
| 2 | `ROUNDING` | Same key, dates within `date_window_days` | each head ≤ `rounding_tol` | `ROUNDING` |
| 3 | `FUZZY_NO` | Same GSTIN and doc_type; `levenshtein_less_equal(a,b,fuzzy_max_dist) ≤ fuzzy_max_dist` OR one number contains the other; dates within window; **mutual-best** pair only (`row_number()` on both sides) | each head ≤ `rounding_tol` | `PROBABLE` + diff `DOC_NO` |
| 4 | `VALUE` | Same GSTIN and doc_no_norm, amounts ignored | n/a | `VALUE_MISMATCH` + per-head diffs; `HEAD_SPLIT` if IGST vs CGST+SGST |
| 5 | `PAN` | Same PAN `substr(gstin,3,10)`, different GSTIN, same doc_no_norm | total tax ≤ `rounding_tol` | `PROBABLE` + diff `GSTIN` |
| 6 | `CROSS_PERIOD` | Leftovers against unmatched rows of other periods in the same FY, within `lookback_months` | as pass 2 | `PERIOD_MISMATCH` |
| 7 | `RESIDUE` | Anti-join of both sides | n/a | `ONLY_IN_BOOKS` / `ONLY_IN_2B` |

A difference above `rounding_tol` but at or below `review_tol` is flagged for quick acceptance in the UI (`v_match_detail.needs_review_only`).

**Baseline for comparison:** `v_naive_match` is a single FULL OUTER JOIN on GSTIN and the raw doc_no. The viva shows naive vs multi-pass side by side, plus `EXPLAIN ANALYZE` before and after indexes.

## 9. Triggers

- `trg_match_audit` (AFTER UPDATE ON recon_match): writes one `recon_action_log` row per changed decision column, with actor taken from `current_setting('app.user_id')`.
- `trg_period_freeze` (BEFORE INSERT/UPDATE/DELETE on recon_match, pr_document, g2b_document, payment): raises if the period is `CLOSED`, unless the current role is `rc_partner`.
- `trg_decided_lock` (BEFORE UPDATE ON recon_match): once `review_decision <> 'PENDING'`, only `rc_partner` may change `pr_id`, `g2b_id` or `match_status`.
- `trg_gstin_pan` (BEFORE INSERT/UPDATE ON client_gstin): the GSTIN's PAN must equal `client.pan`.

## 10. Views

- `v_period_summary(period_id)`: counts and ₹ per `match_status`, ITC in books, ITC in 2B, ITC at risk, and match rate.
- `v_itc_at_risk`: per client and period, the tax on `ONLY_IN_BOOKS`, `VALUE_MISMATCH` excess and pending items.
- `v_rule37_ageing`: books invoices with unpaid balance, bucketed into 0–150, 150–180 and >180 days since invoice date. The >180 bucket proposes `REVERSE_R37`, with interest at `r37_interest_pct`.
- `mv_vendor_scorecard` (MATERIALIZED, refreshed by `sp_refresh_scorecard()`): per client and supplier over 12 months. It reports % only-in-books, value-mismatch rate, IMS rejections, average filing delay, and ITC lost. The `score` formula is written in the view definition.
- `v_deadline_queue`: open matches per client with days to the 14th, 19th and 20th.
- `v_match_detail`: the main UI grid, joining match, books, portal and differences.
- `v_ims_action_plan`: the export for the client to act on IMS.
- `v_accuracy` (synthetic data only): precision and recall per pass, compared against `synth_truth`.

## 11. DCL / security

- **Group roles:** `rc_partner`, `rc_staff`, `rc_client`. Login roles: `rc_migrator` (owner) and `rc_api` (the API's login, a member of all three, with `NOINHERIT`). Each request runs `SET LOCAL ROLE`.
- **Grants:**
  - `rc_client` gets SELECT on summary views only.
  - `rc_staff` gets DML on documents and matches. UPDATE on `recon_match` is restricted to the decision columns through column-level GRANT.
  - Nobody gets UPDATE or DELETE on `recon_action_log`.
- **RLS:** on every tenant table, with the policy `client_id IN (SELECT client_id FROM staff_assignment WHERE user_id = current_setting('app.user_id')::bigint)` for staff, `client_user` for clients, and `true` for partners. Tables without `client_id` use a helper `period_client(period_id)`.
- **Passwords:** stored with pgcrypto `crypt()`, never in plain text.

## 12. Frontend pages

1. Login.
2. Clients (staff see only their assigned clients).
3. Period dashboard: metric cards, the match grid with status chips, re-run button, and filters.
4. Match drawer: books vs portal side by side, differences, decision, IMS action, remark, link and delink, and the audit history.
5. Imports: upload 2B JSON and purchase register CSV, with a preview.
6. Vendor scorecard and follow-up letters.
7. Rule 37 ageing.
8. "Under the hood" panel: shows the SQL of the last procedure call and the `EXPLAIN` output. This is a viva aid.

## 13. Testing and evidence

- **SQL behaviour tests** in pytest against a real PostgreSQL. Each test gets a fresh database built by running all migrations.
- **Synthetic data:** `tools/synth.py` generates valid GSTINs and a purchase register. It derives a 2B JSON with **labelled** perturbations (format change, typo, ±₹0.5 rounding, date shift, wrong GSTIN with same PAN, books-only, 2B-only, B2BA amendment, credit note, duplicate) and writes `synth_truth`.
- **Accuracy target:** ≥95% precision for `EXACT`, `ROUNDING` and `PROBABLE` on the default synthetic set of 500 documents across 3 clients. The report must show the measured figures.

## 14. Viva demo script (must work end to end)

1. Log in as staff. Show they can see only 2 of 3 clients. Then run the same query in psql under `rc_staff` to show RLS.
2. Import 2B and the purchase register, then run the reconciliation and show the dashboard.
3. Open a `PROBABLE` match where `DP/0089` was matched to `89`. Show which rule matched it and the Levenshtein distance.
4. Try linking an already-matched portal invoice and show the UNIQUE violation.
5. Accept a match, close the period, try to edit as staff (fails), then edit as partner (succeeds). Show the audit log.
6. Show naive vs multi-pass counts and `EXPLAIN ANALYZE`.
7. Show Rule 37 ageing and the vendor scorecard formula.

## 15. Team split (suggested)

- **A, DB core:** schema, domains, triggers, DCL and RLS.
- **B, matching:** procedure, synthetic data and accuracy.
- **C, API and imports.**
- **D, frontend and docs:** ER diagram, normalisation document and demo script.

Everyone writes their own SQL tests.
