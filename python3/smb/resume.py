"""
Reliable resumable file retrieval.

When a download is interrupted, three pieces of information decide whether it is
safe to continue:

1. **File identity** -- the share name and the remote path, plus the SMB2
   persistent file id / durable create GUID when a durable handle is available.
2. **Version evidence** -- the remote file size and its last-write / change
   timestamps captured from the server. A fragment may only be extended when the
   remote file still matches this evidence.
3. **Confirmed range** -- the number of bytes that have been fully acknowledged
   by the server (a successful SMB2 READ response) and durably written to the
   local fragment.

Two recovery modes are kept strictly separate:

* *persistent handle recovery* reattaches to the server-side open via an
  SMB2 durable-handle reconnect;
* *path recovery* reopens the file by path and may only reuse the fragment when
  the version evidence still matches.

Whenever the version cannot be confirmed, the old fragment is moved aside
(quarantined) and is never appended to or spliced into a new download.
"""

import os
import json
import time
import binascii
import shutil
import struct
import tempfile


SCHEMA_VERSION = 1

# SMB2 dialects (see smb2_constants.SMB2_DIALECT_*). Durable handles require 2.1.
SMB2_DIALECT_21 = 0x0210

# NTSTATUS values commonly returned when a durable reconnect is refused.
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_NOT_FOUND = 0xC0000225


class ResumeError(Exception):
    """
    Base class for all resumable-retrieval errors.

    Raising this exception never deletes a local fragment: the partially
    downloaded data and its resume token are left untouched for inspection.
    """


class DurableReconnectFailed(Exception):
    """Internal signal that a durable-handle reconnect attempt was refused."""

    def __init__(self, status, reason = ''):
        self.status = status
        self.reason = reason
        super(DurableReconnectFailed, self).__init__('Durable handle reconnect failed (status=0x%08x): %s' % ( status, reason ))


def normalize_path(path):
    """Normalize an SMB path for identity comparison (backslash based, no edges)."""
    path = path.replace('/', '\\')
    return path.strip('\\')


def token_file_for(local_path):
    """Return the sidecar path used to persist a resume token."""
    return local_path + '.smbresume'


def fragment_path_for(local_path):
    """Return the path of the partial fragment backing a resumable download."""
    return local_path + '.part'


def archive_dir_for(local_path):
    """Return the directory where unverifiable fragments are archived."""
    return local_path + '.archive'


class ResumeToken:
    """
    Persisted identity, version evidence and confirmed range for one download.

    The token is serialized to JSON next to the fragment (``<local_path>.smbresume``)
    and rewritten atomically after every confirmed read, so it always describes a
    fragment that is complete up to ``confirmed_offset``.
    """

    def __init__(self, service_name = None, path = None, local_path = None,
                 file_size = None, last_write_time = None, change_time = None,
                 create_time = None, file_attributes = None,
                 confirmed_offset = 0, dialect_revision = None,
                 durable_create_guid = None, durable_file_id = None,
                 updated_at = None):
        self.schema_version = SCHEMA_VERSION
        self.service_name = service_name
        self.path = normalize_path(path) if path is not None else None
        self.local_path = local_path
        self.file_size = file_size
        self.last_write_time = last_write_time
        self.change_time = change_time
        self.create_time = create_time
        self.file_attributes = file_attributes
        self.confirmed_offset = confirmed_offset
        self.dialect_revision = dialect_revision
        self.durable_create_guid = durable_create_guid
        self.durable_file_id = durable_file_id
        self.updated_at = updated_at

    @property
    def has_version_evidence(self):
        """Version evidence is usable only when a size and mtime were recorded."""
        return self.file_size is not None and self.last_write_time is not None

    @property
    def has_durable_handle(self):
        return bool(self.durable_create_guid and self.durable_file_id)

    def to_dict(self):
        return {
            'schema_version': self.schema_version,
            'service_name': self.service_name,
            'path': self.path,
            'local_path': self.local_path,
            'file_size': self.file_size,
            'last_write_time': self.last_write_time,
            'change_time': self.change_time,
            'create_time': self.create_time,
            'file_attributes': self.file_attributes,
            'confirmed_offset': self.confirmed_offset,
            'dialect_revision': self.dialect_revision,
            'durable_create_guid': _bytes_to_hex(self.durable_create_guid),
            'durable_file_id': _bytes_to_hex(self.durable_file_id),
            'updated_at': self.updated_at,
        }

    @classmethod
    def from_dict(cls, data):
        if int(data.get('schema_version', 0)) != SCHEMA_VERSION:
            raise ResumeError('Unsupported resume token schema version: %r' % data.get('schema_version'))
        return cls(
            service_name = data.get('service_name'),
            path = data.get('path'),
            local_path = data.get('local_path'),
            file_size = data.get('file_size'),
            last_write_time = data.get('last_write_time'),
            change_time = data.get('change_time'),
            create_time = data.get('create_time'),
            file_attributes = data.get('file_attributes'),
            confirmed_offset = int(data.get('confirmed_offset', 0)),
            dialect_revision = data.get('dialect_revision'),
            durable_create_guid = _hex_to_bytes(data.get('durable_create_guid')),
            durable_file_id = _hex_to_bytes(data.get('durable_file_id')),
            updated_at = data.get('updated_at'),
        )


