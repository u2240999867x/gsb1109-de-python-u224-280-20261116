import json
import unittest
from unittest import mock

from imap_tools import MailMessage
from imap_tools.unsubscribe import (
    ConsentRecord,
    HeaderReference,
    OneClickRequest,
    SignatureVerification,
    SubscriptionCatalog,
    TargetKind,
    UnsubscribeTarget,
    analyze_headers,
    build_subscription_catalog,
    build_unsubscribe_report,
    header_occurrences,
)


def _raw(*header_lines, body=b'body'):
    return ('\r\n'.join(header_lines).encode() + b'\r\n\r\n' + body)


MESSAGE_ID = '<list-1@example.test>'
RECIPIENT = 'subscriber@example.net'


def _consent(**kwargs):
    values = dict(message_id=MESSAGE_ID, recipient=RECIPIENT)
    values.update(kwargs)
    return ConsentRecord(**values)


def _signed(*references, **kwargs):
    return SignatureVerification(valid=True, covered_headers=tuple(references), **kwargs)


class UnsubscribeParsingTest(unittest.TestCase):

    def test_basic_one_click_and_manual_entry(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u?token=abc>, <mailto:u@example.test>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        list_ref = HeaderReference('List-Unsubscribe', 0)
        post_ref = HeaderReference('List-Unsubscribe-Post', 0)
        report = build_unsubscribe_report(raw, [_signed(list_ref, post_ref)], [_consent()])

        self.assertTrue(report.has_executable_actions)
        self.assertEqual(len(report.plans), 1)
        plan = report.plans[0]
        self.assertEqual(plan.method, 'POST')
        self.assertEqual(plan.url, 'https://example.test/u?token=abc')
        self.assertEqual(plan.body, b'List-Unsubscribe=One-Click')
        self.assertEqual(plan.content_type, 'application/x-www-form-urlencoded')
        self.assertEqual(plan.cookies, ())
        self.assertIsNone(plan.authorization)
        self.assertEqual(dict(plan.headers), {'Content-Type': 'application/x-www-form-urlencoded'})

        kinds = {target.uri: target.kind for target in report.targets}
        self.assertEqual(kinds['https://example.test/u?token=abc'], TargetKind.ONE_CLICK_HTTPS_POST)
        self.assertEqual(kinds['mailto:u@example.test'], TargetKind.MANUAL)

    def test_no_consent_means_no_executable_action(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(
            raw, [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))],
        )
        self.assertEqual(report.plans, ())
        self.assertEqual(len(report.one_click_candidates), 1)
        candidate = report.one_click_candidates[0]
        self.assertTrue(candidate.covered)
        self.assertFalse(candidate.consent)
        self.assertIn('consent', candidate.reason)
        self.assertIn('executable one-click plans: none', report.readable())

    def test_consent_is_bound_to_message_and_recipient(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<a@example.test>',
            ),
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<b@example.test>',
            ),
        ]
        wrong_message = build_unsubscribe_report(raw, evidence, [_consent(message_id='<other@example.test>')])
        wrong_recipient = build_unsubscribe_report(raw, evidence, [_consent(recipient='other@example.net')])
        self.assertEqual(wrong_message.plans, ())
        self.assertEqual(wrong_recipient.plans, ())

    def test_explicit_denial_wins_over_grant(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))]
        report = build_unsubscribe_report(raw, evidence, [_consent(), _consent(granted=False)])
        self.assertEqual(report.plans, ())
        self.assertIn('denied', report.one_click_candidates[0].reason)

    def test_consent_for_specific_url_does_not_authorize_another_url(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))]
        report = build_unsubscribe_report(
            raw, evidence, [_consent(target_url='https://example.test/other')],
        )
        self.assertEqual(report.plans, ())

    def test_https_without_post_header_is_manual(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
        )
        report = build_unsubscribe_report(raw, [_signed(HeaderReference('List-Unsubscribe'))], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.MANUAL)
        self.assertIn('without List-Unsubscribe-Post', report.targets[0].reason)

    def test_post_header_with_wrong_value_is_insufficient_evidence(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=Something-Else',
        )
        report = build_unsubscribe_report(
            raw,
            [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))],
            [_consent()],
        )
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_duplicate_occurrences_keep_separate_identity(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://good.example/u>',
            'List-Unsubscribe: <https://evil.example/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        occurrences = header_occurrences(raw)
        list_values = [item for item in occurrences if item.name.lower() == 'list-unsubscribe']
        self.assertEqual([item.occurrence for item in list_values], [0, 1])
        self.assertEqual(
            [item.raw_value for item in list_values],
            ['<https://good.example/u>', '<https://evil.example/u>'],
        )

        # The signature covers the first occurrence only.
        report = build_unsubscribe_report(
            raw,
            [_signed(HeaderReference('List-Unsubscribe', 0), HeaderReference('List-Unsubscribe-Post', 0))],
            [_consent()],
        )
        by_uri = {target.uri: target for target in report.targets}
        self.assertEqual(by_uri['https://good.example/u'].kind, TargetKind.ONE_CLICK_HTTPS_POST)
        self.assertIsNotNone(by_uri['https://good.example/u'].plan)
        self.assertEqual(by_uri['https://evil.example/u'].kind, TargetKind.INSUFFICIENT_EVIDENCE)
        self.assertFalse(by_uri['https://evil.example/u'].covered)
        self.assertIsNone(by_uri['https://evil.example/u'].plan)
        self.assertEqual([plan.url for plan in report.plans], ['https://good.example/u'])

    def test_duplicate_covered_in_separate_signatures_does_not_establish_one_click(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(HeaderReference('List-Unsubscribe')),
            _signed(HeaderReference('List-Unsubscribe-Post')),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)
        self.assertIn('same valid signature', report.targets[0].reason)

    def test_invalid_signature_contributes_no_coverage(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            SignatureVerification(
                valid=False,
                covered_headers=(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
            ),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_signature_for_another_message_contributes_no_coverage(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<other@example.test>',
            ),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_message_id_is_binding_and_not_case_folded(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))]
        report = build_unsubscribe_report(
            raw, evidence, [_consent(message_id=MESSAGE_ID.upper())],
        )
        self.assertEqual(report.plans, ())

    def test_bare_header_name_is_ambiguous_and_never_grants_coverage(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        verification = SignatureVerification(
            valid=True,
            covered_headers=('List-Unsubscribe', 'List-Unsubscribe-Post'),
            message_id=MESSAGE_ID,
        )
        self.assertEqual(verification.covered_headers, ())
        self.assertEqual(
            verification.ambiguous_headers,
            ('List-Unsubscribe', 'List-Unsubscribe-Post'),
        )
        report = build_unsubscribe_report(raw, [verification], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_folded_value_retains_occurrence_identity_and_parses(self):
        raw = (
            b'Message-ID: <fold@example.test>\r\n'
            b'To: subscriber@example.net\r\n'
            b'List-Unsubscribe: <https://example.test/u?a=1>,\r\n'
            b'\t<mailto:u@example.test>\r\n'
            b'List-Unsubscribe-Post:\r\n List-Unsubscribe=One-Click\r\n'
            b'\r\nbody'
        )
        occurrences = header_occurrences(raw)
        declaration = [item for item in occurrences if item.name.lower() == 'list-unsubscribe'][0]
        self.assertIn('\r\n', declaration.raw_value)
        self.assertEqual(declaration.reference, HeaderReference('List-Unsubscribe', 0))

        report = build_unsubscribe_report(
            raw,
            [_signed(HeaderReference('List-Unsubscribe', 0), HeaderReference('List-Unsubscribe-Post', 0))],
            [_consent(message_id='<fold@example.test>')],
        )
        self.assertEqual([plan.url for plan in report.plans], ['https://example.test/u?a=1'])
        self.assertEqual(
            {target.uri: target.kind for target in report.targets}['mailto:u@example.test'],
            TargetKind.MANUAL,
        )

    def test_case_insensitive_header_names(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'list-unsubscribe: <https://example.test/u>',
            'LIST-UNSUBSCRIBE-POST: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(
            raw,
            [_signed(('List-Unsubscribe', 0), ('list-unsubscribe-post', 0))],
            [_consent()],
        )
        self.assertEqual([plan.url for plan in report.plans], ['https://example.test/u'])

    def test_one_click_token_is_case_sensitive(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: list-unsubscribe=one-click',
        )
        report = build_unsubscribe_report(
            raw,
            [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))],
            [_consent()],
        )
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_mail_message_input(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>, <mailto:u@example.test>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        message = MailMessage.from_bytes(raw)
        report = build_unsubscribe_report(
            message,
            [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))],
            [_consent()],
        )
        self.assertEqual([plan.url for plan in report.plans], ['https://example.test/u'])

    def test_mapping_and_pair_inputs(self):
        headers = {
            'Message-ID': MESSAGE_ID,
            'To': RECIPIENT,
            'List-Unsubscribe': ['<https://example.test/u>', '<https://example.test/v>'],
            'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
        }
        report = analyze_headers(
            headers,
            [_signed(HeaderReference('List-Unsubscribe', 0), HeaderReference('List-Unsubscribe-Post', 0))],
            [_consent()],
        )
        self.assertEqual([plan.url for plan in report.plans], ['https://example.test/u'])
        self.assertEqual(report.targets[1].kind, TargetKind.INSUFFICIENT_EVIDENCE)

    def test_parse_failure_keeps_readable_declaration(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(
            raw,
            [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))],
            [_consent()],
        )
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.UNPARSED)
        self.assertIn('<https://example.test/u', report.targets[0].raw_text)
        self.assertIn('unterminated', report.declarations[0].errors[0])
        self.assertIn('List-Unsubscribe[0]', report.readable())
        self.assertIn('<https://example.test/u', report.readable())

    def test_empty_declaration_is_readable_and_never_executable(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe:',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.UNPARSED)
        self.assertIn('empty List-Unsubscribe value', report.declarations[0].errors)

    def test_malformed_list_blocks_one_click_but_keeps_valid_manual_target(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>, <mailto:u@example.test',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        kinds = {(target.uri, target.kind) for target in report.targets}
        self.assertIn(('https://example.test/u', TargetKind.INSUFFICIENT_EVIDENCE), kinds)
        self.assertIn((None, TargetKind.UNPARSED), kinds)
        self.assertIn('<mailto:u@example.test', report.readable())

    def test_unsupported_scheme_is_manual(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <ftp://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(report.targets[0].kind, TargetKind.UNPARSED)
        self.assertIn('ftp://example.test/u', report.targets[0].raw_text)
        self.assertIn('unsupported URI scheme', report.declarations[0].errors[0])

    def test_https_with_userinfo_or_missing_host_is_not_executable(self):
        for uri in ('https://user:pass@example.test/u', 'https:///u'):
            raw = _raw(
                f'Message-ID: {MESSAGE_ID}',
                f'To: {RECIPIENT}',
                f'List-Unsubscribe: <{uri}>',
                'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
            )
            report = build_unsubscribe_report(raw, [
                _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
            ], [_consent()])
            self.assertEqual(report.plans, ())
            self.assertNotEqual(report.targets[0].kind, TargetKind.ONE_CLICK_HTTPS_POST)

    def test_multiple_https_targets_are_ambiguous(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/a>, <https://example.test/b>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertEqual(
            {target.kind for target in report.targets},
            {TargetKind.INSUFFICIENT_EVIDENCE},
        )

    def test_multiple_post_fields_are_ambiguous(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(
                HeaderReference('List-Unsubscribe'),
                HeaderReference('List-Unsubscribe-Post', 0),
                HeaderReference('List-Unsubscribe-Post', 1),
            ),
        ], [_consent()])
        self.assertEqual(report.plans, ())
        self.assertIn('multiple', report.targets[0].reason)

    def test_ambiguous_message_id_requires_explicit_identity(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            'Message-ID: <list-2@example.test>',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))]
        without_identity = build_unsubscribe_report(raw, evidence, [_consent()])
        self.assertEqual(without_identity.plans, ())
        with_identity = build_unsubscribe_report(
            raw, evidence, [_consent()], message_id=MESSAGE_ID,
        )
        self.assertEqual([plan.url for plan in with_identity.plans], ['https://example.test/u'])

    def test_no_unsubscribe_declaration(self):
        report = build_unsubscribe_report(_raw(f'Message-ID: {MESSAGE_ID}'))
        self.assertEqual(report.declarations, ())
        self.assertEqual(report.plans, ())
        self.assertIn('No List-Unsubscribe declaration found.', report.readable())

    def test_duplicate_to_recipients_are_all_binding(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'To: second@example.net',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [_signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'))]
        report = build_unsubscribe_report(raw, evidence, [_consent(recipient='second@example.net')])
        self.assertEqual(report.recipients, (RECIPIENT, 'second@example.net'))
        self.assertEqual([plan.url for plan in report.plans], ['https://example.test/u'])

    def test_catalog_aggregates_reports(self):
        first = _raw(
            'Message-ID: <a@example.test>',
            'To: subscriber@example.net',
            'List-Unsubscribe: <https://example.test/a>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        second = _raw(
            'Message-ID: <b@example.test>',
            'To: subscriber@example.net',
            'List-Unsubscribe: <mailto:u@example.test>',
        )
        evidence = [
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<a@example.test>',
            ),
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<b@example.test>',
            ),
        ]
        consents = [
            _consent(message_id='<a@example.test>'),
            _consent(message_id='<b@example.test>'),
        ]
        catalog = build_subscription_catalog([first, second], evidence, consents)
        self.assertIsInstance(catalog, SubscriptionCatalog)
        self.assertEqual(len(catalog), 2)
        self.assertEqual([plan.url for plan in catalog.plans], ['https://example.test/a'])
        self.assertEqual(len(catalog.manual_targets), 1)

    def test_catalog_rejects_unbound_evidence_for_multiple_messages(self):
        messages = [
            _raw('Message-ID: <a@example.test>', 'List-Unsubscribe: <https://example.test/a>'),
            _raw('Message-ID: <b@example.test>', 'List-Unsubscribe: <https://example.test/b>'),
        ]
        with self.assertRaises(ValueError):
            build_subscription_catalog(messages, [_signed(HeaderReference('List-Unsubscribe'))])

    def test_catalog_consumes_consent_generator_once_per_message(self):
        first = _raw(
            'Message-ID: <a@example.test>',
            'To: subscriber@example.net',
            'List-Unsubscribe: <https://example.test/a>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        second = _raw(
            'Message-ID: <b@example.test>',
            'To: subscriber@example.net',
            'List-Unsubscribe: <https://example.test/b>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        evidence = [
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<a@example.test>',
            ),
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id='<b@example.test>',
            ),
        ]
        consents = (
            record for record in (
                _consent(message_id='<a@example.test>'),
                _consent(message_id='<b@example.test>'),
            )
        )
        catalog = build_subscription_catalog([first, second], evidence, consents)
        self.assertEqual(
            [plan.url for plan in catalog.plans],
            ['https://example.test/a', 'https://example.test/b'],
        )

    def test_header_reference_identity_is_case_insensitive(self):
        self.assertEqual(
            HeaderReference('List-Unsubscribe', 0),
            HeaderReference('list-unsubscribe', 0),
        )
        self.assertEqual(
            HeaderReference('List-Unsubscribe', 0).key,
            HeaderReference('LIST-UNSUBSCRIBE', 0).key,
        )
        self.assertNotEqual(
            HeaderReference('List-Unsubscribe', 0),
            HeaderReference('List-Unsubscribe', 1),
        )


