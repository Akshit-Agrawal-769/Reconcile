#!/usr/bin/env python3
"""Reconcile on XAMPP: a command-line front end for the MariaDB port.

    pip install mysql-connector-python
    python xampp/reconcile_cli.py init                       # create the database + demo data
    python xampp/reconcile_cli.py load-month --as staff.a@demo.test --gstin 07AAPFU0939F1ZX \\
           --period 092026 --books books.csv --gstr2b gstr2b.json
    python xampp/reconcile_cli.py queue --as staff.a@demo.test --period-id 1

Every command that changes data runs one stored procedure as the user given by --as;
the procedure decides whether that user may do it.
"""
import argparse
import csv
import datetime as dt
import io
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_FILES = ("schema.sql", "logic.sql", "seed.sql")

STAGING_COLUMNS = {
    "GSTIN of Supplier": "gstin_of_supplier", "Supplier Name": "supplier_name",
    "Document Type": "document_type", "Invoice Number": "invoice_number",
    "Invoice date": "invoice_date", "Place Of Supply": "place_of_supply",
    "Reverse Charge": "reverse_charge", "HSN/SAC": "hsn_sac", "Rate": "rate",
    "Taxable Value": "taxable_value", "Integrated Tax Paid": "integrated_tax_paid",
    "Central Tax Paid": "central_tax_paid", "State/UT Tax Paid": "state_ut_tax_paid",
    "Cess Paid": "cess_paid", "Eligible for ITC": "eligible_for_itc",
}
STAGING_COLUMNS = {v: k for k, v in STAGING_COLUMNS.items()}  # column -> CSV header


