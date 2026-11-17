"""Tests for the logix_driver.py file.

The Logix Driver is beholden to the CIPDriver interface. Only tests
which bind it to that interface should be allowed here. Tests binding
to another interface such as Socket are an anti-pattern.

There are quite a few methods in the LogixDriver which are difficult to
read or test due to both code clarity issues and it being inconvenient.

Also the vast majority of methods are private, I think that private 
methods should not be tested directly, but rather, their effects on
public methods should be tested.

pytest --cov=pycomm3 --cov-branch tests/offline/
----------- coverage: platform linux, python 3.8.1-final-0 -----------
Name                           Stmts   Miss Branch BrPart  Cover
----------------------------------------------------------------
pycomm3/logix_driver.py                   798    718    346      0     7%

We're currently at 7% test coverage, I would like to increase that to >=50%
and then continue to do so for the rest of the modules.
"""
from unittest import mock

import pytest

from pycomm3.cip import Array, DINT, UINT
from pycomm3.cip_driver import CIPDriver
from pycomm3.const import MICRO800_PREFIX, SUCCESS, INSUFFICIENT_PACKETS
from pycomm3.exceptions import CommError, PycommError, RequestError
from pycomm3.logix_driver import LogixDriver, encode_value
from pycomm3.packets import (
    RequestPacket,
    ResponsePacket,
    ReadTagFragmentedRequestPacket,
    ReadTagFragmentedResponsePacket,
)
from pycomm3.socket_ import Socket
from pycomm3.tag import Tag
from pycomm3.custom_types import ModuleIdentityObject
from pycomm3.util import cycle

CONNECT_PATH = '192.168.1.100/1'

IDENTITY_CLX_V20 = {'vendor': 'Rockwell Automation/Allen-Bradley',
                'product_type': 'Programmable Logic Controller', 'product_code': 0,
                'revision': {'major': 20, 'minor': 0},
                'status': b'00', 'serial': '00000000',
                'product_name': '1756-L55'}

IDENTITY_CLX_V21 = {'vendor': 'Rockwell Automation/Allen-Bradley',
                'product_type': 'Programmable Logic Controller', 'product_code': 0,
                'revision': {'major': 21, 'minor': 0},
                'status': b'00', 'serial': '00000000',
                'product_name': '1756-L62'}

IDENTITY_CLX_V32 = {'vendor': 'Rockwell Automation/Allen-Bradley',
                'product_type': 'Programmable Logic Controller', 'product_code': 0,
                'revision': {'major': 32, 'minor': 0},
                'status': b'00', 'serial': '00000000',
                'product_name': '1756-L85'}

IDENTITY_M8000 = {'encap_protocol_version': 1,
  'ip_address': '192.168.1.124',
  'product_code': 259,
  'product_name': '2080-LC50-48QWBS',
  'product_type': 'Programmable Logic Controller',
  'revision': {'major': 12, 'minor': 11},
  'serial': '12345678',
  'state': 2,
  'status': b'4\x00',
  'vendor': 'Rockwell Automation/Allen-Bradley'}


def test_open_call_init_driver_open():
    """
    This test is to make sure that the initialize driver method is called during
    the `open()` method of the driver.
    """

    with mock.patch.object(CIPDriver, 'open') as mock_open, \
            mock.patch.object(LogixDriver, '_initialize_driver') as mock_init:
        driver = LogixDriver(CONNECT_PATH)
        driver.open()
        assert mock_open.called
        assert mock_init.called


def test_open_call_init_driver_with():
    """
    This test is to make sure that the initialize driver method is called during
    the `open()` method of the driver.
    """

    with mock.patch.object(CIPDriver, 'open') as mock_open, \
         mock.patch.object(LogixDriver, '_initialize_driver') as mock_init:

        with LogixDriver(CONNECT_PATH):
            ...
        assert mock_open.called
        assert mock_init.called


