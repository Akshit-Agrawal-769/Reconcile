# 1. Project Description

## The problem we picked

Every month a GST-registered business claims input tax credit (ITC) for the GST it paid on its purchases. Since 2022 the law lets it claim only the credit that its suppliers have actually reported: Section 16(2)(aa) of the CGST Act ties the claim to the supplier's GSTR-1, which the portal turns into a statement for the buyer called GSTR-2B. If the buyer's books say ₹18,000 of tax was paid on an invoice and GSTR-2B shows ₹10,000, or shows nothing at all, the difference cannot be claimed that month.

Most small businesses hand this check to a chartered accountant (CA). A mid-sized CA firm looks after 50 to 300 clients, and for each of them, each month, a staff member downloads the GSTR-2B JSON from the portal, exports the purchase register from the client's accounting software, and compares the two in Excel with VLOOKUP. The comparison is slower and less reliable than it sounds. The same invoice is written as INV/104 in one place and INV-0104 in the other. A supplier rounds the tax differently and the totals are off by 40 paise. An invoice dated 30 September appears in October's statement. A supplier forgets to upload the invoice at all.

## What we built

Reconcile is a database that holds both sides of this comparison for every client of a CA firm and does the matching inside the database. For each month it records:

- the client's purchase register (invoices and their lines),
- every version of GSTR-2B downloaded from the portal,
- the result of matching the two, invoice by invoice,
- what the CA's staff decided to do about each mismatch, and who approved it,
- where the month stands: open, in review, waiting for the client, or closed.

The matcher runs in passes, from strict to loose, so that a pair matched by an early pass is never offered to a later one. An exact match needs the same supplier, document type, number and financial year, the same date, and every tax head equal. A rounding match allows up to ₹1 per tax head and a few days on the date. A pair with the same invoice but a larger difference is a value mismatch. Whatever is left over is reported as present only in the books (the client may lose that credit) or only in GSTR-2B.

The rules a CA firm cares about are enforced by the database itself, not by a front end that someone could bypass. A staff member can see only the clients assigned to them. A decision involving more than ₹5,000 of tax must be approved by a partner, and the partner who approves it cannot be the person who proposed it. A month cannot be closed until the client has approved it, and once closed its invoices cannot be edited.

## Two builds

This report describes the version we built for the lab on XAMPP: MariaDB 10.4 with stored procedures and triggers, driven by a Python command-line program. It has 18 normalized tables, six matching passes and every workflow rule listed above.

We first built the project on PostgreSQL 16, and that version is larger. It adds row-level security, a table partitioned by financial year, three more matching passes (near-miss invoice numbers, the same supplier PAN under a different state GSTIN, and invoices that turn up a month late), a background job queue and a web interface. Where the two differ, this report says so.

## How well it matches

We do not have real client data, so we wrote a generator (`tools/synth.py`) that produces a purchase register and a matching GSTR-2B with known differences: reformatted numbers, one-character typos, rounding, shifted dates, wrong amounts, missing invoices and duplicates. Because the generator knows the right answer for every document, we can score the matcher. On 1,020 documents the PostgreSQL build put every one in the expected category. In the demo month used in Section 9, ₹17,03,373 of ITC sits in the books, ₹16,20,045 in GSTR-2B, and ₹1,08,154 is at risk across the invoices that do not match yet, with 89.7% of invoices matched.
