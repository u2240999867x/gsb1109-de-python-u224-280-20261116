from __future__ import annotations

from typing import BinaryIO

from dissect.ntfs.capacity import (
    CapacityCensus,
    EvidenceSource,
    FileReference,
    Namespace,
    PathStatus,
)
from dissect.ntfs.ntfs import NTFS

ROOT = FileReference(5, 1)
DIR = FileReference(10, 1)
FILE = FileReference(20, 1)

MFT = EvidenceSource.MFT
INDEX = EvidenceSource.INDEX


def test_capacity_census_basic() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True, declared_links=1)
    census.add_entity(DIR, is_directory=True, declared_links=1)
    census.add_entity(FILE, declared_links=1)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32_AND_DOS, source=MFT)

    report = census.report()

    assert report.entity_count == 3
    assert report.link_count == 2
    assert report.visible_path_count == 2
    assert report.visible_paths == ("Dir", "Dir\\File.txt")
    assert report.missed_links_lower == 0
    assert report.missed_links_upper == 0

    entity = report.entity(FILE)
    assert entity.observed_links == 1
    assert entity.gap == 0
    assert entity.lower_bound == 1
    assert entity.upper_bound == 1
    assert entity.links[0].path == "Dir\\File.txt"
    assert entity.links[0].path_status is PathStatus.PROVEN


def test_capacity_census_folds_long_short_names() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE, declared_links=1)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="LONGFI~1.TXT", namespace=Namespace.DOS, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="LongFileName.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 1
    assert entity.gap == 0
    assert entity.links[0].names == ("LongFileName.txt", "LONGFI~1.TXT")
    assert entity.links[0].name == "LongFileName.txt"
    assert entity.links[0].path == "Dir\\LongFileName.txt"


def test_capacity_census_fold_requires_sufficient_evidence() -> None:
    other_dir = FileReference(11, 1)
    census = CapacityCensus(scanned_parents={ROOT, DIR, other_dir})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(other_dir, is_directory=True)
    census.add_entity(FILE, declared_links=3)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=other_dir, parent=ROOT, name="Other", namespace=Namespace.WIN32, source=MFT)
    # A lone short name is a link of its own
    census.add_name(child=FILE, parent=DIR, name="ALONE~1.TXT", namespace=Namespace.DOS, source=MFT)
    # A short and a long name in different parents never fold
    census.add_name(child=FILE, parent=DIR, name="PAIR~1.TXT", namespace=Namespace.DOS, source=MFT)
    census.add_name(child=FILE, parent=other_dir, name="Pair.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 3
    assert sorted(link.names for link in entity.links) == [
        ("ALONE~1.TXT",),
        ("PAIR~1.TXT",),
        ("Pair.txt",),
    ]


def test_capacity_census_fold_pairs_one_to_one() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE, declared_links=2)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="A~1.TXT", namespace=Namespace.DOS, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="B~1.TXT", namespace=Namespace.DOS, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="Alpha.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 2
    assert sorted(link.names for link in entity.links) == [("Alpha.txt", "A~1.TXT"), ("B~1.TXT",)]


def test_capacity_census_merges_mft_and_index_evidence() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE, declared_links=1)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=INDEX)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 1
    assert entity.links[0].sources == {MFT, INDEX}


def test_capacity_census_declared_gap_incomplete_coverage() -> None:
    unscanned_dir = FileReference(11, 1)
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(unscanned_dir, is_directory=True)
    census.add_entity(FILE, declared_links=3)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 1
    assert entity.gap == 2
    assert entity.possibly_missed == 2
    assert entity.phantom_links == 0
    assert entity.lower_bound == 1
    assert entity.upper_bound == 3

    assert report.unscanned_directories == (unscanned_dir,)
    assert report.missed_links_upper == 2
    # Declared counts never fabricate paths: only the observed name is visible
    assert report.visible_paths == ("Dir", "Dir\\File.txt")


def test_capacity_census_declared_gap_full_coverage_is_phantom() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE, declared_links=3)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.gap == 2
    assert entity.possibly_missed == 0
    assert entity.phantom_links == 2
    assert entity.lower_bound == 1
    assert entity.upper_bound == 1
    assert report.missed_links_upper == 0


def test_capacity_census_missing_parent_keeps_link() -> None:
    unknown_parent = FileReference(99, 1)
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE, declared_links=2)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=unknown_parent, name="OtherName.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    # The broken parent chain only affects its own path proof
    assert entity.observed_links == 2
    assert entity.gap == 0

    by_name = {link.name: link for link in entity.links}
    assert by_name["File.txt"].path == "Dir\\File.txt"
    assert by_name["File.txt"].path_status is PathStatus.PROVEN
    assert by_name["OtherName.txt"].path is None
    assert by_name["OtherName.txt"].path_status is PathStatus.MISSING_PARENT

    assert report.link_count == 3
    assert report.visible_paths == ("Dir", "Dir\\File.txt")