@pytest.mark.parametrize('identity', [IDENTITY_CLX_V20, IDENTITY_CLX_V21, IDENTITY_CLX_V32])
def test_logix_init_for_version_support_instance_ids_large_connection(identity):
    with mock.patch.object(LogixDriver, '_list_identity') as mock_identity, \
         mock.patch.object(LogixDriver, 'get_plc_info') as mock_get_info, \
         mock.patch.object(LogixDriver, 'get_plc_name') as mock_get_name:

        mock_identity.return_value = identity
        mock_get_info.return_value = identity  # this is the ListIdentity response
                                               # not the same as module idenity, but
                                               # has all the fields needed for the test

        plc = LogixDriver(CONNECT_PATH)
        plc._initialize_driver(False, False)

        assert plc._micro800 is False
        assert plc._cfg['use_instance_ids'] == (identity['revision']['major'] >= 21)
        assert mock_get_info.called
        assert mock_get_name.called


@pytest.mark.parametrize('identity', [IDENTITY_M8000, ])
def test_logix_init_micro800(identity):
    with mock.patch.object(LogixDriver, '_list_identity') as mock_identity, \
         mock.patch.object(LogixDriver, 'get_plc_info') as mock_get_info, \
         mock.patch.object(LogixDriver, 'get_plc_name') as mock_get_name:

        mock_identity.return_value = identity
        mock_get_info.return_value = identity

        plc = LogixDriver(CONNECT_PATH)
        plc._initialize_driver(False, False)

        assert plc._micro800 is True
        assert plc._cfg['use_instance_ids'] is False
        assert mock_get_info.called
        assert not mock_get_name.called
        assert not plc._cfg['cip_path']


@pytest.mark.parametrize('identity', [IDENTITY_CLX_V20, IDENTITY_CLX_V21, IDENTITY_CLX_V32, IDENTITY_M8000])
def test_logix_init_calls_get_tag_list_if_init_tags(identity):
    with mock.patch.object(LogixDriver, '_list_identity') as mock_identity, \
         mock.patch.object(LogixDriver, 'get_plc_info') as mock_get_info, \
         mock.patch.object(LogixDriver, 'get_plc_name'), \
         mock.patch.object(CIPDriver, 'open'), \
         mock.patch.object(LogixDriver, 'get_tag_list') as mock_tag:

        mock_identity.return_value = identity
        mock_get_info.return_value = identity
        driver = LogixDriver(CONNECT_PATH, init_info=False, init_tags=True)
        driver._target_is_connected = True
        driver.open()
    assert mock_tag.called


def test_logix_context_manager_calls_open_and_close():
    with mock.patch.object(LogixDriver, 'open') as mock_open, \
            mock.patch.object(LogixDriver, 'close') as mock_close:
        with LogixDriver(CONNECT_PATH, init_info=False, init_tags=False):
            pass

        assert mock_open.called
        assert mock_close.called


def test__exit__returns_false_on_commerror():
    ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    assert ld.__exit__(None, None, None) is True  # Exit with no exception


def test__exit__returns_true_on_no_error_and_no_exc_type():
    with mock.patch.object(LogixDriver, 'close'):
        ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
        assert ld.__exit__(None, None, None) is True


def test__exit__returns_false_on_no_error_and_exc_type():
    with mock.patch.object(LogixDriver, 'close'):
        ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
        assert ld.__exit__('Some Exc Type', None, None) is False


def test__repr___ret_str():
    ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    _repr = repr(ld)
    assert repr
    assert isinstance(_repr, str)


def test_default_logix_tags_are_empty_dict():
    """Show that LogixDriver tags are an empty dict on init."""
    ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    assert ld.tags == dict()


def test_logix_connected_false_on_init_with_false_init_params():
    ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    assert ld.connected is False


def test_clx_get_plc_time_sends_packet():
    with mock.patch.object(LogixDriver, 'send') as mock_send, \
            mock.patch('pycomm3.cip_driver.with_forward_open'):
        ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
        ld.get_plc_time()
        assert mock_send.called


def test_clx_set_plc_time_sends_packet():
    with mock.patch.object(LogixDriver, 'send') as mock_send, \
            mock.patch('pycomm3.cip_driver.with_forward_open'):
        ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
        ld.set_plc_time()
        assert mock_send.called


# TODO: all of the tag list associated tests

