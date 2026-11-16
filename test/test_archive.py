"""Tests for UNIX_FD snapshot planning in ``dbus_next.archive``."""
import hashlib
import os
import socket
import stat

import pytest

from dbus_next import Message
from dbus_next.signature import Variant
from dbus_next.archive import (ArchivePlanError, DescriptorEntry, FdReference,
                               ReadMethod, ReceiverManifest, ResourceType,
                               SnapshotVersionError, complete_pending_capture,
                               find_fd_references, plan_body_archive,
                               plan_message_archive, REASON_FD_INDEX_UNBACKED,
                               REASON_READ_METHOD_NOT_ALLOWED,
                               REASON_STREAM_ONE_SHOT,
                               REASON_STREAM_UNAUTHORIZED,
                               REASON_UNAUTHORIZED_HANDLE)


def write_tmp_file(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def file_entry(handle, resource_id='file', **kwargs):
    return DescriptorEntry(resource_id=resource_id,
                           resource_type=ResourceType.REGULAR,
                           read_methods=frozenset((ReadMethod.PREAD,)),
                           **kwargs)


def test_only_h_tokens_are_references():
    refs = find_fd_references('ihyqxt', [7, 0, 9, 11, 13, 15])
    assert len(refs) == 1
    assert refs[0] == FdReference(path='[1]', fd_index=0, handle=None)


def test_references_through_containers_and_variants():
    body = [[0, 1], [2, 'x'], {'a': 3, 'b': 4}, [Variant('h', 5)],
            Variant('(hh)', [6, 7])]
    refs = find_fd_references('ah(hs)a{sh}(v)v', body)
    paths = [(ref.path, ref.fd_index) for ref in refs]
    assert paths == [
        ('[0][0]', 0),
        ('[0][1]', 1),
        ('[1][0]', 2),
        ("[2]['a']", 3),
        ("[2]['b']", 4),
        ('[3][0][v]', 5),
        ('[4][v][0]', 6),
        ('[4][v][1]', 7),
    ]


def test_dict_entry_key_can_carry_a_handle():
    refs = find_fd_references('a{hs}', [{0: 'a', 1: 'b'}])
    assert [(ref.path, ref.fd_index) for ref in refs] == [
        ("[0][0].key", 0),
        ("[0][1].key", 1),
    ]


def test_body_mismatch_raises_archive_error():
    with pytest.raises(ArchivePlanError):
        find_fd_references('s', [123])


def test_unbacked_fd_index_is_unresolved():
    plan = plan_body_archive('h', [3], [], ReceiverManifest({}))
    assert plan.tasks == ()
    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].reason == REASON_FD_INDEX_UNBACKED
    assert plan.unresolved[0].references[0].fd_index == 3


def test_handle_outside_manifest_is_unauthorized(tmp_path):
    path = write_tmp_file(tmp_path, 'f', b'data')
    fd = os.open(path, os.O_RDONLY)
    try:
        plan = plan_body_archive('h', [0], [fd], ReceiverManifest({}))
        assert len(plan.unresolved) == 1
        assert plan.unresolved[0].reason == REASON_UNAUTHORIZED_HANDLE
        assert plan.unresolved[0].handle == fd
    finally:
        os.close(fd)


def test_regular_file_snapshot_with_provenance(tmp_path):
    data = b'archive material\n'
    path = write_tmp_file(tmp_path, 'f', data)
    fd = os.open(path, os.O_RDONLY)
    try:
        manifest = ReceiverManifest(
            {fd: file_entry(fd, 'doc')},
            allowed_reads={'doc': frozenset((ReadMethod.PREAD,))})
        plan = plan_body_archive('h', [0], [fd], manifest)

        assert plan.is_complete
        assert len(plan.tasks) == 1
        task = plan.tasks[0]
        assert task.resource_id == 'doc'
        assert task.read_method is ReadMethod.PREAD
        assert task.references == (FdReference('[0]', 0, fd),)

        results = plan.execute()
        assert len(results) == 1
        result = results[0]
        assert result.data == data
        assert result.digest == hashlib.sha256(data).hexdigest()
        assert result.stat['st_ino'] == os.fstat(fd).st_ino
        assert result.references[0].path == '[0]'
    finally:
        os.close(fd)


