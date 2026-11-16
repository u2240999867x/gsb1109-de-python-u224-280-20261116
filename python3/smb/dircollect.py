"""
Directory enumeration collection helpers.

This module provides data structures to record the results of enumerating a
shared directory (e.g. via SMBConnection.listPath) while files on the server
may be concurrently renamed or added.

Key concepts:

* Pagination coverage - an enumeration is usually delivered in several
  response pages. Each page is recorded with its own entries so that the
  coverage of the enumeration remains auditable after the fact.

* Stable file identity - entries may carry the server-provided file
  reference number (SharedFile.file_id). When present and non-zero, this
  identity survives renames and is the ONLY accepted basis for pairing two
  different names as the same file. An old name disappearing while a new
  name of the same size appears is never, by itself, treated as a rename.

* Snapshot - a collection is only called a snapshot when the server
  supplied a comparable directory version (e.g. a change-journal token or
  an equivalent opaque version). Plain SMB1/SMB2 directory queries do not
  provide such a version, so collections built from them are not snapshots
  and instead expose the miss windows during which concurrent renames or
  additions may have been missed.

* Both occurrences kept - when explicit file identity shows the same file
  under two names within one enumeration (renamed mid-scan), both
  occurrences are preserved in the collection; the pairing is exposed via
  DirectoryCollection.linked_renames().
"""


class DirectoryEntry:
    """
    A single observed directory entry, recorded together with the page of
    the enumeration in which it was observed.
    """

    __slots__ = ('filename', 'file_id', 'file_size', 'is_directory', 'page_index', 'observed_at')

    def __init__(self, filename, file_id=None, file_size=0, is_directory=False, page_index=0, observed_at=None):
        self.filename = filename
        self.file_id = file_id
        self.file_size = file_size
        self.is_directory = is_directory
        self.page_index = page_index
        self.observed_at = observed_at

    @property
    def has_stable_identity(self):
        """True only when the server supplied a usable file reference number. None or 0 means unsupported."""
        return bool(self.file_id)

    def __repr__(self):
        return 'DirectoryEntry(%r, file_id=%r, page=%d)' % (self.filename, self.file_id, self.page_index)

    def __eq__(self, other):
        if not isinstance(other, DirectoryEntry):
            return NotImplemented
        return (self.filename, self.file_id, self.file_size, self.is_directory, self.page_index, self.observed_at) == \
               (other.filename, other.file_id, other.file_size, other.is_directory, other.page_index, other.observed_at)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class EnumerationPage:
    """
    One page of a directory enumeration, corresponding to one response
    batch from the server (e.g. one SMB2 QUERY_DIRECTORY response or one
    TRANS2_FIND_FIRST2/NEXT2 response batch).
    """

    def __init__(self, page_index, entries, observed_at=None):
        self.page_index = page_index
        self.entries = list(entries)
        self.observed_at = observed_at

    @property
    def entry_count(self):
        return len(self.entries)

    @property
    def first_name(self):
        return self.entries[0].filename if self.entries else None

    @property
    def last_name(self):
        return self.entries[-1].filename if self.entries else None

    def __repr__(self):
        return 'EnumerationPage(%d, %d entries)' % (self.page_index, self.entry_count)


