# Make the database enforce what others code

Yes, GSTR-2B vs purchase-register reconciliation already exists, both commercially and as open source, so Reconcile cannot be pitched as a new product. It can be pitched, and defended, as a **new architecture**. The mature open-source reference is India Compliance's Purchase Reconciliation Tool for ERPNext. It uses an ordered rule table (exact, fuzzy, ₹1 rounding), a cleaned and fuzzy-matched bill number within a 10-day date window, a PAN-level fallback, and separate match, action and IMS-action fields. **Every one of those guarantees lives in Python, not in the database** ([India Compliance](https://github.com/resilient-tech/india-compliance/blob/develop/india_compliance/gst_india/doctype/purchase_reconciliation_tool/__init__.py)). The closest student-shaped analogue, neevjain-eve's 2026 Next.js + Prisma + PostgreSQL tracker, already models firm, client, GSTIN, period, run, result, decision and audit. It too is ORM-only, with no triggers, views or procedures ([neevjain-eve](https://github.com/neevjain-eve/gst-reconciliation-tracker)). Commercial products (ClearTax, Tally, Zoho, Masters India, GSTZen, KDK) agree on about six concepts but share no vocabulary. Most of them stop at matching. **None of the product docs reviewed documents Rule 37 payment-ageing tracking, publishes a vendor-scoring formula, or labels duplicates as a status.** Tally and Zoho Books also lack multi-client handling ([Tally](https://help.tallysolutions.com/gstr-2b-reconciliation/); [AI Accountant](https://www.aiaccountant.com/blog/gstr-2b-reconciliation-tools-overview-21da6)). The NSUT team should therefore:

- **Copy** India Compliance's rule table and three-axis status model.
- **Copy** neevjain-eve's multi-client hierarchy and its "keep human decisions on re-run" rule.
- **Copy** the GSTN Offline Matching Tool's category definitions as the authoritative reference.
- **Rebuild** all of it as set-based SQL that PostgreSQL itself enforces: UNIQUE one-to-one matches, a GSTIN checksum domain, trigger-written audit and period freezing, row-level security per client, and a stored procedure that runs data-driven matching passes.

The team should cut live GSP/IMS APIs, OCR, messaging automation and every GSTR-2B section except B2B, B2BA and CDNR. That makes the project buildable in a few weeks and turns "doesn't this already exist?" into the strongest question in the viva.

## Seven open-source projects: one mature engine, one near-twin, and many four-bucket scripts

The open-source landscape splits into three tiers. Each one teaches something different. The tiers are summarised here; the per-project lessons are in the table that follows.

**Tier 1: two Frappe/ERPNext apps.**

- **aerele/reconciler** (2021, about 1,500 lines, 246 Frappe Cloud installs) shows the simplest explainable scoring. Each 2B row is scored against candidate Purchase Invoices with eight boolean checks: date equal, number equal, and six amounts within ₹1 ([aerele](https://github.com/aerele/reconciler/blob/master/reconciler/reconciler/doctype/cd_gstr_2b_data_upload_tool/cd_gstr_2b_data_upload_tool.py); [Frappe Cloud](https://cloud.frappe.io/marketplace/apps/reconciler)).
  - Its much-quoted "threshold 12" is **not a match score**. It is `month_threshold`, the number of months back to search for candidate bills. The default is 6 and the recommended value is 12.
  - The code shows real defects. The Mismatch branch sorts ascending and so picks the *worst*-scoring candidate. A loop tests a stale variable. The match reason is a comma-joined string. The PR link is a nullable FK on the 2B row, checked with one query per invoice.
  - It also has a good idea worth stealing: **account freezing**. Accepted rows, and rows dated on or before a frozen-upto date, can be changed only by a privileged role. Bulk accept is refused when the tax difference exceeds ₹10 ([aerele report](https://github.com/aerele/reconciler/blob/master/reconciler/reconciler/report/cr_gstr_2b_vs_pr_gstin_matching_tool/cr_gstr_2b_vs_pr_gstin_matching_tool.py)).
  - It has no IMS support, because it predates IMS.
- **India Compliance** (resilient-tech, committed to on the day of this research) is the state of the art.
  - **Rule table.** GSTIN_RULES compare FY, supplier GSTIN, company GSTIN, bill number, place of supply, reverse charge and five amounts. Each field is marked Exact, Fuzzy, ≤₹1 or ignored, and the rules are applied in order. A rule yields Exact Match, Suggested Match or Mismatch. PAN_RULES then run on the leftovers keyed by PAN, comparing total GST instead of the head-wise split.
  - **Bill-number cleaning.** The cleaner removes the FY token in nine spellings, plus `/` and `-`, collapses spaces and strips leading zeros. It then accepts a pair if `partial_ratio == 100` or RapidFuzz ratio ≥ 90, and rejects pairs more than 10 days apart.
  - **Statuses.** The status model separates `match_status`, `action` (Accept/Pending/Ignore) and `ims_action`. When an IMS Reject is applied, the prior action is saved in `previous_action` and restored if the Reject is reversed ([India Compliance](https://github.com/resilient-tech/india-compliance/blob/develop/india_compliance/gst_india/doctype/purchase_reconciliation_tool/__init__.py); [IMS doctype](https://github.com/resilient-tech/india-compliance/blob/develop/india_compliance/gst_india/doctype/gst_invoice_management_system/gst_invoice_management_system.py)).
  - **Naming.** Current code and docs renamed "Missing in PR/2B" to **"Only in Books" / "Only in 2A/2B"**.
  - **Documented gotchas.** ITC booked through Journal Entries is never matched. A CGST+SGST vs IGST split points to a place-of-supply error. Downloaded 2B has no item detail ([Frappe docs](https://docs.frappe.io/erpnext/purchase-reconciliation-gstr-2b-mismatch)).

**Tier 2: two 2026 PostgreSQL web apps.** These are the true neighbours of Reconcile.

- **neevjain-eve/gst-reconciliation-tracker**:
  - **Schema.** Organization → Client → GstinRegistration → TaxPeriod (unique gstin+year+month), with immutable Invoice and Gstr2bRecord inputs. ReconciliationRun → ReconciliationResult has a unique `pairKey`, a confidence score, an explanation, a JSONB `differences` column, a decision, an assignee, a follow-up due date and an `isStale` flag. There are also Comment and AuditLog tables.
  - **Scoring.** The fuzzy pass scores pairs out of 100: GSTIN 35, invoice number 30, date 10/7/4, taxable 15/10/5, tax 10. A score of 80 or more is auto-classified, 55–79 goes to NEEDS_REVIEW, and anything below 55 is not a candidate. Amounts within ₹1 are MATCHED, and within ₹100 are MATCHED_WITH_VARIANCE.
  - **Re-runs.** Re-running preserves human-touched results ([RECONCILIATION.md](https://github.com/neevjain-eve/gst-reconciliation-tracker/blob/main/docs/RECONCILIATION.md); [migration.sql](https://github.com/neevjain-eve/gst-reconciliation-tracker/blob/main/prisma/migrations/20260921000000_init/migration.sql)).
- **sangamyo/gst-recon-ai** (FastAPI + PostgreSQL + Celery) is the minimal baseline. It uses an exact key `gstin|invoice|date`, sums rows per key, does a pandas outer merge and applies a ₹1 tolerance to give four buckets ([recon_engine.py](https://github.com/sangamyo/gst-recon-ai/blob/main/GSTReconAI/backend/app/services/recon_engine.py)). Putting the date in the key is its main flaw: a date typo turns one real pair into two "missing" rows.

**Tier 3: single-file tools, each with one idea worth keeping.**

- **vsinghidelhi** contributes a catalogue of *explainable* typo classes: one-character substitution, prefix omitted in books (`079` vs `PSB-079`), double paste, and a date typed into the number field ([reconciler.py](https://github.com/vsinghidelhi/gst-reconciliation/blob/main/reconciler.py)).
- **Rekora**, a hackathon project, contributes an IMS-advice table that maps each issue type to a suggested Accept, Reject or Pending ([engine.js](https://github.com/TeamElara/Intelligent-Tax-Reconciliation/blob/main/engine.js)).
- **ebinmichael** and **vkWeb** mostly serve as warnings. vkWeb keys a dictionary on the raw invoice number without aggregating item rows, so multi-rate invoices silently overwrite one another ([helpers.py](https://github.com/vkWeb/gstr-reconciliation/blob/main/reconcile/helpers.py)).

The research found no pure-SQL or Excel-VBA open-source reconciler. GitHub code search was blocked during the research, so small repos may have been missed.

| Project | Copy | Avoid | Do differently in Reconcile |
|---|---|---|---|
| **aerele/reconciler** (Frappe) | Eight-check score as an explainable baseline; account freezing (role + frozen-upto date); ₹10 bulk-accept guard; Supplier view vs Document view reports | Exact raw-string number match; picking the lowest-scoring candidate; comma-joined `cf_reason`; nullable FK on 2B row for the link; per-row `get_value` (N+1) | Express the score as a SQL expression in a view; a `match_difference` child table instead of a reason string; a separate `recon_match` table with UNIQUE on both sides; freezing via trigger + GRANT |
| **India Compliance** (Frappe) | Ordered rule table; FY-aware bill-number cleaner; 10-day window; PAN fallback; signed credit notes matched only within doc type; three status axes; `previous_action` save/restore on IMS Reject | Python-only enforcement of one-to-one; greedy first-hit within a rule; status spread across two doctypes | `match_rule` table read by a stored procedure; cleaner as an IMMUTABLE function backing a generated column; save/restore as a BEFORE UPDATE trigger; 1:1 enforced by constraints |
| **neevjain-eve tracker** (Prisma + PG) | Firm → client → GSTIN → period hierarchy; immutable source tables; `pairKey` uniqueness; human decisions survive re-runs; `isStale`; JSONB differences; follow-up due date | Scoring, status and audit only in the ORM; weighted score hard to defend in a viva | Same hierarchy, with RLS per client; audit by trigger; scores computed in SQL; "survives re-run" enforced by a procedure that never overwrites a decided row |
| **sangamyo/gst-recon-ai** (FastAPI + PG) | One-statement baseline (outer join + tolerance) as "pass zero" and a teaching contrast | Date inside the exact key; only space removal for normalisation | Show the naive FULL OUTER JOIN first, then show how multi-pass matching recovers the pairs it splits |
| **vsinghidelhi** (Python + Apps Script) | Named typo classes; FY token rewrite; ₹2 tolerance as a configurable alternative | Unmatched comma-grouped bills | Record a `typo_class` on PROBABLE matches so every fuzzy decision has a human-readable reason |
| **Rekora** (browser JS) | Issue-type → suggested IMS action map; ITC-at-risk ranking by ₹ | Advice in code, not data | Store the advice as an `ims_advice` lookup table joined by a view |
| **ebinmichael / vkWeb** | Four-bucket UI simplicity | No normalisation (vkWeb); item rows overwriting each other; no date check | Aggregate rate-wise lines to document level before matching (`pr_line` → `pr_document`) |

## Commercial tools agree on six concepts and disagree on every label

The commercial products converge on six concepts:

- exact match
- near or suggested match
- value mismatch
- books only
- portal only
- manual override

Beyond those six, the labels and the extras differ.

- **ClearTax** has Exact Match, Suggested Match, Mismatch, Missing in PR and Missing in 2B. Its actions include "Accept My Value" vs "Accept Supplier Value", partial-percentage ITC claims, ITC Pending, ITC Reversed and ITC Ineligible, all with remarks ([ClearTax](https://docs.cleartax.in/product-help-and-support/product-and-settings/ca-sme/ca-sme-gstr-3b/gstr-2b-vs-purchase-register-reconciliation-a-step-by-step-guide)).
- **Tally** adds "Mismatch in Return Period", "Invoices Rejected in IMS" and "Uncertain Transactions". Its "Potential match (Excluding Section / Party GSTIN / Doc No.)" subtypes are the clearest model of relaxing one key at a time ([Tally](https://help.tallysolutions.com/gstr-2b-reconciliation/)).
- **Masters India** exposes configurable buffers for tax value, taxable value and invoice date, plus auto-accept rules, bulk force-match and delink, and scheduled jobs ([Masters India](https://docs.mastersindia.co/masters-india-gst-software/introduction/reconciliation/gstr-2b-vs-purchase-register-reconciliation)).
- **GSTZen** colour-codes amounts that differ by more than ₹10 as red ([GSTZen](https://www.gstzen.in/?p=5599)).
- **Zoho Books** works per organisation. Its actions are Create-and-Match, Modify-and-Match and Accept, and it can regenerate 2B after IMS actions ([Zoho](https://www.zoho.com/in/books/help/gst/gstr2b-reconcile.html)).

None of the product docs gives the numeric thresholds behind "suggested". The only official definition comes from GSTN's own Offline Matching Tool, which is why the team should cite it:

- **Seven parameters:** GSTIN, document type, number, date, taxable value, total tax and head-wise tax.
- **Exact:** all seven match.
- **Partial:** GSTIN and type match, and exactly one other parameter differs.
- **Probable:** GSTIN or type differs, and the other five match.
- **Tolerance:** selectable from ₹0 to ₹10 per tax head.
- **Number approximation:** ignores `/`, `-` and leading or trailing zeros.

([GSTN Matching Offline Tool manual](https://tutorial.gst.gov.in/downloads/matchingtool.pdf))

The wider workflow features divide the market. Here the gaps matter more than the features:

- **Multi-client support.** ClearTax, IRIS, KDK and Winman have it. Tally and Zoho Books do not. This comes from a competitor-written comparison, so treat it as indicative ([AI Accountant](https://www.aiaccountant.com/blog/gstr-2b-reconciliation-tools-overview-21da6)).
- **Vendor follow-up.** KDK claims one-click vendor mismatch reports, scheduled emails, WhatsApp and "compliance scores", but publishes no details ([KDK](https://www.kdksoftware.com/blog/gstr-2a-2b-reconciliation/)). Vyapar TaxOne (formerly Suvit) offers WhatsApp reminders ([TaxOne](https://t1.suvit.io/gst-reconciliation-feature)).
- **Vendor scoring.** ClearTax's vendor view offers only High/Medium/Low risk tags and filing status.
- **Rule 37.** The research found **no product documenting Rule 37 payment-ageing tracking**, only explainer articles ([ClearTax Rule 37](https://cleartax.in/s/rule-37-of-cgst-sgst-rules-itc-reversal-180-days)).

Complaint evidence is thin but consistent:

- A CA practitioner describes a fixed monthly cycle: download 2B by the 14th, triage by the 17th, act in IMS by the 19th, file 3B on the 20th. They say manual matching takes **"3-5 hours for a medium business"** ([caclubindia](https://www.caclubindia.com/forum/research-how-do-ca-firms-handle-gstr-2b-reconciliation-amp-ims-not-selling-anything--615624.asp)).
- ClearTax reviewers on Capterra (dated 2022) complain about extra charges for PAN-level reports, mismatches between the portal and the tool, and ERP uploads that need extra columns ([Capterra](https://capterra.com/p/265637/ClearTax-GST/reviews/)).

| Product | Copy | Avoid | Do differently in Reconcile |
|---|---|---|---|
| **ClearTax** | Accept-books-value vs accept-portal-value; ITC Pending / Reversed / Ineligible / partial-percentage claim states; remarks on both sides; Vendor and Document views | Opaque "Suggested Match" with no published tolerance; PAN-level reports as a paid add-on | Publish every threshold in a `recon_settings` row; PAN rollup as a free view |
| **TallyPrime** | Period-mismatch bucket; IMS-rejected bucket; "potential match excluding X" as relax-one-key passes; rounding-only quick reconcile | Single-company scope | Relax-one-key passes as numbered rule rows; multi-client from day one |
| **Zoho Books** | Create-and-Match / Modify-and-Match; 2B regeneration after IMS | Per-organisation silo | Model 2B recompute as a `gstr2b_version` snapshot table |
| **Masters India** | Per-client buffers (tax, taxable, date); auto-accept rules; force-match and delink with history; Vendor-Wise and Column-Wise reports | Bulk actions only through Excel upload | Buffers as columns; link and delink history in an append-only log |
| **GSTZen** | Exact / Strong / Weak strength; ₹10 "red" threshold | Colour as the only status signal | Match strength as data (pass number + typo class), with colour in the UI only |
| **KDK Spectrum** | Bulk multi-GSTIN view, task assignment, audit trail, high-value priority (all claimed) | Unpublished scoring | Transparent scorecard formula visible in a view definition |
| **Vyapar TaxOne** | Single dashboard of client follow-ups | WhatsApp automation (out of scope) | `followup` table with status (sent / responded / resolved) and generated letters |
| **GSTN Offline Matching Tool** | Official seven-parameter categories; ₹0–10 tolerance; number approximation | Standalone single-taxpayer use | Cite it as the specification the procedure implements |

## A unified vocabulary on three independent axes

Two lessons decide the status design. First, India Compliance shows that machine verdict, human decision and IMS decision must be separate fields. Collapsing them is the commonest design smell in the smaller repos. Second, every tool uses different words, so Reconcile should define one canonical code per concept and show a mapping table in its report. That mapping table also answers the viva question "where did these categories come from?"

**Axis 1: `match_status`.** This is set only by the reconciliation procedure. Values are PostgreSQL ENUM (or lookup-table) codes.

| Code | Meaning | India Compliance | aerele | GSTN offline tool | ClearTax / Tally / Zoho |
|---|---|---|---|---|---|
| `EXACT` | All seven parameters equal, every head diff = 0 | Exact Match | Exact Match | Exact | Exact Match / Reconciled / Matched |
| `ROUNDING` | Keys equal, every head within the rounding tolerance (default ₹1) | Suggested Match (rule 3) | Partial Match | Exact within tolerance | Suggested / rounding quick-reconcile |
| `PROBABLE` | One key relaxed (number fuzzy, GSTIN same PAN, doc type, date window); sub-reason in `match_difference` | Suggested Match / PAN rules | Probable Match | Partial / Probable | Suggested / Potential (excluding X) / Partially Matched |
| `VALUE_MISMATCH` | Keys match, an amount beyond tolerance; `HEAD_SPLIT` (IGST vs CGST+SGST) recorded as a difference reason | Mismatch | Mismatch | Unmatched | Mismatch |
| `PERIOD_MISMATCH` | Paired with a 2B row from another return period in the same FY | n/a (claim period field) | n/a | n/a | Mismatch in Return Period (Tally) |
| `ONLY_IN_BOOKS` | No 2B counterpart | Only in Books | n/a | In PR not in 2B | Missing in 2B / Missing in GSTN |
| `ONLY_IN_2B` | No books counterpart | Only in 2A/2B | Missing in PR | In 2B not in PR | Missing in PR / Missing in Zoho Books |
| `DUPLICATE` | Same supplier GSTIN + normalised number + doc type + FY appears twice on one side | n/a | n/a | n/a | not shown in docs reviewed |
| `MANUAL` | Pair linked by a user, which overrides the procedure | Manual Match | Accepted link | n/a | Manual Group / Force Match |
| `SUPERSEDED` | Original 2B document replaced by a B2BA/CDNRA amendment | Amended | n/a | n/a | n/a |

The team should use India Compliance's newer **"Only in…" phrasing rather than "Missing in…"**. "Missing in 2B" sounds as if the portal is wrong. "Only in Books" states a fact and leaves the cause open: the supplier has not filed, the GSTIN is wrong, or it is a timing difference.

**Axis 2: `review_decision`.** This is the CA staff's decision. Values are `PENDING`, `ACCEPT_BOOKS_VALUE`, `ACCEPT_PORTAL_VALUE`, `CARRY_FORWARD` (a timing difference, recheck next period), `CHASE_VENDOR`, `IGNORE`, `CLAIM_PARTIAL` (with a percentage), `INELIGIBLE` and `REVERSE_R37`. These merge ClearTax's claim states, India Compliance's Accept/Pending/Ignore and the practitioner's three-bucket triage.

**Axis 3: `ims_action`.** This is the decision the client *would* make on the GSTN portal. Values are `NO_ACTION`, `ACCEPTED`, `REJECTED`, `PENDING` and `DEEMED_ACCEPTED`.

- Records left at no action are deemed accepted when 2B is generated on the 14th ([ClearTax IMS FAQ](https://cleartax.in/s/faqs-on-invoice-management-system-ims-gst)).
- From the October 2025 period, Pending is allowed on credit notes for one tax period only. Accepting a credit note requires an ITC-reduction declaration of none, full or partial. Remarks are visible to the supplier ([EY](https://www.ey.com/en_in/technical/alerts-hub/2025/09/gstn-issues-advisory-on-the-changes-introduced-in-the-invoice-management-system); [Tally Oct 2025](https://tallysolutions.com/gst/gst-return-filing-critical-changes/)).

Those rules become CHECK constraints and triggers: a `pending_since_period` column, an `itc_reduction_declared` column, and a warning view for documents still at `NO_ACTION` as the 14th approaches.

## Five passes, two tolerances, and one-to-one enforced by constraints

The data model follows the shape of the input files. GSTR-2B JSON is `data → docdata → b2b[] → {ctin, trdnm, supprd, supfildt, inv[] → {inum, dt, val, pos, rev, itcavl, rsn, …}}`, with dates as `DD-MM-YYYY` text. CDNR uses `nt[]` with `ntnum` and note type C/D, and B2BA carries `oinum`/`oidt` pointing to the original ([Vayana mirror of the GSTN API](https://docs.enriched-api.vayana.com/routes/basic/gstn/apis/returns/Gstr-Download-Lite-Api/gstr-2b/)). Since API v4 (downloads from 14 November 2024), the rate-wise break-up is gone and only total tax per document remains ([Tally on API v4](https://tallysolutions.com/gst/important-changes-to-gstr-2b-api-by-gstn-from-october-2024/)). Purchase registers, by contrast, arrive rate-wise, with one row per rate and columns such as "GSTIN of Supplier", "Invoice Number" and "Integrated Tax Paid" ([Tally GSTR-2 Excel](https://help.tallysolutions.com/docs/te9rel64/Tax_India/gst/data_captured_in_gstr2_excel.htm)).

So Reconcile should do two things:

- **Stage raw data.** Load the 2B JSON into a `gstr2b_raw(client_id, rtnprd, payload jsonb)` table. Shred it with `jsonb_array_elements` into `gstr2b_document`, and aggregate `pr_line` into `pr_document` before any matching.
- **Use a natural key.** The natural uniqueness key is `(client_id, supplier_gstin, doc_type, doc_no_norm, fiscal_year)`. Invoice numbers are unique per supplier per financial year, not globally.

Two validators belong in DDL rather than application code:

- **GSTIN domain.** A `gstin` DOMAIN checks the regex `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$` plus the mod-36 check character. The algorithm was verified against python-stdnum test vectors: `27AAPFU0939F1ZV` is valid ([python-stdnum](https://arthurdejong.org/python-stdnum/doc/stdnum.in_.gstin)). Use it as a hard constraint on client and books data. Use only a warning flag on imported 2B data, because special registrations (UIN, NRTP, OIDAR) follow other patterns. Even the published API sample GSTIN `01AABCE2207R1Z5` fails the checksum.
- **Normalised number.** An IMMUTABLE `norm_doc_no()` function backs a `GENERATED ALWAYS AS … STORED` column. It uppercases, strips non-alphanumerics and leading zeros, and optionally removes the FY token the way India Compliance's cleaner does.

### Recommended passes

Each pass is one set-based `INSERT INTO recon_match … SELECT … WHERE NOT EXISTS (already matched)`. The pass definitions are rows in a `match_rule` table, which the procedure `sp_reconcile(client_id, period)` reads in `rule_seq` order. Explaining the procedure in a viva then means reading a table aloud.

| Pass | Join key / condition | Tolerance | Result | Lineage |
|---|---|---|---|---|
| 0. Pre-flight | Aggregate lines to documents; sign credit notes negative; mark amended originals `SUPERSEDED`; flag `DUPLICATE` within each side | n/a | Clean candidate pools | India Compliance CN signing; neevjain duplicate step |
| 1. Exact | GSTIN + doc_type + doc_no_norm + FY, date equal | every head diff = 0 | `EXACT` | GSTN "Exact" |
| 2. Rounding | Same key, date within window | each head ≤ rounding tol (₹1) | `ROUNDING` | India Compliance rule 3; GSTN tolerance |
| 3. Fuzzy number | Same GSTIN + doc_type, `levenshtein_less_equal(a,b,2) <= 2` or containment or `similarity() >= 0.9`, date ≤ 10 days | each head ≤ ₹1 | `PROBABLE` (reason `DOC_NO`, typo class) | India Compliance fuzzy rules; vsinghidelhi typo classes |
| 4. Value mismatch | Same GSTIN + doc_no_norm (or pass-3 fuzzy), ignore amounts | diff > rounding tol; flag `NEEDS_REVIEW` if ≤ review tol | `VALUE_MISMATCH` | India Compliance rules 5–6; GSTN "Unmatched" |
| 5. PAN fallback | Same PAN (`substr(gstin,3,10)`), different GSTIN, doc_no_norm equal | total GST ≤ ₹1 | `PROBABLE` (reason `GSTIN`) | India Compliance PAN_RULES; GSTN "Probable" |
| 6. Cross-period | Leftover pools from earlier periods of the same FY | as passes 1–2 | `PERIOD_MISMATCH` | Tally return-period bucket; aerele lookback |
| 7. Residue | Anti-join both sides | n/a | `ONLY_IN_BOOKS` / `ONLY_IN_2B` | all tools |

Pass 3 needs a ranking step. Rank candidates with `row_number() OVER (PARTITION BY pr_id ORDER BY score DESC, abs(amount_diff))`, partition the same way by `g2b_id`, and keep only **mutual-best pairs** (rank 1 on both sides). This avoids India Compliance's first-hit greediness and aerele's worst-score bug.

One-to-one matching is enforced by the schema, not by the loop. `recon_match` has `UNIQUE(pr_id)` and `UNIQUE(g2b_id)`, applied as partial unique indexes on active matches so that delinked history is kept, and every pass uses `ON CONFLICT DO NOTHING`.

Use Levenshtein rather than trigrams for short invoice numbers. pg_trgm discards non-alphanumerics, and very short strings yield few trigrams, so "12" vs "21" behaves poorly. A GIN `gin_trgm_ops` index is still useful for supplier-name search and cross-GSTIN probing ([pg_trgm](https://www.postgresql.org/docs/19/pgtrgm.html); [fuzzystrmatch](https://www.postgresql.org/docs/18/fuzzystrmatch.html)).

### Tolerances, stored per client in `recon_settings`

Tolerances should be columns per client, not constants in code.

- **Rounding tolerance: ₹1 per tax head.** This is the clearest consensus. India Compliance, aerele, neevjain-eve, sangamyo and ebinmichael all use about ₹1. vsinghidelhi uses ₹2.
- **Review tolerance: ₹10.** Below it a difference is a quick accept; above it a human must look. This matches aerele's bulk-accept block, GSTZen's red threshold and the top of GSTN's ₹0–10 tolerance range. neevjain-eve's ₹100 "variance" band is a looser alternative that can be offered as a setting.
- **Date window: 10 days** for fuzzy and no-number passes, following India Compliance.
- **Fuzzy threshold: Levenshtein ≤ 2, or trigram similarity ≥ 0.9.** The 0.9 is borrowed from RapidFuzz's ≥ 90, but `similarity()` is computed on a different scale, so the team should calibrate it on its own synthetic data rather than claim equivalence.
- **Lookback: back to the start of the previous financial year**, as India Compliance does. Expose aerele's 6–12-month alternative as a setting.

**No project documents an empirical justification for ₹1, 90% or 10 days. They are practitioner conventions.** Reconcile can do better: generate synthetic data with labelled perturbations (format changes, typos, rounding, date shifts, wrong GSTINs, 2B-only rows, books-only rows, amendments) and report precision and recall per pass.

## Where Reconcile can beat everything shipped, and what to leave out

The differentiation case rests on doing in PostgreSQL what every existing tool does in application code. Each item below is a syllabus concept that carries real weight in the design.

| # | Differentiator | Existing tools | DBMS concept shown | Effort |
|---|---|---|---|---|
| 1 | One-to-one matching enforced by UNIQUE partial indexes, not loops | India Compliance and aerele enforce it in Python | DDL constraints, `ON CONFLICT` | Easy |
| 2 | Data-driven `match_rule` table executed by `sp_reconcile` | India Compliance hard-codes its rules in Python | Procedures, DML, set-based joins | Medium |
| 3 | Three-axis status with ENUMs and a cross-tool mapping table | Overloaded single status in most repos | Schema design, normalisation | Easy |
| 4 | GSTIN DOMAIN with checksum and FK to `state_code` | No checksum validation seen in the reviewed matchers | Domains, CHECK, IMMUTABLE functions | Easy |
| 5 | Trigger-written append-only `recon_action_log` (who, when, old → new, remark), with UPDATE and DELETE revoked | Only KDK claims an audit trail | Triggers, DCL | Easy |
| 6 | Period freeze: once a period is CLOSED or a row ACCEPTED, a trigger blocks edits unless the role is `partner` | aerele's freezing idea, done in Python | Triggers, roles, GRANT/REVOKE | Easy |
| 7 | Row-level security so article staff see only assigned clients, and client users see only their own firm | Tally and Zoho have no multi-client support; others are opaque | RLS, DCL, multi-tenancy | Medium |
| 8 | Rule 37 ageing view (0–150 / 150–180 / >180 days unpaid) proposing Table 4B reversals and reclaims | Not documented in any product reviewed | Views, date arithmetic, payments join | Easy |
| 9 | `DUPLICATE` detection on the natural key | Not shown as a status in any product doc | Unique and partial indexes, window functions | Easy |
| 10 | Transparent vendor scorecard: % only-in-books, mismatch rate, IMS rejections, filing delay, with the formula visible | ClearTax uses H/M/L tags; KDK scores are unpublished | Materialized view, aggregates | Medium |
| 11 | Vendor follow-up log with generated mismatch letters and status | KDK and TaxOne automate this commercially | One-to-many design, views | Easy |
| 12 | Deadline work queue (14th / 17th / 19th / 20th) and an ITC-at-risk total per client | Practitioner pain point; KDK claims analytics | Views, CTEs | Easy |
| 13 | Measured accuracy: precision and recall per pass on labelled synthetic data | No project publishes accuracy | Analytical SQL | Medium |
| 14 | Naive FULL OUTER JOIN vs multi-pass shown side by side with `EXPLAIN ANALYZE` | Not offered | Query plans, indexes | Easy |

Rule 37 belongs on the list because it is self-contained. If the supplier is not paid within 180 days of the invoice date, the ITC is reversed in GSTR-3B Table 4B. Interest runs under Section 50 only if the ITC has been used, and the credit is reclaimed on payment ([ClearTax Rule 37](https://cleartax.in/s/rule-37-of-cgst-sgst-rules-itc-reversal-180-days)). The same source flags the interest rate (18% or 24%) as unclear, so make it a configurable setting rather than a constant. Rule 37A, where the supplier did not pay its tax by 30 September of the next FY, can only be modelled with a manually entered supplier-3B status.

The scope cut-list follows a single test: **keep anything the database can enforce on imported data, and cut anything that needs external systems, licences or legal certainty the team cannot verify.**

| Cut or stub | Why | Replacement in Reconcile |
|---|---|---|
| Live GSTN/GSP API download of 2B and upload of IMS actions | Needs a GSP licence and credentials; India Compliance needs this plumbing | Import the portal-downloaded JSON/Excel; export an "IMS action plan" CSV; never claim to push to GSTN |
| ISD, IMPG, IMPGSEZ, ECOM sections | Different shapes (flat arrays, port codes), little value for the demo | Keep B2B, B2BA, CDNR (and CDNRA if time allows) |
| Item- and rate-wise matching | API v4 drops the rate-wise break-up | Match document-level totals |
| GSTR-2A, 26AS, GSTR-1 vs 3B reconciliation | Scope creep | Mention as future work |
| OCR of invoices, WhatsApp/SMS/SMTP automation | No DBMS content; unreliable in a lab | Generated letter text plus a follow-up status table |
| ML or LLM matching | Unexplainable to a strict examiner | Deterministic rule table with typo classes |
| Tally/ERP connectors | Format churn | CSV import with a column-mapping table |
| Exact DRC-01C threshold, Table 4 hard-lock behaviour, Sec 16(4) cutoff as fact | Primary sources unconfirmed; secondary sources disagree | Configurable parameters with a "verify on gst.gov.in" note |
| Many-to-many matches (one bill, several invoices) | Breaks the 1:1 guarantee and complicates the procedure | Manual link plus a remark; list as a known limitation |
| PDF client reports, polished dashboards | UI effort with no marks | One summary view rendered as an HTML table |

The cuts address the buildability score of 3/5 that the earlier shortlist gave the ITC Reconciler idea. With 2B limited to three sections and no API work, the remaining effort is mostly DDL, one procedure, a handful of triggers and views, and a synthetic-data generator. Those are exactly what the lab is graded on.

## Answering "doesn't this already exist?" in the viva

The strongest defence concedes the point immediately and moves the argument to architecture, evidence and scope. Four answers are worth rehearsing.

**"Doesn't this already exist?"** "Yes, and we studied it. ClearTax, Tally, Zoho and Masters India sell it, and there are open-source versions: India Compliance for ERPNext, aerele's Reconciler, and a 2026 Prisma/PostgreSQL tracker. We read their source. In every one, the rules that matter live in application code. One-to-one matching, who may change an accepted row, and the audit history are all enforced there, so any script with database access can break them. Reconcile is the same business problem with those guarantees moved into PostgreSQL. A UNIQUE index makes double-matching impossible. A trigger writes the audit log. RLS stops an article clerk from seeing another client's data. A stored procedure runs matching rules that are themselves rows in a table. We are not claiming a new product. We are claiming that the database, not the app, is the source of truth, and we can show you the defects in aerele's matcher that this design rules out: it picks the worst-scoring candidate, it tests a stale variable, and it stores reasons as a comma-joined string."

**"Where did your match categories and tolerances come from?"** "From GSTN's own Offline Matching Tool: seven parameters, Exact/Partial/Probable, ₹0–10 per-head tolerance, ignoring slashes, hyphens and leading zeros. The ₹1 rounding tolerance and 10-day window come from India Compliance's code. Our status table maps each of our codes to ClearTax, Tally, Zoho, India Compliance and GSTN labels. Nobody publishes evidence for these thresholds, so we measured precision and recall per pass on labelled synthetic data. That is something no existing tool reports."

**"What does yours do that the commercial tools don't?"** "Four things we could not find documented in any product. We track Rule 37 180-day payment ageing with proposed reversals. We flag duplicate supplier invoices as a status. We compute a vendor score whose formula is visible in a view definition. And we give staff and client roles enforced by row-level security, which Tally and Zoho Books cannot offer because they have no multi-client support."

**"Why not just use Excel VLOOKUP or a pandas merge?"** "Our baseline is exactly that: one FULL OUTER JOIN on GSTIN plus invoice number, which is what sangamyo's tool does. We show it first, then show the pairs it splits into two 'missing' rows when a date or a slash differs, and how passes 2–5 recover them. `EXPLAIN ANALYZE` on the composite and trigram indexes shows why it stays fast across many clients."

Rehearse one caution too. If asked whether Reconcile files anything, the answer is no. It imports what the portal provides and produces an action plan. IMS and GSTR-3B rules from October 2025 onward are encoded as configurable checks, and some of them are flagged as needing confirmation from GSTN advisories.

## Conclusion

The prior-art search shifts Reconcile's claim from *what* it does to *where* the logic lives. Every serious reconciler converges on the same algorithm: normalise the number, match exactly, relax one key at a time, tolerate ₹1, and send the rest to a human. Every one of them enforces it in Python or JavaScript. That shared blind spot is the project's opening. The features commercial vendors leave undocumented (Rule 37 ageing, duplicates, transparent vendor scoring, staff-level access control) are also the ones that turn naturally into views, constraints, triggers and RLS. The domain's gaps and the course's grading rubric point to the same build.

The second takeaway concerns evidence. No open-source project or vendor justifies its thresholds or reports matching accuracy. A student team that ships a labelled synthetic dataset and a per-pass precision and recall table therefore produces something more rigorous than the market leaders publish, at a cost of about a day's work. A strict professor is far more likely to remember that table than any screen of the UI.
