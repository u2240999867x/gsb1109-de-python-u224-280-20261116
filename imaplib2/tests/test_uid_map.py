import threading

from imaplib2.imaplib2 import IMAP4, Request, _UidMap, _parse_seq_set


class DummyParent:

    def __init__(self):
        self.tagpre = b'T'
        self.tagnum = 0


def make_imap():
    # Bypass IMAP4.__init__ (which opens a socket and starts threads):
    # only the sequence/UID bookkeeping is under test.
    imap = IMAP4.__new__(IMAP4)
    imap.uid_map = _UidMap()
    imap.tagged_commands = {}
    imap.commands_lock = threading.Lock()
    imap.debug = 0
    return imap


def make_request(name, *args, imap=None):
    parent = DummyParent()
    rqb = Request(parent, name=name)
    if imap is None:
        imap = make_imap()
    imap._record_message_set(rqb, args)
    return imap, rqb


class TestUidMap:

    def test_fetch_learns_uid_and_reverse_lookup(self):
        uid_map = _UidMap()
        uid_map.exists_set(3)
        uid_map.fetch(1, 101)
        uid_map.fetch(2, 102)

        assert uid_map.uid(1) == 101
        assert uid_map.uid(2) == 102
        assert uid_map.uid(3) is None          # Not learned yet: undetermined
        assert uid_map.sequence(102) == 2
        assert uid_map.sequence(999) is None

    def test_expunge_renumbers_sequence_but_keeps_uids_stable(self):
        uid_map = _UidMap()
        uid_map.exists_set(3)
        for seq, uid in ((1, 101), (2, 102), (3, 103)):
            uid_map.fetch(seq, uid)

        assert uid_map.expunge(2) == 102      # Removed slot's UID reported

        assert uid_map.exists == 2
        assert uid_map.uid(1) == 101          # Surviving UIDs unchanged
        assert uid_map.uid(2) == 103          # Old seq 3 shifted into slot 2
        assert uid_map.sequence(103) == 2
        assert uid_map.sequence(102) is None  # Expunged UID no longer in view

    def test_expunge_without_known_uid_is_undetermined(self):
        uid_map = _UidMap()
        uid_map.exists_set(2)
        uid_map.fetch(2, 202)

        assert uid_map.expunge(1) is None     # UID of slot 1 never learned
        assert uid_map.uid(1) == 202          # Slot 2 moved down, UID intact

    def test_exists_grow_and_shrink(self):
        uid_map = _UidMap()
        uid_map.exists_set(2)
        uid_map.fetch(1, 11)
        uid_map.fetch(2, 12)

        uid_map.exists_set(4)                  # New messages appended
        assert uid_map.exists == 4
        assert uid_map.uid(1) == 11

        uid_map.exists_set(2)                  # View shrink without EXPUNGE
        assert uid_map.uid(2) == 12
        assert uid_map.uid(3) is None

    def test_reset_clears_view(self):
        uid_map = _UidMap()
        uid_map.exists_set(1)
        uid_map.fetch(1, 9)
        uid_map.reset()
        assert uid_map.exists == 0
        assert uid_map.uid(1) is None


class TestParseSeqSet:

    def test_lists_and_ranges(self):
        assert _parse_seq_set('1,3:5') == [1, 3, 4, 5]
        assert _parse_seq_set(b'2') == [2]

    def test_star_resolved_with_highest(self):
        assert _parse_seq_set('1:*', highest=3) == [1, 2, 3]
        assert _parse_seq_set('*', highest=7) == [7]

    def test_star_unresolved_yields_none(self):
        assert _parse_seq_set('1:*') == [1, None]
        assert _parse_seq_set('*') == [None]


class TestUntaggedEvents:

    def test_fetch_header_with_uid_updates_map(self):
        imap = make_imap()
        imap.uid_map.exists_set(2)

        imap._note_untagged_event('FETCH', b'1 FETCH (UID 101 FLAGS (\\Seen))')
        imap._note_untagged_event(
            'FETCH', (b'2 FETCH (FLAGS (\\Seen) UID 102', b'literal body'))

        assert imap.message_uid(1) == 101
        assert imap.message_uid(2) == 102

    def test_fetch_without_uid_is_ignored(self):
        imap = make_imap()
        imap._note_untagged_event('FETCH', b'1 FETCH (FLAGS (\\Seen))')
        assert imap.message_uid(1) is None

    def test_xuid_header_is_not_treated_as_uid(self):
        imap = make_imap()
        imap._note_untagged_event('FETCH', b'1 FETCH (X-UID 101)')
        assert imap.message_uid(1) is None

    def test_expunge_event_shifts_the_view(self):
        imap = make_imap()
        imap._note_untagged_event('EXISTS', 3)
        for seq, uid in ((1, 101), (2, 102), (3, 103)):
            imap._note_untagged_event('FETCH', b'%d FETCH (UID %d)' % (seq, uid))

        imap._note_untagged_event('EXPUNGE', 2)

        assert imap.uid_map.exists == 2
        assert imap.message_uid(2) == 103      # Physical shift, not a UID change
        assert imap.message_sequence(103) == 2

    def test_other_events_do_not_touch_the_map(self):
        imap = make_imap()
        imap._note_untagged_event('RECENT', 1)
        assert imap.uid_map.exists == 0


