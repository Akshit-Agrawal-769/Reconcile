"""Report figures, drawn with Graphviz `dot`.

    er_chen        the ER model in Chen notation
    schema         the relational schema: tables, keys and foreign keys
    decomposition  worked example A, from the Excel sheet to BCNF
    state_machine  the month's states and who moves it
"""
import re
import subprocess
from pathlib import Path

from report import relations

FONT = "DejaVu Sans"

# ------------------------------------------------------------------ ER model (Chen)
# entity: (weak?, [(attribute, kind)])  kind: key | partial | plain | derived | multi
ER_ENTITIES = {
    "FIRM": (False, [("firm_id", "key"), ("name", "plain")]),
    "CLIENT": (False, [("client_id", "key"), ("pan", "plain"), ("name", "plain"),
                       ("approval_threshold", "plain")]),
    "GSTIN_REG": (False, [("gstin", "key"), ("taxpayer_type", "plain"), ("state", "derived")]),
    "USER": (False, [("user_id", "key"), ("email", "plain"), ("role", "plain")]),
    "SUPPLIER": (False, [("gstin", "key"), ("name", "plain")]),
    "RETURN_PERIOD": (True, [("period", "partial"), ("state", "plain")]),
    "INVOICE": (False, [("invoice_id", "key"), ("invoice_no", "plain"), ("invoice_date", "plain"),
                        ("fin_year", "derived")]),
    "INVOICE_LINE": (True, [("line_no", "partial"), ("hsn", "plain"), ("taxable_value", "plain"),
                            ("tax", "multi")]),
    "GSTR2B_IMPORT": (True, [("version", "partial"), ("source", "plain")]),
    "GSTR2B_ENTRY": (False, [("entry_id", "key"), ("invoice_no", "plain"), ("tax", "multi")]),
    "REVIEW": (False, [("review_id", "key"), ("decision", "plain"), ("ims_action", "plain"),
                       ("state", "plain")]),
    "FOLLOWUP": (False, [("followup_id", "key"), ("channel", "plain"), ("response", "plain")]),
    "FILING": (True, [("return_type", "partial"), ("filed_on", "plain")]),
}

# (relationship, entity A, card A, entity B, card B, identifying?, [attributes])
ER_RELATIONSHIPS = [
    ("SERVES", "FIRM", "1", "CLIENT", "N", False, []),
    ("EMPLOYS", "FIRM", "1", "USER", "N", False, []),
    ("ASSIGNED", "USER", "M", "CLIENT", "N", False, []),
    ("REGISTERS", "CLIENT", "1", "GSTIN_REG", "N", False, []),
    ("FILES_FOR", "GSTIN_REG", "1", "RETURN_PERIOD", "N", True, []),
    ("BUYS", "GSTIN_REG", "1", "INVOICE", "N", False, []),
    ("ISSUES", "SUPPLIER", "1", "INVOICE", "N", False, []),
    ("HAS_LINE", "INVOICE", "1", "INVOICE_LINE", "N", True, []),
    ("RECEIVES", "RETURN_PERIOD", "1", "GSTR2B_IMPORT", "N", True, []),
    ("LISTS", "GSTR2B_IMPORT", "1", "GSTR2B_ENTRY", "N", False, []),
    ("REPORTS", "SUPPLIER", "1", "GSTR2B_ENTRY", "N", False, []),
    ("MATCHES", "INVOICE", "1", "GSTR2B_ENTRY", "1", False, ["status", "diff_tax"]),
    ("DECIDES", "REVIEW", "N", "INVOICE", "1", False, []),
    ("PROPOSES", "USER", "1", "REVIEW", "N", False, []),
    ("CHASES", "FOLLOWUP", "N", "SUPPLIER", "1", False, []),
    ("TRACKS", "GSTIN_REG", "1", "FILING", "N", True, []),
]


def _attr_label(name, kind):
    if kind == "key":
        return f"<<U>{name}</U>>"
    if kind == "partial":
        return f"<<U>{name}</U>>"     # drawn dashed (partial key of a weak entity)
    return f'"{name}"'


