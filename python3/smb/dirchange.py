"""
Directory collection driven by SMB change notifications (SMB2 CHANGE_NOTIFY).

Instead of rescanning the whole tree, a collector feeds the notification
stream into a :class:`DirectoryChangeTracker` which incrementally updates a
known directory tree and tracks the minimal set of ranges that must be
rescanned.

Correctness rules implemented here:

- A rename's OLD_NAME / NEW_NAME pair is matched only inside a provably
  contiguous notification segment of the same session.  A buffer overflow
  or a session disconnect breaks the segment, so names seen on opposite
  sides of the break are never paired.
- After an overflow or disconnect the missed events leave an uncertain
  region.  The region is kept open as a rescan obligation; an OLD_NAME
  whose NEW_NAME was never seen is *not* interpreted as a deletion.
- A cross-directory move affects both the source and the target parent.
  Each side gets its own independent rescan obligation: completing the
  rescan of one side never closes the other side's gap.
- Subtrees that no open obligation covers stay confirmed and may keep
  being delivered to consumers while the gaps are being rescanned.
"""

# Notify actions, matching the SMB2 FILE_ACTION_* values.
ACTION_ADDED = 0x0001
ACTION_REMOVED = 0x0002
ACTION_MODIFIED = 0x0003
ACTION_RENAMED_OLD_NAME = 0x0004
ACTION_RENAMED_NEW_NAME = 0x0005

# Integrity of the link from the previous event of the same session.
LINK_CONTIGUOUS = 'contiguous'      # provably no event was lost
LINK_OVERFLOW = 'overflow'          # notification buffer overflowed
LINK_DISCONNECTED = 'disconnected'  # session dropped and reconnected


def _normalize(path):
    p = path.replace('\\', '/')
    if not p.startswith('/'):
        p = '/' + p
    if len(p) > 1:
        p = p.rstrip('/')
    return p


def _parent(path):
    if path == '/':
        return None
    idx = path.rfind('/')
    return path[:idx] if idx > 0 else '/'


class NotificationEvent:
    """
    One change-notification event.

    ``link`` describes the integrity of the buffer segment between this
    event and the previous event of the same session: LINK_CONTIGUOUS,
    LINK_OVERFLOW or LINK_DISCONNECTED.  The first event of a session is
    normally LINK_CONTIGUOUS.
    """

    def __init__(self, session_id, sequence, action, path, is_dir=False,
                 link=LINK_CONTIGUOUS):
        self.session_id = session_id
        self.sequence = sequence
        self.action = action
        self.path = _normalize(path)
        self.is_dir = bool(is_dir)
        self.link = link

    def __repr__(self):
        return 'NotificationEvent(session=%r, seq=%r, action=%r, path=%r, link=%r)' % (
            self.session_id, self.sequence, self.action, self.path, self.link)


class RescanRange:
    """
    A range of the tree that must be rescanned before it can be delivered.

    A non-recursive range at ``/a/b`` covers the immediate listing of
    ``/a/b`` (its direct children).  A recursive range covers ``/a/b``
    itself and every descendant.
    """

    def __init__(self, path, recursive, reason):
        self.path = _normalize(path)
        self.recursive = bool(recursive)
        self.reason = reason

    def covers(self, path):
        p = _normalize(path)
        if self.recursive:
            return p == self.path or p.startswith(self.path.rstrip('/') + '/') or self.path == '/'
        return _parent(p) == self.path

    def subsumes(self, other):
        if self.recursive:
            return self.covers(other.path)
        return not other.recursive and self.path == other.path

    def __repr__(self):
        return 'RescanRange(%s, recursive=%r, reason=%r)' % (
            self.path, self.recursive, self.reason)

    def __eq__(self, other):
        return (isinstance(other, RescanRange)
                and self.path == other.path
                and self.recursive == other.recursive
                and self.reason == other.reason)

    def __hash__(self):
        return hash((self.path, self.recursive, self.reason))


class DirectoryTree:
    """Flat name tree of known entries: maps normalized path -> is_dir."""

    def __init__(self, entries=()):
        self.entries = {}
        for path, is_dir in entries:
            self.add(path, is_dir)

    def add(self, path, is_dir=False):
        self.entries[_normalize(path)] = bool(is_dir)

    def remove(self, path):
        p = _normalize(path)
        prefix = p.rstrip('/') + '/'
        for key in [k for k in self.entries if k == p or k.startswith(prefix)]:
            del self.entries[key]

    def move(self, old_path, new_path):
        old = _normalize(old_path)
        new = _normalize(new_path)
        if old not in self.entries:
            self.add(new, False)
            return
        prefix = old.rstrip('/') + '/'
        moved = [(k, v) for k, v in self.entries.items()
                 if k == old or k.startswith(prefix)]
        for key, _ in moved:
            del self.entries[key]
        for key, is_dir in moved:
            self.entries[new + key[len(old):]] = is_dir

    def contains(self, path):
        return _normalize(path) in self.entries

    def is_dir(self, path):
        return self.entries.get(_normalize(path), False)

    def __repr__(self):
        return 'DirectoryTree(%r)' % (sorted(self.entries),)


