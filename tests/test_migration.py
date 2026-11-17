from __future__ import unicode_literals

import json
import os
import shutil
import tempfile
import unittest

from fs.migration import (
    STATE_COPIED,
    STATE_DELETABLE,
    STATE_PENDING,
    STATE_VERIFIED,
    Migrator,
    MigrationJournal,
    ShareRegistry,
    migrate_fs,
)
from fs.errors import MigrationConflict, MigrationIncomplete
from fs.osfs import OSFS
from fs import open_fs


class _Stop(Exception):
    """Raised from an event callback to simulate an interruption."""


class MigrationTestMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fs-migration-")
        self.src_dir = os.path.join(self.tmp, "src")
        self.dst_dir = os.path.join(self.tmp, "dst")
        os.makedirs(self.src_dir)
        os.makedirs(self.dst_dir)
        self.journal_path = os.path.join(self.tmp, "journal.json")
        self.registry_path = os.path.join(self.tmp, "shares.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_src(self):
        return OSFS(self.src_dir)

    def make_dst(self):
        return OSFS(self.dst_dir)


class TestMigrateFS(MigrationTestMixin, unittest.TestCase):
    def _populate(self):
        with self.make_src() as src:
            src.makedirs("sub")
            src.writetext("a.txt", "alpha")
            src.writetext("sub/b.txt", "bravo")
            src.writetext("sub/c.bin", b"\x00\x01\x02".decode("latin-1"))

    def test_full_migration_states_and_events(self):
        self._populate()
        events = []
        result = migrate_fs(
            self.make_src(),
            self.make_dst(),
            self.journal_path,
            on_event=lambda event, entry: events.append((event, entry.src_path)),
        )
        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 3)
        self.assertEqual(result.pending, 0)
        self.assertEqual(result.conflicts, [])

        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")
        with self.make_src() as src:
            self.assertTrue(src.isempty("/"))

        event_names = [event for event, _ in events]
        for expected in ("planned", "copied", "verified", "deletable", "completed"):
            self.assertIn(expected, event_names)
        for path in ("/a.txt", "/sub/b.txt", "/sub/c.bin"):
            states = [
                event for event, event_path in events if event_path == path
            ]
            self.assertEqual(
                states,
                ["planned", "copied", "verified", "deletable", "completed"],
            )

    def test_journal_is_persisted_json(self):
        self._populate()
        migrate_fs(self.make_src(), self.make_dst(), self.journal_path)
        with open(self.journal_path, "rb") as journal_file:
            data = json.loads(journal_file.read().decode("utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["entries"]), 3)
        for record in data["entries"].values():
            self.assertEqual(record["state"], "completed")
            self.assertTrue(record["src_hash"])
            self.assertEqual(record["src_hash"], record["dst_hash"])

    def test_resume_after_interruption(self):
        self._populate()

        def stop_after_first_copy(event, entry):
            if event == "copied" and entry.src_path == "/a.txt":
                raise _Stop()

        with self.assertRaises(_Stop):
            migrate_fs(
                self.make_src(),
                self.make_dst(),
                self.journal_path,
                on_event=stop_after_first_copy,
            )

        journal = MigrationJournal(self.journal_path)
        self.assertEqual(journal.get("/a.txt").state, STATE_COPIED)
        self.assertIn(STATE_PENDING, [entry.state for entry in journal.entries().values()])
        with self.make_src() as src:
            self.assertTrue(src.exists("a.txt"))

        result = migrate_fs(self.make_src(), self.make_dst(), self.journal_path)
        self.assertEqual(result.completed, 3)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")
        with self.make_src() as src:
            self.assertTrue(src.isempty("/"))

    def test_replay_does_not_rewrite_or_append(self):
        self._populate()
        copied_paths = []
        migrate_fs(
            self.make_src(),
            self.make_dst(),
            self.journal_path,
            on_event=lambda event, entry: copied_paths.append(
                entry.src_path if event == "copied" else None
            ),
        )
        self.assertEqual(len([p for p in copied_paths if p]), 3)

        copied_on_replay = []
        result = migrate_fs(
            self.make_src(),
            self.make_dst(),
            self.journal_path,
            on_event=lambda event, entry: copied_on_replay.append(
                entry.src_path if event == "copied" else None
            ),
        )
        self.assertEqual(result.completed, 3)
        self.assertEqual([p for p in copied_on_replay if p], [])
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")

    def test_identical_destination_is_not_recopied(self):
        self._populate()
        with self.make_dst() as dst:
            dst.writetext("a.txt", "alpha")
        events = []
        migrate_fs(
            self.make_src(),
            self.make_dst(),
            self.journal_path,
            on_event=lambda event, entry: events.append((event, entry.src_path)),
        )
        self.assertNotIn(("copied", "/a.txt"), events)
        self.assertIn(("verified", "/a.txt"), events)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")

    def test_conflict_same_name_different_content(self):
        self._populate()
        with self.make_dst() as dst:
            dst.writetext("a.txt", "totally different")

        with self.assertRaises(MigrationConflict) as caught:
            migrate_fs(self.make_src(), self.make_dst(), self.journal_path)
        self.assertEqual(caught.exception.path, "/a.txt")
        self.assertNotEqual(caught.exception.src_hash, caught.exception.dst_hash)

        with self.make_src() as src:
            self.assertTrue(src.exists("a.txt"))
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "totally different")

        journal = MigrationJournal(self.journal_path)
        conflicts = journal.conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].src_path, "/a.txt")

        with self.make_dst() as dst:
            dst.remove("a.txt")
        journal.resolve_conflict("/a.txt")
        result = migrate_fs(self.make_src(), self.make_dst(), self.journal_path)
        self.assertEqual(result.completed, 3)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")

    def test_conflict_policy_skip_reports_and_continues(self):
        self._populate()
        with self.make_dst() as dst:
            dst.writetext("a.txt", "totally different")

        with self.assertRaises(MigrationIncomplete) as caught:
            migrate_fs(
                self.make_src(),
                self.make_dst(),
                self.journal_path,
                conflict_policy="skip",
            )
        self.assertEqual(caught.exception.conflicts, ["/a.txt"])
        self.assertEqual(caught.exception.pending, 0)

        with self.make_src() as src:
            self.assertTrue(src.exists("a.txt"))
            self.assertFalse(src.exists("sub/b.txt"))
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "totally different")
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")

    def test_keep_source_completes_without_deletion(self):
        self._populate()
        result = migrate_fs(
            self.make_src(),
            self.make_dst(),
            self.journal_path,
            delete_source=False,
        )
        self.assertEqual(result.completed, 3)
        with self.make_src() as src:
            self.assertTrue(src.exists("a.txt"))
            self.assertTrue(src.exists("sub/b.txt"))
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")

    def test_partial_sidecar_is_truncated_not_appended(self):
        self._populate()
        journal = MigrationJournal(self.journal_path)
        with self.make_src() as src, self.make_dst() as dst:
            migrator = Migrator(src, dst, journal)
            migrator.plan(src, dst)
            entry = journal.get("/sub/b.txt")
            tmp_path = migrator._temp_path(entry)
            dst.makedir("sub", recreate=True)
            dst.writetext(tmp_path, "LEFTOVER-PARTIAL-DATA")
            journal.transition("/sub/b.txt", STATE_PENDING)
            migrator.run(rescan=False)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")
            self.assertFalse(dst.exists(tmp_path))

    def test_interrupted_copy_resumes_without_appending(self):
        self._populate()

        def stop_during_copy(event, entry):
            if event == "copied" and entry.src_path == "/sub/b.txt":
                raise _Stop()

        with self.assertRaises(_Stop):
            migrate_fs(
                self.make_src(),
                self.make_dst(),
                self.journal_path,
                on_event=stop_during_copy,
            )

        result = migrate_fs(self.make_src(), self.make_dst(), self.journal_path)
        self.assertEqual(result.completed, 3)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("sub/b.txt"), "bravo")
            partials = [name for name in dst.listdir("sub") if name.endswith(".part")]
            self.assertEqual(partials, [])

    def test_source_vanished_but_identical_destination_completes(self):
        self._populate()
        with self.make_dst() as dst:
            dst.writetext("a.txt", "alpha")
        journal = MigrationJournal(self.journal_path)
        with self.make_src() as src, self.make_dst() as dst:
            migrator = Migrator(src, dst, journal)
            migrator.plan(src, dst)
        os.remove(os.path.join(self.src_dir, "a.txt"))
        with self.make_src() as src, self.make_dst() as dst:
            migrator = Migrator(src, dst, journal)
            result = migrator.run(rescan=False)
        self.assertEqual(result.completed, 3)
        self.assertEqual(journal.get("/a.txt").state, "completed")

    def test_tampered_destination_before_deletion_keeps_source(self):
        self._populate()

        def tamper(event, entry):
            if event == "deletable" and entry.src_path == "/a.txt":
                with self.make_dst() as dst:
                    dst.writetext("a.txt", "tampered contents")

        with self.assertRaises(MigrationConflict):
            migrate_fs(
                self.make_src(),
                self.make_dst(),
                self.journal_path,
                on_event=tamper,
            )
        with self.make_src() as src:
            self.assertTrue(src.exists("a.txt"))
        journal = MigrationJournal(self.journal_path)
        self.assertEqual(journal.get("/a.txt").state, STATE_PENDING)

    def test_memory_fs_end_to_end(self):
        src = open_fs("mem://")
        dst = open_fs("mem://")
        src.makedirs("d")
        src.writetext("d/f.txt", "hello")
        result = migrate_fs(src, dst, self.journal_path)
        self.assertEqual(result.completed, 1)
        self.assertFalse(src.exists("d/f.txt"))
        self.assertEqual(dst.readtext("d/f.txt"), "hello")

    def test_replan_picks_up_new_resources_and_keeps_state(self):
        self._populate()
        journal = MigrationJournal(self.journal_path)
        with self.make_src() as src, self.make_dst() as dst:
            migrator = Migrator(src, dst, journal)
            migrator.plan(src, dst)
            self.assertEqual(len(journal.entries()), 3)
            journal.transition("/a.txt", STATE_COPIED)
            src.writetext("new.txt", "newcomer")
            migrator.plan(src, dst)
        self.assertEqual(len(journal.entries()), 4)
        self.assertEqual(journal.get("/a.txt").state, STATE_COPIED)
        self.assertEqual(journal.get("/new.txt").state, STATE_PENDING)

    def test_fs_urls_are_accepted(self):
        self._populate()
        result = migrate_fs(
            "osfs://" + self.src_dir,
            "osfs://" + self.dst_dir,
            self.journal_path,
        )
        self.assertEqual(result.completed, 3)
        with self.make_dst() as dst:
            self.assertEqual(dst.readtext("a.txt"), "alpha")


