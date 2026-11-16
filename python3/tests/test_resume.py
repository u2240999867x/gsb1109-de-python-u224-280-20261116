# -*- coding: utf-8 -*-
"""
Offline tests for the resumable retrieval machinery (smb.resume and the SMB2
resumable state machine in smb.base). These tests do not require a live SMB
server: a fake SMB transport answers create/read/close callbacks synchronously.
"""

import os
import struct
import tempfile
import shutil

import pytest

from smb import resume
from smb.base import SMB, SharedFile, NotReadyError
from smb.smb_structs import UnsupportedFeature


DIALECT_21 = 0x0210


def make_attrs(file_size = 1000, mtime = 1700000000.0, attrs = 0x20):
    return SharedFile(create_time = 1699999999.0,
                      last_access_time = mtime,
                      last_write_time = mtime,
                      last_attr_change_time = mtime,
                      file_size = file_size,
                      alloc_size = file_size,
                      file_attributes = attrs,
                      short_name = 'f',
                      filename = 'f')


def make_attrs(file_size = 1000, mtime = 1700000000.0, attrs = 0x20):
    return SharedFile(create_time = 1699999999.0,
                      last_access_time = mtime,
                      last_write_time = mtime,
                      last_attr_change_time = mtime,
                      file_size = file_size,
                      alloc_size = file_size,
                      file_attributes = attrs,
                      short_name = 'f',
                      filename = 'f')


@pytest.fixture
def workspace():
    directory = tempfile.mkdtemp(prefix = 'pysmb-resume-')
    local_path = os.path.join(directory, 'download.bin')
    yield directory, local_path
    shutil.rmtree(directory, ignore_errors = True)


def write_token(local_path, **overrides):
    attrs = make_attrs()
    token = resume.token_from_attributes('share', 'dir/file.bin', local_path, attrs, DIALECT_21,
                                         confirmed_offset = overrides.pop('confirmed_offset', 100))
    for key, value in overrides.items():
        setattr(token, key, value)
    resume.save_resume_token(token)
    return token


def write_fragment(local_path, size, content = None):
    part = resume.fragment_path_for(local_path)
    with open(part, 'wb') as fh:
        fh.write(content if content is not None else (b'x' * size))
    return part


# ---------------------------------------------------------------------------
# Resume token persistence
# ---------------------------------------------------------------------------

def test_token_roundtrip_preserves_durable_ids(workspace):
    _, local_path = workspace
    token = write_token(local_path,
                        durable_create_guid = bytes(range(16)),
                        durable_file_id = bytes(range(16, 32)))
    loaded = resume.load_resume_token(local_path)
    assert loaded.durable_create_guid == bytes(range(16))
    assert loaded.durable_file_id == bytes(range(16, 32))
    assert loaded.path == 'dir\\file.bin'
    assert loaded.confirmed_offset == 100
    assert loaded.service_name == 'share'


def test_missing_token_returns_none(workspace):
    _, local_path = workspace
    assert resume.load_resume_token(local_path) is None


def test_corrupt_token_raises(workspace):
    _, local_path = workspace
    with open(resume.token_file_for(local_path), 'w') as fh:
        fh.write('{not json')
    with pytest.raises(resume.ResumeError):
        resume.load_resume_token(local_path)


def test_token_bound_to_other_path_rejected(workspace):
    directory, local_path = workspace
    other = os.path.join(directory, 'other.bin')
    write_token(other)
    shutil.move(resume.token_file_for(other), resume.token_file_for(local_path))
    with pytest.raises(resume.ResumeError):
        resume.load_resume_token(local_path)


# ---------------------------------------------------------------------------
# decide_resume branches
# ---------------------------------------------------------------------------

def test_fresh_start_creates_empty_fragment_and_token(workspace):
    _, local_path = workspace
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.FRESH
    assert plan.offset == 0
    assert plan.re_read_offsets == []
    assert os.path.getsize(plan.fragment_path) == 0
    assert os.path.exists(resume.token_file_for(local_path))
    assert plan.version_confirmed is True


def test_orphan_fragment_is_archived_not_overwritten(workspace):
    _, local_path = workspace
    write_fragment(local_path, 50, b'precious')
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.FRESH
    assert plan.quarantined_path is not None
    with open(plan.quarantined_path, 'rb') as fh:
        assert fh.read() == b'precious'
    assert os.path.getsize(plan.fragment_path) == 0
    assert plan.re_read_offsets == [ ( 0, 1000 ) ]


