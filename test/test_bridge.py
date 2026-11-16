"""Tests for bridging recorded messages onto an isolated bus."""
from dbus_next import Message, MessageType, Variant
from dbus_next.bridge import (MessageBridge, MissingMappingError, MissingUnixFdError,
                              bridge_message)
from dbus_next.signature import SignatureTree

import pytest


OLD_CALLER = ':1.5'
OLD_SERVICE = ':1.6'
NEW_CALLER = ':9.1'
NEW_SERVICE = ':9.2'


def make_bridge(**kwargs):
    return MessageBridge(
        connection_map={
            OLD_CALLER: NEW_CALLER,
            OLD_SERVICE: NEW_SERVICE,
        },
        path_map={
            '/old/root': '/new/root',
            '/old/alpha': '/new/alpha',
            '/old/beta': '/new/beta',
            '/old/gamma': '/new/gamma',
        },
        **kwargs)


def test_bridge_message_remaps_nested_paths():
    bridge = make_bridge()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='oaoa{so}a{oa{so}}(ov)v',
                  body=[
                      '/old/root',
                      ['/old/alpha', '/old/beta'],
                      {
                          '/old/alpha': '/old/root'
                      },
                      {
                          '/old/beta': {
                              '/old/gamma': '/old/alpha'
                          }
                      },
                      ['/old/gamma', Variant('o', '/old/beta')],
                      Variant('a{sa{so}}', {
                          'key': {
                              '/old/alpha': '/old/gamma'
                          }
                      }),
                  ],
                  serial=4)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.path == '/new/root'
    assert bridged.destination == NEW_SERVICE
    assert bridged.body == [
        '/new/root',
        ['/new/alpha', '/new/beta'],
        {
            '/old/alpha': '/new/root'
        },
        {
            '/new/beta': {
                '/old/gamma': '/new/alpha'
            }
        },
        ['/new/gamma', Variant('o', '/new/beta')],
        Variant('a{sa{so}}', {
            'key': {
                '/old/alpha': '/new/gamma'
            }
        }),
    ]


def test_bridge_message_preserves_plain_strings_and_signatures():
    bridge = make_bridge()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='sa{ss}gas',
                  body=[
                      '/old/root', {
                          '/old/root': '/old/alpha',
                          '/old/beta': '/old/gamma'
                      }, 'a{so}', ['/old/beta']
                  ],
                  serial=5)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.body == [
        '/old/root', {
            '/old/root': '/old/alpha',
            '/old/beta': '/old/gamma'
        }, 'a{so}', ['/old/beta']
    ]


def test_bridge_message_preserves_string_values_that_look_like_paths():
    bridge = make_bridge()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='sa{ss}(ss)v',
                  body=[
                      '/old/root', {
                          '/old/root': '/old/alpha'
                      }, ['/old/beta', '/old/gamma'],
                      Variant('s', '/old/root')
                  ],
                  serial=6)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.body == [
        '/old/root', {
            '/old/root': '/old/alpha'
        }, ['/old/beta', '/old/gamma'],
        Variant('s', '/old/root')
    ]


def test_bridge_message_maps_object_paths_in_dictionary_keys():
    bridge = make_bridge()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='a{oa{so}}',
                  body=[{
                      '/old/alpha': {
                          'key': '/old/beta'
                      }
                  }],
                  serial=7)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.body == [{
        '/new/alpha': {
            'key': '/new/beta'
        }
    }]


def test_bridge_message_drops_missing_mapping():
    bridge = make_bridge()

    missing_path = Message(destination=OLD_SERVICE,
                           path='/old/missing',
                           interface='test.interface',
                           member='Test',
                           serial=1)
    assert bridge.bridge_message(missing_path) is None

    missing_connection = Message(destination=':1.99',
                                 path='/old/root',
                                 interface='test.interface',
                                 member='Test',
                                 serial=2)
    assert bridge.bridge_message(missing_connection) is None

    missing_body_path = Message(destination=OLD_SERVICE,
                                path='/old/root',
                                interface='test.interface',
                                member='Test',
                                signature='ao',
                                body=[['/old/alpha', '/old/missing']],
                                serial=3)
    assert bridge.bridge_message(missing_body_path) is None