def er_chen_dot():
    out = ["graph ER {",
           f'  graph [layout=neato, overlap=false, sep="+14", splines=true, pad=0.3, '
           f'fontname="{FONT}", dpi=300, start=7, epsilon=0.0001];',
           f'  node [fontname="{FONT}", fontsize=11];',
           f'  edge [fontname="{FONT}", fontsize=14];']
    for ent, (weak, attrs) in ER_ENTITIES.items():
        extra = ", peripheries=2" if weak else ""
        out.append(f'  "{ent}" [shape=box, style=filled, fillcolor="#e8eef9", fontsize=17{extra}];')
        for a, kind in attrs:
            node = f"{ent}.{a}"
            style = {"derived": ", style=dotted, penwidth=1.6", "partial": ", style=dashed",
                     "multi": ', peripheries=2'}.get(kind, "")
            out.append(f'  "{node}" [shape=ellipse, label={_attr_label(a, kind)}, fontsize=14, '
                       f'height=0.3, margin="0.03,0.01"{style}];')
            out.append(f'  "{ent}" -- "{node}" [len=0.9];')
    for rname, a, ca, b, cb, ident, attrs in ER_RELATIONSHIPS:
        node = f"R_{rname}"
        extra = ", peripheries=2" if ident else ""
        out.append(f'  "{node}" [shape=diamond, label="{rname.replace("_", " ")}", style=filled, '
                   f'fillcolor="#fdf3dc", fontsize=14{extra}];')
        b_style = ', penwidth=2.2' if ident else ""
        out.append(f'  "{a}" -- "{node}" [label="{ca}", len=1.6];')
        out.append(f'  "{node}" -- "{b}" [label="{cb}", len=1.6{b_style}];')
        for at in attrs:
            an = f"{node}.{at}"
            out.append(f'  "{an}" [shape=ellipse, label="{at}", fontsize=14, height=0.3];')
            out.append(f'  "{node}" -- "{an}" [len=0.8];')
    out.append("}")
    return "\n".join(out)


# ------------------------------------------------------------------ relational schema
FOREIGN_KEYS = [
    ("client", "firm"), ("gstin_reg", "client"), ("app_user", "firm"), ("app_user", "client"),
    ("staff_assignment", "app_user"), ("staff_assignment", "client"), ("return_period", "gstin_reg"),
    ("filing", "gstin_reg"), ("filing", "return_calendar"), ("invoice", "gstin_reg"),
    ("invoice", "party"), ("invoice_line", "invoice"), ("gstr2b_import", "return_period"),
    ("gstr2b_entry", "gstr2b_import"), ("gstr2b_entry", "party"), ("recon_result", "gstr2b_import"),
    ("recon_result", "invoice"), ("recon_result", "gstr2b_entry"), ("review", "gstin_reg"),
    ("review", "party"), ("review", "app_user"), ("followup", "return_period"),
    ("followup", "party"),
]


def _pk_of(table):
    """The PRIMARY KEY columns as declared in xampp/schema.sql."""
    text = (Path(__file__).resolve().parents[1] / "xampp" / "schema.sql").read_text()
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\) ENGINE", text, re.S).group(1)
    cols = re.search(r"PRIMARY KEY \(([^)]*)\)", body).group(1)
    return frozenset(c.strip() for c in cols.split(","))


def _ordered_columns(table):
    text = (Path(__file__).resolve().parents[1] / "xampp" / "schema.sql").read_text()
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\) ENGINE", text, re.S).group(1)
    cols = []
    for line in body.splitlines():
        m = re.match(r"\s+([a-z_0-9]+)\s+[A-Z]", line)
        if m and m.group(1) not in ("PRIMARY", "UNIQUE", "KEY", "CONSTRAINT"):
            cols.append(m.group(1))
    return cols


def schema_dot():
    out = ["digraph schema {",
           f'  graph [rankdir=LR, nodesep=0.25, ranksep=0.9, fontname="{FONT}", dpi=300, pad=0.2];',
           f'  node [shape=plaintext, fontname="{FONT}", fontsize=13];',
           '  edge [arrowhead=crow, arrowtail=none, color="#5a6b85"];']
    for table in relations.FINAL:
        pk = _pk_of(table)
        derived = relations.FINAL[table].derived
        rows = [f'<TR><TD BGCOLOR="#1d2838" ALIGN="LEFT"><FONT COLOR="white"><B>{table}</B></FONT></TD></TR>']
        for c in _ordered_columns(table):
            label = f"<U>{c}</U>" if c in pk else (f"<I>{c}</I>" if c in derived else c)
            rows.append(f'<TR><TD ALIGN="LEFT" PORT="{c}">{label}</TD></TR>')
        out.append(f'  {table} [label=<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" '
                   f'CELLPADDING="3" COLOR="#9fbfa6">{"".join(rows)}</TABLE>>];')
    for child, parent in FOREIGN_KEYS:
        out.append(f"  {parent} -> {child};")
    out.append("}")
    return "\n".join(out)


