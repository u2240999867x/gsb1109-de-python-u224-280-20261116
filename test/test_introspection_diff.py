from dbus_next import introspection as intr
from dbus_next.introspection_diff import (ColumnAction, MemberKind, SignatureStatus,
                                          compare_introspections, main)

import os
import xml.etree.ElementTree as ET


FW_OLD = '''<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
    "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node name="/com/example/device0">
  <interface name="com.example.Device">
    <property name="Level" type="y" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/>
    </property>
    <property name="Mode" type="s" access="readwrite"/>
    <property name="Serial" type="s" access="read"/>
    <property name="Gone" type="u" access="read"/>
    <method name="Mode">
      <arg name="value" type="s" direction="in"/>
    </method>
    <signal name="Changed">
      <arg name="level" type="y" direction="out"/>
    </signal>
  </interface>
  <node name="child">
    <interface name="com.example.Child">
      <property name="Level" type="y" access="read"/>
    </interface>
  </node>
</node>
'''


FW_NEW = '''<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
    "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node name="/com/example/device0">
  <interface name="com.example.Device">
    <property name="Level" type="y" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
    <property name="Mode" type="u" access="readwrite"/>
    <property name="Serial" type="s" access="write"/>
    <method name="Mode">
      <arg name="value" type="s" direction="in"/>
    </method>
    <signal name="Changed">
      <arg name="level" type="y" direction="out"/>
    </signal>
  </interface>
  <node name="child">
    <interface name="com.example.Child">
      <property name="Level" type="y" access="read"/>
    </interface>
  </node>
</node>
'''


def _column(diff, name, kind=MemberKind.PROPERTY, path='/com/example/device0',
            interface='com.example.Device'):
    return diff.column(path, interface, kind, name)


def test_compares_signature_changes_by_member_kind():
    diff = compare_introspections(FW_OLD, FW_NEW, old_source='fw-old', new_source='fw-new')

    level = _column(diff, 'Level')
    assert level.action is ColumnAction.MIGRATE
    assert level.old.signature == 'y'
    assert level.new.signature == 'y'
    assert 'change notification' in level.reason

    mode_property = _column(diff, 'Mode')
    assert mode_property.action is ColumnAction.MIGRATE
    assert mode_property.old.signature == 's'
    assert mode_property.new.signature == 'u'

    mode_method = _column(diff, 'Mode', kind=MemberKind.METHOD)
    assert mode_method.action is ColumnAction.REUSE
    assert mode_method.old.kind is MemberKind.METHOD


def test_unreadable_property_stops_collection():
    diff = compare_introspections(FW_OLD, FW_NEW)
    serial = _column(diff, 'Serial')
    assert serial.action is ColumnAction.STOP
    assert serial.old.readable is True
    assert serial.new.readable is False
    assert 'no longer readable' in serial.reason


def test_writable_property_becoming_read_only_requires_migration():
    old = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s" access="readwrite"/></interface></node>')
    new = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s" access="read"/></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.MIGRATE
    assert a.new.readable is True
    assert a.new.writable is False


def test_removed_member_stops_collection():
    diff = compare_introspections(FW_OLD, FW_NEW)
    gone = _column(diff, 'Gone')
    assert gone.action is ColumnAction.STOP
    assert gone.removed
    assert gone.new is None


def test_new_member_is_reusable():
    old = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/></interface></node>')
    new = ('<node><interface name="com.example.Device"><property name="A" type="s"/>'
           '<property name="B" type="s"/></interface></node>')
    diff = compare_introspections(old, new)
    added = _column(diff, 'B', path='/', interface='com.example.Device')
    assert added.action is ColumnAction.REUSE
    assert added.is_new


def test_same_name_on_different_paths_is_a_different_column():
    diff = compare_introspections(FW_OLD, FW_NEW)
    root_level = _column(diff, 'Level')
    child_level = _column(diff,
                          'Level',
                          path='/com/example/device0/child',
                          interface='com.example.Child')
    assert root_level.identity != child_level.identity
    assert child_level.action is ColumnAction.REUSE


def test_annotation_policy_records_source_and_line():
    diff = compare_introspections(FW_OLD, FW_NEW, old_source='fw-old', new_source='fw-new')
    level = _column(diff, 'Level')
    assert level.action is ColumnAction.MIGRATE
    evidence = level.policy_evidence[0]
    assert evidence.annotation.name == 'org.freedesktop.DBus.Property.EmitsChangedSignal'
    assert evidence.annotation.value == 'false'
    assert evidence.annotation.source == 'fw-new'
    assert evidence.annotation.line == 6
    assert evidence.old_value == 'true'


