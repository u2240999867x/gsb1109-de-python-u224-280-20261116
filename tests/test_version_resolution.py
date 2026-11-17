import base64
import json
import os
import unittest
import warnings
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone

from LnkParse3.version_resolution import EvidenceError
from LnkParse3.version_resolution import resolve_target_version


TARGET_DIR = os.path.join(os.path.dirname(__file__), 'samples')

SAMPLE9_SERIAL = '0xd215cbdb'
SAMPLE9_VOLUME = '2EBF7902-EDAA-43D6-A855-1512E2BABFF2'
SAMPLE9_FILE = '06CC9568-BF32-11EA-ABA1-008CFAAD700E'
SAMPLE9_SIZE = 1490944
SAMPLE9_PATH = 'C:\\AnonymusBrowser-v2.0\\AnonymusBrowser.exe'
SAMPLE9_WRITE = '2020-07-06T04:18:49.576528+00:00'
SAMPLE9_DOS = '2020-07-06T04:18:50+00:00'


@contextmanager
def sample(name):
    with open(os.path.join(TARGET_DIR, name), 'rb') as indata:
        yield base64.b64decode(indata.read())


def volume(**overrides):
    payload = {
        'volume_id': 'vol-c',
        'label': 'Anonymus',
        'identifiers': {'serial': SAMPLE9_SERIAL, 'droid_volume_id': SAMPLE9_VOLUME},
    }
    payload.update(overrides)
    return payload


def entry(version, **overrides):
    payload = {
        'version': version,
        'path': SAMPLE9_PATH,
        'size': SAMPLE9_SIZE,
        'times': {'modified': {'value': SAMPLE9_WRITE, 'precision': '1us'}},
    }
    payload.update(overrides)
    return payload


def catalog(*entries, **overrides):
    payload = {
        'collected_at': {'value': '2026-11-16T00:00:00Z', 'precision': 'day'},
        'volumes': [volume(entries=list(entries))],
    }
    payload.update(overrides)
    return payload


