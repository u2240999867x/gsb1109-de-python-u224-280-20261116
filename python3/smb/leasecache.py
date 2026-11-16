
import time

from .smb2_constants import SMB2_LEASE_READ_CACHING, SMB2_LEASE_HANDLE_CACHING, SMB2_LEASE_WRITE_CACHING


class LeaseCacheEntry:
    """
    A single observation of a remote file, recorded while a lease was held.

    An entry is current only while the lease epoch under which it was recorded
    is still the active epoch for its lease key. Once the server revokes the
    lease (or a new lease is acquired), the entry is archived: it remains
    available as a historical observation but is never served as current data.
    """

    KIND_ATTRIBUTES = 'attributes'
    KIND_DATA = 'data'

    def __init__(self, lease_key, kind, path, value, epoch, offset = 0, observed_at = None):
        self.lease_key = lease_key
        self.kind = kind
        self.path = path
        self.offset = offset
        self.value = value
        self.epoch = epoch
        self.observed_at = time.time() if observed_at is None else observed_at
        self.is_current = False

    def __repr__(self):
        return '<LeaseCacheEntry kind=%s path=%s offset=%d epoch=%d current=%s observed_at=%.3f>' \
               % ( self.kind, self.path, self.offset, self.epoch, self.is_current, self.observed_at )


class _LeaseState:

    __slots__ = ( 'epoch', 'is_valid' )

    def __init__(self):
        self.epoch = 0        #: Bumped on every lease state transition; observations are tagged with the epoch at record time
        self.is_valid = False #: True only while a caching lease is held


