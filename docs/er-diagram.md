# ER Diagram (v2: the middle version)

v2 replaces the 29-entity draft. It starts from the simple ITC Guard model (17 entities) and adds back only the pieces that fix a real problem in the user flow. The result is **21 entities in 4 groups**. Everything else is a view, a column or a trigger.

## The 60-second explanation for the professor

1. **Who:** a CA firm has users and clients. Each client has one or more GSTINs, and each GSTIN has a taxpayer type that decides what it may do. Composition dealers get no ITC. Staff see only the clients assigned to them.
2. **What the client recorded:** invoices with lines. Each line has an HSN/SAC code, and rates are looked up by date, never hard-coded. Suppliers are parties. Payments are recorded against invoices.
3. **What the government shows:** every GSTR-2B upload is a numbered version of that month. Each run of the matcher compares the books with one version and writes results.
4. **What people decided:** a review belongs to a **real invoice** (supplier, type, number, year), not to a row in one upload. Re-uploading 2B or moving to next month never loses work. Filings and follow-ups track deadlines and suppliers.

## Diagram

```mermaid
erDiagram
  FIRM ||--o{ APP_USER : employs
  FIRM ||--o{ CLIENT : manages
  APP_USER ||--o{ STAFF_ASSIGNMENT : "is assigned"
  CLIENT ||--o{ STAFF_ASSIGNMENT : "is handled by"
  CLIENT ||--|{ GSTIN_REG : holds
  TAXPAYER_TYPE ||--o{ GSTIN_REG : "classifies"

  GSTIN_REG ||--o{ INVOICE : records
  PARTY ||--o{ INVOICE : "is counterparty"
  INVOICE ||--|{ INVOICE_LINE : contains
  INVOICE ||--o| INVOICE : "amends or credit note of"
  HSN_SAC ||--o{ INVOICE_LINE : classifies
  HSN_SAC ||--|{ TAX_RATE : "has rates over time"
  INVOICE ||--o{ PAYMENT : "paid by"

  GSTIN_REG ||--o{ RETURN_PERIOD : "reconciled per month"
  RETURN_PERIOD ||--o{ GSTR2B_IMPORT : "has versions"
  GSTR2B_IMPORT ||--o{ GSTR2B_ENTRY : contains
  PARTY ||--o{ GSTR2B_ENTRY : "reported by"
  GSTR2B_IMPORT ||--o{ RECON_RESULT : produces
  INVOICE |o--o{ RECON_RESULT : "books side"
  GSTR2B_ENTRY |o--o{ RECON_RESULT : "portal side"
  GSTIN_REG ||--o{ REVIEW : "decisions for"
  PARTY ||--o{ REVIEW : "about invoices of"
  APP_USER ||--o{ REVIEW : "proposes or approves"

  TAXPAYER_TYPE ||--|{ FILING_RULE : "must file"
  GSTIN_REG ||--o{ FILING : files
  FILING_RULE ||--o{ FILING : generates
  GSTIN_REG ||--o{ FOLLOWUP : chases
  PARTY ||--o{ FOLLOWUP : "is chased"
  APP_USER ||--o{ AUDIT_LOG : performs

  FIRM {
    int firm_id PK
    string name
  }
  APP_USER {
    int user_id PK
    int firm_id FK
    int client_id FK "only for client logins"
    string email UK
    string role "partner, staff, client"
    string password_hash "never GST portal passwords"
  }
  STAFF_ASSIGNMENT {
    int user_id PK,FK
    int client_id PK,FK
  }
  CLIENT {
    int client_id PK
    int firm_id FK
    string legal_name
    char10 pan UK
    decimal rounding_tol "default 1.00"
    decimal approval_threshold "partner approves above this"
  }
  TAXPAYER_TYPE {
    string code PK "REGULAR, COMPOSITION, SERVICE"
    bool itc_allowed "false for COMPOSITION"
  }
  GSTIN_REG {
    char15 gstin PK "checksum domain"
    int client_id FK
    string taxpayer_type FK
    string filing_freq "MONTHLY, QRMP"
    char2 state_code
    date reg_date
  }
  PARTY {
    char15 gstin PK "supplier or customer"
    string name
    string filing_freq
  }
  INVOICE {
    int invoice_id PK
    char15 gstin FK "our client"
    char15 party_gstin FK
    int ref_invoice_id FK "original, for CN or amendment"
    string direction "INWARD, OUTWARD"
    string doc_type "INV, CRN, DBN"
    string invoice_no
    string invoice_no_norm "generated"
    date invoice_date
    string fin_year "generated, partition key"
    char2 place_of_supply
    bool is_rcm
  }
  INVOICE_LINE {
    int line_id PK
    int invoice_id FK
    string hsn_sac FK
    decimal taxable_value
    decimal rate
    decimal igst
    decimal cgst
    decimal sgst
    string itc_category "ELIGIBLE, BLOCKED_17_5"
  }
  HSN_SAC {
    string code PK
    string description
    bool is_service
  }
  TAX_RATE {
    string code PK,FK
    date effective_from PK
    date effective_to
    decimal rate
  }
  PAYMENT {
    int payment_id PK
    int invoice_id FK
    date paid_on
    decimal amount
  }
  RETURN_PERIOD {
    int period_id PK
    char15 gstin FK
    char6 period "MMYYYY"
    string fin_year
    string state "OPEN, IN_REVIEW, AWAITING_APPROVAL, CLOSED"
    int client_approved_by FK
    timestamp client_approved_at
    string override_reason
  }
  GSTR2B_IMPORT {
    int import_id PK
    int period_id FK
    int version "1, 2, 3"
    string source "IMS, DRAFT_2B, RECOMPUTED_2B"
    bool is_current "only one per period"
    int imported_by FK
    timestamp imported_at
  }
  GSTR2B_ENTRY {
    int entry_id PK
    int import_id FK
    char15 party_gstin FK
    string doc_type
    string invoice_no
    string invoice_no_norm "generated"
    date invoice_date
    decimal taxable_value
    decimal igst
    decimal cgst
    decimal sgst
    string ims_status "seen on portal"
  }
  RECON_RESULT {
    int result_id PK
    int import_id FK
    int invoice_id FK "null if only in 2B"
    int entry_id FK "null if only in books"
    string status "EXACT, ROUNDING, PROBABLE, MISMATCH, ONLY_IN_BOOKS, ONLY_IN_2B, DUPLICATE"
    string matched_by "which rule fired"
    decimal diff_tax
  }
  REVIEW {
    int review_id PK
    char15 gstin FK
    char15 party_gstin FK
    string doc_type
    string invoice_no_norm
    string fin_year "UK: these five columns"
    string decision "ACCEPT, CHASE, DEFER, PARTIAL, INELIGIBLE"
    string ims_action "ACCEPT, REJECT, PENDING"
    string state "PROPOSED, APPROVED, DONE"
    int proposed_by FK
    int approved_by FK "must differ from proposed_by"
    string fingerprint "amounts when reviewed"
    string remark
  }
  FILING_RULE {
    string taxpayer_type PK,FK
    string return_type PK "GSTR1, GSTR3B, CMP08, GSTR4"
    string frequency
    int due_day
  }
  FILING {
    int filing_id PK
    char15 gstin FK
    string return_type FK
    char6 period
    date due_date
    date filed_on
    string status
    decimal late_fee
  }
  FOLLOWUP {
    int followup_id PK
    char15 gstin FK
    char15 party_gstin FK
    int period_id FK
    string channel
    date sent_on
    string response
    date next_chase_on
  }
  AUDIT_LOG {
    bigint log_id PK
    int user_id FK
    string table_name
    bigint row_id
    string action
    json old_value
    json new_value
    timestamp changed_at
  }
```

