# 3. ER Model

We drew the conceptual model in Chen notation before writing any SQL. Entities are rectangles, relationships are diamonds and attributes are ovals. A key attribute is underlined. A weak entity, one that cannot be identified without its owner, has a double rectangle; its identifying relationship has a double diamond and a thick line on the weak side, and its partial key sits in a dashed oval. A dotted oval is a derived attribute and a double oval is multivalued. The figure shows each entity's key and the attributes that explain it; the full attribute lists are in the table that follows.

{{fig:er_chen|ER diagram of Reconcile in Chen notation|16}}

## Entities

| Entity | Key | Main attributes | Notes |
|---|---|---|---|
| FIRM | firm_id | name | The CA firm |
| CLIENT | client_id | name, pan, approval_threshold, rounding_tol | A business; one per PAN |
| GSTIN_REG | gstin | taxpayer_type, reg_date, state (derived) | One registration per state; state is the GSTIN's first two digits |
| USER | user_id | email, role, password_hash | Partner, staff or client login |
| SUPPLIER | gstin | name | Shared by all clients who buy from it |
| RETURN_PERIOD | gstin + period (weak) | state, client approval, override reason | A month of one GSTIN |
| INVOICE | invoice_id | invoice_no, invoice_date, doc_type, pos, fin_year (derived) | One purchase document in the books |
| INVOICE_LINE | invoice + line_no (weak) | hsn, rate, taxable_value, tax (multivalued) | Tax has four heads |
| GSTR2B_IMPORT | period + version (weak) | source, imported_at | One download of the portal statement |
| GSTR2B_ENTRY | entry_id | invoice_no, invoice_date, taxable_value, tax (multivalued) | One document as the portal reports it |
| REVIEW | review_id | decision, ims_action, claim_pct, state | What the firm decided about an invoice |
| FOLLOWUP | followup_id | channel, sent_on, response, next_chase_on | Chasing a supplier |
| FILING | gstin + return_type + period (weak) | filed_on, late_fee | GSTR-1 or GSTR-3B for a month |

## Relationships

| Relationship | Between | Cardinality | Participation |
|---|---|---|---|
| SERVES | FIRM, CLIENT | 1 : N | every client belongs to a firm |
| EMPLOYS | FIRM, USER | 1 : N | every user belongs to a firm |
| ASSIGNED | USER, CLIENT | M : N | only staff users take part |
| REGISTERS | CLIENT, GSTIN_REG | 1 : N | every GSTIN has a client |
| FILES_FOR | GSTIN_REG, RETURN_PERIOD | 1 : N, identifying | total on the period side |
| BUYS | GSTIN_REG, INVOICE | 1 : N | every invoice is for one GSTIN |
| ISSUES | SUPPLIER, INVOICE | 1 : N | every invoice has a supplier |
| HAS_LINE | INVOICE, INVOICE_LINE | 1 : N, identifying | an invoice has at least one line |
| RECEIVES | RETURN_PERIOD, GSTR2B_IMPORT | 1 : N, identifying | a month may have several downloads |
| LISTS | GSTR2B_IMPORT, GSTR2B_ENTRY | 1 : N | |
| REPORTS | SUPPLIER, GSTR2B_ENTRY | 1 : N | |
| MATCHES | INVOICE, GSTR2B_ENTRY | 1 : 1 per import | partial on both sides; carries status and diff_tax |
| DECIDES | REVIEW, INVOICE | N : 1 over time, one open decision per invoice | |
| PROPOSES | USER, REVIEW | 1 : N | a second link, approved_by, works the same way |
| CHASES | FOLLOWUP, SUPPLIER | N : 1 | |
| TRACKS | GSTIN_REG, FILING | 1 : N, identifying | |

## Choices worth explaining

RETURN_PERIOD is weak because a month on its own ("September 2026") means nothing; it only exists as September 2026 *for a particular GSTIN*. Its partial key is the period string, and the owner's GSTIN completes it. GSTR2B_IMPORT is weak for the same reason: "version 2" is meaningless without the month it belongs to.

MATCHES is a relationship rather than an entity because a match is a fact about a pair: this invoice in the books corresponds to that document in GSTR-2B, with this status and this difference in tax. Either side can be missing (an invoice only in the books takes part with no 2B document), which is why participation is partial on both sides.

REVIEW hangs off the invoice and not off the match. A staff member's decision is about a real invoice. When a corrected GSTR-2B arrives, the matcher runs again and the match rows are replaced, but the decision about INV-104 from that supplier should survive. So REVIEW is identified by the invoice itself (supplier, document type, number and financial year), not by a match row.

Tax is drawn as multivalued because every amount on a GST document is split into four heads: IGST, CGST, SGST and cess. The set of heads is fixed by law, so in the relational schema we turn this multivalued attribute into four columns rather than a separate table (Section 4).
