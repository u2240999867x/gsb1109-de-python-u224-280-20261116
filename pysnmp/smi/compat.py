"""Two-version MIB column compatibility analysis.

This module answers a narrow question: after a MIB upgrade, can values
collected against the old column definition still be ingested under the
new one?

The analysis is definition based. Two :class:`~pysnmp.smi.builder.MibBuilder`
instances load the two MIB versions in isolation, so every conclusion is
derived from the classes and constraints produced by the version under
test. Observed samples are never accepted as proof of domain compatibility;
when the constraint combination cannot be decided structurally, the column
is reported as unprovable together with the exact construct that blocks a
proof.
"""

from dataclasses import dataclass, field

from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.codec.ber import encoder as ber_encoder
from pyasn1.error import PyAsn1Error
from pyasn1.type import constraint, namedval, univ

from pysnmp.smi.builder import DirMibSource, MibBuilder

REUSABLE = "reusable"
MIGRATION_REQUIRED = "migration-required"
UNPROVABLE = "unprovable"

_APPLICATION_TAG_NAMES = {
    0: "IpAddress",
    1: "Counter32",
    2: "Gauge32/Unsigned32",
    3: "TimeTicks",
    4: "Opaque",
    6: "Counter64",
}

_UNIVERSAL_TAG_NAMES = {
    2: "INTEGER",
    4: "OCTET STRING",
    6: "OBJECT IDENTIFIER",
}


@dataclass
class Counterexample:
    """A value accepted by the old definition and rejected by the new one."""

    value: object
    representation: str
    old_result: str
    new_result: str
    detail: str


@dataclass
class ColumnVerdict:
    """Compatibility outcome for a single table column."""

    column: str
    oid: tuple
    verdict: str
    old_wire_type: str
    new_wire_type: str
    reasons: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    counterexamples: list = field(default_factory=list)

    @property
    def is_reusable(self):
        return self.verdict == REUSABLE


def load_isolated_builder(source_dir):
    """Load MIBs from *source_dir* into a fresh, isolated builder.

    Core MIB packages stay on the search path behind *source_dir*, but each
    builder executes every module itself, so symbols (including base syntax
    classes) are never shared with the other version's builder.
    """
    builder = MibBuilder()
    sources = (DirMibSource(str(source_dir)),) + builder.get_mib_sources()
    builder.set_mib_sources(*sources)
    return builder


def _effective_tag(syntax_cls):
    super_tags = tuple(syntax_cls.tagSet)
    if super_tags:
        return tuple(super_tags[-1])
    return tuple(syntax_cls.tagSet.getBaseTag())


def _wire_type_name(syntax_cls):
    base_tag = _effective_tag(syntax_cls)
    if base_tag[0] == 64:  # tagClassApplication
        name = _APPLICATION_TAG_NAMES.get(base_tag[2], f"APPLICATION[{base_tag[2]}]")
    else:
        name = _UNIVERSAL_TAG_NAMES.get(base_tag[2], f"UNIVERSAL[{base_tag[2]}]")
    return f"{name} {base_tag}"


def _is_integer_family(syntax_cls):
    return issubclass(syntax_cls, univ.Integer)


def _is_octet_family(syntax_cls):
    return issubclass(syntax_cls, univ.OctetString)


def _named_map(syntax_cls):
    named_values = getattr(syntax_cls, "namedValues", None)
    if isinstance(named_values, namedval.NamedValues) and named_values:
        return dict(named_values.items())
    return {}


def _is_enum(syntax_cls):
    return _is_integer_family(syntax_cls) and bool(_named_map(syntax_cls))


def _is_bits(syntax_cls):
    return _is_octet_family(syntax_cls) and bool(_named_map(syntax_cls))


def _merge_ranges(ranges):
    if not ranges:
        return []
    ordered = sorted((lo, hi) for lo, hi in ranges if lo <= hi)
    merged = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [tuple(part) for part in merged]


def _intersect_ranges(left, right):
    result = []
    for lo_a, hi_a in left:
        for lo_b, hi_b in right:
            lo = max(lo_a, lo_b)
            hi = min(hi_a, hi_b)
            if lo <= hi:
                result.append((lo, hi))
    return _merge_ranges(result)


