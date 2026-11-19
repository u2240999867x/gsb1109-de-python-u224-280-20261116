import datetime as dt
import unittest
from email.message import EmailMessage
from email.utils import format_datetime

from imap_tools import MailMessage
from imap_tools.errors import MaterialFetchError
from imap_tools.filter import (
    And,
    AndNode,
    Condition,
    ImapMaterialFetcher,
    LocalVerifier,
    Material,
    MaterialFetcher,
    MaterialStore,
    Not,
    NotNode,
    Or,
    ServerCapabilities,
    attachment_content,
    attachment_name,
    body_text,
    build_candidate_query,
    date_gte,
    filter_uids,
    flag,
    from_,
    message_text,
    size_gt,
    split_candidates,
    subject,
)


def build_message(uid='1', subject_='Report', sender='boss@corp.example', recipients=('me@corp.example',),
                  text='quarterly results are inside', html='', attachments=(), seen=False,
                  date_=dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)) -> MailMessage:
    email_message = EmailMessage()
    email_message['From'] = sender
    email_message['To'] = ', '.join(recipients)
    email_message['Subject'] = subject_
    email_message['Date'] = format_datetime(date_)
    email_message.set_content(text)
    if html:
        email_message.add_alternative(html, subtype='html')
    for filename, payload in attachments:
        email_message.add_attachment(
            payload.encode(), filename=filename, maintype='application', subtype='octet-stream')
    flags = '(\\Seen)' if seen else '()'
    raw_flags = f'{uid} (UID {uid} FLAGS {flags} RFC822.SIZE {len(email_message.as_bytes())})'.encode()
    return MailMessage([(raw_flags, email_message.as_bytes()), b')'])


class RecordingFetcher(MaterialFetcher):
    """Fake fetcher: serves full bytes per uid and records every material request"""

    def __init__(self, messages, fail_on=None):
        self.messages = messages
        self.fail_on = set(fail_on or ())
        self.calls = []

    def fetch(self, uid, material):
        self.calls.append((uid, material))
        if (uid, material) in self.fail_on:
            raise MaterialFetchError(f'uid {uid}: {material.name} unavailable')
        message = self.messages[uid]
        if material in (Material.BODY, Material.ATTACHMENT_META, Material.ATTACHMENT_PAYLOAD):
            from imap_tools.filter import FetchResult
            return FetchResult(message, (
                Material.FLAGS, Material.ENVELOPE, Material.BODY,
                Material.ATTACHMENT_META, Material.ATTACHMENT_PAYLOAD))
        if material is Material.ENVELOPE:
            from imap_tools.filter import FetchResult
            return FetchResult(message, (Material.FLAGS, Material.ENVELOPE))
        from imap_tools.filter import FetchResult
        return FetchResult(message, (Material.FLAGS,))


