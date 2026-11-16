"""Snapshot planning for UNIX_FD references carried by D-Bus messages.

A D-Bus message only stores ``UNIX_FD`` (``h``) parameters as uint32 indices
into the out-of-band descriptor array (``Message.unix_fds``).  The integers
inside the message body are therefore not handles themselves; the actual
references can only be found by walking the parsed signature tree.

This module turns a received message plus the receiver-side manifest (the
list of handles, their resource types, sharing relationships and the read
methods the receiver is authorized to use) into a :class:`SnapshotPlan`.

Only references backed by an authorized, reproducible resource produce a
snapshot task.  Regular files support independent ranged reads (``pread``),
while pipes and sockets are one-shot consumers: their material is retained as
pending evidence and is never read just to compute a digest.  Handles that
share one resource share one verified material and are consumed at most once.
"""
import hashlib
import os
import stat
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Tuple

from .errors import SignatureBodyMismatchError
from .message import Message
from .signature import SignatureTree, SignatureType, Variant


class ResourceType(Enum):
    """The kind of resource a received descriptor refers to."""
    REGULAR = 'regular'
    PIPE = 'pipe'
    SOCKET = 'socket'
    OTHER = 'other'


class ReadMethod(Enum):
    """The read guarantees a resource is authorized for.

    ``PREAD`` is an independent ranged read that does not mutate a shared
    offset and is only valid on regular files.  ``CONSUME`` and ``RECV`` are
    one-shot reads on pipes and sockets respectively: the bytes can only be
    taken once.
    """
    PREAD = 'pread'
    CONSUME = 'consume'
    RECV = 'recv'


class FdReference(NamedTuple):
    """A single ``h`` parameter found while walking the message body.

    :ivar path: The location of the parameter inside the body, e.g. ``[0][2]``.
    :ivar fd_index: The uint32 value stored in the body (index into ``unix_fds``).
    :ivar handle: The resolved descriptor, or ``None`` if the index is unbacked.
    """
    path: str
    fd_index: int
    handle: Optional[int]


class DescriptorEntry(NamedTuple):
    """Receiver-side knowledge about one received descriptor.

    :ivar resource_id: Stable identity of the underlying resource.
    :ivar resource_type: The resource type declared by the receiver.
    :ivar read_methods: Read methods the receiver knows this handle supports.
        Whether their use is authorized is given by
        :attr:`ReceiverManifest.allowed_reads`.
    :ivar offset: Start offset for ranged reads of regular files.
    :ivar length: Number of bytes to capture, or ``None`` to read to EOF.
    :ivar expected_stat: Optional identity fields checked against ``fstat``.
    :ivar note: Free-form receiver-side annotation.
    """
    resource_id: str
    resource_type: ResourceType
    read_methods: FrozenSet[ReadMethod] = frozenset()
    offset: Optional[int] = None
    length: Optional[int] = None
    expected_stat: Optional[Mapping[str, int]] = None
    note: str = ''


class SnapshotTask(NamedTuple):
    """A planned, reproducible capture of one authorized regular file."""
    resource_id: str
    handle: int
    read_method: ReadMethod
    offset: int
    length: Optional[int]
    references: Tuple[FdReference, ...]


class PendingCapture(NamedTuple):
    """A reference whose material cannot be captured without consuming it.

    Pipes and sockets are one-shot resources.  The reference is retained for
    later, externally supplied material and the stream itself is not read.
    """
    resource_id: str
    handle: int
    resource_type: ResourceType
    read_methods: FrozenSet[ReadMethod]
    reason: str
    references: Tuple[FdReference, ...]


class UnresolvedReference(NamedTuple):
    """A reference that cannot produce a snapshot and needs follow-up."""
    resource_id: Optional[str]
    handle: Optional[int]
    reason: str
    references: Tuple[FdReference, ...]


class SnapshotResult(NamedTuple):
    """Material captured for one resource, with full provenance."""
    resource_id: str
    handle: int
    data: bytes
    digest: str
    stat: Mapping[str, int]
    references: Tuple[FdReference, ...]