def _ranges_subset(smaller, larger):
    for lo, hi in smaller:
        covered = False
        for big_lo, big_hi in larger:
            if big_lo <= lo and hi <= big_hi:
                covered = True
                break
        if not covered:
            return False
    return True


def _subtract_ranges(ranges, removed):
    result = []
    for lo, hi in ranges:
        pieces = [(lo, hi)]
        for rem_lo, rem_hi in removed:
            next_pieces = []
            for piece_lo, piece_hi in pieces:
                if rem_hi < piece_lo or rem_lo > piece_hi:
                    next_pieces.append((piece_lo, piece_hi))
                    continue
                if rem_lo > piece_lo:
                    next_pieces.append((piece_lo, min(piece_hi, rem_lo - 1)))
                if rem_hi < piece_hi:
                    next_pieces.append((max(piece_lo, rem_hi + 1), piece_hi))
            pieces = next_pieces
        result.extend(pieces)
    return _merge_ranges(result)


def _numeric_domain(spec, gaps):
    """Return merged inclusive integer ranges allowed by *spec* or None."""
    if isinstance(spec, constraint.ConstraintsIntersection):
        domains = []
        exclusions = []
        for child in spec:
            if isinstance(child, constraint.ConstraintsExclusion):
                operand = _numeric_domain(child._values[0], gaps)
                if operand is None:
                    gaps.append(f"unsupported exclusion operand {child._values[0]!r}")
                    return None
                exclusions.extend(operand)
                continue
            child_domain = _numeric_domain(child, gaps)
            if child_domain is None:
                return None
            domains.append(child_domain)
        if not domains:
            return None
        result = domains[0]
        for child_domain in domains[1:]:
            result = _intersect_ranges(result, child_domain)
        if exclusions:
            result = _subtract_ranges(result, _merge_ranges(exclusions))
        return result

    if isinstance(spec, constraint.ConstraintsUnion):
        domains = []
        for child in spec:
            child_domain = _numeric_domain(child, gaps)
            if child_domain is None:
                return None
            domains.extend(child_domain)
        return _merge_ranges(domains)

    if isinstance(spec, constraint.ValueRangeConstraint):
        return [(spec.start, spec.stop)]

    if isinstance(spec, constraint.SingleValueConstraint):
        return _merge_ranges((value, value) for value in spec._values)

    gaps.append(f"undecidable integer constraint {spec!r}")
    return None


_ALL_BYTES = frozenset(range(256))


def _size_domain(spec, gaps):
    """Return merged inclusive size ranges allowed by *spec* or None."""
    if isinstance(spec, constraint.ConstraintsIntersection):
        filtered = [
            child
            for child in spec
            if not isinstance(child, constraint.PermittedAlphabetConstraint)
        ]
        if not filtered:
            return [(0, 65535)]
        domains = []
        for child in filtered:
            child_domain = _size_domain(child, gaps)
            if child_domain is None:
                return None
            domains.append(child_domain)
        result = domains[0]
        for child_domain in domains[1:]:
            result = _intersect_ranges(result, child_domain)
        return result

    if isinstance(spec, constraint.ConstraintsUnion):
        filtered = [
            child
            for child in spec
            if not isinstance(child, constraint.PermittedAlphabetConstraint)
        ]
        if not filtered:
            return [(0, 65535)]
        domains = []
        for child in filtered:
            child_domain = _size_domain(child, gaps)
            if child_domain is None:
                return None
            domains.extend(child_domain)
        return _merge_ranges(domains)

    if isinstance(spec, constraint.ValueSizeConstraint):
        return [(spec.start, spec.stop)]

    gaps.append(f"undecidable size constraint {spec!r}")
    return None


