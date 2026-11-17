# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#

"""
Post-processing hooks
"""

import abc
import bz2
import contextlib
import errno
import gzip
import hashlib
import json
import lzma
import os
import shutil
import sys
import tempfile
import threading
import time
import typing
import uuid
from tarfile import TarFile
from zipfile import ZipFile

from .utils import get_logger

_JOURNAL_VERSION = 1
_JOURNAL_SUFFIX = ".pooch-journal"
_ACTIVE_TRANSACTIONS = set()
_ACTIVE_LOCK = threading.Lock()
_THREAD_LOCKS = {}
_THREAD_LOCKS_LOCK = threading.Lock()


def _thread_lock(key):
    """
    Return the re-entrant lock used to serialize threads for a key.
    """
    with _THREAD_LOCKS_LOCK:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _extraction_lock(extract_dir):
    """
    Serialize publication and cleanup of one extraction folder.

    An OS-level lock is used so separate processes can't interleave them. The
    lock file lives outside the data cache so nothing extra is created next to
    the extracted files.
    """
    key = os.path.abspath(os.path.normpath(extract_dir))
    with _thread_lock(key):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_dir = os.path.join(tempfile.gettempdir(), "pooch-extraction-locks")
        os.makedirs(lock_dir, exist_ok=True)
        with open(os.path.join(lock_dir, f"{digest}.lock"), "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            with _os_lock(lock_file):
                yield


@contextlib.contextmanager
def _os_lock(lock_file):
    """
    Hold an exclusive lock on an open file, cross-platform.
    """
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        fcntl = None
    if fcntl is None:
        import msvcrt  # noqa: PLC0415

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _journal_directory(extract_dir):
    """
    Return the folder where transaction journals for extract_dir are kept.
    """
    extract_dir = os.path.abspath(os.path.normpath(extract_dir))
    parent, name = os.path.split(extract_dir)
    return os.path.join(parent, f".{name or 'pooch'}{_JOURNAL_SUFFIX}")


def _fingerprint(path):
    """
    Return the identity of the file at path, or None if it doesn't exist.

    The device and inode make the identity change when a same-named file is
    replaced by another call, even if its contents happen to be identical.
    """
    try:
        stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _write_plan(path, plan):
    """
    Atomically write a transaction plan.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temporary)
        raise


def _read_plan(path):
    """
    Read a transaction plan, returning None if it is unreadable.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def _remove_path(path):
    """
    Remove a file, symlink, or directory tree if it exists.
    """
    with contextlib.suppress(FileNotFoundError):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def _remove_empty_directory(path):
    """
    Remove path if it is empty (or already gone).
    """
    try:
        os.rmdir(path)
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
            raise


def _is_active(transaction):
    """
    Check if a transaction belongs to a call running in this process.
    """
    with _ACTIVE_LOCK:
        return transaction in _ACTIVE_TRANSACTIONS


def _journal_paths(journal_dir):
    """
    Yield (plan_path, plan) for every readable transaction plan.
    """
    try:
        names = sorted(os.listdir(journal_dir))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        plan_path = os.path.join(journal_dir, name)
        plan = _read_plan(plan_path)
        if plan is not None:
            yield plan_path, plan


def _replay_stale_transactions(extract_dir):
    """
    Finish or undo transactions left behind by interrupted calls.
    """
    journal_dir = _journal_directory(extract_dir)
    extract_dir = os.path.abspath(os.path.normpath(extract_dir))
    _remove_stale_commit_markers(journal_dir)
    _remove_orphaned_staging(journal_dir)
    for plan_path, plan in list(_journal_paths(journal_dir)):
        if _is_active(plan.get("transaction")):
            continue
        if os.path.abspath(plan.get("extract_dir", "")) != extract_dir:
            continue
        # Publication and cleanup of this folder are serialized by the
        # extraction lock, so any other unfinished plan seen here belongs to
        # a call that was interrupted and can be replayed safely.
        committed = plan.get("state") == "committed" or os.path.exists(
            plan_path + ".committed"
        )
        try:
            if committed:
                _cleanup_committed_transaction(plan_path, plan)
            else:
                _rollback_transaction(plan_path, plan)
        except OSError as error:
            get_logger().warning(
                "Could not clean up interrupted extraction in '%s': %s",
                extract_dir,
                error,
            )


def _remove_orphaned_staging(journal_dir):
    """
    Remove staging folders left before their transaction plan was written.
    """
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return
    for name in names:
        if not name.startswith("staging-"):
            continue
        transaction = name[len("staging-") :]
        if os.path.exists(os.path.join(journal_dir, f"{transaction}.json")):
            continue
        if _is_active(transaction):
            continue
        try:
            _remove_path(os.path.join(journal_dir, name))
        except OSError:
            continue
    _remove_empty_directory(journal_dir)


def _remove_stale_commit_markers(journal_dir):
    """
    Remove markers for committed transactions whose plan couldn't be deleted.
    """
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json.committed"):
            continue
        plan_path = os.path.join(journal_dir, name[: -len(".committed")])
        staging = os.path.join(
            journal_dir, f"staging-{name[: -len('.json.committed')]}"
        )
        try:
            _remove_path(staging)
            _remove_path(plan_path)
            os.remove(os.path.join(journal_dir, name))
        except OSError:
            continue
    _remove_empty_directory(journal_dir)


def _rollback_transaction(plan_path, plan):
    """
    Undo the artifacts this transaction published.

    A file is only deleted while its on-disk identity still matches the one
    recorded for this transaction. Files created or replaced by another call
    after the crash therefore survive, and a pre-existing file that this call
    overwrote is restored from its backup.
    """
    journal_dir = os.path.dirname(plan_path)
    staging = plan.get("staging")
    errors = []
    for entry in reversed(plan.get("entries", [])):
        if entry.get("kind") != "file":
            continue
        final = entry["path"]
        backup = None
        if staging is not None:
            backup = os.path.join(staging, "backup", entry["relative"])
            if not os.path.exists(backup):
                backup = None
        try:
            current = _fingerprint(final)
            identity = entry.get("fingerprint")
            if identity is not None and current == identity:
                os.remove(final)
                current = None
            if backup is not None and current is None:
                os.makedirs(os.path.dirname(final), exist_ok=True)
                os.replace(backup, final)
        except OSError as error:
            errors.append(error)
    for directory in reversed(plan.get("created_directories", [])):
        try:
            _remove_empty_directory(directory)
        except OSError as error:
            errors.append(error)
    if errors:
        # Keep the plan and the staging area (which holds backups) so this
        # rollback can be replayed once the problem goes away.
        raise errors[0]
    if staging is not None:
        _remove_path(staging)
    _remove_path(plan_path)
    _remove_empty_directory(journal_dir)


def _cleanup_committed_transaction(plan_path, plan):
    """
    Remove the leftover staging area of a committed transaction.
    """
    staging = plan.get("staging")
    if staging is not None:
        try:
            _remove_path(staging)
        except OSError:
            return
    _remove_path(plan_path)
    _remove_empty_directory(os.path.dirname(plan_path))


def _missing_directories(path):
    """
    Return the directory components of path that don't exist yet.
    """
    missing = []
    current = path
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return missing[::-1]


class _ExtractionTransaction:
    """
    An extraction staged in a private folder and journaled for safe cleanup.
    """

    def __init__(self, fname, extract_dir, action):
        self.archive = os.path.abspath(fname)
        self.extract_dir = os.path.abspath(os.path.normpath(extract_dir))
        self.action = action
        self.transaction = uuid.uuid4().hex
        self.journal_dir = _journal_directory(self.extract_dir)
        self.plan_path = os.path.join(self.journal_dir, f"{self.transaction}.json")
        self.staging = os.path.join(self.journal_dir, f"staging-{self.transaction}")
        self.data_dir = os.path.join(self.staging, "data")
        self.backup_dir = os.path.join(self.staging, "backup")
        self.entries = []
        self.created_directories = []
        self.state = "new"
        self.created = time.time()

    def _plan(self):
        """
        Build the serializable plan of this transaction.
        """
        return {
            "version": _JOURNAL_VERSION,
            "transaction": self.transaction,
            "owner": self.transaction,
            "created": self.created,
            "state": self.state,
            "archive": self.archive,
            "extract_dir": self.extract_dir,
            "staging": self.staging,
            "entries": self.entries,
            "created_directories": self.created_directories,
        }

    def _forget(self):
        """
        Stop treating this transaction as active in this process.
        """
        with _ACTIVE_LOCK:
            _ACTIVE_TRANSACTIONS.discard(self.transaction)

    def prepare(self):
        """
        Create the staging area and record the start of the transaction.
        """
        with _ACTIVE_LOCK:
            _ACTIVE_TRANSACTIONS.add(self.transaction)
        try:
            self.state = "staging"
            os.makedirs(self.data_dir, exist_ok=True)
            _write_plan(self.plan_path, self._plan())
        except BaseException:
            self._forget()
            _remove_path(self.staging)
            raise

    def add_staged_files(self):
        """
        Record every file and directory produced in the staging area.

        The identity of each staged file is captured before publication.
        Moving it with ``os.replace`` keeps its inode, so after a crash this
        record tells exactly which final files belong to this transaction.
        """
        for path, directories, files in os.walk(self.data_dir):
            parent = os.path.relpath(path, self.data_dir)
            for name in sorted(directories):
                relative = os.path.normpath(os.path.join(parent, name))
                final = os.path.join(self.extract_dir, relative)
                self.entries.append(
                    {
                        "path": final,
                        "relative": relative,
                        "kind": "directory",
                        "owner": self.transaction,
                        "outcome": "staged",
                        "preexisting": os.path.isdir(final),
                    }
                )
            for name in sorted(files):
                relative = os.path.normpath(os.path.join(parent, name))
                final = os.path.join(self.extract_dir, relative)
                self.entries.append(
                    {
                        "path": final,
                        "relative": relative,
                        "kind": "file",
                        "owner": self.transaction,
                        "outcome": "staged",
                        # Recorded before any publication: a same-named file
                        # that was already there is never claimed as ours.
                        "preexisting": _fingerprint(final),
                        "fingerprint": _fingerprint(os.path.join(path, name)),
                    }
                )
        _write_plan(self.plan_path, self._plan())

    def _make_directory(self, path, journal=False):
        """
        Create a directory, recording any component created by this call.

        With *journal*, the new directories are persisted immediately so an
        interruption right after their creation can still clean them up.
        """
        created = False
        for directory in _missing_directories(path):
            try:
                os.mkdir(directory)
                self.created_directories.append(directory)
                created = True
            except FileExistsError:
                pass
        if created and journal:
            _write_plan(self.plan_path, self._plan())

    def _backup(self, final, backup):
        """
        Preserve the current file before replacing it with a staged one.
        """
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        try:
            os.link(final, backup, follow_symlinks=False)
        except OSError:
            shutil.copy2(final, backup, follow_symlinks=False)

    def _publish_file(self, entry):
        """
        Move one staged file into its final location.
        """
        final = entry["path"]
        staged = os.path.join(self.data_dir, entry["relative"])
        self._make_directory(os.path.dirname(final))
        existing = _fingerprint(final)
        if existing is not None and self.action == "fetch":
            # Never claim or overwrite a file this call didn't produce: it
            # may belong to another call or to the user.
            entry["outcome"] = "existing"
            entry["fingerprint"] = None
            return
        if existing is not None:
            self._backup(final, os.path.join(self.backup_dir, entry["relative"]))
        os.replace(staged, final)
        entry["outcome"] = "published"

    def publish(self):
        """
        Publish the staged files into the extraction folder.
        """
        self.state = "publishing"
        self._make_directory(self.extract_dir, journal=True)
        for entry in self.entries:
            if entry["kind"] != "directory":
                continue
            if entry["preexisting"]:
                entry["outcome"] = "existing"
            else:
                self._make_directory(entry["path"], journal=True)
                entry["outcome"] = "published"
        _write_plan(self.plan_path, self._plan())
        for entry in self.entries:
            if entry["kind"] == "file":
                self._publish_file(entry)
        _write_plan(self.plan_path, self._plan())

    def commit(self):
        """
        Mark the transaction as committed, then drop its records.

        If the committed state can't be written, the plan is moved aside as
        ``.committed`` so a replay can never mistake the successfully
        published files for leftovers and undo valid data.
        """
        try:
            self.state = "committed"
            _write_plan(self.plan_path, self._plan())
        except OSError:
            # Leave a marker the replay understands, then drop the plan. The
            # data was published successfully and must never be rolled back.
            marker = self.plan_path + ".committed"
            marked = False
            with contextlib.suppress(OSError):
                with open(marker, "w", encoding="utf-8"):
                    pass
                marked = True
            with contextlib.suppress(OSError):
                os.remove(self.plan_path)
            if not marked:
                # Without any record there is nothing that could replay a
                # rollback, so remove the staging leftovers directly.
                with contextlib.suppress(OSError):
                    _remove_path(self.staging)
        else:
            with contextlib.suppress(OSError):
                _cleanup_committed_transaction(self.plan_path, self._plan())
        self._forget()

    def rollback(self):
        """
        Undo this transaction, removing only artifacts that are still ours.
        """
        try:
            _rollback_transaction(self.plan_path, self._plan())
        finally:
            self._forget()


class ExtractorProcessor(abc.ABC):
    """
    Abstract base class for extractions from compressed archives.

    Subclasses can be used with :meth:`pooch.Pooch.fetch` and
    :func:`pooch.retrieve` to unzip a downloaded data file into a folder in the
    local data store. :meth:`~pooch.Pooch.fetch` will return a list with the
    names of the extracted files instead of the archive.

    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded zip file, with a suffix added).
        Otherwise, files will be unpacked to ``extract_dir``, which is
        interpreted as a *relative path* (relative to the cache location
        provided by :func:`pooch.retrieve` or :meth:`pooch.Pooch.fetch`).

    """

    _verb = "Extracting"

    def __init__(self, members=None, extract_dir=None):
        self.members = members
        self.extract_dir = extract_dir

    @property
    @abc.abstractmethod
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    @abc.abstractmethod
    def _all_members(self, fname):
        """
        Return all the members in the archive.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    @abc.abstractmethod
    def _extract_file(self, fname, extract_dir):
        """
        This method receives an argument for the archive to extract and the
        destination path.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    def _log_extraction(self, member, fname):
        """
        Log an extraction, reporting the final folder instead of staging.

        The actual writes go to a private staging folder that must never show
        up in the logs users see.
        """
        if member is None:
            get_logger().info(
                "%s contents of '%s' to '%s'",
                self._verb,
                fname,
                self.extract_dir,
            )
        else:
            get_logger().info(
                "Extracting '%s' from '%s' to '%s'", member, fname, self.extract_dir
            )

    def __call__(self, fname, action, pooch):  # noqa: ARG002
        """
        Extract all files from the given archive.

        Parameters
        ----------
        fname : str
            Full path of the zipped file in local storage.
        action : str
            Indicates what action was taken by :meth:`pooch.Pooch.fetch` or
            :func:`pooch.retrieve`:

            * ``"download"``: File didn't exist locally and was downloaded
            * ``"update"``: Local file was outdated and was re-download
            * ``"fetch"``: File exists and is updated so it wasn't downloaded

        pooch : :class:`pooch.Pooch`
            The instance of :class:`pooch.Pooch` that is calling this.

        Returns
        -------
        fnames : list of str
            A list of the full path to all files in the extracted archive.

        """
        if self.extract_dir is None:
            self.extract_dir = fname + self.suffix
        else:
            archive_dir = fname.rsplit(os.path.sep, maxsplit=1)[0]
            self.extract_dir = os.path.join(archive_dir, self.extract_dir)
        # Get a list of everyone who is supposed to be in the unpacked folder
        # so we can check if they are all there or if we need to extract new
        # files.
        if self.members is None or not self.members:
            members = self._all_members(fname)
        else:
            members = self.members
        # Finish or undo anything left behind by an interrupted call before
        # deciding whether this call needs to extract. The lock keeps replays
        # and publication from racing with other processes using the same
        # extraction folder.
        with _extraction_lock(self.extract_dir):
            _replay_stale_transactions(self.extract_dir)
            needs_extraction = (
                (action in ("update", "download"))
                or (not os.path.exists(self.extract_dir))
                or not all(
                    os.path.exists(os.path.join(self.extract_dir, m))
                    for m in members
                )
            )
            if needs_extraction:
                # Never write straight into the extraction folder. Extract
                # into a private staging area first so a failure cannot leave
                # a half-unpacked tree behind and so we know exactly which
                # files this call produced.
                transaction = _ExtractionTransaction(
                    fname, self.extract_dir, action
                )
                try:
                    transaction.prepare()
                    self._extract_file(fname, transaction.data_dir)
                    transaction.add_staged_files()
                    transaction.publish()
                    transaction.commit()
                except BaseException:
                    try:
                        transaction.rollback()
                    except OSError as error:
                        get_logger().warning(
                            "Could not roll back extraction to '%s': %s",
                            self.extract_dir,
                            error,
                        )
                    raise

        # Get a list of all file names (including subdirectories) in our folder
        # of unzipped files, filtered by the given members list
        fnames = []
        for path, _, files in os.walk(self.extract_dir):
            for filename in files:
                relpath = os.path.normpath(
                    os.path.join(os.path.relpath(path, self.extract_dir), filename)
                )
                if self.members is None or any(
                    relpath.startswith(os.path.normpath(m)) for m in self.members
                ):
                    fnames.append(os.path.join(path, filename))

        return fnames


class Unzip(ExtractorProcessor):
    """
    Processor that unpacks a zip archive and returns a list of all files.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to unzip a
    downloaded data file into a folder in the local data store. The
    method/function will return a list with the names of the unzipped files
    instead of the zip archive.

    The output folder is ``{fname}.unzip``.

    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the zip archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded zip file, with the suffix
        ``.unzip`` added). Otherwise, files will be unpacked to
        ``extract_dir``, which is interpreted as a *relative path* (relative to
        the cache location provided by :func:`pooch.retrieve` or
        :meth:`pooch.Pooch.fetch`).

    """

    @property
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        """
        return ".unzip"

    def _all_members(self, fname):
        """Return all members from a given archive."""
        with ZipFile(fname, "r") as zip_file:
            return zip_file.namelist()

    def _extract_file(self, fname, extract_dir):
        """
        This method receives an argument for the archive to extract and the
        destination path.
        """
        self._verb = "Unzipping"
        with ZipFile(fname, "r") as zip_file:
            if self.members is None:
                self._log_extraction(None, fname)
                # Unpack all files from the archive into our new folder
                zip_file.extractall(path=extract_dir)
            else:
                for member in self.members:
                    self._log_extraction(member, fname)
                    # If the member is a dir, we need to get the names of the
                    # elements it contains for extraction (ZipFile does not
                    # support dirs on .extract). If it's not a dir, this will
                    # only include the member itself.
                    # Based on:
                    # https://stackoverflow.com/questions/8008829/extract-only-a-single-directory-from-tar
                    subdir_members = [
                        name
                        for name in zip_file.namelist()
                        if os.path.normpath(name).startswith(os.path.normpath(member))
                    ]
                    # Extract the data file from within the archive
                    zip_file.extractall(members=subdir_members, path=extract_dir)


class Untar(ExtractorProcessor):
    """
    Processor that unpacks a tar archive and returns a list of all files.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to untar a
    downloaded data file into a folder in the local data store. The
    method/function will return a list with the names of the extracted files
    instead of the archive.

    The output folder is ``{fname}.untar``.


    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded tar file, with the suffix
        ``.untar`` added). Otherwise, files will be unpacked to
        ``extract_dir``, which is interpreted as a *relative path* (relative to
        the cache location  provided by :func:`pooch.retrieve` or
        :meth:`pooch.Pooch.fetch`).
    """

    @property
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        """
        return ".untar"

    def _all_members(self, fname):
        """Return all members from a given archive."""
        with TarFile.open(fname, "r") as tar_file:
            return [info.name for info in tar_file.getmembers()]

    def _extract_file(self, fname, extract_dir):
        """
        This method receives an argument for the archive to extract and the
        destination path.
        """
        self._verb = "Untarring"
        filter_kwarg = {} if sys.version_info < (3, 12) else {"filter": "data"}
        with TarFile.open(fname, "r") as tar_file:
            if self.members is None:
                self._log_extraction(None, fname)
                # Unpack all files from the archive into our new folder
                tar_file.extractall(path=extract_dir, **filter_kwarg)
            else:
                for member in self.members:
                    self._log_extraction(member, fname)
                    # If the member is a dir, we need to get the names of the
                    # elements it contains for extraction (TarFile does not
                    # support dirs on .extract). If it's not a dir, this will
                    # only include the member itself.
                    # Based on:
                    # https://stackoverflow.com/questions/8008829/extract-only-a-single-directory-from-tar
                    # Can't use .getnames because extractall expects TarInfo
                    # objects.
                    subdir_members = [
                        info
                        for info in tar_file.getmembers()
                        if os.path.normpath(info.name).startswith(
                            os.path.normpath(member)
                        )
                    ]
                    # Extract the data file from within the archive
                    tar_file.extractall(
                        members=subdir_members, path=extract_dir, **filter_kwarg
                    )


