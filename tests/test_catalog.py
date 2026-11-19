import json
import os
import tempfile
import unittest

from imap_tools.catalog import (
    FOLDER_ABSENT,
    FOLDER_ACTIVE,
    FOLDER_SUPERSEDED,
    OCCURRENCE_MOVED,
    OCCURRENCE_PRESENT,
    OCCURRENCE_UNOBSERVED,
    Catalog,
    CatalogConflict,
    CatalogError,
    CopyUid,
    CopyUidMapping,
    FolderObservation,
    MoveEvidence,
    MoveEvidenceError,
    ServerIdentity,
    UnknownFolderError,
    expand_uid_set,
    parse_copyuid,
)

SERVER = ServerIdentity(host='imap.example.com', username='user@example.com')


def obs(name, uidvalidity, uidnext=None, uids=None, messages=None):
    return FolderObservation(
        name=name,
        uidvalidity=uidvalidity,
        uidnext=uidnext,
        uids=uids,
        message_count=messages,
    )


def bootstrap(folders=None):
    catalog = Catalog()
    if folders is None:
        folders = [obs('Archive', 10, uidnext=100, uids=('1', '2'))]
    catalog.observe_folders(SERVER, folders, at='2026-01-01T00:00:00+00:00')
    return catalog


class ServerIdentityTest(unittest.TestCase):
    def test_key_distinguishes_accounts_and_ports(self):
        a = ServerIdentity('imap.example.com', 'user@example.com', 993)
        b = ServerIdentity('imap.example.com', 'other@example.com', 993)
        c = ServerIdentity('imap.example.com', 'user@example.com', 143)
        self.assertEqual(a.key, 'user@example.com@imap.example.com:993')
        self.assertNotEqual(a.key, b.key)
        self.assertNotEqual(a.key, c.key)

    def test_requires_host_and_username(self):
        with self.assertRaises(ValueError):
            ServerIdentity('', 'user')
        with self.assertRaises(ValueError):
            ServerIdentity('host', '')
        with self.assertRaises(ValueError):
            ServerIdentity('host', 'user', 0)


class FolderObservationTest(unittest.TestCase):
    def test_from_status_is_case_insensitive(self):
        observation = FolderObservation.from_status('Archive', {
            'messages': 3,
            'uidnext': 42,
            'uidvalidity': 7,
        }, uids=('1', '2', '3'))
        self.assertEqual(observation.uidvalidity, 7)
        self.assertEqual(observation.uidnext, 42)
        self.assertEqual(observation.message_count, 3)
        self.assertEqual(observation.uids, ('1', '2', '3'))

    def test_from_status_requires_uidvalidity(self):
        with self.assertRaises(ValueError):
            FolderObservation.from_status('Archive', {'MESSAGES': 1})

    def test_rejects_non_decimal_uids(self):
        with self.assertRaises(ValueError):
            FolderObservation('Archive', 1, uids=('1', '*'))