class VersionResolutionTest(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter('ignore', category=UserWarning)
        self.maxDiff = None

    def resolve(self, name, document, rules=None):
        with sample(name) as raw:
            return resolve_target_version(raw, document, rules=rules)

    def test_unique_version_from_object_identifier(self):
        document = catalog(
            entry('2.0-beta', size=1, identifiers={'droid_file_id': SAMPLE9_FILE + '0'}),
            entry('2.0', identifiers={'droid_file_id': SAMPLE9_FILE}),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target_version, '2.0')
        target = result.target
        self.assertEqual(target['strength'], 'strong')
        self.assertEqual(result.as_of.precision, '1us')
        self.assertEqual(result.as_of.source, 'header.modified_time')

        adopted = [candidate for candidate in result.candidates if candidate.version == '2.0'][0]
        fields = {check.lnk_field for check in adopted.matching('object')}
        self.assertIn(
            'extra.DISTRIBUTED_LINK_TRACKER_BLOCK.droid_file_identifier', fields
        )
        object_check = adopted.matching('object')[0]
        self.assertEqual(object_check.lnk_raw, SAMPLE9_FILE)
        self.assertEqual(object_check.catalog_raw, SAMPLE9_FILE)

        excluded = [
            candidate for candidate in result.candidates if candidate.version == '2.0-beta'
        ][0]
        self.assertEqual(excluded.status, 'excluded')
        self.assertTrue(excluded.failing('size'))
        size_check = excluded.failing('size')[0]
        self.assertEqual(size_check.lnk_raw, SAMPLE9_SIZE)
        self.assertEqual(size_check.catalog_raw, 1)

    def test_duplicate_object_identifier_in_catalog_is_contradiction(self):
        document = catalog(
            entry('2.0-a', identifiers={'droid_file_id': SAMPLE9_FILE}),
            entry('2.0-b', identifiers={'droid_file_id': SAMPLE9_FILE}),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'contradiction')
        self.assertIsNone(result.target_version)
        self.assertEqual(
            [item['kind'] for item in result.contradictions], ['catalog_object_conflict']
        )

    def test_multiple_compatible_versions_from_volume_only(self):
        document = catalog(
            entry('build-1', size=None, times={}),
            entry('build-2', size=None, times={}),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'multiple')
        self.assertIsNone(result.target_version)
        self.assertEqual(
            sorted(candidate.version for candidate in result.candidates),
            ['build-1', 'build-2'],
        )
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'ambiguous')
            self.assertEqual(candidate.strength, 'weak')

    def test_identified_volume_with_no_compatible_version_is_unresolved(self):
        document = catalog(
            entry('too-small', size=SAMPLE9_SIZE - 1),
            entry('too-large', size=SAMPLE9_SIZE + 1),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'unresolved')
        self.assertIsNone(result.target_version)
        self.assertEqual(result.contradictions, [])
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'excluded')
            self.assertTrue(candidate.failing('size'))

    def test_object_identifier_contradicted_by_size(self):
        document = catalog(
            entry(
                'wrong-size',
                size=SAMPLE9_SIZE + 1,
                identifiers={'droid_file_id': SAMPLE9_FILE},
            ),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'contradiction')
        self.assertIsNone(result.target_version)
        self.assertEqual(
            [item['kind'] for item in result.contradictions], ['identity_contradicted']
        )
        fields = result.contradictions[0]['fields']
        size_check = [field for field in fields if field['kind'] == 'size'][0]
        self.assertEqual(size_check['lnk_raw'], SAMPLE9_SIZE)
        self.assertEqual(size_check['catalog_raw'], SAMPLE9_SIZE + 1)

    def test_volume_only_size_mismatch_is_unresolved_not_contradiction(self):
        document = catalog(entry('wrong-size', size=SAMPLE9_SIZE + 1))
        result = self.resolve('sample9', document)
        self.assertEqual(result.status, 'unresolved')
        self.assertEqual(result.contradictions, [])
        self.assertTrue(result.candidates[0].failing('size'))

    def test_time_is_compared_only_at_provable_precision(self):
        catalog_day = catalog(
            entry(
                'day-precision',
                times={'modified': {'value': '2020-07-06', 'precision': 'day'}},
            )
        )
        result = self.resolve('sample9', catalog_day)
        self.assertEqual(result.status, 'unique')
        adopted = result.candidates[0]
        time_check = adopted.matching('time')[0]
        self.assertEqual(time_check.lnk_precision, '1us')
        self.assertEqual(time_check.catalog_precision, 'day')
        self.assertIn('day precision', time_check.detail)

        catalog_other_day = catalog(
            entry(
                'other-day',
                times={'modified': {'value': '2020-07-07', 'precision': 'day'}},
            )
        )
        other = self.resolve('sample9', catalog_other_day)
        self.assertEqual(other.status, 'unresolved')
        self.assertTrue(other.candidates[0].failing('time'))

    def test_dos_time_uses_two_second_bucket(self):
        document = catalog(
            entry(
                'dos',
                times={'modified': {'value': SAMPLE9_DOS, 'precision': '2s'}},
            )
        )
        result = self.resolve('sample9', document)
        check = result.candidates[0].matching('time')[0]
        self.assertEqual(check.catalog_precision, '2s')
        self.assertEqual(check.lnk_precision, '1us')
        self.assertIn('2s precision', check.detail)

        # 04:18:50 and 04:18:52 are adjacent two-second buckets, so the header
        # write time matches only the first.
        shifted = catalog(
            entry(
                'shifted',
                times={'modified': {'value': '2020-07-06T04:18:52+00:00', 'precision': '2s'}},
            )
        )
        shifted_result = self.resolve('sample9', shifted)
        self.assertEqual(shifted_result.status, 'unresolved')
        self.assertTrue(shifted_result.candidates[0].failing('time'))

    def test_shell_file_entry_dos_time_is_compared_at_two_seconds(self):
        document = catalog(
            entry(
                'dos',
                times={'modified': {'value': SAMPLE9_DOS, 'precision': '2s'}},
            )
        )
        result = self.resolve('sample9', document)
        checks = [
            check for check in result.candidates[0].checks
            if check.kind == 'time' and 'file_entry' in (check.lnk_field or '')
        ]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].result, 'match')
        self.assertEqual(checks[0].lnk_precision, '2s')
        self.assertEqual(checks[0].lnk_raw, datetime(2020, 7, 6, 4, 18, 50, tzinfo=timezone.utc))

    def test_collection_presence_is_not_inferred_from_shortcut_time(self):
        document = catalog(entry('2.0'))
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target['existed_at_collection'], 'unknown')
        self.assertTrue(
            any('survived until the collection date' in item for item in result.warnings)
        )

    def test_placement_after_shortcut_write_marks_version_absent(self):
        document = catalog(
            entry(
                'late',
                presence={'first_seen': {'value': '2027-01-01', 'precision': 'day'}},
            )
        )
        result = self.resolve('sample9', document)
        self.assertEqual(result.status, 'unresolved')
        self.assertEqual(result.candidates[0].presence_at_link, 'absent')
        self.assertIn(
            'catalog placement happens after the shortcut was written',
            result.candidates[0].reasons,
        )

    def test_removed_before_shortcut_write_marks_version_absent(self):
        document = catalog(
            entry(
                'gone',
                presence={'removed_at': {'value': '2020-01-01', 'precision': 'day'}},
            )
        )
        result = self.resolve('sample9', document)
        self.assertEqual(result.status, 'unresolved')
        self.assertEqual(result.candidates[0].presence_at_link, 'absent')
        self.assertIn(
            'catalog removal happens before the shortcut was written',
            result.candidates[0].reasons,
        )

    def test_later_file_at_same_path_does_not_inherit_relation(self):
        document = catalog(
            entry('2.0'),
            entry(
                '2.0-patched',
                size=2000,
                times={'modified': {'value': '2020-09-01', 'precision': 'day'}},
                presence={'first_seen': {'value': '2020-08-01', 'precision': 'day'}},
            ),
        )
        result = self.resolve('sample9', document)

        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target_version, '2.0')
        older = [item for item in result.candidates if item.version == '2.0'][0]
        newer = [item for item in result.candidates if item.version == '2.0-patched'][0]
        self.assertTrue(older.path_reused_after_link)
        self.assertEqual(older.path_later_versions, ['2.0-patched'])
        self.assertEqual(newer.status, 'excluded')

    def test_collection_observation_proves_survival(self):
        document = catalog(
            entry(
                '2.0',
                presence={
                    'first_seen': {'value': '2020-07-01', 'precision': 'day'},
                    'observed_at_collection': True,
                },
            )
        )
        result = self.resolve('sample9', document)
        self.assertEqual(result.target['existed_at_collection'], 'yes')
        self.assertFalse(
            any('survived until the collection date' in item for item in result.warnings)
        )

    def test_shortcut_internal_size_conflict(self):
        with sample('padded_cli_arguments') as raw:
            result = resolve_target_version(
                raw,
                {
                    'collected_at': '2026-11-16',
                    'entries': [
                        entry(
                            'any',
                            size=16,
                            identifiers={'droid_file_id': SAMPLE9_FILE},
                        )
                    ],
                },
            )
        self.assertEqual(result.status, 'contradiction')
        kinds = [item['kind'] for item in result.contradictions]
        self.assertIn('shortcut_internal_conflict', kinds)
        conflict = [
            item for item in result.contradictions
            if item['kind'] == 'shortcut_internal_conflict'
        ][0]
        raw_sizes = conflict['fields'][0]['lnk_raw']
        self.assertEqual(raw_sizes['header.file_size'], 16)
        self.assertEqual(
            raw_sizes['target.file_entry.file_size'], 1048576
        )

    def test_explicit_identity_rules_can_remap_fields(self):
        document = catalog(
            entry('remapped', identifiers={'custom_object_id': SAMPLE9_FILE}),
        )
        rules = {
            'volume': [],
            'object': [
                {
                    'lnk': 'extra.DISTRIBUTED_LINK_TRACKER_BLOCK.droid_file_identifier',
                    'catalog': 'entry.identifiers.custom_object_id',
                    'normalize': 'uuid',
                    'label': 'custom object id',
                }
            ],
            'size': [],
            'time': [],
            'path': [],
        }
        result = self.resolve('sample9', document, rules=rules)
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target_version, 'remapped')
        self.assertEqual(result.rules_source, 'inline')
        check = result.candidates[0].matching('object')[0]
        self.assertEqual(check.catalog_field, 'entry.identifiers.custom_object_id')
        self.assertEqual(check.lnk_raw, SAMPLE9_FILE)
        # Identity alone makes this a strong match even though size, time and
        # path rules were removed.
        self.assertEqual(result.target['strength'], 'strong')

    def test_missing_rules_reject_unknown_sections(self):
        with sample('sample9') as raw:
            with self.assertRaises(EvidenceError):
                resolve_target_version(raw, catalog(), rules={'bogus': []})

    def test_json_shape_is_stable(self):
        result = self.resolve(
            'sample9',
            catalog(entry('2.0', identifiers={'droid_file_id': SAMPLE9_FILE})),
        )
        payload = json.loads(json.dumps(result.as_dict(), default=str))
        self.assertEqual(
            set(payload),
            {
                'status',
                'target_version',
                'target',
                'as_of',
                'collection',
                'candidates',
                'contradictions',
                'warnings',
                'rules_source',
            },
        )
        candidate = payload['candidates'][0]
        self.assertIn('adopted_fields', candidate)
        self.assertIn('excluded_fields', candidate)
        self.assertEqual(candidate['adopted_fields'][0]['lnk_raw'], SAMPLE9_SERIAL)
        self.assertEqual(candidate['adopted_fields'][0]['catalog_raw'], SAMPLE9_SERIAL)

    def test_catalog_directory_merges_volume_fragments(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, 'volume-c.json'), 'w') as handle:
                json.dump(
                    {
                        'collected_at': '2026-11-16',
                        'volume': {
                            'volume_id': 'vol-c',
                            'identifiers': {'serial': SAMPLE9_SERIAL},
                            'entries': [
                                {
                                    'version': '2.0',
                                    'size': SAMPLE9_SIZE,
                                    'identifiers': {'droid_file_id': SAMPLE9_FILE},
                                }
                            ],
                        },
                    },
                    handle,
                )
            with open(os.path.join(directory, 'entry-other.json'), 'w') as handle:
                json.dump(
                    {
                        'volume': 'vol-c',
                        'entries': [{'version': 'old', 'size': 1}],
                    },
                    handle,
                )

            with sample('sample9') as raw:
                result = resolve_target_version(raw, directory)

        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target_version, '2.0')

    def test_catalog_directory_rejects_duplicate_versions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            for name in ('a.json', 'b.json'):
                with open(os.path.join(directory, name), 'w') as handle:
                    json.dump(
                        {
                            'volume': 'vol-c',
                            'entries': [{'version': '2.0', 'size': SAMPLE9_SIZE}],
                        },
                        handle,
                    )
            with sample('sample9') as raw:
                with self.assertRaises(EvidenceError):
                    resolve_target_version(raw, directory)

    def test_catalog_directory_rejects_conflicting_collection_times(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            for name, collected in (
                ('a.json', '2026-11-16'),
                ('b.json', '2026-11-17'),
            ):
                with open(os.path.join(directory, name), 'w') as handle:
                    json.dump(
                        {
                            'collected_at': collected,
                            'volume': {'volume_id': name, 'entries': []},
                        },
                        handle,
                    )
            with sample('sample9') as raw:
                with self.assertRaises(EvidenceError):
                    resolve_target_version(raw, directory)

    def test_catalog_manifest_takes_precedence(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, 'catalog.json'), 'w') as handle:
                document = catalog(
                    entry('2.0', identifiers={'droid_file_id': SAMPLE9_FILE})
                )
                json.dump(document, handle)
            with open(os.path.join(directory, 'other.json'), 'w') as handle:
                json.dump({'volume': 'ignored', 'entries': [{'version': 'ignored'}]}, handle)

            with sample('sample9') as raw:
                result = resolve_target_version(raw, directory)

        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.target_version, '2.0')

    def test_rules_cannot_be_loaded_from_a_directory(self):
        with sample('sample9') as raw:
            with self.assertRaises(EvidenceError):
                resolve_target_version(raw, catalog(), rules='tests')

    def test_network_shortcut_uses_net_name_for_path(self):
        with sample('network_info') as raw:
            result = resolve_target_version(
                raw,
                {
                    'entries': [
                        {
                            'version': '2017',
                            'path': (
                                '\\\\10.0.0.150\\LMmetal\\A - LM METAL LIFT\\'
                                '01.OBCHOD - BROŽURY - Prodejní a technické informace '
                                'o produktech\\ETN\\ETN-Katalog-ENG\\Katalog ETN 10_2017\\'
                                'Lift-programme\\ETN-lift programme 2017.pdf'
                            ),
                            'size': 21895266,
                            'identifiers': {
                                'droid_file_id': '00000024-0000-0000-6A6D-060000000000'
                            },
                        }
                    ]
                },
            )
        self.assertEqual(result.status, 'unique')
        path_check = result.candidates[0].matching('path')[0]
        self.assertTrue(path_check.lnk_raw.startswith('\\\\10.0.0.150\\LMmetal\\'))
        self.assertIn('2017.pdf', path_check.lnk_raw)


if __name__ == '__main__':
    unittest.main()