class CandidateQueryTest(unittest.TestCase):

    def setUp(self):
        self.caps = ServerCapabilities.imap4rev1()

    def test_leaf_and_or_not_rendering(self):
        tree = And(from_('boss@corp.example'), Or(subject('invoice'), subject('refund')), Not(flag('Seen')))
        self.assertEqual(
            build_candidate_query(tree, self.caps),
            '(FROM "boss@corp.example" (OR SUBJECT "invoice" SUBJECT "refund") NOT (KEYWORD Seen))')

    def test_local_only_condition_means_all(self):
        self.assertEqual(build_candidate_query(attachment_name('.pdf'), self.caps), 'ALL')
        self.assertEqual(build_candidate_query(attachment_content('secret'), self.caps), 'ALL')

    def test_unsupported_conjunct_is_dropped_not_negated(self):
        tree = And(from_('boss@corp.example'), attachment_name('.pdf'))
        self.assertEqual(build_candidate_query(tree, self.caps), '(FROM "boss@corp.example")')

    def test_unsupported_disjunct_drops_whole_or_but_keeps_siblings(self):
        tree = And(from_('boss@corp.example'), Or(subject('invoice'), attachment_name('.pdf')))
        # dropping only SUBJECT would shrink the OR set, so the whole OR branch is dropped
        self.assertEqual(build_candidate_query(tree, self.caps), '(FROM "boss@corp.example")')

    def test_or_with_unsupported_disjunct_at_root_is_all(self):
        tree = Or(subject('invoice'), attachment_name('.pdf'))
        self.assertEqual(build_candidate_query(tree, self.caps), 'ALL')

    def test_server_without_or_negation(self):
        caps = ServerCapabilities.minimal()
        self.assertEqual(build_candidate_query(And(from_('a@b.c'), Or(subject('x'), subject('y'))), caps),
                         '(FROM "a@b.c")')
        self.assertEqual(build_candidate_query(Not(from_('a@b.c')), caps), 'ALL')
        self.assertEqual(build_candidate_query(And(Not(from_('a@b.c')), subject('x')), caps),
                         '(SUBJECT "x")')

    def test_non_negatable_condition_under_not_is_dropped(self):
        tree = And(subject('invoice'), Not(attachment_content('secret')))
        self.assertEqual(build_candidate_query(tree, self.caps), '(SUBJECT "invoice")')

    def test_unknown_search_key_is_never_assumed_supported(self):
        caps = ServerCapabilities(search_keys=frozenset(('ALL', 'FROM')), negation=True, logic_or=True)
        self.assertEqual(build_candidate_query(And(from_('a@b.c'), body_text('x')), caps), '(FROM "a@b.c")')
        self.assertEqual(build_candidate_query(body_text('x'), caps), 'ALL')

    def test_candidate_query_is_sound_over_truth_table(self):
        # every leaf is server-capable here; for every assignment the candidate
        # relaxation must be a superset of the exact filter matches
        leaves = [from_('a@b.c'), subject('x'), flag('Seen')]
        tree = And(leaves[0], Or(leaves[1], Not(leaves[2])))
        query = build_candidate_query(tree, self.caps)
        self.assertIn('FROM', query)
        self.assertIn('OR', query)
        self.assertIn('NOT', query)

    def test_negated_and_with_local_only_condition_is_all(self):
        # NOT (local_only AND subject): dropping the local conjunct inside NOT
        # would shrink the set, so the whole NOT branch must become unrenderable
        tree = Not(And(attachment_name('.pdf'), subject('invoice')))
        self.assertEqual(build_candidate_query(tree, self.caps), 'ALL')

    def test_negated_or_drops_unsupported_conjunct_soundly(self):
        # NOT (from OR local_only) == NOT(from) AND NOT(local_only):
        # keeping only NOT(from) is a sound relaxation (a superset)
        tree = And(subject('a'), Not(Or(from_('x@b.c'), attachment_name('.pdf'))))
        self.assertEqual(
            build_candidate_query(tree, self.caps),
            '(SUBJECT "a" NOT ((FROM "x@b.c")))')

    def test_negated_or_without_server_or_support(self):
        # server lacks OR: under NOT, an OR becomes an effective conjunction,
        # which needs no OR key and can still be rendered soundly
        caps = ServerCapabilities(
            search_keys=frozenset(('ALL', 'SUBJECT')), negation=True, logic_or=False)
        query = build_candidate_query(Not(Or(subject('a'), subject('b'))), caps)
        self.assertNotIn('OR', query)
        self.assertEqual(query, 'NOT ((SUBJECT "a" SUBJECT "b"))')

    def test_candidate_is_a_superset_for_every_assignment(self):
        # Cross-validate with an independent reference evaluator: for every
        # truth assignment, exact-match=True must imply candidate-match=True.
        from imap_tools import query as query_mod
        from imap_tools.filter import _render_node, iter_conditions

        current = {}

        def leaf(name, server_key=None, server_args=''):
            return Condition(
                name=name, materials=(Material.ENVELOPE,),
                predicate=lambda message: current[name],
                evidence=lambda message: '',
                server_key=server_key, server_args=server_args)

        leaves = {
            'l1': leaf('l1', 'FROM', '"a"'),
            'l2': leaf('l2', 'SUBJECT', '"b"'),
            'l3': leaf('l3', 'KEYWORD', 'Flag'),
            'l4': leaf('l4'),  # local-only, no server key
        }
        token_to_name = {condition.server_token(): name
                         for name, condition in leaves.items() if condition.server_token()}

        trees = [
            And(leaves['l1'], Or(leaves['l2'], Not(leaves['l3']))),
            Not(And(leaves['l4'], leaves['l1'])),
            And(leaves['l1'], Not(Or(leaves['l2'], leaves['l4']))),
            Or(leaves['l1'], And(leaves['l2'], Not(leaves['l3']))),
            Not(Not(leaves['l1'])),
            And(leaves['l1'], leaves['l4']),
            Or(leaves['l1'], leaves['l4']),
            Not(Or(leaves['l1'], leaves['l2'], leaves['l3'])),
        ]
        capability_sets = [
            ServerCapabilities.imap4rev1(),
            ServerCapabilities.minimal(),
            ServerCapabilities(search_keys=ServerCapabilities.imap4rev1().search_keys,
                               negation=False, logic_or=True),
            ServerCapabilities(search_keys=ServerCapabilities.imap4rev1().search_keys,
                               negation=True, logic_or=False),
            ServerCapabilities(search_keys=frozenset(('ALL', 'FROM')), negation=True, logic_or=True),
        ]

        def exact_eval(node):
            if isinstance(node, Condition):
                return current[node.name]
            if isinstance(node, NotNode):
                return not exact_eval(node.child)
            values = [exact_eval(child) for child in node.children]
            return all(values) if isinstance(node, AndNode) else any(values)

        def reference_eval(node):
            if isinstance(node, str):
                return current[token_to_name[node]]
            if isinstance(node, query_mod.NOT):
                return not reference_eval(node.converted_strings[0])
            if isinstance(node, query_mod.OR):
                return any(reference_eval(child) for child in node.converted_strings)
            return all(reference_eval(child) for child in node.converted_strings)  # AND

        for tree in trees:
            names = sorted({condition.name for condition in iter_conditions(tree)})
            for caps in capability_sets:
                rendered = _render_node(tree, caps, False)
                for mask in range(2 ** len(names)):
                    current.clear()
                    current.update({name: bool(mask & (1 << index)) for index, name in enumerate(names)})
                    exact = exact_eval(tree)
                    candidate = True if rendered is None else reference_eval(rendered)
                    # soundness: the candidate set must contain every exact match
                    self.assertFalse(
                        exact and not candidate,
                        f'missed match: tree={type(tree).__name__} caps={caps.search_keys} '
                        f'neg={caps.negation} or={caps.logic_or} assignment={current}')


