import unittest
from unittest.mock import patch

from imap_tools import MailBox
from imap_tools.errors import (
    MailboxFolderSelectError,
    MailboxReadOnlyConflictError,
    MailboxReadOnlyError,
    MailboxReadOnlyUnverifiedError,
    MailboxReadOnlyViolationError,
)


class FakeIMAP4:
    """Small in-memory IMAP4 stand-in that records every command."""

    def __init__(self, messages=None, readonly_evidence=True):
        self.state = 'AUTH'
        self.is_readonly = False
        self.untagged_responses = {}
        self.commands = []
        self.store_count = 0
        self.messages = messages or {
            '1': {'flags': (), 'body': b'Subject: one\r\n\r\nbody one'},
            '2': {'flags': ('\\Seen',), 'body': b'Subject: two\r\n\r\nbody two'},
        }
        self.readonly_evidence = readonly_evidence
        self.sock = object()

    # -- plumbing -------------------------------------------------------
    def _command(self, name, *args, **kwargs):
        name_upper = str(name).upper()
        self.commands.append((name_upper, args))
        if name_upper == 'EXAMINE':
            self.state = 'SELECTED'
            self.is_readonly = True
            if self.readonly_evidence:
                self.untagged_responses.setdefault('READ-ONLY', []).append(b'[READ-ONLY] ok')
        elif name_upper == 'SELECT':
            self.state = 'SELECTED'
            self.is_readonly = False
        elif name_upper == 'UNSELECT':
            self.state = 'AUTH'
        return b'TAG'

    def _simple_command(self, name, *args, **kwargs):
        self._command(name, *args)
        if str(name).upper() in ('EXAMINE', 'SELECT'):
            return 'OK', [b'2']
        if str(name).upper() == 'UNSELECT':
            return 'OK', [b'unselected']
        return 'OK', [b'ok']

    def select(self, mailbox, readonly=False):
        result = self._simple_command('EXAMINE' if readonly else 'SELECT', mailbox)
        return 'OK', [b'2']

    def uid(self, command, *args):
        command = command.upper()
        self._command('UID', command, *args)
        recorded_name = 'UID ' + command
        # Rename the just-recorded entry so assertions can filter by subcommand.
        self.commands[-1] = (recorded_name, args)
        if command in ('SEARCH', 'SORT'):
            return 'OK', [b' '.join(k.encode() for k in self.messages)]
        if command == 'FETCH':
            uid_set, parts = args[0], args[1]
            parts_text = parts.decode() if isinstance(parts, bytes) else str(parts)
            fetched = []
            for uid in uid_set.split(','):
                message = self.messages.get(uid)
                if message is None:
                    continue
                meta = f'{uid} (UID {uid} FLAGS ({" ".join(message["flags"])}) RFC822.SIZE {len(message["body"])})'
                if 'BODY[' in parts_text.upper() or 'RFC822' in parts_text.upper():
                    fetched.append((meta.encode(), message['body']))
                else:
                    fetched.append(meta.encode())
            return 'OK', fetched
        if command in ('STORE', 'COPY', 'MOVE'):
            self.store_count += 1
            return 'OK', [b'1']
        raise AssertionError(f'unexpected UID command: {command}')

    def expunge(self):
        self._command('EXPUNGE')
        return 'OK', []

    def append(self, *args, **kwargs):
        self._command('APPEND', *args)
        return 'OK', [b'appended']


