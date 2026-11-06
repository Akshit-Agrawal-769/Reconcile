#!/usr/bin/env python3
"""Synthetic books + GSTR-2B generator with labelled perturbations (stdlib only).

    python tools/synth.py --clients 3 --docs 500 --period 092026 --seed 42 --out data/synth/

Writes into --out:
    clients.json          manifest: gstin, pan, taxpayer_type, legal_name, state, ...
    books_<gstin>.csv     purchase register in the books_staging (Task 5) CSV layout
    gstr2b_<gstin>.json   GSTR-2B payload (b2b / b2ba / cdnr); not for COMPOSITION clients
    truth.csv             gstin, books_invoice_no, portal_invoice_no, perturbation,
                          expected_status

--docs is the number of documents per client. Clients cycle through REGULAR, SERVICE and
COMPOSITION; a COMPOSITION client gets books only (all lines ineligible for ITC) and no
truth rows, because sp_reconcile refuses composition dealers. For every other client the
documents follow MIX exactly (largest-remainder quotas, then shuffled) and 10% of them are
credit notes. Same seed, same arguments => byte-identical output.
"""
import argparse
import calendar
import csv
import datetime as dt
import json
import random
import re
from decimal import Decimal
from pathlib import Path

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# perturbation -> share of a client's documents (spec section 12, task 12 brief)
MIX = {
    "EXACT": 0.50,
    "FORMAT": 0.12,
    "TYPO": 0.06,
    "ROUNDING": 0.08,
    "DATE_SHIFT": 0.04,
    "VALUE": 0.05,
    "OTHER_STATE_GSTIN": 0.02,
    "BOOKS_ONLY": 0.05,
    "2B_ONLY": 0.04,
    "B2BA": 0.02,
    "DUPLICATE": 0.02,
}
# perturbation -> expected sp_reconcile status (controller ruling R13)
EXPECTED = {
    "EXACT": "EXACT",
    "FORMAT": "EXACT",
    "TYPO": "PROBABLE",
    "ROUNDING": "ROUNDING",
    "DATE_SHIFT": "ROUNDING",
    "VALUE": "MISMATCH",
    "OTHER_STATE_GSTIN": "PROBABLE",
    "BOOKS_ONLY": "ONLY_IN_BOOKS",
    "2B_ONLY": "ONLY_IN_2B",
    "B2BA": "EXACT",
    "DUPLICATE": "EXACT",  # plus a second, portal-only DUPLICATE row
}
CREDIT_NOTE_SHARE = 0.10
TAXPAYER_TYPES = ["REGULAR", "SERVICE", "COMPOSITION"]
CLIENT_STATES = ["07", "27", "29", "33", "09", "24"]
SUPPLIER_STATES = ["07", "27", "29", "33", "09", "24", "19", "06", "36", "08"]
SUPPLIERS_PER_CLIENT = 25
RATE = Decimal("18")

BOOKS_HEADER = ["GSTIN of Supplier", "Supplier Name", "Document Type", "Invoice Number",
                "Invoice date", "Place Of Supply", "Reverse Charge", "HSN/SAC", "Rate",
                "Taxable Value", "Integrated Tax Paid", "Central Tax Paid",
                "State/UT Tax Paid", "Cess Paid", "Eligible for ITC"]
TRUTH_HEADER = ["gstin", "books_invoice_no", "portal_invoice_no", "perturbation",
                "expected_status"]
CENT = Decimal("0.01")
HEADS = ("igst", "cgst", "sgst", "cess")


# ---------------------------------------------------------------- identifiers

def generate_gstin(state, pan, entity="1"):
    """state(2) + PAN(10) + entity + 'Z' + mod-36 check character (port of gstin_checksum_ok)."""
    body = f"{state}{pan}{entity}Z"
    total = 0
    for i, ch in enumerate(body, start=1):
        p = CHARS.index(ch) * (1 if i % 2 == 1 else 2)
        total += p // 36 + p % 36
    return body + CHARS[(36 - total % 36) % 36]


def random_pan(rng, kind=None):
    """AAAKA9999A: 3 letters, holder kind (C company, F firm, P person ...), letter, 4 digits, letter."""
    kind = kind or rng.choice("CFPH")
    return ("".join(rng.choice(LETTERS) for _ in range(3)) + kind + rng.choice(LETTERS)
            + f"{rng.randint(0, 9999):04d}" + rng.choice(LETTERS))


_FY_RE = re.compile(r"((?:^|[^A-Z0-9])((?:20)?[0-9]{2})[-/]((?:20)?[0-9]{2}))(?=$|[^A-Z0-9])")


