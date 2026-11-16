"""Two-version MIB column compatibility analysis.

The fixture module GSB-COMPAT-MIB exists twice on purpose: each revision is
executed by its own MibBuilder, so every verdict below is derived from
independently loaded classes rather than from shared module state.
"""

from pathlib import Path

import pytest
from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.error import PyAsn1Error

from pysnmp.smi.compat import (
    MIGRATION_REQUIRED,
    REUSABLE,
    UNPROVABLE,
    analyze_columns,
    load_isolated_builder,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mib_compat"
OLD_DIR = FIXTURE_ROOT / "v1"
NEW_DIR = FIXTURE_ROOT / "v2"
MODULE = "GSB-COMPAT-MIB"


@pytest.fixture(scope="module")
def builders():
    old_builder = load_isolated_builder(OLD_DIR)
    new_builder = load_isolated_builder(NEW_DIR)
    old_builder.load_modules(MODULE)
    new_builder.load_modules(MODULE)
    return old_builder, new_builder


@pytest.fixture(scope="module")
def verdicts(builders):
    old_builder, new_builder = builders
    return {
        verdict.column: verdict
        for verdict in analyze_columns(old_builder, new_builder, MODULE)
    }


def _syntax(builder, column):
    (node,) = builder.import_symbols(MODULE, column)
    return type(node.getSyntax())


def test_two_builders_load_isolated_class_objects(builders):
    old_builder, new_builder = builders
    old_cls = _syntax(old_builder, "stableRange")
    new_cls = _syntax(new_builder, "stableRange")
    assert old_cls is not new_cls
    assert old_builder.mibSymbols is not new_builder.mibSymbols
    assert MODULE in old_builder.mibSymbols
    assert MODULE in new_builder.mibSymbols


def test_unloading_one_builder_does_not_touch_the_other(builders):
    old_builder, new_builder = builders
    old_builder.unload_modules(MODULE)
    assert MODULE not in old_builder.mibSymbols
    assert MODULE in new_builder.mibSymbols
    old_builder.load_modules(MODULE)


def test_verdict_matrix(verdicts):
    expected = {
        "stableEnum": REUSABLE,
        "stableRange": REUSABLE,
        "widenedRange": REUSABLE,
        "flagBits": REUSABLE,
        "alphabetWidened": REUSABLE,
        "exclusionSeven": REUSABLE,
        "narrowedRange": MIGRATION_REQUIRED,
        "tagChanged": MIGRATION_REQUIRED,
        "phaseEnum": MIGRATION_REQUIRED,
        "statusCode": MIGRATION_REQUIRED,
        "roleBits": MIGRATION_REQUIRED,
        "stringSize": MIGRATION_REQUIRED,
        "alphabetRestricted": MIGRATION_REQUIRED,
        "familyChanged": MIGRATION_REQUIRED,
        "containedGap": UNPROVABLE,
    }
    assert {name: verdict.verdict for name, verdict in verdicts.items()} == expected


def test_reusable_columns_carry_no_counterexamples(verdicts):
    for name in (
        "stableEnum",
        "stableRange",
        "widenedRange",
        "flagBits",
        "alphabetWidened",
        "exclusionSeven",
    ):
        assert verdicts[name].counterexamples == []
        assert verdicts[name].gaps == []


def test_narrowed_range_has_concrete_old_accepted_new_rejected_value(
    builders, verdicts
):
    old_builder, new_builder = builders
    old_cls = _syntax(old_builder, "narrowedRange")
    new_cls = _syntax(new_builder, "narrowedRange")
    assert old_cls(1) is not None
    with pytest.raises(PyAsn1Error):
        new_cls(1)
    representations = {
        counter.representation for counter in verdicts["narrowedRange"].counterexamples
    }
    assert "integer 1" in representations


def test_enum_same_domain_but_renamed_is_not_lossless(verdicts):
    phase = verdicts["phaseEnum"]
    assert any("renamed" in reason for reason in phase.reasons)
    assert any("meaning changed" in reason for reason in phase.reasons)
    status = verdicts["statusCode"]
    assert any("testing' -> 'standby'" in reason for reason in status.reasons)


def test_bits_rename_reports_raw_bit_meaning_change(verdicts):
    role = verdicts["roleBits"]
    assert any("bit 1 renamed" in reason for reason in role.reasons)
    assert any("40 change meaning" in reason for reason in role.reasons)
    named_counter = next(
        counter
        for counter in role.counterexamples
        if "named bits" in counter.representation
    )
    assert named_counter.old_result == "accepted"
    assert "Unknown named bit" in named_counter.new_result


def test_size_constraint_narrowing_counterexample(builders, verdicts):
    old_builder, new_builder = builders
    old_cls = _syntax(old_builder, "stringSize")
    new_cls = _syntax(new_builder, "stringSize")
    eight_nulls = b"\x00" * 8
    assert old_cls(eight_nulls) is not None
    with pytest.raises(PyAsn1Error):
        new_cls(eight_nulls)
    assert any(
        "8-octet" in counter.representation
        for counter in verdicts["stringSize"].counterexamples
    )


def test_alphabet_restriction_counterexample(builders, verdicts):
    old_builder, new_builder = builders
    old_cls = _syntax(old_builder, "alphabetRestricted")
    new_cls = _syntax(new_builder, "alphabetRestricted")
    assert old_cls(b"D") is not None
    with pytest.raises(PyAsn1Error):
        new_cls(b"D")


def test_wire_tag_change_is_proven_with_ber_tlv(builders, verdicts):
    old_builder, new_builder = builders
    for column in ("tagChanged", "familyChanged"):
        verdict = verdicts[column]
        tlv_counter = next(
            counter
            for counter in verdict.counterexamples
            if counter.representation.startswith("BER TLV")
        )
        old_cls = _syntax(old_builder, column)
        new_cls = _syntax(new_builder, column)
        old_value = old_cls(tlv_counter.value)
        tlv = tlv_counter.representation.split()[-1]
        assert bytes.fromhex(tlv)
        with pytest.raises(PyAsn1Error):
            ber_decoder.decode(bytes.fromhex(tlv), asn1Spec=new_cls())
        assert old_value is not None


def test_passing_observations_do_not_prove_domain_compatibility(builders, verdicts):
    old_builder, new_builder = builders
    old_cls = _syntax(old_builder, "narrowedRange")
    new_cls = _syntax(new_builder, "narrowedRange")
    observed = (10, 50, 90)
    assert all(old_cls(value) is not None for value in observed)
    assert all(new_cls(value) is not None for value in observed)
    assert verdicts["narrowedRange"].verdict == MIGRATION_REQUIRED
    assert any(
        counter.value == 1 for counter in verdicts["narrowedRange"].counterexamples
    )


def test_unprovable_constraint_leaves_an_explicit_gap(verdicts):
    verdict = verdicts["containedGap"]
    assert verdict.verdict == UNPROVABLE
    assert verdict.counterexamples == []
    assert any("ContainedSubtypeConstraint" in gap for gap in verdict.gaps)


def test_every_reported_counterexample_splits_old_and_new_validation(
    builders, verdicts
):
    old_builder, new_builder = builders
    for verdict in verdicts.values():
        old_cls = _syntax(old_builder, verdict.column)
        new_cls = _syntax(new_builder, verdict.column)
        for counter in verdict.counterexamples:
            if counter.representation.startswith("BER TLV"):
                wire_bytes = bytes.fromhex(counter.representation.split()[-1])
                with pytest.raises(PyAsn1Error):
                    ber_decoder.decode(wire_bytes, asn1Spec=new_cls())
            else:
                assert old_cls(counter.value) is not None
                with pytest.raises(PyAsn1Error):
                    new_cls(counter.value)


def test_column_missing_in_new_revision_is_unprovable():
    old_builder = load_isolated_builder(OLD_DIR)
    new_builder = load_isolated_builder(NEW_DIR)
    old_builder.load_modules(MODULE)
    new_builder.load_modules(MODULE)
    del new_builder.mibSymbols[MODULE]["stableEnum"]
    verdicts = analyze_columns(old_builder, new_builder, MODULE)
    absent = next(verdict for verdict in verdicts if verdict.column == "stableEnum")
    assert absent.verdict == UNPROVABLE
    assert absent.new_wire_type == "column absent"