class SnapshotVersionError(RuntimeError):
    """Raised when a file cannot be version-certified across its read.

    The snapshot for the resource is not published.  Successfully captured
    snapshots from the other tasks are attached as ``partial`` so no verified
    byte has to be captured twice.
    """
    def __init__(self, task: SnapshotTask, before: os.stat_result,
                 after: os.stat_result, partial: List[SnapshotResult]):
        super().__init__(
            f'resource {task.resource_id!r} changed while reading '
            f'(before ino={before.st_ino} size={before.st_size} '
            f'mtime_ns={before.st_mtime_ns}; after ino={after.st_ino} '
            f'size={after.st_size} mtime_ns={after.st_mtime_ns})')
        self.task = task
        self.before = before
        self.after = after
        self.partial = partial


class ArchivePlanError(ValueError):
    """Raised when the message body does not match its declared signature."""


# Reasons recorded on PendingCapture / UnresolvedReference entries.
REASON_STREAM_ONE_SHOT = 'stream-one-shot-consume'
REASON_STREAM_UNAUTHORIZED = 'stream-consume-not-authorized'
REASON_FD_INDEX_UNBACKED = 'fd-index-unbacked'
REASON_UNAUTHORIZED_HANDLE = 'unauthorized-handle'
REASON_ALIAS_TARGET_UNKNOWN = 'alias-target-unknown'
REASON_READ_METHOD_NOT_ALLOWED = 'read-method-not-allowed'
REASON_RESOURCE_TYPE_MISMATCH = 'resource-type-mismatch'
REASON_IDENTITY_MISMATCH = 'identity-mismatch'
REASON_DESCRIPTOR_UNAVAILABLE = 'descriptor-unavailable'
REASON_UNSUPPORTED_RESOURCE_TYPE = 'unsupported-resource-type'

_VERSION_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
_READ_CHUNK = 64 * 1024


def _walk_signature(type_: SignatureType, value: Any,
                    parts: Tuple[str, ...]) -> List[Tuple[str, int]]:
    """Yield ``(parameter_path, fd_index)`` pairs for every ``h`` token."""
    token = type_.token

    if token == 'h':
        if not isinstance(value, int) or isinstance(value, bool):
            raise ArchivePlanError(
                f'UNIX_FD parameter at {"".join(parts)} must be an integer index, '
                f'got {type(value).__name__}')
        return [(''.join(parts), value)]

    if token == 'v':
        if not isinstance(value, Variant):
            raise ArchivePlanError(
                f'variant parameter at {"".join(parts)} must be a Variant, '
                f'got {type(value).__name__}')
        return _walk_signature(value.type, value.value, parts + ('[v]',))

    if token == 'a':
        child = type_.children[0]
        if child.token == '{':
            key_type = child.children[0]
            value_type = child.children[1]
            found: List[Tuple[str, int]] = []
            for key, member in value.items():
                found.extend(
                    _walk_signature(key_type, key, parts + (f'[{key!r}].key',)))
                found.extend(
                    _walk_signature(value_type, member, parts + (f'[{key!r}]',)))
            return found
        if child.token == 'y':
            # a byte array carries no handles
            return []
        found = []
        for index, member in enumerate(value):
            found.extend(_walk_signature(child, member, parts + (f'[{index}]',)))
        return found

    if token == '(':
        found = []
        for index, (child, member) in enumerate(zip(type_.children, value)):
            found.extend(_walk_signature(child, member, parts + (f'[{index}]',)))
        return found

    # every other basic type (integers, strings, ...) is not a handle
    return []


def find_fd_references(signature: str, body: List[Any]) -> List[FdReference]:
    """Parse the body against ``signature`` and return its ``h`` references.

    Plain integer parameters (``y``, ``n``, ``q``, ``i``, ``u``, ``x``,
    ``t``) are never reported: only ``h`` tokens reference descriptors.
    Indices are returned unresolved; mapping them onto handles is the
    planner's job.
    """
    tree = SignatureTree._get(signature)
    try:
        tree.verify(body)
    except SignatureBodyMismatchError as ex:
        raise ArchivePlanError(str(ex))

    references: List[FdReference] = []
    for index, (type_, value) in enumerate(zip(tree.types, body)):
        for path, fd_index in _walk_signature(type_, value, (f'[{index}]',)):
            references.append(FdReference(path=path, fd_index=fd_index, handle=None))
    return references


