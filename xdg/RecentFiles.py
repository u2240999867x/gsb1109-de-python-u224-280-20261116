"""
Implementation of the XDG Recent File Storage Specification
http://standards.freedesktop.org/recent-file-spec

In addition to parsing a single ``recently-used`` file, this module can
aggregate several :class:`RecentFiles` configurations.  Each parsed
``RecentItem`` is kept as a separate piece of evidence tagged with the
configuration it came from, so that:

* filtering and sorting only ever consider occurrences visible to the
  current caller (private occurrences are excluded unless the caller is
  explicitly authorized for one of their groups);
* a public occurrence of a URI never authorizes the release of a private
  occurrence of the same URI;
* deduplicated timestamps and group memberships are derived solely from
  visible occurrences;
* every merged record can be traced back to the actual items that back it;
* removing a configuration withdraws exactly the evidence it provided.
"""

import xml.dom.minidom, xml.sax.saxutils
import os, time, fcntl
import posixpath
import urllib.parse
from collections import OrderedDict
from xdg.Exceptions import ParsingError

# Default ports that do not make two URIs distinct.
_DEFAULT_PORTS = {
    "http": "80",
    "https": "443",
    "ftp": "21",
}


def uri_key(uri):
    """Return a canonical key for URI equivalence comparisons.

    Two URIs are treated as equivalent when they identify the same
    resource, even if spelled differently.  For ``file`` URIs this means
    percent-encoding differences, an empty versus ``localhost`` host and
    ``.``/``..`` path segments are normalized.  For other URIs the scheme
    and host are lower-cased, default ports stripped and the path
    normalized.  Paths remain case sensitive.
    """
    if not isinstance(uri, str):
        raise TypeError("URI must be a string, not %s" % type(uri).__name__)

    parts = urllib.parse.urlsplit(uri)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    path = urllib.parse.unquote(parts.path)
    path = posixpath.normpath(path) if path else ""

    if scheme == "file":
        if host == "localhost":
            host = ""
        return urllib.parse.urlunsplit(("file", host, path, "", ""))

    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = "%s:%d" % (host, port)
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query,
                                     parts.fragment))


class RecentItem:
    """A single recent file occurrence as supplied by one configuration."""

    def __init__(self, uri, mimetype, timestamp, private=False, groups=None,
                 source=None):
        self.URI = uri
        self.key = uri_key(uri)
        self.MimeType = mimetype
        self.Timestamp = int(timestamp)
        self.Private = bool(private)
        self.Groups = list(groups or [])
        # Identifier of the configuration that supplied this occurrence.
        self.source = source

    def matches(self, mimetypes=None, groups=None):
        """Return whether this item passes the MIME type and group filters.

        Both filters are conjunctive: when both are given the item must
        match a MIME type *and* a group.
        """
        if mimetypes and self.MimeType not in mimetypes:
            return False
        if groups and not any(group in self.Groups for group in groups):
            return False
        return True

    def __repr__(self):
        return "<RecentItem %s source=%r private=%s>" % (
            self.URI, self.source, self.Private)


class RecentMerge:
    """A deduplicated view of one or more equivalent :class:`RecentItem` s.

    All attributes are derived exclusively from the occurrences visible to
    the caller that produced the merge; private occurrences that were not
    visible can never contribute here.
    """

    def __init__(self, key, uri, mimetype, timestamp, groups, private,
                 occurrences):
        self.key = key
        self.URI = uri
        self.MimeType = mimetype
        self.Timestamp = timestamp
        self.Groups = groups
        # True only when every backing occurrence is private.
        self.Private = private
        self._occurrences = list(occurrences)

    def getItems(self):
        """Return the visible :class:`RecentItem` occurrences backing this record."""
        return list(self._occurrences)

    def getSources(self):
        """Return the identifiers of the configurations backing this record."""
        return sorted({item.source for item in self._occurrences
                       if item.source is not None})

    def __repr__(self):
        return "<RecentMerge %s timestamp=%d private=%s>" % (
            self.URI, self.Timestamp, self.Private)


def _visible_items(items, groups, include_private):
    """Filter out occurrences the caller is not allowed to see.

    Public occurrences are always visible.  Private occurrences are only
    visible for an explicitly authorized query, and only when the caller
    names one of the item's groups.  A public occurrence of the same URI
    never makes a private one visible.
    """
    authorized = set(groups or ())
    visible = []
    for item in items:
        if not item.Private:
            visible.append(item)
        elif include_private and authorized.intersection(item.Groups):
            visible.append(item)
    return visible


def _filter_items(items, mimetypes, groups):
    return [item for item in items if item.matches(mimetypes, groups)]


