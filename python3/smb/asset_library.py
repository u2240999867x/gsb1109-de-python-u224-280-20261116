"""
In-memory asset library that catalogues NTFS data streams of remote files.

Background
==========
On NTFS, every file has one unnamed *default data stream* (also known as the
``$DATA`` stream) and zero or more *named streams* (alternate data streams,
ADS). A named stream is addressed over SMB/CIFS by appending a colon and the
stream name to the file path, e.g. ``report.docx:Zone.Identifier``.

The catalog in this module deliberately separates the *stable file identity*
from the current path so that the rules below hold:

* Both the default stream and every named stream of a file are registered,
  keeping the stream name, its size and the stable identity of its owning file.
* Streams with the same name on different files do not share an identity:
  a stream identity is always the pair ``(file_id, stream_name)``.
* A stream that cannot be read is still registered but flagged as unreadable;
  it never prevents access to the owning file or its other streams.
* Moving a file only updates its current path. All stream relations migrate
  together because they hang off the stable file identity.
* Text after a colon is an NTFS stream name, never a new path directory.
  Directory paths containing a colon are rejected outright.

The module is pure Python and does not perform any network I/O itself; the
SMB layer feeds discovered entries into the library.
"""

import uuid


#: Internal name used for the unnamed NTFS default data stream.
DEFAULT_STREAM_NAME = ''

#: NTFS stream type suffix accepted in ``file:stream:$DATA`` notations.
DATA_STREAM_TYPE = '$DATA'


class StreamNameError(ValueError):
    """Raised when an NTFS stream name is not valid."""
    pass


class AssetLibraryError(Exception):
    """Base class for asset library errors."""
    pass


class DuplicateFileError(AssetLibraryError):
    """Raised when a path or stable identity is already taken."""
    pass


class UnknownAssetError(AssetLibraryError):
    """Raised when a file or stream cannot be found in the library."""
    pass


def _normalize_path(path):
    """Normalize SMB and POSIX separators so path lookups are equivalent."""
    if not isinstance(path, str):
        raise TypeError('path must be a string, got %r' % type(path).__name__)
    return path.replace('/', '\\')


def _stream_key(stream_name):
    # NTFS stream names are case-insensitive but case-preserving.
    return stream_name.lower()


def validate_stream_name(stream_name):
    """
    Validate an NTFS stream name (without the separating colon).

    The default stream uses an empty string. Named streams must be non-empty
    and must not contain path separators, colons or control characters.

    :return: the validated stream name
    :raises StreamNameError: if the name is invalid
    """
    if stream_name is None:
        stream_name = DEFAULT_STREAM_NAME
    if not isinstance(stream_name, str):
        raise StreamNameError('stream name must be a string, got %r' % type(stream_name).__name__)
    if stream_name == DEFAULT_STREAM_NAME:
        return DEFAULT_STREAM_NAME
    if stream_name.endswith(' ') or stream_name.endswith('.'):
        raise StreamNameError('stream name must not end with a space or period: %r' % stream_name)
    for forbidden in (':', '\\', '/'):
        if forbidden in stream_name:
            raise StreamNameError('stream name must not contain %r: %r' % (forbidden, stream_name))
    if any(ord(ch) < 0x20 for ch in stream_name):
        raise StreamNameError('stream name must not contain control characters: %r' % stream_name)
    return stream_name


def parse_smb_path(raw_path):
    """
    Split an SMB path into its file path and optional stream name.

    Examples::

        parse_smb_path('dir\\\\report.docx')                  -> ('dir\\\\report.docx', '')
        parse_smb_path('dir\\\\report.docx:Zone.Identifier') -> ('dir\\\\report.docx', 'Zone.Identifier')
        parse_smb_path('report.docx:stream:$DATA')           -> ('report.docx', 'stream')
        parse_smb_path('report.docx::$DATA')                 -> ('report.docx', '')

    Text after the first colon is always treated as a stream name and never as
    a path directory.

    :return: a ``(file_path, stream_name)`` tuple where ``stream_name`` is an
             empty string for the default data stream
    :raises StreamNameError: if the stream notation is malformed
    """
    normalized = _normalize_path(raw_path)
    if ':' not in normalized:
        return normalized, DEFAULT_STREAM_NAME

    file_path, _, stream_part = normalized.partition(':')
    if not file_path:
        raise StreamNameError('missing file path before stream colon: %r' % raw_path)

    parts = stream_part.split(':')
    if len(parts) > 2:
        raise StreamNameError('too many colons in stream notation: %r' % raw_path)

    stream_name = parts[0]
    if len(parts) == 2:
        if parts[1] != DATA_STREAM_TYPE:
            raise StreamNameError('unsupported stream type %r (only %s is supported): %r'
                                  % (parts[1], DATA_STREAM_TYPE, raw_path))
    if stream_name == '':
        return file_path, DEFAULT_STREAM_NAME

    validate_stream_name(stream_name)
    return file_path, stream_name


