from dbus_next import Message, MessageType, PropertyAccess, Variant
from dbus_next.constants import EmitsChangedSignal
from dbus_next import introspection as intr
from dbus_next.property_compaction import (CompactionConfig, ObservationKind,
                                           ObservationPoint, PropertyChange,
                                           compact, compact_property_changes)
from dbus_next.service import ServiceInterface, dbus_property

import json
import pytest


INTERFACE = 'com.example.Interface'


def change(serial, changed=None, invalidated=None, interface=INTERFACE, path='/',
           sequence=None):
    return PropertyChange(serial,
                          interface,
                          changed,
                          invalidated,
                          path=path,
                          sequence=sequence)


def config(annotations=None, event_interfaces=None, **kwargs):
    return CompactionConfig(event_interfaces=event_interfaces,
                            annotations=annotations,
                            **kwargs)


def observation(id, property_name, at_serial, interface=INTERFACE, kind='value', **kwargs):
    return ObservationPoint(id, interface, property_name, at_serial, kind=kind, **kwargs)


def answers(result):
    return {state.id: state.answer() for state in result.observations}


def test_consecutive_changes_merge_into_one_batch():
    result = compact_property_changes([
        change(1, {
            'A': 1
        }),
        change(2, {
            'A': 2
        }),
        change(3, {
            'B': 'x'
        }),
    ])

    assert len(result.batches) == 1
    batch = result.batches[0]
    assert batch.serials == [1, 2, 3]
    assert batch.changed == {'A': 2, 'B': 'x'}
    assert batch.invalidated_properties == []
    assert result.records[0]['action'] == 'batch'
    assert result.records[0]['merged'] is True


def test_observation_at_an_intermediate_signal_forces_a_split():
    signals = [change(1, {'A': 1}), change(2, {'A': 2}), change(3, {'A': 3})]
    result = compact_property_changes(signals, [observation('at-2', 'A', 2)])

    assert [batch.serials for batch in result.batches] == [[1, 2], [3]]
    assert result.batches[1].split_reason == 'observation-boundary'
    assert answers(result)['at-2'] == ('known', 2, None)


def test_observation_before_every_signal_keeps_the_first_signal_alone():
    signals = [change(1, {'A': 1}), change(2, {'A': 2}), change(3, {'A': 3})]
    result = compact_property_changes(signals, [observation('at-1', 'A', 1)])

    assert [batch.serials for batch in result.batches] == [[1], [2, 3]]
    assert answers(result)['at-1'] == ('known', 1, None)


def test_invalidation_makes_the_property_unknown_and_never_reuses_the_old_value():
    signals = [
        change(1, {'B': 'old-value'}),
        change(2, invalidated=['B']),
        change(3, {'A': 'unrelated'}),
    ]
    result = compact_property_changes(signals, [
        observation('b', 'B', 1),
        observation('b-after', 'B', 3),
        observation('a', 'A', 3),
    ])

    states = {state.id: state for state in result.observations}
    assert states['b'].value == 'old-value'
    assert states['b-after'].status == 'unknown'
    assert states['b-after'].value is None
    assert states['b-after'].reason == 'invalidated'
    assert states['b-after'].annotation == 'true'
    # The invalidation of B must not leak into A.
    assert states['a'].answer() == ('known', 'unrelated', None)


def test_invalidated_annotation_drops_a_value_reported_in_changed():
    signals = [change(1, {'A': 'stale'}), change(2, {'A': 'current'})]
    result = compact_property_changes(
        signals, [observation('a', 'A', 2)],
        config(annotations={(INTERFACE, 'A'): EmitsChangedSignal.INVALIDATED}))

    batch = result.batches[0]
    assert batch.changed == {}
    assert batch.invalidated_properties == ['A']
    assert batch.notes[0]['reason'] == 'invalidated'
    assert answers(result)['a'] == ('unknown', None, 'invalidated')


