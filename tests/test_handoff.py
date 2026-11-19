"""Tests for pyftpdlib.handoff (two-phase resumable upload handoff)."""

import hashlib
import json
import os

import pytest

from pyftpdlib.handoff import (
    ChecksumMismatchError,
    HandoffStore,
    InvalidStateError,
    UnknownUploadError,
)

PAYLOAD_V1 = b"first version of the payload " * 100
PAYLOAD_V2 = b"second version of the payload " * 100


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def store(tmp_path):
    return HandoffStore(str(tmp_path / "handoff"))


def publish_success(store, name, payload):
    upload_id = store.begin_upload(name)
    store.write(upload_id, payload)
    return store.publish(upload_id, sha256=sha256(payload))


class TestTwoPhasePublish:
    def test_upload_lands_in_temp_file_first(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1[:100])
        # payload sits in incoming/, nothing published yet
        assert os.path.exists(
            os.path.join(store.incoming_dir, upload_id + ".part")
        )
        assert os.listdir(store.published_dir) == []
        assert store.manifest() == []

    def test_publish_renames_into_published(self, store):
        receipt = publish_success(store, "report.csv", PAYLOAD_V1)
        assert not os.path.exists(
            os.path.join(store.incoming_dir, receipt.upload_id + ".part")
        )
        final = os.path.join(store.published_dir, "report.csv")
        with open(final, "rb") as f:
            assert f.read() == PAYLOAD_V1

    def test_manifest_lists_only_verified_and_published(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        # received but not published yet: not in the official list
        assert store.manifest() == []
        store.publish(upload_id, sha256=sha256(PAYLOAD_V1))
        entries = store.manifest()
        assert len(entries) == 1
        assert entries[0]["name"] == "report.csv"
        assert entries[0]["sha256"] == sha256(PAYLOAD_V1)
        assert entries[0]["size"] == len(PAYLOAD_V1)

    def test_checksum_mismatch_blocks_publish_and_manifest(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        with pytest.raises(ChecksumMismatchError):
            store.publish(upload_id, sha256=sha256(b"tampered"))
        assert store.status(upload_id)["status"] == "failed"
        assert store.manifest() == []
        assert os.listdir(store.published_dir) == []
        # a failed upload is terminal
        with pytest.raises(InvalidStateError):
            store.publish(upload_id)


class TestResumableReceivingState:
    def test_offset_tracks_received_bytes(self, store):
        upload_id = store.begin_upload("big.bin")
        assert store.offset(upload_id) == 0
        assert store.write(upload_id, PAYLOAD_V1[:1000]) == 1000
        assert store.offset(upload_id) == 1000

    def test_state_survives_restart(self, store):
        upload_id = store.begin_upload("big.bin")
        store.write(upload_id, PAYLOAD_V1[:1000])
        # simulate a server restart: a new store on the same root
        reopened = HandoffStore(store.root)
        assert reopened.offset(upload_id) == 1000
        assert reopened.status(upload_id)["status"] == "receiving"
        # resume from the recovered offset and complete the upload
        reopened.write(upload_id, PAYLOAD_V1[1000:])
        receipt = reopened.publish(upload_id, sha256=sha256(PAYLOAD_V1))
        assert receipt.size == len(PAYLOAD_V1)

    def test_explicit_offset_allows_resume_at_position(self, store):
        upload_id = store.begin_upload("big.bin")
        store.write(upload_id, PAYLOAD_V1[:500])
        store.write(upload_id, PAYLOAD_V1[500:], offset=500)
        assert store.offset(upload_id) == len(PAYLOAD_V1)
        store.publish(upload_id, sha256=sha256(PAYLOAD_V1))

    def test_offset_beyond_received_is_rejected(self, store):
        upload_id = store.begin_upload("big.bin")
        store.write(upload_id, PAYLOAD_V1[:10])
        with pytest.raises(InvalidStateError):
            store.write(upload_id, b"x", offset=11)

    def test_missing_payload_is_not_resumable(self, store):
        upload_id = store.begin_upload("big.bin")
        store.write(upload_id, PAYLOAD_V1[:10])
        os.remove(os.path.join(store.incoming_dir, upload_id + ".part"))
        reopened = HandoffStore(store.root)
        assert reopened.status(upload_id)["status"] == "aborted"
        with pytest.raises(InvalidStateError):
            reopened.write(upload_id, b"x")


class TestDisconnectSafety:
    def test_partial_upload_does_not_overwrite_published_version(self, store):
        publish_success(store, "report.csv", PAYLOAD_V1)
        # a new upload of the same name starts, then "disconnects"
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V2[:500])
        # old version is still intact and still the official one
        final = os.path.join(store.published_dir, "report.csv")
        with open(final, "rb") as f:
            assert f.read() == PAYLOAD_V1
        entries = store.manifest()
        assert len(entries) == 1
        assert entries[0]["sha256"] == sha256(PAYLOAD_V1)
        # the half-finished upload only exists as a temp file
        assert os.path.exists(
            os.path.join(store.incoming_dir, upload_id + ".part")
        )

    def test_restart_with_pending_upload_keeps_published_version(self, store):
        publish_success(store, "report.csv", PAYLOAD_V1)
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V2[:500])
        reopened = HandoffStore(store.root)
        final = os.path.join(store.published_dir, "report.csv")
        with open(final, "rb") as f:
            assert f.read() == PAYLOAD_V1
        # and the pending upload can still be resumed and published
        reopened.write(upload_id, PAYLOAD_V2[500:])
        reopened.publish(upload_id, sha256=sha256(PAYLOAD_V2))
        with open(final, "rb") as f:
            assert f.read() == PAYLOAD_V2


