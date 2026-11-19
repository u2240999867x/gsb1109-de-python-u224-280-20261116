"""Two-phase data handoff with resumable, crash-safe uploads.

Uploads are received into a temporary file first and only "published"
(atomically renamed) once the payload has been verified.  A file enters
the official manifest only when both payload verification and the
publish rename succeed.

Guarantees:

- every upload session gets a unique ``upload_id``; the temporary file
  and the receipt are keyed by it, so a same-name re-upload can never
  claim another session's receipt;
- receiving state is persisted on disk and recovered on startup, so an
  interrupted upload can be resumed from the exact byte offset;
- a half-finished upload lives only in the ``incoming/`` area and never
  overwrites a previously published version;
- publishing is idempotent: repeating ``publish()`` for the same
  ``upload_id`` returns the same receipt without side effects.

Layout under the store root directory::

    <root>/incoming/<upload_id>.part   temporary payload being received
    <root>/state/<upload_id>.json      resumable receiving state
    <root>/receipts/<upload_id>.json   publish receipt (proof of handoff)
    <root>/published/<name>            published files
    <root>/manifest.json               official list of published files
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid

__all__ = [
    "HandoffStore",
    "HandoffError",
    "UnknownUploadError",
    "InvalidStateError",
    "ChecksumMismatchError",
    "Receipt",
]

_CHUNK_SIZE = 64 * 1024

#: receiving: upload in progress, resumable
#: published: payload verified and renamed into published/
#: failed:    payload verification failed (terminal)
#: aborted:   upload cancelled by the client (terminal)
_STATUSES = ("receiving", "published", "failed", "aborted")


class HandoffError(Exception):
    """Base class for handoff errors."""


class UnknownUploadError(HandoffError):
    """Raised when referencing an upload_id that does not exist."""


class InvalidStateError(HandoffError):
    """Raised when an operation is not allowed in the current state."""


class ChecksumMismatchError(HandoffError):
    """Raised when the received payload fails verification."""


class Receipt:
    """Proof that an upload was verified and published.

    ``idempotent_replay`` is True when the receipt comes from a repeated
    ``publish()`` call for an already published upload.
    """

    def __init__(
        self, upload_id, name, size, sha256, published_at,
        idempotent_replay=False,
    ):
        self.upload_id = upload_id
        self.name = name
        self.size = size
        self.sha256 = sha256
        self.published_at = published_at
        self.idempotent_replay = idempotent_replay

    def as_dict(self):
        return {
            "upload_id": self.upload_id,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "published_at": self.published_at,
            "idempotent_replay": self.idempotent_replay,
        }

    def __eq__(self, other):
        return (
            isinstance(other, Receipt)
            and self.upload_id == other.upload_id
            and self.name == other.name
            and self.size == other.size
            and self.sha256 == other.sha256
            and self.published_at == other.published_at
        )

    def __repr__(self):
        return (
            f"<Receipt upload_id={self.upload_id!r} name={self.name!r} "
            f"size={self.size} replay={self.idempotent_replay}>"
        )


def _atomic_write_json(path, data):
    """Write *data* as JSON to *path* atomically (tmp file + rename)."""
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=".tmp-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _sanitize_name(name):
    """Reduce *name* to a plain file name, rejecting empty/path input."""
    name = os.path.basename(name.strip().replace("\\", "/"))
    if not name or name in (".", ".."):
        raise ValueError(f"invalid file name: {name!r}")
    return name


class HandoffStore:
    """Manage two-phase (temp file -> rename) resumable uploads."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.incoming_dir = os.path.join(self.root, "incoming")
        self.state_dir = os.path.join(self.root, "state")
        self.receipt_dir = os.path.join(self.root, "receipts")
        self.published_dir = os.path.join(self.root, "published")
        self.manifest_path = os.path.join(self.root, "manifest.json")
        for path in (
            self.incoming_dir,
            self.state_dir,
            self.receipt_dir,
            self.published_dir,
        ):
            os.makedirs(path, exist_ok=True)
        if not os.path.exists(self.manifest_path):
            _atomic_write_json(self.manifest_path, {"entries": {}})
        self._lock = threading.RLock()
        self._states = {}
        self._recover()

    # -- paths ---------------------------------------------------------

    def _part_path(self, upload_id):
        return os.path.join(self.incoming_dir, upload_id + ".part")

    def _state_path(self, upload_id):
        return os.path.join(self.state_dir, upload_id + ".json")

    def _receipt_path(self, upload_id):
        return os.path.join(self.receipt_dir, upload_id + ".json")

    def _published_path(self, name):
        return os.path.join(self.published_dir, name)

    # -- recovery ------------------------------------------------------

    def _recover(self):
        """Rebuild in-memory receiving state from disk.

        The received offset is derived from the actual size of the
        temporary file, so recovery is accurate even if the process
        died in the middle of a write.
        """
        for fname in os.listdir(self.state_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.state_dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                continue
            if state.get("status") not in _STATUSES:
                continue
            upload_id = state["upload_id"]
            if state["status"] == "receiving":
                part = self._part_path(upload_id)
                if not os.path.exists(part):
                    # payload is gone; the session cannot be resumed
                    state["status"] = "aborted"
                    _atomic_write_json(path, state)
            self._states[upload_id] = state

    # -- state helpers ---------------------------------------------------

    def _load_state(self, upload_id):
        try:
            return self._states[upload_id]
        except KeyError:
            raise UnknownUploadError(f"no such upload: {upload_id!r}") from None

    def _save_state(self, state):
        state["updated_at"] = time.time()
        _atomic_write_json(self._state_path(state["upload_id"]), state)

    def _offset_of(self, upload_id):
        try:
            return os.path.getsize(self._part_path(upload_id))
        except OSError:
            return 0

    # -- public API ------------------------------------------------------

    def begin_upload(self, name):
        """Start a new upload session for *name* and return its upload_id.

        Every call creates a brand new session with a unique id, even if
        another session (finished or not) exists for the same name.
        """
        name = _sanitize_name(name)
        upload_id = uuid.uuid4().hex
        with self._lock:
            # create the temp payload file eagerly so that a crash right
            # after begin_upload() still recovers a consistent state
            fd = os.open(
                self._part_path(upload_id),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(fd)
            state = {
                "upload_id": upload_id,
                "name": name,
                "status": "receiving",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self._states[upload_id] = state
            self._save_state(state)
        return upload_id

    def find_resumable(self, name):
        """Return the upload_id of a resumable session for *name*, if any."""
        name = _sanitize_name(name)
        with self._lock:
            for state in self._states.values():
                if state["status"] == "receiving" and state["name"] == name:
                    return state["upload_id"]
        return None

    def offset(self, upload_id):
        """Return the number of bytes received so far (resume position)."""
        with self._lock:
            self._load_state(upload_id)
            return self._offset_of(upload_id)

    def temp_path(self, upload_id):
        """Return the path of the temporary payload file for *upload_id*."""
        with self._lock:
            self._load_state(upload_id)
            return self._part_path(upload_id)

    def write(self, upload_id, data, offset=None):
        """Write *data* to the temporary payload and return the new offset.

        *offset* defaults to the current end of the payload.  Passing an
        explicit offset allows out-of-order resume; it must not exceed
        the number of bytes already received.
        """
        if not data:
            raise ValueError("no data to write")
        with self._lock:
            state = self._load_state(upload_id)
            if state["status"] != "receiving":
                raise InvalidStateError(
                    f"cannot write to upload in state {state['status']!r}"
                )
            current = self._offset_of(upload_id)
            if offset is None:
                offset = current
            if offset < 0 or offset > current:
                raise InvalidStateError(
                    f"invalid offset {offset}: {current} bytes received so far"
                )
            with open(self._part_path(upload_id), "r+b") as f:
                f.seek(offset)
                f.write(data)
            return offset + len(data)

    def status(self, upload_id):
        """Return a snapshot of the receiving state for *upload_id*."""
        with self._lock:
            state = dict(self._load_state(upload_id))
            state["received"] = self._offset_of(upload_id)
            return state

    def publish(self, upload_id, sha256=None):
        """Verify the payload and atomically publish it under its name.

        The upload enters the official manifest only if both the payload
        verification and the publish rename succeed.  Repeating this
        call for an already published upload is idempotent: the original
        receipt is returned with ``idempotent_replay`` set to True.
        """
        with self._lock:
            state = self._load_state(upload_id)
            if state["status"] == "published":
                receipt = self._load_receipt(upload_id)
                # self-heal in case a previous run crashed between the
                # rename and the manifest update
                self._manifest_ensure(receipt)
                return Receipt(idempotent_replay=True, **receipt)
            if state["status"] != "receiving":
                raise InvalidStateError(
                    f"cannot publish upload in state {state['status']!r}"
                )

            part = self._part_path(upload_id)
            digest, size = self._hash_file(part)
            if sha256 is not None and digest != sha256.lower():
                state["status"] = "failed"
                state["error"] = "checksum mismatch"
                self._save_state(state)
                raise ChecksumMismatchError(
                    f"payload verification failed for {state['name']!r}: "
                    f"expected sha256 {sha256}, got {digest}"
                )

            # payload verified: durably rename temp file into published/.
            # os.replace is atomic on the same filesystem, so a previous
            # published version is swapped out only now, never by a
            # half-finished upload.
            final_path = self._published_path(state["name"])
            with open(part, "rb") as f:
                os.fsync(f.fileno())
            os.replace(part, final_path)

            receipt = {
                "upload_id": upload_id,
                "name": state["name"],
                "size": size,
                "sha256": digest,
                "published_at": time.time(),
            }
            _atomic_write_json(self._receipt_path(upload_id), receipt)
            state["status"] = "published"
            self._save_state(state)
            self._manifest_ensure(receipt)
            return Receipt(idempotent_replay=False, **receipt)

    def abort(self, upload_id):
        """Cancel an in-progress upload and remove its temporary payload."""
        with self._lock:
            state = self._load_state(upload_id)
            if state["status"] != "receiving":
                raise InvalidStateError(
                    f"cannot abort upload in state {state['status']!r}"
                )
            try:
                os.remove(self._part_path(upload_id))
            except FileNotFoundError:
                pass
            state["status"] = "aborted"
            self._save_state(state)

    def manifest(self):
        """Return the official list of published files."""
        with self._lock:
            with open(self.manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return [dict(entry) for entry in data["entries"].values()]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _load_receipt(self, upload_id):
        with open(self._receipt_path(upload_id), encoding="utf-8") as f:
            return json.load(f)

    def _manifest_ensure(self, receipt):
        """Record *receipt* in the manifest, newest publish wins.

        Idempotent: writing the same receipt twice is a no-op, and an
        older receipt never displaces a newer entry for the same name.
        """
        entry = {
            "upload_id": receipt["upload_id"],
            "name": receipt["name"],
            "size": receipt["size"],
            "sha256": receipt["sha256"],
            "published_at": receipt["published_at"],
        }
        with open(self.manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        existing = data["entries"].get(receipt["name"])
        if existing is not None:
            if existing == entry:
                return
            if existing.get("published_at", 0) >= receipt["published_at"]:
                return
        data["entries"][receipt["name"]] = entry
        _atomic_write_json(self.manifest_path, data)
