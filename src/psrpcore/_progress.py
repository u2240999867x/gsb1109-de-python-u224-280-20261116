# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""Progress activity tracking.

Remote progress records arrive interleaved and can describe more than one
activity tree at once.  This module keeps a current view of those records
without assuming that they arrive in a useful order.

Every activity is identified by its source, pipeline and activity number.  A
generation value can be supplied as evidence when an activity number is reused;
generations are kept separate and the newest comparable generation becomes the
current view.  Updates can also carry a sequence number so that a late, older
record cannot replace a newer one that has already been accepted.
"""

from __future__ import annotations

import dataclasses
import enum
import math
import typing
from collections.abc import Hashable, Mapping

from psrpcore.types import ProgressRecord, ProgressRecordType

_UNSET = object()

_ProgressKeyTuple = typing.Tuple[typing.Hashable, typing.Optional[typing.Hashable], int, typing.Any]


class ProgressStatus(enum.Enum):
    """The state of an activity in the current progress view.

    The three non-progressing states are deliberately distinct:

    * :attr:`NO_UPDATE` means the activity is known, usually because it is
      expected or was referenced as a parent, but no progress record for it has
      been received.
    * :attr:`UNKNOWN_PERCENT` means a progress record was received but it did
      not contain a usable percentage.
    * :attr:`COMPLETED` means the record explicitly carried the completed
      record type.  A completed parent does not change the status of its
      children.
    """

    NO_UPDATE = "no_update"
    UNKNOWN_PERCENT = "unknown_percent"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclasses.dataclass(frozen=True)
class ProgressKey:
    """Identity of one activity generation.

    Args:
        source: The source of the progress records, normally the Runspace Pool
            identifier.  It can be any hashable value.
        pipeline_id: The pipeline identifier, or ``None`` for pool-level
            progress.
        activity_id: The PowerShell ``ActivityId``.
        generation: Evidence that separates two uses of the same activity
            number.  Comparable values (for example integers or strings such as
            ``"run-1"`` and ``"run-2"``) allow the newest generation to become
            current automatically.  ``None`` means no generation evidence was
            supplied.
    """

    source: typing.Hashable
    pipeline_id: typing.Optional[typing.Hashable]
    activity_id: int
    generation: typing.Any = None

    @property
    def pipeline(self) -> typing.Optional[typing.Hashable]:
        """Alias for :attr:`pipeline_id`."""
        return self.pipeline_id

    def as_tuple(self) -> _ProgressKeyTuple:
        """Return the key as a plain tuple."""
        return (self.source, self.pipeline_id, self.activity_id, self.generation)


@dataclasses.dataclass(frozen=True)
class ProgressActivity:
    """The current view of a single activity generation."""

    key: ProgressKey
    activity: typing.Optional[str] = None
    status_description: typing.Optional[str] = None
    current_operation: typing.Optional[str] = None
    percent_complete: typing.Optional[int] = None
    parent_activity_id: typing.Optional[int] = None
    seconds_remaining: typing.Optional[int] = None
    weight: typing.Optional[float] = None
    received: bool = False
    completed: bool = False
    sequence: typing.Any = None
    record: typing.Optional[typing.Any] = dataclasses.field(default=None, repr=False, compare=False)

    @property
    def source(self) -> typing.Hashable:
        """The source this activity belongs to."""
        return self.key.source

    @property
    def pipeline_id(self) -> typing.Optional[typing.Hashable]:
        """The pipeline this activity belongs to."""
        return self.key.pipeline_id

    @property
    def activity_id(self) -> int:
        """The PowerShell activity number."""
        return self.key.activity_id

    @property
    def generation(self) -> typing.Any:
        """The generation evidence for this activity number."""
        return self.key.generation

    @property
    def parent_id(self) -> typing.Optional[int]:
        """Alias for :attr:`parent_activity_id`."""
        return self.parent_activity_id

    @property
    def has_update(self) -> bool:
        """Whether a progress record has been received for this activity."""
        return self.received

    @property
    def percent_known(self) -> bool:
        """Whether the record supplied a usable percentage."""
        return self.percent_complete is not None

    @property
    def status(self) -> ProgressStatus:
        """The distinct view state for this activity."""
        if self.completed:
            return ProgressStatus.COMPLETED
        if not self.received:
            return ProgressStatus.NO_UPDATE
        if self.percent_complete is None:
            return ProgressStatus.UNKNOWN_PERCENT
        return ProgressStatus.IN_PROGRESS

    @property
    def effective_percent(self) -> typing.Optional[float]:
        """The percentage used for aggregation.

        An explicitly completed activity without an explicit percentage is
        treated as 100 percent.  It still has no reported percentage in
        :attr:`percent_complete`.
        """
        if self.percent_complete is not None:
            return float(self.percent_complete)
        if self.completed:
            return 100.0
        return None

    @property
    def Activity(self) -> typing.Optional[str]:
        """Alias for :attr:`activity`."""
        return self.activity

    @property
    def ActivityId(self) -> int:
        """Alias for :attr:`activity_id`."""
        return self.activity_id

    @property
    def ParentActivityId(self) -> typing.Optional[int]:
        """Alias for :attr:`parent_activity_id`."""
        return self.parent_activity_id

    @property
    def PercentComplete(self) -> typing.Optional[int]:
        """Alias for :attr:`percent_complete`."""
        return self.percent_complete

    def children(self, tree: "ProgressTree") -> typing.Tuple["ProgressActivity", ...]:
        """Return direct children of this activity in the given tree."""
        return tree.children(self.activity_id)


@dataclasses.dataclass(frozen=True)
class ProgressCoverage:
    """Coverage information used to decide whether an overall ratio is valid."""

    complete: bool
    weighted_percent: typing.Optional[float]
    covered_weight: float
    activity_count: int
    no_update: typing.Tuple[ProgressKey, ...] = ()
    unknown_percent: typing.Tuple[ProgressKey, ...] = ()
    missing_weight: typing.Tuple[ProgressKey, ...] = ()
    missing_parents: typing.Tuple[ProgressKey, ...] = ()
    cycles: typing.Tuple[typing.Tuple[ProgressKey, ...], ...] = ()
    problems: typing.Tuple[str, ...] = ()

    @property
    def percent(self) -> typing.Optional[float]:
        """Alias for :attr:`weighted_percent`."""
        return self.weighted_percent

    @property
    def reason(self) -> typing.Optional[str]:
        """A short explanation when coverage is not complete."""
        if self.complete:
            return None
        return "; ".join(self.problems) if self.problems else "coverage is incomplete"

    @property
    def sufficient(self) -> bool:
        """Alias for :attr:`complete`."""
        return self.complete

    @property
    def ratio(self) -> typing.Optional[float]:
        """Alias for :attr:`weighted_percent`."""
        return self.weighted_percent


@dataclasses.dataclass(frozen=True)
class ProgressTree:
    """A current progress view for one source, pipeline and generation.

    ``roots`` contains activities that have no known parent and activities
    whose parent is missing.  Activities in a cycle are not roots; they remain
    available through :attr:`activities` and :attr:`cycles` so they can still be
    inspected independently.
    """

    source: typing.Hashable
    pipeline_id: typing.Optional[typing.Hashable]
    generation: typing.Any
    activities: typing.Tuple[ProgressActivity, ...]
    roots: typing.Tuple[ProgressActivity, ...]
    orphans: typing.Tuple[ProgressActivity, ...]
    cycles: typing.Tuple[typing.Tuple[ProgressActivity, ...], ...]
    unresolved: typing.Tuple[ProgressActivity, ...]
    coverage: ProgressCoverage
    _children: typing.Dict[int, typing.Tuple[ProgressActivity, ...]] = dataclasses.field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def percent(self) -> typing.Optional[float]:
        """The weighted overall percentage, or ``None`` if not covered."""
        return self.coverage.weighted_percent

    @property
    def complete(self) -> bool:
        """Whether an overall percentage can be calculated for this tree."""
        return self.coverage.complete

    @property
    def missing_parents(self) -> typing.Tuple[ProgressActivity, ...]:
        """Alias for :attr:`orphans`."""
        return self.orphans

    def children(self, activity_id: int) -> typing.Tuple[ProgressActivity, ...]:
        """Return the direct children of an activity number."""
        return self._children.get(activity_id, ())

    def get(self, activity_id: int) -> typing.Optional[ProgressActivity]:
        """Get an activity by number from this tree."""
        for activity in self.activities:
            if activity.activity_id == activity_id:
                return activity
        return None

    def __iter__(self) -> typing.Iterator[ProgressActivity]:
        return iter(self.activities)


class ProgressView:
    """Maintain a current view of interleaved progress records.

    The view is updated with :meth:`update` (or :meth:`extend` for a batch).
    Only the newest accepted record for each activity generation is retained.
    Older generations remain available through :meth:`get` and
    :attr:`all_activities`, but :attr:`activities` contains the current
    generation of each activity number.

    A record with a lower sequence number than the one already accepted for the
    same generation is ignored.  A completed activity is terminal for its
    generation, so later records for that same generation cannot reopen it;
    a reused activity number must use a different generation value.
    """

    def __init__(
        self,
        weights: typing.Optional[typing.Mapping[typing.Any, float]] = None,
    ) -> None:
        self._activities: typing.Dict[ProgressKey, ProgressActivity] = {}
        self._current: typing.Dict[
            typing.Tuple[typing.Hashable, typing.Optional[typing.Hashable], int], typing.Any
        ] = {}
        self._weights: typing.Dict[ProgressKey, float] = {}
        self._weights_by_id: typing.Dict[int, float] = {}

        if weights:
            for key, value in weights.items():
                if isinstance(key, int) and not isinstance(key, bool):
                    self._weights_by_id[int(key)] = self._validate_weight(value)
                    continue
                coerced = self._coerce_weight_key(key)
                if coerced is not None:
                    self._weights[coerced] = self._validate_weight(value)

    def __len__(self) -> int:
        return len(self.activities)

    def __iter__(self) -> typing.Iterator[ProgressActivity]:
        return iter(self.activities)

    def __contains__(self, key: typing.Any) -> bool:
        try:
            return self._resolve_key(key) in self._activities
        except (KeyError, TypeError, ValueError):
            return False

    def __getitem__(self, key: typing.Any) -> ProgressActivity:
        resolved = self._resolve_key(key)
        return self._activities[resolved]

    @property
    def activities(self) -> typing.Tuple[ProgressActivity, ...]:
        """The current generation of every activity, sorted for display."""
        current = []
        for base, generation in self._current.items():
            key = ProgressKey(base[0], base[1], base[2], generation)
            activity = self._activities.get(key)
            if activity is not None:
                current.append(activity)

        return tuple(sorted(current, key=self._display_sort_key))

    @property
    def current(self) -> typing.Tuple[ProgressActivity, ...]:
        """Alias for :attr:`activities` (the current view)."""
        return self.activities

    @property
    def all_activities(self) -> typing.Tuple[ProgressActivity, ...]:
        """Every retained generation, including non-current generations."""
        return tuple(sorted(self._activities.values(), key=self._display_sort_key))

    @property
    def keys(self) -> typing.Tuple[ProgressKey, ...]:
        """The identities of all retained activities."""
        return tuple(activity.key for activity in self.all_activities)

    def get(
        self,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable],
        activity_id: int,
        generation: typing.Any = _UNSET,
    ) -> typing.Optional[ProgressActivity]:
        """Get an activity.

        When ``generation`` is omitted the current generation for the activity
        number is returned.  Pass ``generation=None`` to look up the
        generation-less identity explicitly.
        """
        if generation is _UNSET:
            base = (source, pipeline_id, activity_id)
            gen = self._current.get(base, None)
        else:
            gen = generation

        return self._activities.get(ProgressKey(source, pipeline_id, activity_id, gen))

    def update(
        self,
        record: typing.Any,
        *,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = None,
        sequence: typing.Any = None,
        weight: typing.Optional[float] = None,
        activity_id: typing.Optional[int] = None,
        **fields: typing.Any,
    ) -> bool:
        """Apply one progress record or :class:`psrpcore.ProgressRecordEvent`.

        Args:
            record: A ``ProgressRecordEvent``, ``ProgressRecord``,
                ``ProgressRecordMsg``, mapping or object with the standard
                progress fields.  An integer is accepted as an activity number
                when the remaining values are supplied as keyword fields.
            source: The source of the record.  For an event this defaults to
                its ``runspace_pool_id``.  Otherwise it is required.
            pipeline_id: The pipeline identifier.  For an event this defaults
                to ``event.pipeline_id``.  Use ``None`` for pool-level
                progress.
            generation: Evidence separating reuses of the same activity
                number.
            sequence: Optional ordering value for records in the same
                generation.  A record with a lower or equal sequence is
                ignored once a numbered record has been accepted.
            weight: Optional aggregation weight supplied by the caller.  A
                previously supplied weight is retained when this is omitted.
            activity_id: Overrides the activity number read from ``record``.
            **fields: Additional activity fields (for example ``activity``,
                ``percent_complete``, ``parent_activity_id`` or
                ``completed``) used with an integer ``record`` or to override
                values read from a mapping/object.

        Returns:
            bool: ``True`` when the record became part of the view, ``False``
            when it was stale and ignored.
        """
        update = self._coerce_update(
            record,
            source=source,
            pipeline_id=pipeline_id,
            generation=generation,
            sequence=sequence,
            weight=weight,
            activity_id=activity_id,
            fields=fields,
        )
        return self._apply(update)

    def update_event(
        self,
        event: typing.Any,
        **kwargs: typing.Any,
    ) -> bool:
        """Apply a :class:`psrpcore.ProgressRecordEvent`."""
        return self.update(event, **kwargs)

    def track(
        self,
        record: typing.Any,
        **kwargs: typing.Any,
    ) -> bool:
        """Alias for :meth:`update`."""
        return self.update(record, **kwargs)

    def snapshot(
        self,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = _UNSET,
    ) -> "ProgressTree":
        """Alias for :meth:`tree`."""
        return self.tree(source=source, pipeline_id=pipeline_id, generation=generation)

    def add(
        self,
        *,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable] = None,
        activity_id: int,
        activity: typing.Optional[str] = None,
        status_description: typing.Optional[str] = None,
        current_operation: typing.Optional[str] = None,
        percent_complete: typing.Optional[int] = None,
        parent_activity_id: typing.Optional[int] = None,
        seconds_remaining: typing.Optional[int] = None,
        completed: bool = False,
        generation: typing.Any = None,
        sequence: typing.Any = None,
        weight: typing.Optional[float] = None,
    ) -> bool:
        """Add a progress update without constructing a progress record."""
        return self.update(
            {
                "ActivityId": activity_id,
                "Activity": activity,
                "StatusDescription": status_description,
                "CurrentOperation": current_operation,
                "PercentComplete": percent_complete,
                "ParentActivityId": parent_activity_id,
                "SecondsRemaining": seconds_remaining,
                "Type": ProgressRecordType.Completed if completed else ProgressRecordType.Processing,
            },
            source=source,
            pipeline_id=pipeline_id,
            generation=generation,
            sequence=sequence,
            weight=weight,
            activity_id=activity_id,
        )

    def expect(
        self,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable],
        activity_id: int,
        *,
        generation: typing.Any = None,
        activity: typing.Optional[str] = None,
        weight: typing.Optional[float] = None,
    ) -> ProgressActivity:
        """Register an activity that has not reported an update yet.

        The resulting activity has :attr:`ProgressActivity.received` set to
        ``False`` and status :attr:`ProgressStatus.NO_UPDATE`.
        """
        key = ProgressKey(source, pipeline_id, activity_id, generation)
        self._validate_key(key)
        if weight is not None:
            weight = self._validate_weight(weight)

        existing = self._activities.get(key)
        if existing is not None and existing.received:
            if weight is not None and existing.weight is None:
                existing = dataclasses.replace(existing, weight=weight)
                self._activities[key] = existing
            return existing

        if existing is None:
            existing = ProgressActivity(
                key=key,
                activity=activity,
                weight=weight,
                received=False,
            )
            self._activities[key] = existing
            self._note_generation(key)

        elif weight is not None or (activity is not None and existing.activity is None):
            existing = dataclasses.replace(
                existing,
                activity=activity if activity is not None else existing.activity,
                weight=weight if weight is not None else existing.weight,
            )
            self._activities[key] = existing

        return existing

    def extend(
        self,
        records: typing.Iterable[typing.Any],
        **defaults: typing.Any,
    ) -> int:
        """Apply a batch of records.

        Each item may be a record/event or a mapping with a ``record`` entry
        plus optional fields.  Control entries (``source``, ``pipeline_id``,
        ``generation``, ``sequence``, ``weight`` and ``activity_id``) are
        passed to :meth:`update`; any other entries are activity fields.

        Returns:
            int: The number of accepted (current) updates.
        """
        accepted = 0
        control_keys = {"record", "source", "pipeline_id", "generation", "sequence", "weight", "activity_id"}
        for item in records:
            if isinstance(item, Mapping) and "record" in item:
                kwargs = {k: item[k] for k in control_keys if k != "record" and k in item}
                fields = {k: v for k, v in item.items() if k not in control_keys}
                kwargs = {**defaults, **kwargs}
                if self.update(item["record"], **kwargs, **fields):
                    accepted += 1
            else:
                if self.update(item, **defaults):
                    accepted += 1
        return accepted

    @classmethod
    def from_records(
        cls,
        records: typing.Iterable[typing.Any],
        **defaults: typing.Any,
    ) -> "ProgressView":
        """Create a view from a batch of records."""
        view = cls()
        view.extend(records, **defaults)
        return view

    @classmethod
    def from_events(
        cls,
        events: typing.Iterable[typing.Any],
        **defaults: typing.Any,
    ) -> "ProgressView":
        """Create a view from progress events."""
        view = cls()
        for event in events:
            view.update_event(event, **defaults)
        return view

    def clear(self) -> None:
        """Remove all activities and generation history.

        Input weights remain configured; use :meth:`set_weight` or a new view
        to change them.
        """
        self._activities.clear()
        self._current.clear()

    def set_weight(
        self,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable],
        activity_id: int,
        weight: float,
        generation: typing.Any = _UNSET,
    ) -> None:
        """Set the input weight for an activity.

        When ``generation`` is omitted the weight applies to whatever
        generation currently uses that activity number.
        """
        key = ProgressKey(
            source,
            pipeline_id,
            int(activity_id),
            self._current.get((source, pipeline_id, int(activity_id)), None)
            if generation is _UNSET
            else generation,
        )
        self._validate_key(key)
        self._weights[key] = self._validate_weight(weight)

        activity = self._activities.get(key)
        if activity is not None:
            self._activities[key] = dataclasses.replace(activity, weight=self._weights[key])

    def generation(
        self,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable],
        activity_id: int,
    ) -> typing.Any:
        """Return the current generation for an activity number."""
        return self._current.get((source, pipeline_id, activity_id), None)

    def tree(
        self,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = _UNSET,
    ) -> ProgressTree:
        """Build the current progress tree for a scope.

        With no arguments the first tree in deterministic source/pipeline
        order is returned.  Missing parents do not prevent their children from
        being included; such children appear in :attr:`ProgressTree.orphans`
        and remain available.
        """
        trees = self.trees(source=source, pipeline_id=pipeline_id, generation=generation)
        if not trees:
            source_value = source
            pipeline_value = None if pipeline_id is _UNSET else pipeline_id
            generation_value = None if generation is _UNSET else generation
            return self._build_tree(source_value, pipeline_value, generation_value, ())
        return trees[0]

    def trees(
        self,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = _UNSET,
    ) -> typing.Tuple[ProgressTree, ...]:
        """Build all trees matching a scope, one per source and pipeline.

        Activity numbers can be reused with different generation evidence.
        Only the current generation is included unless a generation is
        requested explicitly.
        """
        candidates = self._with_stored_weights(self._filter_activities(source, pipeline_id, generation))

        grouped: typing.Dict[
            typing.Tuple[typing.Hashable, typing.Optional[typing.Hashable]],
            typing.List[ProgressActivity],
        ] = {}
        for activity in candidates:
            group_key = (activity.source, activity.pipeline_id)
            grouped.setdefault(group_key, []).append(activity)

        trees = []
        for (group_source, group_pipeline), activities in grouped.items():
            generations = {a.generation for a in activities}
            tree_generation = None
            if generation is not _UNSET:
                tree_generation = generation
            elif len(generations) == 1:
                tree_generation = next(iter(generations))

            trees.append(
                self._build_tree(
                    group_source,
                    group_pipeline,
                    tree_generation,
                    tuple(activities),
                )
            )

        return tuple(sorted(trees, key=self._tree_sort_key))

    def coverage(
        self,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = _UNSET,
        *,
        weights: typing.Optional[typing.Mapping[typing.Any, float]] = None,
        total_weight: typing.Optional[float] = None,
        expected: typing.Optional[typing.Iterable[typing.Any]] = None,
    ) -> ProgressCoverage:
        """Describe coverage and calculate an overall ratio when sufficient.

        Weighted aggregation is only performed when every activity has an
        update, every leaf has a known percentage or explicit completion, every
        leaf has a weight, all parents are present and the graph is acyclic.
        ``total_weight`` can be supplied to require a minimum amount of covered
        weight.  ``weights`` overrides input weights for this calculation.
        """
        current = self._filter_activities(source, pipeline_id, generation)
        current = self._with_stored_weights(current)
        current = self._with_weight_overrides(current, weights)
        current_keys = {(a.source, a.pipeline_id, a.activity_id, a.generation) for a in current}

        expected_missing: typing.List[ProgressKey] = []
        if expected is not None:
            for item in expected:
                key = self._coerce_expected_key(item, source, pipeline_id, generation)
                if key is None:
                    continue
                if key.as_tuple() not in current_keys:
                    expected_missing.append(key)

        grouped: typing.Dict[
            typing.Tuple[typing.Hashable, typing.Optional[typing.Hashable]],
            typing.List[ProgressActivity],
        ] = {}
        for activity in current:
            group_key = (activity.source, activity.pipeline_id)
            grouped.setdefault(group_key, []).append(activity)

        coverages = [
            self._build_tree(*group_key, None, tuple(activities)).coverage
            for group_key, activities in grouped.items()
        ]

        if not coverages:
            return ProgressCoverage(
                complete=False,
                weighted_percent=None,
                covered_weight=0.0,
                activity_count=0,
                problems=("no activities",),
            )

        problems: typing.List[str] = []
        no_update: typing.List[ProgressKey] = []
        unknown_percent: typing.List[ProgressKey] = []
        missing_weight: typing.List[ProgressKey] = []
        missing_parents: typing.List[ProgressKey] = []
        cycles: typing.List[typing.Tuple[ProgressKey, ...]] = []

        for coverage in coverages:
            problems.extend(coverage.problems)
            no_update.extend(coverage.no_update)
            unknown_percent.extend(coverage.unknown_percent)
            missing_weight.extend(coverage.missing_weight)
            missing_parents.extend(coverage.missing_parents)
            cycles.extend(coverage.cycles)

        if expected_missing:
            no_update.extend(expected_missing)
            problems.extend(f"expected activity {key!r} was not reported" for key in expected_missing)

        covered_weight = sum(coverage.covered_weight for coverage in coverages)
        complete = all(c.complete for c in coverages) and not expected_missing
        if total_weight is not None:
            total_weight = self._validate_weight(total_weight)
            if covered_weight + 1e-9 < total_weight:
                complete = False
                problems.append(f"covered weight {covered_weight:g} is less than required {total_weight:g}")

        weighted_percent = None
        if complete:
            weighted_percent = self._combine_percentages(coverages)
            if weighted_percent is None:
                complete = False
                problems.append("total weight must be positive")

        return ProgressCoverage(
            complete=complete,
            weighted_percent=weighted_percent,
            covered_weight=covered_weight,
            activity_count=len(current),
            no_update=tuple(sorted(set(no_update), key=self._key_sort_key)),
            unknown_percent=tuple(sorted(set(unknown_percent), key=self._key_sort_key)),
            missing_weight=tuple(sorted(set(missing_weight), key=self._key_sort_key)),
            missing_parents=tuple(sorted(set(missing_parents), key=self._key_sort_key)),
            cycles=tuple(cycles),
            problems=tuple(dict.fromkeys(problems)),
        )

    def overall_percent(
        self,
        source: typing.Optional[typing.Hashable] = None,
        pipeline_id: typing.Any = _UNSET,
        generation: typing.Any = _UNSET,
        **kwargs: typing.Any,
    ) -> typing.Optional[float]:
        """Return the weighted overall percentage when coverage is complete."""
        return self.coverage(
            source=source,
            pipeline_id=pipeline_id,
            generation=generation,
            **kwargs,
        ).weighted_percent

    def _coerce_update(
        self,
        record: typing.Any,
        *,
        source: typing.Optional[typing.Hashable],
        pipeline_id: typing.Any,
        generation: typing.Any,
        sequence: typing.Any,
        weight: typing.Optional[float],
        activity_id: typing.Optional[int],
        fields: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> "ProgressActivity":
        extra = dict(fields or {})
        raw = record
        derived_source = None
        derived_pipeline: typing.Any = _UNSET

        if hasattr(record, "record") and hasattr(record, "runspace_pool_id"):
            derived_source = record.runspace_pool_id
            derived_pipeline = getattr(record, "pipeline_id", None)
            raw = record.record

        if isinstance(record, int) and not isinstance(record, bool) and activity_id is None:
            activity_id = record
            raw = {}

        if isinstance(raw, Mapping):
            merged: typing.Mapping[str, typing.Any] = {**raw, **extra}
        else:
            merged = extra

        def field(*names: str, default: typing.Any = None) -> typing.Any:
            value = _read_field(merged, *names, default=_UNSET)
            if value is not _UNSET:
                return value
            return _read_field(raw, *names, default=default)

        if source is None:
            source = derived_source
        if source is None:
            source = field("source", "Source", "source_id", "SourceId", "runspace_pool_id")
        if source is None:
            raise ValueError("source is required for a progress update")

        if pipeline_id is _UNSET:
            if derived_pipeline is not _UNSET:
                pipeline_id = derived_pipeline
            else:
                pipeline_id = field("pipeline_id", "PipelineId", "pipeline")

        if activity_id is None:
            activity_id = field("activity_id", "ActivityId", "Id")
        if activity_id is None:
            raise ValueError("activity_id is required for a progress update")

        try:
            activity_id = int(activity_id)
        except (TypeError, ValueError):
            raise ValueError(f"activity_id must be an integer, got {activity_id!r}") from None

        if generation is None:
            generation = field("generation", "Generation", "generation_id")

        if sequence is None:
            sequence = field("sequence", "Sequence", "version", "Version")

        percent = self._validate_percent(field("percent_complete", "PercentComplete", "percent"))
        parent = self._validate_parent(
            field("parent_activity_id", "ParentActivityId", "parent_id", "parent", default=-1)
        )
        seconds = self._validate_optional_int(field("seconds_remaining", "SecondsRemaining", default=-1))

        record_type = field("RecordType", "Type", "record_type", default=ProgressRecordType.Processing)
        if record_type is None:
            record_type = ProgressRecordType.Processing
        completed = self._is_completed(record_type)
        completed_override = field("completed", default=_UNSET)
        if completed_override is not _UNSET:
            completed = bool(completed_override)

        if weight is None:
            weight = field("weight", "Weight")
        weight = self._validate_weight(weight) if weight is not None else None
        if weight is None:
            weight = self._lookup_stored_weight(source, pipeline_id, activity_id, generation)

        key = ProgressKey(source, pipeline_id, activity_id, generation)
        self._validate_key(key)

        return ProgressActivity(
            key=key,
            activity=field("activity", "Activity"),
            status_description=field("status_description", "StatusDescription"),
            current_operation=field("current_operation", "CurrentOperation"),
            percent_complete=percent,
            parent_activity_id=parent,
            seconds_remaining=seconds,
            weight=weight,
            received=True,
            completed=completed,
            sequence=sequence,
            record=raw if isinstance(raw, ProgressRecord) else None,
        )

    def _apply(self, update: ProgressActivity) -> bool:
        existing = self._activities.get(update.key)
        current = self._current_generation(update.key)
        stale_generation = False

        if existing is None and current is not _UNSET:
            # The activity number already has a current generation.  A record
            # from an older generation is stale even though it is a different
            # identity, so it must not overwrite the confirmed newer view.  It
            # is retained so both generations remain separately inspectable.
            if current != update.key.generation and not self._generation_newer(
                update.key.generation, current
            ):
                stale_generation = True

        if existing is not None:
            if existing.completed:
                return False
            if not self._is_newer(update, existing):
                return False

            if update.weight is None:
                update = dataclasses.replace(update, weight=existing.weight)
            if update.activity is None:
                update = dataclasses.replace(update, activity=existing.activity)

        self._activities[update.key] = update
        self._note_generation(update.key)

        # An older generation is retained for inspection but does not become
        # the current view.
        return not stale_generation and self._current_generation(update.key) == update.key.generation

    def _is_newer(self, update: ProgressActivity, existing: ProgressActivity) -> bool:
        if existing.sequence is not None:
            if update.sequence is None:
                return False
            try:
                return update.sequence > existing.sequence
            except TypeError:
                raise ValueError("sequence values must be comparable") from None

        return True

    def _note_generation(self, key: ProgressKey) -> None:
        base = (key.source, key.pipeline_id, key.activity_id)
        current = self._current.get(base, _UNSET)
        if current is _UNSET:
            self._current[base] = key.generation
        elif current != key.generation and self._generation_newer(key.generation, current):
            self._current[base] = key.generation

    def _current_generation(self, key: ProgressKey) -> typing.Any:
        return self._current.get((key.source, key.pipeline_id, key.activity_id), _UNSET)

    @staticmethod
    def _generation_newer(new: typing.Any, old: typing.Any) -> bool:
        if new == old:
            return False
        if old is None:
            # Explicit generation evidence supersedes a record that had none.
            return new is not None
        if new is None:
            # A late record without evidence cannot replace a confirmed newer
            # generation; it remains retained under the generation-less key.
            return False
        try:
            return bool(new > old)
        except TypeError:
            # Without comparable generation evidence, keep the generation that
            # was established first instead of letting a late record switch it.
            return False

    def _resolve_key(self, key: typing.Any) -> ProgressKey:
        if isinstance(key, ProgressKey):
            return key
        if isinstance(key, tuple):
            if len(key) == 3:
                return ProgressKey(
                    key[0],
                    key[1],
                    int(key[2]),
                    self._current.get((key[0], key[1], int(key[2])), None),
                )
            if len(key) == 4:
                return ProgressKey(key[0], key[1], int(key[2]), key[3])
        raise KeyError(key)

    def _filter_activities(
        self,
        source: typing.Optional[typing.Hashable],
        pipeline_id: typing.Any,
        generation: typing.Any,
    ) -> typing.Tuple[ProgressActivity, ...]:
        current = self.all_activities if generation is not _UNSET else self.activities
        if source is not None:
            current = tuple(a for a in current if a.source == source)
        if pipeline_id is not _UNSET:
            current = tuple(a for a in current if a.pipeline_id == pipeline_id)
        if generation is not _UNSET:
            current = tuple(a for a in current if a.generation == generation)
        return current

    def _with_stored_weights(
        self,
        activities: typing.Tuple[ProgressActivity, ...],
    ) -> typing.Tuple[ProgressActivity, ...]:
        if not self._weights:
            return activities

        result = list(activities)
        for index, activity in enumerate(result):
            if activity.weight is not None:
                continue
            weight = self._lookup_stored_weight(
                activity.source,
                activity.pipeline_id,
                activity.activity_id,
                activity.generation,
            )
            if weight is not None:
                result[index] = dataclasses.replace(activity, weight=weight)
        return tuple(result)

    def _with_weight_overrides(
        self,
        activities: typing.Tuple[ProgressActivity, ...],
        weights: typing.Optional[typing.Mapping[typing.Any, float]],
    ) -> typing.Tuple[ProgressActivity, ...]:
        if not weights:
            return activities

        result = list(activities)
        for index, activity in enumerate(result):
            override = self._lookup_weight(weights, activity)
            if override is _UNSET:
                continue
            result[index] = dataclasses.replace(activity, weight=self._validate_weight(override))
        return tuple(result)

    @staticmethod
    def _lookup_weight(weights: typing.Mapping[typing.Any, float], activity: ProgressActivity) -> typing.Any:
        candidates: typing.List[typing.Any] = [
            activity.key,
            activity.key.as_tuple(),
            (activity.source, activity.pipeline_id, activity.activity_id),
            activity.activity_id,
        ]
        for candidate in candidates:
            try:
                if candidate in weights:
                    return weights[candidate]
            except TypeError:
                continue
        return _UNSET

    def _lookup_stored_weight(
        self,
        source: typing.Hashable,
        pipeline_id: typing.Optional[typing.Hashable],
        activity_id: int,
        generation: typing.Any,
    ) -> typing.Optional[float]:
        for key in (
            ProgressKey(source, pipeline_id, activity_id, generation),
            ProgressKey(source, pipeline_id, activity_id, None),
        ):
            if key in self._weights:
                return self._weights[key]
        return self._weights_by_id.get(activity_id)

    @staticmethod
    def _coerce_weight_key(key: typing.Any) -> typing.Optional[ProgressKey]:
        if isinstance(key, ProgressKey):
            return key
        if isinstance(key, tuple):
            if len(key) == 4:
                return ProgressKey(key[0], key[1], int(key[2]), key[3])
            if len(key) == 3:
                return ProgressKey(key[0], key[1], int(key[2]), None)
        return None

    def _coerce_expected_key(
        self,
        item: typing.Any,
        source: typing.Optional[typing.Hashable],
        pipeline_id: typing.Any,
        generation: typing.Any,
    ) -> typing.Optional[ProgressKey]:
        if isinstance(item, ProgressKey):
            return item
        if isinstance(item, tuple):
            if len(item) == 4:
                return ProgressKey(item[0], item[1], int(item[2]), item[3])
            if len(item) == 3:
                return ProgressKey(item[0], item[1], int(item[2]), None if generation is _UNSET else generation)
        if isinstance(item, int):
            if source is None:
                return None
            resolved_pipeline = None if pipeline_id is _UNSET else pipeline_id
            resolved_generation = None if generation is _UNSET else generation
            return ProgressKey(source, resolved_pipeline, int(item), resolved_generation)
        return None

    def _build_tree(
        self,
        source: typing.Optional[typing.Hashable],
        pipeline_id: typing.Optional[typing.Hashable],
        generation: typing.Any,
        activities: typing.Tuple[ProgressActivity, ...],
    ) -> ProgressTree:
        ordered = tuple(sorted(activities, key=self._display_sort_key))
        by_id = {a.activity_id: a for a in ordered}

        children: typing.Dict[int, typing.List[ProgressActivity]] = {a.activity_id: [] for a in ordered}
        for activity in ordered:
            if activity.parent_activity_id in by_id:
                children[activity.parent_activity_id].append(activity)

        cycle_numbers = self._find_cycles(by_id)
        cycle_members = {number for cycle in cycle_numbers for number in cycle}

        roots = tuple(
            a
            for a in ordered
            if a.activity_id not in cycle_members
            and (a.parent_activity_id is None or a.parent_activity_id not in by_id)
        )
        orphans = tuple(a for a in roots if a.parent_activity_id is not None and a.parent_activity_id not in by_id)

        reachable = set()
        stack = [a.activity_id for a in roots]
        while stack:
            number = stack.pop()
            if number in reachable:
                continue
            reachable.add(number)
            stack.extend(child.activity_id for child in children.get(number, ()))
        unresolved = tuple(a for a in ordered if a.activity_id not in reachable)

        cycle_activities = tuple(
            tuple(by_id[number] for number in cycle if number in by_id) for cycle in cycle_numbers
        )
        coverage = self._coverage_for(
            ordered,
            by_id,
            children,
            cycle_numbers,
        )

        return ProgressTree(
            source=source,  # Empty trees can have no source.
            pipeline_id=pipeline_id,
            generation=generation,
            activities=ordered,
            roots=roots,
            orphans=orphans,
            cycles=cycle_activities,
            unresolved=unresolved,
            coverage=coverage,
            _children={number: tuple(values) for number, values in children.items()},
        )

    def _coverage_for(
        self,
        activities: typing.Tuple[ProgressActivity, ...],
        by_id: typing.Dict[int, ProgressActivity],
        children: typing.Dict[int, typing.List[ProgressActivity]],
        cycle_numbers: typing.List[typing.Tuple[int, ...]],
    ) -> ProgressCoverage:
        if not activities:
            return ProgressCoverage(
                complete=False,
                weighted_percent=None,
                covered_weight=0.0,
                activity_count=0,
                problems=("no activities",),
            )

        no_update = tuple(a.key for a in activities if not a.received)
        leaves = tuple(a for a in activities if not children.get(a.activity_id))
        unknown_percent = tuple(
            a.key for a in leaves if not a.completed and a.percent_complete is None
        )
        missing_weight = tuple(a.key for a in leaves if a.weight is None)
        missing_parents = tuple(
            a.key for a in activities if a.parent_activity_id is not None and a.parent_activity_id not in by_id
        )
        cycle_keys = tuple(tuple(by_id[number].key for number in cycle if number in by_id) for cycle in cycle_numbers)

        problems: typing.List[str] = []
        if no_update:
            problems.extend(f"activity {key!r} has no update" for key in no_update)
        if unknown_percent:
            problems.extend(f"activity {key!r} has unknown percent" for key in unknown_percent)
        if missing_weight:
            problems.extend(f"activity {key!r} has no weight" for key in missing_weight)
        if missing_parents:
            for key in missing_parents:
                activity = by_id[key.activity_id]
                problems.append(f"activity {key!r} has missing parent {activity.parent_activity_id}")
        if cycle_keys:
            problems.extend(f"cycle detected: {cycle!r}" for cycle in cycle_keys)

        covered_weight = sum(a.weight or 0.0 for a in self._covered_leaf_activities(activities))
        complete = (
            not no_update
            and not unknown_percent
            and not missing_weight
            and not missing_parents
            and not cycle_keys
            and covered_weight > 0
        )

        weighted_percent: typing.Optional[float] = None
        if complete:
            weighted_percent = sum((a.weight or 0.0) * (a.effective_percent or 0.0) for a in leaves) / covered_weight
        elif covered_weight <= 0:
            problems.append("total weight must be positive")

        return ProgressCoverage(
            complete=complete,
            weighted_percent=weighted_percent,
            covered_weight=covered_weight,
            activity_count=len(activities),
            no_update=no_update,
            unknown_percent=unknown_percent,
            missing_weight=missing_weight,
            missing_parents=missing_parents,
            cycles=cycle_keys,
            problems=tuple(problems),
        )

    @staticmethod
    def _find_cycles(by_id: typing.Dict[int, ProgressActivity]) -> typing.List[typing.Tuple[int, ...]]:
        state: typing.Dict[int, int] = {}
        cycles: typing.List[typing.Tuple[int, ...]] = []
        seen: typing.Set[typing.Tuple[int, ...]] = set()

        for start in by_id:
            if state.get(start) == 2:
                continue

            path: typing.List[int] = []
            positions: typing.Dict[int, int] = {}
            current: typing.Optional[int] = start
            while current is not None and current in by_id:
                if current in positions:
                    cycle = tuple(path[positions[current] :])
                    canonical = _canonical_cycle(cycle)
                    if canonical not in seen:
                        seen.add(canonical)
                        cycles.append(canonical)
                    break
                if state.get(current) == 2:
                    break

                positions[current] = len(path)
                path.append(current)
                state[current] = 1
                current = by_id[current].parent_activity_id

            for number in path:
                state[number] = 2

        return cycles

    def _leaf_activities(
        self,
        activities: typing.Tuple[ProgressActivity, ...],
    ) -> typing.Tuple[ProgressActivity, ...]:
        parent_numbers = {a.parent_activity_id for a in activities if a.parent_activity_id is not None}
        return tuple(a for a in activities if a.activity_id not in parent_numbers)

    @staticmethod
    def _covered_leaf_activities(
        activities: typing.Tuple[ProgressActivity, ...],
    ) -> typing.Tuple[ProgressActivity, ...]:
        parent_numbers = {a.parent_activity_id for a in activities if a.parent_activity_id is not None}
        return tuple(
            a
            for a in activities
            if a.activity_id not in parent_numbers
            and a.received
            and (a.completed or a.percent_complete is not None)
            and a.weight is not None
        )

    def _weighted_percent(self, activities: typing.Tuple[ProgressActivity, ...]) -> typing.Optional[float]:
        leaves = self._leaf_activities(activities)
        total = sum(a.weight or 0.0 for a in leaves)
        if total <= 0:
            return None
        return sum((a.weight or 0.0) * (a.effective_percent or 0.0) for a in leaves) / total

    @staticmethod
    def _combine_percentages(coverages: typing.Sequence[ProgressCoverage]) -> typing.Optional[float]:
        total = sum(c.covered_weight for c in coverages)
        if total <= 0:
            return None
        values = [c.weighted_percent for c in coverages if c.weighted_percent is not None]
        if not values:
            return None
        return sum(c.covered_weight * (c.weighted_percent or 0.0) for c in coverages) / total

    @staticmethod
    def _validate_key(key: ProgressKey) -> None:
        for name, value in (
            ("source", key.source),
            ("pipeline_id", key.pipeline_id),
            ("generation", key.generation),
        ):
            if value is not None and not isinstance(value, Hashable):
                raise TypeError(f"{name} must be hashable, got {type(value).__name__}")

    @staticmethod
    def _validate_percent(value: typing.Any) -> typing.Optional[int]:
        if value is None:
            return None
        try:
            percent = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"percent_complete must be an integer, got {value!r}") from None
        if percent < 0:
            return None
        if percent > 100:
            raise ValueError(f"percent_complete must be between 0 and 100, got {percent}")
        return percent

    @staticmethod
    def _validate_parent(value: typing.Any) -> typing.Optional[int]:
        if value is None:
            return None
        try:
            parent = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"parent_activity_id must be an integer, got {value!r}") from None
        return None if parent < 0 else parent

    @staticmethod
    def _validate_optional_int(value: typing.Any) -> typing.Optional[int]:
        if value is None:
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"value must be an integer, got {value!r}") from None
        return None if result < 0 else result

    @staticmethod
    def _validate_weight(value: typing.Any) -> float:
        try:
            weight = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"weight must be a number, got {value!r}") from None
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"weight must be a finite non-negative number, got {value!r}")
        return weight

    @staticmethod
    def _is_completed(value: typing.Any) -> bool:
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return name.lower() == "completed"
        if isinstance(value, str):
            return value.lower() == "completed"
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _display_sort_key(activity: ProgressActivity) -> typing.Tuple[str, str, int, str]:
        return (
            str(activity.source),
            str(activity.pipeline_id),
            activity.activity_id,
            str(activity.generation),
        )

    @staticmethod
    def _key_sort_key(key: ProgressKey) -> typing.Tuple[str, str, int, str]:
        return (str(key.source), str(key.pipeline_id), key.activity_id, str(key.generation))

    def _tree_sort_key(
        self,
        tree: ProgressTree,
    ) -> typing.Tuple[str, str, str]:
        return (str(tree.source), str(tree.pipeline_id), str(tree.generation))


def _read_field(obj: typing.Any, *names: str, default: typing.Any = None) -> typing.Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _canonical_cycle(cycle: typing.Tuple[int, ...]) -> typing.Tuple[int, ...]:
    if not cycle:
        return cycle
    index = cycle.index(min(cycle))
    return cycle[index:] + cycle[:index]


ProgressTracker = ProgressView
"""Alias for :class:`ProgressView`."""


__all__ = [
    "ProgressActivity",
    "ProgressCoverage",
    "ProgressKey",
    "ProgressStatus",
    "ProgressTracker",
    "ProgressTree",
    "ProgressView",
]