class Decompress:
    """
    Processor that decompress a file and returns the decompressed version.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to decompress
    a downloaded data file so that it can be easily opened. Useful for data
    files that take a long time to decompress (exchanging disk space for
    speed).

    Supported decompression methods are LZMA (``.xz``), bzip2 (``.bz2``), and
    gzip (``.gz``).

    File names with the standard extensions (see above) can use
    ``method="auto"`` to automatically determine the compression method. This
    can be overwritten by setting the *method* argument.

    .. note::

        To unpack zip and tar archives with one or more files, use
        :class:`pooch.Unzip` and :class:`pooch.Untar` instead.

    The output file is ``{fname}.decomp`` by default but it can be changed by
    setting the ``name`` parameter.

    .. warning::

        Passing in ``name`` can cause existing data to be lost! For example, if
        a file already exists with the specified name it will be overwritten
        with the new decompressed file content. **Use this option with
        caution.**

    Parameters
    ----------
    method : str
        Name of the compression method. Can be "auto", "lzma", "xz", "bzip2",
        or "gzip".
    name : None or str
        Defines the decompressed file name. The file name will be
        ``{fname}.decomp`` if ``None`` (default) or the given name otherwise.
        Note that the name should **not** include the full (or relative) path,
        it should be just the file name itself.

    """

    modules: typing.ClassVar = {
        "auto": None,
        "lzma": lzma,
        "xz": lzma,
        "gzip": gzip,
        "bzip2": bz2,
    }
    extensions: typing.ClassVar = {".xz": "lzma", ".gz": "gzip", ".bz2": "bzip2"}

    def __init__(self, method="auto", name=None):
        self.method = method
        self.name = name

    def __call__(self, fname, action, pooch):  # noqa: ARG002
        """
        Decompress the given file.

        The output file will be either ``{fname}.decomp`` or the given *name*
        class attribute.

        Parameters
        ----------
        fname : str
            Full path of the compressed file in local storage.
        action : str
            Indicates what action was taken by :meth:`pooch.Pooch.fetch` or
            :func:`pooch.retrieve`:

            - ``"download"``: File didn't exist locally and was downloaded
            - ``"update"``: Local file was outdated and was re-download
            - ``"fetch"``: File exists and is updated so it wasn't downloaded

        pooch : :class:`pooch.Pooch`
            The instance of :class:`pooch.Pooch` that is calling this.

        Returns
        -------
        fname : str
            The full path to the decompressed file.
        """
        if self.name is None:
            decompressed = fname + ".decomp"
        else:
            decompressed = os.path.join(os.path.dirname(fname), self.name)
        if action in ("update", "download") or not os.path.exists(decompressed):
            get_logger().info(
                "Decompressing '%s' to '%s' using method '%s'.",
                fname,
                decompressed,
                self.method,
            )
            module = self._compression_module(fname)
            with open(decompressed, "w+b") as output, module.open(fname) as compressed:
                shutil.copyfileobj(compressed, output)
        return decompressed

    def _compression_module(self, fname):
        """
        Get the Python module compatible with fname and the chosen method.

        If the *method* attribute is "auto", will select a method based on the
        extension. If no recognized extension is in the file name, will raise a
        ValueError.
        """
        error_archives = "To unpack zip/tar archives, use pooch.Unzip/Untar instead."
        if self.method not in self.modules:
            message = (
                f"Invalid compression method '{self.method}'. "
                f"Must be one of '{list(self.modules.keys())}'."
            )
            if self.method in {"zip", "tar"}:
                message = " ".join([message, error_archives])
            raise ValueError(message)
        if self.method == "auto":
            ext = os.path.splitext(fname)[-1]
            if ext not in self.extensions:
                message = (
                    f"Unrecognized file extension '{ext}'. "
                    f"Must be one of '{list(self.extensions.keys())}'."
                )
                if ext in {".zip", ".tar"}:
                    message = " ".join([message, error_archives])
                raise ValueError(message)
            return self.modules[self.extensions[ext]]
        return self.modules[self.method]