class ReceiverManifest:
    """The receiver-side inventory of what the received descriptors may be.

    :param descriptors: Handle to :class:`DescriptorEntry` mapping.
    :param aliases: Handle to canonical-handle mapping for descriptors known
        to duplicate another one's underlying material.
    :param allowed_reads: Resource id to the set of authorized read methods.
    """
    def __init__(self,
                 descriptors: Mapping[int, DescriptorEntry],
                 aliases: Optional[Mapping[int, int]] = None,
                 allowed_reads: Optional[Mapping[str, FrozenSet[ReadMethod]]] = None):
        self.descriptors: Dict[int, DescriptorEntry] = dict(descriptors)
        self.aliases: Dict[int, int] = dict(aliases or {})
        self.allowed_reads: Dict[str, FrozenSet[ReadMethod]] = {
            resource_id: frozenset(methods)
            for resource_id, methods in (allowed_reads or {}).items()
        }
        self._canonical_cache: Dict[int, int] = {}

    def canonical_handle(self, handle: int) -> int:
        """Resolve sharing aliases to the handle that holds shared material."""
        if handle in self._canonical_cache:
            return self._canonical_cache[handle]

        seen = set()
        current = handle
        while current in self.aliases:
            if current in seen:
                raise ArchivePlanError(f'alias cycle detected at handle {current}')
            seen.add(current)
            current = self.aliases[current]
        self._canonical_cache[handle] = current
        return current


class SnapshotPlan:
    """The archive plan derived from one message.

    :ivar references: Every ``h`` parameter found in the message body.
    :ivar tasks: Reproducible captures, one per authorized regular-file resource.
    :ivar pending: One-shot streams retained for later material.
    :ivar unresolved: References that cannot be archived and need follow-up.
    """
    def __init__(self,
                 references: List[FdReference],
                 tasks: List[SnapshotTask],
                 pending: List[PendingCapture],
                 unresolved: List[UnresolvedReference]):
        self.references: Tuple[FdReference, ...] = tuple(references)
        self.tasks: Tuple[SnapshotTask, ...] = tuple(tasks)
        self.pending: Tuple[PendingCapture, ...] = tuple(pending)
        self.unresolved: Tuple[UnresolvedReference, ...] = tuple(unresolved)

    @property
    def is_complete(self) -> bool:
        """True when every reference has a publishable snapshot task."""
        return not self.pending and not self.unresolved

    def references_for(self, resource_id: str) -> Tuple[FdReference, ...]:
        for task in self.tasks:
            if task.resource_id == resource_id:
                return task.references
        for pending in self.pending:
            if pending.resource_id == resource_id:
                return pending.references
        for item in self.unresolved:
            if item.resource_id == resource_id:
                return item.references
        return ()

    def execute(self) -> List[SnapshotResult]:
        """Capture every planned task. See :func:`execute_snapshot_plan`."""
        return execute_snapshot_plan(self)


def _stat_evidence(stat_result: os.stat_result) -> Dict[str, int]:
    return {
        'st_dev': stat_result.st_dev,
        'st_ino': stat_result.st_ino,
        'st_mode': stat_result.st_mode,
        'st_size': stat_result.st_size,
        'st_mtime_ns': stat_result.st_mtime_ns,
        'st_ctime_ns': stat_result.st_ctime_ns,
    }


def _identity_matches(stat_result: os.stat_result,
                      expected: Optional[Mapping[str, int]]) -> bool:
    if not expected:
        return True
    return all(getattr(stat_result, key) == value for key, value in expected.items())


def _merge(tasks: Dict[str, SnapshotTask],
           task: SnapshotTask) -> None:
    # shared references reuse the one verified material: the first task for a
    # resource is kept and read once, every parameter path is folded into it.
    existing = tasks.get(task.resource_id)
    if existing is None:
        tasks[task.resource_id] = task
        return
    tasks[task.resource_id] = existing._replace(
        references=existing.references + task.references)