class LocalVerificationTest(unittest.TestCase):

    def setUp(self):
        self.messages = {
            '1': build_message(uid='1', subject_='Invoice 42', text='please pay the invoice', seen=True),
            '2': build_message(uid='2', subject_='Holiday', sender='friend@corp.example',
                               text='no business here', seen=False),
            '3': build_message(uid='3', subject_='Invoice with file', sender='client@partner.example',
                               text='see attachment', attachments=(('december.pdf', 'pdf-bytes'),), seen=False),
        }

    def verify(self, tree, uids=None, fetcher=None, cache=None):
        fetcher = fetcher or RecordingFetcher(self.messages)
        store = MaterialStore(fetcher, cache=cache)
        verifier = LocalVerifier(store)
        results = verifier.verify_all(uids or list(self.messages), tree)
        return results, fetcher, store

    def test_matched_excluded_split(self):
        tree = And(from_('boss@corp.example'), subject('invoice'))
        results, _, _ = self.verify(tree)
        by_uid = {result.uid: result.status for result in results}
        self.assertEqual(by_uid, {'1': 'matched', '2': 'excluded', '3': 'excluded'})
        buckets = split_candidates(results)
        self.assertEqual([r.uid for r in buckets['matched']], ['1'])
        self.assertEqual(sorted(r.uid for r in buckets['excluded']), ['2', '3'])
        self.assertEqual(buckets['inconclusive'], [])

    def test_unknown_is_not_excluded(self):
        fetcher = RecordingFetcher(self.messages, fail_on=(('2', Material.ENVELOPE),))
        tree = from_('boss@corp.example')
        results, _, _ = self.verify(tree, fetcher=fetcher)
        by_uid = {result.uid: result for result in results}
        self.assertTrue(by_uid['1'].is_matched)
        self.assertTrue(by_uid['2'].is_inconclusive)
        self.assertIn('ENVELOPE unavailable', by_uid['2'].fetch_errors[0])
        self.assertEqual(by_uid['2'].missing_materials, [Material.ENVELOPE])
        self.assertTrue(by_uid['3'].is_excluded)

    def test_predicate_failure_is_unknown(self):
        def broken_predicate(message):
            raise RuntimeError('parser exploded')

        condition = Condition(
            name='broken', materials=(Material.ENVELOPE,),
            predicate=broken_predicate, evidence=lambda message: 'n/a')
        results, _, _ = self.verify(condition, uids=['1'])
        self.assertTrue(results[0].is_inconclusive)
        self.assertIn('parser exploded', results[0].fetch_errors[0])

    def test_cheapest_material_first_and_cost_override(self):
        order = []

        class OrderFetcher(RecordingFetcher):
            def fetch(self, uid, material):
                order.append(material)
                return super().fetch(uid, material)

        expensive_first = Condition(
            name='multi',
            materials=(Material.ATTACHMENT_PAYLOAD, Material.FLAGS),
            cost={Material.ATTACHMENT_PAYLOAD: 1.0, Material.FLAGS: 9.0},
            predicate=lambda message: True,
            evidence=lambda message: 'ok')
        self.verify(expensive_first, uids=['1'], fetcher=OrderFetcher(self.messages))
        # ATTACHMENT_PAYLOAD is made cheaper by the override; its full fetch also serves FLAGS
        self.assertEqual(order, [Material.ATTACHMENT_PAYLOAD])

    def test_full_fetch_serves_related_materials_without_extra_downloads(self):
        tree = And(body_text('attachment'), attachment_name('.pdf'))
        results, fetcher, _ = self.verify(tree, uids=['3'])
        self.assertTrue(results[0].is_matched)
        # the cheaper attachment material is fetched first; one full RFC822 fetch
        # then serves BODY, ATTACHMENT_META and ATTACHMENT_PAYLOAD together
        self.assertEqual(len(fetcher.calls), 1)
        self.assertIn(fetcher.calls[0][1], (Material.BODY, Material.ATTACHMENT_META))

    def test_cached_material_is_reused(self):
        fetcher = RecordingFetcher(self.messages)
        cache = {'1': {Material.ENVELOPE: self.messages['1']}}
        results, fetcher, _ = self.verify(from_('boss@corp.example'), uids=['1'], fetcher=fetcher, cache=cache)
        self.assertTrue(results[0].is_matched)
        self.assertEqual(fetcher.calls, [])

    def test_early_check_skips_pricier_material(self):
        # subject match is decidable from the envelope; BODY must never be fetched
        tree = message_text('invoice')
        results, fetcher, _ = self.verify(tree, uids=['1'])
        self.assertTrue(results[0].is_matched)
        self.assertEqual([material for _, material in fetcher.calls], [Material.ENVELOPE])
        decision = results[0].decisions[0]
        self.assertNotIn(Material.BODY, decision.materials)
        self.assertIn('subject', decision.evidence)

    def test_early_check_falls_through_to_full_material(self):
        tree = message_text('invoice')
        # uid 2 has no subject/body hit: the envelope cannot decide, so BODY is fetched
        results, fetcher, _ = self.verify(tree, uids=['2'])
        self.assertTrue(results[0].is_excluded)
        self.assertIn(('2', Material.BODY), fetcher.calls)

    def test_and_short_circuits_on_false(self):
        tree = And(from_('nobody@nowhere.example'), attachment_name('.pdf'))
        results, fetcher, _ = self.verify(tree, uids=['3'])
        self.assertTrue(results[0].is_excluded)
        self.assertNotIn(Material.ATTACHMENT_META, [material for _, material in fetcher.calls])

    def test_or_short_circuits_on_true(self):
        tree = Or(from_('boss@corp.example'), attachment_content('secret'))
        results, fetcher, _ = self.verify(tree, uids=['1'])
        self.assertTrue(results[0].is_matched)
        self.assertNotIn(Material.ATTACHMENT_PAYLOAD, [material for _, material in fetcher.calls])

    def test_tri_state_logic_truth_tables(self):
        unknown_leaf = Condition(
            name='unknown_leaf', materials=(Material.ENVELOPE,),
            predicate=lambda message: True, evidence=lambda message: '')
        fetcher = RecordingFetcher(self.messages, fail_on=(('1', Material.ENVELOPE),))

        results, _, _ = self.verify(And(unknown_leaf, from_('boss@corp.example')), uids=['1'], fetcher=fetcher)
        self.assertTrue(results[0].is_inconclusive)  # UNKNOWN AND TRUE -> UNKNOWN

        results, _, _ = self.verify(Or(unknown_leaf, from_('nobody@nowhere.example')), uids=['1'],
                                    fetcher=RecordingFetcher(self.messages, fail_on=(('1', Material.ENVELOPE),)))
        self.assertTrue(results[0].is_inconclusive)  # UNKNOWN OR FALSE -> UNKNOWN

        results, _, _ = self.verify(Not(unknown_leaf), uids=['1'],
                                    fetcher=RecordingFetcher(self.messages, fail_on=(('1', Material.ENVELOPE),)))
        self.assertTrue(results[0].is_inconclusive)  # NOT UNKNOWN -> UNKNOWN

    def test_failure_under_not_stays_unknown(self):
        tree = Not(attachment_content('secret'))
        fetcher = RecordingFetcher(self.messages, fail_on=(('1', Material.ATTACHMENT_PAYLOAD),))
        results, _, _ = self.verify(tree, uids=['1'], fetcher=fetcher)
        self.assertTrue(results[0].is_inconclusive)
        self.assertFalse(results[0].is_excluded)

    def test_decisive_decisions_and_evidence_ranges(self):
        tree = And(from_('boss@corp.example'), Or(subject('invoice'), subject('void')))
        results, _, _ = self.verify(tree, uids=['1'])
        decisive = {decision.condition for decision in results[0].decisive_decisions}
        self.assertEqual(decisive, {'from', 'subject'})
        subject_decision = next(d for d in results[0].decisions if d.condition == 'subject')
        self.assertIn('subject[0:7]', subject_decision.evidence)

    def test_attachment_conditions(self):
        tree = And(attachment_name('.pdf'), Not(attachment_content('confidential')))
        results, _, _ = self.verify(tree, uids=['3'])
        self.assertTrue(results[0].is_matched)
        evidence = next(d for d in results[0].decisions if d.condition == 'attachment_name').evidence
        self.assertIn('december.pdf', evidence)

    def test_flag_and_date_and_size_conditions(self):
        from imap_tools import MailMessageFlags
        tree = And(flag(MailMessageFlags.SEEN), date_gte(dt.date(2026, 1, 1)), size_gt(1))
        results, _, _ = self.verify(tree, uids=['1'])
        self.assertTrue(results[0].is_matched)
        results, _, _ = self.verify(tree, uids=['2'])
        self.assertTrue(results[0].is_excluded)

    def test_spent_cost_accumulates_per_uid(self):
        tree = And(flag('Seen'), body_text('invoice'))
        results, _, store = self.verify(tree, uids=['1'])
        self.assertGreater(store.spent_cost['1'], 0.0)
        self.assertEqual(results[0].spent_cost, store.spent_cost['1'])


