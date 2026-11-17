"""Regression tests for MibTableRow index caches."""

from pysnmp.smi.builder import MibBuilder


def _table_row():
    mib_builder = MibBuilder()
    mib_builder.loadTexts = True
    (MibTableRow, MibTableColumn) = mib_builder.import_symbols(
        "SNMPv2-SMI", "MibTableRow", "MibTableColumn"
    )
    (DisplayString,) = mib_builder.import_symbols("SNMPv2-TC", "DisplayString")

    base = (1, 3, 6, 1, 4, 1, 99999, 2)
    row = MibTableRow(base + (1,))
    row.setIndexNames((0, "TEST-INDEX-CACHE-MIB", "cacheIndex"))
    column = MibTableColumn(base + (1, 1), DisplayString())
    mib_builder.export_symbols(
        "TEST-INDEX-CACHE-MIB", cacheIndex=column, cacheEntry=row
    )
    row.registerSubtrees(column)
    return row


def test_indices_cache_is_keyed_by_the_whole_instance_id():
    row = _table_row()

    first = row.getIndicesFromInstId((3, ord("a"), ord("b"), ord("c")))
    second = row.getIndicesFromInstId((3, ord("x"), ord("y"), ord("z")))

    assert [index.prettyPrint() for index in first] == ["abc"]
    assert [index.prettyPrint() for index in second] == ["xyz"]


def test_empty_instance_id_does_not_return_another_rows_indices():
    row = _table_row()

    row.getIndicesFromInstId((3, ord("a"), ord("b"), ord("c")))

    assert row.getIndicesFromInstId(()) != ("abc",)
