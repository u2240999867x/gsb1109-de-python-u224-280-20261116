from __future__ import annotations

import math
from fractions import Fraction

import pytest

import cbor2
from cbor2 import CBORTag
from cbor2.measurements import (
    TAG_ABSOLUTE,
    TAG_AFFINE,
    AffineRelation,
    Anchor,
    MeasurementFormatError,
    SampleId,
    exact_fraction,
    parse_records,
    reconcile,
)

S = SampleId


def sample(generation: int, number: int, sensor: str = "sensor") -> SampleId:
    return S(sensor, generation, number)


def test_exact_fraction_from_rational_tag():
    assert exact_fraction(CBORTag(30, [2, 5])) == Fraction(2, 5)
    assert exact_fraction(CBORTag(4, [-3, 14123])) == Fraction(14123, 1000)
    assert exact_fraction(CBORTag(5, [-2, 3])) == Fraction(3, 4)
    assert exact_fraction("1/3") == Fraction(1, 3)
    assert exact_fraction([7, 3]) == Fraction(7, 3)


def test_exact_fraction_rejects_non_finite_float():
    with pytest.raises(MeasurementFormatError):
        exact_fraction(math.inf)


def test_exact_fraction_never_loses_precision():
    value = Fraction(1, 3) + Fraction(1, 7)
    assert exact_fraction(cbor2.loads(cbor2.dumps(value))) == value


def test_anchor_and_relation_chain():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(10))],
        relations=[AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(1))],
    )
    assert result.confirmed == {
        sample(1, 0): Fraction(10),
        sample(1, 1): Fraction(21),
    }
    assert result.conflicts == ()


def test_basis_records_which_anchor_supports_value():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(10), source="anchor-0")],
        relations=[
            AffineRelation(
                sample(1, 0), sample(1, 1), Fraction(2), Fraction(1), source="rel-0"
            )
        ],
    )
    evidence = result.evidence[sample(1, 1)]
    kinds = {item.kind for item in evidence}
    assert kinds == {"anchor", "relation"}
    assert {item.source for item in evidence} == {"anchor-0", "rel-0"}


def test_disagreeing_anchors_are_reported_and_not_guessed():
    result = reconcile(
        anchors=[
            Anchor(sample(1, 0), Fraction(1), source="a"),
            Anchor(sample(1, 0), Fraction(2), source="b"),
        ]
    )
    assert result.confirmed == {}
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind == "anchor_conflict"
    assert conflict.values == (Fraction(1), Fraction(2))
    assert conflict.sources == ("a", "b")


def test_conflicting_anchor_does_not_erase_other_components():
    result = reconcile(
        anchors=[
            Anchor(sample(1, 0), Fraction(1)),
            Anchor(sample(1, 0), Fraction(2)),
            Anchor(sample(1, 10), Fraction(50)),
        ],
        relations=[AffineRelation(sample(1, 10), sample(1, 11), Fraction(1), Fraction(1))],
    )
    assert result.confirmed == {
        sample(1, 10): Fraction(50),
        sample(1, 11): Fraction(51),
    }
    assert [conflict.kind for conflict in result.conflicts] == ["anchor_conflict"]


def test_anchorless_cycle_with_unique_fixed_point():
    result = reconcile(
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(1), Fraction(0)),
            AffineRelation(sample(1, 1), sample(1, 2), Fraction(1), Fraction(0)),
            AffineRelation(sample(1, 2), sample(1, 0), Fraction(2), Fraction(3)),
        ]
    )
    assert result.confirmed == {
        sample(1, 0): Fraction(-3),
        sample(1, 1): Fraction(-3),
        sample(1, 2): Fraction(-3),
    }
    assert result.conflicts == ()


def test_anchorless_two_cycle_fixes_both_values():
    result = reconcile(
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(1)),
            AffineRelation(sample(1, 1), sample(1, 0), Fraction(2), Fraction(1)),
        ]
    )
    assert result.confirmed == {
        sample(1, 0): Fraction(-1),
        sample(1, 1): Fraction(-1),
    }


def test_anchorless_relation_remains_undetermined():
    result = reconcile(
        relations=[AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(3))]
    )
    assert result.confirmed == {}
    assert len(result.undetermined) == 1
    relation = result.undetermined[0]
    assert relation.samples == (sample(1, 0), sample(1, 1))
    assert relation.evaluate({sample(1, 0): Fraction(4), sample(1, 1): Fraction(11)}) == 0
    assert result.expressions[sample(1, 0)].evaluate({sample(1, 1): Fraction(11)}) == 4


def test_bad_anchorless_cycle_keeps_other_constraints():
    result = reconcile(
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(1), Fraction(1)),
            AffineRelation(sample(1, 1), sample(1, 2), Fraction(1), Fraction(1)),
            AffineRelation(sample(1, 2), sample(1, 0), Fraction(1), Fraction(1)),
        ]
    )
    assert result.confirmed == {}
    assert [conflict.kind for conflict in result.conflicts] == ["relation_conflict"]
    # Two independent surviving constraints remain, one per free variable.
    assert len(result.undetermined) == 2


