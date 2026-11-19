# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Asset collection: unify FTP directory listing results.

This module is aimed at tools collecting directory information ("asset
collection") from FTP servers.  Directory content can be collected via
MLSD/MLST (machine readable facts, RFC-3659) and/or via LIST (the
human readable "display list").  The two sources declare the same
entries with different granularity, e.g. MLSD declares timestamps with
second precision while LIST only declares hours/minutes (or just the
day for old files).

The classes in this module merge the two sources into a single catalog
while keeping track, for every entry, of:

* the original declarations, one per source, preserved as collected;
* the trusted precision of every declared field;
* unknown facts, which are preserved as-is (never parsed, never
  dropped);
* missing fields, which are simply absent (never zero-filled).

When multiple entries share the same name and a new declaration cannot
be uniquely associated with one of them, the declaration is not
associated at all: the candidates are recorded as an Ambiguity instead
of letting the file order silently decide which entry gets updated.

Finally, when reconciling the catalog with a freshly collected
listing, deletions are emitted only if the collection is known to
cover the whole directory (Coverage.COMPLETE).  A partial or failed
collection never results in a full deletion of the directory content.
"""

import datetime
import enum
import re

__all__ = [
    "Ambiguity",
    "Catalog",
    "CatalogEntry",
    "Coverage",
    "Declaration",
    "Deletion",
    "Field",
    "Precision",
    "parse_list",
    "parse_mlsx",
]


class Precision(enum.Enum):
    """The trusted precision of a declared field value."""

    UNKNOWN = 0  # declared but not parseable; value kept as-is
    DAY = 1  # e.g. LIST "month year" timestamps
    MINUTE = 2  # e.g. LIST "month day hh:mm" timestamps
    SECOND = 3  # e.g. MLSD "modify=YYYYMMDDHHMMSS"
    MICROSECOND = 4  # e.g. MLSD "modify=YYYYMMDDHHMMSS.sss"
    EXACT = 5  # exact non temporal values (size, type, ...)


class Coverage(enum.Enum):
    """How much of a directory a collected listing covers."""

    COMPLETE = "complete"  # the whole directory was listed
    PARTIAL = "partial"  # the listing is known to be incomplete
    FAILED = "failed"  # the collection failed


class Field:
    """A single declared field: value + trusted precision + source."""

    __slots__ = ("value", "precision", "source")

    def __init__(self, value, precision, source):
        self.value = value
        self.precision = precision
        self.source = source

    def __repr__(self):
        return (
            f"Field({self.value!r}, precision={self.precision.name}, "
            f"source={self.source!r})"
        )

    def __eq__(self, other):
        return (
            isinstance(other, Field)
            and self.value == other.value
            and self.precision == other.precision
            and self.source == other.source
        )

    def __ne__(self, other):
        return not self == other


class Declaration:
    """The original declaration of a directory entry from one source.

     - (str) source: where the declaration comes from ("mlsd", "mlst"
       or "list").
     - (str) raw: the original line, as collected (line terminator
       stripped).
     - (str) name: the declared entry name.
     - (dict) fields: the parsed fields as {name: Field}.  Fields which
       were not declared are simply absent: they are never zero-filled.
     - (dict) facts: the raw facts exactly as declared (MLSD/MLST
       only), including unknown facts, which are preserved as-is.
    """

    __slots__ = ("source", "raw", "name", "fields", "facts")

    def __init__(self, source, raw, name, fields=None, facts=None):
        self.source = source
        self.raw = raw
        self.name = name
        self.fields = fields if fields is not None else {}
        self.facts = facts if facts is not None else {}

    def __repr__(self):
        return (
            f"Declaration(source={self.source!r}, name={self.name!r}, "
            f"fields={sorted(self.fields)})"
        )

    @property
    def unknown_facts(self):
        """The declared facts this module does not know about,
        preserved as-is.
        """
        return {
            k: v
            for k, v in self.facts.items()
            if k.lower() not in _KNOWN_FACTS
        }


class CatalogEntry:
    """A unified catalog entry.

    Keeps every original declaration separately, and exposes a unified
    view (`fields`) where each field is the most trusted declaration
    available (higher precision wins; on equal precision the most
    recent declaration wins).  Fields which were never declared are
    absent, never zero-filled.
    """

    def __init__(self, name):
        self.name = name
        self.declarations = []
        self.fields = {}

    def __repr__(self):
        return (
            f"CatalogEntry({self.name!r}, sources={self.sources}, "
            f"fields={sorted(self.fields)})"
        )

    def add(self, declaration):
        """Merge a declaration into this entry."""
        self.declarations.append(declaration)
        for name, field in declaration.fields.items():
            self.fields[name] = self._merge(self.fields.get(name), field)

    @staticmethod
    def _merge(old, new):
        if old is None or new.precision.value >= old.precision.value:
            return new
        return old

    @property
    def sources(self):
        """The list of sources which declared this entry."""
        return [d.source for d in self.declarations]

    @property
    def facts(self):
        """All raw facts declared for this entry, unknown ones
        included, preserved as-is.
        """
        merged = {}
        for declaration in self.declarations:
            merged.update(declaration.facts)
        return merged

    def field(self, name, default=None):
        """Return the unified value for field *name*, or *default* if
        the field was never declared (missing fields are never
        zero-filled).
        """
        field = self.fields.get(name)
        return field.value if field is not None else default


class Ambiguity:
    """Recorded when a declaration cannot be uniquely associated with
    one of the same-name candidate entries.  No candidate is modified:
    the file order never silently decides the association.
    """

    __slots__ = ("dirpath", "declaration", "candidates")

    def __init__(self, dirpath, declaration, candidates):
        self.dirpath = dirpath
        self.declaration = declaration
        self.candidates = candidates

    def __repr__(self):
        return (
            f"Ambiguity({self.declaration.name!r} in {self.dirpath!r}, "
            f"{len(self.candidates)} candidates)"
        )


class Deletion:
    """A catalog entry which disappeared from a completely covered
    directory.
    """

    __slots__ = ("dirpath", "entry")

    def __init__(self, dirpath, entry):
        self.dirpath = dirpath
        self.entry = entry

    @property
    def name(self):
        return self.entry.name

    def __repr__(self):
        return f"Deletion({self.name!r} in {self.dirpath!r})"


# ===================================================================
# --- parsers
# ===================================================================

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

_LIST_TYPES = {"-": "file", "d": "dir", "l": "link"}


def _parse_int(value, source):
    try:
        return Field(int(value), Precision.EXACT, source)
    except ValueError:
        # keep the declared value as-is instead of fabricating one
        return Field(value, Precision.UNKNOWN, source)


def _parse_str(value, source):
    return Field(value, Precision.EXACT, source)


def _parse_timestamp(value, source):
    # RFC-3659 time-val: YYYYMMDDHHMMSS[.sss], always GMT
    try:
        if "." in value:
            value, frac = value.split(".", 1)
            frac = int((frac + "000000")[:6])
        else:
            frac = 0
        stamp = datetime.datetime.strptime(value, "%Y%m%d%H%M%S")
        stamp = stamp.replace(microsecond=frac)
    except ValueError:
        # keep the declared value as-is instead of fabricating one
        return Field(value, Precision.UNKNOWN, source)
    precision = Precision.MICROSECOND if frac else Precision.SECOND
    return Field(stamp, precision, source)


_FACT_PARSERS = {
    "size": _parse_int,
    "modify": _parse_timestamp,
    "create": _parse_timestamp,
    "type": _parse_str,
    "perm": _parse_str,
    "unique": _parse_str,
    "unix.mode": _parse_str,
    "unix.uid": _parse_int,
    "unix.gid": _parse_int,
}

_KNOWN_FACTS = frozenset(_FACT_PARSERS)


def parse_mlsx(line, source="mlsd"):
    """Parse a single MLSD/MLST line into a Declaration.

    Unknown facts are preserved as-is in `Declaration.facts`; facts
    which are not declared are simply absent (never zero-filled).
    """
    if isinstance(line, bytes):
        line = line.decode("utf8", "replace")
    raw = line.rstrip("\r\n")
    # RFC-3659: entry = [ facts ] SP pathname; facts cannot contain
    # spaces, so the first space separates the facts from the name
    facts_part, sep, name = raw.partition(" ")
    if not sep:
        if "=" in facts_part:
            raise ValueError(f"malformed {source} line: {raw!r}")
        # no facts declared; the whole line is the entry name
        name = facts_part
        facts_part = ""
    facts = {}
    fields = {}
    for token in facts_part.split(";"):
        if not token:
            continue
        fact_name, _, value = token.partition("=")
        # preserve every fact as declared, unknown ones included
        facts[fact_name] = value
        parser = _FACT_PARSERS.get(fact_name.lower())
        if parser is not None:
            fields[fact_name.lower()] = parser(value, source)
    return Declaration(source, raw, name, fields, facts)


def parse_list(line, source="list"):
    """Parse a single LIST "display list" line (UNIX "ls -l" style)
    into a Declaration.

    The declared timestamp has MINUTE precision ("Sep 02  3:47") or DAY
    precision ("Sep 02  2021").  In the former case the year is not
    declared and the current year is assumed; the trusted precision
    records what can actually be trusted.  Fields which are not
    declared are simply absent (never zero-filled).
    """
    if isinstance(line, bytes):
        line = line.decode("utf8", "replace")
    raw = line.rstrip("\r\n")
    parts = raw.split(None, 8)
    if len(parts) < 9:
        raise ValueError(f"malformed {source} line: {raw!r}")
    (
        perms,
        nlinks,
        owner,
        group,
        size,
        month,
        day,
        time_or_year,
        name,
    ) = parts
    fields = {"perm": Field(perms, Precision.EXACT, source)}
    ftype = _LIST_TYPES.get(perms[:1])
    if ftype is not None:
        fields["type"] = Field(ftype, Precision.EXACT, source)
    fields["nlinks"] = _parse_int(nlinks, source)
    fields["owner"] = Field(owner, Precision.EXACT, source)
    fields["group"] = Field(group, Precision.EXACT, source)
    fields["size"] = _parse_int(size, source)
    month_num = _MONTHS.get(month)
    if month_num is not None:
        try:
            day_num = int(day)
        except ValueError:
            day_num = 0
        if day_num:
            if re.match(r"^\d{1,2}:\d{2}$", time_or_year):
                # "Sep 02  3:47": the year is not declared, assume the
                # current one; only minute precision can be trusted
                hour, minute = time_or_year.split(":")
                stamp = datetime.datetime(
                    datetime.datetime.now().year,
                    month_num,
                    day_num,
                    int(hour),
                    int(minute),
                )
                fields["modify"] = Field(stamp, Precision.MINUTE, source)
            elif re.match(r"^\d{4}$", time_or_year):
                # "Sep 02  2021": only day precision can be trusted
                stamp = datetime.datetime(
                    int(time_or_year), month_num, day_num
                )
                fields["modify"] = Field(stamp, Precision.DAY, source)
    if " -> " in name:  # symlink, e.g. "symlink -> realfile"
        name, target = name.split(" -> ", 1)
        fields["target"] = Field(target, Precision.EXACT, source)
    return Declaration(source, raw, name, fields)


# ===================================================================
# --- catalog
# ===================================================================


class Catalog:
    """A unified catalog of collected directory entries.

    Entries are organized by directory and associated by name.  Every
    entry records the original declarations (one per source) and the
    trusted precision of each field.
    """

    def __init__(self):
        self._dirs = {}  # dirpath -> {name: [CatalogEntry, ...]}
        self.ambiguities = []
        self.deletions = []

    def _dir(self, dirpath):
        return self._dirs.setdefault(dirpath, {})

    @staticmethod
    def _match(candidates, declaration):
        """Return the candidates a declaration may refer to."""
        unique = declaration.fields.get("unique")
        if unique is None:
            return list(candidates)
        exact = [
            c
            for c in candidates
            if c.fields.get("unique") is not None
            and c.fields["unique"].value == unique.value
        ]
        if exact:
            return exact
        # candidates without a "unique" fact may still be the same item
        return [c for c in candidates if c.fields.get("unique") is None]

    def observe(self, dirpath, declaration):
        """Associate a declaration with a catalog entry.

        Returns the CatalogEntry the declaration was associated with,
        or None if the association is ambiguous: in that case an
        Ambiguity listing the candidates is recorded and no candidate
        is modified (the file order never decides the association).
        """
        entries = self._dir(dirpath)
        candidates = entries.get(declaration.name, [])
        matches = self._match(candidates, declaration)
        if len(matches) == 1:
            entry = matches[0]
        elif not matches:
            entry = CatalogEntry(declaration.name)
            entries.setdefault(declaration.name, []).append(entry)
        else:
            self.ambiguities.append(
                Ambiguity(dirpath, declaration, list(matches))
            )
            return None
        entry.add(declaration)
        return entry

    def observe_mlsx(self, dirpath, lines, source="mlsd"):
        """Observe MLSD (or MLST, via source="mlst") output lines."""
        return [
            self.observe(dirpath, parse_mlsx(line, source=source))
            for line in lines
        ]

    def observe_list(self, dirpath, lines):
        """Observe LIST "display list" output lines."""
        ret = []
        for line in lines:
            if isinstance(line, bytes):
                line = line.decode("utf8", "replace")
            if line.startswith("total "):  # "ls -l" summary line
                continue
            ret.append(self.observe(dirpath, parse_list(line)))
        return ret

    def get(self, dirpath, name):
        """Return the candidate entries for *name* in *dirpath*
        (more than one means the same-name items could not be uniquely
        associated).
        """
        return list(self._dirs.get(dirpath, {}).get(name, []))

    def entries(self, dirpath=None):
        """Yield all catalog entries, sorted by directory and name."""
        dirpaths = [dirpath] if dirpath is not None else sorted(self._dirs)
        for dirpath in dirpaths:
            entries = self._dirs.get(dirpath, {})
            for name in sorted(entries):
                yield from entries[name]

    def reconcile(self, dirpath, names, coverage=Coverage.COMPLETE):
        """Reconcile the catalog with a freshly collected listing.

        Returns the Deletion records for known entries which are not in
        *names*.  Deletions are emitted only when *coverage* is
        Coverage.COMPLETE: a partial or failed collection never results
        in a (full) deletion of the directory content.
        """
        if isinstance(coverage, str):
            coverage = Coverage(coverage)
        if coverage is not Coverage.COMPLETE:
            return []
        seen = set(names)
        entries = self._dir(dirpath)
        deleted = []
        for name in list(entries):
            if name not in seen:
                for entry in entries.pop(name):
                    deleted.append(Deletion(dirpath, entry))
        self.deletions.extend(deleted)
        return deleted