@pytest.mark.skip(reason="""tag parsing is extremely complex, and it's \
nearly impossible to test this without also reverse-engineering it""")
def test__get_tag_list_returns_expected_user_tags():
    EXPECTED_USER_TAGS = [{
        'tag_type': 'struct',  # bit 15 is a 1
        'instance_id': 1,
        'tag_name': b"\x00\x01",
        'symbol_type': "",
        'symbol_address': "",
        'symbol_object_address': "",
        'software_control': "",
        'external_access': "",
        'dimensions': ["", "", ""]
    }]

    TEST_RESPONSE = ResponsePacket()
    # 0 -> 4 are the 'instance', dint
    # 4 -> 6 is the 'tag_length', uint, used internally
    # 8 -> 'tag_length' is 'tag_name'
    # 8+tag_length -> 10+tag_length is 'symbol_type' uint
    # 10+tag_length -> 14+tag_length is 'symbol_address' udint
    # 14+tag_length -> 18+tag_length is 'symbol_object_address' udint
    # 18+tag_length -> 22+tag_length is 'software_control' udint
    # 'dim1', 'dim2' and 'dim3' are the next 12 bytes, udint
    TEST_RESPONSE.data = \
        b"\x00\x00\x00\x01" + \
        b"\x00\x01" + \
        b"\x00\x01" + \
        b"\x00\x00\x00\x00\x00\x10"
    TEST_RESPONSE.command = "Something"
    TEST_RESPONSE.command_status = SUCCESS

    ld = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    with mock.patch.object(RequestPacket, 'send') as mock_send, \
            mock.patch.object(CIPDriver, '_forward_open'), \
            mock.patch.object(LogixDriver, '_parse_instance_attribute_list'):
        mock_send.return_value = TEST_RESPONSE
        actual_tags = ld.get_tag_list()
    assert EXPECTED_USER_TAGS == actual_tags


DINT_TYPE = UINT.encode(DINT.code)
REAL_TYPE = UINT.encode(0xCA)
STRUCT_REPLY = b"\xa0\x02"


def _dint_array_tag_info(elements):
    return {
        "tag_type": "atomic",
        "data_type_name": "DINT",
        "data_type": "DINT",
        "type_class": Array(elements, DINT),
    }


def _fragment_reply(service_status, data):
    """Builds raw SendUnitData reply bytes for a read_tag_fragmented response."""
    message = (
        b"\x00\x00"          # sequence
        b"\xd2"              # service: read_tag_fragmented reply
        b"\x00"              # reserved
        + bytes([service_status])
        + b"\x00"            # additional status size
        + data
    )
    return b"".join([
        b"\x70\x00",          # command: send_unit_data
        b"\x00\x00",          # length (not validated when parsing)
        b"\x00\x00\x00\x00",  # session handle
        b"\x00\x00\x00\x00",  # encapsulation status: success
        b"\x00" * 8,          # sender context
        b"\x00\x00\x00\x00",  # options
        b"\x00\x00\x00\x00",  # interface handle
        b"\x0a\x00",          # timeout
        b"\x02\x00",          # item count
        b"\xa1\x00",          # address item: connection based
        b"\x04\x00",          # address item length
        b"\x00\x00\x00\x00",  # connection id
        b"\xb1\x00",          # data item: connected
        UINT.encode(len(message)),
        message,
    ])


def _send_fragmented_read(replies, elements=3):
    """
    Runs LogixDriver._send_read_fragmented with CIPDriver.send mocked to return
    fragment responses built from ``replies`` (a dict of {byte offset: raw reply}).
    Returns the reassembled response and the list of offsets that were requested.
    """
    driver = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    request = ReadTagFragmentedRequestPacket(
        cycle(65535), "TestArray", elements, _dint_array_tag_info(elements), 0, True
    )
    requested_offsets = []

    def fake_send(req):
        requested_offsets.append(req.offset)
        return ReadTagFragmentedResponsePacket(req, replies[req.offset])

    with mock.patch.object(CIPDriver, "send", side_effect=fake_send):
        response = driver._send_read_fragmented(request)

    return response, requested_offsets


def test_fragmented_read_reassembles_compatible_fragments():
    values = [100, 200, 300]
    data = b"".join(DINT.encode(v) for v in values)
    frag_1, frag_2 = data[:6], data[6:]
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, DINT_TYPE + frag_1),
        len(frag_1): _fragment_reply(SUCCESS, DINT_TYPE + frag_2),
    }

    response, requested_offsets = _send_fragmented_read(replies)

    assert response
    assert response.value == values
    assert response.data_type == "DINT[3]"
    assert response.value_bytes == data
    # offsets are protocol byte offsets, not element counts
    assert requested_offsets == [0, len(frag_1)]
    # the reassembled response references each original fragment response
    assert len(response.responses) == 2
    assert [resp.offset for resp in response.responses] == [0, len(frag_1)]
    assert [resp.service_status for resp in response.responses] == [
        INSUFFICIENT_PACKETS,
        SUCCESS,
    ]
    assert all(resp.data_type_bytes == DINT_TYPE for resp in response.responses)


