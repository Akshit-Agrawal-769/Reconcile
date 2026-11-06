# 6. Lossless Decomposition Proof

Splitting a table is only safe if joining the pieces back gives exactly the original rows, no more and no fewer. A decomposition that adds rows on the join (spurious tuples) is called lossy. We prove our decompositions lossless in two ways: one step at a time with the binary test, and for the whole decomposition at once with the chase (tableau) test.

## The binary test, step by step

For a split of R into R1 and R2, the join is lossless if the common attributes R1 ∩ R2 functionally determine all of R1 or all of R2. In words: the shared columns must be a key of at least one side, so each row on the other side finds at most one partner.

{{table:a_binary}}

Every step passes, and a sequence of lossless binary steps is lossless as a whole, so example A's six relations join back to PURCHASE_LINE exactly.

To see why the condition matters, try a split that fails it. Put the supplier's name with the invoice line instead, as LINE_A(supplier_gstin, invoice_no, line_no, …) and NAME_A(supplier_name, invoice_no). The shared attribute invoice_no is not a key of either side: two suppliers can both use INV-1. Joining the pieces would pair every line of Kaveri Foods' INV-1 with Yamuna Steel's name as well, producing rows that were never in the sheet.

## The chase test for the whole decomposition

The chase checks all six relations at once. We write the 14 attributes as columns A to N:

{{table:a_letters}}

The tableau has one row per relation. A row gets the symbol *a* in each column its relation contains and a row-specific *b* (b1 for the first relation, b2 for the second, and so on) everywhere else. Then we apply each dependency again and again: when two rows agree on its left side, we make them agree on its right side, preferring an *a*. If some row becomes all *a*, the decomposition is lossless. For readability the table shows only the symbol in each cell, not the column subscript.

**Before the chase:**

{{table:a_chase_start}}

**After the chase:**

{{table:a_chase_end}}

The last row, INVOICE_LINE, starts with *a* in columns A, D, F, I, J, K, L and M. FD1 (A → B) copies *a* into B from row 1, FD2 (B → C) then gives C, FD3 (D → E) gives E from row 3, FD4 (ADF → G, N) gives G and N from row 4, and FD5 (G → H) gives H from row 5. The row is now all *a*, so the decomposition is lossless.

Example B passes the same tests: STAFF and CLIENT_STAFF share staff_email, which is the key of STAFF.