class TestRequestTargets:

    def test_sequence_request_resolves_to_uids_with_gaps_undetermined(self):
        imap = make_imap()
        imap.uid_map.exists_set(3)
        imap.uid_map.fetch(1, 101)
        imap.uid_map.fetch(2, 102)
        # Slot 3 has no UID mapping yet when the request is issued.
        imap, rqb = make_request('FETCH', '1:3', '(UID)', imap=imap)

        assert rqb.message_set == ('SEQ', 'FETCH', '1:3')
        assert imap.request_target_uids(rqb) == [101, 102, None]

    def test_uid_request_returns_named_uids_directly(self):
        imap = make_imap()
        imap, rqb = make_request('UID', 'FETCH', '101:103', '(FLAGS)', imap=imap)
        assert rqb.message_set == ('UID', 'FETCH', '101:103')

        # UIDs are stable identifiers, independent of the sequence view.
        assert imap.request_target_uids(rqb) == [101, 102, 103]

    def test_uid_expunge_records_its_uid_set(self):
        imap = make_imap()
        imap, rqb = make_request('UID', 'EXPUNGE', '101,103', imap=imap)
        assert rqb.message_set == ('UID', 'EXPUNGE', '101,103')
        assert imap.request_target_uids(rqb) == [101, 103]

    def test_uid_search_carries_criteria_not_a_message_set(self):
        imap = make_imap()
        # 'ALL' is a search criterion, not a numeric set: it must not be parsed.
        imap, rqb = make_request('UID', 'SEARCH', None, 'ALL', imap=imap)
        assert rqb.message_set is None
        assert imap.request_target_uids(rqb) is None

    def test_expunge_after_request_keeps_frozen_target_uids(self):
        imap = make_imap()
        imap.uid_map.exists_set(3)
        for seq, uid in ((1, 101), (2, 102), (3, 103)):
            imap.uid_map.fetch(seq, uid)

        imap, rqb = make_request('STORE', '2', '+FLAGS', '(\\Deleted)', imap=imap)
        assert rqb.message_set == ('SEQ', 'STORE', '2')
        assert rqb.message_uids == [102]

        # A later EXPUNGE physically shifts sequence slots...
        imap.uid_map.expunge(1)
        assert imap.message_uid(1) == 102

        # ...but the batch originally named by the request is unchanged.
        assert rqb.message_set == ('SEQ', 'STORE', '2')
        assert imap.request_target_uids(rqb) == [102]

    def test_unmapped_slot_in_snapshot_is_undetermined(self):
        imap = make_imap()
        imap.uid_map.exists_set(3)
        imap.uid_map.fetch(1, 101)
        imap, rqb = make_request('FETCH', '1:3', '(UID)', imap=imap)

        # Slot 2/3 UIDs were never learned: undetermined, not guessed.
        assert imap.request_target_uids(rqb) == [101, None, None]

        # Learning the UID afterwards updates the live view but never
        # retroactively rewrites the frozen snapshot.
        imap.uid_map.fetch(2, 102)
        assert imap.message_uid(2) == 102
        assert imap.request_target_uids(rqb) == [101, None, None]

    def test_unresolved_star_is_undetermined(self):
        imap = make_imap()
        imap, rqb = make_request('FETCH', '1:*', '(UID)', imap=imap)
        assert imap.request_target_uids(rqb) is None

        imap.uid_map.exists_set(2)
        imap.uid_map.fetch(1, 101)
        imap.uid_map.fetch(2, 102)
        imap, rqb = make_request('FETCH', '1:*', '(UID)', imap=imap)
        assert imap.request_target_uids(rqb) == [101, 102]

    def test_request_without_message_set(self):
        imap = make_imap()
        imap, rqb = make_request('NOOP', imap=imap)
        assert rqb.message_set is None
        assert imap.request_target_uids(rqb) is None

    def test_malformed_set_never_blocks_the_command(self):
        imap = make_imap()
        # The raw set is still recorded verbatim, but resolution degrades
        # to undetermined instead of raising in the command path.
        imap, rqb = make_request('FETCH', 'not-a-number', '(UID)', imap=imap)
        assert rqb.message_set == ('SEQ', 'FETCH', 'not-a-number')
        assert imap.request_target_uids(rqb) is None

    def test_lookup_by_tag_while_pending(self):
        imap = make_imap()
        imap, rqb = make_request('UID', 'STORE', '55', '+FLAGS', '(X)', imap=imap)
        with imap.commands_lock:
            imap.tagged_commands[rqb.tag] = rqb

        assert imap.request_target_uids(rqb.tag) == [55]
        assert imap.request_target_uids(rqb.tag.decode('ASCII')) == [55]

        with imap.commands_lock:
            imap.tagged_commands.clear()
        assert imap.request_target_uids(rqb.tag) is None
