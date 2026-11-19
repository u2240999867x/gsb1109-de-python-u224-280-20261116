# -*- coding: utf-8 -*-
"""Contact-point attribution under masked directories.

Two directories are reconciled without ever enumerating phone numbers:

* a *partial* directory whose entries contain masked digits (wildcards),
* a *complete* directory whose entries are full, concrete contact points.

Each complete contact point carries an inclusive range of source records it
may correspond to.  A masked entry is matched to a contact only when the
match is legal on every axis:

1. region (country / calling code),
2. valid leading zeros of the presented number,
3. wildcard positions of the main number, interpreted as a digit glob over
   the E.164 national significant number,
4. wildcard positions of the extension, interpreted separately from the
   main number (equal tail digits never merge two customers).

Numbers outside the complete directory are never generated.  An association
is *confirmed* only when it survives every feasible allocation under the
record-count ranges; otherwise it stays a *candidate*.  If no allocation
exists, the conflicting entries and count constraints are reported.

Run ``python -m tools.attribution.contact_attribution --help`` for the CLI.
"""

from __future__ import print_function

import argparse
import csv
import importlib
import json
import os
import re
import sys
from collections import deque

# Use the bundled phonenumbers snapshot when the package is not installed.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLED = os.path.join(_REPO_ROOT, "python")
if os.path.isdir(os.path.join(_BUNDLED, "phonenumbers")) and _BUNDLED not in sys.path:
    sys.path.insert(0, _BUNDLED)

import phonenumbers  # noqa: E402
from phonenumbers.phonenumberutil import (  # noqa: E402
    COUNTRY_CODE_TO_REGION_CODE,
    ValidationResult,
)

WILDCARD_SINGLE = "?"
WILDCARD_GLOB = "*"
_NON_TOKEN = re.compile(r"[^0-9+*?xX]")
_EXT_SPLIT = re.compile(
    r"^(.*?)(?:\s+ext\.?\s*|\s+[xX]\s*|#)([0-9*?xX]+)\s*$"
)
_GLOB_ESCAPE = re.compile(r"([\\^$.|+()\[\]{}])")
_NON_GEO_REGION = "001"


class AttributionError(ValueError):
    """Raised when an input row cannot be interpreted."""


class Extent(object):
    """Inclusive range of source records a contact point may represent."""

    __slots__ = ("low", "high")

    def __init__(self, low, high):
        if low < 0 or high < low:
            raise AttributionError(
                "invalid record-count range [%s, %s]" % (low, high)
            )
        self.low = low
        self.high = high

    def as_tuple(self):
        return self.low, self.high


class ContactPoint(object):
    """A concrete, fully numbered contact point from the complete directory."""

    __slots__ = (
        "contact_id", "region", "country_code", "significant",
        "leading_zeros", "extension", "e164", "national_format",
        "possible", "valid", "ref",
    )

    def __init__(self, contact_id, region, country_code, significant,
                 leading_zeros, extension, e164, national_format,
                 possible, valid, ref):
        self.contact_id = contact_id
        self.region = region
        self.country_code = country_code
        # E.164 national significant digits, including embedded leading
        # zeros (e.g. Italy) but excluding trunk prefixes (e.g. Germany).
        self.significant = significant
        self.leading_zeros = leading_zeros
        self.extension = extension
        self.e164 = e164
        self.national_format = national_format
        self.possible = possible
        self.valid = valid
        self.ref = ref

    def label(self):
        text = self.e164
        if self.extension:
            text += " ext " + self.extension
        return text


class MaskedEntry(object):
    """A masked row of the partial directory."""

    __slots__ = (
        "masked_id", "region", "country_code", "leading_zeros",
        "zero_kind", "main_glob", "extension_glob", "raw", "ref",
    )

    def __init__(self, masked_id, region, country_code, leading_zeros,
                 zero_kind, main_glob, extension_glob, raw, ref):
        self.masked_id = masked_id
        self.region = region
        self.country_code = country_code
        self.leading_zeros = leading_zeros
        # "embedded": zeros are part of E.164; "prefix": national trunk
        # prefix only; "none": no leading zero presented.
        self.zero_kind = zero_kind
        self.main_glob = main_glob
        self.extension_glob = extension_glob
        self.raw = raw
        self.ref = ref