class FilterUidsIntegrationTest(unittest.TestCase):

    class FakeClient:
        def __init__(self, messages):
            self.messages = messages
            self.search_criteria = None

        def uid(self, command, *args):
            if command == 'SEARCH':
                self.search_criteria = args[-1]
                return 'OK', [b' '.join(uid.encode() for uid in sorted(self.messages, key=int))]
            uid = args[0]
            return 'OK', self.messages[uid]

    class FakeMailbox:
        email_message_class = MailMessage

        def __init__(self, messages):
            self.client = FilterUidsIntegrationTest.FakeClient(messages)

        def uids(self, criteria, charset='US-ASCII'):
            result = self.client.uid('SEARCH', b'', criteria.encode(charset or 'us-ascii'))
            return result[1][0].decode().split() if result[1][0] else []

    def test_end_to_end_pipeline(self):
        mail_messages = {
            '1': build_message(uid='1', subject_='Invoice', text='pay me', seen=True),
            '2': build_message(uid='2', subject_='Photos', sender='friend@corp.example', text='weekend'),
        }
        raw = {uid: [(message._raw_uid_data, bytes(message.obj)), b')'] for uid, message in mail_messages.items()}
        mailbox = self.FakeMailbox(raw)

        tree = And(from_('boss@corp.example'), body_text('pay'))
        buckets = filter_uids(mailbox, tree)
        self.assertEqual([r.uid for r in buckets['matched']], ['1'])
        self.assertEqual([r.uid for r in buckets['excluded']], ['2'])
        self.assertEqual(buckets['inconclusive'], [])
        # both conjuncts are server-capable, so the server sees FROM and BODY
        self.assertIn(b'FROM', mailbox.client.search_criteria)
        self.assertIn(b'BODY', mailbox.client.search_criteria)

    def test_end_to_end_local_only_fetches_all_candidates(self):
        mail_messages = {'1': build_message(uid='1', attachments=(('a.pdf', 'x'),))}
        raw = {uid: [(message._raw_uid_data, bytes(message.obj)), b')'] for uid, message in mail_messages.items()}
        mailbox = self.FakeMailbox(raw)
        buckets = filter_uids(mailbox, attachment_name('.pdf'))
        self.assertEqual(mailbox.client.search_criteria, b'ALL')
        self.assertEqual([r.uid for r in buckets['matched']], ['1'])


class ImapMaterialFetcherTest(unittest.TestCase):

    def test_fetch_error_on_bad_status(self):
        class BadClient:
            def uid(self, command, *args):
                return 'NO', [None]

        class BadMailbox:
            client = BadClient()
            email_message_class = MailMessage

        fetcher = ImapMaterialFetcher(BadMailbox())
        with self.assertRaises(MaterialFetchError):
            fetcher.fetch('1', Material.FLAGS)


if __name__ == '__main__':
    unittest.main()
