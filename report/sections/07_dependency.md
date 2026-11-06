# 7. Dependency Preservation

Losslessness says the data survives the split. Dependency preservation says the rules survive too: every dependency of the original relation can still be checked without joining tables back together. Formally, if F is the set of dependencies and F1, …, Fn are its projections onto the decomposed relations, the decomposition preserves dependencies when the closure of F1 ∪ … ∪ Fn equals the closure of F. In practice we check each dependency X → Y: start from X and keep adding whatever any one relation can derive from what we already have; if Y appears, the dependency is preserved.

## Example A: everything is preserved

Each of the six dependencies has all its attributes inside one of the final relations, so each can be checked inside that relation by its key or a single foreign key:

{{table:a_preserve}}

This is the usual outcome of the 3NF synthesis approach: every dependency of a minimal cover gets a relation of its own, so nothing is lost.

## Example B: BCNF loses a dependency

Example B's ASSIGNMENT relation is where BCNF and preservation pull against each other. The BCNF split keeps:

- STAFF(staff_email, return_type, staff_phone), with staff_email → return_type, staff_phone
- CLIENT_STAFF(client_gstin, staff_email), with no non-trivial dependency

Now test FD1, client_gstin, return_type → staff_email. Start with {client_gstin, return_type}. STAFF cannot add anything, because its only left side is staff_email, which we do not have. CLIENT_STAFF cannot add anything either: its only dependency is the trivial one, so knowing client_gstin there does not tell us staff_email, since a client has two people, one per return type. The closure stays {client_gstin, return_type} and never reaches staff_email. So the dependency is lost:

{{table:b_preserve}}

In plain terms: after the split, nothing in any single table stops the firm from assigning two GSTR-1 specialists to the same client. CLIENT_STAFF would happily hold both rows, and only a join with STAFF reveals that both people do GSTR-1.

There are two ways out, and they trade differently:

- **Stay in 3NF.** Keep ASSIGNMENT(client_gstin, return_type, staff_email) with the primary key (client_gstin, return_type). The rule is a key, so the database enforces it for free. The cost is the dependency staff_email → return_type, which is not a key: a person's return type is repeated on every client they handle, and could in principle disagree between rows. A foreign key from (staff_email, return_type) to STAFF closes that gap.
- **Go to BCNF and add a trigger.** Keep the two BCNF tables and enforce FD1 with a trigger on CLIENT_STAFF that refuses a second person of the same return type for a client.

If we ever built example B we would take the first option, because a key is simpler to trust than a trigger. The textbook guarantee behind this choice is that a lossless, dependency-preserving decomposition into 3NF always exists, while BCNF can only promise losslessness.

## Our 18 tables

In the schema we built, each table's dependencies are about its own columns only (Section 8), so they are preserved by construction. The rules that do span tables, such as "a client login belongs to the same firm as its client" or "an invoice of a closed month cannot change", are not functional dependencies of any one relation. We enforce them with foreign keys and triggers, and Section 9 shows the triggers.