def test_invalidated_signal_with_a_value_in_changed_is_still_an_invalidation():
    signals = [change(1, changed={'A': 'untrusted'}, invalidated=['A'])]
    result = compact_property_changes(signals, [observation('a', 'A', 1)])

    assert result.batches[0].changed == {}
    assert result.batches[0].invalidated_properties == ['A']
    assert answers(result)['a'] == ('unknown', None, 'invalidated')


def test_false_annotation_is_not_cacheable_but_reported_changes_are_visible():
    signals = [change(1, {'A': 'v'})]
    annotated = config(annotations={(INTERFACE, 'A'): EmitsChangedSignal.FALSE})

    result = compact_property_changes(signals, [
        observation('a', 'A', 1),
        observation('a-changed', 'A', 1, kind=ObservationKind.CHANGED),
    ], annotated)

    assert answers(result)['a'] == ('unknown', None, 'not-cacheable')
    assert answers(result)['a-changed'] == ('known', True, None)


def test_false_annotation_cannot_answer_a_no_change_window():
    signals = [change(1, {'B': 'b'}), change(2, {'B': 'c'})]
    annotated = config(annotations={(INTERFACE, 'A'): EmitsChangedSignal.FALSE})
    result = compact_property_changes(
        signals, [observation('a-changed', 'A', 2, kind=ObservationKind.CHANGED,
                              after_serial=1)], annotated)

    assert answers(result)['a-changed'] == ('unknown', None, 'unreported-changes')


def test_const_annotation_change_becomes_unknown():
    signals = [change(1, {'A': 'still-the-same'})]
    annotated = config(annotations={(INTERFACE, 'A'): EmitsChangedSignal.CONST})
    result = compact_property_changes(signals, [observation('a', 'A', 1)], annotated)

    assert result.batches[0].invalidated_properties == ['A']
    assert answers(result)['a'] == ('unknown', None, 'const-property-changed')


def test_event_interfaces_keep_every_signal():
    signals = [change(1, {'A': 1}), change(2, {'A': 2}), change(3, {'A': 3})]
    result = compact_property_changes(signals, [], config(event_interfaces=[INTERFACE]))

    assert [batch.serials for batch in result.batches] == [[1], [2], [3]]
    assert all(batch.event and not batch.merged for batch in result.batches)
    assert [batch.split_reason for batch in result.batches] == [
        None, 'event-interface', 'event-interface'
    ]


def test_stream_switch_splits_and_observations_do_not_cross_interfaces():
    signals = [
        change(1, {'A': 1}),
        change(2, {'X': 'x'}, interface='com.example.Other'),
        change(3, {'A': 3}),
    ]
    result = compact_property_changes(signals, [
        observation('a', 'A', 3),
        observation('x', 'X', 2, interface='com.example.Other'),
    ])

    assert [batch.serials for batch in result.batches] == [[1], [2], [3]]
    assert answers(result)['a'] == ('known', 3, None)
    assert answers(result)['x'] == ('known', 'x', None)


def test_gap_makes_only_the_affected_interface_unknown():
    signals = [
        change(1, {'A': 'before'}, sequence=1),
        change(3, {'B': 'after-gap'}, sequence=3),
    ]
    result = compact_property_changes(signals, [
        observation('a-gap', 'A', 2),
        observation('b-gap', 'B', 2),
        observation('a-after', 'A', 3),
        observation('b-after', 'B', 3),
    ])

    assert len(result.gaps) == 1
    assert result.gaps[0].after_serial == 1
    assert result.gaps[0].missing_count == 1
    states = {state.id: state for state in result.observations}
    # The gap may have changed A; the old value must not be filled back in.
    assert states['a-gap'].answer() == ('unknown', None, 'stream-gap')
    assert states['a-after'].answer() == ('unknown', None, 'stream-gap')
    # B was not delivered yet at serial 2 and only a later signal can clear it.
    assert states['b-gap'].answer() == ('unknown', None, 'stream-gap')
    assert states['b-after'].answer() == ('known', 'after-gap', None)