def test_capacity_census_cyclic_parent_chain() -> None:
    dir_a = FileReference(10, 1)
    dir_b = FileReference(11, 1)
    census = CapacityCensus()
    census.add_entity(dir_a, is_directory=True)
    census.add_entity(dir_b, is_directory=True)
    census.add_name(child=dir_a, parent=dir_b, name="A", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=dir_b, parent=dir_a, name="B", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    # Both links still count, only their path proofs fail
    assert report.link_count == 2
    assert report.visible_path_count == 0
    for entity in report.entities:
        assert entity.links[0].path is None
        assert entity.links[0].path_status is PathStatus.CYCLE


def test_capacity_census_unnamed_parent() -> None:
    parent = FileReference(11, 1)
    census = CapacityCensus()
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(parent, is_directory=True)
    census.add_entity(FILE, declared_links=1)
    census.add_name(child=FILE, parent=parent, name="File.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.observed_links == 1
    assert entity.links[0].path is None
    assert entity.links[0].path_status is PathStatus.UNNAMED_PARENT


def test_capacity_census_sequence_reuse_is_stale() -> None:
    current = FileReference(30, 2)
    previous = FileReference(30, 1)
    census = CapacityCensus(scanned_parents={ROOT})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(current, declared_links=1)
    # An observation for a previous incarnation of the same segment
    census.add_name(child=previous, parent=ROOT, name="old.txt", namespace=Namespace.WIN32, source=INDEX)

    report = census.report()

    # Sequence number reuse cannot fill the old relation
    assert report.link_count == 0
    assert len(report.stale_names) == 1
    assert report.stale_names[0].child == previous
    assert report.stale_names[0].name == "old.txt"

    entity = report.entity(current)
    assert entity.observed_links == 0
    assert entity.gap == 1
    assert entity.phantom_links == 1


def test_capacity_census_stale_parent_reference() -> None:
    stale_parent = FileReference(40, 1)
    current_parent = FileReference(40, 3)
    census = CapacityCensus(scanned_parents={ROOT, stale_parent})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(current_parent, is_directory=True)
    census.add_entity(FILE, declared_links=1)
    census.add_name(child=FILE, parent=stale_parent, name="file.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    # The link still counts, but the stale parent reference proves no path
    assert entity.observed_links == 1
    assert entity.links[0].path is None
    assert entity.links[0].path_status is PathStatus.MISSING_PARENT
    # A scanned mark for the old sequence number does not cover the current one
    assert report.unscanned_directories == (current_parent,)


def test_capacity_census_unattributed_names() -> None:
    census = CapacityCensus(scanned_parents={ROOT})
    census.add_entity(ROOT, is_directory=True)
    census.add_name(child=FileReference(77, 7), parent=ROOT, name="ghost.txt", namespace=Namespace.WIN32, source=INDEX)

    report = census.report()

    assert report.link_count == 0
    assert len(report.unattributed_names) == 1
    assert report.unattributed_names[0].name == "ghost.txt"


def test_capacity_census_undeclared_entity() -> None:
    census = CapacityCensus(scanned_parents={ROOT, DIR})
    census.add_entity(ROOT, is_directory=True)
    census.add_entity(DIR, is_directory=True)
    census.add_entity(FILE)
    census.add_name(child=DIR, parent=ROOT, name="Dir", namespace=Namespace.WIN32, source=MFT)
    census.add_name(child=FILE, parent=DIR, name="File.txt", namespace=Namespace.WIN32, source=MFT)

    report = census.report()

    entity = report.entity(FILE)
    assert entity.declared_links is None
    assert entity.gap is None
    assert entity.lower_bound == 1
    assert entity.upper_bound == 1


def test_capacity_census_from_mft(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)
    report = CapacityCensus.from_ntfs(fs).report()

    assert report.entity_count > 0
    assert report.link_count >= report.visible_path_count
    assert report.missed_links_lower == 0
    assert report.missed_links_upper == sum(entity.possibly_missed for entity in report.entities)

    for entity in report.entities:
        assert entity.observed_links == len(entity.links)
        assert entity.lower_bound <= entity.upper_bound
        assert entity.upper_bound == entity.observed_links + entity.possibly_missed
        for link in entity.links:
            assert (link.path is None) == (link.path_status is not PathStatus.PROVEN)

    for expected in ("File.txt", "Large.txt", "Directory", "Directory\\File 1.txt", "Directory\\File 2.txt"):
        assert expected in report.visible_paths


def test_capacity_census_from_mft_no_index_scan(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)
    report = CapacityCensus.from_mft(fs.mft, index_parents=[]).report()

    assert report.scanned_parents == frozenset()
    assert report.unscanned_directories
    # Names from the MFT alone still produce links and proven paths
    assert report.link_count > 0
    assert "Directory\\File 1.txt" in report.visible_paths
