# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

__all__ = [
    "AuthenticationFailed",
    "AuthorizerError",
    "FilesystemError",
    "RenameNotGuaranteedError",
]


class AuthorizerError(Exception):
    """Base class for authorizer exceptions."""


class AuthenticationFailed(Exception):
    """Exception raised when authentication fails for any reason."""


class FilesystemError(Exception):
    """Custom class for filesystem-related exceptions.
    You can raise this from an AbstractedFS subclass in order to
    send a customized error string to the client.
    """


class RenameNotGuaranteedError(FilesystemError):
    """Raised when a rename is requested but the underlying filesystem
    does not provide an atomic, non-replacing rename primitive.

    The two-step RNFR/RNTO procedure can only be made race-free when the
    filesystem is able to reject a rename whose destination already
    exists in a single system call (e.g. Linux ``renameat2`` with
    ``RENAME_NOREPLACE`` or macOS ``renamex_np`` with ``RENAME_EXCL``).
    When no such primitive is available the server must not pretend the
    "check then rename" sequence is atomic: raising this error makes the
    lack of a guarantee explicit instead of silently overwriting an
    existing destination.
    """


class _RetryError(Exception):
    """Raised when a socket operation would block, and hence it should
    be retried at a later time.
    """


class _FileReadWriteError(OSError):
    """Exception raised when reading or writing a file during a transfer."""


class _GiveUpOnSendfile(Exception):
    """Exception raised in case use of sendfile() fails on first try,
    in which case send() will be used.
    """