def _alphabet_sets(spec, gaps):
    """Return permitted bytes; ALL bytes means no alphabet restriction."""
    if isinstance(spec, constraint.ConstraintsIntersection):
        sets = _ALL_BYTES
        for child in spec:
            sets &= _alphabet_sets(child, gaps)
        return sets

    if isinstance(spec, constraint.ConstraintsUnion):
        sets = frozenset()
        for child in spec:
            child_sets = _alphabet_sets(child, gaps)
            if child_sets == _ALL_BYTES:
                return _ALL_BYTES
            sets |= child_sets
        return sets

    if isinstance(spec, constraint.PermittedAlphabetConstraint):
        return frozenset(spec._set)

    if isinstance(
        spec,
        (
            constraint.ValueSizeConstraint,
            constraint.ValueRangeConstraint,
            constraint.SingleValueConstraint,
        ),
    ):
        return _ALL_BYTES

    gaps.append(f"undecidable alphabet constraint {spec!r}")
    return _ALL_BYTES


def _validate(syntax_cls, value):
    try:
        syntax_cls(value)
    except PyAsn1Error as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "accepted"


def _integer_candidates(old_domain, new_domain, old_map):
    candidates = set()
    for domain in (old_domain, new_domain):
        if not domain:
            continue
        for lo, hi in domain:
            candidates.update((lo, hi))
            if lo < hi:
                candidates.update((lo + 1, hi - 1))
            for boundary in (255, 256, 65535, 65536, 2147483647):
                if lo <= boundary <= hi:
                    candidates.add(boundary)
    candidates.update(old_map.values())
    return sorted(candidates)


def _size_candidates(old_sizes, new_sizes, alphabet):
    candidates = set()
    for domain in (old_sizes, new_sizes):
        if not domain:
            continue
        for lo, hi in domain:
            candidates.update((lo, hi))
            if lo < hi:
                candidates.add(lo + 1)
    if not candidates:
        candidates.update((0, 1, 2))
    fill_byte = min(alphabet) if alphabet else 65
    return [(size, bytes([fill_byte]) * size) for size in sorted(candidates)]


def _check_value(syntax_cls, value, representation, detail, findings):
    old_cls, new_cls = syntax_cls
    old_ok, old_message = _validate(old_cls, value)
    new_ok, new_message = _validate(new_cls, value)
    if old_ok and not new_ok:
        findings.append(
            Counterexample(
                value=value,
                representation=representation,
                old_result=old_message,
                new_result=new_message,
                detail=detail,
            )
        )
    return old_ok, new_ok


def _compare_wire_type(old_cls, new_cls, verdict):
    if old_cls.tagSet == new_cls.tagSet:
        return
    verdict.verdict = MIGRATION_REQUIRED
    verdict.reasons.append(
        f"ASN.1 wire tag changed from {verdict.old_wire_type} "
        f"to {verdict.new_wire_type}"
    )
    probes = (1, 4000000000) if _is_integer_family(old_cls) else (b"\x01", b"AB")
    for probe in probes:
        old_ok, old_message = _validate(old_cls, probe)
        if not old_ok:
            continue
        wire_bytes = ber_encoder.encode(old_cls(probe))
        try:
            ber_decoder.decode(wire_bytes, asn1Spec=new_cls())
        except PyAsn1Error as exc:
            verdict.counterexamples.append(
                Counterexample(
                    value=probe,
                    representation=f"BER TLV {wire_bytes.hex()}",
                    old_result=old_message,
                    new_result=f"{type(exc).__name__}: {str(exc).splitlines()[0]}",
                    detail="the old on-wire tag is not legal for the new column",
                )
            )
        return
    verdict.gaps.append(
        "wire tag changed but no old-accepted probe value could be built"
    )


def _compare_enum_semantics(old_cls, new_cls, verdict):
    old_map = _named_map(old_cls)
    new_map = _named_map(new_cls)
    if old_map == new_map:
        return old_map, new_map
    verdict.verdict = MIGRATION_REQUIRED
    old_by_code = {code: name for name, code in old_map.items()}
    new_by_code = {code: name for name, code in new_map.items()}
    for code, old_name in sorted(old_by_code.items()):
        new_name = new_by_code.get(code)
        if new_name is None:
            verdict.reasons.append(
                f"enumeration {old_name!r} ({code}) removed from the new definition"
            )
            _check_value(
                (old_cls, new_cls),
                code,
                f"integer code {code}",
                "enumerated value no longer permitted",
                verdict.counterexamples,
            )
        elif new_name != old_name:
            verdict.reasons.append(
                f"enumeration code {code} renamed {old_name!r} -> {new_name!r}"
            )
    for code, new_name in sorted(new_by_code.items()):
        old_name = old_by_code.get(code)
        if old_name is None:
            verdict.reasons.append(
                f"new enumeration {new_name!r} ({code}) added by the new definition"
            )
        elif old_name != new_name:
            verdict.reasons.append(
                f"code {code} keeps the same domain but its meaning changed "
                f"({old_name!r} -> {new_name!r})"
            )
    return old_map, new_map


