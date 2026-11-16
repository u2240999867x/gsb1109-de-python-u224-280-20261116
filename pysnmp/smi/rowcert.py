#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Certification of MIB table rows carrying a RowStatus column.

A collector gathers RowStatus-managed conceptual rows over time.  Raw
observations may arrive in different polling windows: a status read and a
column read can only be combined into a certified configuration when both
facts are proven within a single common generation.  This module never
guesses missing evidence:

* only ``active`` rows whose *required* columns are all proven together in
  a generation where the status is readable are publishable;
* ``createAndWait`` (and ``createAndGo`` / ``destroy`` written back by a
  broken agent) are write-only values and never count as a ready state;
* a row re-created at the same index after ``destroy`` starts a fresh
  lifetime epoch and cannot inherit field values of the previous row;
* status and column reads from different windows are only joined at
  generations both can prove;
* rows whose status was never observed stay unknown instead of being
  assumed active;
* every raw observation is retained even if no complete configuration can
  ever be certified.
"""

from pysnmp.smi.error import SmiError

# RowStatus numeric values (RFC 2579 / SNMPv2-TC).
NOT_EXISTS = 0
ACTIVE = 1
NOT_IN_SERVICE = 2
NOT_READY = 3
CREATE_AND_GO = 4
CREATE_AND_WAIT = 5
DESTROY = 6

# Human-readable status names, keyed by the RowStatus numeric value.
STATUS_NAMES = {
    NOT_EXISTS: "notExists",
    ACTIVE: "active",
    NOT_IN_SERVICE: "notInService",
    NOT_READY: "notReady",
    CREATE_AND_GO: "createAndGo",
    CREATE_AND_WAIT: "createAndWait",
    DESTROY: "destroy",
}

# Statuses an agent may return on a retrieval operation.
READABLE_STATES = frozenset((ACTIVE, NOT_IN_SERVICE, NOT_READY))

# Statuses that only appear in management set operations.  A well-behaved
# agent never returns these; if one is observed on retrieval, the claim
# contradicts the RowStatus state machine and is reported as such.
WRITE_ONLY_STATES = frozenset((CREATE_AND_GO, CREATE_AND_WAIT, DESTROY))


class RowCertificationError(SmiError):
    """Base class for failures while certifying collected rows."""


class UnknownStatusError(RowCertificationError):
    """A reported RowStatus value is outside the 0..6 defined range."""


class Observation:
    """A single raw fact read from the agent.

    Each observation keeps the generation (polling window) it was taken in,
    so facts from different windows can never be silently merged.
    """

    __slots__ = ("index", "generation", "column", "value", "status", "raw")

    def __init__(
        self, index, generation, column=None, value=None, status=None, raw=None
    ):
        self.index = index
        self.generation = generation
        # ``column`` is None for a RowStatus observation, otherwise the
        # column name (or OID suffix) the value belongs to.
        self.column = column
        self.value = value
        # ``status`` is the raw RowStatus numeric value for status reads.
        self.status = status
        # Optional verbatim payload (e.g. the original varbind) retained for
        # audit purposes; the collector never throws raw evidence away.
        self.raw = raw

    @property
    def is_status(self):
        return self.column is None

    def __repr__(self):
        if self.is_status:
            return "{}(index={!r}, generation={!r}, status={!r})".format(
                self.__class__.__name__,
                self.index,
                self.generation,
                self.status,
            )
        return "{}(index={!r}, generation={!r}, column={!r}, value={!r})".format(
            self.__class__.__name__,
            self.index,
            self.generation,
            self.column,
            self.value,
        )


class _Epoch:
    """One lifetime of a row index.

    A ``destroy`` (or explicit ``notExists``) closes the current epoch; the
    next status observation at the same index opens a fresh epoch.  Field
    values never flow across epochs, so a re-created row cannot inherit the
    columns of the row that was destroyed.
    """

    __slots__ = ("seq", "statuses", "columns", "open")

    def __init__(self, seq):
        self.seq = seq
        # generation -> list of raw RowStatus values observed then
        self.statuses = {}
        # column -> generation -> list of raw values observed then.
        # Several values may land in one generation; only an unambiguous
        # single value can ever be proven.
        self.columns = {}
        self.open = True

    def add_status(self, generation, status):
        self.statuses.setdefault(generation, []).append(status)

    def add_column(self, generation, column, value):
        self.columns.setdefault(column, {}).setdefault(generation, []).append(value)


class RowCollector:
    """Accumulates observations and certifies rows of one MIB table.

    Parameters
    ----------
    required_columns:
        Names of the columns that must hold a value before a row of this
        table may be published.  The RowStatus column itself is handled
        separately and must not be listed here.
    publishable_states:
        RowStatus values under which a complete row is publishable.  Only
        ``active`` is accepted by default, matching the managed-device
        semantics of the textual convention.
    """

    def __init__(self, required_columns=(), publishable_states=(ACTIVE,)):
        self.required_columns = tuple(required_columns)
        self.publishable_states = frozenset(publishable_states)
        # row index -> list of _Epoch, oldest first
        self._epochs = {}
        # every observation ever recorded, in arrival order
        self._observations = []

    # ------------------------------------------------------------------
    # Evidence intake
    # ------------------------------------------------------------------
    def record_status(self, index, generation, status, raw=None):
        """Record a RowStatus value read for ``index`` at ``generation``."""
        status = self._validate_status(status)
        observation = Observation(index, generation, status=status, raw=raw)
        self._observations.append(observation)

        epochs = self._epochs.setdefault(index, [])
        current = epochs[-1] if epochs else None

        if status in (DESTROY, NOT_EXISTS):
            # The row is (being) removed: close the epoch.  A later read
            # re-opens a brand new epoch at the same index.
            if current is None:
                current = _Epoch(len(epochs))
                epochs.append(current)
            current.add_status(generation, status)
            current.open = False
            return observation

        if current is None or not current.open:
            current = _Epoch(len(epochs))
            epochs.append(current)

        current.add_status(generation, status)
        return observation

    def record_column(self, index, generation, column, value, raw=None):
        """Record a non-RowStatus column value for ``index``."""
        observation = Observation(
            index, generation, column=column, value=value, raw=raw
        )
        self._observations.append(observation)

        epochs = self._epochs.setdefault(index, [])
        current = epochs[-1] if epochs else None
        if current is None or not current.open:
            # Column evidence arriving with no open row still has to be
            # kept; it lands in its own evidence-only epoch and can never
            # be joined to a status from a different lifetime.
            current = _Epoch(len(epochs))
            epochs.append(current)
        current.add_column(generation, column, value)
        return observation

    def record(self, observation):
        """Re-ingest an :class:`Observation` object."""
        if observation.is_status:
            return self.record_status(
                observation.index,
                observation.generation,
                observation.status,
                observation.raw,
            )
        return self.record_column(
            observation.index,
            observation.generation,
            observation.column,
            observation.value,
            observation.raw,
        )

    @staticmethod
    def _validate_status(status):
        try:
            status = int(status)
        except (TypeError, ValueError) as exc:
            raise UnknownStatusError(
                "non-integer RowStatus value {!r}".format(status)
            ) from exc
        if status not in STATUS_NAMES:
            raise UnknownStatusError(
                "RowStatus value {!r} is outside 0..6".format(status)
            )
        return status

    # ------------------------------------------------------------------
    # Raw evidence retention
    # ------------------------------------------------------------------
    @property
    def observations(self):
        """All raw observations in arrival order (never pruned)."""
        return tuple(self._observations)

    def observations_for(self, index):
        """Raw observations that mention ``index``.

        Re-evaluation after a supplementary poll therefore always sees the
        original samples alongside the newly collected ones.
        """
        return tuple(
            observation
            for observation in self._observations
            if observation.index == index
        )

    # ------------------------------------------------------------------
    # Certification
    # ------------------------------------------------------------------
    def certify(self):
        """Evaluate every row index and return a certification report.

        The report exposes:

        * ``publishable``: rows proven complete and active in a common
          generation;
        * ``pending_columns``: rows blocked on missing status evidence or
          required columns not provable together with a readable state;
        * ``contradictions``: mutually incompatible status claims,
          write-only statuses returned on retrieval and conflicting values
          observed for one column in a single generation.
        """
        publishable = []
        pending = []
        contradictions = []

        for index, epochs in self._epochs.items():
            for epoch in epochs:
                self._certify_epoch(index, epoch, publishable, pending, contradictions)

        return CertificationReport(
            publishable=publishable,
            pending_columns=pending,
            contradictions=contradictions,
            observations=self._observations,
        )

    def _certify_epoch(self, index, epoch, publishable, pending, contradictions):
        statuses, claims = self._status_evidence(index, epoch)
        contradictions.extend(claims)
        contradictions.extend(self._column_contradictions(index, epoch))

        row = CertifiedRow(
            index=index,
            epoch=epoch.seq,
            status_generations=tuple(sorted(statuses)),
        )

        missing = self._missing_required_columns(epoch, statuses)

        if not statuses:
            # No readable state could be proven at all: a write-only value
            # returned on retrieval is already reported above, and a row
            # whose status was never received must not be guessed active.
            pending.append(
                PendingRow(
                    index=index,
                    epoch=epoch.seq,
                    reason="missing-status",
                    missing_columns=tuple(missing),
                    row=row,
                )
            )
            return

        certified_generation = self._certified_generation(epoch, statuses, missing)
        if certified_generation is not None:
            row.status = statuses[certified_generation]
            row.certified_generation = certified_generation
            row.columns = self._snapshot(epoch, certified_generation)
            publishable.append(row)
            return

        latest_generation = max(statuses)
        row.status = statuses[latest_generation]
        if missing:
            reason = "missing-columns"
        else:
            # All required columns are known but no generation proves them
            # together with an active status (e.g. notInService / notReady,
            # or createAndWait-style negotiated creation still waiting).
            reason = "not-active"
        pending.append(
            PendingRow(
                index=index,
                epoch=epoch.seq,
                reason=reason,
                missing_columns=tuple(missing),
                row=row,
            )
        )

    def _status_evidence(self, index, epoch):
        """Collapse per-generation status reads into proven states.

        Two distinct readable status values within one generation are
        mutually contradictory claims and get reported instead of being
        arbitrarily resolved.  Write-only RowStatus values observed on a
        retrieval are contradictions as well; a readable value seen in
        another generation still counts as evidence.
        """
        proven = {}
        contradictions = []
        for generation, values in sorted(epoch.statuses.items()):
            distinct = set(values)
            for value in sorted(distinct & WRITE_ONLY_STATES):
                contradictions.append(
                    Contradiction(
                        index=index,
                        epoch=epoch.seq,
                        generation=generation,
                        kind="write-only status returned on retrieval",
                        detail=STATUS_NAMES[value],
                    )
                )
            readable = distinct & READABLE_STATES
            if len(readable) > 1:
                contradictions.append(
                    Contradiction(
                        index=index,
                        epoch=epoch.seq,
                        generation=generation,
                        kind="conflicting RowStatus values in one generation",
                        detail=tuple(STATUS_NAMES[value] for value in sorted(readable)),
                    )
                )
            elif readable:
                proven[generation] = next(iter(readable))
        return proven, contradictions

    def _column_contradictions(self, index, epoch):
        """Distinct values recorded for one column in one generation."""
        contradictions = []
        for column, by_generation in epoch.columns.items():
            for generation, values in by_generation.items():
                distinct = self._distinct(values)
                if len(distinct) > 1:
                    contradictions.append(
                        Contradiction(
                            index=index,
                            epoch=epoch.seq,
                            generation=generation,
                            kind="conflicting column values in one generation",
                            detail=(column, tuple(values)),
                        )
                    )
        return contradictions

    @staticmethod
    def _distinct(values):
        unique = []
        for value in values:
            if not any(value == seen for seen in unique):
                unique.append(value)
        return unique

    def _provable_generations(self, epoch, column):
        """Generations where ``column`` holds one unambiguous value."""
        by_generation = epoch.columns.get(column, {})
        return {
            generation
            for generation, values in by_generation.items()
            if len(self._distinct(values)) == 1
        }

    def _missing_required_columns(self, epoch, statuses):
        """Required columns with no provable value at a status generation.

        Status and column reads from different windows are only joined at
        generations both can prove; a required column whose windows never
        overlap a status window (or whose values conflict) is therefore
        still missing.
        """
        missing = []
        for column in self.required_columns:
            provable = self._provable_generations(epoch, column)
            if not provable & set(statuses):
                missing.append(column)
        return missing

    def _certified_generation(self, epoch, statuses, missing):
        """Latest generation proving an active row with all columns."""
        if missing:
            return None
        candidates = [
            generation
            for generation, state in statuses.items()
            if state in self.publishable_states
            and all(
                generation in self._provable_generations(epoch, column)
                for column in self.required_columns
            )
        ]
        if not candidates:
            return None
        return max(candidates)

    @staticmethod
    def _snapshot(epoch, generation):
        """Unambiguous column values provable at exactly ``generation``."""
        columns = {}
        for column, by_generation in epoch.columns.items():
            values = by_generation.get(generation)
            if values is None:
                continue
            distinct = RowCollector._distinct(values)
            if len(distinct) == 1:
                columns[column] = ProvenValue(value=distinct[0], generation=generation)
        return columns


class Contradiction:
    """A status or column claim that cannot be reconciled with the rules."""

    __slots__ = ("index", "epoch", "generation", "kind", "detail")

    def __init__(self, index, epoch, generation, kind, detail):
        self.index = index
        self.epoch = epoch
        self.generation = generation
        self.kind = kind
        self.detail = detail

    def __eq__(self, other):
        if not isinstance(other, Contradiction):
            return NotImplemented
        return (
            self.index == other.index
            and self.epoch == other.epoch
            and self.generation == other.generation
            and self.kind == other.kind
            and self.detail == other.detail
        )

    def __repr__(self):
        return (
            "Contradiction(index={!r}, epoch={!r}, generation={!r}, "
            "kind={!r}, detail={!r})".format(
                self.index,
                self.epoch,
                self.generation,
                self.kind,
                self.detail,
            )
        )


class ProvenValue:
    """A column value paired with the generation that proves it."""

    __slots__ = ("value", "generation")

    def __init__(self, value, generation):
        self.value = value
        self.generation = generation

    def __eq__(self, other):
        if not isinstance(other, ProvenValue):
            return NotImplemented
        return self.value == other.value and self.generation == other.generation

    def __repr__(self):
        return "ProvenValue(value={!r}, generation={!r})".format(
            self.value, self.generation
        )


class CertifiedRow:
    """A row assembled only from evidence sharing a common generation."""

    __slots__ = (
        "index",
        "epoch",
        "status",
        "columns",
        "status_generations",
        "certified_generation",
    )

    def __init__(
        self,
        index,
        epoch,
        status_generations,
        columns=None,
        status=None,
        certified_generation=None,
    ):
        self.index = index
        self.epoch = epoch
        self.status = status
        self.columns = columns if columns is not None else {}
        self.status_generations = status_generations
        self.certified_generation = certified_generation

    @property
    def status_name(self):
        if self.status is None:
            return None
        return STATUS_NAMES[self.status]

    def as_config(self):
        """Return the certifiable column mapping plus proven RowStatus."""
        config = {column: proven.value for column, proven in self.columns.items()}
        config["rowStatus"] = self.status
        return config

    def __repr__(self):
        return "CertifiedRow(index={!r}, epoch={!r}, status={!r}, columns={!r})".format(
            self.index, self.epoch, self.status_name, self.columns
        )


class PendingRow:
    """A row that cannot be published yet, with the evidence still missing.

    ``reason`` is one of:

    * ``missing-status``: no readable RowStatus was ever observed, so the
      row must not be assumed active;
    * ``missing-columns``: a readable state exists but one or more required
      columns cannot be proven in the same generation;
    * ``not-active``: every required column is known but the proven state
      is not publishable (e.g. ``notInService`` / ``notReady``).
    """

    __slots__ = ("index", "epoch", "reason", "missing_columns", "row")

    def __init__(self, index, epoch, reason, missing_columns=(), row=None):
        self.index = index
        self.epoch = epoch
        self.reason = reason
        self.missing_columns = tuple(missing_columns)
        self.row = row

    def __repr__(self):
        return (
            "PendingRow(index={!r}, epoch={!r}, reason={!r}, "
            "missing_columns={!r})".format(
                self.index, self.epoch, self.reason, self.missing_columns
            )
        )


class CertificationReport:
    """Result of :meth:`RowCollector.certify`."""

    __slots__ = ("publishable", "pending_columns", "contradictions", "_observations")

    def __init__(self, publishable, pending_columns, contradictions, observations):
        self.publishable = tuple(publishable)
        self.pending_columns = tuple(pending_columns)
        self.contradictions = tuple(contradictions)
        self._observations = observations

    @property
    def observations(self):
        """Full raw evidence retained for every audit of this report."""
        return tuple(self._observations)

    def __bool__(self):
        return bool(self.publishable)

    def __repr__(self):
        return (
            "CertificationReport(publishable={!r}, pending_columns={!r}, "
            "contradictions={!r})".format(
                self.publishable, self.pending_columns, self.contradictions
            )
        )