def test_gap_is_cleared_by_a_later_value_for_that_property_only():
    signals = [
        change(1, {'A': 1, 'B': 1}, sequence=1),
        change(3, {'A': 3}, sequence=3),
    ]
    result = compact_property_changes(signals, [
        observation('a', 'A', 3),
        observation('b', 'B', 3),
    ])

    states = {state.id: state for state in result.observations}
    assert states['a'].answer() == ('known', 3, None)
    assert states['b'].answer() == ('unknown', None, 'stream-gap')


def test_gap_does_not_affect_a_different_interface():
    signals = [
        change(1, {'A': 1}, sequence=1),
        change(2, {'X': 1}, interface='com.example.Other', sequence=1),
        change(3, {'A': 3}, sequence=3),
    ]
    result = compact_property_changes(signals, [
        observation('x', 'X', 2, interface='com.example.Other'),
        observation('a', 'A', 2),
        observation('a-after-gap', 'A', 3),
    ])

    assert len(result.gaps) == 1
    assert result.gaps[0].interface == INTERFACE
    states = {state.id: state for state in result.observations}
    assert states['x'].answer() == ('known', 1, None)
    assert states['a'].answer() == ('unknown', None, 'stream-gap')
    # The gap on a.b is still unresolved at serial 3; the other interface's
    # signal neither creates it nor fills it.
    assert states['a-after-gap'].answer() == ('known', 3, None)


def test_interleaved_interfaces_are_not_mistaken_for_a_gap():
    signals = [
        change(1, {'A': 1}),
        change(2, {'X': 1}, interface='com.example.Other'),
        change(3, {'A': 3}),
    ]
    result = compact_property_changes(signals, [observation('a', 'A', 3)],
                                      config(dense_serials=True))

    assert result.gaps == []
    assert answers(result)['a'] == ('known', 3, None)


def test_dense_serials_infer_a_gap_from_serials_only():
    signals = [change(1, {'A': 1}), change(3, {'A': 3})]
    result = compact_property_changes(signals,
                                      [observation('gap', 'A', 2),
                                       observation('after', 'A', 3)],
                                      config(dense_serials=True))

    assert len(result.gaps) == 1
    assert answers(result)['gap'] == ('unknown', None, 'stream-gap')
    assert answers(result)['after'] == ('known', 3, None)


def test_serial_jumps_are_not_a_gap_by_default():
    signals = [change(1, {'A': 1}), change(7, {'A': 7})]
    result = compact_property_changes(signals, [observation('a', 'A', 7)])

    assert result.gaps == []
    assert answers(result)['a'] == ('known', 7, None)


def test_explicit_sequence_detects_a_gap():
    signals = [
        change(10, {'A': 1}, sequence=1),
        change(11, {'A': 2}, sequence=3),
    ]
    result = compact_property_changes(signals, [observation('a', 'A', 11)])

    assert len(result.gaps) == 1
    assert result.gaps[0].after_sequence == 1
    assert result.gaps[0].before_sequence == 3
    assert answers(result)['a'] == ('known', 2, None)


def test_backwards_sequence_is_rejected():
    with pytest.raises(ValueError):
        compact_property_changes([
            change(1, {'A': 1}, sequence=2),
            change(2, {'A': 2}, sequence=2),
        ])


def test_out_of_order_serials_are_rejected():
    with pytest.raises(ValueError):
        compact_property_changes([change(2, {'A': 2}), change(1, {'A': 1})])


def test_merge_can_be_disabled():
    result = compact_property_changes(
        [change(1, {'A': 1}), change(2, {'A': 2})], [], config(merge=False))

    assert [batch.serials for batch in result.batches] == [[1], [2]]
    assert result.batches[1].split_reason == 'merging-disabled'


def test_changed_observation_counts_invalidations_as_a_reported_change():
    signals = [change(1, {'A': 1}), change(2, invalidated=['A'])]
    result = compact_property_changes(
        signals, [observation('changed', 'A', 2, kind=ObservationKind.CHANGED)])

    assert answers(result)['changed'] == ('known', True, None)