def test_bridge_message_remaps_serials_and_reply_association():
    bridge = make_bridge(next_serial=iter([100, 101]))
    call = Message(destination=OLD_SERVICE,
                   path='/old/root',
                   interface='test.interface',
                   member='Test',
                   signature='o',
                   body=['/old/alpha'],
                   sender=OLD_CALLER,
                   serial=17)

    bridged = bridge.bridge_message(call)
    assert bridged is not None
    assert bridged.serial == 100
    assert bridge.pending_replies == {100: call}

    reply = Message(message_type=MessageType.METHOD_RETURN,
                    reply_serial=100,
                    sender=NEW_SERVICE,
                    destination=NEW_CALLER,
                    signature='o',
                    body=['/new/beta'])

    restored = bridge.restore_reply(reply)
    assert restored is not None
    assert restored.reply_serial == 17
    assert restored.destination == OLD_CALLER
    assert restored.body == ['/old/beta']


def test_bridge_message_remaps_unix_fds():
    bridge = make_bridge(fd_map={10: 110, 11: 111})
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='ahv(ha{sh})',
                  body=[[0, 1], Variant('h', 1), [1, {
                      'a': 0,
                      'b': 1
                  }]],
                  unix_fds=[10, 11],
                  serial=8)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.unix_fds == [110, 111]
    assert bridged.body == [[0, 1], Variant('h', 1), [1, {
        'a': 0,
        'b': 1
    }]]


def test_bridge_message_drops_missing_fd():
    bridge = make_bridge(fd_map={10: 110})
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='ah',
                  body=[[0, 1]],
                  unix_fds=[10, 11],
                  serial=9)

    assert bridge.bridge_message(msg) is None

    no_handle = Message(destination=OLD_SERVICE,
                        path='/old/root',
                        interface='test.interface',
                        member='Test',
                        signature='h',
                        body=[0],
                        unix_fds=[],
                        serial=10)
    assert bridge.bridge_message(no_handle) is None


def test_bridge_message_drops_invalid_body():
    bridge = make_bridge()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='o',
                  body=['not-an-object-path'],
                  serial=11)

    assert bridge.bridge_message(msg) is None


def test_bridge_message_is_sendable():
    bridge = make_bridge()

    class Connection:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

        def next_serial(self):
            return 1

    target = Connection()
    msg = Message(destination=OLD_SERVICE,
                  path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='o',
                  body=['/old/alpha'],
                  serial=1)

    bridged = bridge.bridge_message(msg)
    assert bridged is not None

    msg.serial = 21
    msg.unix_fds = []
    target_bridge = MessageBridge(connection_map={OLD_SERVICE: target},
                                  path_map={'/old/root': '/new/root',
                                            '/old/alpha': '/new/alpha'})
    target_bridge.send(msg, target)

    assert len(target.sent) == 1
    assert target.sent[0].path == '/new/root'


def test_bridge_message_accepts_recorded_dict_and_route_table():
    bridge = MessageBridge({
        (OLD_SERVICE, '/old/root'): (NEW_SERVICE, '/new/root'),
    })
    record = {
        'destination': OLD_SERVICE,
        'path': '/old/root',
        'interface': 'test.interface',
        'member': 'Test',
        'signature': 'o',
        'body': ['/old/root'],
        'serial': 3,
    }

    bridged = bridge.bridge_message(record)

    assert bridged is not None
    assert bridged.destination == NEW_SERVICE
    assert bridged.path == '/new/root'
    assert bridged.body == ['/new/root']


def test_bridge_message_can_raise_on_missing():
    bridge = MessageBridge(path_map={'/old/root': '/new/root'}, raise_on_missing=True)
    msg = Message(path='/old/missing', interface='test.interface', member='Test', serial=1)

    with pytest.raises(MissingMappingError):
        bridge.bridge_message(msg)


def test_bridge_message_remaps_reply_by_original_serial_mapping():
    bridge = MessageBridge(serial_map={7: 70}, reply_serial_map={70: 7})
    reply = Message(message_type=MessageType.METHOD_RETURN,
                    reply_serial=70,
                    signature='s',
                    body=['ok'])

    restored = bridge.restore_reply(reply)
    assert restored is not None
    assert restored.reply_serial == 7


def test_body_remapping_is_signature_aware_for_arrays_of_variants():
    bridge = make_bridge()
    msg = Message(path='/old/root',
                  interface='test.interface',
                  member='Test',
                  signature='av',
                  body=[[Variant('o', '/old/alpha'), Variant('s', '/old/beta'),
                         Variant('as', ['/old/gamma'])]],
                  serial=12)

    bridged = bridge.bridge_message(msg)

    assert bridged is not None
    assert bridged.body == [[
        Variant('o', '/new/alpha'),
        Variant('s', '/old/beta'),
        Variant('as', ['/old/gamma'])
    ]]
