#!/usr/bin/env python3
"""Consistency check and provable ranges for disordered-site occupancies.

All unknown occupancies are a SINGLE shared assignment: every constraint
refers to the same variables, so a value inferred in one equation is the
same in all others (no per-question re-assignment).

Method: exact rational Fourier-Motzkin elimination.
  * feasibility: derives 0 <= c with c < 0 iff the system is inconsistent;
    the reported conflict set names the original constraints involved
  * provable range of each unknown: eliminate every other variable, then
    read off the tightest lower/upper bounds implied by ALL constraints
    jointly (intersected with the physical domain 0 <= x <= 1)

Input: a JSON spec, e.g.
{
  "variables": ["xA", "xB", "xC"],
  "constraints": [
    {"label": "total",  "terms": {"xA": 1, "xB": 1, "xC": 1}, "sense": "==", "rhs": 1},
    {"label": "excl-1", "terms": {"xA": 1, "xB": 1},          "sense": "<=", "rhs": "3/5"},
    {"label": "fix-A",  "terms": {"xA": 1},                   "sense": ">=", "rhs": "1/4"}
  ]
}
Numbers may be given as "p/q" strings for exactness.
"""

import json
import sys
from fractions import Fraction


def to_frac(value):
    if isinstance(value, str):
        return Fraction(value)
    return Fraction(value)


class Ineq:
    """sum(coeffs[v] * v) <= rhs, carrying provenance (set of source labels)."""

    def __init__(self, coeffs, rhs, sources):
        self.coeffs = {v: c for v, c in coeffs.items() if c != 0}
        self.rhs = rhs
        self.sources = frozenset(sources)

    def __repr__(self):
        body = " + ".join(f"{c}*{v}" for v, c in sorted(self.coeffs.items()))
        return f"{body or '0'} <= {self.rhs}  {sorted(self.sources)}"


def normalize(spec):
    ineqs = []
    for c in spec["constraints"]:
        terms = {v: to_frac(k) for v, k in c["terms"].items()}
        rhs = to_frac(c["rhs"])
        label = c["label"]
        if c["sense"] == "<=":
            ineqs.append(Ineq(terms, rhs, {label}))
        elif c["sense"] == ">=":
            ineqs.append(Ineq({v: -k for v, k in terms.items()}, -rhs, {label}))
        elif c["sense"] == "==":
            ineqs.append(Ineq(terms, rhs, {label}))
            ineqs.append(Ineq({v: -k for v, k in terms.items()}, -rhs, {label}))
        else:
            raise ValueError(f"bad sense {c['sense']!r} in {label!r}")
    for v in spec["variables"]:  # physical domain 0 <= x <= 1
        ineqs.append(Ineq({v: Fraction(1)}, Fraction(1), {f"domain:{v}<=1"}))
        ineqs.append(Ineq({v: Fraction(-1)}, Fraction(0), {f"domain:{v}>=0"}))
    return ineqs


def eliminate(ineqs, var):
    """Fourier-Motzkin elimination of `var`; returns (remaining, contradiction)."""
    pos = [i for i in ineqs if i.coeffs.get(var, 0) > 0]
    neg = [i for i in ineqs if i.coeffs.get(var, 0) < 0]
    rest = [i for i in ineqs if i.coeffs.get(var, 0) == 0]
    out = list(rest)
    for p in pos:
        for n in neg:
            a, b = p.coeffs[var], -n.coeffs[var]  # a,b > 0
            coeffs = {}
            for v, c in p.coeffs.items():
                coeffs[v] = coeffs.get(v, Fraction(0)) + b * c
            for v, c in n.coeffs.items():
                coeffs[v] = coeffs.get(v, Fraction(0)) + a * c
            coeffs.pop(var, None)
            out.append(Ineq(coeffs, b * p.rhs + a * n.rhs, p.sources | n.sources))
    for i in out:
        if not i.coeffs and i.rhs < 0:
            return out, i
    return out, None


def check(spec):
    variables = list(spec["variables"])
    base = normalize(spec)

    # Feasibility: eliminate everything.
    ineqs, contradiction = list(base), None
    for v in variables:
        ineqs, contradiction = eliminate(ineqs, v)
        if contradiction:
            break
    if contradiction:
        return {"feasible": False, "conflict": sorted(contradiction.sources)}

    # Provable range per variable: eliminate all OTHER variables.
    ranges = {}
    for target in variables:
        ineqs = list(base)
        for v in variables:
            if v != target:
                ineqs, _ = eliminate(ineqs, v)
        lo, hi = None, None
        for i in ineqs:
            c = i.coeffs.get(target)
            if c is None:
                continue
            bound = i.rhs / c
            if c > 0:
                hi = bound if hi is None else min(hi, bound)
            else:
                lo = bound if lo is None else max(lo, bound)
        ranges[target] = (lo, hi)
    return {"feasible": True, "ranges": ranges}


def fmt(value):
    if value is None:
        return "-inf"
    return f"{float(value):.6g} ({value})" if value.denominator != 1 else str(value)


def main():
    spec = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else DEMO
    result = check(spec)
    if not result["feasible"]:
        print("INFEASIBLE: no single assignment satisfies all constraints.")
        print("Contradictory constraint group:", ", ".join(result["conflict"]))
        sys.exit(1)
    print("FEASIBLE: one shared assignment exists. Provable ranges:")
    for v, (lo, hi) in result["ranges"].items():
        print(f"  {v}: {fmt(lo)} <= {v} <= {fmt(hi)}")


DEMO = {
    "variables": ["xA", "xB", "xC"],
    "constraints": [
        {"label": "total=1", "terms": {"xA": 1, "xB": 1, "xC": 1}, "sense": "==", "rhs": 1},
        {"label": "excl-AB", "terms": {"xA": 1, "xB": 1}, "sense": "<=", "rhs": "3/4"},
        {"label": "excl-BC", "terms": {"xB": 1, "xC": 1}, "sense": "<=", "rhs": "1/2"},
        {"label": "known-A", "terms": {"xA": 1}, "sense": "==", "rhs": "1/2"},
    ],
}

if __name__ == "__main__":
    main()