def test_path_resume_reuses_confirmed_fragment(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.PATH
    assert plan.offset == 300
    assert plan.re_read_offsets == []
    assert plan.version_confirmed is True


def test_durable_handle_requests_handle_reconnect(workspace):
    _, local_path = workspace
    write_token(local_path,
                confirmed_offset = 300,
                durable_create_guid = bytes(range(16)),
                durable_file_id = bytes(range(16, 32)))
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.HANDLE_PENDING
    assert plan.offset == 300


def test_durable_handle_requires_smb21(workspace):
    _, local_path = workspace
    write_token(local_path,
                confirmed_offset = 300,
                dialect_revision = 0x0202,
                durable_create_guid = bytes(range(16)),
                durable_file_id = bytes(range(16, 32)))
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, 0x0202, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.PATH


def test_size_change_archives_and_requires_full_reread(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(file_size = 999), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.FRESH
    assert plan.re_read_offsets == [ ( 0, 999 ) ]
    assert plan.quarantined_path is not None


def test_mtime_change_archives(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(mtime = 1700000001.5), 'share', 'dir/file.bin', local_path)
    assert plan.re_read_offsets == [ ( 0, 1000 ) ]
    assert plan.quarantined_path is not None


def test_missing_version_evidence_archives(workspace):
    _, local_path = workspace
    token = write_token(local_path, confirmed_offset = 300)
    token.file_size = None
    token.last_write_time = None
    resume.save_resume_token(token)
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.quarantined_path is not None
    assert plan.re_read_offsets == [ ( 0, 1000 ) ]


def test_truncated_fragment_archives(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    write_fragment(local_path, 299)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.quarantined_path is not None
    assert plan.re_read_offsets == [ ( 0, 1000 ) ]


def test_missing_fragment_restarts_from_zero(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    assert plan.mode == resume.ResumePlan.FRESH
    assert plan.re_read_offsets == [ ( 0, 1000 ) ]


def test_identity_mismatch_archives(workspace):
    _, local_path = workspace
    write_token(local_path, confirmed_offset = 300)
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/different.bin', local_path)
    assert plan.quarantined_path is not None
    assert plan.mode == resume.ResumePlan.FRESH


def test_smb1_rejected(workspace):
    _, local_path = workspace
    with pytest.raises(UnsupportedFeature):
        resume.decide_resume(False, None, make_attrs(), 'share', 'dir/file.bin', local_path)


def test_handle_plan_downgrade_clears_durable_ids(workspace):
    _, local_path = workspace
    token = write_token(local_path,
                        confirmed_offset = 300,
                        durable_create_guid = bytes(range(16)),
                        durable_file_id = bytes(range(16, 32)))
    write_fragment(local_path, 300)
    plan = resume.decide_resume(True, DIALECT_21, make_attrs(), 'share', 'dir/file.bin', local_path)
    resume.downgrade_handle_plan_to_path(plan, 'reconnect refused')
    assert plan.mode == resume.ResumePlan.PATH
    reloaded = resume.load_resume_token(local_path)
    assert reloaded.durable_create_guid is None
    assert reloaded.durable_file_id is None


# ---------------------------------------------------------------------------
# Fake SMB transport for end-to-end state machine tests
# ---------------------------------------------------------------------------

class FakeCreatePayload:
    def __init__(self, fid, file_size, file_attributes, durable_file_id, mtime):
        self.fid = fid
        self.file_size = file_size
        self.file_attributes = file_attributes
        self.durable_file_id = durable_file_id
        self.lastwrite_time = mtime


class FakeStandardInfo:
    def __init__(self, end_of_file):
        self.data = struct.pack('<QQI BB', end_of_file, end_of_file, 1, 0, 0)


class FakeReply:
    def __init__(self, status = 0, payload = None, tid = 7):
        self.status = status
        self.payload = payload
        self.tid = tid


class FakeReadPayload:
    def __init__(self, data):
        self.data = data
        self.data_length = len(data)


class PendingRequestMap(dict):
    """
    Fires a synthetic server reply the moment the SMB layer registers a
    pending request, which is the point a real network reply could first arrive.
    """

    def __init__(self, owner):
        super(PendingRequestMap, self).__init__()
        self._owner = owner
        self._outbox = { }

    def queue_message(self, mid, message):
        self._outbox[mid] = message

    def __setitem__(self, mid, request):
        super(PendingRequestMap, self).__setitem__(mid, request)
        message = self._outbox.pop(mid)
        self._owner.handle_message(message, request)


class FakeSMB(SMB):
    """Minimal SMB subclass that services the resumable retrieval synchronously."""

    def __init__(self, remote_bytes, mtime = 1700000000.0, read_size = 128,
                 fail_read_offset = None, reconnect_ok = True,
                 reopen_size = None, reopen_mtime = None, supports_durable = True,
                 probe_size = None, probe_mtime = None, standard_info_size = None,
                 empty_read_offset = None):
        self.has_authenticated = True
        self.is_using_smb2 = True
        self.dialect_revision = DIALECT_21
        self.connected_trees = { 'share': 7 }
        self.pending_requests = PendingRequestMap(self)
        self.max_read_size = read_size
        self.pbar = None
        self.remote_name = 'SERVER'
        self.log = __import__('logging').getLogger('FakeSMB')
        self.remote_bytes = remote_bytes
        self.mtime = mtime
        self.fail_read_offset = fail_read_offset
        self.reconnect_ok = reconnect_ok
        self.reopen_size = reopen_size
        self.reopen_mtime = reopen_mtime
        self.supports_durable = supports_durable
        self.probe_size = probe_size
        self.probe_mtime = probe_mtime
        self.standard_info_size = standard_info_size
        self.empty_read_offset = empty_read_offset
        self.durable_reconnects = 0
        self._fid_counter = 0
        self._mid = 0

    def _getNextMID_SMB2(self):
        self._mid += 1
        return self._mid

    def _getAttributes(self, service_name, path, callback, errback, timeout = 30):
        size = self.probe_size if self.probe_size is not None else len(self.remote_bytes)
        mtime = self.probe_mtime if self.probe_mtime is not None else self.mtime
        callback(make_attrs(size, mtime))

    def _reconnectDurableHandle_SMB2(self, service_name, path, persistent_file_id, create_guid, callback, errback, timeout):
        self.durable_reconnects += 1
        if self.reconnect_ok:
            self._fid_counter += 1
            callback(( 7, ('volatile-%04d' % self._fid_counter).encode().ljust(16, b'\0') ))
        else:
            errback(resume.DurableReconnectFailed(0xC0000225, 'no durable state'))

    def _sendSMBMessage(self, message):
        message.mid = self._getNextMID_SMB2()
        self.pending_requests.queue_message(message.mid, message)

    def handle_message(self, message, request):
        command = message.command
        if command == 0x05:  # CREATE
            self._fid_counter += 1
            fid = ('volatile-%04d' % self._fid_counter).encode().ljust(16, b'\0')
            requests_durable = b'DHnQ' in message.payload.create_context_data
            durable = b'persistent-0001' if (requests_durable and self.supports_durable) else None
            size = self.reopen_size if self.reopen_size is not None else len(self.remote_bytes)
            mtime = self.reopen_mtime if self.reopen_mtime is not None else self.mtime
            reply = FakeReply(0, FakeCreatePayload(fid, size, 0x20, durable, mtime))
        elif command == 0x10:  # QUERY_INFO
            eof = self.standard_info_size if self.standard_info_size is not None else len(self.remote_bytes)
            reply = FakeReply(0, FakeStandardInfo(eof))
        elif command == 0x08:  # READ
            offset = message.payload.read_offset
            count = message.payload.read_len
            if self.fail_read_offset is not None and offset >= self.fail_read_offset:
                reply = FakeReply(0xC0000001)
            elif self.empty_read_offset is not None and offset >= self.empty_read_offset:
                reply = FakeReply(0, FakeReadPayload(b''))
            else:
                reply = FakeReply(0, FakeReadPayload(self.remote_bytes[offset:offset + count]))
        elif command == 0x06:  # CLOSE
            reply = FakeReply(0)
        else:
            reply = FakeReply(0)

        request.callback(reply, **request.kwargs)


def run_retrieval(smb, local_path, durable = False):
    result = { }

    def cb(value):
        result['value'] = value

    def eb(failure):
        result['error'] = failure

    smb._resumableRetrieveFile_SMB2('share', 'dir/file.bin', local_path, cb, eb, timeout = 30)
    return result


def test_end_to_end_fresh_download(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    smb = FakeSMB(payload)
    result = run_retrieval(smb, local_path)
    assert 'error' not in result
    assert result['value'] == ( 0x20, 1000 )
    with open(local_path, 'rb') as fh:
        assert fh.read() == payload
    assert not os.path.exists(resume.fragment_path_for(local_path))
    assert not os.path.exists(resume.token_file_for(local_path))


def test_interruption_then_path_resume(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))

    first = FakeSMB(payload, fail_read_offset = 256, supports_durable = False)
    result = run_retrieval(first, local_path)
    assert isinstance(result['error'], resume.ResumeError)
    token = resume.load_resume_token(local_path)
    assert token.confirmed_offset == 256
    assert os.path.getsize(resume.fragment_path_for(local_path)) == 256
    assert token.durable_file_id is None

    second = FakeSMB(payload, supports_durable = False)
    result = run_retrieval(second, local_path)
    assert 'error' not in result, result
    with open(local_path, 'rb') as fh:
        assert fh.read() == payload


def test_resume_after_remote_file_replaced_archives_old_fragment(workspace):
    _, local_path = workspace
    original = bytes((i % 256 for i in range(1000)))
    first = FakeSMB(original, fail_read_offset = 256)
    run_retrieval(first, local_path)

    replaced = bytes(2000)
    second = FakeSMB(replaced, mtime = 1700000999.0)
    result = run_retrieval(second, local_path)
    assert 'error' not in result, result
    with open(local_path, 'rb') as fh:
        assert fh.read() == replaced
    archive_dir = resume.archive_dir_for(local_path)
    assert os.path.isdir(archive_dir)
    archived = [ name for name in os.listdir(archive_dir) if name.endswith('.part') ]
    assert archived, 'old fragment should have been archived, not spliced'


def test_durable_reconnect_resumes_without_path_reopen(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    first = FakeSMB(payload, fail_read_offset = 384)
    run_retrieval(first, local_path)
    token = resume.load_resume_token(local_path)
    assert token.durable_file_id is not None

    second = FakeSMB(payload, reconnect_ok = True)
    result = run_retrieval(second, local_path)
    assert 'error' not in result, result
    assert second.durable_reconnects == 1
    with open(local_path, 'rb') as fh:
        assert fh.read() == payload


def test_failed_durable_reconnect_falls_back_to_path(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    first = FakeSMB(payload, fail_read_offset = 384)
    run_retrieval(first, local_path)

    # Reconnect is refused and a later read also fails; the surviving token must
    # no longer carry durable ids while still retaining the confirmed prefix.
    second = FakeSMB(payload, reconnect_ok = False, fail_read_offset = 512)
    result = run_retrieval(second, local_path)
    assert isinstance(result['error'], resume.ResumeError)
    assert second.durable_reconnects == 1
    surviving = resume.load_resume_token(local_path)
    assert surviving.durable_file_id is None
    assert surviving.durable_create_guid is None
    assert surviving.confirmed_offset == 512

    third = FakeSMB(payload, supports_durable = False)
    result = run_retrieval(third, local_path)
    assert 'error' not in result, result
    with open(local_path, 'rb') as fh:
        assert fh.read() == payload


def test_unexpected_eof_aborts_without_spin(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    smb = FakeSMB(payload, empty_read_offset = 256)
    result = run_retrieval(smb, local_path)
    assert isinstance(result['error'], resume.ResumeError)
    assert resume.load_resume_token(local_path).confirmed_offset == 256
    assert os.path.getsize(resume.fragment_path_for(local_path)) == 256


def test_path_reopen_detecting_replacement_archives_fragment(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    first = FakeSMB(payload, fail_read_offset = 256, supports_durable = False)
    run_retrieval(first, local_path)

    # The probe still reports the old version (so PATH resume is chosen), but the
    # file at that path has been replaced by the time it is reopened.
    second = FakeSMB(payload, supports_durable = False,
                     reopen_size = 700, reopen_mtime = 1700000555.0)
    result = run_retrieval(second, local_path)
    assert isinstance(result['error'], resume.ResumeError)
    archive_dir = resume.archive_dir_for(local_path)
    assert [ n for n in os.listdir(archive_dir) if n.endswith('.part') ]

    third = FakeSMB(bytes(700), mtime = 1700000555.0, supports_durable = False)
    result = run_retrieval(third, local_path)
    assert 'error' not in result, result
    assert os.path.getsize(local_path) == 700


def test_durable_reattach_detects_truncation_and_archives(workspace):
    _, local_path = workspace
    payload = bytes((i % 256 for i in range(1000)))
    first = FakeSMB(payload, fail_read_offset = 384)
    run_retrieval(first, local_path)

    second = FakeSMB(payload, standard_info_size = 200)
    result = run_retrieval(second, local_path)
    assert isinstance(result['error'], resume.ResumeError)
    archive_dir = resume.archive_dir_for(local_path)
    assert [ n for n in os.listdir(archive_dir) if n.endswith('.part') ]


def test_resumable_requires_smb2(workspace):
    _, local_path = workspace
    smb = FakeSMB(b'data')
    smb.is_using_smb2 = False
    with pytest.raises(UnsupportedFeature):
        smb._resumableRetrieveFile_SMB2('share', 'dir/file.bin', local_path, lambda r: None, lambda e: None)


def test_resumable_requires_authentication(workspace):
    _, local_path = workspace
    smb = FakeSMB(b'data')
    smb.has_authenticated = False
    with pytest.raises(NotReadyError):
        smb._resumableRetrieveFile_SMB2('share', 'dir/file.bin', local_path, lambda r: None, lambda e: None)
