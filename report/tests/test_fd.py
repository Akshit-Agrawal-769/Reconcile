"""The FD toolkit against textbook cases with known answers."""
from report.fd import (F, bcnf_decompose, candidate_keys, chase_lossless, closure, is_bcnf,
                       minimal_cover, normal_form, preserves, project)

R5 = frozenset("ABCDE")
F5 = [F("A", "BC"), F("CD", "E"), F("B", "D"), F("E", "A")]   # Silberschatz 7.13


def test_closure():
    assert closure(frozenset("A"), F5) == R5
    assert closure(frozenset("B"), F5) == frozenset("BD")


def test_candidate_keys():
    assert sorted("".join(sorted(k)) for k in candidate_keys(R5, F5)) == ["A", "BC", "CD", "E"]


def test_minimal_cover_removes_redundancy():
    cover = minimal_cover([F("A", "BC"), F("B", "C"), F("A", "B"), F("AB", "C")])
    assert sorted(("".join(sorted(l)), "".join(sorted(r))) for l, r in cover) == [("A", "B"), ("B", "C")]


def test_minimal_cover_removes_extraneous_lhs():
    cover = minimal_cover([F("AB", "C"), F("A", "B")])
    assert sorted(("".join(sorted(l)), "".join(sorted(r))) for l, r in cover) == [("A", "B"), ("A", "C")]


def test_bcnf_and_3nf_differ():
    """R(J, K, L) with JK -> L, L -> K: in 3NF, not in BCNF (Silberschatz 'dept_advisor')."""
    R, Fs = frozenset("JKL"), [F("JK", "L"), F("L", "K")]
    assert normal_form(R, Fs) == "3NF"
    parts = bcnf_decompose(R, Fs)
    assert {frozenset(p) for p, _ in parts} == {frozenset("LK"), frozenset("JL")}
    assert chase_lossless(R, [p for p, _ in parts], Fs)[0]
    ok, lost = preserves(R, [p for p, _ in parts], Fs)
    assert not ok and lost == [F("JK", "L")]


def test_chase_detects_lossy():
    R, Fs = frozenset("ABC"), [F("A", "B")]
    assert chase_lossless(R, [frozenset("AB"), frozenset("AC")], Fs)[0]
    assert not chase_lossless(R, [frozenset("AB"), frozenset("BC")], Fs)[0]


def test_projection():
    proj = project([F("A", "B"), F("B", "C")], frozenset("AC"))
    assert F("A", "C") in proj


def test_normal_forms():
    assert normal_form(frozenset("ABCD"), [F("AB", "C"), F("A", "D")]) == "1NF"   # partial
    assert normal_form(frozenset("ABC"), [F("A", "B"), F("B", "C")]) == "2NF"     # transitive
    assert normal_form(frozenset("AB"), [F("A", "B")]) == "BCNF"
    assert is_bcnf(frozenset("AB"), [F("A", "B")])