def test_changed_observation_reports_false_when_nothing_is_reported():
    signals = [change(1, {'A': 1}), change(2, {'B': 2})]
    result = compact_property_changes(signals, [
        observation('changed', 'B', 2, kind=ObservationKind.CHANGED, after_serial=1)
    ])

    assert answers(result)['changed'] == ('known', True, None)

    result = compact_property_changes(signals, [
        observation('unchanged', 'A', 2, kind=ObservationKind.CHANGED, after_serial=1)
    ])

    assert answers(result)['unchanged'] == ('known', False, None)


def test_never_reported_property_stays_unknown():
    result = compact_property_changes(
        [change(1, {'A': 1})], [observation('c', 'C', 1)])

    assert answers(result)['c'] == ('unknown', None, 'never-reported')


def test_unknown_property_of_a_gap_is_not_given_the_previous_value():
    result = compact_property_changes(
        [change(1, {'A': 'cached'}, sequence=1),
         change(3, {'A': 'fresh'}, sequence=3)],
        [observation('a', 'A', 2)])

    assert answers(result)['a'] == ('unknown', None, 'stream-gap')


def test_to_dict_is_json_friendly():
    result = compact_property_changes(
        [change(1, {'A': 1})], [observation('a', 'A', 1)])
    payload = result.to_dict()

    assert payload['source_signals'] == 1
    assert payload['batch_count'] == 1
    assert payload['batches'][0]['changed'] == {'A': 1}
    assert payload['observations'][0]['status'] == 'known'
    assert payload['observations'][0]['value'] == 1


def test_compact_accepts_mappings():
    result = compact(
        signals=[
            {
                'serial': 1,
                'interface': INTERFACE,
                'changed': {
                    'A': 'one'
                }
            },
            {
                'serial': 2,
                'interface': INTERFACE,
                'invalidated': ['B']
            },
        ],
        observations=[
            {
                'id': 'a',
                'interface': INTERFACE,
                'property': 'A',
                'at_serial': 2
            },
            {
                'id': 'b',
                'interface': INTERFACE,
                'property': 'B',
                'at_serial': 2
            },
        ])

    assert answers(result)['a'] == ('known', 'one', None)
    assert answers(result)['b'] == ('unknown', None, 'invalidated')


def test_from_message_unwraps_variants():
    message = Message(message_type=MessageType.SIGNAL,
                      path='/test/path',
                      interface='org.freedesktop.DBus.Properties',
                      member='PropertiesChanged',
                      signature='sa{sv}as',
                      body=[INTERFACE, {
                          'A': Variant('s', 'value')
                      }, ['B']],
                      serial=7)

    signal = PropertyChange.from_message(message)

    assert signal.serial == 7
    assert signal.interface == INTERFACE
    assert signal.changed == {'A': 'value'}
    assert signal.invalidated == ['B']
    assert signal.path == '/test/path'


def test_from_message_rejects_other_signals():
    message = Message(message_type=MessageType.SIGNAL,
                      path='/test/path',
                      interface=INTERFACE,
                      member='SomethingElse',
                      signature='s',
                      body=['x'])

    with pytest.raises(ValueError):
        PropertyChange.from_message(message)


class AnnotatedInterface(ServiceInterface):
    def __init__(self):
        super().__init__(INTERFACE)

    @dbus_property(access=PropertyAccess.READ, emits_changed_signal=EmitsChangedSignal.INVALIDATED)
    def invalidated_prop(self) -> 's':
        return 'x'

    @dbus_property(access=PropertyAccess.READ, emits_changed_signal='const')
    def const_prop(self) -> 's':
        return 'y'

    @dbus_property(access=PropertyAccess.READ)
    def default_prop(self) -> 's':
        return 'z'