## What v2 adds to the simple model, and why

These are the 6 additions. Each one fixes a problem the simple model can't handle.

| Added | Problem it fixes | One line for the viva |
|---|---|---|
| `STAFF_ASSIGNMENT` | The simple model has no way to restrict staff | "Row-level security reads this table: staff only see assigned clients." |
| `TAXPAYER_TYPE` | The type was a free-text string, so rules couldn't reference it | "`itc_allowed = false` for composition, and a trigger blocks ITC using it." |
| `GSTR2B_IMPORT` | Re-uploading 2B after the 14th would overwrite or duplicate entries | "Every upload is a version. One is current, enforced by a partial unique index." |
| `REVIEW` | The simple `RECON_RESULT.status` mixes the machine's verdict with the human decision, and is lost on re-run | "Results are rebuilt each run. Reviews are keyed by the real invoice, so they survive re-uploads and carry into next month automatically." |
| `FILING_RULE` | The calendar had no rules about who files what | "Composition files CMP-08 and GSTR-4, regular files GSTR-1 and 3B. Filings are generated from this table." |
| `FOLLOWUP` | Supplier chasing left no evidence | "Who chased which supplier, when, how, and what they replied." |

## What v2 removes from the simple model, and why

| Removed | Replaced by | Reason |
|---|---|---|
| `ITC_LEDGER` table | View `v_itc_ledger` | Available, claimed and blocked amounts can be computed from invoices and reviews. Storing them creates update anomalies. **"We don't store what we can derive."** |
| `ALERT` table | View `v_alerts` | Alerts come from due dates and open reviews. |
| `PARTY.filing_score` | Materialized view `mv_party_score` | It's a derived aggregate, refreshed on demand. |
| `RECON_RESULT.match_score` | `matched_by` (rule name) | A rule name is explainable. A made-up score isn't. |