def ensure_directory_path(path):
    """
    Validate that *path* refers to a directory and not an alternate stream.

    :return: the normalized directory path
    :raises StreamNameError: if a colon suggests a stream notation
    """
    normalized = _normalize_path(path)
    if ':' in normalized:
        raise StreamNameError('colon denotes an NTFS stream, not a directory: %r' % path)
    return normalized


class StreamRecord:
    """
    A single data stream registered against an :class:`AssetFile`.

    The following attributes are available:

    * stream_name : Unicode stream name; an empty string for the default stream
    * size : Stream size in number of bytes
    * is_readable : False if the stream exists but cannot be read
    * file_id : Stable identity of the owning :class:`AssetFile`
    """

    def __init__(self, file_id, stream_name, size, is_readable = True):
        stream_name = validate_stream_name(stream_name)
        if not isinstance(size, int) or size < 0:
            raise ValueError('stream size must be a non-negative integer, got %r' % (size,))
        if not file_id:
            raise ValueError('file_id must be a non-empty stable identity')

        self.file_id = file_id
        self.stream_name = stream_name
        self.size = size
        self.is_readable = bool(is_readable)

    @property
    def is_default(self):
        """True if this is the unnamed default data stream."""
        return self.stream_name == DEFAULT_STREAM_NAME

    @property
    def is_named(self):
        """True if this is a named (alternate data) stream."""
        return self.stream_name != DEFAULT_STREAM_NAME

    @property
    def identity(self):
        """
        Stable stream identity.

        A stream is uniquely identified by the pair ``(file_id, stream_name)``;
        identically named streams of different files therefore never share an
        identity.
        """
        return self.file_id, self.stream_name

    def __eq__(self, other):
        if not isinstance(other, StreamRecord):
            return NotImplemented
        return self.identity == other.identity

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash((self.file_id, _stream_key(self.stream_name)))

    def __repr__(self):
        kind = 'default' if self.is_default else 'named'
        return 'StreamRecord(file_id=%r, stream_name=%r, size=%d, %s)' \
               % (self.file_id, self.stream_name, self.size, kind)


class AssetFile:
    """
    A catalogued file identified by a stable *file_id* independent of its path.

    Application code normally obtains instances through :class:`AssetLibrary`
    rather than constructing them directly.
    """

    def __init__(self, file_id, path):
        if not file_id:
            raise ValueError('file_id must be a non-empty stable identity')
        self.file_id = file_id
        self._path = _normalize_path(path)
        self._streams = {}

    @property
    def path(self):
        """Current path of the file; updated when the file is moved."""
        return self._path

    @property
    def default_stream(self):
        """The registered default data stream, or None if not yet registered."""
        return self._streams.get(_stream_key(DEFAULT_STREAM_NAME))

    def register_stream(self, stream_name, size, is_readable = True):
        """
        Register (or update the size of) a data stream of this file.

        Re-registering the same stream keeps the record and its identity while
        refreshing its size and readability flag.

        :return: the :class:`StreamRecord` that was created or updated
        """
        key = _stream_key(validate_stream_name(stream_name))
        if not isinstance(size, int) or size < 0:
            raise ValueError('stream size must be a non-negative integer, got %r' % (size,))

        record = self._streams.get(key)
        if record is None:
            record = StreamRecord(self.file_id, stream_name, size, is_readable)
            self._streams[key] = record
        else:
            record.size = size
            record.is_readable = bool(is_readable)
        return record

    def mark_stream_unreadable(self, stream_name = DEFAULT_STREAM_NAME):
        """
        Flag a previously registered stream as unreadable.

        An unreadable stream remains catalogued; it does not affect the file,
        the default stream or any sibling named streams.
        """
        record = self.get_stream(stream_name)
        record.is_readable = False
        return record

    def get_stream(self, stream_name = DEFAULT_STREAM_NAME):
        """
        Return the :class:`StreamRecord` for *stream_name*.

        :raises UnknownAssetError: if no such stream is registered
        """
        validate_stream_name(stream_name)
        try:
            return self._streams[_stream_key(stream_name)]
        except KeyError:
            raise UnknownAssetError('stream %r is not registered for file %s'
                                    % (stream_name, self.file_id))

    def has_stream(self, stream_name):
        """Return True if *stream_name* is registered for this file."""
        validate_stream_name(stream_name)
        return _stream_key(stream_name) in self._streams

    def streams(self):
        """Return all registered streams, default stream first."""
        records = list(self._streams.values())
        records.sort(key = lambda record: (not record.is_default, record.stream_name.lower()))
        return records

    def named_streams(self):
        """Return only the registered named (alternate data) streams."""
        return [record for record in self._streams.values() if record.is_named]

    def stream_path(self, stream_name = DEFAULT_STREAM_NAME):
        """
        Return the SMB notation used to open *stream_name*.

        The default stream is addressed by the plain file path; a named stream
        appends ``:stream_name``.
        """
        validate_stream_name(stream_name)
        if stream_name == DEFAULT_STREAM_NAME:
            return self._path
        return '%s:%s' % (self._path, stream_name)

    def _move(self, new_path):
        self._path = _normalize_path(new_path)

    def __repr__(self):
        return 'AssetFile(file_id=%r, path=%r, streams=%d)' \
               % (self.file_id, self._path, len(self._streams))


