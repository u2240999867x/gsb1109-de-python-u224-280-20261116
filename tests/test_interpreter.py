from __future__ import annotations

import pytest

import cbor2
from cbor2 import (
    BatchOutcome,
    CBORTag,
    CorruptEncodingError,
    CyclicInterpretationError,
    InterpretationSession,
    InterpretedObject,
    LabelDictionary,
    LabelRule,
    LabelRuleRejected,
    MissingDictionaryError,
    PendingStatus,
)

TAG_SENSOR = 40000
TAG_ACTUATOR = 40001

SENSOR_RULE = LabelRule(tag=TAG_SENSOR, dictionary_id="sensors", label_key="label")
ACTUATOR_RULE = LabelRule(
    tag=TAG_ACTUATOR, dictionary_id="actuators", label_key="label", required_version=2
)


def sensor_tag(label: str, **extra: object) -> CBORTag:
    return CBORTag(TAG_SENSOR, {"label": label, **extra})


def encode(value: object) -> bytes:
    return cbor2.dumps(value, value_sharing=True)


def make_session(*rules: LabelRule) -> InterpretationSession:
    return InterpretationSession(rules or (SENSOR_RULE, ACTUATOR_RULE))


def test_pending_until_dictionary_supplied():
    session = make_session()
    session.add_batch(encode([sensor_tag("S1")]))

    [outcome] = session.process()
    assert isinstance(outcome, BatchOutcome)
    assert outcome.successful() == {}
    assert not outcome.is_complete
    pending = outcome.pending()
    assert len(pending) == 1
    assert pending[0].status is PendingStatus.pending
    with pytest.raises(MissingDictionaryError):
        outcome.get(0)

    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))
    [outcome] = session.process()
    assert outcome.is_complete
    result = outcome.get(0)
    assert isinstance(result, InterpretedObject)
    assert result.label == "S1"
    assert result.name == "thermometer"
    assert result.payload == {"label": "S1"}


def test_dictionary_version_gating():
    rule = LabelRule(tag=TAG_SENSOR, dictionary_id="sensors", required_version=3)
    session = InterpretationSession([rule])
    session.add_batch(encode([sensor_tag("S1")]))

    session.add_dictionary(LabelDictionary("sensors", 2, {"S1": "old"}))
    [outcome] = session.process()
    assert outcome.pending()[0].status is PendingStatus.pending
    with pytest.raises(MissingDictionaryError) as exc:
        outcome.get(0)
    assert exc.value.required_version == 3

    session.add_dictionary(LabelDictionary("sensors", 3, {"S1": "new"}))
    [outcome] = session.process()
    assert outcome.get(0).name == "new"

    # An older dictionary must never downgrade the active one.
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "ancient"}))
    [outcome] = session.process()
    assert outcome.get(0).name == "new"


def test_shared_object_interpreted_once_with_single_identity():
    tag = sensor_tag("S1")
    session = make_session()
    session.add_batch(encode([tag, tag]))

    [pending] = session.objects
    assert pending.top_indexes == frozenset({(0, 0), (0, 1)})

    # First run: the dictionary is missing, so the object makes a blocked
    # attempt but stays pending (no half-finished interpretation is exposed).
    [blocked] = session.process()
    assert blocked.pending()
    assert pending.attempts == 1

    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))
    [outcome] = session.process()

    values = outcome.successful()
    assert values[0] is values[1]
    assert isinstance(values[0], InterpretedObject)
    assert pending.attempts == 2  # one blocked attempt, one confirmed attempt


def test_confirmed_interpretation_is_not_reexecuted():
    calls = []

    def validator(payload: object) -> None:
        calls.append(payload)

    rule = LabelRule(tag=TAG_SENSOR, dictionary_id="sensors", validator=validator)
    session = InterpretationSession([rule])
    session.add_batch(encode([sensor_tag("S1")]))
    dictionary = LabelDictionary("sensors", 1, {"S1": "thermometer"})

    session.add_dictionary(dictionary)
    session.process()
    session.process()
    session.process()

    assert len(calls) == 1
    [pending] = session.objects
    assert pending.status is PendingStatus.published
    assert pending.attempts == 1


