"""Offline tests for Multiple Service Packet read packing and capacity accounting."""
import random
from unittest import mock

import pytest

from pycomm3.cip import DataTypes, UINT, DINT
from pycomm3.logix_driver import (
    LogixDriver,
    _embedded_read_reply_size,
    _pack_multi_service,
    _tag_value_upper_size,
)
from pycomm3.packets import (
    MultiServiceRequestPacket,
    MultiServiceResponsePacket,
    ReadTagFragmentedRequestPacket,
    ReadTagRequestPacket,
)
from pycomm3.util import cycle

CONNECT_PATH = "192.168.1.100/1"
_SEQUENCE = cycle(65535, start=1)


def atomic_tag(name, dtype, instance_id, elements=1):
    type_class = DataTypes[dtype]
    tag_info = {
        "tag_name": name,
        "tag_type": "atomic",
        "data_type": dtype,
        "data_type_name": dtype,
        "type_class": type_class,
        "instance_id": instance_id,
    }
    return {
        "request_id": instance_id,
        "request_tag": name,
        "user_tag": name,
        "plc_tag": name,
        "bit": None,
        "bool_elements": None,
        "elements": elements,
        "tag_info": tag_info,
    }


def struct_tag(name, instance_id, structure_size, elements=1):
    tag = atomic_tag(name, "DINT", instance_id, elements)
    tag["tag_info"] = {
        "tag_name": name,
        "tag_type": "struct",
        "data_type_name": "MYSTRUCT",
        "instance_id": instance_id,
        "data_type": {
            "template": {
                "structure_size": structure_size,
                "structure_handle": 0x1234,
            }
        },
        "type_class": None,
    }
    return tag


def build_read_request(tag_data):
    request = ReadTagRequestPacket(
        _SEQUENCE,
        tag_data["plc_tag"],
        tag_data["elements"],
        tag_data["tag_info"],
        tag_data["request_id"],
        True,
    )
    request.build_message()
    return request


def make_driver(connection_size=4000):
    driver = LogixDriver(CONNECT_PATH, init_tags=False, init_program_tags=False)
    driver._cfg["connection_size"] = connection_size
    driver._micro800 = False
    return driver


def synthetic_multi_reply(multi_request):
    """Build a wire-shaped Multiple Service reply carrying distinct DINT values."""
    sub_replies = []
    for req in multi_request.requests:
        value_size = _tag_value_upper_size(
            {"tag_info": req.tag_info, "elements": req.elements}
        )
        if req.tag_info["data_type_name"] == "DINT":
            value = DINT.encode(req.request_id)
        else:
            value = b"\x00" * value_size
        type_code = UINT.encode(DataTypes[req.tag_info["data_type_name"]].code)
        sub_replies.append(b"\xcc\x00\x00\x00" + type_code + value)

    count = len(sub_replies)
    offset = 2 + 2 * count
    offsets = []
    for sub in sub_replies:
        offsets.append(UINT.encode(offset))
        offset += len(sub)
    multi_data = UINT.encode(count) + b"".join(offsets) + b"".join(sub_replies)
    return MultiServiceResponsePacket(multi_request, bytes(46) + b"\xcc\x00\x00\x00" + multi_data)


def optimal_bin_count(entries, capacity):
    """Exhaustive minimum number of bins honoring the dual request/reply limits."""
    items = [
        (len(req.message), 2 + reply_size) for req, reply_size in entries
    ]
    n = len(items)
    best = [n]

    def recurse(idx, bins):
        if len(bins) >= best[0]:
            return
        if idx == n:
            best[0] = len(bins)
            return
        req_w, reply_w = items[idx]
        seen = set()
        for b in range(len(bins)):
            r_req, r_reply = bins[b]
            signature = (r_req, r_reply)
            if signature in seen:
                continue
            seen.add(signature)
            if (
                10 + r_req + req_w <= capacity
                and 2 + r_reply + reply_w <= capacity
            ):
                bins[b] = (r_req + req_w, r_reply + reply_w)
                recurse(idx + 1, bins)
                bins[b] = (r_req, r_reply)
        bins.append((req_w, reply_w))
        recurse(idx + 1, bins)
        bins.pop()

    recurse(0, [])
    return best[0]