def save_resume_token(token, token_path = None):
    """Persist the token atomically (temp file + rename) beside the fragment."""
    token.updated_at = time.time()
    token_path = token_path or token_file_for(token.local_path)
    directory = os.path.dirname(os.path.abspath(token_path)) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix = '.smbresume.', dir = directory)
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(token.to_dict(), fh, indent = 2, sort_keys = True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, token_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return token_path


def load_resume_token(local_path):
    """
    Load the token for *local_path*.

    Returns ``None`` when no token exists. Raises :class:`ResumeError` when the
    sidecar is present but cannot be parsed: a damaged token must not be ignored,
    because it is the only proof available for the fragment.
    """
    token_path = token_file_for(local_path)
    if not os.path.exists(token_path):
        return None
    try:
        with open(token_path, 'r') as fh:
            data = json.load(fh)
        token = ResumeToken.from_dict(data)
    except ResumeError:
        raise
    except Exception as ex:
        raise ResumeError('Resume token at %s is unreadable: %s' % ( token_path, ex ))
    if token.local_path and os.path.abspath(token.local_path) != os.path.abspath(local_path):
        raise ResumeError('Resume token %s belongs to a different local path: %s' % ( token_path, token.local_path ))
    return token


def discard_resume_token(local_path):
    token_path = token_file_for(local_path)
    if os.path.exists(token_path):
        os.unlink(token_path)


def archive_fragment(local_path, reason, clock = time.time):
    """
    Move the partial fragment (and its sidecar token) into the archive directory.

    The archived bytes are preserved for forensics but never reused by a later
    download. Returns the archived fragment path (or ``None`` when no fragment
    existed).
    """
    part_path = fragment_path_for(local_path)
    archived_path = None
    if os.path.exists(part_path):
        archive_dir = archive_dir_for(local_path)
        if not os.path.exists(archive_dir):
            os.makedirs(archive_dir)
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(clock()))
        target = os.path.join(archive_dir, '%s.%s.part' % ( os.path.basename(part_path), stamp ))
        target = _unique_path(target)
        shutil.move(part_path, target)
        archived_path = target

        marker = target + '.reason'
        with open(marker, 'w') as fh:
            fh.write(reason + '\n')

    token_path = token_file_for(local_path)
    if os.path.exists(token_path):
        archive_dir = archive_dir_for(local_path)
        if not os.path.exists(archive_dir):
            os.makedirs(archive_dir)
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(clock()))
        target = _unique_path(os.path.join(archive_dir, '%s.%s.smbresume' % ( os.path.basename(token_path), stamp )))
        shutil.move(token_path, target)

    return archived_path


def finalize_resume(local_path):
    """Promote the completed fragment to the final path and remove the token."""
    part_path = fragment_path_for(local_path)
    if not os.path.exists(part_path):
        raise ResumeError('Cannot finalize %s: fragment %s is missing' % ( local_path, part_path ))
    os.replace(part_path, local_path)
    discard_resume_token(local_path)


