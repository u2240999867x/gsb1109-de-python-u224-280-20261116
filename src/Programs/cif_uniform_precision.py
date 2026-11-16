"""
Impose a uniform number of displayed decimal places on selected numeric
CIF measurement columns.

Original values may be written in either of the lexical orders that occur in
real CIF files::

    0.0028(7)          mantissa(esd)
    2.0e-05(9)         mantissa exponent (esd)

The parenthesised standard uncertainty is expressed in units of the last
digit of the *mantissa*, and that unit is then scaled by the exponent.  It
must therefore be interpreted together with the exponent; it is never a hard
lower/upper bound on the value.  For example, ``2.0e-05(9)`` denotes the
value 2.0e-5 with standard uncertainty 9e-6.

The tool parses each original lexical value exactly (decimal arithmetic),
searches the configured decimal-place range for the smallest uniform
precision that keeps every row within the configured display-error limits
for both the value and the standard uncertainty, and emits:

* a rewritten CIF in which only the configured columns change;
* a row-by-row mapping of original to new lexical values;
* a JSON report giving the chosen precision or the rows that block a common
  precision (rows are never silently relaxed).

Unknown (``?``) and inapplicable (``.``) values are copied verbatim.  Every
emitted token is re-parsed by the same parser so that the realised errors
can be checked row by row.
"""
from __future__ import print_function

import argparse
import json
import re
from decimal import Decimal, getcontext, ROUND_HALF_EVEN, ROUND_HALF_UP
from decimal import ROUND_HALF_DOWN, ROUND_CEILING, ROUND_FLOOR, ROUND_UP

getcontext().prec = 60

ROUNDING_MODES = {
    "half-even": ROUND_HALF_EVEN,
    "half-up": ROUND_HALF_UP,
    "half-away": ROUND_HALF_UP,
    "half-down": ROUND_HALF_DOWN,
    "ceiling": ROUND_CEILING,
    "floor": ROUND_FLOOR,
    "up": ROUND_UP,
}

# Sign, mantissa, then an exponent and an esd bracket in either order.
_TOKEN_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<mant>\d+\.\d+|\d+\.|\.\d+|\d+)"
    r"(?:"
    r"(?:[eEdD](?P<exp_a>[+-]?\d+))?(?:\((?P<esd_a>\d+)\))?"
    r"|"
    r"(?:\((?P<esd_b>\d+)\))?(?:[eEdD](?P<exp_b>[+-]?\d+))?"
    r")$"
)

SPECIAL_TOKENS = (".", "?")


class NumericToken(object):
    """Exact decomposition of a CIF numeric lexical value."""

    def __init__(self, text, value, su, ulp, exponent, mant_digits):
        self.text = text
        self.value = value
        self.su = su
        self.ulp = ulp
        self.exponent = exponent
        self.mant_digits = mant_digits

    @property
    def has_su(self):
        return self.su is not None


def parse_numeric_token(text):
    """Return a NumericToken, or None for ./?/unparsable input."""
    if text in SPECIAL_TOKENS:
        return None
    match = _TOKEN_RE.match(text)
    if match is None:
        return None
    parts = match.groupdict()
    exponent_raw = parts["exp_a"] if parts["exp_a"] is not None else parts["exp_b"]
    esd_raw = parts["esd_a"] if parts["esd_a"] is not None else parts["esd_b"]
    exponent = int(exponent_raw) if exponent_raw is not None else 0
    mantissa = parts["mant"]
    if "." in mantissa:
        mant_digits = len(mantissa.split(".")[1])
    else:
        mant_digits = 0
    ulp = Decimal(1).scaleb(exponent - mant_digits)
    value = Decimal((parts["sign"] or "") + mantissa).scaleb(exponent)
    su = None
    if esd_raw is not None:
        su = Decimal(int(esd_raw)) * ulp
    return NumericToken(text, value, su, ulp, exponent, mant_digits)


def _place(decimals):
    return Decimal(1).scaleb(-decimals)


def _tolerance_bound(spec, parsed, decimals):
    """Resolve a tolerance entry to an absolute Decimal bound."""
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        return Decimal(str(spec))
    text = str(spec).strip().lower()
    if text.endswith("ulp_original"):
        factor = Decimal(text.split("ulp_original")[0].strip() or "1")
        return factor * parsed.ulp
    if text == "ulp_display":
        return _place(decimals)
    if text.endswith("ulp_display"):
        factor = Decimal(text.split("ulp_display")[0].strip() or "1")
        return factor * _place(decimals)
    return Decimal(text)


def round_token(parsed, decimals, rounding_mode):
    """Round value and su to fixed-point decimals; return (value_q, su_q)."""
    mode = ROUNDING_MODES[rounding_mode]
    place = _place(decimals)
    value_q = parsed.value.quantize(place, rounding=mode)
    if value_q == 0:
        value_q = Decimal(0).quantize(place)
    su_q = None
    if parsed.has_su:
        su_q = parsed.su.quantize(place, rounding=mode)
    return value_q, su_q