class AssetLibrary:
    """
    Catalogue of :class:`AssetFile` instances keyed by stable file identity.

    Files are indexed both by their immutable *file_id* and their current path.
    Moving a file updates only the path index, so every registered stream
    migrates along with the file identity.
    """

    def __init__(self):
        self._files_by_id = {}
        self._paths = {}

    def register_file(self, path, file_id = None):
        """
        Register a file at *path* and return its :class:`AssetFile`.

        :param string path: current path of the file; stream colons are rejected
        :param string file_id: stable identity supplied by the caller (e.g. an
               SMB file reference number); a unique ID is generated when omitted
        :raises DuplicateFileError: if *path* or *file_id* is already registered
        """
        normalized = ensure_directory_path(path)
        if file_id is None:
            file_id = uuid.uuid4().hex
        file_id = str(file_id)
        if not file_id:
            raise ValueError('file_id must not be empty')
        if file_id in self._files_by_id:
            raise DuplicateFileError('file_id %r is already registered' % file_id)
        if normalized in self._paths:
            raise DuplicateFileError('path %r is already registered' % normalized)

        asset_file = AssetFile(file_id, normalized)
        self._files_by_id[file_id] = asset_file
        self._paths[normalized] = asset_file
        return asset_file

    def get_file(self, file_id):
        """Return the :class:`AssetFile` with *file_id*."""
        try:
            return self._files_by_id[str(file_id)]
        except KeyError:
            raise UnknownAssetError('no file registered with file_id %r' % (file_id,))

    def find_by_path(self, path):
        """
        Resolve a plain file path (no stream colon) to its :class:`AssetFile`.

        :raises StreamNameError: if *path* contains a stream colon
        :raises UnknownAssetError: if the path is not registered
        """
        normalized = ensure_directory_path(path)
        try:
            return self._paths[normalized]
        except KeyError:
            raise UnknownAssetError('no file registered at path %r' % path)

    def register_stream(self, path, stream_name, size, is_readable = True):
        """
        Register a stream for the file at *path*, creating the file first if
        necessary.

        :return: the :class:`StreamRecord` that was created or updated
        """
        normalized = ensure_directory_path(path)
        asset_file = self._paths.get(normalized)
        if asset_file is None:
            asset_file = self.register_file(normalized)
        return asset_file.register_stream(stream_name, size, is_readable)

    def register_smb_entry(self, raw_path, size, is_readable = True):
        """
        Register a stream discovered through its SMB ``path[:stream]`` notation.

        A path without a colon registers the default data stream; a path with a
        colon registers the named stream (and its owning file, on first sight).

        :return: the :class:`StreamRecord` that was created or updated
        """
        file_path, stream_name = parse_smb_path(raw_path)
        return self.register_stream(file_path, stream_name, size, is_readable)

    def resolve_stream(self, raw_path):
        """
        Resolve an SMB ``path[:stream]`` notation to its :class:`StreamRecord`.

        :raises UnknownAssetError: if the file or stream is not registered
        """
        file_path, stream_name = parse_smb_path(raw_path)
        return self.find_by_path(file_path).get_stream(stream_name)

    def move_file(self, old_path, new_path):
        """
        Move a registered file from *old_path* to *new_path*.

        The stable file identity and every registered stream relation are
        preserved; only the current path changes.

        :raises UnknownAssetError: if *old_path* is not registered
        :raises DuplicateFileError: if *new_path* belongs to another file
        :raises StreamNameError: if either path contains a stream colon
        """
        asset_file = self.find_by_path(old_path)
        normalized_new = ensure_directory_path(new_path)
        occupant = self._paths.get(normalized_new)
        if occupant is not None and occupant is not asset_file:
            raise DuplicateFileError('cannot move %s to %r: path already taken by %s'
                                     % (asset_file.file_id, new_path, occupant.file_id))

        del self._paths[asset_file.path]
        asset_file._move(normalized_new)
        self._paths[normalized_new] = asset_file
        return asset_file

    def files(self):
        """Return all registered :class:`AssetFile` instances."""
        return list(self._files_by_id.values())

    def all_streams(self):
        """Return every registered :class:`StreamRecord` across all files."""
        records = []
        for asset_file in self._files_by_id.values():
            records.extend(asset_file.streams())
        return records

    def __len__(self):
        return len(self._files_by_id)

    def __contains__(self, file_id):
        return str(file_id) in self._files_by_id