def test_embedded_reply_size_known_and_unknown():
    dint = atomic_tag("d", "DINT", 1)
    # framing(4) + type prefix(2) + 4 value bytes
    assert _embedded_read_reply_size(dint) == 10
    assert _tag_value_upper_size(dint) == 4

    sint_array = atomic_tag("s", "SINT", 2, elements=7)
    assert _tag_value_upper_size(sint_array) == 7
    assert _embedded_read_reply_size(sint_array) == 4 + 2 + 7

    struct = struct_tag("u", 3, structure_size=88)
    # struct replies carry a 4-byte type/handle prefix
    assert _embedded_read_reply_size(struct) == 4 + 4 + 88

    unknown = atomic_tag("x", "DINT", 4)
    unknown["tag_info"] = dict(unknown["tag_info"])
    unknown["tag_info"]["tag_type"] = "struct"
    unknown["tag_info"]["data_type"] = {"template": {}}
    assert _tag_value_upper_size(unknown) is None
    assert _embedded_read_reply_size(unknown) is None


@pytest.mark.parametrize(
    "tags, capacity",
    [
        ([atomic_tag(f"t{i}", "DINT", i) for i in range(12)], 4000),
        ([atomic_tag(f"t{i}", "DINT", i) for i in range(20)], 60),
        ([atomic_tag(f"t{i}", "SINT", i, elements=(i % 5) + 1) for i in range(10)], 80),
        (
            [atomic_tag("a", "DINT", 1), atomic_tag("b", "SINT", 2, elements=9),
             atomic_tag("c", "REAL", 3, elements=3), atomic_tag("d", "LINT", 4)],
            120,
        ),
    ],
)
def test_pack_respects_both_limits_and_covers_every_tag(tags, capacity):
    entries = []
    for tag in tags:
        request = build_read_request(tag)
        entries.append((request, _embedded_read_reply_size(tag)))

    groups, ledger = _pack_multi_service(entries, capacity)

    seen_ids = []
    for group, entry in zip(groups, ledger):
        assert entry["capacity"] == capacity
        assert entry["request_bytes"] <= capacity
        assert entry["reply_bytes"] <= capacity
        # the request ledger must equal the bytes of the actual wire message
        wire = MultiServiceRequestPacket(_SEQUENCE, group).build_message()
        assert len(wire) == entry["request_bytes"]
        seen_ids.extend(req.request_id for req in group)

    assert sorted(seen_ids) == sorted(tag["request_id"] for tag in tags)
    assert len(seen_ids) == len(set(seen_ids)) == len(tags)


@pytest.mark.parametrize(
    "tags, capacity",
    [
        ([atomic_tag(f"t{i}", "DINT", i) for i in range(12)], 4000),
        ([atomic_tag(f"t{i}", "DINT", i) for i in range(20)], 60),
        ([atomic_tag(f"t{i}", "SINT", i, elements=(i % 5) + 1) for i in range(10)], 80),
    ],
)
def test_pack_reaches_exhaustive_optimum_on_small_instances(tags, capacity):
    entries = [
        (build_read_request(tag), _embedded_read_reply_size(tag)) for tag in tags
    ]
    groups, _ = _pack_multi_service(entries, capacity)
    assert len(groups) == optimal_bin_count(entries, capacity)


def test_pack_never_more_than_one_packet_above_optimum():
    rng = random.Random(2026)
    for _ in range(120):
        capacity = rng.choice([60, 100, 200, 400])
        tags = []
        for i in range(rng.randint(1, 10)):
            tags.append(
                atomic_tag(f"t{i}", rng.choice(["SINT", "INT", "DINT", "LINT"]), i,
                           elements=rng.randint(1, 6))
            )
        entries = [
            (build_read_request(tag), _embedded_read_reply_size(tag)) for tag in tags
        ]
        # drop nothing: every single read here fits, mirroring the driver's filter
        entries = [
            (req, rep) for req, rep in entries
            if 10 + len(req.message) <= capacity and 2 + 2 + rep <= capacity
        ]
        if not entries:
            continue
        groups, ledger = _pack_multi_service(entries, capacity)
        assert len(groups) <= optimal_bin_count(entries, capacity) + 1
        for entry in ledger:
            assert entry["request_bytes"] <= capacity
            assert entry["reply_bytes"] <= capacity