def format_fixed(value_q, su_q, decimals):
    """Emit a fixed-point lexical value with unified decimal places."""
    text = format(value_q, "f")
    if su_q is not None:
        bracket = int((su_q / _place(decimals)).to_integral_value())
        text += "({0})".format(bracket)
    return text


def _check_row(parsed, decimals, rounding_mode, value_spec, su_spec):
    """Round one row and return the realised errors and pass/fail flags."""
    value_q, su_q = round_token(parsed, decimals, rounding_mode)
    new_text = format_fixed(value_q, su_q, decimals)
    reparsed = parse_numeric_token(new_text)
    value_error = abs(reparsed.value - parsed.value)
    su_error = None
    su_bound = _tolerance_bound(su_spec, parsed, decimals)
    value_bound = _tolerance_bound(value_spec, parsed, decimals)
    value_ok = value_error <= value_bound
    su_ok = True
    if parsed.has_su:
        su_error = abs(reparsed.su - parsed.su)
        su_ok = su_error <= su_bound
    return {
        "new_text": new_text,
        "value_error": value_error,
        "su_error": su_error,
        "value_bound": value_bound,
        "su_bound": su_bound,
        "value_ok": value_ok,
        "su_ok": su_ok,
        "ok": value_ok and su_ok,
    }


def evaluate_column(rows, column_spec, rounding_mode):
    """Find the smallest uniform precision, or the rows that block it."""
    minimum = column_spec["min_decimals"]
    maximum = column_spec["max_decimals"]
    value_spec = column_spec.get("value_tolerance")
    su_spec = column_spec.get("su_tolerance")
    candidates = []
    chosen = None
    for decimals in range(minimum, maximum + 1):
        all_ok = True
        for row in rows:
            parsed = row["parsed"]
            if parsed is None:
                continue
            result = _check_row(parsed, decimals, rounding_mode, value_spec, su_spec)
            if not result["ok"]:
                all_ok = False
        candidates.append({"decimals": decimals, "ok": all_ok})
        if all_ok and chosen is None:
            chosen = decimals
    blockers = []
    if chosen is None:
        best = max(candidates, key=lambda item: item["decimals"])["decimals"]
        for row in rows:
            parsed = row["parsed"]
            if parsed is None:
                continue
            result = _check_row(parsed, best, rounding_mode, value_spec, su_spec)
            if not result["ok"]:
                blockers.append({
                    "line": row["line"],
                    "original": parsed.text,
                    "value": str(parsed.value),
                    "su": None if parsed.su is None else str(parsed.su),
                    "value_error": str(result["value_error"]),
                    "value_bound": str(result["value_bound"]),
                    "su_error": None if result["su_error"] is None else str(result["su_error"]),
                    "su_bound": None if result["su_bound"] is None else str(result["su_bound"]),
                })
    return {
        "chosen_decimals": chosen,
        "candidates": candidates,
        "blockers": blockers,
    }


def _read_loops(lines):
    """Yield (header indices, data row line indices) for every loop."""
    index = 0
    while index < len(lines):
        if lines[index].strip() != "loop_":
            index += 1
            continue
        start = index + 1
        headers = []
        cursor = start
        while cursor < len(lines) and lines[cursor].lstrip().startswith("_"):
            headers.append(lines[cursor].strip())
            cursor += 1
        data_rows = []
        while (cursor < len(lines)
               and lines[cursor].strip() != ""
               and not lines[cursor].lstrip().startswith(("_", "loop_", "data_", "#"))):
            data_rows.append(cursor)
            cursor += 1
        yield headers, data_rows
        index = cursor


def collect_column_rows(lines, column_names):
    """Map column name to every (line index, token, parsed token) occurrence."""
    collected = dict((name, []) for name in column_names)
    for headers, data_rows in _read_loops(lines):
        positions = dict((name, pos) for pos, name in enumerate(headers) if name in column_names)
        if not positions:
            continue
        for line_index in data_rows:
            tokens = lines[line_index].split()
            for name, pos in positions.items():
                token = tokens[pos]
                collected[name].append({
                    "line": line_index + 1,
                    "token": token,
                    "parsed": parse_numeric_token(token),
                })
    return collected


def rewrite_lines(lines, plan, column_specs, rounding_mode):
    """Replace target tokens in place, preserving all other byte content."""
    output = list(lines)
    token_re = re.compile(r"\S+")
    target_names = set(column_specs)
    for headers, data_rows in _read_loops(lines):
        positions = dict((pos, name) for pos, name in enumerate(headers) if name in target_names)
        if not positions:
            continue
        for line_index in data_rows:
            spans = list(token_re.finditer(output[line_index]))
            pieces = []
            cursor = 0
            for token_index, match in enumerate(spans):
                pieces.append(output[line_index][cursor:match.start()])
                name = positions.get(token_index)
                token = match.group(0)
                if name is None or token in SPECIAL_TOKENS or parse_numeric_token(token) is None:
                    pieces.append(token)
                else:
                    parsed = parse_numeric_token(token)
                    decimals = plan[name]["chosen_decimals"]
                    value_q, su_q = round_token(parsed, decimals, rounding_mode)
                    pieces.append(format_fixed(value_q, su_q, decimals))
                cursor = match.end()
            pieces.append(output[line_index][cursor:])
            output[line_index] = "".join(pieces)
    return output


