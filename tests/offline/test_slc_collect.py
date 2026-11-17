"""Tests for SLC element collection.

Covers timer/counter/string element decoding, subfield addressing, fixed
stride splitting of multi-element replies and the complete/partial/failed
record model used by ``SLCDriver.collect`` / ``SLCDriver.export``.
"""

import json
import struct
from unittest import mock

import pytest

from pycomm3.const import SLC_REPLY_START, SUCCESS
from pycomm3.exceptions import ResponseError
from pycomm3.packets import RequestPacket, ResponsePacket
from pycomm3.slc_collect import (
    ElementAddress,
    ElementRecord,
    FieldResult,
    SLCCollection,
    format_address,
    parse_element_data,
    split_elements,
)
from pycomm3.slc_driver import SLCDriver, _parse_read_reply, parse_tag
from pycomm3.cip.pccc import PCCC_STRING

CONNECT_PATH = '192.168.1.100/1'


def _reply(payload: bytes, status: int = SUCCESS) -> ResponsePacket:
    raw = bytearray(SLC_REPLY_START)
    raw[58] = status
    return ResponsePacket(RequestPacket(), bytes(raw) + payload)


def _timer(status=0, pre=0, acc=0):
    return struct.pack('<Hhh', status, pre, acc)


def _counter(status=0, pre=0, acc=0):
    return struct.pack('<Hhh', status, pre, acc)


def _string(value: str):
    encoded = PCCC_STRING.encode(value)
    return encoded + b'\x00' * (84 - len(encoded))


def test_parse_timer_element_address():
    parsed = parse_tag('T4:0')
    assert parsed['file_type'] == 'T'
    assert parsed['file_number'] == '4'
    assert parsed['element_number'] == '0'
    assert parsed['sub_element'] is None
    assert parsed['address_field'] == 2
    assert parsed['element_count'] == 1


def test_parse_timer_subfield_is_named():
    parsed = parse_tag('T4:0.PRE')
    assert parsed['sub_element'] == 'PRE'
    assert parsed['address_field'] == 3
    assert parsed['tag'] == 'T4:0.PRE'


def test_parse_counter_status_subfields():
    assert parse_tag('C5:3.DN')['sub_element'] == 'DN'
    assert parse_tag('C5:3.CU')['sub_element'] == 'CU'
    assert parse_tag('C5:3.ACC')['sub_element'] == 'ACC'


def test_parse_string_not_confused_with_timer():
    parsed = parse_tag('ST9:0')
    assert parsed['file_type'] == 'ST'
    assert parsed['file_number'] == '9'
    assert parsed['element_number'] == 0
    assert parsed['sub_element'] is None

    assert parse_tag('ST9:0.LEN')['sub_element'] == 'LEN'
    assert parse_tag('ST9:0.DATA')['sub_element'] == 'DATA'


def test_parse_element_count():
    assert parse_tag('T4:0{2}')['element_count'] == 2
    assert parse_tag('C5:3{4}')['element_count'] == 4
    assert parse_tag('ST9:0{2}')['element_count'] == 2


def test_parse_unrelated_file_addresses_still_supported():
    assert parse_tag('N7:0')['file_type'] == 'N'
    assert parse_tag('F8:0')['file_type'] == 'F'
    assert int(parse_tag('B3:0/5')['sub_element']) == 5
    assert parse_tag('S:5')['file_type'] == 'S'
    assert parse_tag('I1:0.2')['file_type'] == 'I'


def test_format_address_round_trips():
    for address in ('T4:0', 'T4:0.PRE', 'C5:1.DN', 'ST9:2.DATA'):
        parsed = parse_tag(address)
        rebuilt = format_address(
            parsed['file_type'],
            parsed['file_number'],
            parsed['element_number'],
            parsed.get('sub_element'),
        )
        assert rebuilt == address
        assert parse_tag(rebuilt) == parsed


def test_address_uses_parts_not_display_separator():
    parsed = parse_tag('T4:0/PRE')
    element = ElementAddress(
        parsed['file_type'],
        parsed['file_number'],
        parsed['element_number'],
        parsed['sub_element'],
    )
    assert element.address == 'T4:0.PRE'
    assert element.identity == ('T', 4, 0)


def test_timer_element_decodes_status_and_numeric_words_separately():
    # status word: DN bit 13
    data = _timer(status=1 << 13, pre=100, acc=50)
    tag = parse_tag('T4:0')
    result = _parse_read_reply(tag, data)

    assert result.value == {'PRE': 100, 'ACC': 50, 'EN': False, 'TT': False, 'DN': True}
    assert result.value['PRE'] == 100
    assert result.value['ACC'] == 50
    assert result.value['DN'] is True


def test_counter_element_status_bits_and_values():
    status = (1 << 15) | (1 << 13)  # CU and DN
    data = _counter(status=status, pre=25, acc=10)
    result = _parse_read_reply(parse_tag('C5:0'), data)

    assert result.value['CU'] is True
    assert result.value['CD'] is False
    assert result.value['DN'] is True
    assert result.value['OV'] is False
    assert result.value['PRE'] == 25
    assert result.value['ACC'] == 10