# ---------------------------------------------------------------------------
# Token normalization and metadata helpers
# ---------------------------------------------------------------------------

def _split_extension(raw):
    """Split a raw string into (main part, extension part or None)."""
    match = _EXT_SPLIT.match(raw.strip())
    if match is None:
        return raw.strip(), None
    return match.group(1).strip(), match.group(2)


def _normalize_token(raw):
    """Keep digits and wildcard markers only; X/x means a masked digit."""
    token = _NON_TOKEN.sub("", raw)
    return token.replace("X", WILDCARD_SINGLE).replace("x", WILDCARD_SINGLE)


def _has_wildcard(token):
    return WILDCARD_GLOB in token or WILDCARD_SINGLE in token


def _glob_to_regex(glob):
    """Translate a digit glob (``*`` any run, ``?`` one digit) to regex."""
    pattern = _GLOB_ESCAPE.sub(r"\\\1", glob)
    pattern = pattern.replace(WILDCARD_GLOB, r"\d*").replace(WILDCARD_SINGLE, r"\d")
    return re.compile(r"^" + pattern + r"$")


def _glob_min_max_len(glob):
    fixed = len(re.sub(r"[*?]", "", glob))
    questions = glob.count(WILDCARD_SINGLE)
    stars = glob.count(WILDCARD_GLOB)
    return fixed + questions, fixed + questions + stars * 64


def _region_metadata(region):
    module = importlib.import_module(
        "phonenumbers.data.region_" + region
    )
    return getattr(module, "PHONE_METADATA_" + region)


def _leading_zeros_of(parsed):
    if parsed.number_of_leading_zeros:
        return int(parsed.number_of_leading_zeros)
    return 1 if parsed.italian_leading_zero else 0


def _resolve_region(country_code, explicit_region):
    regions = COUNTRY_CODE_TO_REGION_CODE.get(country_code, ())
    if explicit_region:
        if regions and explicit_region not in regions:
            raise AttributionError(
                "region %s does not own calling code +%d"
                % (explicit_region, country_code)
            )
        return explicit_region
    if len(regions) == 1:
        return regions[0]
    if not regions:
        raise AttributionError(
            "calling code +%d is not assigned to any region" % country_code
        )
    raise AttributionError(
        "calling code +%d is shared by %s; an explicit region is required"
        % (country_code, ", ".join(regions))
    )


def _concrete_anchor(token):
    """Return the leading concrete digit run before the first wildcard."""
    anchor = re.match(r"^[0-9]+", token)
    return anchor.group(0) if anchor else ""


def _parse_anchor(region, country_code, anchor, international):
    if len(anchor) < 2:
        return None
    if international:
        candidate = "+%d%s" % (country_code, anchor)
        try:
            return phonenumbers.parse(candidate, None)
        except phonenumbers.NumberParseException:
            return None
    try:
        return phonenumbers.parse(anchor, region)
    except phonenumbers.NumberParseException:
        return None


