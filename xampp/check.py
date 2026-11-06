#!/usr/bin/env python3
"""End-to-end check of the MariaDB port on your XAMPP.

    pip install mysql-connector-python
    python xampp/check.py              # uses root with no password on 127.0.0.1:3306

It builds a throwaway database `reconcile_check`, loads the port, runs one synthetic
month with a known answer key, and tests the rules (tenancy, maker-checker, approval,
closed months, the audit log). Each step prints [ok] or [FAIL] with the server's
message. The last line is ALL CHECKS PASSED when everything worked.
"""
import argparse
import csv
import datetime as dt
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from tools.synth import generate  # noqa: E402
from xampp import reconcile_cli as cli  # noqa: E402
from xampp import rules  # noqa: E402

PORT_STATUSES = {"EXACT", "ROUNDING", "MISMATCH", "ONLY_IN_BOOKS", "ONLY_IN_2B", "DUPLICATE"}


def compare_with_truth(truth, books_status, portal_statuses):
    """Compare match results with the generator's answer key.

    books_status: {books invoice no: status}; portal_statuses: {2B invoice no: [statuses]}.
    Rows whose expected status needs a pass only the PostgreSQL build has (PROBABLE) are
    skipped. Returns (correct, [(invoice, expected, got)], skipped)."""
    ok, wrong, skipped = 0, [], 0
    for t in truth:
        exp = t["expected_status"]
        if exp not in PORT_STATUSES:
            skipped += 1
            continue
        if t["books_invoice_no"]:
            got = books_status.get(t["books_invoice_no"])
            key = t["books_invoice_no"]
            good = got == exp
        else:
            key = t["portal_invoice_no"]
            got = portal_statuses.get(key, [])
            good = exp in got
        if good:
            ok += 1
        else:
            wrong.append((key, exp, got))
    return ok, wrong, skipped


