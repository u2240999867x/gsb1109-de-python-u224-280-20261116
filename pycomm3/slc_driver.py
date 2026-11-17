# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

__all__ = [
    "SLCDriver",
    "parse_tag",
    "format_address",
    "SLCCollection",
    "ElementRecord",
    "ElementAddress",
    "FieldResult",
]

import logging
import re
from collections import OrderedDict
from typing import List, Tuple, Optional, Union

from .cip_driver import CIPDriver, with_forward_open
from .cip import (
    PCCC_CT,
    PCCC_DATA_TYPE,
    PCCC_DATA_SIZE,
    PCCC_ELEMENT_FIELDS,
    PCCC_ELEMENT_FIELD_NAMES,
    PCCC_ERROR_CODE,
    USINT,
    UINT,
    PCCCDataTypes,
)
from .const import (
    SUCCESS,
    SLC_CMD_CODE,
    SLC_FNC_READ,
    SLC_FNC_WRITE,
    SLC_REPLY_START,
    PCCC_PATH,
)
from .exceptions import ResponseError, RequestError
from .slc_collect import (
    SLCCollection,
    ElementAddress,
    ElementRecord,
    FieldResult,
    format_address,
    get_bit,
    parse_element_data,
    split_elements,
)
from .tag import Tag
from .packets import SendUnitDataRequestPacket

AtomicValueType = Union[int, float, bool]
TagValueType = Union[AtomicValueType, List[Union[AtomicValueType, str]]]
ReadWriteReturnType = Union[Tag, List[Tag]]