def test_ranged_read_uses_pread_and_keeps_shared_offset(tmp_path):
    path = write_tmp_file(tmp_path, 'f', b'0123456789')
    fd = os.open(path, os.O_RDONLY)
    try:
        os.lseek(fd, 2, os.SEEK_SET)
        entry = file_entry(fd, 'doc', offset=4, length=3)
        manifest = ReceiverManifest(
            {fd: entry},
            allowed_reads={'doc': frozenset((ReadMethod.PREAD,))})
        results = plan_body_archive('h', [0], [fd], manifest).execute()
        assert results[0].data == b'456'
        # pread must not move the shared file offset
        assert os.lseek(fd, 0, os.SEEK_CUR) == 2
    finally:
        os.close(fd)


def test_regular_file_without_pread_authorization_has_no_task(tmp_path):
    path = write_tmp_file(tmp_path, 'f', b'x')
    fd = os.open(path, os.O_RDONLY)
    try:
        manifest = ReceiverManifest(
            {fd: file_entry(fd, 'doc')},
            allowed_reads={'doc': frozenset((ReadMethod.CONSUME,))})
        plan = plan_body_archive('h', [0], [fd], manifest)
        assert plan.tasks == ()
        assert plan.unresolved[0].reason == REASON_READ_METHOD_NOT_ALLOWED
    finally:
        os.close(fd)


def test_pipe_is_retained_and_never_consumed():
    read_end, write_end = os.pipe()
    os.write(write_end, b'pipe bytes')
    try:
        manifest = ReceiverManifest(
            {read_end: DescriptorEntry('stream', ResourceType.PIPE,
                                       frozenset((ReadMethod.CONSUME,)))},
            allowed_reads={'stream': frozenset((ReadMethod.CONSUME,))})
        plan = plan_body_archive('h', [0], [read_end], manifest)

        assert plan.tasks == ()
        assert len(plan.pending) == 1
        pending = plan.pending[0]
        assert pending.resource_type is ResourceType.PIPE
        assert pending.reason == REASON_STREAM_ONE_SHOT
        assert pending.references == (FdReference('[0]', 0, read_end),)
        assert not plan.is_complete

        # nothing was read ahead: the bytes are still there
        assert os.read(read_end, 100) == b'pipe bytes'
    finally:
        os.close(read_end)
        os.close(write_end)


def test_socket_is_retained_and_never_consumed():
    peer, local = socket.socketpair()
    peer.sendall(b'socket bytes')
    fd = local.fileno()
    try:
        manifest = ReceiverManifest(
            {fd: DescriptorEntry('sock', ResourceType.SOCKET,
                                 frozenset((ReadMethod.RECV,)))},
            allowed_reads={'sock': frozenset((ReadMethod.RECV,))})
        plan = plan_body_archive('h', [0], [fd], manifest)

        assert len(plan.pending) == 1
        assert plan.pending[0].resource_type is ResourceType.SOCKET
        assert plan.pending[0].reason == REASON_STREAM_ONE_SHOT
        assert local.recv(100) == b'socket bytes'
    finally:
        peer.close()
        local.close()


def test_unauthorized_stream_is_pending_without_consuming():
    read_end, write_end = os.pipe()
    os.write(write_end, b'secret')
    try:
        manifest = ReceiverManifest({
            read_end: DescriptorEntry('stream', ResourceType.PIPE, frozenset())
        })
        plan = plan_body_archive('h', [0], [read_end], manifest)
        assert plan.tasks == ()
        assert len(plan.pending) == 1
        assert plan.pending[0].reason == REASON_STREAM_UNAUTHORIZED
        assert os.read(read_end, 100) == b'secret'
    finally:
        os.close(read_end)
        os.close(write_end)


def test_shared_resource_produces_one_task_and_reads_once(tmp_path, monkeypatch):
    path = write_tmp_file(tmp_path, 'f', b'shared material')
    fd1 = os.open(path, os.O_RDONLY)
    fd2 = os.dup(fd1)
    pread_calls = []
    real_pread = os.pread

    def counting_pread(fd, size, offset):
        chunk = real_pread(fd, size, offset)
        if chunk:
            pread_calls.append((fd, len(chunk), offset))
        return chunk

    monkeypatch.setattr('dbus_next.archive.os.pread', counting_pread)
    try:
        manifest = ReceiverManifest(
            {
                fd1: file_entry(fd1, 'shared'),
                fd2: file_entry(fd2, 'shared'),
            },
            aliases={fd2: fd1},
            allowed_reads={'shared': frozenset((ReadMethod.PREAD,))})
        plan = plan_body_archive('hh', [0, 1], [fd1, fd2], manifest)

        assert len(plan.tasks) == 1
        task = plan.tasks[0]
        assert task.handle == fd1
        assert {ref.path for ref in task.references} == {'[0]', '[1]'}

        results = plan.execute()
        assert len(results) == 1
        assert results[0].data == b'shared material'
        assert len(pread_calls) == 1
    finally:
        os.close(fd1)
        os.close(fd2)


