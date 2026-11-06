# 5. Normalization up to BCNF

Our tables came out of the ER mapping already close to BCNF, so walking through them one by one would not show much. Instead we start from what a CA firm really works with today, the purchase register sheet in Excel, and normalize it step by step. Its final relations are the core of our schema. A second, shorter example shows the one situation where 3NF and BCNF give different answers. Section 8 applies the same tests to every one of our 18 tables.

The tests we use at each step are the textbook ones. A relation is in **1NF** when every value is atomic. It is in **2NF** when, in addition, no non-prime attribute depends on only part of a candidate key. It is in **3NF** when, for every dependency X → A, either X is a superkey or A is part of some candidate key. It is in **BCNF** when, for every non-trivial dependency X → A, X is a superkey. We worked each step by hand and then checked every key, closure and normal form with a small Python program (`report/fd.py`), which the tables below are printed from.

## Example A: the purchase register sheet

A typical sheet has one row per invoice, with the invoice's lines squeezed into repeating columns or merged cells, and the CA types the GSTR-2B tax next to each invoice by hand:

| Client GSTIN | Client PAN | Client | Supplier GSTIN | Supplier | Invoice | Date | Month | Lines (HSN, rate, taxable, tax) | 2B tax |
|---|---|---|---|---|---|---|---|---|---|
| 07EMUFB1186R1ZS | EMUFB1186R | Delta Distributors | 07KXOFJ9977C1Z6 | Kaveri Foods | INV-11111405 | 14-08-2026 | 082026 | (8471, 18, 2,10,000.00, 37,800.00); (8471, 18, 1,21,775.56, 21,919.60) | 0.00 |
| 07EMUFB1186R1ZS | EMUFB1186R | Delta Distributors | 06PBGCY4709E1ZV | Yamuna Steel | INV-11112749 | 22-08-2026 | 082026 | (8471, 18, 50,000.00, 9,000.00) | 8,630.67 |

**Unnormalized form.** The lines column holds a repeating group, so the sheet is not even in 1NF. A formula cannot add up the taxable values of one invoice, and nothing stops two rows from giving the same invoice different dates.

**1NF.** We flatten the repeating group so that each row is one invoice line. The relation now has 14 atomic attributes:

PURCHASE_LINE(client_gstin, client_pan, client_name, supplier_gstin, supplier_name, invoice_no, invoice_date, return_period, line_no, hsn, rate, taxable_value, tax_amount, portal_tax)

The functional dependencies we found by asking what determines what:

{{table:a_fds}}

FD4 assumes one sheet covers one financial year, which is how firms keep them; the real schema adds the financial year to the invoice key. In FD5, return_period means the calendar month of the invoice date, the month the invoice belongs to in the books. (When a late invoice reaches GSTR-2B a month later, as in Section 1, that is a fact about the portal side, not about this sheet.) The only candidate key is {client_gstin, supplier_gstin, invoice_no, line_no}: no attribute outside it determines any of its members, and its closure is the whole relation. So the prime attributes are those four and every other attribute is non-prime.

**2NF.** Three dependencies have a left side that is only part of the key: FD1 (client_gstin), FD3 (supplier_gstin) and FD4 (client_gstin, supplier_gstin, invoice_no). These are partial dependencies, and they are why the sheet repeats the client's name on every line of every invoice. We split out each left side together with everything it determines:

{{table:a_2nf}}

**3NF.** Two of these relations still have a transitive dependency, a non-key attribute determining another non-key attribute. In REGISTRATION, client_gstin → client_pan → client_name: a client with GSTINs in Delhi and Haryana would have its name stored twice. In INVOICE_HDR, invoice → invoice_date → return_period. We split both:

{{table:a_3nf}}

MONTH_OF deserves a remark. It only records that 14-08-2026 falls in 082026, which a function can compute. In the real schema we do not store it at all; the matcher works out a month from the date with `DATE_FORMAT(invoice_date, '%m%Y')`. Normalization pointed at the problem (the month should not be typed next to each invoice) and the fix was to stop storing it.

