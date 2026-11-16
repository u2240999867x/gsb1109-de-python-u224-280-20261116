import datetime
from io import BytesIO
from types import SimpleNamespace

import pytest

from smb.restore import (
    AmbiguousRestoreSetError,
    BackupGroup,
    CONTRADICTION,
    PinnedRestore,
    RequiredFile,
    RestorePlanner,
    RestoreSetRevokedError,
    ShareSnapshots,
    Snapshot,
    SMBFileProbe,
    SnapshotUnavailableError,
    TimeWindow,
    build_snapshot_path,
    find_restore_sets,
    format_snapshot_time,
    normalize_path,
    plan_from_manifest,
    snapshots_for_connection,
)


UTC = datetime.timezone.utc


def dt(value):
    return datetime.datetime.fromisoformat(value).replace(tzinfo=UTC)


SHARES = [
    ShareSnapshots('alpha', [
        Snapshot(dt('2026-11-10T01:00:00'), 'A-0'),
        Snapshot(dt('2026-11-12T02:00:00'), 'A-1'),
    ]),
    ShareSnapshots('beta', [
        Snapshot(dt('2026-11-10T01:00:00'), 'B-0'),
        Snapshot(dt('2026-11-12T02:00:00'), 'B-1'),
    ]),
    ShareSnapshots('gamma', [
        Snapshot(dt('2026-11-10T01:00:30'), 'G-0'),
        Snapshot(dt('2026-11-12T02:00:30'), 'G-2'),
    ]),
]


def groups_for(members):
    return [BackupGroup('g1', members)]


def test_same_timestamp_alone_is_not_correspondence():
    report = find_restore_sets(SHARES, [], TimeWindow())
    assert report.restore_sets == ()
    assert report.missing_correspondence
    assert 'no_backup_groups' in {item.reason for item in report.missing_correspondence}


def test_group_without_all_shares_is_incomplete():
    report = find_restore_sets(SHARES, groups_for({
        'alpha': ('A-1', 'catalogue:a1'),
        'beta': ('B-1', 'catalogue:b1'),
    }), TimeWindow())

    assert report.restore_sets == ()
    reasons = [item.reason for item in report.missing_correspondence]
    assert 'member_missing' in reasons
    missing = [item for item in report.missing_correspondence if item.reason == 'member_missing']
    assert len(missing) == 1 and missing[0].share == 'gamma'


def test_complete_group_is_selected_and_paths_are_pinned():
    report = find_restore_sets(SHARES, groups_for({
        'alpha': ('A-1', 'catalogue:a1'),
        'beta': ('B-1', 'catalogue:b1'),
        'gamma': ('G-2', 'catalogue:g2'),
    }), TimeWindow(dt('2026-11-01T00:00:00'), dt('2026-11-30T00:00:00')))

    assert len(report.restore_sets) == 1
    restore = report.select_set('g1')
    assert restore.shares == ('alpha', 'beta', 'gamma')
    assert restore.pinned_path('alpha', '/Accounting/ledger.csv') == (
        '/@GMT-2026.11.12-02.00.00/Accounting/ledger.csv')
    assert build_snapshot_path(restore.snapshots['gamma'], '/') == '/@GMT-2026.11.12-02.00.30'
    assert format_snapshot_time(restore.snapshots['alpha'].timestamp) == '@GMT-2026.11.12-02.00.00'


def test_group_evidence_is_required_per_share():
    report = find_restore_sets(SHARES, [BackupGroup('g1', {
        'alpha': {'snapshot_id': 'A-1', 'evidence': 'catalogue:a1'},
        'beta': {'snapshot_id': 'B-1'},
        'gamma': {'snapshot_id': 'G-2', 'evidence': 'catalogue:g2'},
    }, evidence='')], TimeWindow())

    assert report.restore_sets == ()
    evidence = [item for item in report.missing_correspondence if item.reason == 'evidence_missing']
    assert len(evidence) == 1 and evidence[0].share == 'beta'


