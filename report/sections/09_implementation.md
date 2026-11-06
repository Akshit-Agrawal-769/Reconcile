# 9. Implementation on XAMPP and Python

## Setup

The implementation runs on XAMPP 8.2 for Windows, which ships MariaDB 10.4, and Python 3.10 or newer with the `mysql-connector-python` driver. Everything lives in the `xampp/` folder of the repository:

| File | What it holds |
|---|---|
| `schema.sql` | the 18 tables and the staging table, with keys, foreign keys and CHECK constraints |
| `logic.sql` | 8 functions, 15 procedures, 14 triggers, 4 views and 3 roles |
| `seed.sql` | the demo firm, three clients, four logins and the 2026-27 due dates |
| `reconcile_cli.py` | a command-line program for working a month |
| `check.py` | an end-to-end test of every rule |

After starting MySQL from the XAMPP Control Panel, the three SQL files can be imported in phpMyAdmin in order, or loaded in one go with `python xampp/reconcile_cli.py init`. The SQL is written for MariaDB 10.4 specifically. It uses no `JSON_TABLE` (added in 10.6) and no `INSERT … RETURNING` (10.5). It wraps every routine in `DELIMITER $$` so that phpMyAdmin and the command-line client read it the same way.

{{box:Screenshot: phpMyAdmin showing the reconcile database and its tables}}

## Constraints in the tables

Most of the business rules from Section 2 are plain constraints. BR3 (IGST or CGST + SGST, never both) and the rule that a partial claim needs a percentage are CHECK constraints, which MariaDB enforces from version 10.2:

```sql
CONSTRAINT invoice_line_heads CHECK (igst = 0 OR (cgst = 0 AND sgst = 0)),
CONSTRAINT invoice_line_rate  CHECK (rate IN (0, 0.1, 0.25, 1.5, 3, 5, 6, 12, 18, 28, 40)),
CONSTRAINT review_partial     CHECK ((decision = 'PARTIAL') = (claim_pct IS NOT NULL)),
```

BR2, one invoice per supplier per number per year, is the unique key `invoice_once (gstin, party_gstin, doc_type, invoice_no_norm, fin_year)`. Here `fin_year` is a generated column computed from the invoice date, and `invoice_no_norm` is filled by a trigger that calls `norm_doc_no`. That function turns "inv/0104/26-27" into "INV104", so a reformatted number cannot slip past the key.

## Rules that need triggers

Rules that look at other rows or other tables need triggers. Maker-checker (BR6) is four lines:

```sql
CREATE TRIGGER review_maker_checker BEFORE UPDATE ON review FOR EACH ROW
BEGIN
    IF NEW.approved_by IS NOT NULL AND NEW.approved_by = NEW.proposed_by THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'maker cannot approve own review';
    END IF;
END$$
```

The other triggers do the following:

- **Freezing a closed month (BR8).** Before any insert, update or delete on `invoice`, a trigger checks whether that invoice's month is closed for that GSTIN.
- **The month's state machine (BR9).** A trigger checks every change of state against `period_transition`.
- **The GSTIN check digit (BR1).** Triggers verify it on `gstin_reg` and `party`, and also check that the PAN inside a client's GSTIN matches the client.
- **An append-only audit log (BR10).** Two triggers refuse every UPDATE and DELETE on it.

## Procedures as the only door

MariaDB has no row-level security, so the tenancy rule (BR5) is checked inside stored procedures. The three application roles, `rc_partner`, `rc_staff` and `rc_client`, get EXECUTE on procedures and nothing else: no INSERT, UPDATE or DELETE on any data table, and no direct SELECT on the views either, which they read through `sp_queue` and `sp_totals`. Every procedure takes the acting user's id first. Those that touch a client's month call `guard_period`, which raises "not your client" unless the user is a partner of the client's firm, a staff member assigned to the client, or the client's own login; `sp_approve` checks the same thing through the review's GSTIN. Closing a month shows how the rules stack:

```sql
CREATE PROCEDURE sp_close_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
BEGIN
    CALL require_role(p_user, 'partner');
    CALL guard_period(p_user, p_period, FALSE);
    IF (SELECT client_approved_at IS NULL AND override_reason IS NULL
          FROM return_period WHERE period_id = p_period) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'client approval missing';
    END IF;
    IF pending_reviews(p_period) > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reviews awaiting approval';
    END IF;
    CALL move_period(p_user, p_period, 'CLOSED');
    UPDATE return_period SET closed_at = NOW() WHERE period_id = p_period;
END$$
```

## The matcher

`sp_reconcile` runs six passes. Each pass is one INSERT … SELECT into `recon_result`. A `NOT EXISTS` against rows already matched makes sure no invoice or 2B document is matched twice, and `ROW_NUMBER()` picks one partner when several qualify.

| Pass | Status | Condition |
|---|---|---|
| 0 | Duplicate | the same document appears twice in one GSTR-2B; all but the first |
| 1 | Exact | same supplier, document type, normalized number and financial year; same date; all four tax heads equal |
| 2 | Rounding | as exact, but dates within the client's window (10 days) and each head within ₹1 |
| 3 | Value mismatch | same supplier, type, number and year, but some head differs by more than ₹1 |
| 4 | Only in books | an invoice of the month that nothing matched |
| 5 | Only in 2B | a 2B document that nothing matched |

The rules above decide the order. A pair with a 40-paise difference must be caught as rounding before the value pass sees it, and only what is left after every pass counts as missing.

## Python