def test_rejected_edge_does_not_remove_independent_component():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(10)), Anchor(sample(1, 20), Fraction(7))],
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(1)),
            AffineRelation(sample(1, 0), sample(1, 20), Fraction(3), Fraction(4)),
        ],
    )
    assert result.confirmed[sample(1, 0)] == 10
    assert result.confirmed[sample(1, 1)] == 21
    assert result.confirmed[sample(1, 20)] == 7
    assert [conflict.kind for conflict in result.conflicts] == ["relation_conflict"]


def test_generations_are_never_connected():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(1)), Anchor(sample(2, 0), Fraction(2))],
        relations=[AffineRelation(sample(1, 0), sample(2, 1), Fraction(1), Fraction(0))],
    )
    assert result.confirmed == {
        sample(1, 0): Fraction(1),
        sample(2, 0): Fraction(2),
    }
    assert [conflict.kind for conflict in result.conflicts] == ["cross_generation"]
    assert {component.generation for component in result.components} == {1, 2}


def test_allow_cross_generation_is_opt_in():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(1))],
        relations=[AffineRelation(sample(1, 0), sample(2, 1), Fraction(1), Fraction(0))],
        allow_cross_generation=True,
    )
    assert result.confirmed[sample(2, 1)] == 1


def test_duplicate_relations_count_once():
    relation = AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(0))
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(4))],
        relations=[relation, relation, relation],
    )
    assert result.confirmed[sample(1, 1)] == 8
    assert result.duplicate_relations == 2
    assert len(result.relations) == 1


def test_reverse_form_is_the_same_relation():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(4))],
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(2), Fraction(0)),
            AffineRelation(sample(1, 1), sample(1, 0), Fraction(1, 2), Fraction(0)),
        ],
    )
    assert result.duplicate_relations == 1
    assert result.confirmed[sample(1, 1)] == 8


def test_duplicate_anchors_count_once():
    anchor = Anchor(sample(1, 0), Fraction(5))
    result = reconcile(anchors=[anchor, anchor, anchor])
    assert result.confirmed == {sample(1, 0): Fraction(5)}
    assert result.duplicate_anchors == 2


def test_malformed_record_does_not_abort_valid_component():
    result = reconcile([["broken"], CBORTag(TAG_ABSOLUTE, [["sensor", 1, 0], [1, 2]])])
    assert result.confirmed == {sample(1, 0): Fraction(1, 2)}
    assert len(result.rejected) == 1


def test_strict_mode_raises():
    with pytest.raises(MeasurementFormatError):
        reconcile([["broken"]], strict=True)


def test_parse_cbor_sequence_of_custom_tags():
    data = b"".join(
        [
            cbor2.dumps(CBORTag(TAG_ABSOLUTE, [["sensor", 7, 0], Fraction(1, 3)])),
            cbor2.dumps(
                CBORTag(
                    TAG_AFFINE,
                    [["sensor", 7, 0], ["sensor", 7, 1], Fraction(1, 2), Fraction(0)],
                )
            ),
        ]
    )
    result = reconcile(data)
    assert result.confirmed == {
        sample(7, 0): Fraction(1, 3),
        sample(7, 1): Fraction(1, 6),
    }


def test_parse_cbor_map_envelope():
    data = cbor2.dumps(
        {
            "sensor": "s",
            "anchors": [{"generation": 1, "sample": 0, "value": [1, 3]}],
            "relations": [
                {"generation": 1, "x": 0, "y": 1, "a": [1, 2], "b": [0, 1]}
            ],
        }
    )
    result = reconcile(data)
    assert result.confirmed == {
        S("s", 1, 0): Fraction(1, 3),
        S("s", 1, 1): Fraction(1, 6),
    }


def test_parse_map_uses_exact_rational_tags():
    data = cbor2.dumps(
        {
            "anchors": [
                {
                    "sensor": "s",
                    "generation": 1,
                    "sample": 0,
                    "value": CBORTag(30, [1, 3]),
                }
            ]
        }
    )
    result = reconcile(data)
    assert result.confirmed == {S("s", 1, 0): Fraction(1, 3)}


def test_parse_regrouped_generations():
    result = reconcile(
        {
            "sensor": "s",
            "generations": {
                1: {"samples": {0: Fraction(10), 1: Fraction(20)}},
                2: {"samples": {0: Fraction(30)}},
            },
        }
    )
    assert result.confirmed == {
        S("s", 1, 0): Fraction(10),
        S("s", 1, 1): Fraction(20),
        S("s", 2, 0): Fraction(30),
    }
    assert len({component.generation for component in result.components}) == 2


def test_parse_records_separates_kinds():
    parsed = parse_records(
        [CBORTag(TAG_ABSOLUTE, [["s", 1, 0], 5]), ["s", 1, 0, "s", 1, 1, 2, 1]]
    )
    assert len(parsed.anchors) == 1
    assert len(parsed.relations) == 1
    assert parsed.rejected == ()


