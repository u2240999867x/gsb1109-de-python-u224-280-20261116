
from smb.leasecache import LeaseCache, LeaseCacheEntry
from smb.smb2_constants import (
    SMB2_LEASE_NONE,
    SMB2_LEASE_READ_CACHING,
    SMB2_LEASE_HANDLE_CACHING,
    SMB2_LEASE_WRITE_CACHING,
)


LEASE_KEY = b'\x11' * 16
RW_LEASE = SMB2_LEASE_READ_CACHING | SMB2_LEASE_WRITE_CACHING


def make_cache():
    cache = LeaseCache()
    cache.lease_acquired(LEASE_KEY, RW_LEASE)
    return cache


def test_lookup_miss_without_observation():
    cache = make_cache()
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) is None


def test_record_and_lookup_attributes():
    cache = make_cache()
    entry = cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 10 })
    assert entry.is_current
    hit = cache.lookup_attributes(LEASE_KEY, '/dir/file.txt')
    assert hit is entry
    assert hit.value == { 'size': 10 }


def test_record_and_lookup_data():
    cache = make_cache()
    entry = cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123456789')
    assert entry.is_current
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 10) == b'0123456789'
    # subranges of a cached chunk are served as well
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 4, 3) == b'456'
    # ranges not fully covered by a cached chunk miss
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 8, 4) is None


def test_large_chunks_bypass_cache():
    cache = LeaseCache(max_cached_chunk_size = 8)
    cache.lease_acquired(LEASE_KEY, RW_LEASE)
    assert cache.record_data(LEASE_KEY, '/f', 0, b'123456789') is None
    assert cache.lookup_data(LEASE_KEY, '/f', 0, 9) is None
    assert cache.record_data(LEASE_KEY, '/f', 0, b'12345678') is not None
    assert cache.lookup_data(LEASE_KEY, '/f', 0, 8) == b'12345678'


def test_revocation_stops_serving_current_data():
    cache = make_cache()
    cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 10 })
    cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123')

    cache.lease_revoked(LEASE_KEY)

    # Old observations are no longer treated as current...
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) is None
    # ...but remain available in the archive
    archived = cache.archived_entries(LEASE_KEY)
    assert len(archived) == 2
    assert all(not e.is_current for e in archived)
    kinds = sorted(e.kind for e in archived)
    assert kinds == [ LeaseCacheEntry.KIND_ATTRIBUTES, LeaseCacheEntry.KIND_DATA ]


def test_observation_without_valid_lease_is_archived_not_current():
    cache = make_cache()
    cache.lease_revoked(LEASE_KEY)
    # Data fetched from the server after revocation (revalidation) must not be
    # served from cache while no caching lease is held.
    entry = cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 20 })
    assert not entry.is_current
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert cache.archived_entries(LEASE_KEY) == [ entry ]


def test_reacquisition_does_not_revalidate_old_data():
    cache = make_cache()
    cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 10 })
    cache.lease_revoked(LEASE_KEY)

    cache.lease_acquired(LEASE_KEY, RW_LEASE)

    # The new lease does not prove that the old observations are still valid
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert len(cache.archived_entries(LEASE_KEY)) == 1

    # Only fresh observations under the new lease become current
    entry = cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 30 })
    assert entry.is_current
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt').value == { 'size': 30 }


def test_reacquisition_without_break_does_not_revalidate_old_data():
    cache = make_cache()
    cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 10 })
    # Even if the break notification was never processed, re-acquiring the
    # lease must not make the old observations current again.
    cache.lease_acquired(LEASE_KEY, RW_LEASE)
    assert cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert len(cache.archived_entries(LEASE_KEY)) == 1


def test_lease_without_caching_bits_disables_caching():
    cache = LeaseCache()
    cache.lease_acquired(LEASE_KEY, SMB2_LEASE_NONE)
    entry = cache.record_attributes(LEASE_KEY, '/f', { 'size': 1 })
    assert not entry.is_current
    assert cache.lookup_attributes(LEASE_KEY, '/f') is None


def test_handle_caching_lease_enables_caching():
    cache = LeaseCache()
    cache.lease_acquired(LEASE_KEY, SMB2_LEASE_HANDLE_CACHING)
    entry = cache.record_attributes(LEASE_KEY, '/f', { 'size': 1 })
    assert entry.is_current


def test_inflight_read_completing_after_revocation_is_archived():
    cache = make_cache()
    token = cache.begin_read(LEASE_KEY)

    # Server revokes the lease while the read is on the wire
    cache.lease_revoked(LEASE_KEY)

    # The response arrives now: it is a valid historical observation, but it
    # must not be treated as current data.
    entry = cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123', token = token)
    assert not entry.is_current
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) is None
    assert cache.archived_entries(LEASE_KEY) == [ entry ]


def test_inflight_read_completing_after_reacquisition_is_archived():
    cache = make_cache()
    token = cache.begin_read(LEASE_KEY)

    cache.lease_revoked(LEASE_KEY)
    cache.lease_acquired(LEASE_KEY, RW_LEASE)

    # The read was issued under the old lease epoch; the new lease does not
    # prove its data is still valid.
    entry = cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123', token = token)
    assert not entry.is_current
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) is None

    # A read issued under the new epoch becomes current
    token2 = cache.begin_read(LEASE_KEY)
    entry2 = cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'4567', token = token2)
    assert entry2.is_current
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) == b'4567'


def test_inflight_read_completing_before_revocation_stays_current():
    cache = make_cache()
    token = cache.begin_read(LEASE_KEY)
    entry = cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123', token = token)
    assert entry.is_current
    assert cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) == b'0123'


def test_superseded_observation_is_archived():
    cache = make_cache()
    old = cache.record_attributes(LEASE_KEY, '/f', { 'size': 1 })
    new = cache.record_attributes(LEASE_KEY, '/f', { 'size': 2 })
    assert not old.is_current
    assert new.is_current
    assert cache.lookup_attributes(LEASE_KEY, '/f').value == { 'size': 2 }
    assert cache.archived_entries(LEASE_KEY) == [ old ]


def test_revocation_is_scoped_to_lease_key():
    other_key = b'\x22' * 16
    cache = make_cache()
    cache.lease_acquired(other_key, RW_LEASE)
    cache.record_attributes(LEASE_KEY, '/a', { 'size': 1 })
    cache.record_attributes(other_key, '/b', { 'size': 2 })

    cache.lease_revoked(LEASE_KEY)

    assert cache.lookup_attributes(LEASE_KEY, '/a') is None
    assert cache.lookup_attributes(other_key, '/b') is not None
    assert len(cache.archived_entries(LEASE_KEY)) == 1
    assert cache.archived_entries(other_key) == [ ]