**BCNF.** In each of the six relations, the left side of every dependency is a key of that relation, so all six are in BCNF and example A stops here.

{{fig:decomposition|Example A from the Excel sheet to BCNF. Each arrow is one decomposition step.|16}}

The six relations map directly onto our tables: GSTIN_REG and CLIENT are `gstin_reg` and `client`, SUPPLIER is `party`, INVOICE and INVOICE_LINE are `invoice` and `invoice_line`. In the real schema, portal_tax moves to `gstr2b_entry`, because the portal is a separate source with its own documents.

## Example B: when 3NF is not BCNF

Some larger firms split return work by type: one person prepares every client's GSTR-1 and another prepares GSTR-3B. Suppose we had to record that. Each client has one person for each return type, and each person works on only one return type:

FILING_DESK(client_gstin, return_type, staff_email, staff_phone)

{{table:b_fds}}

The candidate keys are {client_gstin, return_type} and {client_gstin, staff_email}, since knowing the client and the person also tells you the return type. staff_phone depends on staff_email, which is only part of the second key, so FILING_DESK is not even in 2NF. Splitting off STAFF(staff_email, staff_phone) fixes that and leaves:

{{table:b_stages}}

ASSIGNMENT is in 3NF: in staff_email → return_type the right side is prime. It is not in BCNF, because staff_email is not a superkey of ASSIGNMENT. The BCNF decomposition moves return_type next to the person, giving STAFF(staff_email, return_type, staff_phone) and CLIENT_STAFF(client_gstin, staff_email). Both are in BCNF, and Section 6 shows the join is lossless, but Section 7 shows that something is lost: no single table any longer contains client_gstin, return_type and staff_email together, so the database can no longer check by a key alone that a client has only one GSTR-1 person.

Our firm's assignments do not work this way (`staff_assignment` is just user × client), so this design is not in the build. We include it because it shows what BCNF can cost.

## What normalization changed in our own tables

Running the same tests on our first draft of the schema caught five problems, and the fixes are in the tables Section 4 lists:

- `followup` had a `gstin` column next to `period_id`. But period_id → gstin through the period, so the client's GSTIN was stored twice. We dropped it.
- `recon_result` had a `matched_by` column naming the pass. With our six passes, status → matched_by (every status comes from exactly one pass), so matched_by was a transitive dependency. We dropped it. The PostgreSQL build keeps it, because there two different passes can both produce the same status.
- `gstr2b_entry` carried the supplier's trade name from the JSON. party_gstin → party_name, so the name belongs in `party`.
- `gstr2b_import` had an `is_current` flag. Whether an import is the latest depends on the other rows of the table, which is not a dependency within the row at all, and a flag like that can disagree with the data. The view `v_current_import` computes it instead.
- `filing` had a `due_date` column. For a monthly filer, (return_type, period) → due_date: GSTR-3B for September is due on 20 October whoever files it. That is a partial dependency on the key (gstin, return_type, period), so the due dates moved into `return_calendar`. Quarterly (QRMP) filers have different dates that also depend on their state; our port covers monthly filers, and covering both would add the filing frequency and the state group to the calendar's key.

Two columns we kept on purpose. `invoice` and `gstr2b_entry` store `invoice_no_norm` (the number with punctuation, leading zeros and financial-year segments removed) and `fin_year` (from the date). Each is a function of another column in the same row, so strictly they are redundant. In `invoice` we keep them because the rule "one invoice per supplier per number per financial year" (BR2) is enforced with a `UNIQUE` index on them; in `gstr2b_entry` they make up the index the matcher joins on. `fin_year` is a generated column. `invoice_no_norm` has to be filled by a trigger, because the normalising function is a stored function and MariaDB does not allow those inside a generated column. Either way users never write them, so they cannot disagree with their source. We treat them as part of the index, not as attributes of the relation, and Section 8 lists them separately.