def test_string_element_decodes_len_and_data():
    data = _string('ABC')

    # whole-element reads keep the historical ``str`` shape
    assert _parse_read_reply(parse_tag('ST9:0'), data).value == 'ABC'
    assert _parse_read_reply(parse_tag('ST9:0.LEN'), data).value == 3
    assert _parse_read_reply(parse_tag('ST9:0.DATA'), data).value == 'ABC'

    # ``collect`` exposes both subfields with their own addresses
    values = parse_element_data('ST', data)
    assert values['LEN'] == (3, None)
    assert values['DATA'] == ('ABC', None)


def test_multi_element_read_does_not_borrow_next_element_value():
    first = _timer(status=1 << 13, pre=100, acc=50)
    second = _timer(status=0, pre=999, acc=888)
    result = _parse_read_reply(parse_tag('T4:0{2}'), first + second)

    assert result.value[0]['PRE'] == 100
    assert result.value[0]['ACC'] == 50
    assert result.value[0]['DN'] is True
    assert result.value[1]['PRE'] == 999
    assert result.value[1]['ACC'] == 888
    assert result.value[1]['DN'] is False


def test_split_elements_uses_fixed_stride():
    data = _timer(pre=1, acc=2) + _timer(pre=3, acc=4)
    chunks = split_elements('T', data, 2)
    assert len(chunks) == 2
    assert chunks[0] == data[:6]
    assert chunks[1] == data[6:12]


def test_parse_element_data_isolates_field_failures():
    values = parse_element_data('T', _timer(pre=7, acc=8))
    assert values['PRE'] == (7, None)
    assert values['ACC'] == (8, None)

    short = parse_element_data('ST', b'\x03\x00AB')
    assert short['LEN'][0] == 3
    assert short['DATA'][1] is not None


def test_field_read_returns_only_that_field():
    data = _timer(status=1 << 14, pre=300, acc=20)
    assert _parse_read_reply(parse_tag('T4:0.ACC'), data).value == 20
    assert _parse_read_reply(parse_tag('T4:0.PRE'), data).value == 300
    assert _parse_read_reply(parse_tag('T4:0.TT'), data).value is True


def test_whole_element_read_is_all_or_nothing():
    # status word only: PRE is fine, ACC is missing, so the whole read fails
    data = struct.pack('<H', 1 << 13)
    with pytest.raises(ResponseError):
        _parse_read_reply(parse_tag('T4:0'), data)


def test_field_read_error_only_closes_that_field():
    # string LEN only, DATA is truncated
    data = b'\x03\x00AB'
    with pytest.raises(ResponseError):
        _parse_read_reply(parse_tag('ST9:0.DATA'), data)
    assert _parse_read_reply(parse_tag('ST9:0.LEN'), data).value == 3


def _driver():
    return SLCDriver(CONNECT_PATH)


def test_collect_complete_elements():
    driver = _driver()
    payload = _timer(status=1 << 13, pre=100, acc=50) + _timer(pre=999, acc=888)
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(payload)) as send:
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0{2}')

    assert send.call_count == 1
    assert len(collection) == 2
    assert [record.address for record in collection] == ['T4:0', 'T4:1']
    assert len(collection.complete) == 2
    assert collection.partial == []
    assert collection.failed == []
    assert collection[0].value['ACC'] == 50
    assert collection[1].value['ACC'] == 888


def test_collect_keeps_identity_and_canonical_addresses():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(_timer(pre=5, acc=6))):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0/PRE')

    record = collection[0]
    assert record.identity == ('T', 4, 0)
    assert record.address == 'T4:0'
    assert record.fields['PRE'].address == 'T4:0.PRE'
    assert record.fields['PRE'].element_address == 'T4:0'
    assert record.fields['PRE'].value == 5


def test_collect_subfield_declaration_is_partial_but_ok():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(_timer(pre=5, acc=6))):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0.PRE')

    record = collection[0]
    assert record.declared == ('PRE',)
    assert record.ok is True
    assert record.complete is False
    assert record.partial is True
    assert record.value == {'PRE': 5}


def test_collect_expands_when_full_element_declared_after_subfield():
    driver = _driver()
    payload = _timer(status=1 << 13, pre=5, acc=6)
    with mock.patch.object(
        SLCDriver, 'send', side_effect=[_reply(payload), _reply(payload)]
    ):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0.DN', 'T4:0')

    record = collection[0]
    assert record.declared == ('DN', 'EN', 'TT', 'PRE', 'ACC')
    assert record.complete is True
    assert record.value['DN'] is True


def test_collect_request_failure_marks_elements_failed():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(b'', status=16)):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0{2}')

    assert len(collection) == 2
    assert collection.complete == []
    assert collection.partial == []
    assert len(collection.failed) == 2
    for record in collection:
        assert record.identity[0] == 'T'
        assert record.error is not None
        assert set(record.missing) == set(record.all_fields)