def test_pack_homogeneous_reads_hit_theoretical_lower_bound():
    # equally sized reads are the dominant batch case and must pack optimally
    capacity = 4000
    tags = [atomic_tag(f"tag{i}", "DINT", i) for i in range(300)]
    entries = [
        (build_read_request(tag), _embedded_read_reply_size(tag)) for tag in tags
    ]
    groups, ledger = _pack_multi_service(entries, capacity)

    lower_bound = max(
        -(-sum(len(req.message) for req, _ in entries) // (capacity - 10)),
        -(-sum(2 + rep for _, rep in entries) // (capacity - 2)),
    )
    assert len(groups) == lower_bound
    for entry in ledger:
        assert entry["request_bytes"] <= capacity
        assert entry["reply_bytes"] <= capacity


def test_build_multi_reads_merges_small_tags_into_minimal_packets():
    tags = [atomic_tag(f"tag{i}", "DINT", 100 + i) for i in range(60)]
    parsed = {i: tag for i, tag in enumerate(tags)}
    for tag, idx in zip(tags, parsed):
        tag["request_id"] = idx

    driver = make_driver(4000)
    requests = driver._read_build_multi_requests(parsed)

    multis = [r for r in requests if isinstance(r, MultiServiceRequestPacket)]
    assert len(multis) == 1
    multi = multis[0]
    assert [r.request_id for r in multi.requests] == list(range(60))

    ledger = driver.read_pack_ledger[0]
    assert ledger["request_bytes"] == len(multi.build_message())
    assert ledger["request_bytes"] <= 4000
    assert ledger["reply_bytes"] <= 4000


def test_ledger_matches_actual_reply_and_subresults_keep_identity():
    tags = [atomic_tag(f"tag{i}", "DINT", i) for i in range(16)]
    parsed = {i: tag for i, tag in enumerate(tags)}
    for tag, idx in zip(tags, parsed):
        tag["request_id"] = idx

    driver = make_driver(70)
    requests = driver._read_build_multi_requests(parsed)
    multis = [r for r in requests if isinstance(r, MultiServiceRequestPacket)]
    assert len(multis) > 1

    all_responses = {}
    for multi, entry in zip(multis, driver.read_pack_ledger):
        reply = synthetic_multi_reply(multi)
        assert reply.is_valid(), reply.error
        # the real reply must never exceed the planned upper bound
        assert len(reply.data) <= entry["reply_bytes"]
        for sub in reply.responses:
            assert sub.is_valid(), sub.error
            all_responses[sub.request.request_id] = sub.value

    # regrouping must not misattribute a value to another tag
    assert sorted(all_responses) == list(range(16))
    for request_id, value in all_responses.items():
        assert value == request_id


def test_oversized_tag_is_fragmented_and_kept_separate():
    big = struct_tag("big", 9, structure_size=5000)
    tags = [atomic_tag("a", "DINT", 1), big, atomic_tag("b", "SINT", 2)]
    parsed = {i: tag for i, tag in enumerate(tags)}
    for tag, idx in zip(tags, parsed):
        tag["request_id"] = idx

    driver = make_driver(4000)
    requests = driver._read_build_multi_requests(parsed)

    fragments = [r for r in requests if isinstance(r, ReadTagFragmentedRequestPacket)]
    multis = [r for r in requests if isinstance(r, MultiServiceRequestPacket)]

    assert len(fragments) == 1
    assert fragments[0].request_id == 1
    assert [r.request_id for r in multis[0].requests] == [0, 2]


def test_unknown_reply_bound_is_never_assumed_zero():
    unknown = atomic_tag("u", "DINT", 5)
    unknown["tag_info"] = dict(unknown["tag_info"])
    unknown["tag_info"]["tag_type"] = "struct"
    unknown["tag_info"]["data_type"] = {"template": {}}

    tags = [atomic_tag("x", "DINT", 1), unknown]
    parsed = {i: tag for i, tag in enumerate(tags)}
    for tag, idx in zip(tags, parsed):
        tag["request_id"] = idx

    driver = make_driver(4000)
    requests = driver._read_build_multi_requests(parsed)

    fragments = [r for r in requests if isinstance(r, ReadTagFragmentedRequestPacket)]
    multis = [r for r in requests if isinstance(r, MultiServiceRequestPacket)]

    assert len(fragments) == 1
    assert fragments[0].request_id == 1
    assert [r.request_id for r in multis[0].requests] == [0]


def test_ledger_is_reset_on_single_request_path():
    driver = make_driver(4000)
    tag = atomic_tag("only", "DINT", 1)
    driver._read_pack_ledger = [{"stale": True}]

    driver._read_build_requests({0: tag})
    assert driver.read_pack_ledger == []


def test_errored_tags_are_skipped_without_gaps_in_coverage():
    good = atomic_tag("good", "DINT", 1)
    errored = {
        "request_id": 1,
        "request_tag": "bad",
        "error": "Tag doesn't exist",
    }
    parsed = {0: good, 1: errored}

    driver = make_driver(4000)
    requests = driver._read_build_multi_requests(parsed)
    multis = [r for r in requests if isinstance(r, MultiServiceRequestPacket)]

    assert len(multis) == 1
    assert [r.request_id for r in multis[0].requests] == [0]
    assert driver.read_pack_ledger[0]["request_ids"] == [0]


def _identity_multi_reply(multi_request):
    """Build a reply whose every sub-result value equals its own request id."""
    sub_replies = []
    for req in multi_request.requests:
        type_class = req.tag_info["type_class"]
        type_code = UINT.encode(DataTypes[req.tag_info["data_type_name"]].code)
        value = type_class.encode(req.request_id)
        sub_replies.append(b"\xcc\x00\x00\x00" + type_code + value)

    count = len(sub_replies)
    offset = 2 + 2 * count
    offsets = []
    for sub in sub_replies:
        offsets.append(UINT.encode(offset))
        offset += len(sub)
    multi_data = UINT.encode(count) + b"".join(offsets) + b"".join(sub_replies)
    return MultiServiceResponsePacket(multi_request, bytes(46) + b"\xcc\x00\x00\x00" + multi_data)


def test_public_read_preserves_caller_order_and_tag_identity_across_packets():
    # mixed reply sizes force Best-Fit Decreasing to reorder the embedded requests
    type_names = ["SINT", "DINT", "LINT", "INT", "REAL", "DINT", "SINT", "LINT"]
    tag_defs = [atomic_tag(f"tag{i}", type_names[i % len(type_names)], i) for i in range(16)]
    tag_names = [tag["request_tag"] for tag in tag_defs]

    driver = make_driver(90)
    driver._target_is_connected = True
    driver._tags = {tag["request_tag"]: tag["tag_info"] for tag in tag_defs}

    built = {}

    def fake_send(request):
        built[len(built)] = request
        return _identity_multi_reply(request)

    with mock.patch.object(LogixDriver, "send", side_effect=fake_send):
        results = driver.read(*tag_names)

    multis = [r for r in built.values() if isinstance(r, MultiServiceRequestPacket)]
    assert len(multis) > 1  # the tight capacity really did split the batch

    # the embedded order must differ somewhere, proving regrouping happened
    embedded_order = [r.request_id for multi in multis for r in multi.requests]
    assert embedded_order != list(range(16))
    assert sorted(embedded_order) == list(range(16))

    # yet the public results stay in caller order and never misattribute values
    assert len(results) == 16
    assert [result.tag for result in results] == tag_names
    for index, result in enumerate(results):
        assert result.error is None
        assert result.value == index
