import unittest

from imap_tools import (
    ContactRole,
    DeliveryStatus,
    EntryKind,
    MailMessage,
    build_contact_roles,
    check_delivery,
    parse_envelope,
    parse_header_addresses,
)


class ParseHeaderAddressesTest(unittest.TestCase):

    def test_display_names_and_order_preserved(self):
        result = parse_header_addresses('"Alice" <alice@x.com>, Bob <bob@x.com>, carol@x.com')
        self.assertEqual(
            [(e.name, e.email) for e in result.entries],
            [('Alice', 'alice@x.com'), ('Bob', 'bob@x.com'), ('', 'carol@x.com')]
        )
        self.assertEqual([e.order for e in result.entries], [0, 1, 2])

    def test_duplicates_are_kept(self):
        result = parse_header_addresses('a@x.com, a@x.com, "Dup" <a@x.com>')
        self.assertEqual([e.email for e in result.entries], ['a@x.com', 'a@x.com', 'a@x.com'])
        self.assertEqual(result.entries[2].name, 'Dup')

    def test_address_group_members_and_metadata_preserved(self):
        result = parse_header_addresses('Team: a@x.com, b@x.com;, c@y.com')
        self.assertEqual([e.email for e in result.entries], ['a@x.com', 'b@x.com', 'c@y.com'])
        self.assertEqual([e.group for e in result.entries], ['Team', 'Team', None])
        group = result.groups[0]
        self.assertEqual(group.name, 'Team')
        self.assertTrue(group.closed)
        self.assertEqual([m.email for m in group.members], ['a@x.com', 'b@x.com'])

    def test_empty_group_kept_without_members(self):
        result = parse_header_addresses('undisclosed recipients: ;')
        self.assertEqual(result.entries, ())
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].name, 'undisclosed recipients')
        self.assertEqual(result.groups[0].members, [])
        self.assertTrue(result.groups[0].closed)

    def test_group_name_looking_like_email_is_not_a_contact(self):
        result = parse_header_addresses('team@x.com: ;, real@x.com')
        self.assertEqual([e.email for e in result.entries], ['real@x.com'])
        self.assertEqual(result.groups[0].name, 'team@x.com')

    def test_group_without_closing_semicolon_marked_open(self):
        result = parse_header_addresses('Team: a@x.com, b@x.com')
        self.assertFalse(result.groups[0].closed)
        self.assertEqual([e.group for e in result.entries], ['Team', 'Team'])

    def test_separators_inside_quotes_and_angles_kept(self):
        result = parse_header_addresses('"Last, First" <lf@x.com>, "a;b" <ab@y.com>, N <"a,b"@z.com>')
        self.assertEqual([e.email for e in result.entries], ['lf@x.com', 'ab@y.com', '"a,b"@z.com'])
        self.assertEqual(result.entries[0].name, 'Last, First')

    def test_broken_comment_is_isolated_and_later_addresses_survive(self):
        result = parse_header_addresses('Broken (unclosed <bad@x.com>, "Bob" <bob@y.com>, carol@z.com')
        self.assertEqual([e.email for e in result.entries if e.valid], ['bad@x.com', 'bob@y.com', 'carol@z.com'])
        damaged = result.entries[0]
        self.assertIn('unclosed_comment', damaged.defects)
        self.assertIn('bad@x.com', damaged.raw)

    def test_broken_quote_does_not_swallow_following_addresses(self):
        result = parse_header_addresses('"unclosed quote <a@x.com>, b@y.com, c@z.com')
        self.assertEqual(result.entries[0].kind, EntryKind.BROKEN)
        self.assertIn('unclosed_quote', result.entries[0].defects)
        self.assertEqual([e.email for e in result.entries if e.valid], ['b@y.com', 'c@z.com'])

    def test_quarantined_fragment_keeps_raw_text(self):
        result = parse_header_addresses('<Undisclosed-Recipient:@mailman.enron.com;>, ok@x.com')
        broken = result.entries[0]
        self.assertEqual(broken.kind, EntryKind.BROKEN)
        self.assertIn('Undisclosed-Recipient', broken.raw)
        self.assertEqual([e.email for e in result.entries if e.valid], ['ok@x.com'])

    def test_bad_encoded_word_flagged_but_address_kept(self):
        result = parse_header_addresses('=?BAD?Q?=zz?= <a@x.com>, b@y.com')
        self.assertEqual([e.email for e in result.entries], ['a@x.com', 'b@y.com'])
        self.assertEqual(result.entries[0].defects, ('bad_encoded_word',))

    def test_bare_word_is_name_only_not_a_contact(self):
        result = parse_header_addresses('Array, a@x.com')
        self.assertEqual(result.entries[0].kind, EntryKind.NAME_ONLY)
        self.assertEqual(result.entries[0].name, 'Array')
        self.assertEqual(result.entries[0].email, '')
        self.assertEqual(result.entries[1].email, 'a@x.com')

    def test_none_and_empty_input(self):
        self.assertEqual(parse_header_addresses(None).entries, ())
        self.assertEqual(parse_header_addresses('').entries, ())


