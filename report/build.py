"""Build the report: computed tables + figures + section text -> .docx -> .pdf.

    python -m report.build            # writes report/out/Reconcile_DBMS_Report.docx and .pdf

Every table in sections 5-8 is computed here from report/relations.py with report/fd.py,
so the report cannot drift from the toolkit or from xampp/schema.sql.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from report import figures, relations as rel
from report.fd import candidate_keys, chase_lossless, closure, minimal_cover, normal_form, project, show

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NAME = "Reconcile_DBMS_Report"


def fmt_attrs(a):
    return ", ".join(sorted(a))


def fmt_key(keys):
    return "; ".join("{" + ", ".join(sorted(k)) + "}" for k in sorted(keys, key=lambda k: (len(k), sorted(k))))


def grouped_cover(fds):
    """Minimal cover written with one line per left side (right sides listed together)."""
    groups = {}
    for lhs, rhs in minimal_cover(fds):
        groups.setdefault(lhs, set()).update(rhs)
    return [f"{', '.join(sorted(l))} → {', '.join(sorted(r))}" for l, r in
            sorted(groups.items(), key=lambda kv: (len(kv[0]), sorted(kv[0])))]


def fragment_rows(w, stage):
    rows = [["Relation", "Attributes", "Candidate key(s)", "Dependencies that hold", "Normal form"]]
    for name, attrs in w.stages[stage].items():
        f = rel.projected(w, attrs)
        deps = "; ".join(grouped_cover(f)) or "none"
        rows.append([name, fmt_attrs(attrs), fmt_key(candidate_keys(attrs, f)), deps,
                     normal_form(attrs, f)])
    return rows


A_MEANING = [
    "A GSTIN belongs to one PAN (characters 3 to 12)",
    "A PAN is one client, with one name",
    "A supplier's GSTIN gives its trade name",
    "An invoice has one date and one GSTR-2B tax figure",
    "A date falls in one return month",
    "A line of an invoice has one HSN, rate, value and tax",
]


def schema_fks():
    text = (ROOT / "xampp" / "schema.sql").read_text()
    out = {}
    for table, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\) ENGINE", text, re.S):
        fks = re.findall(r"FOREIGN KEY \(([^)]*)\)\s+REFERENCES (\w+)", body)
        out[table] = [f"{c.replace(' ', '')} → {t}" for c, t in fks]
    return out


def tables():
    t = {}
    fks = schema_fks()
    rows = [["Table", "Primary key", "Other candidate keys", "Foreign keys"]]
    for name, r in rel.FINAL.items():
        pk = figures._pk_of(name)
        others = [k for k in candidate_keys(r.attrs, r.fds) if k != pk]
        rows.append([name, ", ".join(sorted(pk)), fmt_key(others) or "none",
                     "; ".join(fks.get(name, [])) or "none"])
    t["schema_list"] = rows

    w = rel.WORKED_A
    t["a_fds"] = [["No.", "Dependency", "What it says"]] + [
        [f"FD{i}", show(fd), A_MEANING[i - 1]] for i, fd in enumerate(w.fds, 1)]
    t["a_2nf"] = fragment_rows(w, "2NF")
    t["a_3nf"] = fragment_rows(w, "3NF")

    # binary lossless steps
    s2, s3 = w.stages["2NF"], w.stages["3NF"]
    rest1 = w.attrs - (s2["REGISTRATION"] - A("client_gstin"))
    rest2 = rest1 - (s2["SUPPLIER"] - A("supplier_gstin"))
    steps = [("PURCHASE_LINE", s2["REGISTRATION"], rest1),
             ("rest", s2["SUPPLIER"], rest2),
             ("rest", s2["INVOICE_HDR"], s2["INVOICE_LINE"]),
             ("REGISTRATION", s3["GSTIN_REG"], s3["CLIENT"]),
             ("INVOICE_HDR", s3["INVOICE"], s3["MONTH_OF"])]
    names = {frozenset(v): k for k, v in {**s2, **s3}.items()}
    brows = [["Split", "Into", "Common attributes", "They determine", "Lossless"]]
    for i, (src, r1, r2) in enumerate(steps, 1):
        common = r1 & r2
        cl = closure(common, w.fds)
        side = names.get(frozenset(r1), "R1") if cl >= r1 else names.get(frozenset(r2), "the rest")
        ok = cl >= r1 or cl >= r2
        brows.append([f"{i}. {src}", f"{names.get(frozenset(r1), 'R1')} + {names.get(frozenset(r2), 'rest')}",
                      fmt_attrs(common), f"all of {side}" if ok else "neither side", "yes" if ok else "NO"])
        assert ok
    t["a_binary"] = brows

    cols = rel.WORKED_A_ORDER
    assert set(cols) == w.attrs
    letters = [chr(ord("A") + i) for i in range(len(cols))]
    half = (len(cols) + 1) // 2
    t["a_letters"] = [["Col.", "Attribute", "Col.", "Attribute"]] + [
        [letters[i], cols[i], letters[i + half] if i + half < len(cols) else "",
         cols[i + half] if i + half < len(cols) else ""] for i in range(half)]
    order = ["GSTIN_REG", "CLIENT", "SUPPLIER", "INVOICE", "MONTH_OF", "INVOICE_LINE"]
    parts = [s3[n] for n in order]
    start = [[f"a{c}" if c in p else f"b{i}{c}" for c in cols] for i, p in enumerate(parts, 1)]
    ok, end = chase_lossless(w.attrs, parts, w.fds)
    # chase_lossless works in sorted column order; print in the text's A-N order.
    pos = [sorted(w.attrs).index(c) for c in cols]
    end = [[r[i] for i in pos] for r in end]
    assert ok and all(v.startswith("a") for v in end[-1]), "INVOICE_LINE row must become all a"

    def tableau(rows):
        def sym(v):
            return "a" if v.startswith("a") else re.match(r"b\d+", v).group(0)
        return [["Relation"] + letters] + [[n] + [sym(v) for v in r] for n, r in zip(order, rows)]
    t["a_chase_start"] = tableau(start)
    t["a_chase_end"] = tableau(end)
    # The narrative's order of FDs uses letters; check the letters the text names.
    L = dict(zip(cols, letters))
    assert (L["client_gstin"], L["client_pan"], L["client_name"], L["supplier_gstin"], L["supplier_name"],
            L["invoice_no"], L["invoice_date"], L["return_period"], L["portal_tax"]) == tuple("ABCDEFGHN"), L

    t["a_preserve"] = [["Dependency", "Checked inside", "How"]] + [
        [show(fd), next(n for n, a in s3.items() if fd[0] | fd[1] <= a),
         "primary key" if fd[0] == candidate_keys(next(a for a in s3.values() if fd[0] | fd[1] <= a),
                                                  project(w.fds, next(a for a in s3.values() if fd[0] | fd[1] <= a)))[0]
         else "key of the relation"] for fd in w.fds]

    b = rel.WORKED_B
    t["b_fds"] = [["No.", "Dependency", "What it says"],
                  ["FD1", show(b.fds[0]), "A client has one person per return type"],
                  ["FD2", show(b.fds[1]), "A person handles one return type and has one phone"]]
    t["b_stages"] = fragment_rows(b, "3NF")
    from report.fd import preserves
    okp, lost = preserves(b.attrs, list(b.stages["BCNF"].values()), b.fds)
    assert not okp
    lost_s = {show(f) for f in lost}
    t["b_preserve"] = [["Dependency (minimal cover)", "Preserved by STAFF + CLIENT_STAFF?"]] + [
        [show(f), "no: needs a join" if show(f) in lost_s else "yes, in STAFF"] for f in minimal_cover(b.fds)]

    rows = [["Table", "Minimal cover", "Candidate keys", "NF"]]
    for name, r in rel.FINAL.items():
        cover = grouped_cover(r.fds) or ["none (all-key relation)"]
        label = name + (f" (derived: {', '.join(sorted(r.derived))})" if r.derived else "")
        rows.append([label, "\n".join(cover), fmt_key(candidate_keys(r.attrs, r.fds)),
                     normal_form(r.attrs, r.fds)])
    t["final_cover"] = rows
    return t


def A(s):
    return frozenset(s.split())


def soffice_pdf(docx, out_dir):
    env = dict(os.environ, HOME=str(out_dir))   # private LibreOffice profile
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)],
                   check=True, capture_output=True, env=env, timeout=240)
    return out_dir / (docx.stem + ".pdf")


HEADING = re.compile(r"^#\s+(.*)$", re.M)


def heading_pages(pdf, headings):
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                          check=True).stdout
    pages = text.split("\f")
    found = {}
    for h in headings:
        for i, p in enumerate(pages[2:], start=3):
            if re.search(rf"^\s*{re.escape(h)}\s*$", p, re.M):
                found[h] = i - 2          # body page numbers start after cover + index
                break
    return found


def build(out_dir=None):
    out = Path(out_dir or HERE / "out")
    out.mkdir(parents=True, exist_ok=True)
    figs = figures.build(out / "fig")
    (out / "tables.json").write_text(json.dumps(tables(), ensure_ascii=False, indent=1))
    headings = [m.group(1).strip() for f in sorted((HERE / "sections").glob("*.md"))
                for m in HEADING.finditer(f.read_text(encoding="utf-8"))]
    pages = {}
    node = shutil.which("node") or "node"
    for _ in range(3):     # later passes writes the real page numbers into the index
        (out / "index.json").write_text(json.dumps(pages))
        subprocess.run([node, str(HERE / "build.mjs"), str(out), str(out / f"{NAME}.docx")],
                       check=True, env=dict(os.environ))
        pdf = soffice_pdf(out / f"{NAME}.docx", out)
        new = heading_pages(pdf, headings)
        if new == pages:
            break
        pages = new
    return out / f"{NAME}.docx", pdf


if __name__ == "__main__":
    d, p = build(sys.argv[1] if len(sys.argv) > 1 else None)
    print(d)
    print(p)