def _classify_leading_zeros(region, metadata, country_code,
                            national_token, international):
    """Validate leading zeros and return (zero count, kind, significant glob).

    The significant glob is expressed on the E.164 national significant
    number, so national and international forms of the same number compare
    equal while the presented zeros are still validated explicitly.
    """
    zeros = len(national_token) - len(national_token.lstrip("0"))
    prefix = getattr(metadata, "national_prefix", None) if metadata else None

    anchor = _concrete_anchor(national_token)
    parsed = _parse_anchor(region, country_code, anchor, international)
    if not international and parsed is not None and anchor:
        canonical = re.sub(
            r"\D", "",
            phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.NATIONAL
            ),
        )
        if not canonical.startswith(anchor):
            raise AttributionError(
                "national masked entry %r omits the required national "
                "prefix; canonical form begins %r"
                % (national_token, canonical)
            )

    if zeros == 0:
        if not international and prefix and national_token.startswith(prefix):
            # Non-zero trunk prefix such as Russia's 8.
            return 0, "prefix", national_token[len(prefix):]
        return 0, "none", national_token

    embedded = parsed is not None and _leading_zeros_of(parsed) > 0

    if prefix is None:
        # No trunk prefix exists: every presented zero is embedded in E.164.
        if parsed is not None and not embedded:
            raise AttributionError(
                "leading zero(s) in %r are not valid for region %s"
                % (national_token, region)
            )
        return zeros, "embedded", national_token

    if embedded and not national_token.startswith(prefix):
        return zeros, "embedded", national_token

    if international:
        raise AttributionError(
            "the leading zero of %r is a national trunk prefix and must not "
            "appear in international (E.164) form" % national_token
        )

    if not set(prefix) == {"0"} or zeros != len(prefix):
        raise AttributionError(
            "%r carries %d leading zero(s) but region %s requires prefix %r"
            % (national_token, zeros, region, prefix)
        )
    return zeros, "prefix", national_token[zeros:]


# ---------------------------------------------------------------------------
# Directory parsing
# ---------------------------------------------------------------------------

def parse_contact(contact_id, raw, explicit_region=None, ref=None):
    """Parse one concrete row of the complete directory."""
    main_raw, ext_raw = _split_extension(raw)
    main_token = _normalize_token(main_raw)
    if _has_wildcard(main_token):
        raise AttributionError(
            "complete directory entry %r contains wildcard digits" % raw
        )
    extension = _normalize_token(ext_raw) if ext_raw is not None else None
    if extension and not extension.isdigit():
        raise AttributionError(
            "complete directory extension %r contains wildcard digits" % raw
        )

    international = main_token.startswith("+")
    try:
        parsed = phonenumbers.parse(
            main_token, None if international else explicit_region
        )
    except phonenumbers.NumberParseException as exc:
        raise AttributionError("cannot parse %r: %s" % (raw, exc))

    country_code = parsed.country_code
    if explicit_region:
        region = _resolve_region(country_code, explicit_region)
    else:
        derived = phonenumbers.region_code_for_number(parsed)
        if derived and derived != _NON_GEO_REGION:
            if derived not in COUNTRY_CODE_TO_REGION_CODE.get(country_code, ()):
                raise AttributionError(
                    "derived region %s does not own calling code +%d"
                    % (derived, country_code)
                )
            region = derived
        else:
            region = _resolve_region(country_code, None)
    if region != _NON_GEO_REGION and not international:
        parsed_region = phonenumbers.region_code_for_number(parsed)
        if parsed_region not in (None, _NON_GEO_REGION) and parsed_region != region:
            raise AttributionError(
                "%r belongs to region %s, not %s"
                % (raw, parsed_region, region)
            )

    reason = phonenumbers.is_possible_number_with_reason(parsed)
    possible = reason == ValidationResult.IS_POSSIBLE
    valid = phonenumbers.is_valid_number(parsed)

    e164 = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )
    national_format = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.NATIONAL
    )
    significant = e164[1 + len(str(country_code)):]

    presented = main_token[1:] if international else main_token
    canonical_national = re.sub(r"\D", "", national_format)
    if international:
        presented_body = presented[len(str(country_code)):]
        if presented_body != significant:
            raise AttributionError(
                "%r has inconsistent leading zeros; E.164 body is %r"
                % (raw, significant)
            )
        leading_zeros = _leading_zeros_of(parsed)
    else:
        if presented != canonical_national:
            raise AttributionError(
                "%r has inconsistent leading zeros; national form is %r"
                % (raw, canonical_national)
            )
        leading_zeros = len(
            canonical_national
        ) - len(canonical_national.lstrip("0"))

    return ContactPoint(
        contact_id, region, country_code, significant, leading_zeros,
        extension, e164, national_format, possible, valid, ref,
    )