def norm_doc_no(d):
    """Python mirror of the SQL norm_doc_no (003): drop consecutive-year FY segments,
    keep A-Z0-9, strip leading zeros of each digit run that starts the string or follows a letter."""
    rest, outp = d.upper(), ""
    while True:
        m = _FY_RE.search(rest)
        if not m:
            break
        outp += rest[:m.start(1)]
        if (int(m.group(2)[-2:]) + 1) % 100 == int(m.group(3)[-2:]):
            lead = m.group(1)[0]
            outp += "" if lead.isdigit() else lead
        else:
            outp += m.group(1)
        rest = rest[m.end(1):]
    s = re.sub(r"[^A-Z0-9]", "", outp + rest)
    s = re.sub(r"(^|[A-Z])0+([0-9])", r"\1\2", s)
    return s or "0"


def fin_year_of(d):
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def quotas(n, mix):
    """Largest-remainder rounding of n * share so the counts sum to n exactly."""
    raw = {k: n * v for k, v in mix.items()}
    out = {k: int(v) for k, v in raw.items()}
    left = n - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), list(mix).index(k)))[:left]:
        out[k] += 1
    return out


# ---------------------------------------------------------------- generation

class Numbers:
    """Per-client document numbers: 'INV-'/'CN-' + 6 digits, gaps >= 10, unique after norm."""

    def __init__(self, rng, month=9):
        self.rng = rng
        # Each month draws from its own block of a million numbers, so two months of the
        # same client (same FY) never reuse a number. September keeps the original range.
        base = 100000 + ((month - 9) % 12) * 1_000_000
        self.next = {"INV": base + rng.randint(0, 9999), "CN": base + rng.randint(0, 9999)}
        self.used = set()

    def take(self, prefix):
        while True:
            n = self.next[prefix]
            self.next[prefix] += self.rng.randint(10, 400)
            s = f"{prefix}-{n:06d}"
            if norm_doc_no(s) not in self.used:
                self.used.add(norm_doc_no(s))
                return s

    def typo(self, number):
        """One character edit on the digits (never the first digit, so no leading-zero
        change) whose normalised form is new for this client."""
        prefix, digits = number.split("-")
        rng = self.rng
        while True:
            op = rng.choice(["sub", "ins", "del"])
            if op == "sub":
                j = rng.randint(1, len(digits) - 1)
                d = rng.choice([c for c in "0123456789" if c != digits[j]])
                new = digits[:j] + d + digits[j + 1:]
            elif op == "ins":
                j = rng.randint(1, len(digits))
                new = digits[:j] + rng.choice("0123456789") + digits[j:]
            else:
                j = rng.randint(1, len(digits) - 1)
                new = digits[:j] + digits[j + 1:]
            s = f"{prefix}-{new}"
            if norm_doc_no(s) not in self.used:
                self.used.add(norm_doc_no(s))
                return s


def reformat(rng, number, doc_date):
    """Same document number written differently; normalises to the same value."""
    prefix, digits = number.split("-")
    fy = fin_year_of(doc_date)
    fy_short = f"{fy[2:4]}-{fy[5:7]}"
    variants = [f"{prefix}/{digits}", f"{prefix}{digits}", f"{prefix}-00{digits}",
                f"{prefix}/{fy_short}/{digits}", f"{prefix} {digits}",
                f"{prefix.lower()}/{digits}"]
    s = rng.choice(variants)
    assert norm_doc_no(s) == norm_doc_no(number), (s, number)
    return s


def money(rng, lo, hi):
    return Decimal(rng.randint(int(lo * 100), int(hi * 100))) / 100


def make_lines(rng, intra, hsn):
    lines = []
    for _ in range(rng.choice([1, 1, 1, 2, 2, 3])):
        tv = money(rng, 500, 250000)
        if intra:
            half = (tv * RATE / 200).quantize(CENT)
            heads = {"igst": Decimal("0"), "cgst": half, "sgst": half, "cess": Decimal("0")}
        else:
            heads = {"igst": (tv * RATE / 100).quantize(CENT), "cgst": Decimal("0"),
                     "sgst": Decimal("0"), "cess": Decimal("0")}
        lines.append({"hsn": hsn, "txval": tv, **heads})
    return lines


def doc_totals(lines):
    return {k: sum((ln[k] for ln in lines), Decimal("0")) for k in ("txval",) + HEADS}


