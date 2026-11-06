# 8. Minimal Cover of Each Table

A minimal (canonical) cover of a set of dependencies F is an equivalent set Fc with three properties: every right side is a single attribute, no left side has an attribute it can do without, and no dependency can be dropped without changing the closure. We get one in three passes:

- split every right side into single attributes;
- for each dependency X → A and each attribute B in X, remove B if A is still in the closure of X − B;
- remove any dependency X → A whose A is in the closure of X using only the others.

## One table by hand: client

`client` has the attributes client_id, firm_id, name, pan, approval_threshold, rounding_tol and date_window_days. The dependencies we start from are client_id → firm_id, name, pan, approval_threshold, rounding_tol, date_window_days (the primary key), pan → client_id (a PAN identifies one client), and, because we listed it when first writing the table, pan → name.

- **Split right sides.** client_id → firm_id, client_id → name, client_id → pan, client_id → approval_threshold, client_id → rounding_tol, client_id → date_window_days, pan → client_id, pan → name.
- **Left sides.** Every left side is a single attribute, so there is nothing to remove.
- **Redundant dependencies.** pan → name follows from pan → client_id and client_id → name, so it goes. Nothing else can go: dropping pan → client_id would lose the fact that PAN is a key, and dropping any client_id → X would leave X unreachable.

That leaves seven dependencies. Both keys of the table, client_id and pan, are on left sides, and every left side is a superkey, so `client` is in BCNF.

## All tables

The table below was produced by running the same three passes in `report/fd.py` over the dependencies of every table. For tables with a single key, the minimal cover is just "key → each other column", which we write once with the columns listed together to save space. The *Keys* column lists every candidate key. Columns marked as derived are the index columns explained at the end of Section 5; they are not part of the relation being tested.

{{table:final_cover}}

Every table is in BCNF: in each of them, the left side of every dependency in the minimal cover is a candidate key.

Two tables need a note. First, `recon_result` lists only result_id as a key. Its two `UNIQUE` constraints, (import_id, invoice_id) and (import_id, entry_id), contain columns that are NULL for unmatched documents, and the textbook definitions of dependency and key assume no NULLs. Splitting the table into matched pairs, books-only rows and 2B-only rows would remove the NULLs; we kept one table because the queue always reads the three together. Second, `invoice_line` stores CGST and SGST separately even though the law splits intra-state tax in half. They are not forced equal, because the halves can differ by a paisa after rounding, so neither column determines the other.