class ContactRolesTest(unittest.TestCase):

    def test_roles_headers_and_global_order(self):
        records = build_contact_roles([
            ('From', 'Boss <boss@x.com>'),
            ('To', 'a@x.com, b@x.com'),
            ('To', 'a@x.com'),
            ('Cc', 'a@x.com, c@x.com'),
            ('Bcc', 'secret@hidden.com'),
            ('Subject', 'ignored'),
        ])
        self.assertEqual([r.order for r in records], list(range(len(records))))
        self.assertEqual([r.role for r in records].count(ContactRole.TO), 3)
        same_address = [r.role for r in records if r.email == 'a@x.com']
        self.assertEqual(same_address, [ContactRole.TO, ContactRole.TO, ContactRole.CC])

    def test_duplicate_header_indexes_preserved(self):
        records = build_contact_roles([('To', 'a@x.com'), ('To', 'a@x.com, b@x.com')])
        self.assertEqual([r.header_index for r in records], [0, 1, 1])

    def test_mapping_input_with_header_lists(self):
        records = build_contact_roles({'to': ['a@x.com', 'b@x.com'], 'cc': 'c@x.com'})
        self.assertEqual([(r.role.value, r.email) for r in records],
                         [('to', 'a@x.com'), ('to', 'b@x.com'), ('cc', 'c@x.com')])

    def test_header_names_case_insensitive(self):
        records = build_contact_roles([('FROM', 'a@x.com'), ('CC', 'b@x.com')])
        self.assertEqual([r.role for r in records], [ContactRole.FROM, ContactRole.CC])

    def test_empty_input(self):
        self.assertEqual(build_contact_roles([]), ())


class EnvelopeCheckTest(unittest.TestCase):

    def setUp(self):
        self.records = build_contact_roles([
            ('From', 'boss@x.com'),
            ('To', 'a@x.com, b@x.com'),
            ('Cc', 'c@x.com'),
            ('Bcc', 'secret@hidden.com'),
        ])

    def test_partial_envelope_unknown_is_not_not_delivered(self):
        report = check_delivery(self.records, parse_envelope(['a@x.com', 'b@x.com']))
        cc_record = next(r for r in self.records if r.email == 'c@x.com')
        self.assertEqual(report.status_for(cc_record), DeliveryStatus.UNKNOWN)
        self.assertEqual(report.not_delivered, ())
        self.assertEqual(len(report.delivered), 2)

    def test_complete_envelope_marks_absence_not_delivered(self):
        envelope = parse_envelope(['a@x.com', 'b@x.com'], complete=True)
        report = check_delivery(self.records, envelope)
        cc_record = next(r for r in self.records if r.email == 'c@x.com')
        bcc_record = next(r for r in self.records if r.email == 'secret@hidden.com')
        self.assertEqual(report.status_for(cc_record), DeliveryStatus.NOT_DELIVERED)
        self.assertEqual(report.status_for(bcc_record), DeliveryStatus.NOT_DELIVERED)

    def test_envelope_match_is_case_insensitive(self):
        report = check_delivery(self.records, parse_envelope(['A@X.COM']))
        self.assertEqual(report.delivered[0].email, 'a@x.com')

    def test_envelope_only_recipients_reported_as_extra(self):
        envelope = parse_envelope({'recipients': ['a@x.com', 'shadow@only-envelope.com']})
        report = check_delivery(self.records, envelope)
        self.assertEqual(report.extra_envelope_recipients, ('shadow@only-envelope.com',))
        # A BCC address present in the envelope is delivered, not extra.
        report2 = check_delivery(self.records, parse_envelope(['secret@hidden.com']))
        self.assertEqual(report2.extra_envelope_recipients, ())
        self.assertEqual(report2.delivered[0].email, 'secret@hidden.com')

    def test_mail_from_match(self):
        envelope = parse_envelope({'mail_from': 'boss@x.com', 'recipients': ['a@x.com']})
        report = check_delivery(self.records, envelope)
        self.assertTrue(report.mail_from_matches)

    def test_broken_record_without_address_stays_unknown(self):
        records = build_contact_roles([('To', '"unclosed <a@x.com>, b@y.com')])
        report = check_delivery(records, parse_envelope(['b@y.com'], complete=True))
        broken = next(r for r in records if r.kind is EntryKind.BROKEN)
        self.assertEqual(report.status_for(broken), DeliveryStatus.UNKNOWN)

    def test_envelope_input_formats(self):
        self.assertEqual(parse_envelope('a@x.com, b@x.com').recipient_emails, ('a@x.com', 'b@x.com'))
        envelope = parse_envelope({'from': 'f@x.com', 'to': [{'name': 'A', 'email': 'a@x.com'}]})
        self.assertEqual(envelope.mail_from, 'f@x.com')
        self.assertEqual(envelope.recipient_emails, ('a@x.com',))
        self.assertFalse(envelope.complete)


class MailMessageIntegrationTest(unittest.TestCase):

    def test_contact_roles_and_delivery_check(self):
        eml = (b'From: Boss <boss@x.com>\r\n'
               b'To: a@x.com\r\n'
               b'Cc: c@x.com\r\n'
               b'Subject: s\r\n\r\nbody')
        message = MailMessage.from_bytes(eml)
        self.assertEqual([r.email for r in message.contact_roles], ['boss@x.com', 'a@x.com', 'c@x.com'])
        report = message.check_delivery(['a@x.com'])
        self.assertEqual(len(report.delivered), 1)
        self.assertEqual(len(report.unknown), 2)
        self.assertEqual(report.not_delivered, ())

    def test_group_header_on_real_message(self):
        with open('tests/messages/error_emails/empty_group_lists.eml', 'rb') as f:
            message = MailMessage.from_bytes(f.read())
        to_records = [r for r in message.contact_roles if r.role is ContactRole.TO]
        self.assertEqual(to_records, [])


if __name__ == '__main__':
    unittest.main()
