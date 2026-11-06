# 2. Requirement Analysis

We began by writing down how a month of GST work moves through a CA firm: who prepares the files, who checks them, and who signs off. The requirements below come from that walk-through and from the GST rules on ITC.

## Users

| User | Who they are | What they need to do |
|---|---|---|
| Staff | Article assistants and accountants who prepare returns | Load a client's files, see the mismatches, decide what to do about each, send the month for approval |
| Partner | The CA who signs off | Approve large decisions, close the month, reopen it if a supplier amends an invoice |
| Client | The business owner | See how much credit they can claim, approve the month so the return can be filed |

A staff member works on a handful of clients; a partner can see every client of the firm; a client sees only their own business.

## Functional requirements

These are numbered because later sections refer back to them.

- **FR1.** Record CA firms, their clients, each client's GSTINs (one per state) and the firm's users with their roles.
- **FR2.** Open a return period (a month, written MMYYYY as on the portal) for a GSTIN.
- **FR3.** Import the client's purchase register from the CSV their software exports, invoice by invoice and line by line.
- **FR4.** Import GSTR-2B from the portal's JSON. Keep every download as a new version; the latest version is the one that counts.
- **FR5.** Match the two sides and give every invoice and every 2B document exactly one status: exact, rounding, value mismatch, only in books, only in 2B, or duplicate.
- **FR6.** Let staff record a decision for each mismatch (accept, chase the supplier, defer, claim part, ineligible) and the action to take on the portal's Invoice Management System (accept, reject, keep pending).
- **FR7.** Send decisions above the client's threshold to a partner for approval.
- **FR8.** Move the month through open, in review, awaiting client approval and closed, and let a partner reopen it with a reason.
- **FR9.** Show the month's totals: ITC in the books, ITC in GSTR-2B, ITC at risk and the match rate.
- **FR10.** Log follow-ups with suppliers and keep the filing calendar (GSTR-1 and GSTR-3B due dates).

## Data requirements

From the list above, the database must hold four groups of data.

- **Who:** firms, clients (identified by PAN), GSTIN registrations, suppliers (identified by GSTIN), users and which staff look after which client.
- **What the client says:** invoices with supplier, document type, number, date and place of supply; and invoice lines with HSN code, rate, taxable value and the four tax heads (IGST, CGST, SGST and cess).
- **What the portal says:** each GSTR-2B download with its version and source, and every document listed in it.
- **What the firm decided:** match results, decisions with their approvals, the state of each month, follow-ups, filings and an audit trail.

## Business rules

These rules must hold no matter which program touches the data, so we enforce them in the database (Section 9 shows how).

- **BR1.** A GSTIN is 15 characters and its last character is a check digit computed from the other fourteen. Characters 3 to 12 of a client's GSTIN are its PAN.
- **BR2.** A supplier cannot issue two invoices with the same number in one financial year. "INV/0104" and "inv-104" are the same number.
- **BR3.** An invoice line is either inter-state (IGST only) or intra-state (CGST and SGST in equal halves), never both.
- **BR4.** A credit note reduces credit, so its amounts are stored as negative numbers.
- **BR5.** Staff see and change only the clients assigned to them.
- **BR6.** A decision above the client's approval threshold needs a partner's approval, and nobody approves their own decision (maker-checker).
- **BR7.** A month closes only after the client approves it (or a partner overrides with a written reason) and no decision is waiting for approval.
- **BR8.** The invoices of a closed month cannot be added, changed or deleted.
- **BR9.** The month's state can move only along the allowed transitions.
- **BR10.** The audit log is never edited or deleted.

## Non-functional requirements and limits

The database should give the same answer whether the data arrives from Python, phpMyAdmin or a future web screen, which is why the rules live in triggers and procedures. It must run on a student's laptop under XAMPP. Speed matters less than correctness: one client's month is a few hundred to a few thousand invoices.

We left some things out on purpose. The program does not log in to the GST portal (that needs a licensed GST Suvidha Provider), does not file returns, and does not send emails or WhatsApp messages; it only records that a follow-up was sent.
