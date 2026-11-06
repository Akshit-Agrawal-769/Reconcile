"""Uploads: validate the file, store it, stage books rows, and enqueue the import job.

Validation here is about the file's *shape* (is it JSON, are the 15 columns there, is
each date a date). Whether its content is acceptable — the right month, valid GSTINs, a
closed period — is the database's call, made when the job runs or the job is enqueued.
"""
import csv
import datetime as _dt
import decimal
import hashlib
import io
import json

import psycopg

from api import sql
from api.db import ApiError, tx

MAX_BYTES = 10 * 1024 * 1024
SOURCES = ("IMS", "DRAFT_2B", "RECOMPUTED_2B")

# The purchase-register export format (tools/synth.py BOOKS_HEADER) → books_staging column.
BOOKS_COLUMNS = {
    "GSTIN of Supplier": "gstin_of_supplier",
    "Supplier Name": "supplier_name",
    "Document Type": "document_type",
    "Invoice Number": "invoice_number",
    "Invoice date": "invoice_date",
    "Place Of Supply": "place_of_supply",
    "Reverse Charge": "reverse_charge",
    "HSN/SAC": "hsn_sac",
    "Rate": "rate",
    "Taxable Value": "taxable_value",
    "Integrated Tax Paid": "integrated_tax_paid",
    "Central Tax Paid": "central_tax_paid",
    "State/UT Tax Paid": "state_ut_tax_paid",
    "Cess Paid": "cess_paid",
    "Eligible for ITC": "eligible_for_itc",
}
BOOKS_HEADER = list(BOOKS_COLUMNS)
AMOUNTS = ("Rate", "Taxable Value", "Integrated Tax Paid", "Central Tax Paid",
           "State/UT Tax Paid", "Cess Paid")
ZERO_IF_BLANK = ("Integrated Tax Paid", "Central Tax Paid", "State/UT Tax Paid", "Cess Paid")
# The values sp_import_books accepts (Plan A). Checked here so a bad row is refused at
# upload with its row number, instead of failing the import job later.
DOC_TYPES = {"invoice": "INV", "inv": "INV", "credit note": "CRN", "crn": "CRN",
             "debit note": "DBN", "dbn": "DBN"}
ITC_FLAGS = ("yes", "no", "ineligible", "blocked")


def _text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApiError(400, "file is not UTF-8 text")


def validate_2b(obj) -> None:
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict):
        raise ApiError(400, "not a GSTR-2B file: data missing")
    data = obj["data"]
    if not isinstance(data.get("rtnprd"), str):
        raise ApiError(400, "not a GSTR-2B file: data.rtnprd missing")
    if not isinstance(data.get("docdata"), dict):
        raise ApiError(400, "not a GSTR-2B file: data.docdata missing")


def _norm(h: str) -> str:
    return " ".join(h.split()).casefold()