def test_zero_slope_relation_pins_target():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(99))],
        relations=[AffineRelation(sample(1, 0), sample(1, 1), Fraction(0), Fraction(5))],
    )
    assert result.confirmed == {
        sample(1, 0): Fraction(99),
        sample(1, 1): Fraction(5),
    }


def test_self_relation_tautology_is_ignored():
    result = reconcile(
        relations=[AffineRelation(sample(1, 0), sample(1, 0), Fraction(1), Fraction(0))]
    )
    assert result.confirmed == {}
    assert result.conflicts == ()


def test_self_relation_contradiction_is_reported():
    result = reconcile(
        relations=[AffineRelation(sample(1, 0), sample(1, 0), Fraction(1), Fraction(1))]
    )
    assert [conflict.kind for conflict in result.conflicts] == ["bad_relation"]


def test_repeated_derivation_does_not_double_count():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(100))],
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(3), Fraction(1)),
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(3), Fraction(1)),
            AffineRelation(sample(1, 1), sample(1, 0), Fraction(1, 3), Fraction(-1, 3)),
        ],
    )
    assert result.confirmed[sample(1, 1)] == 301
    assert result.duplicate_relations == 2
    assert result.conflicts == ()


def test_result_is_json_serialisable_and_keeps_fractions_as_text():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(1, 3))],
        relations=[AffineRelation(sample(1, 0), sample(1, 1), Fraction(1, 7), Fraction(0))],
    )
    payload = result.to_dict()
    assert payload["confirmed"][0]["value"] == "1/3"
    assert payload["relations"][0]["a"] == "1/7"


def test_confirmed_values_never_contain_floats():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(1, 3))],
        relations=[AffineRelation(sample(1, 0), sample(1, 1), Fraction(1, 7), Fraction(1, 11))],
    )
    assert all(isinstance(value, Fraction) for value in result.confirmed.values())


def test_conflicted_sample_does_not_propagate():
    result = reconcile(
        anchors=[
            Anchor(sample(1, 0), Fraction(1)),
            Anchor(sample(1, 0), Fraction(2)),
            Anchor(sample(1, 5), Fraction(7)),
        ],
        relations=[
            AffineRelation(sample(1, 0), sample(1, 1), Fraction(1), Fraction(1)),
            AffineRelation(sample(1, 5), sample(1, 6), Fraction(2), Fraction(0)),
        ],
    )
    assert result.confirmed == {
        sample(1, 5): Fraction(7),
        sample(1, 6): Fraction(14),
    }
    assert result.conflicted == (sample(1, 0),)
    assert {conflict.kind for conflict in result.conflicts} == {"anchor_conflict"}


def test_cross_generation_relations_do_not_merge_components():
    result = reconcile(
        anchors=[Anchor(sample(1, 0), Fraction(1)), Anchor(sample(2, 0), Fraction(2))],
        relations=[AffineRelation(sample(1, 0), sample(2, 0), Fraction(1), Fraction(0))],
    )
    assert [component.samples for component in result.components] == [
        (sample(1, 0),),
        (sample(2, 0),),
    ]


def test_compact_relation_layout():
    data = CBORTag(
        TAG_AFFINE, ["sensor", 7, 0, 1, {"n": 2, "d": 1}, {"n": 1, "d": 1}]
    )
    result = reconcile(
        [CBORTag(TAG_ABSOLUTE, ["sensor", 7, 0, {"n": 10, "d": 1}]), data]
    )
    assert result.confirmed == {
        sample(7, 0): Fraction(10),
        sample(7, 1): Fraction(21),
    }


def test_relation_map_with_samples_key():
    result = reconcile(
        [
            {
                "samples": [["s", 1, 0], ["s", 1, 1]],
                "coefficients": [[2, 1], [1, 1]],
            }
        ],
        anchors=[Anchor(S("s", 1, 0), Fraction(3))],
    )
    assert result.confirmed[S("s", 1, 1)] == 7


def test_eight_field_relation_uses_both_endpoints():
    records = [
        CBORTag(TAG_ABSOLUTE, ["temp", 1, 0, {"n": 10, "d": 1}]),
        CBORTag(
            TAG_AFFINE,
            ["temp", 1, 0, "temp", 1, 1, {"n": 2, "d": 1}, {"n": 1, "d": 1}],
        ),
    ]
    result = reconcile(records)
    assert result.confirmed == {
        S("temp", 1, 0): Fraction(10),
        S("temp", 1, 1): Fraction(21),
    }


def test_relation_can_be_cross_generation_in_endpoint_form():
    result = reconcile(
        [
            CBORTag(TAG_ABSOLUTE, [["temp", 1, 0], 5]),
            CBORTag(TAG_AFFINE, [["temp", 1, 0], ["temp", 2, 0], 1, 0]),
        ]
    )
    assert [conflict.kind for conflict in result.conflicts] == ["cross_generation"]


def test_ambiguous_eight_field_relation_is_rejected():
    result = reconcile([CBORTag(TAG_AFFINE, ["temp", 1, 0, "temp", 2, 0, 1, 0])])
    assert [record.reason for record in result.rejected] == ["malformed_relation"]
