# -*- coding: utf-8 -*-

__all__ = [
    "GroupConsistency",
    "SampleCounter",
    "GroupReadResult",
    "ReadPlan",
    "evaluate_consistency",
]

from enum import Enum
from math import isclose
from time import monotonic
from typing import Any, List, NamedTuple, Optional

from .tag import Tag


class GroupConsistency(str, Enum):
    """
    Verification status for a group of samples that are required to belong to the
    same controller scan cycle.
    """

    CONSISTENT = "consistent"  #: proven to come from a single scan cycle
    CONFLICTING = "conflicting"  #: proven to span a scan cycle boundary
    UNPROVABLE = "unprovable"  #: neither proven consistent nor proven conflicting


class SampleCounter(NamedTuple):
    """
    Input convention describing the controller-provided sample counter.

    The controller increments ``tag`` once per scan cycle and never faster than
    ``max_rate`` counts per second.  ``modulus`` is the value at which the counter
    wraps back to zero (e.g. ``2 ** 32`` for a free-running 32-bit counter).
    """

    tag: str  #: name of the counter tag, incremented once per scan cycle
    modulus: int  #: value at which the counter wraps back to zero
    max_rate: float  #: maximum advance rate of the counter, in counts per second


class GroupReadResult(NamedTuple):
    """
    Result of a :class:`ReadPlan` group read.

    ``results`` holds the individual ``Tag`` observations.  They may only be
    treated as a same-scan-cycle group when ``status`` is
    ``GroupConsistency.CONSISTENT``; otherwise they are independent observations.
    """

    status: GroupConsistency  #: consistency status of the group
    results: List[Tag]  #: individual tag observations
    counter_before: Optional[int] = None  #: counter value sampled before the group
    counter_after: Optional[int] = None  #: counter value sampled after the group
    window: Optional[float] = None  #: seconds between bracketing the group read

    def __bool__(self):
        """``True`` only if the group is proven scan-cycle consistent."""
        return self.status is GroupConsistency.CONSISTENT


def evaluate_consistency(
    counter_before: int,
    counter_after: int,
    window: float,
    modulus: int,
    max_rate: float,
) -> GroupConsistency:
    """
    Decide the consistency status of a group read from the controller evidence.

    Different counter values prove the scan advanced during the read
    (``CONFLICTING``).  Equal counter values only prove consistency if the counter
    could not have wrapped within ``window`` seconds, i.e. the counter cannot
    advance a full ``modulus`` at ``max_rate`` inside the window.  Equal values
    with a window large enough to span a wraparound are ``UNPROVABLE``.
    """
    if counter_before != counter_after:
        return GroupConsistency.CONFLICTING
    if modulus <= 0 or max_rate <= 0:
        return GroupConsistency.UNPROVABLE
    max_advance = window * max_rate
    if max_advance < modulus and not isclose(max_advance, modulus):
        return GroupConsistency.CONSISTENT
    return GroupConsistency.UNPROVABLE


class ReadPlan:
    """
    A read plan that verifies whether a group of tags belongs to the same
    controller scan cycle.

    Each group read is bracketed by samples of the controller-provided
    :class:`SampleCounter`: the counter is read, the whole group is read, and the
    counter is read again.  Because the entire group read happens between the two
    counter samples, every request it is split into (multi-service packets and
    fragmented reads alike) falls within the same proof window.  Note that a
    multi-service packet by itself is **not** an atomic snapshot; only the counter
    evidence can certify same-scan consistency.

    Plans without a counter convention (or sources that cannot provide one) still
    deliver the tag values, but always as independent observations with status
    ``UNPROVABLE`` — same-scan values are never fabricated.
    """

    def __init__(self, driver: Any, counter: Optional[SampleCounter] = None):
        self.driver = driver
        self.counter = counter

    def read_group(
        self, *tags: str, counter: Optional[SampleCounter] = None
    ) -> GroupReadResult:
        """
        Read ``tags`` as a group and verify the same-scan-cycle promise.

        :param tags: one or more tags to read as a group
        :param counter: optional :class:`SampleCounter` overriding the plan's convention
        :return: a :class:`GroupReadResult` with the observations and their status
        """
        if not tags:
            raise ValueError("read_group requires at least one tag")

        convention = counter if counter is not None else self.counter
        if convention is None:
            # no controller evidence available: deliver independent observations
            return GroupReadResult(
                status=GroupConsistency.UNPROVABLE,
                results=_as_list(self.driver.read(*tags)),
            )

        start = monotonic()
        counter_before = self._read_counter(convention)
        results = _as_list(self.driver.read(*tags))
        counter_after = self._read_counter(convention)
        window = monotonic() - start

        if counter_before is None or counter_after is None:
            # counter evidence missing: deliver independent observations
            return GroupReadResult(
                status=GroupConsistency.UNPROVABLE,
                results=results,
                counter_before=counter_before,
                counter_after=counter_after,
                window=window,
            )

        status = evaluate_consistency(
            counter_before,
            counter_after,
            window,
            convention.modulus,
            convention.max_rate,
        )
        return GroupReadResult(
            status=status,
            results=results,
            counter_before=counter_before,
            counter_after=counter_after,
            window=window,
        )

    def _read_counter(self, counter: SampleCounter) -> Optional[int]:
        result = self.driver.read(counter.tag)
        if not result or not isinstance(result.value, int):
            return None
        return result.value


def _as_list(results: Any) -> List[Tag]:
    if isinstance(results, Tag):
        return [results]
    return list(results)
