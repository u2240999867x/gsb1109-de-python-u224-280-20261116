# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#

"""
Reference counting for files shared between data packages.

Files downloaded (or produced by processors) can be shared by more than one
:class:`~pooch.Pooch` instance. When a registry entry is removed from a single
package version, the corresponding file must only be deleted when no other
package still references it.

The reference counts are stored in a small JSON file inside each cache
directory. Every count is keyed by the path of the referenced file, relative
to the directory holding the index, so the cache folder stays portable.
"""

import json
import os
import threading

from .utils import get_logger


def _default_index_path(cache_dir):
    """
    Return the location of the reference index for *cache_dir*.
    """
    return os.path.join(str(cache_dir), ".pooch-references.json")


class SharedFileRegistry:
    """
    Persistent reference counts for files shared across data packages.

    The registry lives next to the cached files and records how many package
    entries currently rely on each file. Adding a reference increments the
    count; removing one decrements it. The underlying file (and any emptied
    parent directories) is only deleted once its count reaches zero.

    Parameters
    ----------
    cache_dir : str or pathlib.Path
        Directory that contains the cached files tracked by this registry.
    index_file : str or pathlib.Path or None
        Location of the JSON index. If None, it is stored in a hidden file
        named ``.pooch-references.json`` inside *cache_dir*.

    """

    def __init__(self, cache_dir, index_file=None):
        self.cache_dir = os.path.abspath(os.fspath(cache_dir))
        if index_file is None:
            index_file = _default_index_path(self.cache_dir)
        self.index_file = os.path.abspath(os.fspath(index_file))
        # Reentrant so bulk operations (e.g. remove_owner) can reuse the
        # single-file helpers while still holding the lock.
        self._lock = threading.RLock()

    def _load(self):
        """Read the reference index from disk, tolerating a missing file."""
        if not os.path.exists(self.index_file):
            return {}
        with open(self.index_file, encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError:
                get_logger().warning(
                    "Ignoring corrupted reference index '%s'.", self.index_file
                )
                return {}
        if not isinstance(data, dict):
            get_logger().warning(
                "Ignoring malformed reference index '%s'.", self.index_file
            )
            return {}
        references = {}
        for key, entry in data.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            owners = entry.get("owners")
            count = entry.get("count")
            if not isinstance(owners, list) or not all(
                isinstance(owner, str) for owner in owners
            ):
                continue
            if not isinstance(count, int) or count != len(owners) or count <= 0:
                continue
            references[key] = {"count": count, "owners": list(owners)}
        return references

    def _save(self, references):
        """Persist the reference index atomically."""
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)
        temporary = self.index_file + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(references, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.index_file)

    def _relative_key(self, file_path):
        """
        Convert an absolute file path to a portable index key.

        Raises
        ------
        ValueError
            If *file_path* is not contained in the cache directory.
        """
        absolute = os.path.abspath(os.fspath(file_path))
        root = self.cache_dir
        if absolute != root and not absolute.startswith(root + os.path.sep):
            message = (
                f"Cannot track '{absolute}': it is outside of the cache "
                f"directory '{root}'."
            )
            raise ValueError(message)
        return os.path.relpath(absolute, root)

    def add(self, file_path, owner):
        """
        Record that *owner* references *file_path*.

        Re-adding the same owner does not inflate the count: each owner is
        counted once per file. The absolute path to the tracked file is
        returned so callers can keep using it directly.

        Parameters
        ----------
        file_path : str or pathlib.Path
            File that is being referenced.
        owner : str
            Stable identifier of the referencing package entry, for example
            ``"{package_name}:{version}:{file_name}"``.

        Returns
        -------
        absolute_path : str
            The absolute path of the tracked file.
        """
        with self._lock:
            references = self._load()
            key = self._relative_key(file_path)
            entry = references.setdefault(
                key, {"count": 0, "owners": []}
            )
            if owner not in entry["owners"]:
                entry["owners"].append(owner)
                entry["count"] = len(entry["owners"])
                # Persist only when the reference actually changed so
                # repeated fetches of an already tracked file stay cheap.
                self._save(references)
        return os.path.join(self.cache_dir, key)

    def remove(self, file_path, owner, cleanup=True):
        """
        Drop *owner*'s reference to *file_path*.

        The file is deleted only when no owners remain. Empty parent
        directories below the cache root are removed afterwards when
        *cleanup* is True. The cache directory itself and the reference index
        are never deleted.

        Parameters
        ----------
        file_path : str or pathlib.Path
            File whose reference should be removed.
        owner : str
            Stable identifier used when the reference was added.
        cleanup : bool
            When True (default), delete the file once unreferenced and prune
            emptied directories.

        Returns
        -------
        deleted : bool
            True if the file was removed from disk, False if other owners
            still reference it (or if it was never tracked).
        """
        with self._lock:
            references = self._load()
            key = self._relative_key(file_path)
            entry = references.get(key)
            if entry is None or owner not in entry["owners"]:
                get_logger().info(
                    "No reference from '%s' to '%s' was recorded; nothing to do.",
                    owner,
                    key,
                )
                return False
            entry["owners"].remove(owner)
            entry["count"] = len(entry["owners"])
            deleted = False
            if entry["count"] == 0:
                del references[key]
                if cleanup:
                    absolute = os.path.join(self.cache_dir, key)
                    deleted = self._delete_file(absolute)
            self._save(references)
            if cleanup:
                self._prune_empty_directories(key)
            return deleted

    def _delete_file(self, absolute):
        """Delete a single unreferenced file, tolerating an already gone file."""
        try:
            os.remove(absolute)
        except FileNotFoundError:
            return False
        except OSError as error:
            get_logger().warning(
                "Could not remove unreferenced file '%s': %s", absolute, error
            )
            return False
        get_logger().info(
            "Removed unreferenced file '%s' from the cache.", absolute
        )
        return True

    def _prune_empty_directories(self, relative_key):
        """
        Remove directories left empty after a deletion, up to the cache root.
        """
        relative_dir = os.path.dirname(relative_key)
        while relative_dir:
            directory = os.path.join(self.cache_dir, relative_dir)
            try:
                os.rmdir(directory)
            except OSError:
                break
            get_logger().info("Removed empty directory '%s'.", directory)
            relative_dir = os.path.dirname(relative_dir)

    def count(self, file_path):
        """
        Return the current number of owners referencing *file_path*.
        """
        with self._lock:
            references = self._load()
            entry = references.get(self._relative_key(file_path))
            return 0 if entry is None else entry["count"]

    def owners(self, file_path):
        """
        Return the sorted list of owners referencing *file_path*.
        """
        with self._lock:
            references = self._load()
            entry = references.get(self._relative_key(file_path))
            return [] if entry is None else sorted(entry["owners"])

    def is_shared(self, file_path):
        """
        Return True if more than one owner references *file_path*.
        """
        return self.count(file_path) > 1

    def tracked_files(self):
        """
        Return absolute paths of every tracked file and its owner list.
        """
        with self._lock:
            references = self._load()
        return {
            os.path.join(self.cache_dir, key): sorted(entry["owners"])
            for key, entry in references.items()
        }

    def remove_owner(self, owner, cleanup=True):
        """
        Remove every reference held by *owner*.

        Useful when a package version drops a registry entry: only that
        version's references are removed while shared files keep their other
        owners. Returns the paths that were actually deleted.

        Parameters
        ----------
        owner : str
            Stable identifier of the package entry being removed.
        cleanup : bool
            When True (default), delete files that become unreferenced.

        Returns
        -------
        deleted : list of str
            Absolute paths removed from disk.
        """
        with self._lock:
            references = self._load()
            affected = [
                key
                for key, entry in references.items()
                if owner in entry["owners"]
            ]
        deleted = []
        for key in affected:
            absolute = os.path.join(self.cache_dir, key)
            if self.remove(absolute, owner, cleanup=cleanup):
                deleted.append(absolute)
        return deleted