class UidSetTest(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(expand_uid_set('1,3:5,9'), ['1', '3', '4', '5', '9'])
        self.assertEqual(expand_uid_set('2:*', max_uid=5), ['2', '3', '4', '5'])
        self.assertEqual(expand_uid_set('*', max_uid=1), ['1'])

    def test_expand_star_requires_max(self):
        with self.assertRaises(ValueError):
            expand_uid_set('*')

    def test_parse_copyuid(self):
        mapping = parse_copyuid(
            [b'OK [COPYUID 777 10:12 40:42] Done'],
            source_max_uid=20,
            destination_max_uid=99,
        )
        self.assertEqual(mapping.uidvalidity, 777)
        self.assertEqual(
            [(pair.source_uid, pair.destination_uid) for pair in mapping.pairs],
            [('10', '40'), ('11', '41'), ('12', '42')],
        )


    def test_parse_copyuid_collects_chunked_responses(self):
        mapping = parse_copyuid([
            b'OK [COPYUID 777 1:2 10:11] Done',
            b'OK [COPYUID 777 3 20] Done',
        ])
        self.assertEqual(mapping.uidvalidity, 777)
        self.assertEqual(
            [(pair.source_uid, pair.destination_uid) for pair in mapping.pairs],
            [('1', '10'), ('2', '11'), ('3', '20')],
        )

    def test_parse_copyuid_rejects_mixed_destinations(self):
        with self.assertRaises(ValueError):
            parse_copyuid([
                b'OK [COPYUID 777 1 10] Done',
                b'OK [COPYUID 778 2 11] Done',
            ])

    def test_parse_copyuid_requires_code(self):
        with self.assertRaises(ValueError):
            parse_copyuid([b'OK Completed'])

    def test_parse_copyuid_size_mismatch(self):
        with self.assertRaises(ValueError):
            parse_copyuid([b'OK [COPYUID 1 1:3 5:6] Done'])


class FolderCatalogTest(unittest.TestCase):
    def test_new_folder_gets_generation_one(self):
        catalog = Catalog()
        report = catalog.observe_folders(SERVER, [obs('Archive', 10, uidnext=100)], at='2026-01-01T00:00:00Z')
        self.assertTrue(report.ok)
        self.assertEqual(len(report.created), 1)
        record = catalog.folder(report.created[0])
        self.assertEqual(record.status, FOLDER_ACTIVE)
        self.assertEqual(record.name, 'Archive')
        self.assertEqual(record.generation, 1)

    def test_rename_is_tracked_by_uidvalidity(self):
        catalog = bootstrap()
        original = catalog.find_folder(SERVER, 'Archive')
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive 2026', 10, uidnext=100, uids=('1', '2'))],
            at='2026-02-01T00:00:00Z',
        )
        self.assertTrue(report.ok)
        self.assertEqual([event.old_name for event in report.renamed], ['Archive'])
        self.assertEqual([event.new_name for event in report.renamed], ['Archive 2026'])
        record = catalog.folder(original.folder_id)
        self.assertEqual(record.name, 'Archive 2026')
        self.assertEqual(record.previous_names, ['Archive'])
        self.assertEqual(record.all_names, ('Archive', 'Archive 2026'))
        self.assertEqual(len(catalog.folders()), 1)

    def test_rename_requires_more_than_uidvalidity(self):
        # Two folders share a UIDVALIDITY, which IMAP permits because the value
        # is unique per mailbox, not per server.  A bare UIDVALIDITY match is
        # therefore ambiguous and must not produce a rename.
        catalog = bootstrap([
            obs('Archive', 10),
            obs('Sent', 10),
        ])
        report = catalog.observe_folders(
            SERVER,
            [obs('Sent', 10), obs('Old Archive', 10)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertEqual(report.renamed, ())
        self.assertTrue(any(conflict.kind == 'ambiguous_rename' for conflict in report.conflicts))
        # The observation is preserved as a new generation rather than guessed
        # to be a rename, and the old record is no longer claimed as its origin.
        self.assertIsNotNone(catalog.find_folder(SERVER, 'Old Archive'))
        self.assertEqual(catalog.find_folder(SERVER, 'Archive').status, FOLDER_ABSENT)

    def test_uidnext_decrease_blocks_rename(self):
        catalog = bootstrap()
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive 2026', 10, uidnext=99)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertEqual(report.renamed, ())
        self.assertTrue(any(
            conflict.kind == 'contradictory_rename_evidence'
            for conflict in report.conflicts))
        self.assertIsNotNone(catalog.find_folder(SERVER, 'Archive 2026'))
        self.assertEqual(catalog.find_folder(SERVER, 'Archive').status, FOLDER_ABSENT)

    def test_uidnext_match_breaks_uidvalidity_tie(self):
        catalog = bootstrap([
            obs('Archive', 10, uidnext=100),
            obs('Sent', 10, uidnext=50),
        ])
        report = catalog.observe_folders(
            SERVER,
            [obs('Sent', 10, uidnext=50), obs('Cold Storage', 10, uidnext=100)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertTrue(report.ok, [str(conflict) for conflict in report.conflicts])
        archive = catalog.find_folder(SERVER, 'Cold Storage')
        self.assertEqual(archive.previous_names, ['Archive'])
        sent = catalog.find_folder(SERVER, 'Sent')
        self.assertEqual(sent.previous_names, [])

    def test_name_swap_between_generations(self):
        catalog = bootstrap([
            obs('A', 10, uidnext=20),
            obs('B', 11, uidnext=30),
        ])
        report = catalog.observe_folders(
            SERVER,
            [obs('A', 11, uidnext=30), obs('B', 10, uidnext=20)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertTrue(report.ok, [str(conflict) for conflict in report.conflicts])
        self.assertEqual(
            {(event.old_name, event.new_name) for event in report.renamed},
            {('A', 'B'), ('B', 'A')},
        )
        self.assertEqual(catalog.find_folder(SERVER, 'A').uidvalidity, 11)
        self.assertEqual(catalog.find_folder(SERVER, 'B').uidvalidity, 10)

    def test_delete_and_recreate_same_name_is_new_generation(self):
        catalog = bootstrap()
        old = catalog.find_folder(SERVER, 'Archive')
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive', 99, uidnext=1)],
            at='2026-02-01T00:00:00Z',
        )
        new = catalog.find_folder(SERVER, 'Archive')
        self.assertNotEqual(old.folder_id, new.folder_id)
        self.assertEqual(new.generation, 2)
        self.assertEqual(catalog.folder(old.folder_id).status, FOLDER_SUPERSEDED)
        self.assertEqual(catalog.folder(old.folder_id).superseded_by, new.folder_id)
        self.assertEqual(report.recreated, ((old.folder_id, new.folder_id),))
        self.assertEqual(len(catalog.folders()), 2)

    def test_superseded_identity_reappearing_is_conflict(self):
        catalog = bootstrap()
        catalog.observe_folders(
            SERVER,
            [obs('Archive', 99, uidnext=1)],
            at='2026-02-01T00:00:00Z',
        )
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive', 10, uidnext=100)],
            at='2026-03-01T00:00:00Z',
        )
        self.assertIn('Archive', report.unresolved)
        self.assertTrue(any(
            conflict.kind == 'superseded_identity_reappeared'
            for conflict in report.conflicts))

    def test_absent_folder_is_not_deleted(self):
        catalog = bootstrap()
        old = catalog.find_folder(SERVER, 'Archive')
        report = catalog.observe_folders(
            SERVER,
            [obs('Other', 77, uidnext=1)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertIn(old.folder_id, report.absent)
        self.assertEqual(catalog.folder(old.folder_id).status, FOLDER_ABSENT)
        self.assertEqual(catalog.folder(old.folder_id).absent_since.isoformat(),
                         '2026-02-01T00:00:00+00:00')

    def test_absent_folder_returning_keeps_identity(self):
        catalog = bootstrap()
        old = catalog.find_folder(SERVER, 'Archive')
        catalog.observe_folders(SERVER, [obs('Other', 77, uidnext=1)],
                                at='2026-02-01T00:00:00Z')
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive', 10, uidnext=100)],
            at='2026-03-01T00:00:00Z',
        )
        self.assertTrue(report.ok)
        record = catalog.find_folder(SERVER, 'Archive')
        self.assertEqual(record.folder_id, old.folder_id)
        self.assertEqual(record.status, FOLDER_ACTIVE)
        self.assertEqual(len(catalog.folders()), 2)

    def test_duplicate_snapshot_names_rejected(self):
        catalog = Catalog()
        with self.assertRaises(ValueError):
            catalog.observe_folders(SERVER, [obs('A', 1), obs('A', 2)])

    def test_same_name_same_uidvalidity_is_same_generation(self):
        catalog = bootstrap()
        report = catalog.observe_folders(
            SERVER,
            [obs('Archive', 10, uidnext=120, uids=('1', '2', '3'))],
            at='2026-02-01T00:00:00Z',
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.created, ())
        self.assertEqual(report.renamed, ())
        self.assertEqual(len(catalog.folders()), 1)


class OccurrenceTest(unittest.TestCase):
    def setUp(self):
        self.catalog = bootstrap()
        self.archive = self.catalog.find_folder(SERVER, 'Archive')
        self.catalog.observe_folders(
            SERVER,
            [obs('Archive', 10, uidnext=100, uids=('1', '2'))],
            at='2026-01-01T00:00:00Z',
        )
        self.catalog.observe_folders(
            SERVER,
            [obs('Archive', 10, uidnext=100, uids=('1', '2')), obs('Projects', 20, uidnext=50)],
            at='2026-01-02T00:00:00Z',
        )
        self.projects = self.catalog.find_folder(SERVER, 'Projects')

    def test_move_keeps_old_occurrence(self):
        self.catalog.observe_messages(self.archive.folder_id, ['1', '2'])
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'),))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        report = self.catalog.record_move(evidence)
        self.assertEqual(len(report.moved), 1)
        old = self.catalog.occurrence(self.archive.folder_id, '1')
        new = self.catalog.occurrence(self.projects.folder_id, '7')
        self.assertEqual(old.status, OCCURRENCE_MOVED)
        self.assertEqual(old.moved_to, new.occurrence_id)
        self.assertEqual(new.moved_from, old.occurrence_id)
        self.assertEqual(new.status, OCCURRENCE_PRESENT)

    def test_move_rejects_wrong_destination_uidvalidity(self):
        mapping = CopyUidMapping(uidvalidity=999, pairs=(CopyUid('1', '7'),))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        with self.assertRaises(MoveEvidenceError):
            self.catalog.record_move(evidence)

    def test_move_rejects_existing_destination_occurrence(self):
        self.catalog.observe_messages(self.projects.folder_id, ['7'])
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'),))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        with self.assertRaises(MoveEvidenceError):
            self.catalog.record_move(evidence)

    def test_move_is_idempotent(self):
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'), CopyUid('2', '8')))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        self.catalog.record_move(evidence)
        second = self.catalog.record_move(evidence)
        self.assertEqual(second.moved, ())
        self.assertEqual(len(second.already_applied), 2)

    def test_move_conflict_leaves_catalog_unchanged(self):
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'), CopyUid('2', '8')))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        self.catalog.record_move(evidence)
        conflicting = MoveEvidence(
            server=SERVER,
            source_folder_id=self.archive.folder_id,
            destination_folder_id=self.projects.folder_id,
            destination_uidvalidity=20,
            pairs=(CopyUid('2', '9'),),
        )
        with self.assertRaises(MoveEvidenceError):
            self.catalog.record_move(conflicting)
        self.assertEqual(self.catalog.occurrence(self.archive.folder_id, '2').moved_to,
                         f'{self.projects.folder_id}/8')
        self.assertIsNone(self.catalog.occurrence(self.projects.folder_id, '9'))

    def test_absent_source_folder_keeps_move_link(self):
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'),))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        self.catalog.record_move(evidence)
        self.catalog.observe_folders(
            SERVER,
            [obs('Projects', 20, uidnext=50)],
            at='2026-02-01T00:00:00Z',
        )
        self.assertEqual(self.catalog.folder(self.archive.folder_id).status, FOLDER_ABSENT)
        old = self.catalog.occurrence(self.archive.folder_id, '1')
        self.assertEqual(old.status, OCCURRENCE_MOVED)
        self.assertEqual(old.moved_to, f'{self.projects.folder_id}/7')

    def test_complete_snapshot_marks_unobserved_not_deleted(self):
        self.catalog.observe_messages(self.archive.folder_id, ['1', '2'], complete=True)
        self.catalog.observe_messages(self.archive.folder_id, ['1'], complete=True)
        self.assertEqual(self.catalog.occurrence(self.archive.folder_id, '2').status,
                         OCCURRENCE_UNOBSERVED)
        self.assertEqual(self.catalog.occurrence(self.archive.folder_id, '2').status,
                         OCCURRENCE_UNOBSERVED)
        self.assertEqual(len(self.catalog.occurrences(folder_id=self.archive.folder_id)), 2)

    def test_partial_snapshot_does_not_mark_unobserved(self):
        self.catalog.observe_messages(self.archive.folder_id, ['1', '2'])
        self.catalog.observe_messages(self.archive.folder_id, ['1'])
        self.assertEqual(self.catalog.occurrence(self.archive.folder_id, '2').status,
                         OCCURRENCE_PRESENT)

    def test_mark_deleted_is_explicit(self):
        self.catalog.observe_messages(self.archive.folder_id, ['1'])
        self.catalog.mark_deleted(self.archive.folder_id, ['1'])
        self.assertEqual(self.catalog.occurrence(self.archive.folder_id, '1').status, 'deleted')

    def test_mark_deleted_refuses_moved_occurrence(self):
        mapping = CopyUidMapping(uidvalidity=20, pairs=(CopyUid('1', '7'),))
        evidence = MoveEvidence.from_copyuid(
            SERVER, self.archive.folder_id, self.projects.folder_id, mapping)
        self.catalog.record_move(evidence)
        with self.assertRaises(CatalogError):
            self.catalog.mark_deleted(self.archive.folder_id, ['1'])

    def test_superseded_generation_rejects_new_messages(self):
        catalog = bootstrap()
        old = catalog.find_folder(SERVER, 'Archive')
        catalog.observe_folders(
            SERVER,
            [obs('Archive', 99, uidnext=1)],
            at='2026-02-01T00:00:00Z',
        )
        with self.assertRaises(CatalogError):
            catalog.observe_messages(old.folder_id, ['1'])
        new = catalog.find_folder(SERVER, 'Archive')
        self.assertNotEqual(new.folder_id, old.folder_id)

    def test_unknown_folder(self):
        with self.assertRaises(UnknownFolderError):
            self.catalog.observe_messages('missing', ['1'])


class PersistenceTest(unittest.TestCase):
    def test_round_trip_preserves_history(self):
        catalog = bootstrap()
        archive = catalog.find_folder(SERVER, 'Archive')
        catalog.observe_messages(archive.folder_id, ['1', '2'])
        catalog.observe_folders(SERVER, [obs('Archive 2026', 10, uidnext=100)],
                                at='2026-02-01T00:00:00Z')
        catalog.observe_folders(SERVER, [obs('Archive 2026', 55, uidnext=1)],
                                at='2026-03-01T00:00:00Z')
        restored = Catalog.loads(catalog.dumps())
        self.assertEqual(
            [record.folder_id for record in restored.folders(server=SERVER)],
            [record.folder_id for record in catalog.folders(server=SERVER)],
        )
        self.assertEqual(len(restored.renames(SERVER)), 1)
        self.assertEqual(len(restored.occurrences()), 2)
        self.assertEqual(restored.dumps(), catalog.dumps())

    def test_save_and_load_file(self):
        catalog = bootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'nested', 'catalog.json')
            catalog.save(path)
            self.assertTrue(os.path.exists(path))
            restored = Catalog.load(path)
        self.assertEqual(restored.dumps(), catalog.dumps())

    def test_rejects_unknown_format_and_version(self):
        data = json.loads(bootstrap().dumps())
        data['format'] = 'something-else'
        with self.assertRaises(ValueError):
            Catalog.from_dict(data)
        data = json.loads(bootstrap().dumps())
        data['version'] = 999
        with self.assertRaises(ValueError):
            Catalog.from_dict(data)

    def test_catalog_is_plain_json(self):
        catalog = bootstrap()
        payload = json.loads(catalog.dumps())
        self.assertEqual(payload['format'], 'imap_tools.catalog')
        self.assertEqual(payload['version'], 1)


class ConflictTest(unittest.TestCase):
    def test_conflict_str_is_informative(self):
        conflict = CatalogConflict(
            kind='ambiguous_rename',
            message='no unique match',
            folder_ids=('a#1', 'a#2'),
            names=('A', 'B'),
        )
        text = str(conflict)
        self.assertIn('ambiguous_rename', text)
        self.assertIn('a#1', text)
        self.assertIn('A', text)


class ExportTest(unittest.TestCase):
    def test_public_package_exports(self):
        import imap_tools
        for name in ('Catalog', 'FolderObservation', 'MoveEvidence', 'ServerIdentity', 'parse_copyuid'):
            self.assertIn(name, imap_tools.__all__)
            self.assertTrue(hasattr(imap_tools, name))

    def test_no_content_matching_api(self):
        from imap_tools import catalog
        names = [name for name in dir(catalog) if not name.startswith('_')]
        suspicious = [
            name for name in names
            if any(word in name.lower() for word in ('subject', 'messageid', 'checksum', 'content'))
        ]
        self.assertEqual(suspicious, [])


if __name__ == '__main__':
    unittest.main()
