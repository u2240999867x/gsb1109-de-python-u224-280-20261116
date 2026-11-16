from dbus_next import Message, MessageType, Variant
from dbus_next.errors import InvalidBusNameError, InvalidObjectPathError
from dbus_next.provenance import (MessageArchive, ScopedBusName, ScopedConnectionId,
                                  ScopedMessage, ScopedObjectPath, ScopedSerial, SourceDomain,
                                  tag_message, untag_message)

import pytest


def make_message(body=None, serial=42):
    return Message(destination='org.example.Service',
                   path='/org/example/Obj',
                   interface='org.example.Iface',
                   member='DoThing',
                   message_type=MessageType.METHOD_CALL,
                   sender=':1.42',
                   signature='sua{sv}',
                   body=body if body is not None else
                   ['org.example.Service', 7, {'key': Variant('s', '/org/example/Obj')}],
                   serial=serial)


def assert_same_message(restored, original):
    assert restored.signature == original.signature
    assert restored.signature_tree == original.signature_tree
    assert restored.body == original.body
    assert restored.destination == original.destination
    assert restored.path == original.path
    assert restored.interface == original.interface
    assert restored.member == original.member
    assert restored.sender == original.sender
    assert restored.serial == original.serial
    assert restored.message_type == original.message_type
    assert restored.flags == original.flags


def test_source_domain_validation():
    assert SourceDomain('host_a').name == 'host_a'
    assert SourceDomain(SourceDomain('host_a')) == SourceDomain('host_a')
    assert str(SourceDomain('host_a')) == 'host_a'
    assert SourceDomain('host_a') != SourceDomain('host_b')
    for invalid in ('', 'has.dot', 'has/slash', 'has:colon', '1leadingdigit', None):
        with pytest.raises(ValueError):
            SourceDomain(invalid)


def test_scoped_bus_name_roundtrip():
    name = ScopedBusName('hostA', 'org.example.Service')
    assert name.original == 'org.example.Service'
    assert name.domain == SourceDomain('hostA')
    assert name.encode() == 'hostA.org.example.Service'
    assert ScopedBusName.decode('hostA.org.example.Service', 'hostA') == name
    assert ScopedBusName.decode('hostB.org.example.Service', 'hostA') is None
    assert ScopedBusName.decode('org.example.Service', 'hostA') is None

    unique = ScopedBusName('hostA', ':1.42')
    assert unique.encode() == ':hostA.1.42'
    assert ScopedBusName.decode(':hostA.1.42', 'hostA') == unique

    # a name that already starts with the domain still round-trips
    nested = ScopedBusName('hostA', 'hostA.org.example.Service')
    assert ScopedBusName.decode(nested.encode(), 'hostA') == nested

    with pytest.raises(InvalidBusNameError):
        ScopedBusName('hostA', 'not a bus name')


def test_scoped_connection_id():
    conn = ScopedConnectionId('hostA', ':1.42')
    assert conn.original == ':1.42'
    assert conn.encode() == ':hostA.1.42'
    assert ScopedConnectionId.decode(':hostA.1.42', 'hostA') == conn
    assert ScopedConnectionId.decode('hostA.org.example.Service', 'hostA') is None
    with pytest.raises(InvalidBusNameError):
        ScopedConnectionId('hostA', 'org.example.Service')


def test_scoped_object_path_roundtrip():
    path = ScopedObjectPath('hostA', '/org/example/Obj')
    assert path.original == '/org/example/Obj'
    assert path.encode() == '/hostA/org/example/Obj'
    assert ScopedObjectPath.decode('/hostA/org/example/Obj', 'hostA') == path
    assert ScopedObjectPath.decode('/hostB/org/example/Obj', 'hostA') is None

    root = ScopedObjectPath('hostA', '/')
    assert root.encode() == '/hostA'
    assert ScopedObjectPath.decode('/hostA', 'hostA') == root

    with pytest.raises(InvalidObjectPathError):
        ScopedObjectPath('hostA', 'not/a/path')


def test_cross_bus_references_do_not_auto_connect():
    # same original name on different buses must not compare equal
    assert ScopedBusName('hostA', 'org.example.Service') \
        != ScopedBusName('hostB', 'org.example.Service')
    assert ScopedConnectionId('hostA', ':1.42') != ScopedConnectionId('hostB', ':1.42')
    assert ScopedObjectPath('hostA', '/org/example') != ScopedObjectPath('hostB', '/org/example')
    assert ScopedSerial('hostA', 7) != ScopedSerial('hostB', 7)
    assert not ScopedBusName('hostA', 'org.example.Service').matches(
        ScopedBusName('hostB', 'org.example.Service'))

    # different identifier kinds never connect either
    assert ScopedBusName('hostA', 'org.example.Service') \
        != ScopedObjectPath('hostA', '/org/example/Service')
    assert ScopedSerial('hostA', 7) != 7

    # same domain and same name does connect
    assert ScopedBusName('hostA', 'org.example.Service') \
        == ScopedBusName('hostA', 'org.example.Service')


