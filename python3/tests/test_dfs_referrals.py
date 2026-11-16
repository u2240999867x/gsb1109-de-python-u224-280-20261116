import unittest
from datetime import datetime, timedelta, timezone

from smb.dfs_referrals import ReferralEntry, load_referrals, resolve_referrals


NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=7)).isoformat()
PAST = (NOW - timedelta(days=7)).isoformat()


def make_entry(logical, target, allowed, expires_at=None):
    return ReferralEntry(logical, target, allowed, expires_at)


class ResolveReferralsTest(unittest.TestCase):

    def test_direct_resolution(self):
        entries = [make_entry('L1', 'T1', ['T1'])]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(len(report['resolved']), 1)
        result = report['resolved'][0]
        self.assertEqual(result['logical_path'], 'L1')
        self.assertEqual(result['actual_target'], 'T1')
        self.assertEqual(result['chain'], ['L1', 'T1'])
        self.assertEqual(report['cycles'], [])
        self.assertEqual(report['out_of_bounds'], [])

    def test_chained_referral(self):
        entries = [
            make_entry('L1', 'L2', ['L2', 'T2']),
            make_entry('L2', 'T2', ['T2']),
        ]
        report = resolve_referrals(entries, NOW)
        resolved = {r['logical_path']: r for r in report['resolved']}
        self.assertEqual(resolved['L1']['actual_target'], 'T2')
        self.assertEqual(resolved['L1']['chain'], ['L1', 'L2', 'T2'])
        self.assertEqual(resolved['L2']['actual_target'], 'T2')

    def test_shared_target_for_two_entries(self):
        entries = [
            make_entry('L1', 'T', ['T']),
            make_entry('L2', 'T', ['T']),
        ]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(len(report['resolved']), 2)
        self.assertEqual({r['actual_target'] for r in report['resolved']}, {'T'})

    def test_cycle_detected(self):
        entries = [
            make_entry('L1', 'L2', ['L1', 'L2']),
            make_entry('L2', 'L1', ['L1', 'L2']),
        ]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(report['resolved'], [])
        self.assertEqual(len(report['cycles']), 2)
        cycles = {c['logical_path']: c for c in report['cycles']}
        self.assertEqual(cycles['L1']['chain'], ['L1', 'L2', 'L1'])
        self.assertEqual(cycles['L1']['cycle_target'], 'L1')

    def test_self_loop_detected(self):
        entries = [make_entry('L1', 'L1', ['L1'])]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(len(report['cycles']), 1)
        self.assertEqual(report['cycles'][0]['chain'], ['L1', 'L1'])

    def test_out_of_bounds_target(self):
        entries = [make_entry('L1', 'EVIL', ['T1'])]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(report['resolved'], [])
        self.assertEqual(len(report['out_of_bounds']), 1)
        self.assertEqual(report['out_of_bounds'][0]['rejected_target'], 'EVIL')

    def test_authorization_not_inherited_through_chain(self):
        # L1 chains through L2 to T2. T2 is authorized for L2 but not for L1,
        # so L1 must be rejected even though the intermediate hop L2 allows it.
        entries = [
            make_entry('L1', 'L2', ['L2']),
            make_entry('L2', 'T2', ['T2']),
        ]
        report = resolve_referrals(entries, NOW)
        self.assertEqual([r['logical_path'] for r in report['resolved']], ['L2'])
        self.assertEqual(len(report['out_of_bounds']), 1)
        rejected = report['out_of_bounds'][0]
        self.assertEqual(rejected['logical_path'], 'L1')
        self.assertEqual(rejected['rejected_target'], 'T2')
        self.assertEqual(rejected['chain'], ['L1', 'L2', 'T2'])

    def test_expired_referral(self):
        entries = [
            make_entry('L1', 'L2', ['L2', 'T2']),
            make_entry('L2', 'T2', ['T2'], expires_at=datetime.fromisoformat(PAST)),
        ]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(report['resolved'], [])
        self.assertEqual(len(report['expired']), 2)
        expired = {e['logical_path']: e for e in report['expired']}
        self.assertEqual(expired['L1']['expired_referral'], 'L2')
        self.assertEqual(expired['L1']['chain'], ['L1', 'L2'])
        self.assertEqual(expired['L2']['chain'], ['L2'])

    def test_valid_referral_within_validity(self):
        entries = [make_entry('L1', 'T1', ['T1'], expires_at=datetime.fromisoformat(FUTURE))]
        report = resolve_referrals(entries, NOW)
        self.assertEqual(len(report['resolved']), 1)
        self.assertEqual(report['expired'], [])


class LoadReferralsTest(unittest.TestCase):

    def test_load_from_json_dict(self):
        data = {
            'referrals': [
                {'logical_path': 'L1', 'target': 'T1',
                 'allowed_targets': ['T1'], 'expires_at': FUTURE},
                {'logical_path': 'L2', 'target': 'T2', 'allowed_targets': ['T2']},
            ]
        }
        entries = load_referrals(data)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].expires_at, datetime.fromisoformat(FUTURE))
        self.assertIsNone(entries[1].expires_at)
        self.assertFalse(entries[0].is_expired(NOW))

    def test_z_suffix_timestamp(self):
        data = {'referrals': [{'logical_path': 'L1', 'target': 'T1',
                               'allowed_targets': ['T1'],
                               'expires_at': '2026-12-01T00:00:00Z'}]}
        entries = load_referrals(data)
        self.assertEqual(entries[0].expires_at,
                         datetime(2026, 12, 1, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()