class DirectoryChangeTracker:
    """
    Applies session-ordered notification events to a known directory tree
    and tracks the rescan obligations produced by uncertain changes.

    Usage::

        tracker = DirectoryChangeTracker(known_entries, watch_root='/')
        tracker.apply(events)            # updates tracker.tree
        gaps = tracker.open_rescans()    # ranges that must be rescanned
        ... rescan one of the ranges ...
        tracker.close_rescan(path)       # closes only obligations it covers
        tracker.is_confirmed(path)       # unrelated subtrees stay deliverable
    """

    def __init__(self, known_entries=(), watch_root='/'):
        self.tree = (known_entries if isinstance(known_entries, DirectoryTree)
                     else DirectoryTree(known_entries))
        self.watch_root = _normalize(watch_root)
        self._obligations = []

    def apply(self, events):
        """
        Apply a batch of events.  Events are grouped per session and each
        session is replayed in sequence order; rename pairing never crosses
        a session boundary or a broken buffer link.
        """
        by_session = {}
        for ev in events:
            by_session.setdefault(ev.session_id, []).append(ev)
        for session_id in sorted(by_session, key=str):
            chain = sorted(by_session[session_id], key=lambda e: e.sequence)
            self._apply_session(chain)
        return self.open_rescans()

    def open_rescans(self):
        """The minimal list of ranges that still must be rescanned."""
        return list(self._obligations)

    def close_rescan(self, path, recursive=True):
        """
        Mark a completed rescan of ``path``.  Only obligations fully covered
        by that rescan are closed; obligations on any other range (e.g. the
        other side of a cross-directory move) stay open.
        """
        done = RescanRange(path, recursive, reason='rescan_completed')
        self._obligations = [ob for ob in self._obligations
                             if not done.subsumes(ob)]
        return self.open_rescans()

    def is_confirmed(self, path):
        """True when no open obligation covers ``path``."""
        return not any(ob.covers(path) for ob in self._obligations)

    def _add_obligation(self, path, recursive, reason):
        new = RescanRange(path, recursive, reason)
        if any(ob.subsumes(new) or ob == new for ob in self._obligations):
            return
        self._obligations = [ob for ob in self._obligations
                             if not new.subsumes(ob)]
        self._obligations.append(new)

    def _apply_session(self, chain):
        pending_old = None
        for ev in chain:
            if ev.link != LINK_CONTIGUOUS:
                # Lost events: the whole watched scope becomes uncertain and
                # any pending OLD_NAME can no longer be paired.  The pending
                # old name is NOT treated as a deletion.
                self._add_obligation(self.watch_root, True, ev.link)
                pending_old = None

            if ev.action == ACTION_RENAMED_OLD_NAME:
                if pending_old is not None:
                    self._unpaired_old(pending_old)
                pending_old = ev
            elif ev.action == ACTION_RENAMED_NEW_NAME:
                if pending_old is not None:
                    self._apply_move(pending_old, ev)
                    pending_old = None
                else:
                    # NEW_NAME without a provably contiguous OLD_NAME: the
                    # origin is unknown, so the target listing is uncertain.
                    self.tree.add(ev.path, ev.is_dir)
                    self._add_obligation(_parent(ev.path), False,
                                         'rename_new_unpaired')
                    if ev.is_dir:
                        self._add_obligation(ev.path, True,
                                             'rename_new_unpaired')
            else:
                if pending_old is not None:
                    self._unpaired_old(pending_old)
                    pending_old = None
                self._apply_simple(ev)

        if pending_old is not None:
            self._unpaired_old(pending_old)

    def _apply_simple(self, ev):
        if ev.action == ACTION_ADDED:
            self.tree.add(ev.path, ev.is_dir)
            if ev.is_dir:
                # A new directory's contents are unknown until scanned.
                self._add_obligation(ev.path, True, 'added_dir')
        elif ev.action == ACTION_REMOVED:
            self.tree.remove(ev.path)
        # ACTION_MODIFIED does not change the name tree.

    def _apply_move(self, old_ev, new_ev):
        old_parent = _parent(old_ev.path)
        new_parent = _parent(new_ev.path)
        self.tree.move(old_ev.path, new_ev.path)
        if old_parent != new_parent:
            # Cross-directory move: both sides are affected and each side
            # gets an independent obligation.
            self._add_obligation(old_parent, False, 'cross_dir_move_source')
            self._add_obligation(new_parent, False, 'cross_dir_move_target')

    def _unpaired_old(self, old_ev):
        # The matching NEW_NAME was never seen inside a provably contiguous
        # segment.  Keep the old entry in the tree and mark its parent
        # listing uncertain; never interpret this as a deletion.
        self._add_obligation(_parent(old_ev.path), False,
                             'rename_old_unpaired')