def test_required_file_missing_marks_only_that_group():
    shares = list(SHARES) + [
        ShareSnapshots('delta', [
            Snapshot(dt('2026-11-11T02:00:00'), 'D-2', files=['/reports/a.csv']),
            Snapshot(dt('2026-11-12T02:00:00'), 'D-1', files=['/reports/a.csv']),
        ]),
    ]
    groups = [
        BackupGroup('good', {
            'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'),
            'gamma': ('G-2', 'c3'), 'delta': ('D-1', 'c4'),
        }),
        BackupGroup('bad', {
            'alpha': ('A-0', 'c5'), 'beta': ('B-0', 'c6'),
            'gamma': ('G-0', 'c7'), 'delta': ('D-2', 'c8'),
        }),
    ]
    report = find_restore_sets(
        shares, groups, TimeWindow(),
        required_files=[('alpha', '/Accounting/ledger.csv'), ('delta', '/reports/a.csv')],
        probe=lambda share, snapshot, path: snapshot.snapshot_id in ('A-1', 'D-1'),
    )

    assert [item.group_id for item in report.restore_sets] == ['good']
    missing = report.missing_files
    assert len(missing) == 2
    assert {item.share for item in missing} == {'alpha', 'delta'}
    assert all(item.group_id == 'bad' for item in missing)


def test_directory_required_file_accepts_listing_or_descendant_manifest():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1',
                     files=['/Accounting/ledger.csv']),
        ]),
    ]
    group = [BackupGroup('g1', {'alpha': ('A-1', 'evidence')})]
    report = find_restore_sets(
        shares, group, TimeWindow(),
        required_files=[RequiredFile('alpha', '/Accounting/', is_directory=True)],
    )
    assert len(report.restore_sets) == 1


def test_out_of_window_and_time_mismatch_are_distinct_diagnostic_classes():
    report = find_restore_sets(SHARES, groups_for({
        'alpha': {'snapshot_id': 'A-1', 'timestamp': dt('2026-11-11T00:00:00'), 'evidence': 'c1'},
        'beta': ('B-0', 'c2'),
        'gamma': ('G-0', 'c3'),
    }), TimeWindow(dt('2026-11-12T00:00:00'), dt('2026-11-13T00:00:00')))

    assert report.restore_sets == ()
    assert any(item.kind == CONTRADICTION and item.reason == 'snapshot_time_conflict'
               for item in report.contradictions)
    # beta and gamma are outside the window; alpha never resolves.
    assert {item.reason for item in report.outside_window} == {'snapshot_outside_window'}


def test_snapshot_declaring_another_group_is_a_contradiction():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1', declared_group='other'),
        ]),
        ShareSnapshots('beta', [Snapshot(dt('2026-11-12T02:00:00'), 'B-1')]),
    ]
    report = find_restore_sets(shares, groups_for({
        'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'),
    }), TimeWindow())

    assert report.restore_sets == ()
    assert any(item.reason == 'snapshot_group_conflict' for item in report.contradictions)


def test_one_snapshot_cannot_be_claimed_by_two_groups():
    shares = [
        ShareSnapshots('alpha', [Snapshot(dt('2026-11-12T02:00:00'), 'A-1')]),
        ShareSnapshots('beta', [Snapshot(dt('2026-11-12T02:00:00'), 'B-1')]),
    ]
    groups = [
        BackupGroup('g1', {'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2')}),
        BackupGroup('g2', {'alpha': ('A-1', 'c3'), 'beta': ('B-1', 'c4')}),
    ]
    report = find_restore_sets(shares, groups, TimeWindow())

    assert report.restore_sets == ()
    assert {item.group_id for item in report.contradictions} == {'g1', 'g2'}


def test_no_complete_set_does_not_fall_back_to_live_paths():
    report = find_restore_sets(SHARES, groups_for({
        'alpha': ('A-1', 'c1'),
        'beta': ('B-1', 'c2'),
    }), TimeWindow())
    with pytest.raises(Exception) as error:
        report.select_set()
    assert 'no evidence-complete common restore set' in str(error.value)