def make_client(rng, idx, used_pans):
    ttype = TAXPAYER_TYPES[idx % len(TAXPAYER_TYPES)]
    state = CLIENT_STATES[idx % len(CLIENT_STATES)]
    pan = unique_pan(rng, used_pans, kind=rng.choice("CF"))
    suppliers = []
    for s in range(SUPPLIERS_PER_CLIENT):
        sp = unique_pan(rng, used_pans, kind=rng.choice("CFP"))
        st = state if rng.random() < 0.4 else rng.choice([x for x in SUPPLIER_STATES if x != state])
        suppliers.append({"pan": sp, "state": st, "gstin": generate_gstin(st, sp),
                          "name": f"Supplier {idx + 1}-{s + 1:02d} {sp[:3].title()} Traders"})
    return {"gstin": generate_gstin(state, pan), "pan": pan, "taxpayer_type": ttype,
            "legal_name": f"Synth Client {idx + 1} ({ttype.title()})", "state": state,
            "filing_freq": "MONTHLY", "suppliers": suppliers}


def unique_pan(rng, used, kind):
    while True:
        p = random_pan(rng, kind)
        if p not in used:
            used.add(p)
            return p


def client_docs(rng, client, n, period):
    """Return (books_docs, portal_docs, truth_rows) for one client."""
    month, year = int(period[:2]), int(period[2:])
    days = calendar.monthrange(year, month)[1]
    comp = client["taxpayer_type"] == "COMPOSITION"
    hsn = "998314" if client["taxpayer_type"] == "SERVICE" else "8471"
    numbers = Numbers(rng, month)

    if comp:
        perts = ["BOOKS_ONLY"] * n
    else:
        perts = [k for k, c in quotas(n, MIX).items() for _ in range(c)]
        rng.shuffle(perts)
    eligible_cn = [i for i, p in enumerate(perts) if p != "B2BA"]  # no cdnra section
    cn_idx = set(rng.sample(eligible_cn, round(n * CREDIT_NOTE_SHARE)))

    books, portal, truth = [], [], []
    g = client["gstin"]
    for i, pert in enumerate(perts):
        sup = rng.choice(client["suppliers"])
        is_cn = i in cn_idx
        number = numbers.take("CN" if is_cn else "INV")
        date = dt.date(year, month, rng.randint(1, days))
        intra = sup["state"] == client["state"]
        lines = make_lines(rng, intra, hsn)
        tot = doc_totals(lines)
        bdoc = {"sup": sup, "doc_type": "CRN" if is_cn else "INV", "number": number,
                "date": date, "pos": client["state"], "lines": lines,
                "itc": "No" if comp else "Yes"}
        pdoc = {"ctin": sup["gstin"], "trdnm": sup["name"], "number": number, "date": date,
                "section": "cdnr" if is_cn else "b2b", **tot}
        if comp:
            books.append(bdoc)
            continue

        in_books, pdocs = True, [pdoc]
        exp = EXPECTED[pert]
        bno, pno = number, number
        if pert == "FORMAT":
            pdoc["number"] = pno = reformat(rng, number, date)
        elif pert == "TYPO":
            pdoc["number"] = pno = numbers.typo(number)
        elif pert == "ROUNDING":
            head = rng.choice(["cgst", "sgst"]) if intra else "igst"
            pdoc[head] += Decimal(rng.randint(10, 90)) / 100 * rng.choice([1, -1])
        elif pert == "DATE_SHIFT":
            delta = rng.randint(1, 5) * rng.choice([1, -1])
            shifted = date + dt.timedelta(days=delta)
            if fin_year_of(shifted) != fin_year_of(date):
                shifted = date - dt.timedelta(days=delta)
            pdoc["date"] = shifted
        elif pert == "VALUE":
            head = "cgst" if intra else "igst"
            delta = Decimal(rng.randint(5000, 50000)) / 100
            if rng.random() < 0.5 and pdoc[head] - delta >= 1:
                pdoc[head] -= delta
            else:
                pdoc[head] += delta
        elif pert == "OTHER_STATE_GSTIN":
            other = rng.choice([s for s in SUPPLIER_STATES if s != sup["state"]])
            pdoc["ctin"] = generate_gstin(other, sup["pan"])
            tax = tot["igst"] + tot["cgst"] + tot["sgst"]
            if other == client["state"]:
                half = (tax / 2).quantize(CENT)
                pdoc.update(igst=Decimal("0"), cgst=half, sgst=tax - half)
            else:
                pdoc.update(igst=tax, cgst=Decimal("0"), sgst=Decimal("0"))
        elif pert == "BOOKS_ONLY":
            pdocs, pno = [], ""
        elif pert == "2B_ONLY":
            in_books, bno = False, ""
        elif pert == "B2BA":
            pdoc.update(section="b2ba", oinum=number)
        elif pert == "DUPLICATE":
            pdocs = [pdoc, dict(pdoc)]

        if in_books:
            books.append(bdoc)
        portal.extend(pdocs)
        truth.append([g, bno, pno, pert, exp])
        if pert == "DUPLICATE":
            truth.append([g, "", pno, pert, "DUPLICATE"])
    return books, portal, truth