def parse_masked(masked_id, raw, explicit_region=None, ref=None):
    """Parse one masked row of the partial directory."""
    main_raw, ext_raw = _split_extension(raw)
    main_token = _normalize_token(main_raw)
    if not main_token:
        raise AttributionError("masked entry %r has no digits" % raw)
    extension_glob = _normalize_token(ext_raw) if ext_raw is not None else None

    international = main_token.startswith("+")
    if international:
        digits = main_token[1:]
        anchor_match = re.match(r"^[0-9]+", digits)
        anchor = anchor_match.group(0) if anchor_match else ""
        country_code = None
        for length in (3, 2, 1):
            if len(anchor) >= length and int(anchor[:length]) in COUNTRY_CODE_TO_REGION_CODE:
                country_code = int(anchor[:length])
                break
        if country_code is None:
            raise AttributionError(
                "masked entry %r has no recognizable calling code" % raw
            )
        region = _resolve_region(country_code, explicit_region)
        national_token = digits[len(str(country_code)):]
    else:
        if not explicit_region:
            raise AttributionError(
                "national masked entry %r requires an explicit region" % raw
            )
        region = explicit_region
        metadata = _region_metadata(region)
        country_code = metadata.country_code
        national_token = main_token

    metadata = None if region == _NON_GEO_REGION else _region_metadata(region)
    leading_zeros, zero_kind, main_glob = _classify_leading_zeros(
        region, metadata, country_code, national_token, international
    )

    if not main_glob or not re.search(r"[0-9]", main_glob):
        raise AttributionError(
            "masked entry %r masks the whole number; refusing to enumerate" % raw
        )
    return MaskedEntry(
        masked_id, region, country_code, leading_zeros, zero_kind,
        main_glob, extension_glob, raw, ref,
    )


# ---------------------------------------------------------------------------
# Structural matching (no number is ever generated)
# ---------------------------------------------------------------------------

def _matches_glob(glob, text):
    return _glob_to_regex(glob).match(text) is not None


def structural_candidates(masked, contacts):
    """Return the contacts that satisfy region, leading-zero and mask rules."""
    matches = []
    main_min, main_max = _glob_min_max_len(masked.main_glob)
    for contact in contacts:
        if contact.region != masked.region:
            continue
        if contact.country_code != masked.country_code:
            continue
        significant = contact.significant
        if not main_min <= len(significant) <= main_max:
            continue
        if not _matches_glob(masked.main_glob, significant):
            continue
        if masked.extension_glob is not None:
            # Extension masking is interpreted independently of the main
            # number, so a missing extension can never satisfy an ext mask.
            if contact.extension is None:
                continue
            if not _matches_glob(masked.extension_glob, contact.extension):
                continue
        matches.append(contact)
    return matches


# ---------------------------------------------------------------------------
# Feasibility: bounded b-matching via lower-bound max flow
# ---------------------------------------------------------------------------

class _Dinic(object):

    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]

    def add_edge(self, u, v, cap):
        # Residual capacity above a mandatory lower bound; the lower bound
        # itself is enforced separately through node demand balances.
        forward = [v, cap, len(self.graph[v])]
        backward = [u, 0, len(self.graph[u])]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def _bfs(self, source, sink):
        level = [-1] * self.size
        level[source] = 0
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                v, cap = edge[0], edge[1]
                if cap > 0 and level[v] < 0:
                    level[v] = level[u] + 1
                    queue.append(v)
        self.level = level
        return level[sink] >= 0

    def _dfs(self, u, sink, pushed):
        if u == sink:
            return pushed
        while self.ptr[u] < len(self.graph[u]):
            edge = self.graph[u][self.ptr[u]]
            v, cap = edge[0], edge[1]
            if cap > 0 and self.level[v] == self.level[u] + 1:
                flow = self._dfs(v, sink, min(pushed, cap))
                if flow:
                    edge[1] -= flow
                    self.graph[v][edge[2]][1] += flow
                    return flow
            self.ptr[u] += 1
        return 0

    def max_flow(self, source, sink):
        total = 0
        inf = sys.maxsize
        while self._bfs(source, sink):
            self.ptr = [0] * self.size
            while True:
                pushed = self._dfs(source, sink, inf)
                if not pushed:
                    break
                total += pushed
        return total