def test_multiple_complete_sets_require_explicit_selection():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1'),
            Snapshot(dt('2026-11-12T03:00:00'), 'A-2'),
        ]),
        ShareSnapshots('beta', [
            Snapshot(dt('2026-11-12T02:00:00'), 'B-1'),
            Snapshot(dt('2026-11-12T03:00:00'), 'B-2'),
        ]),
    ]
    groups = [
        BackupGroup('g1', {'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2')}),
        BackupGroup('g2', {'alpha': ('A-2', 'c3'), 'beta': ('B-2', 'c4')}),
    ]
    report = find_restore_sets(shares, groups, TimeWindow())

    assert [item.group_id for item in report.restore_sets] == ['g1', 'g2']
    with pytest.raises(AmbiguousRestoreSetError):
        report.select_set()
    assert report.select_set('g2').group_id == 'g2'


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def listPath(self, share, path, **kwargs):
        self.calls.append(('listPath', share, path))
        return []

    def getAttributes(self, share, path, **kwargs):
        self.calls.append(('getAttributes', share, path))
        return object()

    def retrieveFile(self, share, path, file_obj, **kwargs):
        self.calls.append(('retrieveFile', share, path))
        return 0, 0

    def retrieveFileFromOffset(self, share, path, file_obj, *args, **kwargs):
        self.calls.append(('retrieveFileFromOffset', share, path))
        return 0, 0


def make_complete_report():
    return find_restore_sets(SHARES, groups_for({
        'alpha': ('A-1', 'c1'),
        'beta': ('B-1', 'c2'),
        'gamma': ('G-2', 'c3'),
    }), TimeWindow())


def test_pinned_reads_use_snapshot_paths_only():
    report = make_complete_report()
    connection = RecordingConnection()
    session = PinnedRestore.from_report(report, connection, 'g1')

    session.listPath('alpha')
    session.getAttributes('beta', '/Accounting/ledger.csv')
    session.retrieveFile('gamma', '/x.csv', BytesIO())

    assert connection.calls == [
        ('listPath', 'alpha', '/@GMT-2026.11.12-02.00.00'),
        ('getAttributes', 'beta', '/@GMT-2026.11.12-02.00.00/Accounting/ledger.csv'),
        ('retrieveFile', 'gamma', '/@GMT-2026.11.12-02.00.30/x.csv'),
    ]
    with pytest.raises(Exception):
        session.listPath('unknown')


def test_revoking_one_copy_only_removes_dependent_sets_and_diagnostics():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-10T02:00:00'), 'A-0'),
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1'),
        ]),
        ShareSnapshots('beta', [
            Snapshot(dt('2026-11-10T02:00:00'), 'B-0'),
            Snapshot(dt('2026-11-12T02:00:00'), 'B-1'),
        ]),
        ShareSnapshots('gamma', [
            Snapshot(dt('2026-11-10T02:00:00'), 'G-0'),
            Snapshot(dt('2026-11-12T02:00:00'), 'G-1'),
        ]),
    ]
    groups = [
        BackupGroup('newer', {
            'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'), 'gamma': ('G-1', 'c3'),
        }),
        BackupGroup('older', {
            'alpha': ('A-0', 'c4'), 'beta': ('B-0', 'c5'), 'gamma': ('G-0', 'c6'),
        }),
    ]
    report = find_restore_sets(shares, groups, TimeWindow())

    assert [item.group_id for item in report.restore_sets] == ['newer', 'older']
    assert len(report.select_set('newer').members) == 3

    revoked = report.invalidate_snapshot(snapshot_id='A-1', reason='copy expired')
    assert [item.group_id for item in revoked] == ['newer']
    assert [item.group_id for item in report.restore_sets] == ['older']
    assert PinnedRestore.from_report(report, RecordingConnection()).group_id == 'older'
    assert report.unavailable[0].snapshot_id == 'A-1'