def _merge_items(items, limit=0):
    """Deduplicate equivalent occurrences and return sorted merged records."""
    buckets = OrderedDict()
    for item in items:
        buckets.setdefault(item.key, []).append(item)

    merged = []
    for key, occurrences in buckets.items():
        # Stable ordering keeps the first-seen occurrence on timestamp ties.
        ordered = sorted(occurrences, key=lambda item: item.Timestamp,
                         reverse=True)
        primary = ordered[0]
        groups = sorted({group for item in occurrences
                         for group in item.Groups})
        private = all(item.Private for item in occurrences)
        merged.append(RecentMerge(
            key, primary.URI, primary.MimeType, primary.Timestamp,
            groups, private, ordered))

    merged.sort(key=lambda record: (-record.Timestamp, record.key))
    if limit and limit > 0:
        merged = merged[:limit]
    return merged


class RecentFiles:
    def __init__(self):
        self.RecentFiles = []
        self.filename = ""

    def parse(self, filename=None):
        """Parse a list of recently used files.

        filename defaults to ``~/.recently-used``.
        """
        if not filename:
            filename = os.path.join(os.getenv("HOME"), ".recently-used")

        try:
            doc = xml.dom.minidom.parse(filename)
        except IOError:
            raise ParsingError('File not found', filename)
        except xml.parsers.expat.ExpatError:
            raise ParsingError('Not a valid .menu file', filename)

        self.filename = filename

        for child in doc.childNodes:
            if child.nodeType == xml.dom.Node.ELEMENT_NODE:
                if child.tagName == "RecentFiles":
                    for recent in child.childNodes:
                        if recent.nodeType == xml.dom.Node.ELEMENT_NODE:
                            if recent.tagName == "RecentItem":
                                self.__parseRecentItem(recent)

        self.sort()

    def __parseRecentItem(self, item):
        recent = RecentFile()
        self.RecentFiles.append(recent)

        for attribute in item.childNodes:
            if attribute.nodeType == xml.dom.Node.ELEMENT_NODE:
                if attribute.tagName == "URI":
                    recent.URI = attribute.childNodes[0].nodeValue
                elif attribute.tagName == "Mime-Type":
                    recent.MimeType = attribute.childNodes[0].nodeValue
                elif attribute.tagName == "Timestamp":
                    recent.Timestamp = int(attribute.childNodes[0].nodeValue)
                elif attribute.tagName == "Private":
                    recent.Private = True
                elif attribute.tagName == "Groups":

                    for group in attribute.childNodes:
                        if group.nodeType == xml.dom.Node.ELEMENT_NODE:
                            if group.tagName == "Group":
                                recent.Groups.append(group.childNodes[0].nodeValue)

    def write(self, filename=None):
        """Write the list of recently used files to disk.

        If the instance is already associated with a file, filename can be
        omitted to save it there again.
        """
        if not filename and not self.filename:
            raise ParsingError('File not found', filename)
        elif not filename:
            filename = self.filename

        with open(filename, "w") as f:
            fcntl.lockf(f, fcntl.LOCK_EX)
            f.write('<?xml version="1.0"?>\n')
            f.write("<RecentFiles>\n")

            for r in self.RecentFiles:
                f.write("  <RecentItem>\n")
                f.write("    <URI>%s</URI>\n" % xml.sax.saxutils.escape(r.URI))
                f.write("    <Mime-Type>%s</Mime-Type>\n" % r.MimeType)
                f.write("    <Timestamp>%s</Timestamp>\n" % r.Timestamp)
                if r.Private == True:
                    f.write("    <Private/>\n")
                if len(r.Groups) > 0:
                    f.write("    <Groups>\n")
                    for group in r.Groups:
                        f.write("      <Group>%s</Group>\n" % group)
                    f.write("    </Groups>\n")
                f.write("  </RecentItem>\n")

            f.write("</RecentFiles>\n")
            fcntl.lockf(f, fcntl.LOCK_UN)

    def getItems(self, mimetypes=None, groups=None, include_private=False):
        """Return the visible :class:`RecentItem` occurrences of this source.

        ``mimetypes`` and ``groups`` are combined conjunctively.  Private
        items are excluded unless ``include_private`` is true *and* the item
        belongs to one of the named ``groups``.
        """
        source = self.filename or None
        items = [RecentItem(rf.URI, rf.MimeType, rf.Timestamp, rf.Private,
                            rf.Groups, source) for rf in self.RecentFiles]
        items = _visible_items(items, groups, include_private)
        return _filter_items(items, mimetypes, groups)

    def query(self, mimetypes=None, groups=None, limit=0,
              include_private=False):
        """Deduplicate this source into :class:`RecentMerge` records.

        Equivalent URIs are merged, newest timestamp wins, and the returned
        records are sorted newest first.  ``limit`` keeps the first K
        deduplicated records.
        """
        return _merge_items(
            self.getItems(mimetypes, groups, include_private), limit)

    def getFiles(self, mimetypes=None, groups=None, limit=0):
        """Get a list of recently used files.

        The parameters can be used to filter by mime types, by group, or to
        limit the number of items returned. By default, the entire list is
        returned, except for items marked private.  When both filters are
        given they are combined (an item must match a mime type and a
        group), and each item is returned at most once.
        """
        mime_set = set(mimetypes or ())
        group_set = set(groups or ())
        tmp = []
        for item in self.RecentFiles:
            if item.Private:
                continue
            if mime_set and item.MimeType not in mime_set:
                continue
            if group_set and not group_set.intersection(item.Groups):
                continue
            tmp.append(item)
            if limit != 0 and len(tmp) == limit:
                break

        return tmp

    def addFile(self, item, mimetype, groups=None, private=False):
        """Add a recently used file.

        item should be the URI of the file, typically starting with ``file:///``.
        """
        # check if entry already there
        if item in self.RecentFiles:
            index = self.RecentFiles.index(item)
            recent = self.RecentFiles[index]
        else:
            # delete if more then 500 files
            if len(self.RecentFiles) == 500:
                self.RecentFiles.pop()
            # add entry
            recent = RecentFile()
            self.RecentFiles.append(recent)

        recent.URI = item
        recent.MimeType = mimetype
        recent.Timestamp = int(time.time())
        recent.Private = private
        if groups:
            recent.Groups = groups

        self.sort()

    def deleteFile(self, item):
        """Remove a recently used file, by URI, from the list.
        """
        if item in self.RecentFiles:
            self.RecentFiles.remove(item)

    def sort(self):
        self.RecentFiles.sort()
        self.RecentFiles.reverse()


