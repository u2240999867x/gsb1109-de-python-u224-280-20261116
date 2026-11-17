# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""Runspace Pool capacity tracking.

The Runspace Pool min/max counts are adjusted at runtime with
``SetMinRunspaces``/``SetMaxRunspaces`` requests. The response that confirms or
rejects a request can arrive after other requests have already been sent, so the
confirmed values cannot be updated as soon as a request is queued. This module
keeps the confirmed values, the still unconfirmed requests, and the requests
that were acknowledged but cannot be adopted yet (because min and max would be
invalid together) separate from each other.
"""

import dataclasses
import typing
import uuid

_MIN = "min"
_MAX = "max"
_FIELDS = (_MIN, _MAX)


@dataclasses.dataclass(frozen=True)
class RunspacePoolChange:
    """A pending or deferred change to a Runspace Pool limit.

    Attributes:
        field: Either ``min`` or ``max``.
        value: The requested runspace count.
        ci: The call id that identifies the request.
        sequence: The local order the request was sent in. Used to ignore an
            older acknowledgement that arrives after a newer one.
        state: ``pending`` when the response has not been received yet or
            ``deferred`` when the request was acknowledged but could not be
            adopted because the resulting min/max pair was invalid.
    """

    field: str
    value: int
    ci: int
    sequence: int
    state: str = "pending"

    @property
    def is_min(self) -> bool:
        """Whether this change targets the minimum runspace count."""
        return self.field == _MIN

    @property
    def is_max(self) -> bool:
        """Whether this change targets the maximum runspace count."""
        return self.field == _MAX

    @property
    def is_pending(self) -> bool:
        """Whether the peer has not responded to this change yet."""
        return self.state == "pending"

    @property
    def is_deferred(self) -> bool:
        """Whether this change was acknowledged but has not been adopted."""
        return self.state == "deferred"


@dataclasses.dataclass(frozen=True)
class RunspacePoolCapacity:
    """Observable snapshot of a Runspace Pool's capacity.

    The snapshot separates the values that are actually confirmed by the peer
    from the requests that are still waiting for a response and from the
    pipelines that have been admitted into the pool.

    Attributes:
        runspace_pool_id: The Runspace Pool the snapshot was taken from.
        min_runspaces: The confirmed minimum runspace count.
        max_runspaces: The confirmed maximum runspace count.
        pending_changes: Requests that have not been acknowledged yet.
        deferred_changes: Requests that were acknowledged but are not adopted
            because their min/max pair is not valid yet.
        admitted_pipeline_ids: The pipelines that currently occupy the pool.
    """

    runspace_pool_id: uuid.UUID
    min_runspaces: int
    max_runspaces: int
    pending_changes: typing.Tuple[RunspacePoolChange, ...] = ()
    deferred_changes: typing.Tuple[RunspacePoolChange, ...] = ()
    admitted_pipeline_ids: typing.Tuple[uuid.UUID, ...] = ()

    @property
    def confirmed_min_runspaces(self) -> int:
        """The minimum runspace count that has been confirmed."""
        return self.min_runspaces

    @property
    def confirmed_max_runspaces(self) -> int:
        """The maximum runspace count that has been confirmed."""
        return self.max_runspaces

    @property
    def pending_min_runspaces(self) -> typing.Optional[int]:
        """The latest minimum count still waiting for a response."""
        return self._latest_value(self.pending_changes, _MIN)

    @property
    def pending_max_runspaces(self) -> typing.Optional[int]:
        """The latest maximum count still waiting for a response."""
        return self._latest_value(self.pending_changes, _MAX)

    @property
    def deferred_min_runspaces(self) -> typing.Optional[int]:
        """The latest acknowledged, not adopted minimum count."""
        return self._latest_value(self.deferred_changes, _MIN)

    @property
    def deferred_max_runspaces(self) -> typing.Optional[int]:
        """The latest acknowledged, not adopted maximum count."""
        return self._latest_value(self.deferred_changes, _MAX)

    @property
    def admitted_pipeline_count(self) -> int:
        """The number of pipelines currently occupying the pool."""
        return len(self.admitted_pipeline_ids)

    @property
    def admitted_pipelines(self) -> typing.Tuple[uuid.UUID, ...]:
        """The identifiers of the pipelines currently occupying the pool."""
        return self.admitted_pipeline_ids

    @property
    def confirmed_limits(self) -> typing.Tuple[int, int]:
        """The confirmed ``(min, max)`` runspace pair."""
        return self.min_runspaces, self.max_runspaces

    @property
    def pending_limits(self) -> typing.Tuple[typing.Optional[int], typing.Optional[int]]:
        """The latest ``(min, max)`` values still waiting for a response."""
        return self.pending_min_runspaces, self.pending_max_runspaces

    @property
    def unresolved_changes(self) -> typing.Tuple[RunspacePoolChange, ...]:
        """Changes that are not reflected in the confirmed values yet.

        This is the combination of requests that have not been answered and
        requests that were acknowledged but are not adopted because the
        min/max pair is not legal yet.
        """
        return tuple(sorted(self.pending_changes + self.deferred_changes, key=lambda c: c.sequence))

    @property
    def available_runspaces(self) -> int:
        """The number of further pipelines the confirmed maximum allows."""
        return max(0, self.max_runspaces - self.admitted_pipeline_count)

    @property
    def has_pending_changes(self) -> bool:
        """Whether any request is waiting for or blocked on a response."""
        return bool(self.pending_changes or self.deferred_changes)

    @staticmethod
    def _latest_value(
        changes: typing.Tuple[RunspacePoolChange, ...],
        field: str,
    ) -> typing.Optional[int]:
        matching = [c for c in changes if c.field == field]
        return matching[-1].value if matching else None


class _RunspaceCapacityTracker:
    """Tracks confirmed limits and outstanding Runspace Pool changes."""

    def __init__(
        self,
        min_runspaces: int = 0,
        max_runspaces: int = 0,
    ) -> None:
        self.min_runspaces = min_runspaces
        self.max_runspaces = max_runspaces
        self.epoch = 0
        self._sequence = 0
        self._pending: typing.Dict[int, RunspacePoolChange] = {}
        self._deferred: typing.List[RunspacePoolChange] = []
        self._applied_sequence = {_MIN: 0, _MAX: 0}

    def set_confirmed(
        self,
        min_runspaces: typing.Optional[int] = None,
        max_runspaces: typing.Optional[int] = None,
    ) -> None:
        """Sets confirmed values and drops outstanding requests for them."""
        dropped = []
        if min_runspaces is not None:
            self.min_runspaces = min_runspaces
            dropped.append(_MIN)
        if max_runspaces is not None:
            self.max_runspaces = max_runspaces
            dropped.append(_MAX)

        for field in dropped:
            self._applied_sequence[field] = 0
            self._pending = {ci: c for ci, c in self._pending.items() if c.field != field}
            self._deferred = [c for c in self._deferred if c.field != field]

    def invalidate(self) -> None:
        """Drops outstanding requests without changing confirmed values.

        This is used when the connection or the pool identity changes so a
        response for the previous connection can no longer affect this pool.
        """
        self.epoch += 1
        self._pending.clear()
        self._deferred.clear()
        self._applied_sequence = {_MIN: 0, _MAX: 0}

    def issue(
        self,
        field: str,
        value: int,
        ci: int,
    ) -> RunspacePoolChange:
        """Records a change that is about to be sent to the peer."""
        self._sequence += 1
        change = RunspacePoolChange(field=field, value=value, ci=ci, sequence=self._sequence)
        self._pending[ci] = change
        return change

    def has_uncommitted(
        self,
        field: str,
    ) -> bool:
        """Whether a field has a change that has not been adopted yet."""
        return any(c.field == field for c in self._pending.values()) or any(
            c.field == field for c in self._deferred
        )

    def resolve(
        self,
        ci: int,
        success: bool,
    ) -> bool:
        """Resolves a change with the response from the peer.

        Returns ``True`` when the response matched an outstanding request. A
        response for an unknown call id (for example a late response from a
        previous pool) is ignored.
        """
        change = self._pending.pop(ci, None)
        if change is None:
            return False

        if success and change.sequence > self._applied_sequence[change.field]:
            self._deferred.append(dataclasses.replace(change, state="deferred"))
            self._adopt_ready_changes()

        return True

    def pending_changes(self) -> typing.Tuple[RunspacePoolChange, ...]:
        """The unacknowledged changes in the order they were sent."""
        return tuple(sorted(self._pending.values(), key=lambda c: c.sequence))

    def deferred_changes(self) -> typing.Tuple[RunspacePoolChange, ...]:
        """The acknowledged but unadopted changes in the order they were sent."""
        return tuple(sorted(self._deferred, key=lambda c: c.sequence))

    def adopt_ready_changes(self) -> None:
        """Adopts deferred changes when their pair is legal now."""
        self._adopt_ready_changes()

    def unresolved_changes(self) -> typing.Tuple[RunspacePoolChange, ...]:
        """All changes that are not reflected in the confirmed values yet."""
        return tuple(
            sorted(
                list(self._pending.values()) + self._deferred,
                key=lambda c: c.sequence,
            )
        )

    def _adopt_ready_changes(self) -> None:
        candidates: typing.Dict[str, RunspacePoolChange] = {}
        for field in _FIELDS:
            unapplied = [
                c for c in self._deferred if c.field == field and c.sequence > self._applied_sequence[field]
            ]
            if unapplied:
                candidates[field] = max(unapplied, key=lambda c: c.sequence)

        candidate_min = candidates[_MIN].value if _MIN in candidates else self.min_runspaces
        candidate_max = candidates[_MAX].value if _MAX in candidates else self.max_runspaces

        # Do not adopt either side until the resulting pair is valid. An
        # acknowledged min that is larger than the current max stays deferred
        # until a matching max has been confirmed.
        if candidate_min > candidate_max:
            return

        for field, change in candidates.items():
            if field == _MIN:
                self.min_runspaces = change.value
            else:
                self.max_runspaces = change.value
            self._applied_sequence[field] = change.sequence

        self._deferred = [
            c for c in self._deferred if c.sequence > self._applied_sequence[c.field]
        ]
