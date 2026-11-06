"""Python copies of the three rules the database enforces, used by the CLI and by
check.py to confirm the MariaDB functions give the same answers as PostgreSQL's."""
import datetime as _dt
import re

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FY = re.compile(r"((?:^|[^A-Z0-9])((?:20)?[0-9]{2})[-/]((?:20)?[0-9]{2}))(?=$|[^A-Z0-9])")


def gstin_ok(g: str) -> bool:
    """GSTIN check character: mod-36 over the first 14 characters (CBIC algorithm)."""
    if not isinstance(g, str) or len(g) != 15:
        return False
    total = 0
    for i, ch in enumerate(g[:14], start=1):
        v = ALPHABET.find(ch)
        if v < 0:
            return False
        p = v * (1 if i % 2 == 1 else 2)
        total += p // 36 + p % 36
    return ALPHABET.find(g[14]) == (36 - total % 36) % 36


def norm_doc_no(d: str) -> str:
    """Invoice number for matching: upper-case, financial-year segments such as 26-27
    or 2026-2027 removed, punctuation removed, leading zeros of each number removed."""
    rest, out = d.upper(), ""
    while True:
        m = _FY.search(rest)
        if not m:
            break
        out += rest[:m.start(1)]
        if (int(m.group(2)[-2:]) + 1) % 100 == int(m.group(3)[-2:]):
            lead = m.group(1)[0]
            out += "" if lead.isdigit() else lead
        else:
            out += m.group(1)
        rest = rest[m.end(1):]
    s = re.sub(r"[^A-Z0-9]", "", out + rest)
    s = re.sub(r"(^|[A-Z])0+([0-9])", r"\1\2", s)
    return s or "0"


def fin_year_of(d) -> str:
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"