def test_unknown_type_is_not_downgraded_to_string():
    old = ('<node><interface name="com.example.Device">'
           '<property name="Mystery" type="zz" access="read"/>'
           '</interface></node>')
    new = old
    diff = compare_introspections(old, new)
    mystery = _column(diff, 'Mystery', path='/', interface='com.example.Device')
    assert mystery.action is ColumnAction.MIGRATE
    assert mystery.unknown_type
    assert mystery.old.signature == 'zz'
    assert mystery.new.signature == 'zz'
    assert mystery.new.type.status is SignatureStatus.UNKNOWN
    assert mystery.new.type.error
    assert mystery.new.signature != 's'


def test_unknown_argument_type_is_retained_verbatim():
    old = ('<node><interface name="com.example.Device">'
           '<method name="Read"><arg name="token" type="qq" direction="in"/></method>'
           '</interface></node>')
    diff = compare_introspections(old, old)
    read = _column(diff, 'Read', kind=MemberKind.METHOD, path='/',
                   interface='com.example.Device')
    assert read.action is ColumnAction.MIGRATE
    assert read.new.args[0].signature == 'qq'
    assert read.new.args[0].type.status is SignatureStatus.UNKNOWN


def test_deprecated_member_stops_collection():
    old = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/></interface></node>')
    new = ('<node><interface name="com.example.Device"><property name="A" type="s">'
           '<annotation name="org.freedesktop.DBus.Deprecated" value="true"/>'
           '</property></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.STOP
    assert a.policy_evidence[0].annotation.name == 'org.freedesktop.DBus.Deprecated'


def test_removing_deprecation_requires_migration():
    old = ('<node><interface name="com.example.Device"><property name="A" type="s">'
           '<annotation name="org.freedesktop.DBus.Deprecated" value="true"/>'
           '</property></interface></node>')
    new = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.MIGRATE
    assert a.policy_evidence[0].effect == 'undeprecated'


def test_interface_annotation_applies_when_member_has_none():
    old = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/></interface></node>')
    new = ('<node><interface name="com.example.Device">'
           '<annotation name="org.freedesktop.DBus.Deprecated" value="true"/>'
           '<property name="A" type="s"/></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.STOP
    evidence = a.policy_evidence[0]
    assert evidence.scope == 'interface'
    assert evidence.annotation.source == 'new'


def test_unknown_annotation_value_requires_review_with_old_value():
    old = ('<node><interface name="com.example.Device"><property name="A" type="y">'
           '<annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/>'
           '</property></interface></node>')
    new = ('<node><interface name="com.example.Device"><property name="A" type="y">'
           '<annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" '
           'value="sometimes"/></property></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.MIGRATE
    evidence = a.policy_evidence[0]
    assert evidence.effect == 'unknown-annotation-value'
    assert evidence.annotation.value == 'sometimes'
    assert evidence.annotation.source == 'new'
    assert evidence.old_value == 'true'


def test_no_reply_with_output_stops_collection():
    old = ('<node><interface name="com.example.Device">'
           '<method name="Read"><arg name="value" type="s" direction="out"/></method>'
           '</interface></node>')
    new = ('<node><interface name="com.example.Device"><method name="Read">'
           '<annotation name="org.freedesktop.DBus.Method.NoReply" value="true"/>'
           '<arg name="value" type="s" direction="out"/></method></interface></node>')
    diff = compare_introspections(old, new)
    read = _column(diff, 'Read', kind=MemberKind.METHOD, path='/',
                   interface='com.example.Device')
    assert read.action is ColumnAction.STOP
    assert read.policy_evidence[0].effect == 'no-reply-with-output'


def test_stop_policy_wins_over_signature_change():
    old = ('<node><interface name="com.example.Device">'
           '<method name="Read"><arg type="s" direction="out"/></method>'
           '</interface></node>')
    new = ('<node><interface name="com.example.Device"><method name="Read">'
           '<annotation name="org.freedesktop.DBus.Deprecated" value="true"/>'
           '<arg type="y" direction="out"/></method></interface></node>')
    diff = compare_introspections(old, new)
    read = _column(diff, 'Read', kind=MemberKind.METHOD, path='/',
                   interface='com.example.Device')
    assert read.action is ColumnAction.STOP
    assert 'deprecated' in read.reason


def test_method_timeout_change_requires_migration():
    old = ('<node><interface name="com.example.Device"><method name="Read">'
           '<annotation name="org.freedesktop.DBus.Method.Timeout" value="1000"/>'
           '</method></interface></node>')
    new = ('<node><interface name="com.example.Device"><method name="Read">'
           '<annotation name="org.freedesktop.DBus.Method.Timeout" value="5000"/>'
           '</method></interface></node>')
    diff = compare_introspections(old, new)
    read = _column(diff, 'Read', kind=MemberKind.METHOD, path='/',
                   interface='com.example.Device')
    assert read.action is ColumnAction.MIGRATE
    assert read.policy_evidence[0].effect == 'timeout-changed'


def test_annotations_round_trip_on_introspection_model():
    node = intr.Node.parse(FW_OLD, source='fw-old.xml')
    interface = node.interfaces[0]
    level = interface.properties[0]
    assert level.annotations[0].name == 'org.freedesktop.DBus.Property.EmitsChangedSignal'
    assert level.annotations[0].value == 'true'
    assert level.source == 'fw-old.xml'
    assert level.line == 5

    reparsed = intr.Node.parse(node.tostring())
    level = reparsed.interfaces[0].properties[0]
    assert level.annotations[0].value == 'true'


def test_accepts_parsed_nodes_and_elements():
    old_node = intr.Node.parse(FW_OLD, source='fw-old.xml')
    new_node = intr.Node.parse(FW_NEW, source='fw-new.xml')
    from_nodes = compare_introspections(old_node, new_node)

    old_element = ET.fromstring(FW_OLD)
    new_element = ET.fromstring(FW_NEW)
    from_elements = compare_introspections(old_element, new_element)

    assert from_nodes.to_dict()['summary'] == from_elements.to_dict()['summary']
    assert from_nodes.old_source == 'fw-old.xml'


def test_duplicate_member_declarations_are_reported():
    old = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/></interface></node>')
    new = ('<node><interface name="com.example.Device">'
           '<property name="A" type="s"/>'
           '<property name="A" type="y"/></interface></node>')
    diff = compare_introspections(old, new)
    a = _column(diff, 'A', path='/', interface='com.example.Device')
    assert a.action is ColumnAction.REUSE
    assert len(a.duplicates) == 1
    assert a.duplicates[0]['document'] == 'new'


def test_output_formats_include_all_three_lists():
    diff = compare_introspections(FW_OLD, FW_NEW, old_source='fw-old', new_source='fw-new')
    rendered = diff.to_dict()
    assert set(rendered) >= {'reusable', 'migrate', 'stop', 'summary', 'columns'}
    assert rendered['summary']['reuse'] == len(diff.reusable)
    assert rendered['summary']['migrate'] == len(diff.migrate)
    assert rendered['summary']['stop'] == len(diff.stop)
    assert '可沿用' in diff.to_markdown()
    assert '须迁移' in diff.to_markdown()
    assert '停止采集' in diff.to_markdown()
    assert '## 可沿用' in diff.to_markdown()
    assert '## 须迁移' in diff.to_markdown()
    assert '## 停止采集' in diff.to_markdown()
    csv_output = diff.to_csv()
    assert 'action,action_label,path,interface,kind,name' in csv_output


def test_text_input_provenance_is_preserved_in_plan():
    diff = compare_introspections(FW_OLD, FW_NEW)
    level = _column(diff, 'Level')
    change = next(c for c in level.annotation_changes
                  if c.name == 'org.freedesktop.DBus.Property.EmitsChangedSignal')
    assert change.old.value == 'true'
    assert change.old.source == 'old'
    assert change.new.value == 'false'
    assert change.new.source == 'new'


def test_cli_writes_a_plan(tmp_path):
    old_path = tmp_path / 'old.xml'
    new_path = tmp_path / 'new.xml'
    output_path = tmp_path / 'plan.json'
    old_path.write_text(FW_OLD)
    new_path.write_text(FW_NEW)

    assert main([str(old_path), str(new_path), '--output', str(output_path)]) == 0
    import json
    plan = json.loads(output_path.read_text())
    assert plan['summary']['stop'] >= 1
    assert plan['new_source'] == str(new_path)


def test_example_fixture_annotation_is_preserved():
    path = os.path.join(os.path.dirname(__file__), 'data', 'introspection.xml')
    with open(path) as fixture:
        node = intr.Node.parse(fixture.read(), source=path)

    frobate = node.interfaces[0].methods[0]
    assert frobate.annotations[0].name == 'org.freedesktop.DBus.Deprecated'
    assert frobate.annotations[0].value == 'true'
    assert frobate.annotations[0].source == path