def test_version_change_during_read_aborts_snapshot(tmp_path, monkeypatch):
    path1 = write_tmp_file(tmp_path, 'a', b'first')
    path2 = write_tmp_file(tmp_path, 'b', b'second material')
    fd1 = os.open(path1, os.O_RDONLY)
    fd2 = os.open(path2, os.O_RDONLY)
    real_fstat = os.fstat
    fstat_calls = {'count': 0}

    def faking_fstat(fd):
        st = real_fstat(fd)
        fstat_calls['count'] += 1
        # only the post-read fstat of fd2 reports a different version
        if fd == fd2 and fstat_calls['count'] % 2 == 0:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   st.st_uid, st.st_gid, st.st_size + 1,
                                   st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr('dbus_next.archive.os.fstat', faking_fstat)
    try:
        manifest = ReceiverManifest(
            {fd1: file_entry(fd1, 'res-a'), fd2: file_entry(fd2, 'res-b')},
            allowed_reads={
                'res-a': frozenset((ReadMethod.PREAD,)),
                'res-b': frozenset((ReadMethod.PREAD,)),
            })
        plan = plan_body_archive('hh', [0, 1], [fd1, fd2], manifest)
        with pytest.raises(SnapshotVersionError) as exc:
            plan.execute()
        assert exc.value.task.resource_id == 'res-b'
        # res-a was verified and retained; res-b was not published
        assert [result.resource_id for result in exc.value.partial] == ['res-a']
        assert all(result.resource_id != 'res-b' for result in exc.value.partial)
    finally:
        os.close(fd1)
        os.close(fd2)


def test_pending_material_can_be_completed_with_provenance():
    read_end, write_end = os.pipe()
    os.write(write_end, b'later')
    try:
        manifest = ReceiverManifest(
            {read_end: DescriptorEntry('stream', ResourceType.PIPE,
                                       frozenset((ReadMethod.CONSUME,)))},
            allowed_reads={'stream': frozenset((ReadMethod.CONSUME,))})
        plan = plan_body_archive('h', [0], [read_end], manifest)
        material = os.read(read_end, 100)

        result = complete_pending_capture(plan, 'stream', material)
        assert result.data == b'later'
        assert result.digest == hashlib.sha256(b'later').hexdigest()
        assert result.references == (FdReference('[0]', 0, read_end),)
    finally:
        os.close(read_end)
        os.close(write_end)


def test_unauthorized_pending_cannot_be_completed():
    read_end, write_end = os.pipe()
    try:
        manifest = ReceiverManifest({
            read_end: DescriptorEntry('stream', ResourceType.PIPE, frozenset())
        })
        plan = plan_body_archive('h', [0], [read_end], manifest)
        with pytest.raises(ArchivePlanError):
            complete_pending_capture(plan, 'stream', b'x')
    finally:
        os.close(read_end)
        os.close(write_end)


def test_identity_mismatch_is_unresolved(tmp_path):
    path = write_tmp_file(tmp_path, 'f', b'data')
    fd = os.open(path, os.O_RDONLY)
    try:
        entry = file_entry(fd, 'doc', expected_stat={'st_ino': 999999})
        manifest = ReceiverManifest(
            {fd: entry},
            allowed_reads={'doc': frozenset((ReadMethod.PREAD,))})
        plan = plan_body_archive('h', [0], [fd], manifest)
        assert plan.tasks == ()
        assert plan.unresolved[0].reason == 'identity-mismatch'
    finally:
        os.close(fd)


def test_plan_from_real_message(tmp_path):
    path = write_tmp_file(tmp_path, 'f', b'message bytes')
    fd = os.open(path, os.O_RDONLY)
    try:
        message = Message(path='/test/path',
                          member='Member',
                          signature='ah',
                          body=[[0]],
                          unix_fds=[fd])
        manifest = ReceiverManifest(
            {fd: file_entry(fd, 'doc')},
            allowed_reads={'doc': frozenset((ReadMethod.PREAD,))})
        plan = plan_message_archive(message, manifest)
        results = plan.execute()
        assert results[0].data == b'message bytes'
        assert results[0].references[0].path == '[0][0]'
        assert stat.S_ISREG(results[0].stat['st_mode'])
    finally:
        os.close(fd)


def test_no_references_is_complete():
    plan = plan_body_archive('su', ['text', 5], [], ReceiverManifest({}))
    assert plan.references == ()
    assert plan.is_complete
    assert plan.execute() == []
