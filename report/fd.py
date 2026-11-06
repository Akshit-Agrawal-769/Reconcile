"""Functional dependencies: the textbook algorithms, small enough to read.

Used to compute (not hand-write) every key, minimal cover, normal form, lossless-join
test and dependency-preservation result printed in the report.
"""
from itertools import combinations


def F(lhs, rhs):
    """An FD from two attribute lists: F("a b", "c") or, for one-letter attributes, F("AB", "C")."""
    def attrs(x):
        if isinstance(x, (set, frozenset, list, tuple)):
            return frozenset(x)
        if " " in x.strip():                     # "client_gstin return_type"
            return frozenset(x.split())
        if x.isalpha() and x.isupper():          # textbook style: "AB" is {A, B}
            return frozenset(x)
        return frozenset([x])                    # one named attribute
    return (attrs(lhs), attrs(rhs))


def closure(attrs, fds):
    result = set(attrs)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return frozenset(result)


def is_superkey(attrs, R, fds):
    return closure(attrs, fds) >= R


def candidate_keys(R, fds):
    R = frozenset(R)
    # attributes never on a right-hand side must be in every key
    on_rhs = set().union(*(rhs for _, rhs in fds)) if fds else set()
    core = frozenset(a for a in R if a not in on_rhs)
    rest = sorted(R - core)
    keys = []
    for n in range(len(rest) + 1):
        for extra in combinations(rest, n):
            cand = core | frozenset(extra)
            if any(k <= cand for k in keys):
                continue
            if is_superkey(cand, R, fds):
                keys.append(cand)
    return keys


def minimal_cover(fds):
    """Singleton right sides, no extraneous left-side attribute, no redundant FD."""
    fs = [(lhs, frozenset([a])) for lhs, rhs in fds for a in rhs if a not in lhs]
    # remove extraneous attributes from left sides
    out = []
    for lhs, rhs in fs:
        lhs = set(lhs)
        for a in sorted(lhs):
            if len(lhs) > 1 and rhs <= closure(frozenset(lhs - {a}), fs):
                lhs.discard(a)
        out.append((frozenset(lhs), rhs))
    out = list(dict.fromkeys(out))
    # remove redundant FDs
    i = 0
    while i < len(out):
        others = out[:i] + out[i + 1:]
        if out[i][1] <= closure(out[i][0], others):
            out = others
        else:
            i += 1
    return sorted(out, key=lambda f: (sorted(f[0]), sorted(f[1])))


def project(fds, attrs):
    """FDs implied by fds that hold on the attribute set attrs (exponential, fine for small sets)."""
    attrs = frozenset(attrs)
    result = []
    for n in range(1, len(attrs) + 1):
        for lhs in combinations(sorted(attrs), n):
            lhs = frozenset(lhs)
            rhs = (closure(lhs, fds) & attrs) - lhs
            if rhs:
                result.append((lhs, rhs))
    return minimal_cover(result)


def bcnf_violations(R, fds):
    return [(l, r) for l, r in fds if not r <= l and not is_superkey(l, R, fds)]


def is_bcnf(R, fds):
    return not bcnf_violations(frozenset(R), fds)


def is_3nf(R, fds):
    R = frozenset(R)
    prime = frozenset().union(*candidate_keys(R, fds))
    return all(is_superkey(l, R, fds) or (r - l) <= prime for l, r in minimal_cover(fds))


def is_2nf(R, fds):
    R = frozenset(R)
    keys = candidate_keys(R, fds)
    prime = frozenset().union(*keys)
    for k in keys:
        for n in range(1, len(k)):
            for part in combinations(sorted(k), n):
                if (closure(frozenset(part), fds) - prime) - frozenset(part):
                    return False
    return True


def normal_form(R, fds):
    R = frozenset(R)
    if is_bcnf(R, fds):
        return "BCNF"
    if is_3nf(R, fds):
        return "3NF"
    if is_2nf(R, fds):
        return "2NF"
    return "1NF"


def bcnf_decompose(R, fds):
    """Standard BCNF decomposition. Returns [(attrs, projected FDs)]."""
    todo, done = [(frozenset(R), minimal_cover(fds))], []
    while todo:
        r, f = todo.pop(0)
        bad = bcnf_violations(r, f)
        if not bad:
            done.append((r, f))
            continue
        lhs, _ = bad[0]
        r1 = closure(lhs, f) & r
        r2 = lhs | (r - r1)
        todo += [(r1, project(f, r1)), (r2, project(f, r2))]
    return done


def chase_lossless(R, parts, fds):
    """The tableau (chase) test. Returns (lossless?, final tableau as rows of 'a'/'b' strings)."""
    cols = sorted(R)
    rows = [[f"a{c}" if c in p else f"b{i}{c}" for c in cols] for i, p in enumerate(parts, 1)]
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            li = [cols.index(a) for a in lhs]
            for x in rows:
                for y in rows:
                    if x is y or any(x[i] != y[i] for i in li):
                        continue
                    for a in rhs:
                        j = cols.index(a)
                        if x[j] != y[j]:
                            keep = min(x[j], y[j], key=lambda v: (not v.startswith("a"), v))
                            old = y[j] if keep == x[j] else x[j]
                            for row in rows:
                                if row[j] == old:
                                    row[j] = keep
                            changed = True
    lossless = any(all(v.startswith("a") for v in row) for row in rows)
    return lossless, rows


def preserves(R, parts, fds):
    """Is every FD of the minimal cover implied by the union of the projections?
    Returns (preserved?, [lost FDs])."""
    union = [f for p in parts for f in project(fds, p)]
    lost = [(l, r) for l, r in minimal_cover(fds) if not r <= closure(l, union)]
    return not lost, lost


def show(fd):
    lhs, rhs = fd
    return f"{', '.join(sorted(lhs))} → {', '.join(sorted(rhs))}"