# ------------------------------------------------------------------ pure helpers
def split_sql(text, skip_database=False):
    """Split a .sql file into statements, honouring DELIMITER lines (as the mysql
    client does), so routine bodies containing ';' stay in one piece."""
    delim, buf, out = ";", [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            delim = stripped.split(None, 1)[1]
            continue
        buf.append(line)
        if stripped.endswith(delim):
            stmt = "\n".join(buf).strip()
            stmt = stmt[: -len(delim)].rstrip()
            buf = []
            body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if not body:
                continue
            if skip_database and re.match(r"(CREATE DATABASE|USE)\b", body, re.I):
                continue
            out.append(body)
    return out


def parse_books_csv(text):
    """The purchase register CSV as staging rows (the database validates the values)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {h.strip().casefold(): h for h in (reader.fieldnames or [])}
    missing = [h for h in STAGING_COLUMNS.values() if h.casefold() not in headers]
    if missing:
        raise SystemExit("missing columns: " + ", ".join(missing))
    rows = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append({col: (raw[headers[h.casefold()]] or "").strip().replace(",", "")
                     if col in ("rate", "taxable_value", "integrated_tax_paid", "central_tax_paid",
                                "state_ut_tax_paid", "cess_paid")
                     else (raw[headers[h.casefold()]] or "").strip()
                     for col, h in STAGING_COLUMNS.items()})
    return rows


def _amt(d, k):
    return Decimal(str(d.get(k) or 0))


def flatten_2b(payload):
    """One row per document of a GSTR-2B JSON (b2b, b2ba and cdnr sections).
    Credit notes are negated, as in the PostgreSQL import."""
    dd = payload["data"]["docdata"]
    rows = []
    for sec in ("b2b", "b2ba"):
        for sup in dd.get(sec, []):
            for d in sup.get("inv", []):
                rows.append({"ctin": sup["ctin"], "trdnm": sup.get("trdnm"), "inum": d["inum"],
                             "dt": d["dt"], "doc_type": "INV",
                             **{k: _amt(d, k) for k in ("txval", "igst", "cgst", "sgst", "cess")}})
    for sup in dd.get("cdnr", []):
        for d in sup.get("nt", []):
            sign = -1 if str(d.get("ntty", "")).upper() == "C" else 1
            rows.append({"ctin": sup["ctin"], "trdnm": sup.get("trdnm"), "inum": d["ntnum"],
                         "dt": d["dt"], "doc_type": "CRN" if sign < 0 else "DBN",
                         **{k: sign * _amt(d, k) for k in ("txval", "igst", "cgst", "sgst", "cess")}})
    return rows


# ------------------------------------------------------------------ database
def connect(args, database=True):
    try:
        import mysql.connector
    except ImportError:
        raise SystemExit("install the driver first: pip install mysql-connector-python")
    try:
        # MariaDB 10.4 has no utf8mb4_0900_ai_ci (recent connectors' default), so the
        # collation is named explicitly; use_pure because older C extensions ignore it.
        return mysql.connector.connect(host=args.host, port=args.port, user=args.user,
                                       password=args.password,
                                       database=args.db if database else None, autocommit=True,
                                       charset="utf8mb4", collation="utf8mb4_unicode_ci",
                                       use_pure=True)
    except mysql.connector.Error as e:
        raise SystemExit(f"cannot connect to MySQL/MariaDB at {args.host}:{args.port} ({e.msg}). "
                         "Start MySQL in the XAMPP Control Panel.")


def run_file(cur, name, skip_database=True):
    for stmt in split_sql((HERE / name).read_text(encoding="utf-8"), skip_database=skip_database):
        cur.execute(stmt)


def init_db(args, seed=True):
    conn = connect(args, database=False)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{args.db}`")
    cur.execute(f"CREATE DATABASE `{args.db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute(f"USE `{args.db}`")
    for name in SQL_FILES if seed else SQL_FILES[:2]:
        run_file(cur, name)
    conn.close()


def user_id(cur, email):
    cur.execute("SELECT user_id FROM app_user WHERE email = %s", (email,))
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"no such user: {email}")
    return row[0]


def call(cur, proc, *args):
    """Call a procedure; returns its argument list with OUT values filled in."""
    return cur.callproc(proc, args)


def load_month(cur, uid, gstin, period, books_text, payload, source="DRAFT_2B"):
    pid = call(cur, "sp_open_period", uid, gstin, period, 0)[3]
    rows = parse_books_csv(books_text)
    cols = list(STAGING_COLUMNS)
    cur.executemany(f"INSERT INTO books_staging (period_id, {', '.join(cols)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(cols))})",
                    [(pid, *(r[c] for c in cols)) for r in rows])
    invoices = call(cur, "sp_import_books", uid, pid, 0)[2]
    imp = call(cur, "sp_begin_2b_import", uid, pid, source, 0)[3]
    entries = flatten_2b(payload)
    for e in entries:
        call(cur, "sp_add_2b_entry", uid, imp, e["ctin"], e["trdnm"], e["doc_type"], e["inum"],
             dt.datetime.strptime(e["dt"], "%d-%m-%Y").date(),
             e["txval"], e["igst"], e["cgst"], e["sgst"], e["cess"])
    rows_matched = call(cur, "sp_reconcile", uid, imp, 0)[2]
    return {"period_id": pid, "invoices": invoices, "import_id": imp, "entries": len(entries),
            "results": rows_matched}


def current_import(cur, period_id):
    cur.execute("SELECT import_id FROM v_current_import WHERE period_id = %s", (period_id,))
    row = cur.fetchone()
    if row is None:
        raise SystemExit("this month has no GSTR-2B import yet")
    return row[0]


def print_table(cur):
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    widths = [max(len(str(n)), *(len(str(r[i])) for r in rows)) if rows else len(n)
              for i, n in enumerate(names)]
    print("  ".join(n.ljust(w) for n, w in zip(names, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(v if v is not None else "").ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} rows)")


# ------------------------------------------------------------------ commands
def main(argv=None):
    ap = argparse.ArgumentParser(prog="reconcile_cli.py", description="Reconcile on XAMPP (MariaDB).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="reconcile")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("init", help="drop and create the database, load schema, logic and demo data")
    p = sub.add_parser("load-month", help="open a month, import books CSV and GSTR-2B JSON, reconcile")
    p.add_argument("--as", dest="who", required=True)
    p.add_argument("--gstin", required=True)
    p.add_argument("--period", required=True, help="MMYYYY")
    p.add_argument("--books", required=True)
    p.add_argument("--gstr2b", required=True)
    p.add_argument("--source", default="DRAFT_2B", choices=["DRAFT_2B", "IMS", "RECOMPUTED_2B"])
    for name, text in (("reconcile", "match the month's current GSTR-2B again"),
                       ("queue", "show the exception queue"), ("totals", "show the month's four numbers"),
                       ("submit", "send the month for client approval"),
                       ("client-approve", "client approves the month"), ("close", "partner closes the month")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--as", dest="who", required=True)
        p.add_argument("--period-id", type=int, required=True)
    p = sub.add_parser("reopen", help="partner reopens a closed month")
    p.add_argument("--as", dest="who", required=True)
    p.add_argument("--period-id", type=int, required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("decide", help="propose a decision for one match row")
    p.add_argument("--as", dest="who", required=True)
    p.add_argument("--result", type=int, required=True)
    p.add_argument("--decision", required=True, choices=["ACCEPT", "CHASE", "DEFER", "PARTIAL", "INELIGIBLE"])
    p.add_argument("--ims", required=True, choices=["ACCEPT", "REJECT", "PENDING", "NONE"])
    p.add_argument("--pct", type=Decimal)
    p.add_argument("--remark")
    p = sub.add_parser("approve", help="partner approves a proposed decision")
    p.add_argument("--as", dest="who", required=True)
    p.add_argument("--review", type=int, required=True)

    a = ap.parse_args(argv)
    if a.cmd == "init":
        init_db(a)
        print(f"database {a.db} created with demo data (logins use password demo123)")
        return 0

    import mysql.connector
    conn = connect(a)
    cur = conn.cursor()
    try:
        uid = user_id(cur, a.who)
        if a.cmd == "load-month":
            r = load_month(cur, uid, a.gstin, a.period, Path(a.books).read_text(encoding="utf-8-sig"),
                           json.loads(Path(a.gstr2b).read_text(encoding="utf-8")), a.source)
            print(f"period {r['period_id']}: {r['invoices']} invoices, {r['entries']} 2B documents, "
                  f"{r['results']} match rows")
        elif a.cmd == "reconcile":
            print(call(cur, "sp_reconcile", uid, current_import(cur, a.period_id), 0)[2], "match rows")
        elif a.cmd in ("queue", "totals"):
            cur.callproc("sp_queue" if a.cmd == "queue" else "sp_totals", (uid, a.period_id))
            for result in cur.stored_results():
                print_table(result)
        elif a.cmd == "decide":
            print("review", call(cur, "sp_propose", uid, a.result, a.decision, a.ims, a.pct, a.remark, 0)[6])
        elif a.cmd == "approve":
            call(cur, "sp_approve", uid, a.review)
            print("approved")
        elif a.cmd == "reopen":
            call(cur, "sp_reopen_period", uid, a.period_id, a.reason)
            print("reopened")
        else:
            proc = {"submit": "sp_submit_period", "client-approve": "sp_client_approve",
                    "close": "sp_close_period"}[a.cmd]
            call(cur, proc, uid, a.period_id)
            print("done")
    except mysql.connector.Error as e:
        print(f"refused: {e.msg}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