class ResumePlan:
    """
    The result of preparing a resumable download.

    Attributes:

    * ``mode`` -- ``'fresh'`` (no usable prior state), ``'handle'`` (resuming via
      a reattached durable handle) or ``'path'`` (reopening by path).
    * ``version_confirmed`` -- ``True`` only when the server still matches the
      recorded version evidence.
    * ``offset`` -- remote byte offset from which the next READ must start.
    * ``confirmed_offset`` -- size of the trustworthy local fragment.
    * ``re_read_offsets`` -- sorted list of ``(start, end_exclusive)`` byte
      ranges that must be downloaded again; empty when the fragment is reusable.
    * ``quarantined_path`` -- location of an archived unusable fragment, if any.
    * ``reason`` -- human-readable explanation of the decision.
    * ``token`` -- the persisted :class:`ResumeToken`.
    * ``handle`` -- ``(tid, fid)`` of a reattached durable handle, or ``None``.
    """

    FRESH = 'fresh'
    HANDLE = 'handle'
    HANDLE_PENDING = 'handle_pending'
    PATH = 'path'

    def __init__(self, mode, token, reason, version_confirmed = False,
                 offset = 0, re_read_offsets = None, quarantined_path = None,
                 handle = None):
        self.mode = mode
        self.token = token
        self.reason = reason
        self.version_confirmed = version_confirmed
        self.offset = offset
        self.confirmed_offset = token.confirmed_offset
        self.re_read_offsets = list(re_read_offsets or [])
        self.quarantined_path = quarantined_path
        self.handle = handle

    @property
    def fragment_path(self):
        return fragment_path_for(self.token.local_path)

    @property
    def must_reread_everything(self):
        return bool(self.re_read_offsets)


def decide_resume(is_smb2, dialect_revision, remote, service_name, path,
                  local_path, clock = time.time):
    """
    Decide how a (possibly interrupted) download should proceed.

    *remote* is the freshly fetched :class:`smb.base.SharedFile` attributes of
    the remote file. This function performs no network I/O so it can be shared
    by the blocking and Twisted transports.

    When the token holds a durable handle on an SMB2.1+ connection, the plan is
    returned in mode :data:`ResumePlan.HANDLE_PENDING`: the transport layer must
    then attempt a durable reconnect and either upgrade it to
    :data:`ResumePlan.HANDLE` (reattached handle) or fall back to
    :data:`ResumePlan.PATH`.

    Never raises for a changed remote file: instead it returns a plan that
    archives the old fragment and lists the ranges that must be re-read.
    """
    if not is_smb2:
        from .smb_structs import UnsupportedFeature
        raise UnsupportedFeature('Resumable retrieval requires SMB2; durable handles are not available on SMB1 connections')

    token = load_resume_token(local_path)
    part_path = fragment_path_for(local_path)

    if token is None:
        quarantined = None
        reason = 'No resume token found for %s; starting a new download.' % local_path
        if os.path.exists(part_path):
            # An orphan fragment has no identity/version evidence: archive it,
            # never overwrite or splice it.
            orphan_reason = 'Orphan fragment without a resume token: identity and version cannot be established.'
            quarantined = archive_fragment(local_path, orphan_reason, clock)
            reason = 'No resume token found but a fragment existed; archived orphan fragment at %s and restarting.' % quarantined
        return _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = quarantined is not None,
                            reason = reason, quarantined_path = quarantined)

    if normalize_path(service_name) != normalize_path(token.service_name) or normalize_path(path) != token.path:
        quarantined = archive_fragment(local_path, 'Token identity mismatch: token refers to %s on %s, request is for %s on %s'
                                       % ( token.path, token.service_name, normalize_path(path), normalize_path(service_name) ), clock)
        return _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = quarantined is not None,
                            reason = 'Resume token belongs to a different remote file; archived fragment at %s and restarting.' % quarantined,
                            quarantined_path = quarantined)

    if not token.has_version_evidence:
        reason = ('Cannot confirm the version of %s on %s: the resume token lacks size/timestamp evidence. '
                  'Archiving the old fragment instead of splicing unverifiable bytes.' % ( path, service_name ))
        quarantined = archive_fragment(local_path, reason, clock)
        plan = _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = quarantined is not None,
                            reason = reason, quarantined_path = quarantined)
        plan.re_read_offsets = [ ( 0, remote.file_size ) ]
        return plan

    if not _version_matches(token, remote):
        reason = ('Remote file %s on %s has changed since the fragment was downloaded '
                  '(token: size=%s mtime=%s; server: size=%s mtime=%s). Old fragment archived and full re-read required.'
                  % ( path, service_name, token.file_size, token.last_write_time,
                      remote.file_size, remote.last_write_time ))
        quarantined = archive_fragment(local_path, reason, clock)
        plan = _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = quarantined is not None,
                            reason = reason, quarantined_path = quarantined)
        plan.re_read_offsets = [ ( 0, remote.file_size ) ]
        return plan

    if not os.path.exists(part_path):
        reason = 'Version of %s on %s is confirmed but the local fragment %s is missing; restarting from offset 0.' % ( path, service_name, part_path )
        plan = _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = False, reason = reason)
        plan.re_read_offsets = [ ( 0, remote.file_size ) ]
        return plan

    if os.path.getsize(part_path) != token.confirmed_offset:
        reason = ('Local fragment %s has %d bytes but the token confirms only %d; the fragment is corrupt or tampered. '
                  'Archiving it and re-reading the whole file.' % ( part_path, os.path.getsize(part_path), token.confirmed_offset ))
        quarantined = archive_fragment(local_path, reason, clock)
        plan = _start_fresh(local_path, service_name, path, remote, dialect_revision,
                            prior_fragment = quarantined is not None,
                            reason = reason, quarantined_path = quarantined)
        plan.re_read_offsets = [ ( 0, remote.file_size ) ]
        return plan

    if token.has_durable_handle and dialect_revision is not None and dialect_revision >= SMB2_DIALECT_21:
        return ResumePlan(ResumePlan.HANDLE_PENDING, token,
                          'Durable handle recorded for %s on %s; attempting to reattach before resuming from offset %d.'
                          % ( path, service_name, token.confirmed_offset ),
                          version_confirmed = True, offset = token.confirmed_offset)

    reason = 'Reopening %s on %s by path at confirmed offset %d; version evidence (size=%d, mtime=%s) matches.' \
             % ( path, service_name, token.confirmed_offset, token.file_size, token.last_write_time )
    return ResumePlan(ResumePlan.PATH, token, reason,
                      version_confirmed = True, offset = token.confirmed_offset)


