import json
import re
import tempfile
import unittest
from pathlib import Path

import imap_tools
from imap_tools import MailMessage
from imap_tools.incremental import (
    CoverageStatus,
    FileSystemArchive,
    ImportStateError,
    IncrementalImporter,
    MailboxImportState,
    MessageSource,
)
from imap_tools.mailbox import BaseMailBox

RAW_TEMPLATE = (
    b'From: sender@example.com\n'
    b'To: user@example.com\n'
    b'Subject: message 1\n'
    b'Message-ID: <1@example.com>\n'
    b'Date: Sat, 22 Nov 2008 11:04:59 +1100\n'
    b'\n'
    b'Body of message 1.\n'
)


class FakeFolder:
    def __init__(self, server):
        self.server = server
        self.current = None

    def get(self):
        return self.current

    def set(self, folder, readonly=False):
        if self.server.status_error is not None:
            raise self.server.status_error
        self.current = folder
        return 'OK', [b'1']

    def status(self, folder=None, options=None):
        if self.server.status_error is not None:
            raise self.server.status_error
        folder = folder or self.current
        return {
            'UIDVALIDITY': self.server.uidvalidity,
            'UIDNEXT': self.server.uidnext,
        }


class FakeMailBox:
    def __init__(self, uidvalidity=100, messages=None, status_error=None, search_error=None, fetch_errors=None):
        self.uidvalidity = uidvalidity
        self.messages = dict(messages or {})
        self.status_error = status_error
        self.search_error = search_error
        self.fetch_errors = dict(fetch_errors or {})
        self.folder = FakeFolder(self)
        self.fetch_calls = []
        self.search_calls = []

    @property
    def uidnext(self):
        return max(self.messages, default=0) + 1

    def uids(self, criteria='ALL', charset='US-ASCII', sort=None):
        if self.search_error is not None:
            raise self.search_error
        self.search_calls.append(str(criteria))
        match = re.search(r'UID\s+(\d+):\*', str(criteria))
        if not match:
            raise AssertionError(f'unexpected criteria: {criteria}')
        start = int(match.group(1))
        return [str(uid) for uid in sorted(self.messages) if uid >= start]

    def fetch(self, criteria='ALL', charset='US-ASCII', *, uid_list=None, **kwargs):
        uid = int(list(uid_list)[0])
        self.fetch_calls.append(uid)
        error = self.fetch_errors.get(uid)
        if error is not None:
            raise error
        if uid not in self.messages:
            return iter(())
        raw = self.messages[uid]
        return iter([MailMessage([(f'1 (UID {uid} FLAGS ())'.encode(), raw)])])


class IncrementalImporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _mailbox(self, **kwargs):
        mailbox = FakeMailBox(**kwargs)
        if mailbox.status_error is None:
            mailbox.folder.current = 'INBOX'
        return mailbox

    def _state(self, name='state.json'):
        return MailboxImportState(self.root / name)

    def test_initial_import_records_folder_uidvalidity_and_confirmed_range(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE, 3: RAW_TEMPLATE})
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertTrue(result.complete)
        self.assertEqual(result.status, CoverageStatus.COMPLETE)
        self.assertEqual(result.fetched, 3)
        self.assertEqual(result.confirmed, 3)
        self.assertEqual(result.uidvalidity, 100)

        folder_state = state.for_folder('INBOX')
        self.assertEqual(folder_state.folder, 'INBOX')
        self.assertEqual(folder_state.uidvalidity, 100)
        self.assertEqual(folder_state.cursor, 3)
        self.assertEqual(folder_state.confirmed_ranges, [[1, 3]])
        self.assertTrue(all(folder_state.is_confirmed(uid) for uid in (1, 2, 3)))
        self.assertFalse(folder_state.is_confirmed(4))

        reloaded = MailboxImportState.load(self.root / 'state.json')
        self.assertEqual(reloaded.for_folder('INBOX').to_dict(), folder_state.to_dict())

    def test_repeated_run_skips_confirmed_messages(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE})
        state = self._state()
        IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        mailbox.fetch_calls.clear()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertTrue(result.complete)
        self.assertEqual(result.fetched, 0)
        self.assertEqual(mailbox.fetch_calls, [])

        mailbox.messages[3] = RAW_TEMPLATE
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertTrue(result.complete)
        self.assertEqual(mailbox.fetch_calls, [3])
        self.assertEqual(state.for_folder('INBOX').confirmed_ranges, [[1, 3]])

    def test_failed_download_stops_cursor_and_is_retried(self):
        mailbox = self._mailbox(
            messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE, 3: RAW_TEMPLATE, 4: RAW_TEMPLATE},
            fetch_errors={3: ConnectionError('connection reset')},
        )
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertFalse(result.complete)
        self.assertEqual(result.status, CoverageStatus.INCOMPLETE)
        self.assertEqual(result.failed_uid, 3)
        self.assertEqual(result.fetched, 2)
        folder_state = state.for_folder('INBOX')
        self.assertEqual(folder_state.cursor, 2)
        self.assertEqual(folder_state.confirmed_ranges, [[1, 2]])
        self.assertFalse(folder_state.is_confirmed(3))
        self.assertFalse(folder_state.is_confirmed(4))

        mailbox.fetch_errors.clear()
        mailbox.fetch_calls.clear()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertTrue(result.complete)
        self.assertEqual(mailbox.fetch_calls, [3, 4])
        self.assertEqual(folder_state.cursor, 4)
        self.assertEqual(folder_state.confirmed_ranges, [[1, 4]])

    def test_empty_successful_search_is_complete(self):
        mailbox = self._mailbox(messages={})
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertTrue(result.complete)
        self.assertEqual(result.status, CoverageStatus.COMPLETE)
        self.assertTrue(result.coverage.searched)
        self.assertEqual(result.coverage.matched, 0)
        self.assertEqual(result.fetched, 0)
        self.assertIsNone(result.failed_uid)
        self.assertEqual(state.for_folder('INBOX').confirmed_ranges, [])

    def test_connection_error_is_unknown_not_complete(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE}, status_error=ConnectionError('no route to host'))
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertFalse(result.complete)
        self.assertEqual(result.status, CoverageStatus.UNKNOWN)
        self.assertFalse(result.coverage.searched)
        self.assertIn('no route to host', result.error)
        self.assertEqual(state.for_folder('INBOX').cursor, 0)

    def test_search_error_is_unknown_not_complete(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE}, search_error=ConnectionError('search failed'))
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertFalse(result.complete)
        self.assertEqual(result.status, CoverageStatus.UNKNOWN)
        self.assertFalse(result.coverage.searched)

    def test_uidvalidity_change_does_not_reuse_old_uids(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE, 3: RAW_TEMPLATE})
        state = self._state()
        IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        # Same folder, new epoch: all UIDs are reused for different messages.
        mailbox.uidvalidity = 200
        mailbox.messages = {1: b'From: new@example.com\nSubject: new one\n\nnew\n'}
        mailbox.fetch_calls.clear()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        self.assertTrue(result.complete)
        self.assertTrue(result.uidvalidity_changed)
        self.assertEqual(result.started_from_uid, 1)
        self.assertEqual(mailbox.fetch_calls, [1])

        folder_state = state.for_folder('INBOX')
        self.assertEqual(folder_state.uidvalidity, 200)
        self.assertEqual(folder_state.confirmed_ranges, [[1, 1]])
        self.assertEqual(len(folder_state.epochs), 1)
        old_epoch = folder_state.epochs[0]
        self.assertEqual(old_epoch['uidvalidity'], 100)
        self.assertEqual(old_epoch['cursor'], 3)
        self.assertEqual(old_epoch['confirmed'], [[1, 3]])

    def test_archive_keeps_provenance_across_uidvalidity_change(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE})
        archive = FileSystemArchive(self.root / 'archive')
        state = self._state()
        IncrementalImporter(mailbox, state=state, folder='INBOX', archive=archive).run()

        mailbox.uidvalidity = 200
        mailbox.messages = {1: b'From: new@example.com\nSubject: new one\n\nnew\n'}
        IncrementalImporter(mailbox, state=state, folder='INBOX', archive=archive).run()

        old_eml, old_meta = archive.path_for(MessageSource('INBOX', 100, 1))
        new_eml, new_meta = archive.path_for(MessageSource('INBOX', 200, 1))
        self.assertTrue(old_eml.exists())
        self.assertTrue(new_eml.exists())
        self.assertNotEqual(old_eml, new_eml)

        old_source = json.loads(old_meta.read_text(encoding='utf-8'))['source']
        new_source = json.loads(new_meta.read_text(encoding='utf-8'))['source']
        self.assertEqual(old_source, {'folder': 'INBOX', 'uidvalidity': 100, 'uid': 1})
        self.assertEqual(new_source, {'folder': 'INBOX', 'uidvalidity': 200, 'uid': 1})
        self.assertEqual(old_eml.read_bytes(), RAW_TEMPLATE)
        self.assertIn(b'Subject: new one', new_eml.read_bytes())

    def test_folders_keep_independent_state(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE})
        state = self._state()
        IncrementalImporter(mailbox, state=state, folder='INBOX').run()

        mailbox.folder.current = 'Archive'
        mailbox.messages = {7: RAW_TEMPLATE}
        result = IncrementalImporter(mailbox, state=state, folder='Archive').run()

        self.assertTrue(result.complete)
        self.assertEqual(result.started_from_uid, 1)
        self.assertEqual(mailbox.fetch_calls[-1], 7)
        self.assertEqual(state.for_folder('INBOX').confirmed_ranges, [[1, 1]])
        self.assertEqual(state.for_folder('Archive').confirmed_ranges, [[7, 7]])

    def test_callback_failure_does_not_confirm_uid(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE})
        state = self._state()
        calls = []

        def on_message(message, source):
            calls.append(source.uid)
            if source.uid == 2:
                raise RuntimeError('consumer rejected message')

        result = IncrementalImporter(
            mailbox, state=state, folder='INBOX', on_message=on_message).run()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, CoverageStatus.INCOMPLETE)
        self.assertEqual(result.failed_uid, 2)
        self.assertEqual(state.for_folder('INBOX').confirmed_ranges, [[1, 1]])
        self.assertEqual(state.for_folder('INBOX').cursor, 1)

    def test_state_save_failure_rolls_back_last_confirmation(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE})
        state = self._state()
        original_save = state.save

        def failing_save(path=None):
            raise ImportStateError('disk full')

        state.save = failing_save
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertEqual(result.status, CoverageStatus.UNKNOWN)
        self.assertEqual(result.failed_uid, 1)
        self.assertEqual(state.for_folder('INBOX').confirmed_ranges, [])
        self.assertEqual(state.for_folder('INBOX').cursor, 0)
        state.save = original_save

    def test_empty_scan_advances_past_uidnext_gap(self):
        mailbox = self._mailbox(messages={5: RAW_TEMPLATE})
        state = self._state()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertTrue(result.complete)
        self.assertEqual(result.cursor, 5)
        self.assertEqual(state.for_folder('INBOX').next_uid, 6)

        mailbox.messages = {6: RAW_TEMPLATE}
        mailbox.fetch_calls.clear()
        result = IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        self.assertTrue(result.complete)
        self.assertEqual(mailbox.fetch_calls, [6])

    def test_uidvalidity_transition_is_idempotent(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE, 2: RAW_TEMPLATE})
        state = self._state()
        IncrementalImporter(mailbox, state=state, folder='INBOX').run()
        folder_state = state.for_folder('INBOX')
        folder_state.activate(200)
        folder_state.activate(200)
        folder_state.activate(200)
        self.assertEqual([epoch['uidvalidity'] for epoch in folder_state.epochs], [100])

    def test_header_only_archive_is_rejected(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE})
        with self.assertRaises(ValueError):
            IncrementalImporter(
                mailbox, state=self._state(), folder='INBOX',
                archive=self.root / 'archive', headers_only=True)

    def test_invalid_state_is_rejected(self):
        with self.assertRaises(ImportStateError):
            MailboxImportState.loads('{"version": 99, "folders": {}}')
        with self.assertRaises(ImportStateError):
            MailboxImportState.loads('not json')

    def test_mailbox_facade_delegates_to_importer(self):
        mailbox = self._mailbox(messages={1: RAW_TEMPLATE})
        state = self._state()
        result = BaseMailBox.incremental_import(mailbox, state=state, folder='INBOX')
        self.assertTrue(result.complete)
        self.assertEqual(result.fetched, 1)
        self.assertEqual(state.for_folder('INBOX').cursor, 1)

        mailbox2 = self._mailbox(messages={2: RAW_TEMPLATE})
        result = imap_tools.incremental_import(mailbox2, state=self._state('state2.json'), folder='INBOX')
        self.assertTrue(result.complete)
        self.assertEqual(result.fetched, 1)

    def test_malformed_state_values_are_rejected(self):
        for payload in (
            '{"version": 1, "folders": {"INBOX": {"folder": "INBOX", "cursor": "abc"}}}',
            '{"version": 1, "folders": {"INBOX": {"folder": "INBOX", "cursor": -1}}}',
            '{"version": 1, "folders": {"INBOX": {"folder": "INBOX", "confirmed": [[3]]}}}',
        ):
            with self.assertRaises(ImportStateError):
                MailboxImportState.loads(payload)

    def test_confirmed_ranges_are_normalized(self):
        from imap_tools.incremental import FolderImportState
        state = FolderImportState('INBOX', confirmed=[[5, 7], [1, 2], [3, 3]])
        self.assertEqual(state.confirmed_ranges, [[1, 3], [5, 7]])
        state.confirm(4)
        self.assertEqual(state.confirmed_ranges, [[1, 7]])


if __name__ == '__main__':
    unittest.main()