class ReadOnlyScopeTest(unittest.TestCase):
    def make_mailbox(self, client):
        with patch.object(MailBox, '_get_mailbox_client', return_value=client):
            mailbox = MailBox('localhost')
        # The fake client is not a full imaplib.IMAP4; register the guard hook.
        mailbox.client = client
        mailbox.folder.mailbox = mailbox
        return mailbox

    def test_headers_first_and_bodies_use_peek(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            messages = list(mailbox.fetch())
            self.assertEqual(len(messages), 2)
            fetch_commands = [args for name, args in client.commands if name == 'UID FETCH']
            self.assertTrue(fetch_commands)
            self.assertTrue(all('BODY.PEEK[HEADER]' in a[1] for a in fetch_commands))
            # Bodies are not in the first fetch, and later property access is PEEK only.
            self.assertEqual(messages[0].text, 'body one')
            body_fetches = [args for name, args in client.commands
                            if name == 'UID FETCH' and 'BODY.PEEK[]' in args[1]]
            self.assertEqual(len(body_fetches), 1)
            self.assertTrue(all(client.store_count == 0 for _ in (0,)))
        report = mailbox.readonly_report
        self.assertTrue(report.complete)
        self.assertTrue(report.readonly_verified)
        self.assertIn('READ-ONLY', ' '.join(report.readonly_evidence))

    def test_attachment_payload_uses_same_peek_scope(self):
        body = (
            b'Subject: att\r\nContent-Type: multipart/mixed; boundary=BB\r\n\r\n'
            b'--BB\r\nContent-Type: text/plain\r\n\r\nhello\r\n'
            b'--BB\r\nContent-Type: application/octet-stream\r\n'
            b'Content-Disposition: attachment; filename=a.bin\r\n'
            b'Content-Transfer-Encoding: base64\r\n\r\naGk=\r\n--BB--\r\n'
        )
        client = FakeIMAP4(messages={'1': {'flags': (), 'body': body}})
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            msg = next(mailbox.fetch())
            self.assertEqual(msg.subject, 'att')
            attachments = msg.attachments
            self.assertEqual(attachments[0].payload, b'hi')
            self.assertEqual(scope.report.lazy_loads, 1)
        self.assertTrue(mailbox.readonly_report.complete)

    def test_writable_borrowed_selection_is_refused_without_select(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        mailbox.folder._current_folder = 'INBOX'
        mailbox.folder._current_readonly = False
        client.state = 'SELECTED'
        before = list(client.commands)
        with self.assertRaises(MailboxReadOnlyConflictError):
            with mailbox.readonly('INBOX'):
                pass
        self.assertEqual(client.commands, before)
        self.assertFalse(mailbox.folder.is_readonly())

    def test_failed_same_folder_examine_keeps_borrowed_selection(self):
        class NoEvidenceClient(FakeIMAP4):
            def _simple_command(self, name, *args, **kwargs):
                if str(name).upper() == 'EXAMINE':
                    self._command(name, *args)
                    return 'NO', [b'no evidence']
                return super()._simple_command(name, *args, **kwargs)

        client = NoEvidenceClient()
        mailbox = self.make_mailbox(client)
        mailbox.folder._current_folder = 'INBOX'
        mailbox.folder._current_readonly = True
        client.state = 'SELECTED'
        client.is_readonly = True
        with self.assertRaises(MailboxFolderSelectError):
            mailbox.readonly().__enter__()
        self.assertEqual(client.state, 'SELECTED')
        self.assertTrue(client.is_readonly)
        self.assertEqual(mailbox.folder.get(), 'INBOX')

    def test_other_folder_on_selected_connection_is_refused(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        mailbox.folder._current_folder = 'INBOX'
        mailbox.folder._current_readonly = True
        client.state = 'SELECTED'
        client.is_readonly = True
        self.assertFalse(mailbox.preflight_readonly('Archive')['can_enter'])
        with self.assertRaises(MailboxReadOnlyConflictError):
            with mailbox.readonly('Archive'):
                pass

    def test_mutation_attempts_are_refused_and_reported(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            with self.assertRaises(MailboxReadOnlyViolationError):
                mailbox.flag(['1'], '\\Seen', True)
            with self.assertRaises(MailboxReadOnlyViolationError):
                mailbox.delete(['1'])
            with self.assertRaises(MailboxReadOnlyViolationError):
                list(mailbox.fetch(mark_seen=True))
            with self.assertRaises(MailboxReadOnlyViolationError):
                mailbox.folder.set('INBOX')
            self.assertEqual(client.store_count, 0)
        report = mailbox.readonly_report
        self.assertFalse(report.complete)
        self.assertTrue(report.write_attempts)

    def test_non_peek_fetch_is_refused_even_via_client(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            with self.assertRaises(MailboxReadOnlyViolationError):
                client.uid('fetch', '1', '(BODY[] UID FLAGS)')
            with self.assertRaises(MailboxReadOnlyViolationError):
                client.uid('fetch', '1', '(RFC822 UID FLAGS)')
            client.uid('fetch', '1', '(BODY.PEEK[] UID FLAGS)')
        self.assertFalse(mailbox.readonly_report.complete)

    def test_external_flag_change_is_observed_not_restored(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            list(mailbox.fetch())
            # Another client/user sets \Seen on message 1.
            client.messages['1']['flags'] = ('\\Seen',)
        report = mailbox.readonly_report
        self.assertTrue(report.complete)
        self.assertEqual(report.external_flag_changes[0].uid, '1')
        self.assertIn('\\Seen', report.external_flag_changes[0].after)
        # No STORE at all: the difference was not "repaired".
        self.assertEqual(client.store_count, 0)

    def test_imaplib_key_only_readonly_evidence_is_accepted(self):
        class KeyOnlyClient(FakeIMAP4):
            def _command(self, name, *args, **kwargs):
                result = super()._command(name, *args, **kwargs)
                if str(name).upper() == 'EXAMINE':
                    # imaplib records a response code without data as key -> None
                    self.untagged_responses['READ-ONLY'] = [None]
                return result

        client = KeyOnlyClient(readonly_evidence=False)
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            self.assertTrue(scope.report.readonly_verified)
        self.assertTrue(mailbox.readonly_report.complete)

    def test_folder_set_readonly_records_imaplib_evidence(self):
        client = FakeIMAP4(readonly_evidence=False)
        mailbox = self.make_mailbox(client)
        original_simple = client._simple_command

        def simple(name, *args, **kwargs):
            result = original_simple(name, *args, **kwargs)
            if str(name).upper() == 'EXAMINE':
                client.untagged_responses['READ-ONLY'] = [None]
            return result

        client._simple_command = simple
        mailbox.login_result = ('OK', [b'ok'])
        client.state = 'AUTH'
        mailbox.folder.set('INBOX', readonly=True)
        self.assertTrue(mailbox.folder.is_readonly())
        self.assertTrue(mailbox.folder.readonly_evidence())
        with mailbox.readonly() as scope:
            self.assertTrue(scope.report.readonly_verified)

    def test_missing_readonly_evidence_blocks_publish(self):
        client = FakeIMAP4(readonly_evidence=False)
        mailbox = self.make_mailbox(client)
        with self.assertRaises(MailboxReadOnlyUnverifiedError):
            mailbox.readonly('INBOX').__enter__()
        self.assertFalse(mailbox.readonly_complete)
        report = mailbox.readonly_report
        self.assertFalse(report.complete)
        self.assertIn('did not prove READ-ONLY', report.reason)

    def test_lazy_access_after_scope_is_refused(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            message = next(mailbox.fetch())
        with self.assertRaises(MailboxReadOnlyError):
            _ = message.text

    def test_selection_change_during_scope_is_detected(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            list(mailbox.fetch())
            mailbox.folder._current_folder = 'Other'
            with self.assertRaises(MailboxReadOnlyUnverifiedError):
                list(mailbox.fetch())
        self.assertFalse(mailbox.readonly_report.complete)
        self.assertFalse(mailbox.readonly_report.selection_unchanged)

    def test_reconnect_breaks_the_guarantee(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            list(mailbox.fetch())
            mailbox.client = FakeIMAP4()
        report = mailbox.readonly_report
        self.assertFalse(report.complete)
        self.assertTrue(report.reconnect_detected)

    def test_early_exception_keeps_verification_and_scope_blocked(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with self.assertRaises(RuntimeError):
            with mailbox.readonly('INBOX') as scope:
                message = next(mailbox.fetch())
                raise RuntimeError('caller failed early')
        report = mailbox.readonly_report
        self.assertFalse(report.complete)
        self.assertIn('caller failed early', report.reason)
        with self.assertRaises(MailboxReadOnlyError):
            _ = message.text

    def test_client_select_and_append_are_refused_before_mutation(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            is_readonly_before = client.is_readonly
            with self.assertRaises(MailboxReadOnlyViolationError):
                client.select('Other')
            self.assertEqual(client.is_readonly, is_readonly_before)
            self.assertEqual(client.state, 'SELECTED')
            with self.assertRaises(MailboxReadOnlyViolationError):
                client.append('INBOX', None, None, b'raw')
            self.assertEqual(client.state, 'SELECTED')
        self.assertFalse(mailbox.readonly_report.complete)

    def test_fetch_without_uid_metadata_is_unverified(self):
        class NoUidClient(FakeIMAP4):
            def uid(self, command, *args):
                command = command.upper()
                self._command('UID', command, *args)
                self.commands[-1] = ('UID ' + command, args)
                if command in ('SEARCH', 'SORT'):
                    return 'OK', [b'1']
                return 'OK', [b'* 1 FETCH (FLAGS ())']

        client = NoUidClient()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            list(mailbox.fetch())
            self.assertFalse(scope.verify())
        report = mailbox.readonly_report
        self.assertFalse(report.complete)
        self.assertIn('no UID/FLAGS', report.reason)

    def test_strict_exit_raises_unverified(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with self.assertRaises(MailboxReadOnlyUnverifiedError):
            with mailbox.readonly('INBOX', strict=True) as scope:
                with self.assertRaises(MailboxReadOnlyViolationError):
                    mailbox.delete(['1'])

    def test_report_is_queryable_and_serializable(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            list(mailbox.fetch(headers_only=True))
            self.assertFalse(scope.readonly_complete)
        data = mailbox.readonly_report.as_dict()
        for key in ('complete', 'readonly_verified', 'side_effects_verified',
                    'external_flag_changes', 'write_attempts', 'observed_uids'):
            self.assertIn(key, data)

    def test_scope_delegates_and_expires(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            self.assertEqual(len(list(scope.fetch())), 2)
            self.assertEqual(len(scope.uids()), 2)
        with self.assertRaises(MailboxReadOnlyError):
            scope.fetch()

    def test_early_generator_close_is_reported_but_verified(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        with mailbox.readonly('INBOX') as scope:
            first = next(mailbox.fetch())
            self.assertEqual(first.uid, '1')
        report = mailbox.readonly_report
        self.assertTrue(report.partial_read)
        self.assertTrue(report.complete)

    def test_mark_seen_false_outside_scope_keeps_peek(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        list(mailbox.fetch(mark_seen=False))
        fetch_args = [args for name, args in client.commands if name == 'UID FETCH']
        self.assertTrue(all('BODY.PEEK[]' in a[1] for a in fetch_args))

    def test_regular_fetch_default_still_marks_seen(self):
        client = FakeIMAP4()
        mailbox = self.make_mailbox(client)
        list(mailbox.fetch())
        fetch_args = [args for name, args in client.commands if name == 'UID FETCH']
        self.assertTrue(all('BODY[HEADER]' not in a[1] for a in fetch_args))
        self.assertTrue(all('BODY[]' in a[1] for a in fetch_args))


if __name__ == '__main__':
    unittest.main()