# ------------------------------------------------------------------ worked example tree
def decomposition_dot():
    w = relations.WORKED_A
    out = ["digraph D {",
           f'  graph [rankdir=LR, fontname="{FONT}", dpi=300, nodesep=0.18, ranksep=0.5, pad=0.2];',
           f'  node [shape=box, style="rounded,filled", fillcolor="#f5f7f3", fontname="{FONT}", fontsize=10];',
           '  edge [color="#5a6b85"];',
           '  UNF [label="Purchase register sheet (UNF)\\nrepeating group of invoice lines", fillcolor="#f9e3e3"];',
           '  N1 [label="1NF: one row per invoice line\\nkey: client_gstin, supplier_gstin, invoice_no, line_no", fillcolor="#fbf0d9"];',
           "  UNF -> N1;"]

    def label(name, attrs):
        return f'{name}\\n' + "\\n".join(sorted(attrs))
    for name, attrs in w.stages["2NF"].items():
        out.append(f'  "2_{name}" [label="{label(name, attrs)}", fillcolor="#e6ebf8"];')
        out.append(f'  N1 -> "2_{name}";')
    parent = {"GSTIN_REG": "REGISTRATION", "CLIENT": "REGISTRATION", "SUPPLIER": "SUPPLIER",
              "INVOICE": "INVOICE_HDR", "MONTH_OF": "INVOICE_HDR", "INVOICE_LINE": "INVOICE_LINE"}
    for name, attrs in w.stages["3NF"].items():
        out.append(f'  "3_{name}" [label="{label(name, attrs)}", fillcolor="#e3f1e7"];')
        out.append(f'  "2_{parent[name]}" -> "3_{name}";')
    out += ['  { rank=same; ' + "; ".join(f'"2_{n}"' for n in w.stages["2NF"]) + "; }",
            '  { rank=same; ' + "; ".join(f'"3_{n}"' for n in w.stages["3NF"]) + "; }", "}"]
    return "\n".join(out)


def state_machine_dot():
    return "\n".join([
        "digraph S {",
        f'  graph [rankdir=LR, fontname="{FONT}", dpi=300, pad=0.2, nodesep=0.5];',
        f'  node [shape=box, style="rounded,filled", fillcolor="#e6ebf8", fontname="{FONT}", fontsize=11];',
        f'  edge [fontname="{FONT}", fontsize=9, color="#5a6b85"];',
        "  OPEN -> IN_REVIEW [label=\"first 2B import\\n(system)\"];",
        "  IN_REVIEW -> AWAITING_APPROVAL [label=\"submit\\n(staff)\"];",
        "  AWAITING_APPROVAL -> IN_REVIEW [label=\"send back\\n(staff)\"];",
        "  AWAITING_APPROVAL -> CLOSED [label=\"close: client approved,\\nno pending reviews (partner)\"];",
        "  CLOSED -> IN_REVIEW [label=\"reopen with reason\\n(partner)\", style=dashed];",
        '  CLOSED [fillcolor="#e3f1e7"];',
        "}"])


def render(dot_source, path):
    engine = "neato" if "layout=neato" in dot_source else "dot"
    subprocess.run([engine, "-Tpng", "-o", str(path)], input=dot_source.encode(), check=True)
    return Path(path)


def build(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "er_chen": render(er_chen_dot(), out_dir / "er_chen.png"),
        "schema": render(schema_dot(), out_dir / "schema.png"),
        "decomposition": render(decomposition_dot(), out_dir / "decomposition.png"),
        "state_machine": render(state_machine_dot(), out_dir / "state_machine.png"),
    }


if __name__ == "__main__":
    for k, v in build(Path(__file__).parent / "out" / "fig").items():
        print(k, v)
