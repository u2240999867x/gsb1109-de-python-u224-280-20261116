import unittest
from unittest.mock import patch

from imap_tools import MailBox
from imap_tools.body_structure import parse_section_literals
from imap_tools.errors import (
    MessageFolderMismatchError,
    MessageGenerationError,
    MessageMaterialError,
)
from imap_tools.message import MessagePartState

HEADER = b'Subject: two parts\r\nFrom: a@example.com\r\nTo: b@example.com\r\n\r\n'
BODY_TEXT = b'hello body'
BODY_HTML = b'<b>hello</b>'
ATTACHMENT = b'PDFDATA'
ATTACHMENT_WIRE = b'UERGREFUQQ=='  # base64(ATTACHMENT), server returns encoded bytes

SIMPLE_STRUCTURE = (
    '("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1 NIL NIL NIL NIL)'
)
MIXED_STRUCTURE = (
    '(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1 NIL NIL NIL NIL)'
    '("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1 NIL NIL NIL NIL)'
    '("APPLICATION" "PDF" ("NAME" "doc.pdf") NIL NIL "BASE64" 100 NIL '
    '("ATTACHMENT" ("FILENAME" "doc.pdf")) NIL NIL) "MIXED" ("BOUNDARY" "b") NIL NIL NIL)'
)


def header_response(uid='5', structure=SIMPLE_STRUCTURE, size=1000):
    prefix = f'1 (UID {uid} FLAGS (\\Seen) RFC822.SIZE {size} BODYSTRUCTURE {structure})'.encode()
    return 'OK', [prefix, (b' BODY[HEADER]', HEADER)]


def section_response(uid, section, payload):
    prefix = b'1 (UID ' + uid.encode() + b' BODY[' + section.encode() + b'] {' \
        + str(len(payload)).encode() + b'}'
    return 'OK', [(prefix, payload)]


class FakeClient:
    def __init__(self):
        self.responses = []
        self.calls = []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if not self.responses:
            raise AssertionError(f'unexpected command: {command} {args}')
        return self.responses.pop(0)


class PartialMessageTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        patcher = patch.object(MailBox, '_get_mailbox_client', return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mailbox = MailBox('localhost')
        self.mailbox.folder._current_folder = 'INBOX'
        self.mailbox.folder._uidvalidity = 11
        # STATUS is queried to avoid trusting a stale SELECT-time generation
        status_patch = patch.object(
            self.mailbox.folder, 'status', return_value={'UIDVALIDITY': 11})
        self.status_mock = status_patch.start()
        self.addCleanup(status_patch.stop)

    def _fetch_header(self, structure=MIXED_STRUCTURE):
        self.client.responses = [('OK', [b'5']), header_response(structure=structure)]
        return next(self.mailbox.fetch(headers_only=True))

    def test_header_scope_is_complete_without_body(self):
        msg = self._fetch_header()
        self.assertEqual(msg.uid, '5')
        self.assertEqual(msg.ref.uidvalidity, 11)
        self.assertTrue(msg.headers_complete)
        self.assertFalse(msg.body_complete)
        self.assertFalse(msg.attachments_complete)
        self.assertTrue(msg.partial)
        self.assertEqual(msg.status.headers, MessagePartState.COMPLETE)
        self.assertEqual(msg.status.body, MessagePartState.PENDING)
        self.assertEqual(msg.text, '')
        self.assertEqual(msg.html, '')
        self.assertEqual(msg.size_received, len(HEADER))
        self.assertEqual(msg.size, len(bytes(msg.obj)))
        self.assertEqual(msg.size_rfc822, 1000)
        self.assertIsNone(msg.body_size)
        self.assertEqual(msg.body_size_declared, 22)
        self.assertEqual([item.section for item in msg.body_refs], ['1', '2'])
        self.assertEqual([item.filename for item in msg.attachment_refs], ['doc.pdf'])
        self.assertEqual(msg.body_refs[0].declared_size, 10)
        self.assertIsNone(msg.body_refs[0].size)

    def test_body_backfill_keeps_header_size_and_attachment_pending(self):
        msg = self._fetch_header()
        self.client.responses = [
            section_response('5', '1', BODY_TEXT),
            section_response('5', '2', BODY_HTML),
        ]
        self.mailbox.fetch_body([msg])
        self.assertEqual(msg.text, 'hello body')
        self.assertEqual(msg.html, '<b>hello</b>')
        self.assertTrue(msg.body_complete)
        self.assertFalse(msg.attachments_complete)
        self.assertTrue(msg.headers_complete)
        self.assertEqual(msg.size_received, len(HEADER))
        self.assertEqual(msg.body_size, len(BODY_TEXT) + len(BODY_HTML))
        self.assertIsNone(msg.attachment_size)

    def test_stale_uidvalidity_is_rejected_before_fetch(self):
        msg = self._fetch_header()
        self.status_mock.return_value = {'UIDVALIDITY': 12}
        before = len(self.client.calls)
        with self.assertRaises(MessageGenerationError):
            self.mailbox.fetch_body([msg])
        self.assertEqual(len(self.client.calls), before)
        self.assertFalse(msg.body_complete)

    def test_unknown_uidvalidity_is_rejected(self):
        msg = self._fetch_header()
        self.mailbox.folder._uidvalidity = None
        self.status_mock.side_effect = RuntimeError('no status')
        with self.assertRaises(MessageGenerationError):
            self.mailbox.fetch_body([msg])

    def test_folder_mismatch_is_rejected(self):
        msg = self._fetch_header()
        self.mailbox.folder._current_folder = 'Archive'
        with self.assertRaises(MessageFolderMismatchError):
            self.mailbox.fetch_body([msg])

    def test_selective_attachment_fetch(self):
        msg = self._fetch_header()
        self.client.responses = [section_response('5', '3', ATTACHMENT_WIRE)]
        self.mailbox.fetch_attachments([msg], 'doc.pdf')
        ref = msg.attachment_materials[0]
        self.assertEqual(ref.payload, ATTACHMENT)
        self.assertEqual(ref.wire_size, len(ATTACHMENT_WIRE))
        self.assertEqual(ref.size, len(ATTACHMENT))
        self.assertTrue(ref.complete)
        self.assertTrue(msg.headers_complete)
        self.assertFalse(msg.body_complete)
        self.assertEqual(msg.attachments, [])
        self.assertEqual(ref.state, MessagePartState.COMPLETE)

    def test_retry_fetches_only_pending_attachments(self):
        msg = self._fetch_header()
        self.client.responses = [section_response('5', '3', ATTACHMENT_WIRE)]
        self.mailbox.fetch_attachments([msg], 'doc.pdf')
        calls = len(self.client.calls)
        self.mailbox.fetch_attachments([msg], 'doc.pdf')
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(msg.attachment_materials[0].payload, ATTACHMENT)

    def test_unknown_attachment_selector_raises(self):
        msg = self._fetch_header()
        with self.assertRaises(MessageMaterialError):
            self.mailbox.fetch_attachments([msg], 'missing.pdf')
        self.assertEqual(msg.attachment_materials[0].state, MessagePartState.PENDING)

    def test_missing_attachment_does_not_change_headers(self):
        msg = self._fetch_header()
        self.client.responses = [('OK', [b'1 (UID 5 BODY[3] NIL)'])]
        self.mailbox.fetch_attachments([msg], 'doc.pdf')
        self.assertTrue(msg.headers_complete)
        self.assertEqual(msg.status.attachments, MessagePartState.MISSING)
        self.assertTrue(msg.attachment_materials[0].missing)
        self.assertEqual(msg.attachment_materials[0].payload, None)

    def test_bulk_headers_with_inline_bodystructure(self):
        prefix = (b'1 (UID 5 FLAGS (\\Seen) RFC822.SIZE 900 BODY[HEADER] {'
                  + str(len(HEADER)).encode() + b'}')
        self.client.responses = [('OK', [b'5']), ('OK', [
            (prefix, HEADER),
            b' BODYSTRUCTURE ' + MIXED_STRUCTURE.encode() + b')',
        ])]
        msg = next(self.mailbox.fetch(headers_only=True, bulk=True))
        self.assertTrue(msg.headers_complete)
        self.assertEqual([item.section for item in msg.part_refs], ['1', '2', '3'])
        self.assertEqual(msg.status.body, MessagePartState.PENDING)
        self.assertEqual(msg.size_rfc822, 900)
        self.assertEqual(msg.size_received, len(HEADER))

    def test_structure_is_backfilled_before_body(self):
        self.client.responses = [
            ('OK', [b'5']),
            ('OK', [(b'1 (UID 5 BODY[HEADER] {' + str(len(HEADER)).encode() + b'}', HEADER)]),
        ]
        msg = next(self.mailbox.fetch(headers_only=True, structure=False))
        self.assertEqual(msg.part_refs, ())
        self.assertEqual(msg.status.body, MessagePartState.NOT_REQUESTED)
        self.client.responses = [
            ('OK', [(b'1 (UID 5 BODYSTRUCTURE ' + MIXED_STRUCTURE.encode() + b')',)]),
            section_response('5', '1', BODY_TEXT),
            section_response('5', '2', BODY_HTML),
        ]
        self.mailbox.fetch_body([msg])
        self.assertEqual([item.section for item in msg.part_refs], ['1', '2', '3'])
        self.assertEqual(msg.text, 'hello body')
        self.assertTrue(msg.body_complete)
        self.assertFalse(msg.attachments_complete)

    def test_text_attachment_is_not_body(self):
        # text/plain attachment: disposition follows lines/md5 at indexes 8..9
        structure = (
            '(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 5 1 NIL '
            '("ATTACHMENT" ("FILENAME" "note.txt")) NIL NIL) "MIXED" ("BOUNDARY" "b") NIL NIL NIL)'
        )
        self.client.responses = [('OK', [b'5']), header_response(structure=structure)]
        msg = next(self.mailbox.fetch(headers_only=True))
        self.assertEqual([item.section for item in msg.body_refs], [])
        ref = msg.attachment_refs[0]
        self.assertEqual(ref.content_disposition, 'attachment')
        self.assertEqual(ref.filename, 'note.txt')

    def test_section_literals_are_mapped_by_uid_and_section(self):
        data = [
            (b'1 (UID 5 BODY[1] {3}', b'abc'),
            (b' BODY[2] {3}', b'def'),
            (b'2 (UID 6 BODY[1] {3}', b'ghi'),
        ]
        self.assertEqual(parse_section_literals(data), {
            '5': {'1': b'abc', '2': b'def'},
            '6': {'1': b'ghi'},
        })


if __name__ == '__main__':
    unittest.main()
