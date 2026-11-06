"""The built report: length, order, wording. Builds once per test session (slow: ~20 s)."""
import re
import subprocess
from pathlib import Path

import pytest

from report import build

HEADINGS = ["Project Description", "Requirement Analysis", "ER Model", "ER to Relational Schema",
            "Normalization up to BCNF", "Lossless Decomposition Proof", "Dependency Preservation",
            "Minimal Cover of Each Table", "Implementation on XAMPP and Python"]

BANNED = ["delve", "robust", "seamless", "leverage", "comprehensive", "crucial", "plays a vital role",
          "in conclusion", "fast-paced", "cutting-edge", "game-changer", "utilize", "furthermore",
          "moreover", "it is important to note", "in today's", "unlock", "empower", "streamline",
          "elevate", "harness", "tapestry", "navigate the", "realm", "holistic", "synergy"]


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("report")
    docx, pdf = build.build(out)
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                          check=True).stdout.replace("\u200b", "")
    pages = text.split("\f")
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    n = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    return docx, pdf, pages, n


def test_report_page_count(built):
    _, _, _, n = built
    assert 28 <= n <= 32, n


def test_cover_then_index(built):
    _, _, pages, _ = built
    assert "Roll No." in pages[0] and "[Name]" in pages[0]
    assert "Netaji Subhas University of Technology" in pages[0]
    assert "Index" in pages[1] or "Contents" in pages[1]
    for h in HEADINGS:
        assert h in pages[1], h


def test_cover_has_placeholders(built):
    _, _, pages, _ = built
    for p in ("[Name]", "[Roll No.]", "[Course code]", "[Supervisor]", "[Month Year]"):
        assert p in pages[0], p


def test_every_section_heading_present_in_order(built):
    _, _, pages, _ = built
    body = "\n".join(pages[2:])
    positions = [body.find(h) for h in HEADINGS]
    assert all(p >= 0 for p in positions), dict(zip(HEADINGS, positions))
    assert positions == sorted(positions)


def test_no_banned_phrases(built):
    _, _, pages, _ = built
    text = " ".join(pages).lower()
    hits = [w for w in BANNED if re.search(rf"\b{re.escape(w)}\b", text)]
    assert not hits, hits


def test_no_em_dash_chains(built):
    _, _, pages, _ = built
    text = " ".join(pages)
    assert text.count("—") <= 8, text.count("—")


def test_figures_and_computed_tables_present(built):
    docx, _, pages, _ = built
    import zipfile
    media = [n for n in zipfile.ZipFile(docx).namelist() if n.startswith("word/media/")]
    assert len(media) >= 4
    body = " ".join(pages)
    for table in ("return_calendar", "recon_result", "review", "followup"):
        assert table in body
    assert "client_gstin, return_type → staff_email" in body   # the lost dependency, section 7


FALSE_OR_UNMEASURED = [
    "lands in the expected category",          # I4: never measured on XAMPP
    "reported by both builds",                 # I4: the PG build pairs them
    "is a reserved word",                      # minor 3: USER is not reserved in MariaDB
    "cannot take apart nested JSON",           # minor 4: overstated
    "every client's GSTR-3B for September",    # I9: not true for quarterly filers
    "the standard mapping steps from Elmasri", # I10: steps differ from the book's
    "unavoidable",                             # I5: tenancy is not unavoidable
    "honest case", "hand-worked guesses", "taught us",   # prose the reviewer flagged
    "Every procedure takes the acting user's id first and calls",   # sp_approve does not
]


def test_no_false_or_unmeasured_claims(built):
    _, _, pages, _ = built
    text = " ".join(" ".join(p.split()) for p in pages)
    hits = [p for p in FALSE_OR_UNMEASURED if p.lower() in text.lower()]
    assert not hits, hits


def test_no_words_broken_inside_table_cells(built):
    """Minor 1 (re-graded): BCNF and table names must not wrap mid-word."""
    _, _, pages, _ = built
    text = "\n".join(pages)
    for broken in ("BCN\n", "RETURN_PERIO\n", "GSTR2B_IMPOR\n", "REGISTRATIO\n", "ASSIGNMEN\n"):
        assert not re.search(rf"\b{broken.strip()}\s*$", text, re.M), broken