def plan_body_archive(signature: str, body: List[Any], unix_fds: List[int],
                      manifest: ReceiverManifest) -> SnapshotPlan:
    """Build a :class:`SnapshotPlan` for a parsed message body."""
    raw_references = find_fd_references(signature, body)
    references: List[FdReference] = []
    refs_by_handle: Dict[int, List[FdReference]] = {}
    unresolved: List[UnresolvedReference] = []

    for reference in raw_references:
        if reference.fd_index < 0 or reference.fd_index >= len(unix_fds):
            resolved = reference
            unresolved.append(
                UnresolvedReference(resource_id=None,
                                    handle=None,
                                    reason=REASON_FD_INDEX_UNBACKED,
                                    references=(reference,)))
        else:
            handle = unix_fds[reference.fd_index]
            resolved = reference._replace(handle=handle)
            refs_by_handle.setdefault(handle, []).append(resolved)
        references.append(resolved)

    tasks: Dict[str, SnapshotTask] = {}
    pending_by_resource: Dict[str, PendingCapture] = {}
    pending_refs: Dict[str, List[FdReference]] = {}

    def mark_pending(entry: DescriptorEntry, handle: int,
                     resource_type: ResourceType, reason: str,
                     refs: List[FdReference]) -> None:
        pending_refs.setdefault(entry.resource_id, []).extend(refs)
        pending_by_resource[entry.resource_id] = PendingCapture(
            resource_id=entry.resource_id,
            handle=handle,
            resource_type=resource_type,
            read_methods=frozenset(entry.read_methods),
            reason=reason,
            references=tuple(pending_refs[entry.resource_id]))

    def mark_unresolved(resource_id: Optional[str], handle: Optional[int],
                        reason: str, refs: List[FdReference]) -> None:
        unresolved.append(
            UnresolvedReference(resource_id=resource_id,
                                handle=handle,
                                reason=reason,
                                references=tuple(refs)))

    for handle, refs in refs_by_handle.items():
        entry = manifest.descriptors.get(handle)
        if entry is None:
            mark_unresolved(None, handle, REASON_UNAUTHORIZED_HANDLE, refs)
            continue

        canonical = manifest.canonical_handle(handle)
        canonical_entry = manifest.descriptors.get(canonical)
        if canonical_entry is None:
            mark_unresolved(entry.resource_id, handle,
                            REASON_ALIAS_TARGET_UNKNOWN, refs)
            continue

        authorized = manifest.allowed_reads.get(canonical_entry.resource_id, frozenset())

        try:
            stat_result = os.fstat(handle)
        except OSError:
            mark_unresolved(canonical_entry.resource_id, handle,
                            REASON_DESCRIPTOR_UNAVAILABLE, refs)
            continue

        if stat.S_ISREG(stat_result.st_mode):
            if canonical_entry.resource_type is not ResourceType.REGULAR:
                mark_unresolved(canonical_entry.resource_id, handle,
                                REASON_RESOURCE_TYPE_MISMATCH, refs)
                continue
            if not _identity_matches(stat_result, canonical_entry.expected_stat):
                mark_unresolved(canonical_entry.resource_id, handle,
                                REASON_IDENTITY_MISMATCH, refs)
                continue
            if ReadMethod.PREAD not in authorized:
                mark_unresolved(canonical_entry.resource_id, handle,
                                REASON_READ_METHOD_NOT_ALLOWED, refs)
                continue

            offset = (canonical_entry.offset
                      if canonical_entry.offset is not None else 0)
            task = SnapshotTask(resource_id=canonical_entry.resource_id,
                                handle=canonical,
                                read_method=ReadMethod.PREAD,
                                offset=offset,
                                length=canonical_entry.length,
                                references=tuple(refs))
            _merge(tasks, task)

        elif stat.S_ISFIFO(stat_result.st_mode) or stat.S_ISSOCK(stat_result.st_mode):
            actual_type = (ResourceType.PIPE if stat.S_ISFIFO(stat_result.st_mode)
                           else ResourceType.SOCKET)
            required_method = (ReadMethod.CONSUME if actual_type is ResourceType.PIPE
                               else ReadMethod.RECV)
            if canonical_entry.resource_type is not actual_type:
                mark_unresolved(canonical_entry.resource_id, handle,
                                REASON_RESOURCE_TYPE_MISMATCH, refs)
                continue
            # Streams are one-shot: the planner never reads them ahead, not
            # even when consumption is authorized.  Unauthorized streams are
            # retained with an explicit reason and no usable read method.
            reason = (REASON_STREAM_ONE_SHOT if required_method in authorized
                      else REASON_STREAM_UNAUTHORIZED)
            mark_pending(canonical_entry, canonical, actual_type, reason, refs)
        else:
            mark_unresolved(canonical_entry.resource_id, handle,
                            REASON_UNSUPPORTED_RESOURCE_TYPE, refs)

    return SnapshotPlan(references=references,
                        tasks=list(tasks.values()),
                        pending=list(pending_by_resource.values()),
                        unresolved=unresolved)