def test_attached_report_is_revoked_and_missing_file_diagnostics_withdrawn():
    report = find_restore_sets(
        SHARES, groups_for({
            'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'), 'gamma': ('G-2', 'c3'),
        }), TimeWindow(),
        required_files=[('alpha', '/missing.csv')],
        probe=lambda share, snapshot, path: False,
    )
    assert report.restore_sets == ()
    assert report.missing_files


def test_mark_unavailable_revokes_set_and_blocks_reads():
    report = make_complete_report()
    connection = RecordingConnection()
    session = PinnedRestore.from_report(report, connection, 'g1')
    session.mark_unavailable('beta', reason='copy expired')

    assert report.restore_sets == ()
    assert session.available is False
    with pytest.raises(SnapshotUnavailableError):
        session.getAttributes('beta', '/x')
    with pytest.raises(RestoreSetRevokedError):
        PinnedRestore.from_report(report, connection, 'g1')


def test_timestamp_alias_revocation_matches_utc_copy():
    report = make_complete_report()
    revoked = report.invalidate_snapshot(snapshot_id='A-1', reason='gone')
    assert [item.group_id for item in revoked] == ['g1']
    assert report.restore_sets == ()
    assert report.unavailable and report.unavailable[0].snapshot_id == 'A-1'


def test_planner_wrapper_and_structured_report():
    planner = RestorePlanner(SHARES, groups_for({
        'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'), 'gamma': ('G-2', 'c3'),
    }), TimeWindow())
    report = planner.plan()
    payload = report.as_dict()
    assert payload['restore_sets'][0]['group_id'] == 'g1'
    assert payload['window'] == {'start': None, 'end': None}


def test_two_copies_at_the_same_time_cannot_be_addressed_by_gmt_name():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1'),
            Snapshot(dt('2026-11-12T02:00:00'), 'A-2'),
        ]),
        ShareSnapshots('beta', [Snapshot(dt('2026-11-12T02:00:00'), 'B-1')]),
    ]
    report = find_restore_sets(shares, groups_for({
        'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'),
    }), TimeWindow())

    assert report.restore_sets == ()
    assert any(item.reason == 'ambiguous_snapshot_time' for item in report.contradictions)


def test_explicit_absent_file_in_manifest_is_missing():
    shares = [
        ShareSnapshots('alpha', [
            Snapshot(dt('2026-11-12T02:00:00'), 'A-1',
                     files={'/present.csv': True, '/absent.csv': False}),
        ]),
    ]
    group = [BackupGroup('g1', {'alpha': ('A-1', 'evidence')})]
    present = find_restore_sets(
        shares, group, TimeWindow(),
        required_files=[('alpha', '/present.csv')],
    )
    absent = find_restore_sets(
        shares, group, TimeWindow(),
        required_files=[('alpha', '/absent.csv')],
    )
    assert len(present.restore_sets) == 1
    assert absent.restore_sets == ()
    assert [item.path for item in absent.missing_files] == ['/absent.csv']


def test_path_traversal_is_rejected_before_building_gmt_path():
    with pytest.raises(ValueError):
        normalize_path('/a/../../b')
    with pytest.raises(ValueError):
        RequiredFile('alpha', '/../../escape')


def test_manifest_loader_preserves_missing_evidence():
    manifest = {
        'shares': {
            'finance': {'snapshots': [
                {'id': 'finance-1', 'time': '2026-11-12T02:00:00Z',
                 'backup_group': 'nightly', 'files': ['/ledger.xlsx']},
            ]},
            'archive': {'copies': [
                {'id': 'archive-1', 'time': '2026-11-12T02:00:30Z',
                 'backup_group': 'nightly', 'files': ['/reports/q3.pdf']},
            ]},
        },
        'backup_groups': [
            {'id': 'nightly', 'backup_time': '2026-11-12T02:00:00Z',
             'correspondence': {
                 'finance': {'id': 'finance-1', 'proof': 'catalogue:finance'},
                 'archive': {'id': 'archive-1'},
             }},
        ],
        'window': {'from': '2026-11-11T00:00:00Z', 'to': '2026-11-14T00:00:00Z'},
        'required_files': {'finance': ['/ledger.xlsx'], 'archive': ['/reports/']},
    }
    report = plan_from_manifest(manifest)
    assert report.restore_sets == ()
    assert [item.reason for item in report.missing_correspondence] == ['evidence_missing']
    assert report.diagnostics_for('nightly')


