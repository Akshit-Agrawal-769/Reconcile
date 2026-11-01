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
