"""
Tests for RowStatus-driven MIB table row certification.

The collector may read a row's RowStatus column and its other columns in
different polling windows.  These tests pin the evidence rules:

* only rows proven ``active`` with every required column in one common
  generation are publishable;
* ``createAndWait`` / ``notReady`` / ``notInService`` rows are not ready;
* missing required columns and missing status evidence stay pending rather
  than being guessed;
* ``destroy`` followed by a re-creation at the same index starts a fresh
  lifetime that cannot inherit the previous row's columns;
* write-only RowStatus values returned on retrieval and conflicting claims
  in one generation are reported as contradictions;
* every raw observation is retained for later audits and re-judgment.
"""

import pytest

from pysnmp.smi import rowcert
from pysnmp.smi.rowcert import (
    ACTIVE,
    CREATE_AND_WAIT,
    DESTROY,
    NOT_IN_SERVICE,
    NOT_READY,
    RowCollector,
    UnknownStatusError,
)

REQUIRED = ("colA", "colB")
INDEX = (1, 2)


def _pending_for(report, index):
    matches = [row for row in report.pending_columns if row.index == index]
    assert len(matches) == 1
    return matches[0]


def test_active_row_with_all_required_columns_in_one_generation_publishes():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert len(report.publishable) == 1
    row = report.publishable[0]
    assert row.index == INDEX
    assert row.status == ACTIVE
    assert row.status_name == "active"
    assert row.certified_generation == 1
    assert row.columns["colA"].value == "alpha"
    assert row.columns["colB"].generation == 1
    assert row.as_config() == {
        "colA": "alpha",
        "colB": "beta",
        "rowStatus": ACTIVE,
    }
    assert report.pending_columns == ()
    assert report.contradictions == ()
    assert bool(report) is True


def test_create_and_wait_is_not_a_ready_state():
    collector = RowCollector(required_columns=REQUIRED)
    # createAndWait is a write-only SET value; an agent echoing it back on
    # retrieval violates the state machine, and even a following notReady
    # negotiated creation must not be published.
    collector.record_status(INDEX, 1, CREATE_AND_WAIT)
    collector.record_status(INDEX, 1, NOT_READY)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.reason == "not-active"
    assert pending.row.status == NOT_READY
    assert [claim.kind for claim in report.contradictions] == [
        "write-only status returned on retrieval"
    ]
    assert report.contradictions[0].detail == "createAndWait"


@pytest.mark.parametrize("state", [NOT_READY, NOT_IN_SERVICE])
def test_non_active_readable_states_are_not_published(state):
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, state)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.reason == "not-active"
    assert pending.missing_columns == ()
    assert pending.row.status == state


def test_missing_required_column_blocks_publication_and_names_it():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 1, "colA", "alpha")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.reason == "missing-columns"
    assert pending.missing_columns == ("colB",)


def test_status_and_columns_from_different_windows_are_not_combined():
    collector = RowCollector(required_columns=REQUIRED)
    # Status proven in generation 1, columns only in generation 2: there is
    # no generation proving both facts together, so nothing is publishable.
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 2, "colA", "alpha")
    collector.record_column(INDEX, 2, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.reason == "missing-columns"
    assert set(pending.missing_columns) == {"colA", "colB"}


def test_supplementary_poll_re_judges_row_at_common_generation():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 2, "colA", "alpha")
    collector.record_column(INDEX, 2, "colB", "beta")
    first_report = collector.certify()
    assert first_report.publishable == ()

    # Follow-up poll reads status and columns together in one window.
    collector.record_status(INDEX, 3, ACTIVE)
    collector.record_column(INDEX, 3, "colA", "alpha-2")
    collector.record_column(INDEX, 3, "colB", "beta-2")
    second_report = collector.certify()

    assert len(second_report.publishable) == 1
    row = second_report.publishable[0]
    assert row.certified_generation == 3
    assert row.columns["colA"].value == "alpha-2"
    assert second_report.pending_columns == ()
    # All original samples remain available for audit.
    assert len(second_report.observations) == 6
    assert len(collector.observations_for(INDEX)) == 6


def test_destroyed_row_recreated_at_same_index_does_not_inherit_fields():
    collector = RowCollector(required_columns=REQUIRED)

    # First lifetime: complete, active row.
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 1, "colA", "old-a")
    collector.record_column(INDEX, 1, "colB", "old-b")

    # destroy ends the first lifetime.
    collector.record_status(INDEX, 2, DESTROY)

    # Second lifetime at the same index: negotiated creation, only colA is
    # re-supplied.  colB from the destroyed row must not be inherited.
    collector.record_status(INDEX, 3, NOT_READY)
    collector.record_column(INDEX, 3, "colA", "new-a")

    report = collector.certify()

    # The old, independently proven row remains on record.
    assert len(report.publishable) == 1
    assert report.publishable[0].epoch == 0
    assert report.publishable[0].columns["colB"].value == "old-b"

    pending = [row for row in report.pending_columns if row.epoch == 1]
    assert len(pending) == 1
    assert pending[0].reason == "missing-columns"
    assert pending[0].missing_columns == ("colB",)
    # The fresh lifetime only knows colA; old-b is not carried over.
    new_epoch_rows = [row for row in report.publishable if row.epoch == 1]
    assert new_epoch_rows == []

    destroy_claims = [
        claim for claim in report.contradictions if claim.detail == "destroy"
    ]
    assert len(destroy_claims) == 1
    assert destroy_claims[0].generation == 2