def plan_message_archive(message: Message,
                         manifest: ReceiverManifest) -> SnapshotPlan:
    """Build a :class:`SnapshotPlan` for a received D-Bus message."""
    return plan_body_archive(message.signature, message.body,
                             list(message.unix_fds), manifest)


def complete_pending_capture(plan: SnapshotPlan, resource_id: str,
                             data: bytes,
                             stat_evidence: Optional[Mapping[str, int]] = None
                             ) -> SnapshotResult:
    """Seal externally supplied material onto a retained stream reference.

    One-shot resources are never read by the planner.  When the authorized
    consumer later supplies the captured bytes, this turns the pending entry
    into a :class:`SnapshotResult` whose references still point back at the
    original message parameters.  Unauthorized pending entries cannot be
    completed this way.
    """
    for pending in plan.pending:
        if pending.resource_id != resource_id:
            continue
        if pending.reason is not REASON_STREAM_ONE_SHOT:
            raise ArchivePlanError(
                f'resource {resource_id!r} is pending as {pending.reason!r}, '
                'no authorized material can be attached')
        digest = hashlib.sha256(data).hexdigest()
        return SnapshotResult(resource_id=resource_id,
                              handle=pending.handle,
                              data=data,
                              digest=digest,
                              stat=dict(stat_evidence or {}),
                              references=pending.references)
    raise ArchivePlanError(f'no pending capture for resource {resource_id!r}')


def _read_range(handle: int, offset: int, length: Optional[int]) -> bytes:
    chunks: List[bytes] = []
    position = offset
    remaining = length
    while True:
        chunk_size = _READ_CHUNK if remaining is None else min(remaining, _READ_CHUNK)
        if chunk_size <= 0:
            break
        chunk = os.pread(handle, chunk_size, position)
        if not chunk:
            break
        chunks.append(chunk)
        position += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return b''.join(chunks)


def execute_snapshot_plan(plan: SnapshotPlan) -> List[SnapshotResult]:
    """Capture all tasks of ``plan`` with version certification.

    Each regular file is read with independent ``pread`` ranges so the shared
    file offset is untouched.  An ``fstat`` taken before and after the read
    must certify the same version; otherwise :class:`SnapshotVersionError` is
    raised and no snapshot is published for that resource.  Shared resources
    have a single task and are therefore read at most once.
    """
    results: List[SnapshotResult] = []
    for task in plan.tasks:
        before = os.fstat(task.handle)
        data = _read_range(task.handle, task.offset, task.length)
        after = os.fstat(task.handle)

        if any(getattr(before, field) != getattr(after, field)
               for field in _VERSION_FIELDS):
            raise SnapshotVersionError(task, before, after, results)

        digest = hashlib.sha256(data).hexdigest()
        results.append(
            SnapshotResult(resource_id=task.resource_id,
                           handle=task.handle,
                           data=data,
                           digest=digest,
                           stat=_stat_evidence(after),
                           references=task.references))
    return results