def _compare_bits_semantics(old_cls, new_cls, verdict):
    old_map = _named_map(old_cls)
    new_map = _named_map(new_cls)
    if old_map == new_map:
        return
    verdict.verdict = MIGRATION_REQUIRED
    old_by_position = {position: name for name, position in old_map.items()}
    new_by_position = {position: name for name, position in new_map.items()}
    for position, old_name in sorted(old_by_position.items()):
        new_name = new_by_position.get(position)
        if new_name is not None and new_name != old_name:
            verdict.reasons.append(
                f"bit {position} renamed {old_name!r} -> {new_name!r}"
            )
        elif new_name is None:
            verdict.reasons.append(
                f"named bit {old_name!r} ({position}) removed from the new definition"
            )
    for position, new_name in sorted(new_by_position.items()):
        if position not in old_by_position:
            verdict.reasons.append(
                f"newly named bit {new_name!r} at position {position} assigns meaning "
                "to previously unnamed raw bits"
            )
    old_named = tuple(old_map)
    if old_named:
        old_ok, _ = _validate(old_cls, old_named)
        new_ok, new_message = _validate(new_cls, old_named)
        if old_ok and not new_ok:
            verdict.counterexamples.append(
                Counterexample(
                    value=old_named,
                    representation=f"named bits {old_named!r}",
                    old_result="accepted",
                    new_result=new_message,
                    detail="named-bit tuple valid under the old definition",
                )
            )
    for position in range(max(old_map.values(), default=-1) + 1):
        octet_index, remainder = divmod(position, 8)
        raw = bytearray(octet_index + 1)
        raw[octet_index] = 0x01 << (7 - remainder)
        raw = bytes(raw)
        old_pretty = old_cls(raw).prettyOut(raw)
        new_pretty = new_cls(raw).prettyOut(raw)
        if old_pretty != new_pretty:
            verdict.reasons.append(
                f"raw bits {raw.hex()} change meaning: {old_pretty!r} -> {new_pretty!r}"
            )


def _compare_integer_domain(old_cls, new_cls, old_map, verdict):
    gaps = []
    old_domain = _numeric_domain(old_cls.subtypeSpec, gaps)
    new_gaps = []
    new_domain = _numeric_domain(new_cls.subtypeSpec, new_gaps)
    verdict.gaps.extend(gaps)
    verdict.gaps.extend(new_gaps)

    if old_domain is not None and new_domain is not None:
        if _ranges_subset(old_domain, new_domain):
            return
        verdict.reasons.append("new value range does not cover the old integer domain")
    elif not verdict.gaps:
        verdict.gaps.append("integer domain could not be reduced to ranges")

    for value in _integer_candidates(old_domain, new_domain, old_map):
        _check_value(
            (old_cls, new_cls),
            value,
            f"integer {value}",
            "boundary or enumerated value of the compared domains",
            verdict.counterexamples,
        )


