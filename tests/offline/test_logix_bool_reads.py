"""Offline tests for reading BOOL arrays and BOOLs stored in DWORDs.

BOOL arrays are transferred as DWORDs, but users request a range of BOOLs.
These tests exercise the mapping between the requested logical BOOL indexes,
the actual DWORD range read from the PLC, and each BOOL's source word/bit.
"""
from pycomm3.cip.data_types import DWORD
from pycomm3.logix_driver import LogixDriver


class FakeResponse:
    def __init__(self, request, value):
        self.request = request
        self.tag = request.tag
        self.value = value
        self.data_type = f"BOOL[{request.elements * 32}]"
        self.error = None

    def __bool__(self):
        return True


class FakeMultiResponse:
    def __init__(self, responses):
        self.responses = responses
        self.error = None

    def __bool__(self):
        return True


class BoolDriver(LogixDriver):
    """LogixDriver with a static tag catalog and a fake DWORD transport."""

    catalog = {}

    def __init__(self, words, *args, **kwargs):
        super().__init__("1.2.3.4", init_info=False, init_tags=False)
        self._connection_opened = True
        self._forward_open = lambda: True
        self._forward_close = lambda: True
        self._cfg["use_instance_ids"] = False
        self.words = words
        self.requests = []
        self.return_words = None

    def _get_tag_info(self, base, attrs):
        base = base.split("[", 1)[0]
        attrs = [attr.split("[", 1)[0] for attr in attrs]
        key = ".".join([base, *attrs])
        return self.catalog[key]

    def send(self, request):
        requests = request.requests if request.type_ == "multi" else [request]
        responses = []
        for req in requests:
            self.requests.append((req.tag, req.elements))
            start = int(req.tag.rsplit("[", 1)[-1].rstrip("]")) if "[" in req.tag else 0
            count = req.elements
            if self.return_words is not None:
                count = max(0, min(count, self.return_words))
            value = []
            for word in self.words[start : start + count]:
                value.extend(bool(word >> bit & 1) for bit in range(32))
            responses.append(FakeResponse(req, value))

        if request.type_ == "multi":
            return FakeMultiResponse(responses)
        return responses[0]


def bool_bits(words):
    value = []
    for word in words:
        value.extend(bool(word >> bit & 1) for bit in range(32))
    return value


ARR = {
    "data_type": "DWORD",
    "data_type_name": "DWORD",
    "array": 3,
    "offset": 0,
    "tag_type": "atomic",
    "type_class": DWORD,
}
UDT = {
    "data_type": "DWORD",
    "data_type_name": "DWORD",
    "array": 2,
    "offset": 4,
    "tag_type": "atomic",
    "type_class": DWORD,
}


def make_driver(words=(0x00000005, 0x80000001, 0xA5A5A5A5)):
    driver = BoolDriver(words)
    driver.catalog = {"arr": ARR, "udt.bools": UDT}
    return driver


def test_single_bit_uses_only_its_word_and_bit():
    driver = make_driver()
    result = driver.read("arr[45]")

    assert driver.requests == [("arr[1]", 1)]
    assert result.value == bool_bits(driver.words)[45]
    assert result.type == "BOOL"


def test_range_spanning_word_boundary():
    driver = make_driver()
    result = driver.read("arr[30]{8}")

    assert driver.requests == [("arr[0]", 2)]
    assert result.value == bool_bits(driver.words)[30:38]
    assert result.type == "BOOL[8]"


def test_non_aligned_range_and_tail_do_not_include_neighbor_bits():
    driver = make_driver()
    result = driver.read("arr[1]{32}")

    # logical indexes 1..32, backed by words 0 and 1
    assert driver.requests == [("arr[0]", 2)]
    assert result.value == bool_bits(driver.words)[1:33]
    assert len(result.value) == 32


def test_request_beyond_array_end_only_returns_known_bits():
    driver = make_driver()
    result = driver.read("arr[95]{4}")

    bits = bool_bits(driver.words)
    assert driver.requests == [("arr[2]", 1)]
    assert result.value == [bits[95], None, None, None]


def test_request_entirely_outside_array_does_not_read():
    driver = make_driver()
    result = driver.read("arr[96]{4}")

    assert driver.requests == []
    assert result.value == [None, None, None, None]
    assert result.type == "BOOL[4]"


def test_missing_word_only_marks_dependent_bits_unknown():
    driver = make_driver()
    driver.return_words = 1
    result = driver.read("arr[30]{8}")

    bits = bool_bits(driver.words)
    # first 2 BOOLs come from the returned word, the other 6 depend on the missing word
    assert result.value == bits[30:32] + [None] * 6


def test_udt_bool_array_slice_is_located_by_member_offset():
    driver = make_driver()
    result = driver.read("udt.bools[33]{10}")

    # BOOL indexes 33..42 all live in the second word of the UDT member
    assert driver.requests == [("udt.bools[1]", 1)]
    assert result.value == bool_bits(driver.words)[33:43]


def test_catalog_bit_offset_participates_in_locating_bits():
    driver = make_driver()
    member = {
        "data_type": "DWORD",
        "data_type_name": "DWORD",
        "array": 1,
        "bit": 4,
        "tag_type": "atomic",
        "type_class": DWORD,
    }
    driver.catalog = {"bits": member}

    result = driver.read("bits{2}")

    assert driver.requests == [("bits", 1)]
    assert result.value == bool_bits(driver.words)[4:6]


def test_shared_read_range_keeps_each_requests_own_indexes():
    driver = make_driver()
    results = driver.read("arr[40]{8}", "arr[40]{4}", "arr[41]", "arr[45]")

    # all four requests read the same single word, only once
    assert driver.requests == [("arr[1]", 1)]

    bits = bool_bits(driver.words)
    assert results[0].value == bits[40:48]
    assert results[1].value == bits[40:44]
    assert results[2].value == bits[41]
    assert results[3].value == bits[45]
    assert [result.tag for result in results] == [
        "arr[40]", "arr[40]", "arr[41]", "arr[45]",
    ]


def test_single_request_reads_only_the_needed_words():
    driver = make_driver()
    result = driver.read("arr[35]{2}")

    assert driver.requests == [("arr[1]", 1)]
    assert result.value == bool_bits(driver.words)[35:37]


def test_multi_word_range_that_starts_and_ends_mid_word():
    driver = make_driver()
    result = driver.read("arr[5]{60}")

    # logical indexes 5..64 are backed by words 0, 1, and 2
    assert driver.requests == [("arr[0]", 3)]
    assert result.value == bool_bits(driver.words)[5:65]
    assert len(result.value) == 60


def test_plain_dword_read_is_not_treated_as_bool_array():
    driver = make_driver()
    member = {
        "data_type": "DWORD",
        "data_type_name": "DWORD",
        "array": 0,
        "tag_type": "atomic",
        "type_class": DWORD,
    }
    driver.catalog = {"word": member}

    result = driver.read("word")

    assert driver.requests == [("word", 1)]
    assert result.value == bool_bits(driver.words)[:32]
    assert result.type == "BOOL[32]"