class TestSharedSources(MigrationTestMixin, unittest.TestCase):
    def test_shared_source_not_deleted_by_first_finishing_task(self):
        with self.make_src() as src:
            src.writetext("shared.txt", "payload")

        dst_a = os.path.join(self.tmp, "dst_a")
        dst_b = os.path.join(self.tmp, "dst_b")
        os.makedirs(dst_a)
        os.makedirs(dst_b)
        journal_a = os.path.join(self.tmp, "journal_a.json")
        journal_b = os.path.join(self.tmp, "journal_b.json")

        registry = ShareRegistry(self.registry_path)
        with OSFS(self.src_dir) as src_a, OSFS(dst_a) as dst_fs_a:
            migrator_a = Migrator(src_a, dst_fs_a, MigrationJournal(journal_a),
                                  share_registry=registry)
            migrator_a.plan(src_a, dst_fs_a)
        with OSFS(self.src_dir) as src_b, OSFS(dst_b) as dst_fs_b:
            migrator_b = Migrator(src_b, dst_fs_b, MigrationJournal(journal_b),
                                  share_registry=registry)
            migrator_b.plan(src_b, dst_fs_b)

        with OSFS(self.src_dir) as src_a, OSFS(dst_a) as dst_fs_a:
            migrator_a = Migrator(
                src_a, dst_fs_a, MigrationJournal(journal_a),
                share_registry=registry,
            )
            with self.assertRaises(MigrationIncomplete):
                migrator_a.run(rescan=False)
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, "shared.txt")))
        with open(os.path.join(dst_a, "shared.txt")) as dst_file:
            self.assertEqual(dst_file.read(), "payload")

        with OSFS(self.src_dir) as src_b, OSFS(dst_b) as dst_fs_b:
            migrator_b = Migrator(
                src_b, dst_fs_b, MigrationJournal(journal_b),
                share_registry=registry,
            )
            migrator_b.run(rescan=False)
        self.assertFalse(os.path.exists(os.path.join(self.src_dir, "shared.txt")))

        with OSFS(self.src_dir) as src_a, OSFS(dst_a) as dst_fs_a:
            migrator_a = Migrator(
                src_a, dst_fs_a, MigrationJournal(journal_a),
                share_registry=registry,
            )
            result = migrator_a.run(rescan=False)
        self.assertEqual(result.completed, 1)

    def test_shared_memory_fs_uses_source_name(self):
        src = open_fs("mem://")
        dst_a = open_fs("mem://")
        dst_b = open_fs("mem://")
        src.writetext("shared.txt", "payload")

        registry = ShareRegistry(self.registry_path)
        journal_a = MigrationJournal(os.path.join(self.tmp, "ja.json"))
        journal_b = MigrationJournal(os.path.join(self.tmp, "jb.json"))

        migrator_a = Migrator(
            src, dst_a, journal_a,
            share_registry=registry, source_name="backend-1",
        )
        migrator_b = Migrator(
            src, dst_b, journal_b,
            share_registry=registry, source_name="backend-1",
        )
        migrator_a.plan(src, dst_a)
        migrator_b.plan(src, dst_b)

        with self.assertRaises(MigrationIncomplete):
            migrator_a.run(rescan=False)
        self.assertTrue(src.exists("shared.txt"))
        self.assertEqual(dst_a.readtext("shared.txt"), "payload")

        migrator_b.run(rescan=False)
        self.assertFalse(src.exists("shared.txt"))
        result = migrator_a.run(rescan=False)
        self.assertEqual(result.completed, 1)


class TestJournalStateMachine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fs-journal-")
        self.journal_path = os.path.join(self.tmp, "journal.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_illegal_transition_rejected(self):
        journal = MigrationJournal(self.journal_path)
        with journal.lock:
            journal.upsert("/f.txt", "/f.txt")
            journal.upsert("/f.txt", "/f.txt", state=STATE_COPIED)
            journal.upsert("/f.txt", "/f.txt", state=STATE_VERIFIED)
            journal.upsert("/f.txt", "/f.txt", state=STATE_DELETABLE)
            journal.upsert("/f.txt", "/f.txt", state="completed")
        with self.assertRaises(ValueError):
            journal.transition("/f.txt", STATE_PENDING)

    def test_reopen_keeps_job_id_and_counts(self):
        journal = MigrationJournal(self.journal_path)
        with journal.lock:
            journal.upsert("/a", "/a", state=STATE_COPIED)
            journal.save()
        reopened = MigrationJournal(self.journal_path)
        self.assertEqual(reopened.job_id, journal.job_id)
        self.assertEqual(reopened.get("/a").state, STATE_COPIED)
        self.assertEqual(reopened.counts()[STATE_COPIED], 1)


if __name__ == "__main__":
    unittest.main()