def parse_books_csv(text: str) -> list[dict]:
    """Rows keyed by books_staging column. Raises ApiError(400) naming the first problem."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, None) or []
    index = {_norm(h): i for i, h in enumerate(header)}
    missing = [h for h in BOOKS_HEADER if _norm(h) not in index]
    if missing:
        raise ApiError(400, "missing columns: " + ", ".join(missing))
    rows = []
    try:
        for raw in reader:
            if any(cell.strip() for cell in raw):
                rows.append(_books_row(raw, index, len(rows) + 1))
    except csv.Error as e:  # e.g. an unclosed quote running past the field size limit
        raise ApiError(400, f"row {len(rows) + 1}: the file is not valid CSV ({e})")
    if not rows:
        raise ApiError(400, "the file has no invoice rows")
    return rows


def _books_row(raw, index, n):
    cell = {h: (raw[index[_norm(h)]].strip() if index[_norm(h)] < len(raw) else "")
            for h in BOOKS_HEADER}
    try:
        _dt.datetime.strptime(cell["Invoice date"], "%d-%m-%Y")
    except ValueError:
        raise ApiError(400, f'row {n}: Invoice date "{cell["Invoice date"]}" is not DD-MM-YYYY')
    for h in AMOUNTS:
        v = cell[h].replace(",", "")
        if v == "" and h in ZERO_IF_BLANK:
            v = "0"
        try:
            decimal.Decimal(v)
            if not decimal.Decimal(v).is_finite():
                raise decimal.InvalidOperation
        except decimal.InvalidOperation:
            raise ApiError(400, f'row {n}: {h} "{cell[h]}" is not a number')
        cell[h] = v
    if cell["Document Type"].casefold() not in DOC_TYPES:
        raise ApiError(400, f'row {n}: Document Type "{cell["Document Type"]}" must be '
                            "Invoice, Credit Note or Debit Note")
    if cell["Eligible for ITC"].casefold() not in ITC_FLAGS:
        raise ApiError(400, f'row {n}: Eligible for ITC "{cell["Eligible for ITC"]}" must be '
                            "Yes, No, Ineligible or Blocked")
    return {BOOKS_COLUMNS[h]: cell[h] for h in BOOKS_HEADER}


def _file(ctx):
    f = (ctx.form or {}).get("file")
    if not isinstance(f, tuple):
        raise ApiError(400, "choose a file to upload")
    filename, content = f
    if len(content) > MAX_BYTES:
        raise ApiError(400, "file is larger than 10 MB")
    return (filename or "upload")[:255], content


def _visible_period(c, period_id) -> str:
    row = c.execute(sql.PERIOD_VISIBLE, (period_id,)).fetchone()
    if row is None:
        raise ApiError(404, "period not found")
    return row[1]


def _store(c, period_id, kind, filename, content: bytes, text: str) -> int:
    """Store the file once. The same bytes again are refused, unless the job for the
    earlier copy failed: then the stored upload is reused for a fresh attempt."""
    sha = hashlib.sha256(content).hexdigest()
    try:
        with c.transaction():
            return c.execute(sql.INSERT_UPLOAD, (period_id, kind, filename, text, sha)).fetchone()[0]
    except psycopg.errors.UniqueViolation:
        upload_id, last_status = c.execute(sql.EXISTING_UPLOAD, (period_id, kind, sha)).fetchone()
        if last_status == "FAILED":
            return upload_id
        raise ApiError(400, "this file was already uploaded for this period")


def upload_2b(ctx):
    period_id = ctx.path("period_id")
    filename, content = _file(ctx)
    source = (ctx.form or {}).get("source") or "DRAFT_2B"
    if source not in SOURCES:
        raise ApiError(400, "source: must be one of " + ", ".join(SOURCES))
    text = _text(content)
    try:
        obj = json.loads(text)
    except ValueError:
        raise ApiError(400, "file is not valid JSON")
    validate_2b(obj)
    with tx(ctx.conn, ctx.user) as c:
        _visible_period(c, period_id)
        upload_id = _store(c, period_id, "GSTR2B", filename, content, text)
        job_id = c.execute(sql.ENQUEUE, ("IMPORT_2B", period_id, upload_id, None,
                                         json.dumps({"source": source}))).fetchone()[0]
    return 201, {"upload_id": upload_id, "job_id": job_id}


def upload_books(ctx):
    period_id = ctx.path("period_id")
    filename, content = _file(ctx)
    text = _text(content)
    rows = parse_books_csv(text)
    with tx(ctx.conn, ctx.user) as c:
        gstin = _visible_period(c, period_id)
        bad = c.execute(sql.FIRST_BAD_GSTIN,
                        (json.dumps([r["gstin_of_supplier"] for r in rows]),)).fetchone()
        if bad:
            raise ApiError(400, f'row {bad[0]}: GSTIN of Supplier "{bad[1]}" is not a valid GSTIN')
        keys = [{"i": i, "sup": r["gstin_of_supplier"],
                 "dt": DOC_TYPES[r["document_type"].casefold()],
                 "no": r["invoice_number"], "d": r["invoice_date"]} for i, r in enumerate(rows)]
        already = {row[0] for row in c.execute(sql.ALREADY_IMPORTED, (json.dumps(keys), gstin))}
        skipped = len({(k["sup"], k["dt"], k["no"], k["d"]) for k in keys if k["i"] in already})
        rows = [r for i, r in enumerate(rows) if i not in already]
        upload_id = _store(c, period_id, "BOOKS", filename, content, text)
        # Each upload replaces rows an earlier upload left unimported (say, its job
        # failed), so one bad batch can never block the month's next import.
        c.execute(sql.CLEAR_STAGING, (period_id,))
        cols = list(BOOKS_COLUMNS.values())
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            values = ", ".join(["(%s, " + ", ".join(["%s"] * len(cols)) + ")"] * len(chunk))
            params = [v for r in chunk for v in (period_id, *(r[k] for k in cols))]
            c.execute(sql.INSERT_STAGING_PREFIX + values, params)
        job_id = c.execute(sql.ENQUEUE, ("IMPORT_BOOKS", period_id, upload_id, None, "{}")).fetchone()[0]
    return 201, {"upload_id": upload_id, "job_id": job_id, "rows": len(rows), "skipped": skipped}