class AggregatedRecentFiles:
    """Aggregate several :class:`RecentFiles` configurations into one view.

    Each configuration is registered under a unique source identifier and
    contributes independent :class:`RecentItem` evidence.  Removing a
    source withdraws exactly the evidence that source provided, leaving the
    other configurations untouched.
    """

    def __init__(self):
        self._sources = OrderedDict()
        self._items = []

    def add_source(self, source, recent_files):
        """Register a parsed :class:`RecentFiles` under ``source``.

        Every item it contains becomes a :class:`RecentItem` tagged with
        ``source``.  Raises ``ValueError`` if the source is already known.
        """
        if source in self._sources:
            raise ValueError("source %r is already registered" % source)
        self._sources[source] = recent_files
        for rf in recent_files.RecentFiles:
            self._items.append(RecentItem(
                rf.URI, rf.MimeType, rf.Timestamp, rf.Private, rf.Groups,
                source))

    def remove_source(self, source):
        """Withdraw all evidence supplied by ``source``.

        Returns True if the source was registered, False otherwise.
        """
        if source not in self._sources:
            return False
        del self._sources[source]
        self._items = [item for item in self._items
                       if item.source != source]
        return True

    def sources(self):
        """Return the currently registered source identifiers."""
        return list(self._sources)

    def getItems(self, mimetypes=None, groups=None, include_private=False):
        """Return the visible occurrences across all sources.

        Private occurrences are excluded unless ``include_private`` is true
        and the caller names one of their groups; a public occurrence of the
        same URI never makes a private occurrence visible.
        """
        items = _visible_items(list(self._items), groups, include_private)
        return _filter_items(items, mimetypes, groups)

    def query(self, mimetypes=None, groups=None, limit=0,
              include_private=False):
        """Return deduplicated, newest-first :class:`RecentMerge` records.

        Filtering, sorting and deduplication use only occurrences visible
        to this caller, so merged timestamps and groups cannot carry
        private evidence.  ``limit`` returns only the first K deduplicated
        records.
        """
        return _merge_items(
            self.getItems(mimetypes, groups, include_private), limit)


class RecentFile:
    def __init__(self):
        self.URI = ""
        self.MimeType = ""
        self.Timestamp = ""
        self.Private = False
        self.Groups = []

    def __cmp__(self, other):
        return cmp(self.Timestamp, other.Timestamp)

    def __lt__ (self, other):
        return self.Timestamp < other.Timestamp

    def __eq__(self, other):
        return self.URI == str(other)

    def __str__(self):
        return self.URI
