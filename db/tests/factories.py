import itertools

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_counter = itertools.count(1)


def make_gstin(pan, state="07"):
    """Build a valid GSTIN: state + PAN + entity '1' + 'Z' + mod-36 check char."""
    body = f"{state}{pan}1Z"
    total = 0
    for i, ch in enumerate(body, start=1):
        p = CHARS.index(ch) * (1 if i % 2 == 1 else 2)
        total += p // 36 + p % 36
    return body + CHARS[(36 - total % 36) % 36]


def default_firm(conn):
    row = conn.execute("SELECT firm_id FROM firm ORDER BY firm_id LIMIT 1").fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO firm(name) VALUES ('Test Firm') RETURNING firm_id"
    ).fetchone()[0]


def make_client(conn, pan="AAPFU0939F", name="Acme"):
    return conn.execute(
        "INSERT INTO client(firm_id, name, pan) VALUES (%s, %s, %s) RETURNING client_id",
        (default_firm(conn), name, pan),
    ).fetchone()[0]


def make_reg(conn, client_id, ttype="REGULAR", state="07", freq="MONTHLY"):
    pan = conn.execute("SELECT pan FROM client WHERE client_id = %s", (client_id,)).fetchone()[0]
    g = make_gstin(pan, state)
    conn.execute(
        "INSERT INTO gstin_reg(gstin, client_id, taxpayer_type, filing_freq, state_code, reg_date) "
        "VALUES (%s, %s, %s, %s, %s, '2020-01-01')",
        (g, client_id, ttype, freq, state),
    )
    return g


def make_party(conn, pan="ABCDE1234F", state="07"):
    g = make_gstin(pan, state)
    conn.execute(
        "INSERT INTO party(gstin, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (g, f"Party {pan}"),
    )
    return g


def make_user(conn, role, client_id=None):
    n = next(_counter)
    return conn.execute(
        "INSERT INTO app_user(firm_id, client_id, email, role, password_hash) "
        "VALUES (%s, %s, %s, %s, 'x') RETURNING user_id",
        (default_firm(conn), client_id, f"{role}{n}@test.local", role),
    ).fetchone()[0]


def assign(conn, user_id, client_id):
    conn.execute(
        "INSERT INTO staff_assignment(user_id, client_id) VALUES (%s, %s)",
        (user_id, client_id),
    )


def make_invoice(conn, gstin, party_gstin, invoice_no, invoice_date, lines,
                 direction="INWARD", doc_type="INV", pos=None,
                 is_rcm=False, is_import=False):
    """Insert an invoice with lines; return (invoice_id, fin_year)."""
    if pos is None:
        pos = (party_gstin if direction == "INWARD" else gstin)[:2]
    fy = conn.execute("SELECT fin_year_of(%s::date)", (str(invoice_date),)).fetchone()[0]
    inv_id = conn.execute(
        "INSERT INTO invoice(gstin, party_gstin, direction, doc_type, invoice_no, "
        "invoice_date, fin_year, place_of_supply, is_rcm, is_import) "
        "VALUES (%s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s) RETURNING invoice_id",
        (gstin, party_gstin, direction, doc_type, invoice_no, str(invoice_date),
         fy, pos, is_rcm, is_import),
    ).fetchone()[0]
    for ln in lines:
        conn.execute(
            "INSERT INTO invoice_line(invoice_id, fin_year, hsn_sac, taxable_value, rate, "
            "igst, cgst, sgst, cess, itc_category) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (inv_id, fy, ln.get("hsn_sac", "8471"), ln["taxable_value"],
             ln.get("rate", 18), ln.get("igst", 0), ln.get("cgst", 0),
             ln.get("sgst", 0), ln.get("cess", 0), ln.get("itc_category", "ELIGIBLE")),
        )
    return inv_id, fy


def make_period(conn, gstin, period="092026"):
    return conn.execute("SELECT sp_open_period(%s, %s)", (gstin, period)).fetchone()[0]


def build_2b_payload(gstin, rtnprd, docs):
    """Build a GSTR-2B payload from flat doc dicts (document-level amounts)."""
    import datetime

    b2b, b2ba, cdnr = {}, {}, {}
    names = {}
    for d in docs:
        dt = d["dt"]
        if isinstance(dt, (datetime.date, datetime.datetime)):
            dt = dt.strftime("%d-%m-%Y")
        amt = {k: d.get(k, 0) for k in ("igst", "cgst", "sgst", "cess")}
        txval = d["txval"]
        val = float(txval) + sum(float(v) for v in amt.values())
        section = d.get("section", "b2b")
        ctin = d["ctin"]
        names[ctin] = d.get("trdnm", "Supplier")
        if section == "cdnr":
            row = {"ntnum": d["inum"], "ntty": d.get("ntty", "C"), "dt": dt,
                   "val": val, "txval": txval, **amt}
            cdnr.setdefault(ctin, []).append(row)
        else:
            row = {"inum": d["inum"], "dt": dt, "val": val, "pos": gstin[:2],
                   "rev": "N", "itcavl": "Y", "txval": txval, **amt}
            if section == "b2ba":
                row["oinum"] = d.get("oinum")
            (b2ba if section == "b2ba" else b2b).setdefault(ctin, []).append(row)

    def grp(m, key):
        return [{"ctin": c, "trdnm": names[c], key: rows} for c, rows in m.items()]

    return {"data": {"gstin": gstin, "rtnprd": rtnprd, "docdata": {
        "b2b": grp(b2b, "inv"), "b2ba": grp(b2ba, "inv"), "cdnr": grp(cdnr, "nt")}}}


def import_2b(conn, period_id, payload, user_id, source="DRAFT_2B"):
    import json

    return conn.execute(
        "SELECT sp_import_gstr2b(%s, %s::jsonb, %s, %s)",
        (period_id, json.dumps(payload), source, user_id),
    ).fetchone()[0]


def reconcile(conn, import_id, user_id):
    return conn.execute("SELECT sp_reconcile(%s, %s)", (import_id, user_id)).fetchone()[0]


def recon_rows(conn, import_id):
    """(status, matched_by, books_invoice_no, portal_invoice_no, diff_tax) ordered stably."""
    return conn.execute(
        "SELECT r.status::text, r.matched_by, i.invoice_no, e.invoice_no, r.diff_tax "
        "FROM recon_result r "
        "LEFT JOIN invoice i ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy "
        "LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id AND e.fin_year = r.entry_fy "
        "WHERE r.import_id = %s "
        "ORDER BY r.status::text, i.invoice_no, e.invoice_no",
        (import_id,),
    ).fetchall()