def downgrade_handle_plan_to_path(plan, reason):
    """
    Convert a HANDLE_PENDING/HANDLE plan into a PATH plan after a failed durable
    reconnect. The recorded durable ids are discarded so a later attempt cannot
    reuse stale handle evidence.
    """
    plan.token.durable_file_id = None
    plan.token.durable_create_guid = None
    save_resume_token(plan.token)
    plan.mode = ResumePlan.PATH
    plan.handle = None
    plan.reason = reason
    return plan


def upgrade_handle_plan(plan, tid, fid, reason):
    """Attach the reconnected ``(tid, fid)`` to a HANDLE_PENDING plan."""
    save_resume_token(plan.token)
    plan.mode = ResumePlan.HANDLE
    plan.handle = ( tid, fid )
    plan.reason = reason
    return plan


def token_from_attributes(service_name, path, local_path, attrs, dialect_revision,
                          confirmed_offset = 0, durable_create_guid = None, durable_file_id = None):
    return ResumeToken(service_name = service_name,
                       path = path,
                       local_path = local_path,
                       file_size = attrs.file_size,
                       last_write_time = attrs.last_write_time,
                       change_time = getattr(attrs, 'last_attr_change_time', None),
                       create_time = attrs.create_time,
                       file_attributes = attrs.file_attributes,
                       confirmed_offset = confirmed_offset,
                       dialect_revision = dialect_revision,
                       durable_create_guid = durable_create_guid,
                       durable_file_id = durable_file_id)


def build_durable_create_context(create_guid):
    """
    Build an SMB2_CREATE_CONTEXT requesting a durable handle (name ``DHnQ``).

    The context data is the 16-byte *create_guid* ([MS-SMB2] 2.2.13.2.12).
    The server returns the persistent file id in its create response.
    """
    if len(create_guid) != 16:
        raise ValueError('durable create GUID must be 16 bytes')
    return _pack_create_context(b'DHnQ', create_guid)