def test_service_property_annotation_reaches_introspection():
    interface = AnnotatedInterface()
    introspected = interface.introspect()

    by_name = {prop.name: prop for prop in introspected.properties}
    assert by_name['invalidated_prop'].emits_changed_signal == EmitsChangedSignal.INVALIDATED
    assert by_name['const_prop'].emits_changed_signal == EmitsChangedSignal.CONST
    assert by_name['default_prop'].emits_changed_signal == EmitsChangedSignal.TRUE

    xml = introspected.to_xml()
    annotation = xml.find(
        "property[@name='invalidated_prop']/annotation"
        "[@name='org.freedesktop.DBus.Property.EmitsChangedSignal']")
    assert annotation is not None
    assert annotation.get('value') == 'invalidated'

    xml_roundtrip = intr.Interface.from_xml(xml)
    roundtrip = {prop.name: prop for prop in xml_roundtrip.properties}
    assert roundtrip['invalidated_prop'].emits_changed_signal == \
        EmitsChangedSignal.INVALIDATED


def test_introspection_rejects_an_invalid_annotation():
    element = intr.ET.fromstring(
        '<property name="A" type="s" access="read">'
        '<annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" '
        'value="sometimes"/></property>')

    with pytest.raises(Exception):
        intr.Property.from_xml(element)


def test_annotation_map_feeds_the_config():
    interface = AnnotatedInterface().introspect()
    result = compact_property_changes(
        [change(1, {'invalidated_prop': 'new'})],
        [observation('prop', 'invalidated_prop', 1)],
        CompactionConfig(annotations={
            (interface.name, prop.name): prop.emits_changed_signal
            for prop in interface.properties
        }))

    assert answers(result)['prop'] == ('unknown', None, 'invalidated')


def test_from_introspection_builds_the_config():
    interface = AnnotatedInterface().introspect()
    result = compact_property_changes([change(1, {'invalidated_prop': 'new'})],
                                      [observation('prop', 'invalidated_prop', 1)],
                                      CompactionConfig.from_introspection(interface))

    assert answers(result)['prop'] == ('unknown', None, 'invalidated')


def test_annotations_accept_interface_colon_property_keys():
    result = compact_property_changes(
        [change(1, {'A': 'value'})], [observation('a', 'A', 1)],
        config(annotations={f'{INTERFACE}:A': 'invalidated'}))

    assert answers(result)['a'] == ('unknown', None, 'invalidated')


def test_annotations_accept_nested_interface_mappings():
    result = compact_property_changes(
        [change(1, {'A': 'value'})], [observation('a', 'A', 1)],
        config(annotations={INTERFACE: {'A': 'invalidated'}}))

    assert answers(result)['a'] == ('unknown', None, 'invalidated')


def test_cli_reads_and_writes_json(tmp_path):
    from dbus_next.property_compaction import _main

    payload = {
        'signals': [
            {'serial': 1, 'interface': INTERFACE, 'changed': {'A': 1}},
            {'serial': 2, 'interface': INTERFACE, 'changed': {'A': 2}},
            {'serial': 3, 'interface': INTERFACE, 'invalidated': ['B']},
        ],
        'observations': [
            {'id': 'a', 'interface': INTERFACE, 'property': 'A', 'at_serial': 3},
            {'id': 'b', 'interface': INTERFACE, 'property': 'B', 'at_serial': 3},
        ],
        'annotations': {INTERFACE: {'B': 'true'}},
    }
    input_path = tmp_path / 'input.json'
    input_path.write_text(json.dumps(payload), encoding='utf-8')

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert _main([str(input_path)]) == 0

    result = json.loads(buffer.getvalue())
    assert result['source_signals'] == 3
    assert result['batches'][0]['changed'] == {'A': 2}
    states = {state['id']: state for state in result['observations']}
    assert states['a']['status'] == 'known'
    assert states['a']['value'] == 2
    assert states['b']['status'] == 'unknown'
    assert states['b']['reason'] == 'invalidated'

    assert json.loads(json.dumps(result)) == result


def test_mixed_sequence_usage_in_one_stream_is_rejected():
    with pytest.raises(ValueError):
        compact_property_changes([
            change(1, {'A': 1}, sequence=1),
            change(2, {'A': 2}),
        ])