class LeaseCache:
    """
    Lease-aware cache for shared file attributes and small content chunks.

    Observations (file attributes and data chunks no larger than
    max_cached_chunk_size) are served from the cache only while the lease
    under which they were recorded remains in force. When the server revokes
    the lease (see lease_revoked), all current observations for the lease key
    are archived: they stay queryable via archived_entries as historical
    observations, but lookups no longer return them, so new requests must be
    revalidated against the server.

    In-flight requests are handled via begin_read: a read that was issued
    before a revocation but completes after it is archived rather than
    installed as current, even if a new lease has already been acquired in
    the meantime. Acquiring a lease (lease_acquired) always starts a new
    epoch and never re-validates observations from earlier epochs.
    """

    def __init__(self, max_cached_chunk_size = 4096):
        self.max_cached_chunk_size = max_cached_chunk_size
        self._leases = { }     #: lease key mapped to _LeaseState instance
        self._current = { }    #: (lease key, kind, path, offset) mapped to current LeaseCacheEntry instance
        self._archive = [ ]    #: revoked or superseded LeaseCacheEntry instances, oldest first
        self._inflight = { }   #: token mapped to (lease key, epoch) captured by begin_read
        self._next_token = 1

    #
    # Lease lifecycle
    #

    def lease_acquired(self, lease_key, lease_state):
        """
        Records that a lease has been (re-)granted for lease_key.

        A new epoch is started; observations recorded under earlier epochs are
        archived and are not re-validated by the new lease. Caching is enabled
        only if lease_state contains at least one of the SMB2_LEASE_xxx_CACHING
        bits.

        @return: the new epoch value
        """
        state = self._stateFor(lease_key)
        self._archiveCurrent(lease_key)
        state.epoch += 1
        state.is_valid = (lease_state & (SMB2_LEASE_READ_CACHING | SMB2_LEASE_HANDLE_CACHING | SMB2_LEASE_WRITE_CACHING)) != 0
        return state.epoch

    def lease_revoked(self, lease_key):
        """
        Records that the server has revoked the lease for lease_key (e.g. upon
        a lease break notification). All current observations for the lease key
        are archived and will no longer be served as current data.

        @return: the new epoch value
        """
        state = self._stateFor(lease_key)
        self._archiveCurrent(lease_key)
        state.epoch += 1
        state.is_valid = False
        return state.epoch

    def is_caching(self, lease_key):
        """
        @return: True if a caching lease is currently held for lease_key
        """
        return self._stateFor(lease_key).is_valid

    #
    # In-flight request tracking
    #

    def begin_read(self, lease_key):
        """
        Marks the start of an in-flight read or query under the current lease
        epoch. The returned token should be passed to record_attributes or
        record_data when the response arrives; if the lease has transitioned
        since (revoked or re-acquired), the observation will be archived
        instead of being treated as current.

        @return: an opaque token identifying the in-flight request
        """
        state = self._stateFor(lease_key)
        token = self._next_token
        self._next_token += 1
        self._inflight[token] = (lease_key, state.epoch)
        return token

    #
    # Recording observations
    #

    def record_attributes(self, lease_key, path, attributes, token = None):
        """
        Records an observation of the attributes of the file at path.

        @param token: optional token returned by begin_read for the in-flight request
        @return: the LeaseCacheEntry instance; its is_current attribute indicates
                 whether the observation is now served as current data
        """
        return self._record(lease_key, LeaseCacheEntry.KIND_ATTRIBUTES, path, 0, attributes, token)

    def record_data(self, lease_key, path, offset, data, token = None):
        """
        Records an observation of a content chunk of the file at path.
        Chunks larger than max_cached_chunk_size are not cached.

        @param token: optional token returned by begin_read for the in-flight request
        @return: the LeaseCacheEntry instance, or None if the chunk is too large to cache
        """
        if len(data) > self.max_cached_chunk_size:
            return None
        return self._record(lease_key, LeaseCacheEntry.KIND_DATA, path, offset, bytes(data), token)

    #
    # Lookups (current data only)
    #

    def lookup_attributes(self, lease_key, path):
        """
        @return: the current LeaseCacheEntry for the attributes of the file at
                 path, or None if no current observation exists (the request
                 must then be revalidated against the server)
        """
        return self._current.get((lease_key, LeaseCacheEntry.KIND_ATTRIBUTES, path, 0))

    def lookup_data(self, lease_key, path, offset, length):
        """
        @return: a string of length bytes of current file content starting at
                 offset, or None if no current cached chunk covers the range
        """
        for entry in self._current.values():
            if entry.kind == LeaseCacheEntry.KIND_DATA \
               and entry.lease_key == lease_key and entry.path == path \
               and entry.offset <= offset and offset + length <= entry.offset + len(entry.value):
                start = offset - entry.offset
                return entry.value[start:start+length]
        return None

    #
    # Archive access
    #

    def archived_entries(self, lease_key = None):
        """
        @return: a list of archived (no longer current) observations, oldest
                 first. If lease_key is given, only observations recorded under
                 that lease key are returned.
        """
        if lease_key is None:
            return list(self._archive)
        return [ e for e in self._archive if e.lease_key == lease_key ]

    def current_entries(self, lease_key = None):
        """
        @return: a list of current observations. If lease_key is given, only
                 observations recorded under that lease key are returned.
        """
        if lease_key is None:
            return list(self._current.values())
        return [ e for e in self._current.values() if e.lease_key == lease_key ]

    #
    # Internals
    #

    def _stateFor(self, lease_key):
        state = self._leases.get(lease_key)
        if state is None:
            state = self._leases[lease_key] = _LeaseState()
        return state

    def _record(self, lease_key, kind, path, offset, value, token):
        state = self._stateFor(lease_key)
        if token is not None:
            token_lease_key, epoch = self._inflight.pop(token, (lease_key, state.epoch))
            if token_lease_key != lease_key:
                epoch = state.epoch
        else:
            epoch = state.epoch

        entry = LeaseCacheEntry(lease_key, kind, path, value, epoch, offset)
        if state.is_valid and epoch == state.epoch:
            self._installCurrent(entry)
        else:
            # The lease transitioned while the request was in flight, or no
            # caching lease is held: keep the observation in the archive but
            # do not treat it as current.
            self._archive.append(entry)
        return entry

    def _installCurrent(self, entry):
        key = (entry.lease_key, entry.kind, entry.path, entry.offset)
        old = self._current.get(key)
        if old is not None:
            old.is_current = False
            self._archive.append(old)
        entry.is_current = True
        self._current[key] = entry

    def _archiveCurrent(self, lease_key):
        for key, entry in list(self._current.items()):
            if key[0] == lease_key:
                entry.is_current = False
                self._archive.append(entry)
                del self._current[key]