class Checker:
    def __init__(self):
        self.failed = 0

    def ok(self, text):
        print(f"[ok]   {text}")

    def fail(self, text):
        self.failed += 1
        print(f"[FAIL] {text}")

    def expect(self, cond, text, detail=""):
        (self.ok if cond else self.fail)(text if cond else f"{text}: {detail}")

    def refused(self, fn, message, text):
        import mysql.connector
        try:
            fn()
        except mysql.connector.Error as e:
            self.expect(message in e.msg, text, f"refused with '{e.msg}', expected '{message}'")
            return
        self.fail(f"{text}: it was allowed, expected '{message}'")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="reconcile_check")
    a = ap.parse_args(argv)
    c = Checker()

    import mysql.connector
    cli.init_db(a)
    c.ok(f"created database {a.db} and loaded schema.sql, logic.sql, seed.sql")
    conn = cli.connect(a)
    cur = conn.cursor()

    # 1. The three shared rules give the same answers as the PostgreSQL build.
    samples = ["INV-104", "inv/0104", "INV-104/26-27", "2026-27/INV/7", "INV-26-28", "0007",
               "---", "A-007-B", "INV 2026-2027 009"]
    for s in samples:
        cur.execute("SELECT norm_doc_no(%s)", (s,))
        got = cur.fetchone()[0]
        c.expect(got == rules.norm_doc_no(s), f"norm_doc_no('{s}') = '{got}'", f"Python says '{rules.norm_doc_no(s)}'")
    for g in ("07AAPFU0939F1ZX", "07ABCDE1234F1Z2", "07ABCDE1234F1Z9", "07abcde1234f1z2"):
        cur.execute("SELECT gstin_checksum_ok(%s)", (g,))
        got = bool(cur.fetchone()[0])
        c.expect(got == rules.gstin_ok(g), f"gstin_checksum_ok('{g}') = {got}")
    cur.execute("SELECT fin_year_of('2026-03-31'), fin_year_of('2026-04-01')")
    c.expect(cur.fetchone() == ("2025-26", "2026-27"), "fin_year_of around 1 April")

    # 2. A synthetic client and one month with a known answer key.
    out = Path(tempfile.mkdtemp(prefix="reconcile-check-"))
    m = generate(clients=1, docs=60, period="092026", seed=7, out=str(out))[0]
    cur.execute("INSERT INTO client (firm_id, name, pan) VALUES (1, 'Delta Distributors', %s)", (m["pan"],))
    client_id = cur.lastrowid
    cur.execute("INSERT INTO gstin_reg (gstin, client_id, taxpayer_type, reg_date) "
                "VALUES (%s, %s, 'REGULAR', '2019-04-01')", (m["gstin"], client_id))
    cur.execute("INSERT INTO staff_assignment VALUES (2, %s)", (client_id,))
    cur.execute("INSERT INTO app_user (firm_id, client_id, email, role, password_hash) "
                "VALUES (1, %s, 'delta@demo.test', 'client', SHA2('x', 256))", (client_id,))
    staff_a, staff_b, partner = (cli.user_id(cur, e) for e in
                                 ("staff.a@demo.test", "staff.b@demo.test", "partner@demo.test"))
    owner = cli.user_id(cur, "delta@demo.test")
    books_text = (out / m["books_file"]).read_text(encoding="utf-8-sig")
    payload = json.loads((out / m["gstr2b_file"]).read_text())

    c.refused(lambda: cli.call(cur, "sp_open_period", staff_b, m["gstin"], "092026", 0),
              "not your client", "staff B cannot open a month for a client not assigned to them")
    r = cli.load_month(cur, staff_a, m["gstin"], "092026", books_text, payload)
    pid, imp = r["period_id"], r["import_id"]
    c.ok(f"loaded {r['invoices']} invoices and {r['entries']} 2B documents; {r['results']} match rows")

    # 3. Results against the answer key.
    cur.execute("SELECT i.invoice_no, r.status FROM recon_result r JOIN invoice i ON i.invoice_id = r.invoice_id "
                "WHERE r.import_id = %s", (imp,))
    books_status = dict(cur.fetchall())
    cur.execute("SELECT e.invoice_no, r.status FROM recon_result r JOIN gstr2b_entry e ON e.entry_id = r.entry_id "
                "WHERE r.import_id = %s", (imp,))
    portal = {}
    for no, st in cur.fetchall():
        portal.setdefault(no, []).append(st)
    truth = list(csv.DictReader(open(out / "truth.csv", encoding="utf-8")))
    good, wrong, skipped = compare_with_truth(truth, books_status, portal)
    c.expect(not wrong, f"{good} of {good + len(wrong)} documents matched as the answer key says "
                        f"({skipped} skipped: they need the fuzzy/PAN passes of the full build)",
             f"wrong: {wrong[:5]}")

    # 4. The dashboard totals agree with a sum done here in Python.
    month_tax = Decimal("0")
    for row in cli.parse_books_csv(books_text):
        sign = -1 if row["document_type"].upper() in ("CREDIT NOTE", "CRN") else 1
        month_tax += sign * sum(Decimal(row[k] or "0") for k in
                                ("integrated_tax_paid", "central_tax_paid", "state_ut_tax_paid", "cess_paid"))
    cur.execute("SELECT itc_books, itc_at_risk, match_rate_pct FROM v_period_totals WHERE period_id = %s", (pid,))
    itc_books, at_risk, rate = cur.fetchone()
    c.expect(Decimal(itc_books) == month_tax, f"v_period_totals: ITC in books Rs {itc_books}, "
                                              f"at risk Rs {at_risk}, match rate {rate}%", f"Python sum is {month_tax}")

    # 5. Decisions and maker-checker.
    cur.execute("SELECT result_id FROM v_queue WHERE period_id = %s AND status = 'ONLY_IN_BOOKS' "
                "ORDER BY ABS(diff_tax) DESC LIMIT 1", (pid,))
    big = cur.fetchone()[0]
    rev = cli.call(cur, "sp_propose", partner, big, "CHASE", "PENDING", None, "asked supplier", 0)[6]
    c.refused(lambda: cli.call(cur, "sp_approve", partner, rev), "maker cannot approve own review",
              "a partner cannot approve a decision they proposed")
    rev = cli.call(cur, "sp_propose", staff_a, big, "CHASE", "PENDING", None, "asked supplier", 0)[6]
    c.refused(lambda: cli.call(cur, "sp_approve", staff_a, rev), "only a partner can approve",
              "staff cannot approve")
    c.refused(lambda: cli.call(cur, "sp_propose", staff_a, big, "PARTIAL", "ACCEPT", None, "", 0),
              "", "a partial claim without a percentage is refused by a CHECK constraint")

    # 6. The month's workflow.
    cli.call(cur, "sp_submit_period", staff_a, pid)
    c.refused(lambda: cli.call(cur, "sp_close_period", partner, pid), "client approval missing",
              "the month cannot close before the client approves")
    c.refused(lambda: cli.call(cur, "sp_client_approve", staff_a, pid), "not allowed for your role",
              "staff cannot approve on the client's behalf")
    cli.call(cur, "sp_client_approve", owner, pid)
    c.refused(lambda: cli.call(cur, "sp_close_period", partner, pid), "reviews awaiting approval",
              "the month cannot close while a large decision waits for a partner")
    cli.call(cur, "sp_approve", partner, rev)
    cli.call(cur, "sp_close_period", partner, pid)
    cur.execute("SELECT state FROM return_period WHERE period_id = %s", (pid,))
    c.expect(cur.fetchone()[0] == "CLOSED", "the month closed after approval and sign-off")

    # 7. A closed month is frozen, and history cannot be rewritten.
    c.refused(lambda: cur.execute("UPDATE invoice SET pos = '09' WHERE gstin = %s LIMIT 1", (m["gstin"],)),
              "invoice of a closed period cannot change", "invoices of a closed month cannot be edited")
    c.refused(lambda: cur.execute("UPDATE return_period SET state = 'OPEN' WHERE period_id = %s", (pid,)),
              "illegal state transition", "the state machine refuses a jump back to OPEN")
    c.refused(lambda: cur.execute("DELETE FROM audit_log LIMIT 1"), "audit log is append-only",
              "the audit log cannot be deleted")
    c.refused(lambda: cli.call(cur, "sp_reopen_period", partner, pid, " "), "reopen reason required",
              "reopening needs a reason")
    cli.call(cur, "sp_reopen_period", partner, pid, "supplier amended an invoice")
    cur.execute("SELECT state FROM return_period WHERE period_id = %s", (pid,))
    c.expect(cur.fetchone()[0] == "IN_REVIEW", "a partner reopened the month with a reason")
    cur.execute("SELECT COUNT(*) FROM audit_log")
    c.ok(f"audit log holds {cur.fetchone()[0]} entries")

    conn.close()
    if c.failed:
        print(f"{c.failed} CHECK(S) FAILED. Paste the [FAIL] lines back to us.")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