IO_RE = re.compile(
    r"(?P<file_type>[IO])(?P<file_number>\d{1,3})?"
    r"(:)(?P<element_number>\d{1,3})"
    r"((\.)(?P<position_number>\d{1,3}))?"
    r"(/(?P<sub_element>\d{1,2}))?"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

CT_FIELD_RE = re.compile(
    r"(?<!S)(?P<file_type>[CT])(?P<file_number>\d{1,3})"
    r"[:.]"
    r"(?P<element_number>\d{1,3})"
    r"(?:[:./](?P<field>ACC|PRE|EN|DN|TT|CU|CD|OV|UN|UA))?"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

LFBN_RE = re.compile(
    r"(?P<file_type>[LFBN])(?P<file_number>\d{1,3})"
    r"[:.]"
    r"(?P<element_number>\d{1,3})"
    r"(?:[/.](?P<sub_element>\d{1,2}))?"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

S_RE = re.compile(
    r"(?P<file_type>S)"
    r":(?P<element_number>\d{1,3})"
    r"(?:/(?P<sub_element>\d{1,2}))?"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

A_RE = re.compile(
    r"(?P<file_type>A)(?P<file_number>\d{1,3})"
    r":(?P<element_number>\d{1,4})"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

B_RE = re.compile(
    r"(?P<file_type>B)(?P<file_number>\d{1,3})"
    r"(/)(?P<element_number>\d{1,4})"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

ST_RE = re.compile(
    r"(?P<file_type>ST)(?P<file_number>\d{1,3})"
    r"[:.](?P<element_number>\d{1,4})"
    r"(?:\.(?P<field>LEN|DATA))?"
    r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
    flags=re.IGNORECASE,
)

# all patterns used by parse_tag, tried in order
_TAG_PATTERNS = (
    ("ST", ST_RE),
    ("CT", CT_FIELD_RE),
    ("LFBN", LFBN_RE),
    ("IO", IO_RE),
    ("A", A_RE),
    ("S", S_RE),
    ("B", B_RE),
)


def _match_tag(tag: str) -> Optional[Tuple[str, re.Match]]:
    """
    Return the pattern name and match spanning the complete address.

    ``re.search`` alone would accept ``ST9:0`` as ``T9:0`` or ignore trailing
    subfields, so only a match covering the whole (stripped) address counts.
    Longer file-type prefixes are tried first so ``ST`` wins over ``T``.
    """
    candidate = tag.strip()
    for name, pattern in _TAG_PATTERNS:
        t = pattern.fullmatch(candidate)
        if t is not None:
            return name, t
    return None


class SLCDriver(CIPDriver):
    """
    An Ethernet/IP Client driver for reading and writing of data files in SLC or MicroLogix PLCs.
    """

    __log = logging.getLogger(f"{__module__}.{__qualname__}")
    _auto_slot_cip_path = True

    def __init__(self, path, *args, **kwargs):
        super().__init__(path, *args, large_packets=False, **kwargs)

    def _msg_start(self):
        """
        No idea what this part is, but it starts all messages
        """
        return b"".join(
            (
                b"\x4b",
                b"\x02",
                b"\x20",  # 8-bit class
                PCCC_PATH,  # b"\x67\x24\x01"
                b"\x07",
                self._cfg["vid"], #"vid": b"\x09\x10",
                self._cfg["vsn"], #"vsn": b"\x09\x10\x19\x71",
            )
        )


    @with_forward_open
    def read(self, *addresses: str) -> ReadWriteReturnType:
        """
        Reads data file addresses. To read multiple words add the word count to the address using curly braces,
        e.g. ``N120:10{10}``.

        Does not track request/response size like the CLXDriver.

        :param addresses: one or many data file addresses to read
        :return: a single or list of ``Tag`` objects
        """
        results = [self._read_tag(tag) for tag in addresses]

        if len(results) == 1:
            return results[0]

        return results

    @with_forward_open
    def collect(self, *addresses: str) -> SLCCollection:
        """
        Collect timer, counter and string elements as records of subfields.

        Each address may be an element (``T4:0``), a subfield (``T4:0.PRE``)
        or a multi-element range (``T4:0{2}``).  Each element range is read
        with a single request and split into fixed-size elements, so values
        are always attributed to the element they were read from.

        A failed read only removes the element's values from the record; the
        element identity is kept and the record is reported as failed.  When
        some but not all declared subfields are present, the record is
        ``partial``; all declared subfields present is ``complete``.

        :param addresses: element or subfield addresses to collect
        :return: an :class:`SLCCollection` of :class:`ElementRecord` objects
        """
        records: "OrderedDict[Tuple[str, int, int], ElementRecord]" = OrderedDict()

        for address in addresses:
            parsed = parse_tag(address)
            if parsed is None:
                raise RequestError(
                    f"Error parsing the tag passed to collect() - {address}"
                )
            if parsed["file_type"] not in PCCC_ELEMENT_FIELDS:
                raise RequestError(
                    f"collect() only supports timer, counter and string files, got {address}"
                )

            file_type = parsed["file_type"]
            file_number = int(parsed["file_number"])
            element_number = int(parsed["element_number"])
            element_count = int(parsed.get("element_count") or 1)
            sub_element = parsed.get("sub_element")

            if sub_element is not None:
                declared = (str(sub_element).upper(),)
            else:
                declared = PCCC_ELEMENT_FIELD_NAMES[file_type]

            for offset in range(element_count):
                element = ElementAddress(
                    file_type, file_number, element_number + offset
                )
                record = records.get(element.identity)
                if record is None:
                    record = ElementRecord(element, (), OrderedDict())
                    records[element.identity] = record
                record.declare(*declared)

            parsed_range = dict(
                parsed,
                element_count=element_count,
                sub_element=None,
                address_field=2,
            )
            try:
                response = self._read_raw(parsed_range)
                status = request_status(response.raw)
            except Exception as err:  # pragma: no cover - defensive
                status = str(err)
                response = None

            chunks = (
                split_elements(file_type, response.raw[SLC_REPLY_START:], element_count)
                if status is None and response is not None
                else []
            )

            for offset in range(element_count):
                element = ElementAddress(
                    file_type, file_number, element_number + offset
                )
                record = records[element.identity]

                if status is not None or offset >= len(chunks):
                    error = status or "No data for element in reply"
                    for field in record.declared:
                        record.set_field(
                            FieldResult(
                                field=field,
                                address=element.with_field(field).address,
                                value=None,
                                error=error,
                                kind=element.with_field(field).kind,
                                element_address=element.element_address,
                            )
                        )
                    if record.failed:
                        record.error = error
                    continue

                values = parse_element_data(file_type, chunks[offset])
                for field in record.declared:
                    value, error = values.get(field, (None, "field not available"))
                    record.set_field(
                        FieldResult(
                            field=field,
                            address=element.with_field(field).address,
                            value=value,
                            error=error,
                            kind=element.with_field(field).kind,
                            element_address=element.element_address,
                        )
                    )
                if not record.errors:
                    record.error = None

        return SLCCollection(list(records.values()))

    def export(self, *addresses: str) -> dict:
        """
        Collect addresses and return a JSON-serializable export.

        The export keeps each element's declared identity and its subfields
        separately, so a subfield error does not hide the element or its
        other values.  :meth:`collect` is the underlying API and
        :meth:`pycomm3.slc_collect.SLCCollection.to_json` produces JSON text.

        :param addresses: element or subfield addresses to collect
        :return: a ``dict`` with the collection data
        """
        return self.collect(*addresses).as_dict()

    def _read_tag(self, tag) -> Tag:
        _tag = parse_tag(tag)
        if _tag is None:
            raise RequestError(f"Error parsing the tag passed to read() - {tag}")

        response = self._read_raw(_tag)
        status = request_status(response.raw)

        if status is not None:
            return Tag(_tag["tag"], None, _tag["file_type"], status)

        try:
            return _parse_read_reply(_tag, response.raw[SLC_REPLY_START:])
        except ResponseError as err:
            self.__log.exception(f'Failed to parse read reply for {_tag["tag"]}')
            return Tag(_tag["tag"], None, _tag["file_type"], str(err))

    def _read_raw(self, _tag: dict):
        """Send one read request for a parsed tag and return the response."""
        byte_size = PCCC_DATA_SIZE[_tag["file_type"]] * _tag["element_count"]
        if byte_size > 0xFF:
            raise RequestError(
                f"Read of {byte_size} bytes exceeds the SLC request limit (255); "
                f"split the address range"
            )
        message_request = [
            self._msg_start(),
            # page 83 of eip manual
            SLC_CMD_CODE,  # request command code
            b"\x00",  # status code
            UINT.encode(next(self._sequence)),  # transaction identifier
            SLC_FNC_READ,  # function code
            USINT.encode(byte_size),  # byte size
            USINT.encode(int(_tag["file_number"])),
            PCCC_DATA_TYPE[_tag["file_type"]],
            USINT.encode(int(_tag["element_number"])),
            USINT.encode(int(_tag.get("pos_number", 0))),  # sub-element number
        ]

        request = SendUnitDataRequestPacket(self._sequence)
        request.add(b"".join(message_request))
        response = self.send(request)
        self.__log.debug(f"SLC read_tag({_tag['tag']})")
        return response

    @with_forward_open
    def write(self, *address_values: Tuple[str, TagValueType]) -> ReadWriteReturnType:
        """
        Write values to data file addresses.  To write to multiple words in a file use curly braces in the address
        to indicate the number of words, then set the value to a list of values to write e.g. ``('N120:10{10}', [1, 2, ...])``.

        Does not track request/response size like the CLXDriver.


        :param address_values: one or many 2-element tuples of (address, value)
        :return: a single or list of ``Tag`` objects
        """
        results = [self._write_tag(tag, value) for tag, value in address_values]

        if len(results) == 1:
            return results[0]

        return results

    def _write_tag(self, tag: str, value: TagValueType) -> Tag:
        """write tag from a connected plc
        Possible combination can be passed to this method:
            c.write_tag('N7:0', [-30, 32767, -32767])
            c.write_tag('N7:0', 21)
            c.read_tag('N7:0', 10)
        It is not possible to write status bit
        :return: None is returned in case of error
        """
        _tag = parse_tag(tag)
        if _tag is None:
            raise RequestError(f"Error parsing the tag passed to write() - {tag}")

        _tag["data_size"] = PCCC_DATA_SIZE[_tag["file_type"]]

        message_request = [
            self._msg_start(),
            SLC_CMD_CODE,
            b"\x00",
            UINT.encode(next(self._sequence)),
            SLC_FNC_WRITE,
            USINT.encode(_tag["data_size"] * _tag["element_count"]),
            USINT.encode(int(_tag["file_number"])),
            PCCC_DATA_TYPE[_tag["file_type"]],
            USINT.encode(int(_tag["element_number"])),
            USINT.encode(int(_tag.get("pos_number", 0))),
            writeable_value(_tag, value),
        ]
        request = SendUnitDataRequestPacket(self._sequence)
        request.add(b"".join(message_request))
        response = self.send(request)

        status = request_status(response.raw)
        if status is not None:
            return Tag(_tag["tag"], None, _tag["file_type"], status)

        return Tag(_tag["tag"], value, _tag["file_type"], None)

    @with_forward_open
    def get_processor_type(self):

        msg_request = (
            self._msg_start(),
            # diagnostic status - CMD 06, FNC 03 - pg93 DF1 manual (1770-rm516)
            b"\x06",  # CMD
            b"\x00",  # status code
            UINT.encode(next(self._sequence)),  # transaction identifier
            b"\x03",  # FNC
        )

        request = SendUnitDataRequestPacket(self._sequence)
        request.add(b"".join(msg_request))
        response = self.send(request)
        if response:
            try:
                typ = response.raw[SLC_REPLY_START:][5:16].decode("utf-8").strip()
            except Exception as err:
                self.__log.exception(f"failed getting processor type: {err}")
                typ = None
            finally:
                return typ
        else:
            self.__log.error(
                f"failed to get processor type: {request_status(response.raw)}",
            )
            return None
        
    @with_forward_open
    def get_datalog_queue(self, num_data_logs, queue_num):
        data = []
        
        for i in range(num_data_logs):        
            data.append(self._get_datalog(queue_num))
        
        #extra read to clear the queue
        #will thow error in _get_datalog due to Status == None
        trash = self._get_datalog(queue_num)
        
        if data is not None:
            return data
        else:
            raise ResponseError("No Data in Queue")
        raise ResponseError("Failed to read processor type")
        
    def _get_datalog(self, queue_num):
        msg_request = [
            b"\x4b",            # Ethernet/IP Service Code
            b"\x02",            # Request Path Size, 2 words
            b"\x20",            # Request Path, Path Segment (8-bit Class)
            b"\x67",            # Request Path, Path Segment, Class (PCCC Class)
            b"\x24",            # Request Path, Path Segment (8 Bit Instance)
            b"\x01",            # Request Path, Path Segment, Instance 
            b"\x07",            # Requestor ID, Length
            b"\x4d\x00",        # Requestor ID, CIP Vendor ID
            b"\xa1\x4e\xc3\x30",# Requestor ID, CIP Serial Number
            b"\x0f",            # PCCC Command Data, CMD code
            b"\x00",            # PCCC Command Data, Status Code
            b"\x30\x00",         # PCCC Command Data, Transaction Code
            b"\xa2",            # PCCC Command Data, Function Code
            b"\x6d",            # Function Specific Data, Byte Size
            b"\x00",            # Function Specific Data, File Number
            b"\xa5",            # Function Specific Data, File Type
            USINT.encode(queue_num), # Function Specific Data, Element Number (queue to be read)
            b"\x00",            # Function Specific Data, Sub-Element Number
        ]
        
        request = SendUnitDataRequestPacket(self._sequence)
        request.add(b"".join(msg_request))
        response = self.send(request)

        status = request_status(response.raw)
        
        if status is None:
            try:
                datalog_entry = response.raw[SLC_REPLY_START:]
                datalog_entry = datalog_entry.decode("UTF-8")
            except Exception as err:
                self.__log.exception("Failed to retreive data log")
            finally:
                return datalog_entry
        else:
            self.__log.error(
                f"Failed to retreive data log",
            )
            return None
        
    
    @with_forward_open
    def get_file_directory(self):
        plc_type = self.get_processor_type()

        if plc_type is not None:
            sys0_info = _get_sys0_info(plc_type)
            # file_type, element = _get_file_and_element_for_plc_type(plc_type)
            sys0_info["size"] = self._get_file_directory_size(sys0_info)
            
            if sys0_info["size"] is not None:
                data = self._read_whole_file_directory(sys0_info)
                return _parse_file0(sys0_info, data)
            else:
                raise ResponseError("Failed to read file directory size")
        else:
            raise ResponseError("Failed to read processor type")

    def _get_file_directory_size(self, sys0_info):
        msg_request = [
            self._msg_start(),
            SLC_CMD_CODE,  # request command code
            b"\x00",  # status code
            UINT.encode(next(self._sequence)),  # transaction identifier
            b"\xa1",  # function code, from RSLinx capture
            sys0_info["size_len"],  # size
            b"\x00",  # file number
            sys0_info["file_type"],
            sys0_info["size_element"],
        ]

        request = SendUnitDataRequestPacket(self._sequence)
        request.add(b"".join(msg_request))
        response = self.send(request)
        status = request_status(response.raw)
        if status is None:
            try:
                size = UINT.decode(response.raw[SLC_REPLY_START:]) - sys0_info.get("size_const", 0)
                self.__log.debug(f"SYS 0 file size: {size}")
            except Exception as err:
                self.__log.exception("failed to parse size of File 0")
                size = None
            finally:
                return size
        else:
            self.__log.error(
                f"failed to read size of File 0: {status}",
            )
            return None

    def _read_whole_file_directory(self, sys0_info):
        file0_data = b""
        offset = 0
        file0_size = sys0_info["size"]
        file_type = sys0_info["file_type"]

        while len(file0_data) < file0_size:
            bytes_remaining = file0_size - len(file0_data)
            size = 0x50 if bytes_remaining > 0x50 else bytes_remaining

            msg_request = [
                self._msg_start(),
                # page 83 of eip manual
                SLC_CMD_CODE,  # request command code
                b"\x00",  # status code
                UINT.encode(next(self._sequence)),  # transaction identifier
                b"\xa1",
                # SLC_FNC_READ,  # function code
                USINT.encode(size),  # size
                b"\x00",  # file number
                file_type,
            ]

            msg_request += (
                [USINT.encode(offset)] if offset < 256 else [b"\xFF", UINT.encode(offset)]
            )

            request = SendUnitDataRequestPacket(self._sequence)
            request.add(b"".join(msg_request))
            response = self.send(request)
            status = request_status(response.raw)
            if status is None:
                data = response.raw[SLC_REPLY_START:]
                offset += len(data) // 2
                file0_data += data
            else:
                msg = f"Error reading File 0 contents: {status}"
                self.__log.error(msg)
                raise ResponseError(msg)

        return file0_data


def _parse_file0(sys0_info, data):
    num_data_files = data[52]
    num_lad_files = data[46]
    print(f"data files: {num_data_files}, logic files: {num_lad_files}")

    file_pos = sys0_info["file_position"]
    row_size = sys0_info["row_size"]

    data_files = {}
    file_num = 0
    while file_pos < len(data):
        file_code = data[file_pos : file_pos + 1]
        file_type = PCCC_DATA_TYPE.get(file_code, None)

        if file_type:
            file_name = f"{file_type}{file_num}"

            element_size = PCCC_DATA_SIZE.get(file_type, 2)
            file_size = UINT.decode(data[file_pos + 1 :])
            data_files[file_name] = {
                "elements": file_size // element_size,
                "length": file_size,
            }

        if file_type or file_code == b"\x81":  # 0x81 reserved type, for skipped file numbers?
            file_num += 1

        file_pos += row_size

    return data_files


def _get_sys0_info(plc_type):
    prefix = plc_type[:4]

    if prefix in {
        "1761",
    }:  # MLX1000, SLC 5/02
        # FIXME: Not sure if these are correct, never tested
        return {
            "file_position": 93,
            "row_size": 8,
            "file_type": b"\x00",
            "size_element": b"\x23",
            "size_len": b"\x04",
        }
    elif prefix in {"1763", "1762", "1764"}:  # MLX 1100, 1200, 1500
        # FIXME: values from 1100 and 1400, not tested on 1200/1500
        return {
            "file_position": 233,
            "row_size": 10,
            "file_type": b"\x02",
            "size_element": b"\x28",
            "size_len": b"\x08",
            "size_const": 19968,  # no idea why, but this seems like a const added to the size? wtf?
        }
    elif prefix in {
        "1766",
    }:  # MLX 1400
        return {
            "file_position": 233,
            "row_size": 10,
            "file_type": b"\x03",
            "size_element": b"\x2b",
            "size_len": b"\x08",
            "size_const": 19968,  # no idea why, but this seems like a const added to the size? wtf?
            "file_type_queue": b"\xA5",
        }
    else:  # SLC 5/05
        return {
            "file_position": 79,
            "row_size": 10,
            "file_type": b"\x01",
            "size_element": b"\x23",
            "size_len": b"\x04",
        }


def _parse_read_reply(tag, data) -> Tag:
    """
    Decode a read reply into a ``Tag``.

    Timer, counter and string files are decoded as whole elements, word by
    word, using the element layout from :data:`PCCC_ELEMENT_FIELDS`; the
    sub-element field (if any) selects one value from the decoded element.
    Other file types keep the previous flat-word behavior.
    """
    try:
        file_type = tag["file_type"]
        element_count = int(tag.get("element_count") or 1)
        data_size = PCCC_DATA_SIZE[file_type]

        if file_type in PCCC_ELEMENT_FIELDS:
            fields = _load_element_fields(file_type, data, element_count)
            is_field_read = (
                tag.get("address_field") == 3
                and tag.get("sub_element") is not None
            )
            if is_field_read:
                field_name = _normalize_field_name(file_type, tag["sub_element"])
                values = []
                for field_values in fields:
                    value, error = field_values[field_name]
                    if error is not None:
                        raise ResponseError(error)
                    values.append(value)
            else:
                values = [_element_value(file_type, field_values) for field_values in fields]

            if element_count > 1:
                return Tag(tag["tag"], values, file_type, None)
            return Tag(tag["tag"], values[0], file_type, None)

        unpack_func = PCCCDataTypes[file_type].decode
        bit_read = tag.get("address_field", 0) == 3
        bit_position = int(tag.get("sub_element") or 0)
        if bit_read:
            tag_value = unpack_func(data[:data_size])
            return Tag(tag["tag"], get_bit(tag_value, bit_position), file_type, None)

        values_list = [
            unpack_func(data[i : i + data_size])
            for i in range(0, len(data), data_size)
        ]
        if len(values_list) > 1:
            return Tag(tag["tag"], values_list, file_type, None)
        return Tag(tag["tag"], values_list[0], file_type, None)
    except Exception as err:
        raise ResponseError("Failed parsing tag read reply") from err


def _normalize_field_name(file_type: str, sub_element) -> str:
    """Map a parsed sub-element back to a field name."""
    if isinstance(sub_element, int) or str(sub_element).isdigit():
        # legacy callers used the bit number from PCCC_CT as the sub-element
        bit = int(sub_element)
        for name, _, kind, field_bit in PCCC_ELEMENT_FIELDS[file_type]:
            if kind == "status" and field_bit == bit:
                return name
        legacy = {number: name for name, number in PCCC_CT.items()}
        if bit in legacy and legacy[bit] in PCCC_ELEMENT_FIELD_NAMES[file_type]:
            return legacy[bit]
        raise ResponseError(
            f"Unknown sub-element {sub_element!r} for file type {file_type!r}"
        )

    name = str(sub_element).upper()
    if name not in PCCC_ELEMENT_FIELD_NAMES[file_type]:
        raise ResponseError(
            f"Unknown field {sub_element!r} for file type {file_type!r}"
        )
    return name


def _element_value(file_type: str, field_values):
    """
    Build the value of a whole element.

    Strings historically read as a plain ``str``, so keep that shape while
    exposing ``LEN``/``DATA`` through subfield reads and ``collect``.  A
    whole-element read is all-or-nothing; use :meth:`SLCDriver.collect` to
    represent an element whose fields could only partially be read.
    """
    if file_type == "ST":
        value, error = field_values["DATA"]
        if error is not None:
            raise ResponseError(error)
        return value

    values = OrderedDict()
    for name in PCCC_ELEMENT_FIELD_NAMES[file_type]:
        value, error = field_values[name]
        if error is not None:
            raise ResponseError(error)
        values[name] = value
    return values


def _load_element_fields(file_type: str, data: bytes, element_count: int):
    """Decode each element of ``file_type`` from ``data``."""
    return [
        parse_element_data(file_type, chunk)
        for chunk in split_elements(file_type, data, element_count)
    ]


def parse_tag(tag: str) -> Optional[dict]:
    matched = _match_tag(tag)
    if matched is None:
        return None
    name, t = matched
    return _TAG_PARSERS[name](t)


def _tag_name(t) -> str:
    _cnt = t.group("_elem_cnt_token")
    return t.group(0).replace(_cnt, "") if _cnt else t.group(0)


def _element_count(t) -> int:
    return int(t.group("element_count") or 1)


def _parse_ct_tag(t) -> Optional[dict]:
    if not (
        (1 <= int(t.group("file_number")) <= 255)
        and (0 <= int(t.group("element_number")) <= 255)
    ):
        return None
    field = t.group("field")
    if field is not None:
        field = field.upper()
    return {
        "file_type": t.group("file_type").upper(),
        "file_number": t.group("file_number"),
        "element_number": t.group("element_number"),
        "sub_element": field,
        "address_field": 3 if field else 2,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


def _parse_lfbn_tag(t) -> Optional[dict]:
    if not (
        (1 <= int(t.group("file_number")) <= 255)
        and (0 <= int(t.group("element_number")) <= 255)
    ):
        return None

    sub_element = t.group("sub_element")
    if sub_element is not None and not 0 <= int(sub_element) <= 15:
        return None

    return {
        "file_type": t.group("file_type").upper(),
        "file_number": t.group("file_number"),
        "element_number": t.group("element_number"),
        "sub_element": sub_element,
        "address_field": 3 if sub_element is not None else 2,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


def _parse_io_tag(t) -> Optional[dict]:
    if not (0 <= int(t.group("element_number")) <= 255):
        return None
    if t.group("sub_element") is not None and not (
        0 <= int(t.group("sub_element")) <= 15
    ):
        return None

    return {
        "file_type": t.group("file_type").upper(),
        "file_number": "0" if t.group("file_type").upper() == "O" else "1",
        "element_number": t.group("element_number"),
        "pos_number": t.group("position_number") or "0",
        "sub_element": t.group("sub_element") or 0,
        "address_field": 3 if t.group("sub_element") is not None else 2,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


def _parse_a_tag(t) -> Optional[dict]:
    if not (
        (1 <= int(t.group("file_number")) <= 255)
        and (0 <= int(t.group("element_number")) <= 255)
    ):
        return None
    return {
        "file_type": "A",
        "file_number": t.group("file_number"),
        "element_number": int(t.group("element_number")),
        "address_field": 2,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


def _parse_s_tag(t) -> Optional[dict]:
    if not (0 <= int(t.group("element_number")) <= 255):
        return None

    sub_element = t.group("sub_element")
    if sub_element is not None and not 0 <= int(sub_element) <= 15:
        return None

    return {
        "file_type": "S",
        "file_number": "2",
        "element_number": t.group("element_number"),
        "sub_element": sub_element,
        "address_field": 3 if sub_element is not None else 2,
        "element_count": _element_count(t),
        "tag": t.group(0),
    }


def _parse_st_tag(t) -> Optional[dict]:
    if not (
        (1 <= int(t.group("file_number")) <= 255)
        and (0 <= int(t.group("element_number")) <= 255)
    ):
        return None

    field = t.group("field")
    if field is not None:
        field = field.upper()

    return {
        "file_type": "ST",
        "file_number": t.group("file_number"),
        "element_number": int(t.group("element_number")),
        "sub_element": field,
        "address_field": 3 if field else 2,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


def _parse_b_tag(t) -> Optional[dict]:
    """Binary file bit address, e.g. ``B3/5`` -> element 0, bit 5."""
    if not (
        (1 <= int(t.group("file_number")) <= 255)
        and (0 <= int(t.group("element_number")) <= 4095)
    ):
        return None

    bit_position = int(t.group("element_number"))
    element_number, sub_element = divmod(bit_position, 16)
    return {
        "file_type": "B",
        "file_number": t.group("file_number"),
        "element_number": element_number,
        "sub_element": sub_element,
        "address_field": 3,
        "element_count": _element_count(t),
        "tag": _tag_name(t),
    }


_TAG_PARSERS = {
    "ST": _parse_st_tag,
    "CT": _parse_ct_tag,
    "LFBN": _parse_lfbn_tag,
    "IO": _parse_io_tag,
    "A": _parse_a_tag,
    "S": _parse_s_tag,
    "B": _parse_b_tag,
}


def _sub_element_bit(tag: dict) -> int:
    """Resolve the bit position for a status field address."""
    sub_element = tag.get("sub_element")
    if isinstance(sub_element, int):
        return sub_element
    fields = PCCC_ELEMENT_FIELDS.get(tag["file_type"], ())
    for name, _, kind, bit in fields:
        if name == str(sub_element).upper() and kind == "status":
            return bit
    try:
        return int(sub_element)
    except (TypeError, ValueError):
        return 0


def writeable_value(tag: dict, value: Union[bytes, TagValueType]) -> bytes:
    if isinstance(value, bytes):
        return value
    bit_field = tag.get("address_field", 0) == 3
    sub_element = tag.get("sub_element")
    bit_position = _sub_element_bit(tag) if bit_field else 0
    bit_mask = UINT.encode(2 ** bit_position) if bit_field else b"\xFF\xFF"

    element_count = tag.get("element_count") or 1
    if element_count > 1:
        if len(value) < element_count:
            raise RequestError(
                f"Insufficient data for requested elements, expected {element_count} and got {len(value)}"
            )
        if len(value) > element_count:
            value = value[:element_count]

    try:
        pack_func = PCCCDataTypes[tag["file_type"]].encode

        if element_count > 1:
            _value = b"".join(pack_func(val) for val in value)
        else:
            if bit_field:
                tag["data_size"] = 2

                if tag["file_type"] in ["T", "C"] and sub_element in {
                    "PRE",
                    "ACC",
                }:
                    bit_mask = b"\xff\xff"
                    _value = pack_func(value)
                else:
                    _value = bit_mask if value else b"\x00\x00"
            else:
                _value = pack_func(value)

    except Exception as err:
        raise RequestError(
            f'Failed to create a writeable value for {tag["tag"]} from {value}'
        ) from err

    else:
        return bit_mask + _value


def request_status(data) -> Optional[str]:
    try:
        _status_code = int(data[58])
        if _status_code == SUCCESS:
            return None
        return PCCC_ERROR_CODE.get(_status_code, "Unknown Status")
    except Exception:
        return "Unknown Status"