## What the 29-entity draft had that v2 folds away

| v1 entity | Where it went in v2 |
|---|---|
| `invoice_identity` | Gone. `REVIEW` uses the five natural-key columns directly. |
| `item_decision` + `carried_from` chain | Gone. One `REVIEW` row per real invoice lives across months, so carry-forward needs no extra table. |
| `client_settings` | Columns on `CLIENT` |
| `client_user` | `APP_USER.client_id` |
| `client_approval` | Columns on `RETURN_PERIOD` |
| `period_transition` | A trigger with the allowed moves in a CASE statement |
| `match_rule`, `match_difference` | `RECON_RESULT.matched_by` and `diff_tax` |
| `followup_item`, `job_queue`, `state_code` | Out of the ER: generated letter text, infrastructure, and a CHECK constraint respectively |
| `pr_document` + `sales_summary` | One `INVOICE` table with `direction`, which makes liability possible later |

## Rules the database enforces

- **Composition dealers get no ITC.** A trigger rejects an `INWARD` invoice line with `itc_category = ELIGIBLE` when the GSTIN's `taxpayer_type.itc_allowed = false`. It also refuses reconciliation for that GSTIN.
- **Place of supply.** A CHECK constraint enforces: same state as the GSTIN means CGST+SGST, otherwise IGST.
- **Rates by date.** The line's rate must equal the `TAX_RATE` row valid on `invoice_date`. A mismatch shows as a warning.
- **One-to-one matching.** `UNIQUE (import_id, invoice_id)` and `UNIQUE (import_id, entry_id)`.
- **One current upload.** A partial unique index on `GSTR2B_IMPORT (period_id) WHERE is_current`.
- **Maker-checker.** `CHECK (approved_by <> proposed_by)`. Approval is required above `CLIENT.approval_threshold`.
- **Month states.** `OPEN → IN_REVIEW → AWAITING_APPROVAL → CLOSED`, plus a partner-only reopen. A trigger blocks a close without client approval or an override reason.
- **Changed since review.** A view compares `REVIEW.fingerprint` with the current result's amounts. Nothing extra is stored.
- **Append-only audit.** No role can UPDATE or DELETE `AUDIT_LOG`.
- **Partitioning.** `INVOICE`, `INVOICE_LINE`, `GSTR2B_ENTRY` and `AUDIT_LOG` are partitioned by `fin_year`.

## State machines

```mermaid
stateDiagram-v2
  [*] --> OPEN
  OPEN --> IN_REVIEW: first upload
  IN_REVIEW --> AWAITING_APPROVAL: staff submit
  AWAITING_APPROVAL --> IN_REVIEW: partner sends back, or newer upload changes items
  AWAITING_APPROVAL --> CLOSED: partner closes after client approval
  CLOSED --> IN_REVIEW: partner reopens
```

```mermaid
stateDiagram-v2
  [*] --> PROPOSED: staff decide
  PROPOSED --> APPROVED: auto below threshold, else partner (not the proposer)
  APPROVED --> DONE: action taken on portal
  APPROVED --> PROPOSED: amounts changed in a newer upload
  DONE --> [*]
```
