from smb.dircollect import (
    DirectoryCollection,
    DirectoryEntry,
    EnumerationPage,
    MissWindow,
    diff_collections,
)


def _entry(name, file_id=None, size=0, page=0, at=None):
    return DirectoryEntry(name, file_id=file_id, file_size=size, page_index=page, observed_at=at)


def _collection(pages_entries, version=None, started=None, finished=None):
    pages = [ EnumerationPage(i, entries, observed_at=(started + i if started is not None else None))
              for i, entries in enumerate(pages_entries) ]
    return DirectoryCollection('share', '\\dir', pages,
                               directory_version=version,
                               started_at=started, finished_at=finished)


def test_snapshot_only_with_server_directory_version():
    with_version = _collection([[ _entry('a.txt', file_id=1) ]], version=b'\x01\x02')
    assert with_version.is_snapshot
    assert with_version.miss_windows() == []

    without_version = _collection([[ _entry('a.txt', file_id=1) ]])
    assert not without_version.is_snapshot


def test_miss_windows_cover_page_boundaries():
    collection = _collection([
        [ _entry('a.txt', file_id=1, page=0) ],
        [ _entry('b.txt', file_id=2, page=1) ],
        [ _entry('c.txt', file_id=3, page=2) ],
    ], started=100.0, finished=102.0)
    windows = collection.miss_windows()
    assert windows == [ MissWindow(0, 1, started_at=100.0, ended_at=101.0),
                        MissWindow(1, 2, started_at=101.0, ended_at=102.0) ]


def test_single_page_unversioned_collection_reports_whole_scan_window():
    collection = _collection([[ _entry('a.txt') ]], started=100.0, finished=100.5)
    assert collection.miss_windows() == [ MissWindow(None, None, started_at=100.0, ended_at=100.5) ]


def test_pagination_coverage_is_preserved():
    page0 = [ _entry('a.txt', file_id=1, page=0), _entry('b.txt', file_id=2, page=0) ]
    page1 = [ _entry('c.txt', file_id=3, page=1) ]
    collection = _collection([page0, page1])
    assert [ p.entry_count for p in collection.pages ] == [ 2, 1 ]
    assert collection.pages[0].first_name == 'a.txt'
    assert collection.pages[0].last_name == 'b.txt'
    assert [ e.page_index for e in collection.entries ] == [ 0, 0, 1 ]


def test_rename_mid_scan_keeps_both_occurrences_with_explicit_identity():
    collection = _collection([
        [ _entry('old.txt', file_id=42, size=10, page=0) ],
        [ _entry('new.txt', file_id=42, size=10, page=1) ],
    ])
    names = [ e.filename for e in collection.entries ]
    assert names == [ 'old.txt', 'new.txt' ]  # both occurrences preserved
    pairs = collection.linked_renames()
    assert len(pairs) == 1
    old, new = pairs[0]
    assert old.filename == 'old.txt' and new.filename == 'new.txt'
    assert old.file_id == new.file_id == 42


def test_zero_or_none_file_id_is_not_a_stable_identity():
    assert not _entry('a.txt', file_id=None).has_stable_identity
    assert not _entry('a.txt', file_id=0).has_stable_identity
    assert _entry('a.txt', file_id=7).has_stable_identity


def test_diff_same_size_name_change_without_identity_is_not_a_rename():
    earlier = _collection([[ _entry('old.txt', size=100) ]])
    later = _collection([[ _entry('new.txt', size=100) ]])
    diff = diff_collections(earlier, later)
    assert diff.renames == []
    assert [ e.filename for e in diff.disappeared ] == [ 'old.txt' ]
    assert [ e.filename for e in diff.appeared ] == [ 'new.txt' ]


def test_diff_rename_detected_only_via_matching_file_id():
    earlier = _collection([[ _entry('old.txt', file_id=9, size=100),
                             _entry('gone.txt', size=5) ]])
    later = _collection([[ _entry('new.txt', file_id=9, size=100),
                           _entry('added.txt', size=5) ]])
    diff = diff_collections(earlier, later)
    assert len(diff.renames) == 1
    old, new = diff.renames[0]
    assert (old.filename, new.filename) == ('old.txt', 'new.txt')
    # 'gone.txt' -> 'added.txt' share a size but no identity: not a rename
    assert [ e.filename for e in diff.disappeared ] == [ 'gone.txt' ]
    assert [ e.filename for e in diff.appeared ] == [ 'added.txt' ]


def test_diff_unchanged_entries_are_matched():
    earlier = _collection([[ _entry('same.txt', file_id=3), _entry('plain.txt') ]])
    later = _collection([[ _entry('same.txt', file_id=3), _entry('plain.txt') ]])
    diff = diff_collections(earlier, later)
    assert diff.renames == [] and diff.appeared == [] and diff.disappeared == []


def test_diff_deleted_identity_is_disappeared_not_matched_by_name():
    earlier = _collection([[ _entry('a.txt', file_id=1) ]])
    later = _collection([[ _entry('a.txt') ]])  # same name, but no identity to confirm
    diff = diff_collections(earlier, later)
    assert diff.renames == []
    assert [ e.filename for e in diff.disappeared ] == [ 'a.txt' ]
    assert [ e.filename for e in diff.appeared ] == [ 'a.txt' ]