def _feasible(masked_ids, contact_ids, edges, extents, excluded=None):
    """Check a bounded b-matching; every masked node needs degree exactly 1.

    When ``excluded`` is an edge, that single association is removed first.
    An edge is mandatory (confirmed) exactly when excluding it makes the
    whole problem infeasible.
    """
    active_masked = list(masked_ids)
    bounds = dict((cid, extents[cid].as_tuple()) for cid in contact_ids)
    edge_set = set(edges)
    if excluded is not None:
        edge_set.discard(excluded)

    masked_index = dict((m, i) for i, m in enumerate(active_masked))
    contact_index = dict((c, i) for i, c in enumerate(contact_ids))
    n_m, n_c = len(active_masked), len(contact_ids)
    source, sink = n_m + n_c, n_m + n_c + 1
    ss, tt = sink + 1, sink + 2
    dinic = _Dinic(tt + 1)
    demand = [0] * (tt + 1)

    def bounded_edge(u, v, low, high):
        dinic.add_edge(u, v, high - low)
        demand[v] += low
        demand[u] -= low

    for i in range(n_m):
        bounded_edge(source, i, 1, 1)
    for m, c in edge_set:
        if m not in masked_index or c not in contact_index:
            continue
        bounded_edge(masked_index[m], n_m + contact_index[c], 0, 1)
    for c in contact_ids:
        low, high = bounds[c]
        bounded_edge(n_m + contact_index[c], sink, low, high)

    dinic.add_edge(sink, source, sys.maxsize // 2)
    required = 0
    for node, value in enumerate(demand):
        if value > 0:
            dinic.add_edge(ss, node, value)
            required += value
        elif value < 0:
            dinic.add_edge(node, tt, -value)
    if dinic.max_flow(ss, tt) != required:
        return False

    # The circulation must saturate every source -> masked edge.
    for edge in dinic.graph[source]:
        if edge[0] < n_m and edge[1] != 0:
            return False
    return True


def _hall_witness(masked_ids, contact_ids, edges, extents):
    """Return a subset of masked entries whose neighbour capacity is short."""
    masked_index = dict((m, i) for i, m in enumerate(masked_ids))
    contact_index = dict((c, i) for i, c in enumerate(contact_ids))
    n_m, n_c = len(masked_ids), len(contact_ids)
    source, sink = n_m + n_c, n_m + n_c + 1
    dinic = _Dinic(sink + 1)
    for i in range(n_m):
        dinic.add_edge(source, i, 1)
    for m, c in edges:
        dinic.add_edge(masked_index[m], n_m + contact_index[c], 1)
    for c in contact_ids:
        dinic.add_edge(n_m + contact_index[c], sink, extents[c].high)
    dinic.max_flow(source, sink)

    reachable = [False] * dinic.size
    reachable[source] = True
    queue = deque([source])
    while queue:
        u = queue.popleft()
        for edge in dinic.graph[u]:
            if edge[1] > 0 and not reachable[edge[0]]:
                reachable[edge[0]] = True
                queue.append(edge[0])
    witness = [m for m, i in masked_index.items() if reachable[i]]
    if not witness:
        witness = list(masked_ids)
    neighbours = sorted(set(c for (m, c) in edges if m in witness))
    return {
        "masked_entries": witness,
        "neighbour_contacts": neighbours,
        "available_capacity": sum(extents[c].high for c in neighbours),
        "required_assignments": len(witness),
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def _as_rows(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def solve(partial_rows, complete_rows, extent_rows, default_region=None):
    """Reconcile the two directories and return a structured verdict."""
    partial_rows = _as_rows(partial_rows)
    complete_rows = _as_rows(complete_rows)
    extent_rows = _as_rows(extent_rows)

    warnings = []
    errors = []
    contacts = []
    masked_entries = []
    extents = {}

    for row in extent_rows:
        try:
            contact_id = str(row["contact_id"])
            extents[contact_id] = Extent(
                int(row["min_records"]), int(row["max_records"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"row": row, "error": "bad extent: %s" % exc})

    seen_contacts = set()
    for row in complete_rows:
        contact_id = str(row["contact_id"])
        region = row.get("region") or default_region
        try:
            if contact_id in seen_contacts:
                raise AttributionError("duplicate contact_id %r" % contact_id)
            contact = parse_contact(
                contact_id, str(row["number"]), region, row.get("ref")
            )
            if not contact.possible:
                raise AttributionError(
                    "%r is not a possible number for its region" % row["number"]
                )
            if not contact.valid:
                warnings.append(
                    "contact %s (%s) is possible but not assigned in metadata"
                    % (contact_id, contact.label())
                )
            contacts.append(contact)
            seen_contacts.add(contact_id)
        except AttributionError as exc:
            errors.append({"row": row, "error": str(exc)})

    contact_ids = [c.contact_id for c in contacts]
    for contact_id in contact_ids:
        if contact_id not in extents:
            errors.append({
                "row": {"contact_id": contact_id},
                "error": "missing record-count range",
            })

    seen_masked = set()
    for row in partial_rows:
        masked_id = str(row["masked_id"])
        region = row.get("region") or default_region
        try:
            if masked_id in seen_masked:
                raise AttributionError("duplicate masked_id %r" % masked_id)
            masked_entries.append(
                parse_masked(masked_id, str(row["masked_number"]), region,
                             row.get("ref"))
            )
            seen_masked.add(masked_id)
        except AttributionError as exc:
            errors.append({"row": row, "error": str(exc)})

    result = {
        "status": "infeasible" if errors else "pending",
        "rule_basis": [
            "region and calling code must agree",
            "leading zeros are validated per region (embedded vs trunk prefix)",
            "main-number glob matches E.164 significant digit positions "
            "without enumeration",
            "extension glob is evaluated separately from the main number",
            "equal tail digits alone never merge two contacts",
            "only numbers present in the complete directory are candidates",
        ],
        "contacts": [_contact_dict(c, extents.get(c.contact_id)) for c in contacts],
        "masked_entries": [_masked_dict(m) for m in masked_entries],
        "confirmed": [],
        "candidates": [],
        "unmatched_masked_entries": [],
        "conflicts": [],
        "warnings": warnings,
        "input_errors": errors,
    }
    if errors:
        return result

    edge_table = {}
    for masked in masked_entries:
        candidates = structural_candidates(masked, contacts)
        edge_table[masked.masked_id] = [c.contact_id for c in candidates]
        if not candidates:
            result["unmatched_masked_entries"].append({
                "masked_id": masked.masked_id,
                "raw": masked.raw,
                "reason": "no contact satisfies region, leading-zero and mask",
            })

    edges = set(
        (masked_id, contact_id)
        for masked_id, ids in edge_table.items()
        for contact_id in ids
    )
    masked_ids = [m.masked_id for m in masked_entries]

    if not _feasible(masked_ids, contact_ids, edges, extents):
        result["status"] = "infeasible"
        result["conflicts"] = _diagnose(
            masked_ids, contact_ids, edges, extents, edge_table
        )
        for masked_id, ids in edge_table.items():
            for contact_id in ids:
                result["candidates"].append(_association_dict(
                    masked_id, contact_id, "candidate",
                    "overall allocation infeasible", edge_table, extents,
                ))
        return result

    result["status"] = "feasible"
    for masked_id, ids in edge_table.items():
        for contact_id in ids:
            mandatory = not _feasible(
                masked_ids, contact_ids, edges, extents,
                excluded=(masked_id, contact_id),
            )
            reason = (
                "forced by every feasible allocation"
                if mandatory
                else "legal match but another allocation exists"
            )
            result[
                "confirmed" if mandatory else "candidates"
            ].append(_association_dict(
                masked_id, contact_id,
                "confirmed" if mandatory else "candidate",
                reason, edge_table, extents,
            ))
    return result


def _diagnose(masked_ids, contact_ids, edges, extents, edge_table):
    conflicts = []
    for masked_id in masked_ids:
        if not edge_table.get(masked_id):
            conflicts.append({
                "kind": "no_candidate",
                "masked_id": masked_id,
                "detail": "no legal contact within the complete directory",
            })
    for contact_id in contact_ids:
        low = extents[contact_id].low
        degree = sum(1 for (m, c) in edges if c == contact_id)
        if degree < low:
            conflicts.append({
                "kind": "lower_bound_unreachable",
                "contact_id": contact_id,
                "required_minimum": low,
                "legal_masked_neighbours": degree,
            })
    witness = _hall_witness(masked_ids, contact_ids, edges, extents)
    if witness["neighbour_contacts"] and (
        witness["available_capacity"] < witness["required_assignments"]
    ):
        conflicts.append({
            "kind": "capacity_shortfall",
            "detail": witness,
            "constraints": dict(
                (c, list(extents[c].as_tuple()))
                for c in witness["neighbour_contacts"]
            ),
        })
    return conflicts


def _contact_dict(contact, extent):
    return {
        "contact_id": contact.contact_id,
        "region": contact.region,
        "calling_code": contact.country_code,
        "leading_zeros": contact.leading_zeros,
        "significant_number": contact.significant,
        "extension": contact.extension,
        "e164": contact.e164,
        "national_format": contact.national_format,
        "record_range": list(extent.as_tuple()) if extent else None,
        "ref": contact.ref,
    }


def _masked_dict(masked):
    return {
        "masked_id": masked.masked_id,
        "region": masked.region,
        "calling_code": masked.country_code,
        "leading_zeros": masked.leading_zeros,
        "zero_kind": masked.zero_kind,
        "main_glob": masked.main_glob,
        "extension_glob": masked.extension_glob,
        "raw": masked.raw,
        "ref": masked.ref,
    }


def _association_dict(masked_id, contact_id, certainty, reason,
                      edge_table, extents):
    return {
        "masked_id": masked_id,
        "contact_id": contact_id,
        "certainty": certainty,
        "reason": reason,
        "alternative_contact_count": len(edge_table[masked_id]) - 1,
        "contact_record_range": list(extents[contact_id].as_tuple()),
    }


# ---------------------------------------------------------------------------
# CSV / CLI
# ---------------------------------------------------------------------------

def _read_csv(path):
    with open(path, "r", newline="") as handle:
        return list(csv.DictReader(handle))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Attribute masked directory entries to complete contact "
                    "points without enumerating unknown numbers."
    )
    parser.add_argument("--partial", required=True,
                        help="CSV: masked_id,masked_number,region?,ref?")
    parser.add_argument("--complete", required=True,
                        help="CSV: contact_id,number,region?,ref?")
    parser.add_argument("--extents", required=True,
                        help="CSV: contact_id,min_records,max_records")
    parser.add_argument("--default-region",
                        help="region used when a row omits one")
    parser.add_argument("--out", help="write JSON verdict here (default stdout)")
    args = parser.parse_args(argv)

    verdict = solve(
        _read_csv(args.partial),
        _read_csv(args.complete),
        _read_csv(args.extents),
        default_region=args.default_region,
    )
    text = json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0 if verdict["status"] == "feasible" else 2


if __name__ == "__main__":
    sys.exit(main())