class UnsubscribeSafetyTest(unittest.TestCase):

    def test_plan_rejects_non_https_url(self):
        for url in ('http://example.test/u', 'mailto:u@example.test', 'https:///u', 'https://u:p@example.test/u'):
            with self.assertRaises(ValueError):
                OneClickRequest(url=url)

    def test_plan_rejects_protocol_deviations(self):
        with self.assertRaises(ValueError):
            OneClickRequest(url='https://example.test/u', method='GET')
        with self.assertRaises(ValueError):
            OneClickRequest(url='https://example.test/u', body=b'other')
        with self.assertRaises(ValueError):
            OneClickRequest(url='https://example.test/u', content_type='text/plain')

    def test_plan_contains_no_authentication_context(self):
        plan = OneClickRequest(url='https://example.test/u')
        self.assertEqual(plan.cookies, ())
        self.assertIsNone(plan.authorization)
        self.assertEqual(set(plan.headers), {'Content-Type'})
        self.assertEqual(plan.as_dict()['cookies'], ())
        self.assertIsNone(plan.as_dict()['authorization'])

    def test_analysis_makes_no_network_requests(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://unreachable.invalid/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        with mock.patch('socket.create_connection', side_effect=AssertionError('network used')), \
                mock.patch('socket.socket', side_effect=AssertionError('socket used')), \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('network used')):
            report = build_unsubscribe_report(raw, [
                _signed(HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post')),
            ], [_consent()])
        self.assertEqual(len(report.one_click_candidates), 1)
        self.assertEqual(report.one_click_candidates[0].uri, 'https://unreachable.invalid/u')

    def test_readable_output_marks_unparsed_declaration(self):
        target = UnsubscribeTarget(
            kind=TargetKind.UNPARSED,
            header=HeaderReference('List-Unsubscribe', 0),
            raw_text='not a URI',
            reason='could not parse',
        )
        self.assertFalse(target.executable)
        self.assertIsInstance(target.raw_text, str)

    def test_report_dict_is_json_serializable_and_has_no_auth_context(self):
        raw = _raw(
            f'Message-ID: {MESSAGE_ID}',
            f'To: {RECIPIENT}',
            'List-Unsubscribe: <https://example.test/u>',
            'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        )
        report = build_unsubscribe_report(raw, [
            _signed(
                HeaderReference('List-Unsubscribe'), HeaderReference('List-Unsubscribe-Post'),
                message_id=MESSAGE_ID,
            ),
        ], [_consent()])
        payload = json.dumps(report.to_dict())
        self.assertIn('List-Unsubscribe=One-Click', payload)
        self.assertIn('"cookies": []', payload)
        self.assertIn('"authorization": null', payload)
        self.assertNotIn('Cookie', payload)


if __name__ == '__main__':
    unittest.main()