def test_independent_top_level_objects_are_isolated_on_failure():
    good = sensor_tag("S1")
    # Structurally invalid label payload -> terminal rule rejection.
    bad = CBORTag(TAG_SENSOR, {"other": "value"})
    session = make_session()
    session.add_batch(encode([good, bad, 42, "plain"]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    [outcome] = session.process()
    assert outcome.get(0).name == "thermometer"
    assert outcome.get(2) == 42
    assert outcome.get(3) == "plain"

    with pytest.raises(LabelRuleRejected):
        outcome.get(1)

    [failure] = outcome.failures()
    assert failure.item_index == 1
    assert isinstance(failure.error, LabelRuleRejected)
    assert not outcome.is_complete


def test_three_failure_kinds_are_distinct():
    session = make_session()
    corrupt_id = session.add_batch(b"\x42\x61")
    good_batch = session.add_batch(
        encode(
            [
                sensor_tag("S1"),
                CBORTag(TAG_SENSOR, {"other": "value"}),
                CBORTag(999, "untagged-rule"),
            ]
        )
    )
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    outcomes = session.process()
    corrupt_outcome, good_outcome = outcomes

    assert corrupt_outcome.batch_id == corrupt_id
    assert isinstance(corrupt_outcome.failures()[0].error, CorruptEncodingError)
    with pytest.raises(CorruptEncodingError):
        corrupt_outcome.get(0)
    assert corrupt_outcome.successful() == {}

    assert good_outcome.batch_id == good_batch
    assert good_outcome.get(0).name == "thermometer"
    # Malformed payload -> rule rejection; unknown tags without a rule pass through.
    passthrough = good_outcome.get(2)
    assert passthrough.tag == 999
    assert passthrough.value == "untagged-rule"
    with pytest.raises(LabelRuleRejected):
        good_outcome.get(1)

    # Missing dictionary is non-terminal and distinct from the other two.
    other = make_session()
    other.add_batch(encode([sensor_tag("S9")]))
    [other_outcome] = other.process()
    with pytest.raises(MissingDictionaryError):
        other_outcome.get(0)
    assert other_outcome.failures() == ()


def test_undefined_label_stays_pending_until_dictionary_completes():
    session = make_session()
    session.add_batch(encode([sensor_tag("S1"), sensor_tag("S2")]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "first"}))

    [outcome] = session.process()
    assert outcome.get(0).name == "first"
    with pytest.raises(MissingDictionaryError) as exc:
        outcome.get(1)
    assert exc.value.label == "S2"
    assert outcome.failures() == ()

    session.add_dictionary(LabelDictionary("sensors", 2, {"S1": "first", "S2": "second"}))
    [outcome] = session.process()
    assert outcome.is_complete
    assert outcome.get(1).name == "second"


def test_rejected_object_is_terminal_and_never_retried():
    calls = []

    def validator(payload: object) -> None:
        calls.append(payload)
        raise ValueError("always rejected")

    rule = LabelRule(tag=TAG_SENSOR, dictionary_id="sensors", validator=validator)
    session = InterpretationSession([rule])
    session.add_batch(encode([sensor_tag("S1")]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    session.process()
    session.process()
    session.process()

    [pending] = session.objects
    assert pending.status is PendingStatus.failed
    assert isinstance(pending.error, LabelRuleRejected)
    # The validator ran exactly once; later runs never re-executed it.
    assert len(calls) == 1
    assert pending.attempts == 1


def test_validator_rejection():
    def reject_even_versions(payload: object) -> None:
        if payload["seq"] % 2 == 0:
            raise ValueError("even sequence not allowed")

    rule = LabelRule(tag=TAG_SENSOR, dictionary_id="sensors", validator=reject_even_versions)
    session = InterpretationSession([rule])
    session.add_batch(encode([sensor_tag("S1", seq=2)]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    [outcome] = session.process()
    with pytest.raises(LabelRuleRejected):
        outcome.get(0)


def test_missing_label_key_is_rejected():
    session = make_session()
    session.add_batch(encode([CBORTag(TAG_SENSOR, {"other": "value"})]))
    session.add_dictionary(LabelDictionary("sensors", 1, {}))

    [outcome] = session.process()
    with pytest.raises(LabelRuleRejected):
        outcome.get(0)


def test_dependency_ordering_drives_interpretation():
    inner = CBORTag(TAG_ACTUATOR, {"label": "A1"})
    outer = CBORTag(TAG_SENSOR, {"label": "S1", "part": inner})
    session = make_session()
    session.add_batch(encode([outer]))

    session.process()
    outer_pending, inner_pending = sorted(session.objects, key=lambda obj: obj.id)
    assert inner_pending in outer_pending.dependencies

    session.add_dictionary(LabelDictionary("actuators", 2, {"A1": "valve"}))
    session.process()
    assert inner_pending.status is PendingStatus.published
    assert outer_pending.status is PendingStatus.pending

    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "rig"}))
    [outcome] = session.process()
    result = outcome.get(0)
    assert result.name == "rig"
    assert result.payload["part"] is inner_pending.interpretation


def test_cycle_without_legal_ordering_reports_blocking_references():
    map_a: dict[str, object] = {}
    map_b: dict[str, object] = {}
    tag_a = CBORTag(TAG_SENSOR, map_a)
    tag_b = CBORTag(TAG_ACTUATOR, map_b)
    map_a["peer"] = tag_b
    map_b["peer"] = tag_a

    session = make_session()
    session.add_batch(encode([tag_a, tag_b]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "a"}))
    session.add_dictionary(LabelDictionary("actuators", 2, {"A1": "b"}))

    [outcome] = session.process()
    assert outcome.successful() == {}
    failed = session.objects
    assert all(obj.status is PendingStatus.failed for obj in failed)
    errors = {obj.error for obj in failed}
    assert len(errors) == 1
    error = errors.pop()
    assert isinstance(error, CyclicInterpretationError)
    id_a = next(obj.id for obj in failed if obj.rule.tag == TAG_SENSOR)
    id_b = next(obj.id for obj in failed if obj.rule.tag == TAG_ACTUATOR)
    assert set(error.blocking_references) == {(id_a, id_b), (id_b, id_a)}
    with pytest.raises(CyclicInterpretationError):
        outcome.get(0)


def test_structural_self_reference_has_legal_ordering():
    payload: dict[str, object] = {}
    tag = CBORTag(TAG_SENSOR, payload)
    payload["self"] = tag
    payload["label"] = "S1"

    session = make_session()
    session.add_batch(encode([tag]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    [outcome] = session.process()
    result = outcome.get(0)
    assert isinstance(result, InterpretedObject)
    assert result.payload["self"] is result


def test_sessions_using_different_dictionaries_are_isolated():
    session_a = make_session()
    session_b = make_session()
    session_a.add_batch(encode([sensor_tag("S1")]))
    session_b.add_batch(encode([sensor_tag("S1")]))

    session_b.add_dictionary(LabelDictionary("sensors", 1, {"S1": "from-b"}))
    outcomes_a = session_a.process()
    outcomes_b = session_b.process()

    assert outcomes_a[0].pending()
    with pytest.raises(MissingDictionaryError):
        outcomes_a[0].get(0)
    assert outcomes_b[0].get(0).name == "from-b"
    assert len(session_a.objects) == 1
    assert len(session_b.objects) == 1
    assert session_a.objects[0] is not session_b.objects[0]


def test_corrupt_batch_does_not_affect_other_batches():
    session = make_session()
    session.add_batch(b"\x42\x61")
    session.add_batch(encode([sensor_tag("S1")]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    corrupt_outcome, good_outcome = session.process()
    assert isinstance(corrupt_outcome.failures()[0].error, CorruptEncodingError)
    assert good_outcome.get(0).name == "thermometer"


def test_batches_added_after_processing_join_existing_state():
    session = make_session()
    first_id = session.add_batch(encode([sensor_tag("S1")]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "first"}))
    [first_outcome] = session.process()
    assert first_outcome.get(0).name == "first"

    second_id = session.add_batch(encode([sensor_tag("S2")]))
    outcomes = session.process()
    assert outcomes[0].batch_id == first_id
    assert outcomes[1].batch_id == second_id
    assert outcomes[0].is_complete
    assert outcomes[1].pending()

    session.add_dictionary(LabelDictionary("sensors", 2, {"S1": "first", "S2": "second"}))
    outcomes = session.process()
    assert outcomes[0].get(0).name == "first"
    assert outcomes[1].get(0).name == "second"


def test_shared_nested_structure_keeps_single_interpretation():
    shared = sensor_tag("S1")
    root = {"left": [shared], "right": {"deep": shared}}
    session = make_session()
    session.add_batch(encode([root]))
    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))

    [outcome] = session.process()
    result = outcome.get(0)
    assert result["left"][0] is result["right"]["deep"]
    assert isinstance(result["left"][0], InterpretedObject)


def test_blocked_container_is_not_reused_half_built_after_dictionary_arrives():
    # The blocked label sits after an already materialized key.  Without
    # rebuilding views per run, the first (aborted) map would be cached without
    # its second key.
    root = {"ok": "plain", "blocked": sensor_tag("S1")}
    session = make_session()
    session.add_batch(encode([root]))

    [outcome] = session.process()
    assert outcome.successful() == {}

    session.add_dictionary(LabelDictionary("sensors", 1, {"S1": "thermometer"}))
    [outcome] = session.process()
    result = outcome.get(0)
    assert result["ok"] == "plain"
    assert isinstance(result["blocked"], InterpretedObject)
    assert result["blocked"].name == "thermometer"
