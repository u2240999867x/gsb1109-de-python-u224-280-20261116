from __future__ import annotations


class Error(Exception):
    pass


class BrokenIndexError(Error):
    pass


class BrokenMftError(Error):
    pass


class FilenameNotAvailableError(Error):
    pass


class FileNotFoundError(Error, FileNotFoundError):
    pass


class DanglingReferenceError(FileNotFoundError):
    """Raised when a file reference points to a record that has been reused or no longer exists.

    A file reference consists of both a record (segment) number and a sequence number. When a record is reused,
    the record number can point at a different file than the one the reference was created for. Such a reference
    is dangling and must not resolve to the record currently occupying that record number.
    """


class MftNotAvailableError(Error):
    pass


class IsADirectoryError(Error, IsADirectoryError):
    pass


class NotADirectoryError(Error, NotADirectoryError):
    pass


class NotAReparsePointError(Error):
    pass


class VolumeNotAvailableError(Error):
    pass