# ---------------------------------------------------------------- writers

def fmt(d):
    return f"{d:.2f}"


def write_books(path, books):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(BOOKS_HEADER)
        for b in books:
            for ln in b["lines"]:
                w.writerow([b["sup"]["gstin"], b["sup"]["name"],
                            "Credit Note" if b["doc_type"] == "CRN" else "Invoice",
                            b["number"], b["date"].strftime("%d-%m-%Y"), b["pos"], "N",
                            ln["hsn"], str(RATE), fmt(ln["txval"]), fmt(ln["igst"]),
                            fmt(ln["cgst"]), fmt(ln["sgst"]), fmt(ln["cess"]), b["itc"]])


def num(d):
    return float(d.quantize(CENT))


def build_2b(gstin, period, docs):
    """Same structure as db/tests/factories.build_2b_payload (document-level amounts)."""
    sections = {"b2b": {}, "b2ba": {}, "cdnr": {}}
    names = {}
    for d in docs:
        amt = {k: num(d[k]) for k in HEADS}
        val = num(d["txval"] + sum(d[k] for k in HEADS))
        names[d["ctin"]] = d["trdnm"]
        dtxt = d["date"].strftime("%d-%m-%Y")
        if d["section"] == "cdnr":
            row = {"ntnum": d["number"], "ntty": "C", "dt": dtxt, "val": val,
                   "txval": num(d["txval"]), **amt}
        else:
            row = {"inum": d["number"], "dt": dtxt, "val": val, "pos": gstin[:2], "rev": "N",
                   "itcavl": "Y", "txval": num(d["txval"]), **amt}
            if d["section"] == "b2ba":
                row["oinum"] = d["oinum"]
        sections[d["section"]].setdefault(d["ctin"], []).append(row)

    def grp(m, key):
        return [{"ctin": c, "trdnm": names[c], key: rows} for c, rows in m.items()]

    return {"data": {"gstin": gstin, "rtnprd": period, "docdata": {
        "b2b": grp(sections["b2b"], "inv"), "b2ba": grp(sections["b2ba"], "inv"),
        "cdnr": grp(sections["cdnr"], "nt")}}}


def generate(clients=3, docs=500, period="092026", seed=42, out="data/synth"):
    if not re.fullmatch(r"(0[1-9]|1[0-2])[0-9]{4}", period):
        raise ValueError(f"period must be MMYYYY: {period}")
    rng = random.Random(seed)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    used_pans = set()
    manifest, all_truth = [], []
    for idx in range(clients):
        c = make_client(rng, idx, used_pans)
        books, portal, truth = client_docs(rng, c, docs, period)
        books_file = f"books_{c['gstin']}.csv"
        write_books(out / books_file, books)
        gfile = None
        if c["taxpayer_type"] != "COMPOSITION":
            gfile = f"gstr2b_{c['gstin']}.json"
            (out / gfile).write_text(json.dumps(build_2b(c["gstin"], period, portal), indent=1)
                                     + "\n")
        all_truth.extend(truth)
        manifest.append({k: c[k] for k in ("gstin", "pan", "taxpayer_type", "legal_name",
                                            "state", "filing_freq")}
                        | {"books_file": books_file, "gstr2b_file": gfile,
                           "books_docs": len(books), "portal_docs": len(portal)})
    (out / "clients.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with open(out / "truth.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(TRUTH_HEADER)
        w.writerows(all_truth)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clients", type=int, default=3)
    ap.add_argument("--docs", type=int, default=500, help="documents per client")
    ap.add_argument("--period", default="092026", help="return period MMYYYY")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/synth/")
    a = ap.parse_args(argv)
    manifest = generate(a.clients, a.docs, a.period, a.seed, a.out)
    for c in manifest:
        print(f"{c['gstin']} {c['taxpayer_type']:<11} books={c['books_docs']} "
              f"portal={c['portal_docs']}")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