def test_fragmented_read_partial_reply_is_not_a_failure():
    data = DINT.encode(1)
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, DINT_TYPE + data[:2]),
        2: _fragment_reply(SUCCESS, DINT_TYPE + data[2:]),
    }

    response, _ = _send_fragmented_read(replies, elements=1)

    assert response
    assert response.value == 1
    assert response.error is None


def test_fragmented_read_does_not_merge_changed_data_type():
    data = DINT.encode(1) + DINT.encode(2)
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, DINT_TYPE + data[:4]),
        4: _fragment_reply(SUCCESS, REAL_TYPE + data[4:]),
    }

    response, _ = _send_fragmented_read(replies)

    assert not response
    assert "data type" in response.error
    assert response.value is None
    # both incomplete observations are kept, not merged together
    assert len(response.responses) == 2
    assert response.responses[0].data_type_bytes == DINT_TYPE
    assert response.responses[1].data_type_bytes == REAL_TYPE


def test_fragmented_read_does_not_merge_changed_structure_template():
    handle_1 = STRUCT_REPLY + UINT.encode(0x1234)
    handle_2 = STRUCT_REPLY + UINT.encode(0x5678)
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, handle_1 + b"\x01" * 8),
        8: _fragment_reply(SUCCESS, handle_2 + b"\x02" * 8),
    }

    response, _ = _send_fragmented_read(replies)

    assert not response
    assert "data type" in response.error
    assert len(response.responses) == 2
    assert response.responses[0].data_type_bytes == handle_1
    assert response.responses[1].data_type_bytes == handle_2


def test_fragmented_read_failed_fragment_kept_at_original_position():
    data = DINT.encode(1) + DINT.encode(2)
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, DINT_TYPE + data[:4]),
        4: None,  # transport-level failure, no reply received
    }

    response, requested_offsets = _send_fragmented_read(replies)

    assert not response
    assert response.error == "No response data received"
    assert requested_offsets == [0, 4]
    # the gap stays at its original position in the segment list
    assert len(response.responses) == 2
    assert response.responses[0]
    assert not response.responses[1]
    assert response.responses[1].offset == 4


def test_fragmented_read_no_progress_fragment_fails():
    replies = {
        0: _fragment_reply(INSUFFICIENT_PACKETS, DINT_TYPE),  # no value bytes
    }

    response, requested_offsets = _send_fragmented_read(replies)

    assert not response
    assert "no new data" in response.error
    assert requested_offsets == [0]


def _fragment_response(offset, value_bytes, data_type=DINT_TYPE):
    request = ReadTagFragmentedRequestPacket(
        cycle(65535), "TestArray", 1, _dint_array_tag_info(1), 0, True, offset=offset
    )
    return ReadTagFragmentedResponsePacket(
        request, _fragment_reply(SUCCESS, data_type + value_bytes)
    )


def test_merge_fragment_verifies_matching_overlap():
    buffer = bytearray(b"\x01\x02\x0a\x0b")
    response = _fragment_response(2, b"\x0a\x0b\x0c\x0d")

    assert LogixDriver._merge_fragment(buffer, response) is None
    assert buffer == b"\x01\x02\x0a\x0b\x0c\x0d"


def test_merge_fragment_rejects_mismatched_overlap():
    buffer = bytearray(b"\x01\x02\xff\xff")
    response = _fragment_response(2, b"\x0a\x0b\x0c\x0d")

    assert LogixDriver._merge_fragment(buffer, response) is not None
    assert buffer == b"\x01\x02\xff\xff"  # buffer left unchanged


def test_merge_fragment_rejects_gap():
    buffer = bytearray(b"\x01\x02")
    response = _fragment_response(6, b"\x0a\x0b")

    assert LogixDriver._merge_fragment(buffer, response) is not None
    assert buffer == b"\x01\x02"  # buffer left unchanged