class TestReceiptOwnership:
    def test_same_name_upload_gets_fresh_identity(self, store):
        receipt_a = publish_success(store, "report.csv", PAYLOAD_V1)
        upload_b = store.begin_upload("report.csv")
        assert upload_b != receipt_a.upload_id
        # the new session has no receipt and cannot claim the old one
        assert not os.path.exists(
            os.path.join(store.receipt_dir, upload_b + ".json")
        )
        store.write(upload_b, PAYLOAD_V2)
        receipt_b = store.publish(upload_b, sha256=sha256(PAYLOAD_V2))
        assert receipt_b.upload_id == upload_b
        assert receipt_b.sha256 == sha256(PAYLOAD_V2)
        # the old receipt is untouched and still belongs to upload A
        with open(
            os.path.join(store.receipt_dir, receipt_a.upload_id + ".json")
        ) as f:
            old = json.load(f)
        assert old["upload_id"] == receipt_a.upload_id
        assert old["sha256"] == sha256(PAYLOAD_V1)

    def test_unknown_upload_id_is_rejected(self, store):
        with pytest.raises(UnknownUploadError):
            store.offset("0" * 32)
        with pytest.raises(UnknownUploadError):
            store.publish("0" * 32)
        with pytest.raises(UnknownUploadError):
            store.status("0" * 32)


class TestIdempotentPublish:
    def test_repeated_publish_returns_same_receipt(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        first = store.publish(upload_id, sha256=sha256(PAYLOAD_V1))
        assert not first.idempotent_replay
        second = store.publish(upload_id, sha256=sha256(PAYLOAD_V1))
        assert second.idempotent_replay
        assert second.upload_id == first.upload_id
        assert second.sha256 == first.sha256
        assert second.size == first.size
        assert second.published_at == first.published_at

    def test_repeated_publish_has_no_side_effects(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        store.publish(upload_id)
        before = store.manifest()
        store.publish(upload_id)
        store.publish(upload_id)
        after = store.manifest()
        assert before == after
        assert len(after) == 1
        # the temp file is gone after the first publish
        assert not os.path.exists(
            os.path.join(store.incoming_dir, upload_id + ".part")
        )

    def test_replay_survives_restart(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        first = store.publish(upload_id)
        reopened = HandoffStore(store.root)
        second = reopened.publish(upload_id)
        assert second.idempotent_replay
        assert second == first

    def test_replay_self_heals_missing_manifest_entry(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1)
        store.publish(upload_id)
        # simulate a crash between the rename and the manifest update
        with open(store.manifest_path) as f:
            data = json.load(f)
        del data["entries"]["report.csv"]
        with open(store.manifest_path, "w") as f:
            json.dump(data, f)
        assert store.manifest() == []
        replay = store.publish(upload_id)
        assert replay.idempotent_replay
        entries = store.manifest()
        assert len(entries) == 1
        assert entries[0]["upload_id"] == upload_id

    def test_older_replay_does_not_displace_newer_entry(self, store):
        old = publish_success(store, "report.csv", PAYLOAD_V1)
        new = publish_success(store, "report.csv", PAYLOAD_V2)
        # replaying the older publish must not roll back the manifest
        store.publish(old.upload_id)
        entries = store.manifest()
        assert len(entries) == 1
        assert entries[0]["upload_id"] == new.upload_id
        assert entries[0]["sha256"] == sha256(PAYLOAD_V2)


class TestAbort:
    def test_abort_removes_temp_file(self, store):
        upload_id = store.begin_upload("report.csv")
        store.write(upload_id, PAYLOAD_V1[:100])
        store.abort(upload_id)
        assert store.status(upload_id)["status"] == "aborted"
        assert not os.path.exists(
            os.path.join(store.incoming_dir, upload_id + ".part")
        )
        with pytest.raises(InvalidStateError):
            store.publish(upload_id)

    def test_abort_published_upload_is_rejected(self, store):
        receipt = publish_success(store, "report.csv", PAYLOAD_V1)
        with pytest.raises(InvalidStateError):
            store.abort(receipt.upload_id)


class TestNameSanitizing:
    @pytest.mark.parametrize("name", ["", ".", "..", "  "])
    def test_invalid_names_rejected(self, store, name):
        with pytest.raises(ValueError):
            store.begin_upload(name)

    def test_path_components_are_stripped(self, store):
        receipt = publish_success(store, "../etc/evil.csv", PAYLOAD_V1)
        assert receipt.name == "evil.csv"
        assert os.path.exists(os.path.join(store.published_dir, "evil.csv"))