Python handles the parts SQL is poor at. MariaDB 10.4 has JSON functions but no `JSON_TABLE`, so turning GSTR-2B's nested arrays into rows would need a loop in SQL; Python reads the JSON in a few lines and calls `sp_add_2b_entry` once per document. It also reads the purchase register CSV into `books_staging` for `sp_import_books` to validate. One month is loaded with:

```text
python xampp/reconcile_cli.py load-month --as staff.a@demo.test --gstin 07AAPFU0939F1ZX \
       --period 092026 --books books.csv --gstr2b gstr2b.json
python xampp/reconcile_cli.py queue --as staff.a@demo.test --period-id 1
python xampp/reconcile_cli.py decide --as staff.a@demo.test --result 31 --decision CHASE --ims PENDING
python xampp/reconcile_cli.py approve --as partner@demo.test --review 1
```

## Testing

`check.py` builds a throwaway database, loads one generated month whose correct answer is known, and then tries to break each rule:

- a staff member opening a month for someone else's client;
- a partner approving their own decision, and staff approving at all;
- closing before the client approves, and closing with a decision still pending;
- editing an invoice after the month is closed, and jumping a closed month back to open;
- deleting from the audit log.

Each attempt must be refused with the expected message, and each line prints `[ok]` or `[FAIL]`. It also compares the SQL versions of the check-digit, number-normalizing and financial-year functions with Python copies of the same rules.

{{box:Screenshot: output of python xampp/check.py ending in ALL CHECKS PASSED}}

The check is built so that, on the generated month of 58 invoices, every document the six passes can classify has to land in the category the generator expects, or the step fails. Five documents need passes only the PostgreSQL build has: four one-character typos and one supplier invoicing from a second state. The XAMPP build reports each as one invoice only in the books and one only in 2B, and the check counts them as skipped. The PostgreSQL build, run on the same generator at 500 documents per client, put 1,020 out of 1,020 documents in the right category.

## A month in numbers

To show what the queue looks like in use, here is the top of the exception queue for Delta Distributors' August 2026 demo month, taken from the PostgreSQL build (the XAMPP views `v_queue` and `v_period_totals` give the same columns). It holds 61 items. Mismatches come first and the largest tax difference is at the top. Staff A has already taken four decisions, and three of them are waiting for a partner because they are above the ₹5,000 threshold.

| Status | Supplier GSTIN | Invoice | Tax difference (₹) | Decision |
|---|---|---|---|---|
| Only in books | 07KXOFJ9977C1Z6 | INV-11111405 | 59,719.60 | Chase supplier (awaiting partner) |
| Only in books | 06PBGCY4709E1ZV | INV-11116719 | 27,016.90 | Chase supplier (awaiting partner) |
| Only in books | 06NRICW6804L1ZL | INV-11115120 | 21,048.16 | not reviewed |
| Only in 2B | 07KXOFJ9977C1Z6 | INV-11114567 | −20,574.08 | Ineligible (awaiting partner) |
| Only in 2B | 07KXOFJ9977C1Z6 | INV-11114515 | −4,113.86 | not reviewed |
| Value mismatch | 06PBGCY4709E1ZV | INV-11112749 | 369.33 | Accept |
| Value mismatch | 08HZFFW3999C1Z5 | INV-11113928 | −75.94 | not reviewed |
| Rounding | 07RWCCS0976T1ZU | INV-11113991 | −0.61 | none needed |

The month's totals were ₹17,03,373.48 of ITC in the books, ₹16,20,045.29 in GSTR-2B and ₹1,08,153.99 at risk, with 89.7% of the books invoices matched. The first row is the sample invoice from Section 5: the supplier never uploaded it, so ₹59,719.60 of credit waits until they do. The negative differences are invoices the portal shows but the books do not; the one marked ineligible is a purchase the client says it never made, so the action on the portal's IMS is to reject it.

## Limitations

- The XAMPP build matches on exact keys only. A typo in the invoice number ("INV-1104" against "INV-1194") shows up as one invoice only in the books and one only in 2B; the PostgreSQL build pairs them by edit distance.
- Passwords are hashed with salted SHA-256 because XAMPP's MariaDB has no bcrypt. That is acceptable for a lab demo but not for a real firm.
- Tenancy is enforced inside procedures, and a procedure trusts the user id it is given. Anyone who can call the procedures can claim to be another user. A real deployment would give every user their own database account and have the procedures read `USER()`; the PostgreSQL build closes the gap with row-level security tied to the session.
- An invoice dated 30 September that the client enters in October's books is matched only within September, because the matcher looks at invoices dated in the month. If September is already closed, the trigger that freezes closed months refuses it, and the whole October import stops with that message. The PostgreSQL build's cross-period pass handles the first case.
- The four tax heads are fixed columns. If GST law added a fifth levy the schema would change, which a separate tax-head table would have avoided at the cost of the row-level CHECK constraints.
- We tested with generated data. Real purchase registers will have combinations the generator does not produce, such as a typo and a date shift on the same invoice.

## What the full build adds

The PostgreSQL version keeps the same tables and rules and adds:

- row-level security, so the tenancy check is applied to every query and not only inside procedures;
- an invoice table partitioned by financial year;
- three more matching passes, which pair near-miss numbers by edit distance, a supplier's other-state GSTIN by PAN, and invoices that reach GSTR-2B a month late;
- a job queue claimed with `FOR UPDATE SKIP LOCKED`, so uploads are processed in the background;
- a React web interface;
- 304 automated tests.

The XAMPP version covers every table, normalization step and business rule this report discusses. What it gives up is listed above: row-level security, partitioning, three matching passes and the background queue.
