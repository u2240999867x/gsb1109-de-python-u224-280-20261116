import base64
import unittest

from imap_tools import InnerMessage, MailMessage, parse_inner_messages, parse_text_summary


class DigestTest(unittest.TestCase):
    def test_rfc822_inner(self):
        raw = (
            b"From: outer@x.com\r\nSubject: outer\r\n"
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n"
            b"--b\r\nContent-Type: message/rfc822\r\n\r\n"
            b"From: inner@x.com\r\nMessage-ID: <i@x>\r\nSubject: inner\r\n\r\nbody\r\n"
            b"--b--\r\n"
        )
        message = MailMessage.from_bytes(raw)
        inners = message.inner_messages()
        self.assertEqual(len(inners), 1)
        self.assertEqual(inners[0].message['From'], 'inner@x.com')
        self.assertEqual(inners[0].message['Message-ID'], '<i@x>')
        self.assertEqual(inners[0].origin, 'message/rfc822')
        self.assertEqual(inners[0].index, 0)
        # outer delivery message is preserved
        self.assertEqual(message.from_, 'outer@x.com')
        self.assertEqual(message.subject, 'outer')

    def test_digest_children(self):
        raw = (
            b"From: outer@x.com\r\nDate: Tue, 03 Jan 2017 22:26:59 +0500\r\n"
            b"Content-Type: multipart/digest; boundary=b\r\n\r\n"
            b"--b\r\nFrom: one@x.com\r\nMessage-ID: <1@x>\r\n\r\nfirst\r\n"
            b"--b\r\nFrom: two@x.com\r\nMessage-ID: <2@x>\r\n\r\nsecond\r\n"
            b"--b--\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages()
        self.assertEqual([i.message['From'] for i in inners], ['one@x.com', 'two@x.com'])
        self.assertEqual([i.index for i in inners], [0, 1])

    def test_missing_inner_fields_not_inherited(self):
        raw = (
            b"From: outer@x.com\r\nDate: Tue, 03 Jan 2017 22:26:59 +0500\r\n"
            b"Content-Type: multipart/digest; boundary=b\r\n\r\n"
            b"--b\r\nSubject: no sender and date\r\nMessage-ID: <x@x>\r\n\r\nbody\r\n"
            b"--b--\r\n"
        )
        message = MailMessage.from_bytes(raw)
        inners = message.inner_messages()
        self.assertEqual(len(inners), 1)
        self.assertIsNone(inners[0].message['From'])
        self.assertIsNone(inners[0].message['Date'])
        self.assertEqual(message.from_, 'outer@x.com')

    def test_text_separator(self):
        separator = '----- FORWARD -----'
        text = (
            "From: a@x.com\r\nMessage-ID: <1@x>\r\n\r\nbody A\r\n"
            + separator + "\r\n"
            "From: b@x.com\r\nMessage-ID: <2@x>\r\n\r\nbody B\r\n"
        )
        inners = parse_text_summary(text, separator)
        self.assertEqual([i.message['From'] for i in inners], ['a@x.com', 'b@x.com'])
        self.assertTrue(all(isinstance(i, InnerMessage) for i in inners))

    def test_text_separator_bytes_input(self):
        separator = '### NEXT (v2.0) ***'
        text = (
            "From: a@x.com\r\nMessage-ID: <1@x>\r\n\r\nbody A\r\n"
            + separator + "\r\n"
            "From: b@x.com\r\nMessage-ID: <2@x>\r\n\r\nbody B\r\n"
        )
        inners = parse_text_summary(text.encode(), separator.encode())
        self.assertEqual([i.message['From'] for i in inners], ['a@x.com', 'b@x.com'])

    def test_separator_in_quoted_text_does_not_split(self):
        separator = '----- FORWARD -----'
        text = (
            "From: a@x.com\r\nMessage-ID: <1@x>\r\n\r\n"
            "prefix " + separator + " suffix\r\nquoted line\r\n"
            + separator + "\r\n"
            "From: b@x.com\r\nMessage-ID: <2@x>\r\n\r\nbody B\r\n"
        )
        inners = parse_text_summary(text, separator)
        self.assertEqual(len(inners), 2)
        self.assertIn('prefix ' + separator + ' suffix', inners[0].message.get_payload())

    def test_unparseable_segment_keeps_remaining(self):
        separator = '----- FORWARD -----'
        text = (
            "From: good1@x.com\r\nMessage-ID: <1@x>\r\n\r\nbody 1\r\n"
            + separator + "\r\n"
            "plain prose without any header\r\n\r\nstill prose\r\n"
            + separator + "\r\n"
            "From: good2@x.com\r\nMessage-ID: <2@x>\r\n\r\nbody 2\r\n"
        )
        inners = parse_text_summary(text, separator)
        self.assertEqual([i.message['From'] for i in inners], ['good1@x.com', 'good2@x.com'])

    def test_duplicate_message_id_keeps_occurrences(self):
        separator = '----- FORWARD -----'
        text = (
            "From: a@x.com\r\nMessage-ID: <dup@x>\r\n\r\nfirst\r\n"
            + separator + "\r\n"
            "From: b@x.com\r\nMessage-ID: <dup@x>\r\n\r\nsecond\r\n"
        )
        inners = parse_text_summary(text, separator)
        self.assertEqual(len(inners), 2)
        self.assertEqual([i.message['Message-ID'] for i in inners], ['<dup@x>', '<dup@x>'])
        self.assertEqual([i.index for i in inners], [0, 1])

    def test_digest_skips_broken_child_keeps_rest(self):
        raw = (
            b"From: outer@x.com\r\nContent-Type: multipart/digest; boundary=b\r\n\r\n"
            b"--b\r\nFrom: good1@x.com\r\nMessage-ID: <1@x>\r\n\r\none\r\n"
            b"--b\r\nbroken prose, no header lines\r\nmore prose\r\n"
            b"--b\r\nFrom: good2@x.com\r\nMessage-ID: <2@x>\r\n\r\ntwo\r\n"
            b"--b--\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages()
        self.assertEqual([i.message['From'] for i in inners], ['good1@x.com', 'good2@x.com'])

    def test_nested_rfc822(self):
        raw = (
            b"Content-Type: message/rfc822\r\n\r\n"
            b"From: middle@x.com\r\nMessage-ID: <m@x>\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            b"From: deep@x.com\r\nMessage-ID: <d@x>\r\n\r\ndeep body\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages()
        self.assertEqual([i.message['From'] for i in inners], ['middle@x.com', 'deep@x.com'])

    def test_base64_encoded_rfc822(self):
        inner = b"From: enc@x.com\r\nMessage-ID: <e@x>\r\n\r\nenc body\r\n"
        raw = (
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(inner) + b"\r\n--b--\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages()
        self.assertEqual(len(inners), 1)
        self.assertEqual(inners[0].message['From'], 'enc@x.com')

    def test_broken_base64_rfc822_is_skipped(self):
        raw = (
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            b"@@not base64@@\r\n--b--\r\n"
        )
        self.assertEqual(MailMessage.from_bytes(raw).inner_messages(), [])

    def test_text_separator_inside_message_part(self):
        separator = '----- FORWARD -----'
        text = (
            "From: a@x.com\r\nMessage-ID: <1@x>\r\n\r\nbody A\r\n"
            + separator + "\r\n"
            "From: b@x.com\r\nMessage-ID: <2@x>\r\n\r\nbody B\r\n"
        ).encode()
        raw = (
            b"From: outer@x.com\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
            b"--b\r\nContent-Type: text/plain\r\n\r\n" + text + b"\r\n--b--\r\n"
        )
        message = MailMessage.from_bytes(raw)
        self.assertEqual([i.message['From'] for i in message.inner_messages(separator)], ['a@x.com', 'b@x.com'])
        # without a separator, plain text is not treated as inner messages
        self.assertEqual(message.inner_messages(), [])

    def test_separator_substring_does_not_split(self):
        separator = '### NEXT ###'
        text = "From: a@x.com\r\n\r\nline with " + separator + " inside\r\n"
        inners = parse_text_summary(text, separator)
        self.assertEqual(len(inners), 1)
        self.assertIn(separator, inners[0].message.get_payload())

    def test_encapsulated_text_is_not_split(self):
        separator = '----- FORWARD -----'
        inner = ("From: in@x.com\r\nMessage-ID: <in@x>\r\n\r\n" + separator + "\r\nquoted\r\n").encode()
        raw = (
            b"From: outer@x.com\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--b--\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages(text_separator=separator)
        self.assertEqual(len(inners), 1)
        self.assertEqual(inners[0].message['From'], 'in@x.com')
        self.assertIn(separator, inners[0].message.get_payload())

    def test_empty_digest(self):
        raw = b"Content-Type: multipart/digest; boundary=b\r\n\r\npreamble\r\n--b--\r\n"
        self.assertEqual(parse_inner_messages(MailMessage.from_bytes(raw).obj), [])

    def test_inner_message_can_be_multipart(self):
        inner = (
            b"From: m@x.com\r\nMessage-ID: <m@x>\r\n"
            b"Content-Type: multipart/alternative; boundary=c\r\n\r\n"
            b"--c\r\nContent-Type: text/plain\r\n\r\nplain\r\n"
            b"--c\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n"
            b"--c--\r\n"
        )
        raw = (
            b"From: outer@x.com\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--b--\r\n"
        )
        inners = MailMessage.from_bytes(raw).inner_messages()
        self.assertEqual(len(inners), 1)
        self.assertEqual(inners[0].message.get_content_type(), 'multipart/alternative')
        self.assertEqual(inners[0].message['From'], 'm@x.com')

    def test_broken_multipart_chunk_is_skipped(self):
        separator = '----- FORWARD -----'
        broken = "Content-Type: multipart/mixed; boundary=b\n\nbody without any boundary\n"
        good = "From: good@x.com\r\nMessage-ID: <g@x>\r\n\r\nbody\r\n"
        text = broken + "\n" + separator + "\n" + good
        inners = parse_text_summary(text, separator)
        self.assertEqual([i.message['From'] for i in inners], ['good@x.com'])

    def test_outer_message_not_modified(self):
        raw = (
            b"From: outer@x.com\r\nSubject: keep\r\n"
            b"Content-Type: multipart/digest; boundary=b\r\n\r\n"
            b"--b\r\nFrom: inner@x.com\r\nMessage-ID: <i@x>\r\n\r\nbody\r\n--b--\r\n"
        )
        message = MailMessage.from_bytes(raw)
        before = bytes(message.obj)
        message.inner_messages()
        self.assertEqual(bytes(message.obj), before)
        self.assertEqual(message.subject, 'keep')


if __name__ == "__main__":
    unittest.main()
