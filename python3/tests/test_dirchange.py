from smb.dirchange import (
    ACTION_ADDED, ACTION_REMOVED, ACTION_MODIFIED,
    ACTION_RENAMED_OLD_NAME, ACTION_RENAMED_NEW_NAME,
    LINK_CONTIGUOUS, LINK_OVERFLOW, LINK_DISCONNECTED,
    DirectoryChangeTracker, NotificationEvent, RescanRange,
)

KNOWN = [
    ('/', True),
    ('/alpha', True),
    ('/alpha/a.txt', False),
    ('/alpha/old.txt', False),
    ('/beta', True),
    ('/beta/b.txt', False),
    ('/gamma', True),
    ('/gamma/g.txt', False),
]


def make_tracker():
    return DirectoryChangeTracker(KNOWN, watch_root='/')


def ev(seq, action, path, is_dir=False, link=LINK_CONTIGUOUS, session='s1'):
    return NotificationEvent(session, seq, action, path, is_dir, link)


def test_contiguous_rename_pair_same_dir():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/old.txt'),
        ev(2, ACTION_RENAMED_NEW_NAME, '/alpha/new.txt'),
    ])
    assert not tracker.tree.contains('/alpha/old.txt')
    assert tracker.tree.contains('/alpha/new.txt')
    assert gaps == []
    assert tracker.is_confirmed('/alpha')


def test_unpaired_old_name_is_not_a_deletion():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/old.txt'),
        ev(2, ACTION_ADDED, '/alpha/other.txt'),
    ])
    # The old entry must be kept: the unseen new name is not a deletion.
    assert tracker.tree.contains('/alpha/old.txt')
    assert tracker.tree.contains('/alpha/other.txt')
    assert RescanRange('/alpha', False, 'rename_old_unpaired') in gaps
    assert not tracker.is_confirmed('/alpha/a.txt')


def test_unpaired_new_name_marks_target_uncertain():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_NEW_NAME, '/beta/arrived.txt'),
    ])
    assert tracker.tree.contains('/beta/arrived.txt')
    assert RescanRange('/beta', False, 'rename_new_unpaired') in gaps


def test_overflow_keeps_uncertain_region_and_blocks_pairing():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/old.txt'),
        # Buffer overflowed before seq 2: the new name was lost.
        ev(2, ACTION_RENAMED_NEW_NAME, '/beta/new.txt', link=LINK_OVERFLOW),
    ])
    # No pairing across the overflow; the old name survives.
    assert tracker.tree.contains('/alpha/old.txt')
    assert RescanRange('/', True, LINK_OVERFLOW) in gaps
    # Nothing under the watch root is deliverable until the rescan.
    assert not tracker.is_confirmed('/gamma/g.txt')


def test_disconnect_keeps_uncertain_region():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_REMOVED, '/alpha/a.txt', link=LINK_DISCONNECTED),
    ])
    assert not tracker.tree.contains('/alpha/a.txt')
    assert RescanRange('/', True, LINK_DISCONNECTED) in gaps


def test_cross_dir_move_obligations_are_independent():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/old.txt'),
        ev(2, ACTION_RENAMED_NEW_NAME, '/beta/old.txt'),
    ])
    assert not tracker.tree.contains('/alpha/old.txt')
    assert tracker.tree.contains('/beta/old.txt')
    assert RescanRange('/alpha', False, 'cross_dir_move_source') in gaps
    assert RescanRange('/beta', False, 'cross_dir_move_target') in gaps

    # Rescanning the source side must not close the target side's gap.
    remaining = tracker.close_rescan('/alpha', recursive=True)
    assert RescanRange('/beta', False, 'cross_dir_move_target') in remaining
    assert not any(ob.path == '/alpha' for ob in remaining)

    # Unrelated confirmed subtrees are still deliverable throughout.
    assert tracker.is_confirmed('/gamma/g.txt')

    remaining = tracker.close_rescan('/beta', recursive=True)
    assert remaining == []


def test_added_dir_requires_recursive_rescan():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_ADDED, '/alpha/sub', is_dir=True),
    ])
    assert tracker.tree.contains('/alpha/sub')
    assert RescanRange('/alpha/sub', True, 'added_dir') in gaps
    assert not tracker.is_confirmed('/alpha/sub')
    # Siblings of the new directory stay confirmed.
    assert tracker.is_confirmed('/alpha/a.txt')


def test_removed_dir_removes_subtree():
    tracker = make_tracker()
    tracker.apply([ev(1, ACTION_REMOVED, '/alpha', is_dir=True)])
    assert not tracker.tree.contains('/alpha')
    assert not tracker.tree.contains('/alpha/a.txt')


def test_modified_does_not_open_gaps():
    tracker = make_tracker()
    gaps = tracker.apply([ev(1, ACTION_MODIFIED, '/alpha/a.txt')])
    assert gaps == []
    assert tracker.is_confirmed('/alpha/a.txt')


def test_pairing_is_per_session():
    tracker = make_tracker()
    gaps = tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/old.txt', session='s1'),
        ev(1, ACTION_RENAMED_NEW_NAME, '/alpha/new.txt', session='s2'),
    ])
    # OLD and NEW arrived on different sessions: never paired.
    assert tracker.tree.contains('/alpha/old.txt')
    assert tracker.tree.contains('/alpha/new.txt')
    assert RescanRange('/alpha', False, 'rename_old_unpaired') in gaps
    assert not tracker.is_confirmed('/alpha/a.txt')


def test_dir_move_carries_subtree():
    tracker = DirectoryChangeTracker(
        [('/', True), ('/alpha', True), ('/alpha/sub', True),
         ('/alpha/sub/f.txt', False), ('/beta', True)],
        watch_root='/')
    tracker.apply([
        ev(1, ACTION_RENAMED_OLD_NAME, '/alpha/sub', is_dir=True),
        ev(2, ACTION_RENAMED_NEW_NAME, '/beta/sub', is_dir=True),
    ])
    assert tracker.tree.contains('/beta/sub/f.txt')
    assert not tracker.tree.contains('/alpha/sub')