def test_report_summary_and_manifest_json_payload():
    report = make_complete_report()
    summary = report.summary()
    assert summary['complete_sets'] == ['g1']
    assert summary['missing_files'] == 0
    assert report.group_evidence_missing == report.missing_correspondence
    assert report.file_missing == report.missing_files
    assert report.declarations_conflicting == report.contradictions


def test_group_time_outside_window_rejects_otherwise_complete_set():
    groups = [BackupGroup('g1', {
        'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'), 'gamma': ('G-2', 'c3'),
    }, time=dt('2026-11-20T00:00:00'))]
    report = find_restore_sets(
        SHARES, groups,
        TimeWindow(dt('2026-11-11T00:00:00'), dt('2026-11-13T00:00:00')),
    )
    assert report.restore_sets == ()
    assert [item.reason for item in report.outside_window] == ['group_time_outside_window']


def test_extra_trusted_member_outside_scope_does_not_invalidate_set():
    groups = [BackupGroup('g1', {
        'alpha': ('A-1', 'c1'), 'beta': ('B-1', 'c2'),
        'gamma': ('G-2', 'c3'), 'other': ('O-1', 'c4'),
    })]
    report = find_restore_sets(SHARES, groups, TimeWindow())
    assert [item.group_id for item in report.restore_sets] == ['g1']
    assert [item.share for item in report.diagnostics if item.kind == 'out_of_scope'] == ['other']


def test_smb_file_probe_uses_pinned_attribute_lookup():
    class Connection:
        def __init__(self):
            self.calls = []
            self.entries = {
                '/@GMT-2026.11.12-02.00.00/reports': True,
                '/@GMT-2026.11.12-02.00.00/reports/q3.pdf': False,
            }

        def getAttributes(self, share, path):
            self.calls.append((share, path))
            if path not in self.entries:
                raise FileNotFoundError(path)
            return SimpleNamespace(isDirectory=self.entries[path])

    connection = Connection()
    probe = SMBFileProbe(connection)
    snapshot = Snapshot(dt('2026-11-12T02:00:00'), 'A-1')
    assert probe('alpha', snapshot, '/reports/') is True
    assert probe('alpha', snapshot, '/reports/q3.pdf') is True
    assert probe('alpha', snapshot, '/missing.pdf') is False
    assert probe('alpha', snapshot, '/q3.pdf') is False
    assert connection.calls[0] == ('alpha', '/@GMT-2026.11.12-02.00.00/reports')
    assert all('@GMT-' in path for _, path in connection.calls)


def test_shadow_component_in_logical_path_is_rejected():
    report = make_complete_report()
    session = PinnedRestore(report.select_set('g1'), RecordingConnection())
    with pytest.raises(ValueError):
        session.getAttributes('alpha', '/@GMT-2026.11.01-00.00.00/x')


def test_snapshots_for_connection_uses_share_listing():
    class Connection:
        def __init__(self):
            self.calls = []

        def listSnapshots(self, share, path, timeout=30):
            self.calls.append((share, path, timeout))
            return [dt('2026-11-12T02:00:00')]

    connection = Connection()
    listing = snapshots_for_connection(connection, 'alpha')
    assert connection.calls == [('alpha', '/', 30)]
    assert listing.name == 'alpha'
    assert listing.snapshots[0].snapshot_id == '@GMT-2026.11.12-02.00.00'
    assert listing.snapshots[0].timestamp.tzinfo is not None
