"""Offline tests for the read plan (scan-cycle consistency verification)."""
from unittest import mock

import pytest

from pycomm3 import (
    GroupConsistency,
    GroupReadResult,
    ReadPlan,
    SampleCounter,
    Tag,
    evaluate_consistency,
)
from pycomm3.logix_driver import LogixDriver

CONNECT_PATH = "192.168.1.100/1"
COUNTER_TAG = "ScanCounter"
COUNTER = SampleCounter(tag=COUNTER_TAG, modulus=2 ** 32, max_rate=1000.0)


class FakeDriver:
    """Minimal driver stub serving counter values from a queue and tag values from a dict."""

    def __init__(self, counter_values=(), values=None):
        self._counter_values = iter(counter_values)
        self._values = values or {}
        self.events = []

    def read(self, *tags):
        if len(tags) == 1 and tags[0] == COUNTER_TAG:
            value = next(self._counter_values)
            self.events.append(("counter", value))
            if value is None:
                return Tag(COUNTER_TAG, None, None, "counter read failed")
            return Tag(COUNTER_TAG, value, "DINT", None)
        self.events.append(("group", tags))
        results = [Tag(tag, self._values.get(tag), "DINT", None) for tag in tags]
        return results[0] if len(results) == 1 else results


class FragmentingDriver(FakeDriver):
    """Driver stub whose group read is split into per-tag fragments."""

    def read(self, *tags):
        if len(tags) == 1 and tags[0] == COUNTER_TAG:
            return super().read(*tags)
        for tag in tags:
            self.events.append(("fragment", tag))
        results = [Tag(tag, self._values.get(tag), "DINT", None) for tag in tags]
        return results[0] if len(results) == 1 else results


def test_consistent_when_counters_equal_and_window_cannot_wrap():
    driver = FakeDriver(counter_values=[7, 7], values={"a": 1, "b": 2})
    plan = ReadPlan(driver, COUNTER)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[10.0, 10.05]):
        result = plan.read_group("a", "b")

    assert result.status is GroupConsistency.CONSISTENT
    assert result  # truthy only when consistent
    assert result.counter_before == 7
    assert result.counter_after == 7
    assert result.window == pytest.approx(0.05)
    assert [tag.value for tag in result.results] == [1, 2]


def test_conflicting_when_counter_advances():
    driver = FakeDriver(counter_values=[7, 8], values={"a": 1, "b": 2})
    plan = ReadPlan(driver, COUNTER)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[10.0, 10.01]):
        result = plan.read_group("a", "b")

    assert result.status is GroupConsistency.CONFLICTING
    assert not result
    assert [tag.value for tag in result.results] == [1, 2]


def test_unprovable_when_equal_counters_but_window_spans_wraparound():
    counter = SampleCounter(tag=COUNTER_TAG, modulus=100, max_rate=1000.0)
    driver = FakeDriver(counter_values=[7, 7], values={"a": 1})
    plan = ReadPlan(driver, counter)
    # window * max_rate == modulus: a full wraparound fits inside the window
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[10.0, 10.1]):
        result = plan.read_group("a")

    assert result.status is GroupConsistency.UNPROVABLE
    assert result.counter_before == result.counter_after == 7


def test_consistent_when_window_just_below_wraparound():
    counter = SampleCounter(tag=COUNTER_TAG, modulus=100, max_rate=1000.0)
    driver = FakeDriver(counter_values=[7, 7], values={"a": 1})
    plan = ReadPlan(driver, counter)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[10.0, 10.09]):
        result = plan.read_group("a")

    assert result.status is GroupConsistency.CONSISTENT


def test_unprovable_without_counter_convention_delivers_independent_observations():
    driver = FakeDriver(values={"a": 1, "b": 2})
    plan = ReadPlan(driver)
    result = plan.read_group("a", "b")

    assert result.status is GroupConsistency.UNPROVABLE
    assert result.counter_before is None
    assert result.counter_after is None
    assert result.window is None
    assert [tag.value for tag in result.results] == [1, 2]
    # no counter evidence was ever requested from the source
    assert all(event[0] != "counter" for event in driver.events)


def test_unprovable_when_counter_read_fails():
    driver = FakeDriver(counter_values=[None, 7], values={"a": 1})
    plan = ReadPlan(driver, COUNTER)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[10.0, 10.01]):
        result = plan.read_group("a")

    assert result.status is GroupConsistency.UNPROVABLE
    assert result.counter_before is None
    assert [tag.value for tag in result.results] == [1]


def test_fragments_fall_within_common_proof_window():
    driver = FragmentingDriver(counter_values=[3, 3], values={"a": 1, "b": 2, "c": 3})
    plan = ReadPlan(driver, COUNTER)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[5.0, 5.02]):
        result = plan.read_group("a", "b", "c")

    assert result.status is GroupConsistency.CONSISTENT
    event_names = [event[0] for event in driver.events]
    assert event_names[0] == "counter"
    assert event_names[-1] == "counter"
    assert event_names[1:-1] == ["fragment"] * 3


def test_read_group_requires_at_least_one_tag():
    plan = ReadPlan(FakeDriver(), COUNTER)
    with pytest.raises(ValueError):
        plan.read_group()


def test_counter_convention_overridable_per_call():
    driver = FakeDriver(counter_values=[4, 4], values={"a": 1})
    plan = ReadPlan(driver)
    with mock.patch("pycomm3.read_plan.monotonic", side_effect=[1.0, 1.01]):
        result = plan.read_group("a", counter=COUNTER)

    assert result.status is GroupConsistency.CONSISTENT


@pytest.mark.parametrize(
    "before, after, window, modulus, max_rate, expected",
    [
        (1, 2, 0.01, 2 ** 32, 1000.0, GroupConsistency.CONFLICTING),
        (5, 5, 0.01, 2 ** 32, 1000.0, GroupConsistency.CONSISTENT),
        (5, 5, 0.1, 100, 1000.0, GroupConsistency.UNPROVABLE),  # exact wrap window
        (5, 5, 0.2, 100, 1000.0, GroupConsistency.UNPROVABLE),
        (5, 5, 0.01, 0, 1000.0, GroupConsistency.UNPROVABLE),  # no modulus
        (5, 5, 0.01, 2 ** 32, 0.0, GroupConsistency.UNPROVABLE),  # no rate bound
    ],
)
def test_evaluate_consistency(before, after, window, modulus, max_rate, expected):
    assert evaluate_consistency(before, after, window, modulus, max_rate) is expected


def test_group_read_result_truthiness():
    assert GroupReadResult(GroupConsistency.CONSISTENT, [])
    assert not GroupReadResult(GroupConsistency.CONFLICTING, [])
    assert not GroupReadResult(GroupConsistency.UNPROVABLE, [])


def test_logix_driver_read_plan_factory():
    driver = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)

    plan = driver.read_plan(COUNTER)
    assert isinstance(plan, ReadPlan)
    assert plan.driver is driver
    assert plan.counter is COUNTER

    default_plan = driver.read_plan()
    assert default_plan.counter is None