def test_collect_short_response_only_affects_missing_element():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(_timer(pre=1, acc=2))):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0{2}')

    assert collection[0].complete is True
    assert collection[1].failed is True


def test_collect_truncated_element_only_closes_missing_field():
    driver = _driver()
    # status word + PRE, ACC word missing
    truncated = struct.pack('<Hh', 1 << 13, 100)
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(truncated)):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0')

    record = collection[0]
    assert record.complete is False
    assert record.partial is True
    assert record.failed is False
    assert record.value == {'EN': False, 'TT': False, 'DN': True, 'PRE': 100}
    assert set(record.errors) == {'ACC'}
    assert record.fields['ACC'].value is None
    assert record.fields['ACC'].address == 'T4:0.ACC'
    assert record.fields['PRE'].value == 100


def test_collect_string_element_records_identity():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(_string('HELLO'))):
        collection = SLCDriver.collect.__wrapped__(driver, 'ST9:0')

    record = collection[0]
    assert record.identity == ('ST', 9, 0)
    assert record.address == 'ST9:0'
    assert record.value == {'LEN': 5, 'DATA': 'HELLO'}
    assert record.complete is True
    assert record.fields['DATA'].kind == 'data'


def test_collect_counter_fields_have_kinds():
    driver = _driver()
    status = 1 << 15
    with mock.patch.object(
        SLCDriver, 'send', return_value=_reply(_counter(status=status, pre=5, acc=7))
    ):
        collection = SLCDriver.collect.__wrapped__(driver, 'C5:0')

    record = collection[0]
    assert record.fields['CU'].kind == 'status'
    assert record.fields['PRE'].kind == 'numeric'
    assert record.fields['CU'].value is True


def test_collect_rejects_non_element_files():
    with pytest.raises(Exception):
        SLCDriver.collect.__wrapped__(_driver(), 'N7:0')


def test_export_is_json_serializable_and_keeps_states_separate():
    driver = _driver()
    with mock.patch.object(SLCDriver, 'send', return_value=_reply(_timer(pre=1, acc=2))), \
         mock.patch.object(SLCDriver, 'collect', SLCDriver.collect.__wrapped__):
        export = driver.export('T4:0.PRE')

    assert json.dumps(export)
    assert export['complete'] == []
    assert export['partial'] == ['T4:0']
    assert export['failed'] == []
    record = export['records'][0]
    assert record['identity'] == ['T', 4, 0]
    assert record['fields']['PRE']['address'] == 'T4:0.PRE'
    assert record['fields']['PRE']['element'] == 'T4:0'


def test_element_record_repr_marks_state():
    fields = {
        'PRE': FieldResult('PRE', 'T4:0.PRE', 1, None, 'numeric', 'T4:0'),
    }
    record = ElementRecord(ElementAddress('T', 4, 0), ('PRE',), fields)
    assert 'partial' in repr(record)
    assert record.as_dict()['complete'] is False
    assert record.as_dict()['partial'] is True


def test_collect_later_failure_does_not_erase_good_values():
    driver = _driver()
    good = _reply(_timer(status=1 << 13, pre=100, acc=50))
    failed = _reply(b'', status=16)
    with mock.patch.object(SLCDriver, 'send', side_effect=[good, failed]):
        collection = SLCDriver.collect.__wrapped__(driver, 'T4:0', 'T4:0.PRE')

    record = collection[0]
    assert record.declared == ('EN', 'TT', 'DN', 'PRE', 'ACC')
    assert record.complete is True
    assert record.partial is False
    assert record.value['PRE'] == 100
    assert record.value['ACC'] == 50


def test_collect_lookup_by_address():
    record = ElementRecord(
        ElementAddress('T', 4, 1),
        ('PRE',),
        {'PRE': FieldResult('PRE', 'T4:1.PRE', 2, None, 'numeric', 'T4:1')},
    )
    collection = SLCCollection([record])
    assert collection['t4:1'] is record
    assert collection.get('T4:9') is None
    with pytest.raises(KeyError):
        collection['T4:9']


def test_collection_states_are_disjoint():
    complete = ElementRecord(
        ElementAddress('T', 4, 0),
        ('EN', 'TT', 'DN', 'PRE', 'ACC'),
        {
            name: FieldResult(name, f'T4:0.{name}', 0, None, 'numeric', 'T4:0')
            for name in ('EN', 'TT', 'DN', 'PRE', 'ACC')
        },
    )
    partial = ElementRecord(
        ElementAddress('T', 4, 1),
        ('PRE', 'ACC'),
        {'PRE': FieldResult('PRE', 'T4:1.PRE', 1, None, 'numeric', 'T4:1')},
    )
    failed = ElementRecord(ElementAddress('T', 4, 2), ('PRE',), {})
    collection = SLCCollection([complete, partial, failed])

    assert collection.complete == [complete]
    assert collection.partial == [partial]
    assert collection.failed == [failed]