def _compare_octet_domain(old_cls, new_cls, verdict):
    gaps = []
    old_sizes = _size_domain(old_cls.subtypeSpec, gaps)
    new_gaps = []
    new_sizes = _size_domain(new_cls.subtypeSpec, new_gaps)
    alphabet_gaps = []
    old_alphabet = _alphabet_sets(old_cls.subtypeSpec, alphabet_gaps)
    new_alphabet = _alphabet_sets(new_cls.subtypeSpec, alphabet_gaps)
    verdict.gaps.extend(gaps + new_gaps + alphabet_gaps)

    size_proven = (
        old_sizes is not None
        and new_sizes is not None
        and _ranges_subset(old_sizes, new_sizes)
    )
    if old_sizes is not None and new_sizes is not None and not size_proven:
        verdict.reasons.append("new SIZE constraint does not cover the old sizes")

    alphabet_proven = old_alphabet.issubset(new_alphabet)
    if not alphabet_proven:
        verdict.reasons.append("new permitted alphabet drops old byte values")

    if size_proven and alphabet_proven and not verdict.gaps:
        return

    for size, raw in _size_candidates(old_sizes, new_sizes, old_alphabet):
        _check_value(
            (old_cls, new_cls),
            raw,
            f"{size}-octet value {raw!r}",
            "boundary size of the old domain",
            verdict.counterexamples,
        )
    for byte_value in sorted(old_alphabet):
        raw = bytes([byte_value])
        _check_value(
            (old_cls, new_cls),
            raw,
            f"single octet 0x{byte_value:02x}",
            "permitted alphabet of the old definition",
            verdict.counterexamples,
        )


def analyze_column(old_column, new_column):
    """Compare one column loaded from two isolated builders."""
    old_cls = type(old_column.getSyntax())
    new_cls = type(new_column.getSyntax())
    verdict = ColumnVerdict(
        column=old_column.getLabel(),
        oid=old_column.name,
        verdict=REUSABLE,
        old_wire_type=_wire_type_name(old_cls),
        new_wire_type=_wire_type_name(new_cls),
    )

    _compare_wire_type(old_cls, new_cls, verdict)
    if old_cls.tagSet != new_cls.tagSet:
        return verdict

    old_map = _named_map(old_cls)
    if _is_enum(old_cls) or _is_enum(new_cls):
        _compare_enum_semantics(old_cls, new_cls, verdict)

    if _is_bits(old_cls) or _is_bits(new_cls):
        _compare_bits_semantics(old_cls, new_cls, verdict)
        _compare_octet_domain(old_cls, new_cls, verdict)
    elif _is_integer_family(old_cls) and _is_integer_family(new_cls):
        _compare_integer_domain(old_cls, new_cls, old_map, verdict)
    elif _is_octet_family(old_cls) and _is_octet_family(new_cls):
        _compare_octet_domain(old_cls, new_cls, verdict)
    else:
        verdict.gaps.append(
            f"no structural rule for {old_cls.__name__} vs {new_cls.__name__}"
        )

    if verdict.counterexamples or verdict.verdict == MIGRATION_REQUIRED:
        verdict.verdict = MIGRATION_REQUIRED
    elif verdict.gaps:
        verdict.verdict = UNPROVABLE
    return verdict


def analyze_columns(old_builder, new_builder, module_name):
    """Compare all table columns exported by two versions of one module."""
    (old_column_cls,) = old_builder.import_symbols("SNMPv2-SMI", "MibTableColumn")
    (new_column_cls,) = new_builder.import_symbols("SNMPv2-SMI", "MibTableColumn")

    old_builder.load_modules(module_name)
    new_builder.load_modules(module_name)
    old_symbols = old_builder.mibSymbols[module_name]
    new_symbols = new_builder.mibSymbols[module_name]
    verdicts = []
    for name, old_node in old_symbols.items():
        if not isinstance(old_node, old_column_cls):
            continue
        new_node = new_symbols.get(name)
        if not isinstance(new_node, new_column_cls):
            verdicts.append(
                ColumnVerdict(
                    column=name,
                    oid=old_node.name,
                    verdict=UNPROVABLE,
                    old_wire_type=_wire_type_name(type(old_node.getSyntax())),
                    new_wire_type="column absent",
                    reasons=["column removed or renamed in the new module"],
                    gaps=["cannot compare a column that has no new counterpart"],
                )
            )
            continue
        verdicts.append(analyze_column(old_node, new_node))
    return verdicts


def compare_mib_versions(old_source_dir, new_source_dir, module_name):
    """Load two module versions in isolation and return column verdicts."""
    old_builder = load_isolated_builder(old_source_dir)
    new_builder = load_isolated_builder(new_source_dir)
    return analyze_columns(old_builder, new_builder, module_name)