def test_tag_untag_message_roundtrip():
    msg = make_message()
    tagged = tag_message('hostA', msg)

    assert tagged.destination == 'hostA.org.example.Service'
    assert tagged.sender == ':hostA.1.42'
    assert tagged.path == '/hostA/org/example/Obj'
    # interface and member are shared vocabulary and stay as-is
    assert tagged.interface == 'org.example.Iface'
    assert tagged.member == 'DoThing'
    # body values are never rewritten, even look-alike strings
    assert tagged.body == msg.body

    restored = untag_message('hostA', tagged)
    assert_same_message(restored, msg)

    # untagging with a foreign domain leaves every field untouched
    foreign = untag_message('hostB', tagged)
    assert foreign.destination == tagged.destination
    assert foreign.sender == tagged.sender
    assert foreign.path == tagged.path
    assert foreign.body == tagged.body


def test_tag_message_does_not_touch_lookalike_body_strings():
    # body strings that look exactly like tagged names must pass through
    # the archive unchanged
    msg = make_message(body=['hostA.org.example.Service', 1, {'k': Variant('s', '/hostA')}])
    archive = MessageArchive()
    archive.add('hostA', msg)
    restored = MessageArchive.deserialize(archive.serialize()).split('hostA')[0]
    assert restored.body == msg.body


def test_scoped_message_views():
    msg = make_message()
    scoped = ScopedMessage('hostA', msg)

    assert scoped.serial == ScopedSerial('hostA', 42)
    assert scoped.reply_serial is None
    assert scoped.path == ScopedObjectPath('hostA', '/org/example/Obj')
    assert scoped.sender == ScopedConnectionId('hostA', ':1.42')
    assert scoped.destination == ScopedBusName('hostA', 'org.example.Service')

    tagged = scoped.scoped_message()
    assert tagged.path == '/hostA/org/example/Obj'
    assert tagged.body == msg.body

    record, offset = ScopedMessage.unarchive(scoped.archive())
    assert offset == len(scoped.archive())
    assert record == scoped
    assert_same_message(record.message, msg)


def test_archive_merge_and_split_roundtrip():
    msg_a = make_message()
    msg_b = make_message(body=['different', 8, {'k': Variant('u', 3)}], serial=42)

    archive = MessageArchive()
    archive.add('hostA', msg_a)
    archive.add('hostB', msg_b)
    assert len(archive) == 2

    restored = MessageArchive.deserialize(archive.serialize())
    assert len(restored) == 2

    split_a = restored.split('hostA')
    split_b = restored.split('hostB')
    assert len(split_a) == 1
    assert len(split_b) == 1

    # parameter types and values are identical after archiving and
    # splitting back to a single machine
    assert_same_message(split_a[0], msg_a)
    assert_same_message(split_b[0], msg_b)

    # the two hosts kept identical names and serials without colliding
    assert restored.records[0].serial != restored.records[1].serial
    assert restored.records[0].path != restored.records[1].path


def test_archive_lookup_is_domain_scoped():
    archive = MessageArchive()
    archive.add('hostA', make_message())
    archive.add('hostB', make_message())

    hits_a = archive.lookup(ScopedObjectPath('hostA', '/org/example/Obj'))
    assert [r.domain for r in hits_a] == [SourceDomain('hostA')]

    hits_b = archive.lookup(ScopedBusName('hostB', 'org.example.Service'))
    assert [r.domain for r in hits_b] == [SourceDomain('hostB')]

    # same serial on both hosts: each lookup only connects within its domain
    assert archive.lookup(ScopedSerial('hostA', 42)) == [archive.records[0]]
    assert archive.lookup(ScopedSerial('hostB', 42)) == [archive.records[1]]

    conn_hits = archive.lookup(ScopedConnectionId('hostA', ':1.42'))
    assert [r.domain for r in conn_hits] == [SourceDomain('hostA')]

    with pytest.raises(TypeError):
        archive.lookup('org.example.Service')


def test_unarchive_rejects_truncated_records():
    archive = MessageArchive()
    archive.add('hostA', make_message())
    data = archive.serialize()
    with pytest.raises(ValueError):
        ScopedMessage.unarchive(data[:len(data) - 1])
    with pytest.raises(ValueError):
        ScopedMessage.unarchive(b'\x00')