def build_mapping(lines, plan, column_specs, rounding_mode):
    """Row-by-row original/new mapping with re-parsed, realised errors."""
    rows = collect_column_rows(lines, set(column_specs))
    blocking_lines = dict(
        (name, set(item["line"] for item in plan[name]["blockers"]))
        for name in column_specs
    )
    records = []
    for name in sorted(column_specs):
        decimals = plan[name]["chosen_decimals"]
        spec = column_specs[name]
        for entry in rows[name]:
            token = entry["token"]
            line = entry["line"]
            if token in SPECIAL_TOKENS:
                records.append({
                    "line": line, "column": name, "original": token,
                    "new": token, "value_error": "", "su_error": "",
                    "status": "preserved",
                })
                continue
            parsed = entry["parsed"]
            if parsed is None:
                records.append({
                    "line": line, "column": name, "original": token,
                    "new": token, "value_error": "", "su_error": "",
                    "status": "unparsable",
                })
                continue
            if decimals is None:
                if line in blocking_lines[name]:
                    records.append({
                        "line": line, "column": name, "original": token,
                        "new": "", "value_error": "", "su_error": "",
                        "status": "blocked",
                    })
                else:
                    records.append({
                        "line": line, "column": name, "original": token,
                        "new": "", "value_error": "", "su_error": "",
                        "status": "not-rewritten",
                    })
                continue
            result = _check_row(parsed, decimals, rounding_mode,
                                spec.get("value_tolerance"), spec.get("su_tolerance"))
            records.append({
                "line": line, "column": name, "original": token,
                "new": result["new_text"],
                "value_error": str(result["value_error"]),
                "su_error": "" if result["su_error"] is None else str(result["su_error"]),
                "status": "ok" if result["ok"] else "violation",
            })
    return records


def load_spec(path):
    with open(path) as handle:
        spec = json.load(handle)
    spec.setdefault("rounding", "half-even")
    for name, column in spec["columns"].items():
        column.setdefault("min_decimals", 0)
        column.setdefault("max_decimals", 12)
        column.setdefault("value_tolerance", "0.5ulp_original")
        column.setdefault("su_tolerance", "0.5ulp_original")
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cif", help="input CIF file")
    parser.add_argument("--spec", required=True, help="JSON precision specification")
    parser.add_argument("--output-cif", help="rewritten CIF output path")
    parser.add_argument("--mapping", help="row-by-row original/new mapping (TSV)")
    parser.add_argument("--report", help="per-column precision report (JSON)")
    arguments = parser.parse_args(argv)

    spec = load_spec(arguments.spec)
    rounding_mode = spec["rounding"]
    if rounding_mode not in ROUNDING_MODES:
        parser.error("unsupported rounding mode: {0}".format(rounding_mode))

    with open(arguments.cif) as handle:
        original_text = handle.read()
    lines = original_text.splitlines(keepends=True)
    bare_lines = [line.rstrip("\r\n") for line in lines]

    column_specs = spec["columns"]
    rows = collect_column_rows(bare_lines, set(column_specs))
    plan = {}
    for name, column_spec in column_specs.items():
        evaluation = evaluate_column(rows[name], column_spec, rounding_mode)
        plan[name] = evaluation

    feasible = all(plan[name]["chosen_decimals"] is not None for name in column_specs)
    if feasible and arguments.output_cif:
        rewritten = rewrite_lines(bare_lines, plan, column_specs, rounding_mode)
        endings = [line[len(line.rstrip("\r\n")):] for line in lines]
        with open(arguments.output_cif, "w") as handle:
            for text, ending in zip(rewritten, endings):
                handle.write(text + ending)

    if arguments.mapping:
        records = build_mapping(bare_lines, plan, column_specs, rounding_mode)
        header = ["line", "column", "original", "new", "value_error",
                  "su_error", "status"]
        with open(arguments.mapping, "w") as handle:
            handle.write("\t".join(header) + "\n")
            for record in records:
                handle.write("\t".join(str(record[key]) for key in header) + "\n")

    if arguments.report:
        report = {
            "source": arguments.cif,
            "rounding": rounding_mode,
            "feasible": feasible,
            "columns": plan,
        }
        with open(arguments.report, "w") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)

    for name in sorted(column_specs):
        chosen = plan[name]["chosen_decimals"]
        if chosen is None:
            print("{0}: no common precision; {1} blocking row(s)".format(
                name, len(plan[name]["blockers"])))
        else:
            print("{0}: uniform decimals = {1}".format(name, chosen))
    return 0 if feasible else 2


if __name__ == "__main__":
    raise SystemExit(main())