class MissWindow:
    """
    An interval of a non-snapshot enumeration during which concurrent
    renames or additions may have been missed: changes that happen after
    one page was observed and before the next page is observed are not
    reliably reflected in the collection.
    """

    def __init__(self, after_page, before_page, started_at=None, ended_at=None):
        self.after_page = after_page    #: page index of the page observed before the window
        self.before_page = before_page  #: page index of the page observed after the window
        self.started_at = started_at
        self.ended_at = ended_at

    def __repr__(self):
        return 'MissWindow(after_page=%r, before_page=%r)' % (self.after_page, self.before_page)

    def __eq__(self, other):
        if not isinstance(other, MissWindow):
            return NotImplemented
        return (self.after_page, self.before_page, self.started_at, self.ended_at) == \
               (other.after_page, other.before_page, other.started_at, other.ended_at)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class DirectoryCollection:
    """
    The recorded result of enumerating one shared directory, preserving
    pagination coverage and stable file identities.

    directory_version: an opaque, comparable version token for the
    directory supplied by the server (e.g. a change-journal based token).
    None when the server provides no such version - in that case the
    collection is NOT a snapshot and miss_windows() describes where
    concurrent changes may have been missed.
    """

    def __init__(self, service_name, path, pages, directory_version=None, started_at=None, finished_at=None):
        self.service_name = service_name
        self.path = path
        self.pages = sorted(pages, key=lambda p: p.page_index)
        self.directory_version = directory_version
        self.started_at = started_at
        self.finished_at = finished_at

    @classmethod
    def from_shared_files(cls, service_name, path, shared_files, directory_version=None, observed_at=None):
        """
        Build a single-page collection from a list of smb.base.SharedFile
        instances as returned by SMBConnection.listPath.
        """
        entries = [ DirectoryEntry(f.filename, file_id=f.file_id, file_size=f.file_size,
                                   is_directory=f.isDirectory, page_index=0, observed_at=observed_at)
                    for f in shared_files ]
        return cls(service_name, path, [ EnumerationPage(0, entries, observed_at=observed_at) ],
                   directory_version=directory_version, started_at=observed_at, finished_at=observed_at)

    @property
    def is_snapshot(self):
        """
        True only when the server supplied a comparable directory version.
        Without such a version the collection is a best-effort scan, not a
        snapshot, and miss_windows() applies.
        """
        return self.directory_version is not None

    @property
    def entries(self):
        """All observed entries across all pages, in page order. Entries are never deduplicated."""
        result = []
        for page in self.pages:
            result.extend(page.entries)
        return result

    def miss_windows(self):
        """
        List the intervals during which concurrent renames or additions may
        have been missed. Empty for snapshots, because the comparable
        directory version makes the collection consistent.
        """
        if self.is_snapshot:
            return []
        windows = []
        for previous, following in zip(self.pages, self.pages[1:]):
            windows.append(MissWindow(previous.page_index, following.page_index,
                                      started_at=previous.observed_at, ended_at=following.observed_at))
        if not windows and (self.started_at is not None or self.finished_at is not None):
            # Single-page (or empty) unversioned enumeration: the whole scan
            # interval is one window during which changes may be missed.
            windows.append(MissWindow(None, None, started_at=self.started_at, ended_at=self.finished_at))
        return windows

    def identities(self):
        """
        Map each stable file identity (non-zero file_id) to the list of
        entries observed with that identity. A file renamed mid-scan
        appears here under two names; both occurrences are kept.
        """
        result = {}
        for entry in self.entries:
            if entry.has_stable_identity:
                result.setdefault(entry.file_id, []).append(entry)
        return result

    def linked_renames(self):
        """
        Pairs (old_entry, new_entry) observed within this collection where
        the same explicit file identity appeared under two different names.
        Only file_id is used; name/size similarity is never sufficient.
        """
        pairs = []
        for entries in self.identities().values():
            ordered = sorted(entries, key=lambda e: (e.page_index, e.observed_at or 0))
            for previous, following in zip(ordered, ordered[1:]):
                if previous.filename != following.filename:
                    pairs.append((previous, following))
        return pairs


class DirectoryDiff:
    """Result of comparing two DirectoryCollection instances."""

    def __init__(self, renames, appeared, disappeared):
        self.renames = renames          #: list of (old_entry, new_entry) paired by explicit file identity
        self.appeared = appeared        #: entries present only in the later collection
        self.disappeared = disappeared  #: entries present only in the earlier collection

    def __repr__(self):
        return 'DirectoryDiff(renames=%d, appeared=%d, disappeared=%d)' % (
            len(self.renames), len(self.appeared), len(self.disappeared))


def diff_collections(earlier, later):
    """
    Compare two collections of the same directory.

    Rename detection rules:

    * Two entries are paired as a rename ONLY when both carry the same
      explicit, non-zero file identity under different names.
    * An old name disappearing while a new name appears - even with the
      same file size - is NOT a rename; such entries are reported as
      disappeared and appeared respectively.
    * Entries without a stable identity are matched by name only.
    """
    earlier_entries = earlier.entries
    later_entries = later.entries

    matched_earlier = set()
    matched_later = set()
    renames = []

    # Pass 1: pair by explicit file identity.
    earlier_by_id = {}
    for i, entry in enumerate(earlier_entries):
        if entry.has_stable_identity:
            earlier_by_id.setdefault(entry.file_id, []).append(i)
    later_by_id = {}
    for j, entry in enumerate(later_entries):
        if entry.has_stable_identity:
            later_by_id.setdefault(entry.file_id, []).append(j)

    for file_id in earlier_by_id:
        if file_id not in later_by_id:
            continue
        old_indexes = earlier_by_id[file_id]
        new_indexes = later_by_id[file_id]
        # Same name on both sides: unchanged, pair them off first.
        for i in list(old_indexes):
            for j in list(new_indexes):
                if j in matched_later:
                    continue
                if earlier_entries[i].filename == later_entries[j].filename:
                    matched_earlier.add(i)
                    matched_later.add(j)
                    break
        # Remaining entries with the same identity but different names: renames.
        remaining_old = [ i for i in old_indexes if i not in matched_earlier ]
        remaining_new = [ j for j in new_indexes if j not in matched_later ]
        for i, j in zip(remaining_old, remaining_new):
            matched_earlier.add(i)
            matched_later.add(j)
            renames.append((earlier_entries[i], later_entries[j]))

    # Pass 2: entries without stable identity match by name only.
    later_unmatched_by_name = {}
    for j, entry in enumerate(later_entries):
        if j not in matched_later and not entry.has_stable_identity:
            later_unmatched_by_name.setdefault(entry.filename, []).append(j)
    for i, entry in enumerate(earlier_entries):
        if i in matched_earlier or entry.has_stable_identity:
            continue
        candidates = later_unmatched_by_name.get(entry.filename)
        if candidates:
            matched_earlier.add(i)
            j = candidates.pop(0)
            matched_later.add(j)

    disappeared = [ entry for i, entry in enumerate(earlier_entries) if i not in matched_earlier ]
    appeared = [ entry for j, entry in enumerate(later_entries) if j not in matched_later ]
    return DirectoryDiff(renames, appeared, disappeared)