def build_durable_reconnect_context(persistent_file_id, create_guid):
    """
    Build an SMB2_CREATE_CONTEXT that reattaches an existing durable handle.

    Per [MS-SMB2] 2.2.13.2.13 the ``DHnQ`` reconnect data is the 16-byte
    persistent file id obtained from the original create response followed by
    the original 16-byte create GUID.
    """
    if len(persistent_file_id) != 16 or len(create_guid) != 16:
        raise ValueError('persistent file id and create GUID must each be 16 bytes')
    return _pack_create_context(b'DHnQ', persistent_file_id + create_guid)


def build_mxac_context():
    # MxAc: query maximum access mask; pysmb's existing opens carry an empty MxAc context.
    return _pack_create_context(b'MxAc', b'')


def build_qfid_context():
    # QFid: query on-disk file id; no data payload.
    return _pack_create_context(b'QFid', b'')


def chain_create_contexts(contexts):
    """
    Join individually packed create contexts, setting NextOffset on each
    context except the last. Every context is 8-byte aligned, so the next
    context starts exactly len(context) bytes after the current one.
    """
    out = b''
    for index, ctx in enumerate(contexts):
        if index < len(contexts) - 1:
            ctx = struct.pack('<I', len(ctx)) + ctx[4:]
        out += ctx
    return out


def parse_create_contexts(raw_data, context_offset, context_length):
    """
    Parse SMB2_CREATE contexts ([MS-SMB2] 2.2.13.2 / 2.2.14.2).

    *raw_data* is the complete SMB2 message (starting at the 64-byte header) and
    *context_offset* is the absolute location of the first context. Within each
    context, NameOffset/DataOffset/NextOffset are relative to the start of that
    context, so positions are computed against the running *pos*.

    Returns a dict mapping the 4-character context name (``str``) to its data
    bytes. Malformed trailing contexts are ignored so an unknown extension can
    never break durable-id extraction.
    """
    contexts = { }
    end = context_offset + context_length
    pos = context_offset
    while pos + 16 <= end:
        next_offset, name_offset, name_length, _reserved, data_offset, data_length = \
            struct.unpack('<IHHHHI', raw_data[pos:pos + 16])
        try:
            name = raw_data[pos + name_offset:pos + name_offset + name_length].decode('ascii')
            data = raw_data[pos + data_offset:pos + data_offset + data_length]
        except Exception:
            break
        contexts[name] = data
        if next_offset == 0:
            break
        pos += next_offset
    return contexts


def _start_fresh(local_path, service_name, path, remote, dialect_revision, prior_fragment, reason, quarantined_path = None):
    part_path = fragment_path_for(local_path)
    with open(part_path, 'wb'):
        pass
    token = token_from_attributes(service_name, path, local_path, remote, dialect_revision)
    save_resume_token(token)
    plan = ResumePlan(ResumePlan.FRESH, token, reason, version_confirmed = True,
                      offset = 0, quarantined_path = quarantined_path)
    plan.re_read_offsets = [ ( 0, remote.file_size ) ] if prior_fragment else [ ]
    return plan


def _version_matches(token, remote):
    if not token.has_version_evidence or remote.last_write_time is None:
        return False
    if token.file_size != remote.file_size:
        return False
    if token.last_write_time != remote.last_write_time:
        return False
    if token.change_time is not None and getattr(remote, 'last_attr_change_time', None) is not None:
        if token.change_time != remote.last_attr_change_time:
            return False
    return True


def _pack_create_context(name, data):
    # SMB2_CREATE_CONTEXT request offsets are relative to the start of this context.
    name_offset = 16
    data_offset = name_offset + len(name)
    data_offset += (8 - (data_offset % 8)) % 8
    header = struct.pack('<IHHHHI', 0, name_offset, len(name), 0, data_offset, len(data))
    body = name + b'\0' * (data_offset - name_offset - len(name)) + data
    packed = header + body
    packed += b'\0' * ((8 - (len(packed) % 8)) % 8)
    return packed


def _unique_path(path):
    if not os.path.exists(path):
        return path
    counter = 1
    stem, suffix = os.path.splitext(path)
    while True:
        candidate = '%s.%d%s' % ( stem, counter, suffix )
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _bytes_to_hex(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return binascii.hexlify(value).decode('ascii')


def _hex_to_bytes(value):
    if not value:
        return None
    return binascii.unhexlify(value.encode('ascii') if isinstance(value, str) else value)
