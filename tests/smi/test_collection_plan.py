"""Tests for MIB-driven supplementary collection planning."""

import pytest

from pysnmp.smi import builder, view
from pysnmp.smi.collection import (
    CollectionField,
    CollectionPlanner,
    ConfirmedVariable,
    build_collection_plan,
)

BASE = (1, 3, 6, 1, 4, 1, 99999, 1)
ENTRY = BASE + (1,)
INDEX = ENTRY + (1,)
LABEL = ENTRY + (2,)
COUNTER = ENTRY + (3,)
STATUS = ENTRY + (4,)
SCALAR = BASE + (3,)


def _mib_view():
    mib_builder = builder.MibBuilder()
    mib_builder.loadTexts = True

    (MibTable, MibTableRow, MibTableColumn, MibScalar) = mib_builder.import_symbols(
        "SNMPv2-SMI", "MibTable", "MibTableRow", "MibTableColumn", "MibScalar"
    )
    (Integer32,) = mib_builder.import_symbols("SNMPv2-SMI", "Integer32")
    (OctetString,) = mib_builder.import_symbols("SNMPv2-TC", "DisplayString")
    (RowStatus,) = mib_builder.import_symbols("SNMPv2-TC", "RowStatus")

    mib_builder.export_symbols(
        "COLLECTION-TEST-MIB",
        ctTable=MibTable(BASE),
        ctEntry=MibTableRow(ENTRY)
        .setMaxAccess("not-accessible")
        .setIndexNames((0, "COLLECTION-TEST-MIB", "ctIndex")),
        ctIndex=MibTableColumn(INDEX, Integer32()).setMaxAccess("read-only"),
        ctLabel=MibTableColumn(LABEL, OctetString()).setMaxAccess("read-only"),
        ctCounter=MibTableColumn(COUNTER, Integer32())
        .setUnits("packets")
        .setMaxAccess("read-only"),
        ctStatus=MibTableColumn(STATUS, RowStatus()).setMaxAccess("read-create"),
        ctScalar=MibScalar(SCALAR, Integer32()).setMaxAccess("read-only"),
    )

    return view.MibViewController(mib_builder)


def test_shared_index_and_row_status_requested_once():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [
            CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a"),
            CollectionField("COLLECTION-TEST-MIB", "ctLabel", (7,), "rev-a"),
        ],
    )

    oids = [request.oid for request in plan.requests]

    assert len(oids) == len(set(oids))
    assert COUNTER + (7,) in oids
    assert LABEL + (7,) in oids
    assert INDEX + (7,) in oids
    assert STATUS + (7,) in oids
    assert oids.count(STATUS + (7,)) == 1


def test_units_come_from_mib_and_never_become_requests():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a")],
    )

    assert plan.units["COLLECTION-TEST-MIB::ctCounter"] == "packets"
    assert all(request.reason != "units" for request in plan.requests)


def test_row_status_is_derived_only_for_table_columns():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctScalar", (), "rev-a")],
    )

    assert [request.oid for request in plan.requests] == [SCALAR + (0,)]
    assert plan.metadata[0].index_refs == ()
    assert plan.metadata[0].row_status_ref is None


def test_confirmed_compatible_dependency_is_not_requested():
    mib_view = _mib_view()
    revision = CollectionPlanner(mib_view)._module_revision("COLLECTION-TEST-MIB")

    confirmed = [
        ConfirmedVariable(
            STATUS + (7,), 1, "rev-a", mib_revision=revision, syntax="RowStatus"
        ),
        ConfirmedVariable(LABEL + (7,), "core-1", "rev-a"),
    ]

    plan = build_collection_plan(
        mib_view,
        [
            CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a"),
            CollectionField("COLLECTION-TEST-MIB", "ctLabel", (7,), "rev-a"),
        ],
        confirmed=confirmed,
    )

    oids = [request.oid for request in plan.requests]
    assert STATUS + (7,) not in oids
    assert LABEL + (7,) not in oids
    assert {variable.oid for variable in plan.reused} == {
        STATUS + (7,),
        LABEL + (7,),
    }


def test_confirmed_value_from_other_revision_is_not_reused():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a")],
        confirmed=[
            ConfirmedVariable(
                STATUS + (7,),
                1,
                "rev-a",
                mib_revision="1999-01-01 00:00",
                syntax="RowStatus",
            )
        ],
    )

    assert STATUS + (7,) in [request.oid for request in plan.requests]


def test_row_status_from_other_generation_is_not_reused():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-b")],
        confirmed=[ConfirmedVariable(STATUS + (7,), 1, "rev-a")],
    )

    assert STATUS + (7,) in [request.oid for request in plan.requests]


def test_rows_are_not_spliced_across_generations():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [
            CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a"),
            CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-b"),
        ],
    )

    result = plan.resolve({COUNTER + (7,): 42}, generation="rev-a")

    old = result.get("rev-a", (7,))
    new = result.get("rev-b", (7,))

    assert old is not None and old.complete
    assert old.get("COLLECTION-TEST-MIB::ctCounter") == 42
    assert new is not None and not new.complete
    assert "COLLECTION-TEST-MIB::ctCounter" in new.missing


def test_incompatible_syntax_is_not_reused():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a")],
        confirmed=[
            ConfirmedVariable(COUNTER + (7,), 42, "rev-a", syntax="OctetString")
        ],
    )

    assert COUNTER + (7,) in [request.oid for request in plan.requests]


def test_plan_rejects_unresolvable_fields():
    mib_view = _mib_view()

    with pytest.raises(Exception):
        build_collection_plan(
            mib_view,
            [CollectionField("COLLECTION-TEST-MIB", "ctMissing", (7,), "rev-a")],
        )


def test_varbind_inputs_are_supported():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [("COLLECTION-TEST-MIB", "ctCounter", 7)],
        generation="rev-a",
    )

    assert [request.oid for request in plan.requests] == [
        COUNTER + (7,),
        INDEX + (7,),
        STATUS + (7,),
    ]


def test_index_cache_does_not_leak_between_rows():
    mib_view = _mib_view()

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (7,), "rev-a")],
    )
    metadata = plan.metadata[0]

    assert metadata.index_refs[0].oid == INDEX + (7,)

    plan = build_collection_plan(
        mib_view,
        [CollectionField("COLLECTION-TEST-MIB", "ctCounter", (9,), "rev-a")],
    )
    metadata = plan.metadata[0]

    assert metadata.index_refs[0].oid == INDEX + (9,)
