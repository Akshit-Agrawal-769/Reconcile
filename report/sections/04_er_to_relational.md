# 4. ER to Relational Schema

We converted the ER diagram to tables by following the mapping algorithm in Elmasri and Navathe (Chapter 9) in its own order. Each step below names the rule, then what it produced for us.

## The mapping steps

**Step 1: strong entities.** Each strong entity becomes a table with its simple attributes, and its key becomes the primary key. This gives `firm`, `client`, `gstin_reg`, `app_user` (we avoided the name USER because MariaDB already has a `user` table and a `USER()` function), `party` (the supplier), `invoice`, `gstr2b_entry`, `review` and `followup`. Derived attributes are left out: a GSTIN registration has no `state` column because `LEFT(gstin, 2)` gives it.

**Step 2: weak entities.** A weak entity becomes a table whose key is the owner's key plus the partial key.

- RETURN_PERIOD would have the key (gstin, period). We kept that pair as a `UNIQUE` key and added a short surrogate key, `period_id`, because five other tables point to a period and a single integer is easier to carry around than a 15-character GSTIN plus a month.
- GSTR2B_IMPORT gets `import_id`, with (period_id, version) kept `UNIQUE`.
- INVOICE_LINE keeps the composite key (invoice_id, line_no) as it is: nothing points to a line.
- FILING keeps (gstin, return_type, period).

**Step 3: 1 : 1 relationships.** MATCHES links an invoice and a 2B document and carries two attributes. The book's usual choice is to put the foreign key on the side with total participation, but here neither side is total: an invoice may have no 2B partner and the other way round. So MATCHES becomes its own table, `recon_result`, with both foreign keys, the status and the tax difference. A row for an unmatched invoice has no entry_id and a row for an unmatched 2B document has no invoice_id. The pairs (import_id, invoice_id) and (import_id, entry_id) are declared `UNIQUE`, which MariaDB allows over NULLs, so each document appears at most once per import.

**Step 4: 1 : N relationships.** The key of the "one" side goes into the table on the "many" side as a foreign key. SERVES puts `firm_id` in `client`; REGISTERS puts `client_id` in `gstin_reg`; BUYS and ISSUES put `gstin` and `party_gstin` in `invoice`; LISTS and REPORTS put `import_id` and `party_gstin` in `gstr2b_entry`; PROPOSES puts `proposed_by` and `approved_by` in `review`.

DECIDES is the exception. A decision can be about a document that is only in GSTR-2B, which has no row in `invoice`, so `review` does not point to `invoice` at all. It carries the invoice's natural identity instead, the supplier, document type, normalized number and financial year, with foreign keys to `gstin_reg` and `party`. The same decision then applies whichever side the document turns up on.

**Step 5: M : N relationships.** ASSIGNED becomes `staff_assignment(user_id, client_id)`, whose key is the pair.

**Step 6: multivalued attributes.** The usual rule makes a new table for a multivalued attribute. Tax is the exception we allowed: the four heads are fixed by GST law, every document has all four (most are zero), and the CHECK constraints in BR3 need to compare them within one row. So `invoice_line` and `gstr2b_entry` have four columns, `igst`, `cgst`, `sgst` and `cess`.

**Step 7: n-ary relationships.** CHASES is really three-way: a follow-up is about one supplier, in one client's month, sent by one user. So `followup` carries `party_gstin`, `period_id` and `sent_by`.

**Who did it.** Several foreign keys record which user did something: `imported_by` in `invoice` and `gstr2b_import`, `sent_by`, `client_approved_by`, and `app_user.client_id` for a client's own login. Each is a 1 : N relationship between USER and that entity. We left them off the diagram because they would add a dozen lines to USER without saying anything new about the data.

**Lookup tables.** Two tables do not come from the ER diagram but from the rules. `period_transition` lists the allowed moves of a month's state and who may make each one (BR9). `return_calendar` holds the due date for each return and month; Section 5 shows why it had to be a table of its own.

## The resulting schema

{{fig:schema|Relational schema. Underlined columns are primary keys; italic columns are derived and kept only for an index; arrows run from the referenced table to the referencing one.|16}}

{{table:schema_list}}

`books_staging` is not in the list. It holds the purchase register exactly as the CSV arrives, as text, so that `sp_import_books` can validate it before moving it into `invoice` and `invoice_line`. It is a load buffer that is emptied after every import, not part of the data model, so we keep it out of the normalization that follows.