def test_recreated_row_becomes_publishable_only_with_own_evidence():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 1, "colA", "old-a")
    collector.record_column(INDEX, 1, "colB", "old-b")
    collector.record_status(INDEX, 2, DESTROY)
    collector.record_status(INDEX, 3, NOT_IN_SERVICE)
    collector.record_column(INDEX, 3, "colA", "new-a")

    # Supplementary poll completes the new lifetime and activates it.
    collector.record_status(INDEX, 4, ACTIVE)
    collector.record_column(INDEX, 4, "colA", "new-a")
    collector.record_column(INDEX, 4, "colB", "new-b")

    report = collector.certify()

    epochs = {row.epoch: row for row in report.publishable}
    assert set(epochs) == {0, 1}
    assert epochs[1].columns["colA"].value == "new-a"
    assert epochs[1].columns["colB"].value == "new-b"
    assert epochs[1].certified_generation == 4


def test_row_without_status_evidence_is_not_assumed_active():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.reason == "missing-status"
    assert pending.row.status is None
    assert pending.row.status_name is None


def test_conflicting_status_values_in_one_generation_are_reported():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_status(INDEX, 1, NOT_IN_SERVICE)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    claims = [
        claim
        for claim in report.contradictions
        if claim.kind == "conflicting RowStatus values in one generation"
    ]
    assert len(claims) == 1
    assert claims[0].index == INDEX
    assert claims[0].generation == 1
    assert claims[0].detail == ("active", "notInService")


def test_conflicting_column_values_in_one_generation_block_proof():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, ACTIVE)
    collector.record_column(INDEX, 1, "colA", "alpha")
    collector.record_column(INDEX, 1, "colA", "ALPHA")
    collector.record_column(INDEX, 1, "colB", "beta")

    report = collector.certify()

    assert report.publishable == ()
    pending = _pending_for(report, INDEX)
    assert pending.missing_columns == ("colA",)
    claims = [
        claim
        for claim in report.contradictions
        if claim.kind == "conflicting column values in one generation"
    ]
    assert len(claims) == 1
    assert claims[0].detail == ("colA", ("alpha", "ALPHA"))


def test_raw_observations_are_retained_even_when_nothing_certifies():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status(INDEX, 1, CREATE_AND_WAIT)
    collector.record_column(INDEX, 1, "colA", "alpha")

    report = collector.certify()

    assert report.publishable == ()
    assert len(report.observations) == 2
    assert [obs.generation for obs in report.observations] == [1, 1]
    status_observation = report.observations[0]
    assert status_observation.is_status is True
    assert status_observation.status == CREATE_AND_WAIT
    assert report.observations[1].column == "colA"


def test_indexes_are_certified_independently():
    collector = RowCollector(required_columns=REQUIRED)
    collector.record_status((1,), 1, ACTIVE)
    collector.record_column((1,), 1, "colA", "a1")
    collector.record_column((1,), 1, "colB", "b1")

    collector.record_status((2,), 1, ACTIVE)
    collector.record_column((2,), 1, "colA", "a2")

    report = collector.certify()

    assert [row.index for row in report.publishable] == [(1,)]
    pending = _pending_for(report, (2,))
    assert pending.missing_columns == ("colB",)


def test_unknown_row_status_value_raises():
    collector = RowCollector(required_columns=REQUIRED)
    with pytest.raises(UnknownStatusError):
        collector.record_status(INDEX, 1, 9)
    with pytest.raises(UnknownStatusError):
        collector.record_status(INDEX, 1, "active")


def test_status_constants_match_rowstatus_textual_convention():
    from pysnmp.smi.builder import MibBuilder

    mib_builder = MibBuilder()
    (row_status,) = mib_builder.import_symbols("SNMPv2-TC", "RowStatus")

    assert rowcert.ACTIVE == row_status.stActive
    assert rowcert.NOT_READY == row_status.stNotReady
    assert rowcert.NOT_IN_SERVICE == row_status.stNotInService
    assert rowcert.CREATE_AND_GO == row_status.stCreateAndGo
    assert rowcert.CREATE_AND_WAIT == row_status.stCreateAndWait
    assert rowcert.DESTROY == row_status.stDestroy
