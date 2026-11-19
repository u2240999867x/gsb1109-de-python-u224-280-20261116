"""Solve for the classification of meta data fields after stacking.

When source DICOM data sets are stacked into a multi dimensional array, each
meta data field must land in one of the classification "buckets" used by
`dcmmeta.DcmMetaExtension`: a base of 'global', 'time', or 'vector' crossed
with a sub classification of 'const', 'samples', or 'slices'.

The `MetaSummary` class collects the meta data observed from each source
instance together with the mapping from instances to output voxels. It can
then find every classification scheme (an assignment of a classification to
each field) that jointly explains the observations, while respecting any
previously confirmed classifications and the allowed classifications.

The solver follows a few guiding principles:

* Identical sample values are not enough to promote a field to a global
  constant, and missing observations can not participate in proving
  constancy. A field is only a candidate for ('global', 'const') if every
  source instance provides a value and they are all equal.
* When more than one classification is consistent with the observations all
  of them are kept as candidates.
* Looking up a field by output index always leads back to the correct
  original instance(s), even after reordering the time or vector dimensions.
* Fields that can not be placed into any allowed classification are reported
  together with all of their conflicting observations. Sources are never
  dropped just to make a field fit into the extension.
"""
from collections import namedtuple
from itertools import product


CLASSIFICATIONS = (
    ('global', 'const'),
    ('global', 'slices'),
    ('time', 'samples'),
    ('time', 'slices'),
    ('vector', 'samples'),
    ('vector', 'slices'),
)
'''All possible meta data classifications, mirroring
`dcmmeta.DcmMetaExtension.classifications`.'''


class Conflict(namedtuple('Conflict',
                          ['field', 'classification', 'slot', 'observations',
                           'reason'])):
    '''Record of observations preventing a classification for a field.

    Attributes
    ----------
    field : str
        The meta data key.

    classification : tuple
        The classification that the observations conflict with.

    slot : int or None
        The slot (position in the values array for the classification) where
        the conflict was observed. None if the conflict is not tied to a
        particular slot.

    observations : tuple
        Tuples of (source, value) for every observation involved in the
        conflict. No source is left out.

    reason : str
        One of 'inconsistent', 'missing', or 'not allowed'.
    '''
    __slots__ = ()


class Lookup(namedtuple('Lookup', ['value', 'sources'])):
    '''The result of looking up a field at an output index.

    Attributes
    ----------
    value
        The meta data value at the queried index (None if unobserved).

    sources : tuple
        The original instances the value was observed in.
    '''
    __slots__ = ()


def valid_classifications(shape):
    '''Get the classifications that are valid for the given stack shape.

    Parameters
    ----------
    shape : tuple
        The (n_slices, n_times, n_vectors) shape of the stack.

    Returns
    -------
    valid : tuple
        The valid classifications, mirroring
        `dcmmeta.DcmMetaExtension.get_valid_classes`.
    '''
    n_slices, n_times, n_vectors = shape
    if n_times == 1 and n_vectors == 1:
        return CLASSIFICATIONS[:2]
    if n_vectors == 1:
        return CLASSIFICATIONS[:4]
    if n_times == 1:
        return CLASSIFICATIONS[:2] + CLASSIFICATIONS[4:]
    return CLASSIFICATIONS


def slot_shape(classification, shape):
    '''Get the shape of the values array for a classification.

    Parameters
    ----------
    classification : tuple
        The (base, sub) classification.

    shape : tuple
        The (n_slices, n_times, n_vectors) shape of the stack.

    Returns
    -------
    slot_shape : tuple
        The number of slots along each axis of the classification. The empty
        tuple for ('global', 'const').
    '''
    n_slices, n_times, n_vectors = shape
    base, sub = classification
    if classification == ('global', 'const'):
        return tuple()
    if sub == 'slices':
        if base == 'global':
            return (n_slices, n_times, n_vectors)
        if base == 'time':
            return (n_slices,)
        if base == 'vector':
            return (n_slices, n_times)
    else:
        if base == 'time':
            return (n_times, n_vectors)
        if base == 'vector':
            return (n_vectors,)
    raise ValueError('Invalid classification: %s' % (classification,))


def slot_count(classification, shape):
    '''Get the number of values for a classification (its multiplicity).'''
    result = 1
    for dim_size in slot_shape(classification, shape):
        result *= dim_size
    return result


def slot_index(classification, index, shape):
    '''Map an output index to the slot for the given classification.

    Parameters
    ----------
    classification : tuple
        The (base, sub) classification.

    index : tuple
        The (slice, time, vector) output index.

    shape : tuple
        The (n_slices, n_times, n_vectors) shape of the stack.

    Returns
    -------
    slot : int
        The index into the values array for the classification, using the
        same layout as `dcmmeta.DcmMetaExtension`.
    '''
    slc_idx, time_idx, vec_idx = index
    n_slices, n_times, n_vectors = shape
    base, sub = classification
    if classification == ('global', 'const'):
        return 0
    if classification == ('global', 'slices'):
        return slc_idx + n_slices * (time_idx + n_times * vec_idx)
    if classification == ('time', 'samples'):
        return time_idx + n_times * vec_idx
    if classification == ('time', 'slices'):
        return slc_idx
    if classification == ('vector', 'samples'):
        return vec_idx
    if classification == ('vector', 'slices'):
        return slc_idx + n_slices * time_idx
    raise ValueError('Invalid classification: %s' % (classification,))


def _vals_equal(val1, val2):
    '''Test two meta data values for equality.'''
    eq = val1 == val2
    if isinstance(eq, bool):
        return eq
    # Array like values (e.g. numpy arrays) produce an array of comparisons
    return bool(eq.all())


class MetaSummary(object):
    '''Summarize meta data from source instances and solve classifications.

    Parameters
    ----------
    shape : tuple, optional
        The (n_slices, n_times, n_vectors) shape of the stack. If None it is
        inferred from the instance indices that are added.

    allowed : sequence, optional
        The allowed classifications. If None, all classifications that are
        valid for the stack shape are allowed.

    instances : sequence, optional
        Each element is a tuple of (index, meta) or (index, meta, source)
        which is passed to `add`.

    confirmed : dict, optional
        A mapping from meta data keys to their confirmed classification.
    '''

    def __init__(self, shape=None, allowed=None, instances=None,
                 confirmed=None):
        if shape is not None:
            shape = tuple(shape)
            if len(shape) != 3:
                raise ValueError("The shape must be "
                                 "(n_slices, n_times, n_vectors)")
        self._shape = shape
        if allowed is not None:
            allowed = tuple(allowed)
            for classification in allowed:
                if classification not in CLASSIFICATIONS:
                    raise ValueError('Invalid classification: %s'
                                     % (classification,))
        self._allowed = allowed
        self._indices = []
        self._sources = []
        self._obs = {}
        self._confirmed = {}
        if instances is not None:
            for inst_info in instances:
                self.add(*inst_info)
        if confirmed is not None:
            for field, classification in confirmed.items():
                self.confirm(field, classification)

    @property
    def shape(self):
        '''The (n_slices, n_times, n_vectors) shape of the stack.'''
        if self._shape is not None:
            return self._shape
        if not self._indices:
            return (0, 0, 0)
        return tuple(max(idx[dim] for idx in self._indices) + 1
                     for dim in range(3))

    @property
    def allowed(self):
        '''The allowed classifications for this summary.'''
        if self._allowed is not None:
            return self._allowed
        return valid_classifications(self.shape)

    @property
    def fields(self):
        '''The meta data keys that have been observed.'''
        return tuple(self._obs.keys())

    @property
    def instances(self):
        '''A tuple of (source, index) for each added instance.'''
        return tuple(zip(self._sources, self._indices))

    @property
    def confirmed(self):
        '''Mapping of meta data keys to confirmed classifications.'''
        return dict(self._confirmed)

    def add(self, index, meta, source=None):
        '''Add the meta data observed from a single source instance.

        Parameters
        ----------
        index : tuple
            The (slice, time, vector) output index for the instance.

        meta : dict
            The meta data observed from the instance. Keys that are absent
            (or have a value of None) are treated as missing observations.

        source : optional
            A hashable identifying the original instance. If None, the
            number of previously added instances is used.

        Returns
        -------
        source
            The source identifier used for the instance.
        '''
        index = tuple(index)
        if len(index) != 3:
            raise ValueError('The index must be (slice, time, vector)')
        if self._shape is not None:
            for dim, idx_val in enumerate(index):
                if not 0 <= idx_val < self._shape[dim]:
                    raise IndexError('Index %s is out of bounds for shape %s'
                                     % (index, self._shape))
        inst_idx = len(self._indices)
        if source is None:
            source = inst_idx
        self._indices.append(index)
        self._sources.append(source)
        for field, values in self._obs.items():
            values.append(meta.get(field))
        for field, value in meta.items():
            if field not in self._obs:
                self._obs[field] = [None] * inst_idx + [value]
        return source

    def confirm(self, field, classification):
        '''Confirm the classification for a meta data key.

        Parameters
        ----------
        field : str
            The meta data key.

        classification : tuple
            The confirmed (base, sub) classification.
        '''
        if classification not in CLASSIFICATIONS:
            raise ValueError('Invalid classification: %s'
                             % (classification,))
        self._confirmed[field] = classification

    def _is_consistent(self, field, classification):
        '''Check if a classification is consistent with the observations.'''
        values = self._obs[field]
        if classification == ('global', 'const'):
            #Missing observations can not prove constancy, and identical
            #sample values alone are not enough either: every instance must
            #provide a value and they must all be equal.
            if not values or any(val is None for val in values):
                return False
            first_val = values[0]
            return all(_vals_equal(first_val, val) for val in values[1:])
        slot_vals = {}
        for inst_idx, value in enumerate(values):
            if value is None:
                continue
            slot = slot_index(classification, self._indices[inst_idx],
                              self.shape)
            if slot in slot_vals:
                if not _vals_equal(slot_vals[slot], value):
                    return False
            else:
                slot_vals[slot] = value
        return True

    def candidates(self, field):
        '''Get the classifications that explain the observations for a field.

        Parameters
        ----------
        field : str
            The meta data key.

        Returns
        -------
        candidates : tuple
            Every allowed classification that is consistent with the
            observations (and any confirmed classification). May be empty if
            the field can not be classified, see `conflicts`.
        '''
        if field not in self._obs:
            raise KeyError('Unknown meta data key: %s' % field)
        confirmed = self._confirmed.get(field)
        if confirmed is not None:
            if confirmed not in self.allowed:
                return tuple()
            check = (confirmed,)
        else:
            check = self.allowed
        return tuple(classification for classification in check
                     if self._is_consistent(field, classification))

    def classification_conflicts(self, field, classification):
        '''Get the observations conflicting with a classification.

        Parameters
        ----------
        field : str
            The meta data key.

        classification : tuple
            The (base, sub) classification to check.

        Returns
        -------
        conflicts : tuple
            A `Conflict` for each slot with conflicting observations. Every
            observation involved is included, no source is dropped.
        '''
        if field not in self._obs:
            raise KeyError('Unknown meta data key: %s' % field)
        if classification not in CLASSIFICATIONS:
            raise ValueError('Invalid classification: %s'
                             % (classification,))
        values = self._obs[field]
        result = []
        if classification == ('global', 'const'):
            missing = tuple((source, None)
                            for source, value in zip(self._sources, values)
                            if value is None)
            if missing:
                result.append(Conflict(field, classification, 0, missing,
                                       'missing'))
            observed = tuple((source, value)
                             for source, value in zip(self._sources, values)
                             if value is not None)
            distinct = []
            for source, value in observed:
                for group in distinct:
                    if _vals_equal(group[0][1], value):
                        group.append((source, value))
                        break
                else:
                    distinct.append([(source, value)])
            if len(distinct) > 1:
                result.append(Conflict(field, classification, 0, observed,
                                       'inconsistent'))
            return tuple(result)
        slot_obs = {}
        for inst_idx, value in enumerate(values):
            if value is None:
                continue
            slot = slot_index(classification, self._indices[inst_idx],
                              self.shape)
            slot_obs.setdefault(slot, []).append(
                (self._sources[inst_idx], value))
        for slot, observations in sorted(slot_obs.items()):
            first_val = observations[0][1]
            if any(not _vals_equal(first_val, value)
                   for _, value in observations[1:]):
                result.append(Conflict(field, classification, slot,
                                       tuple(observations), 'inconsistent'))
        return tuple(result)

    def conflicts(self, field=None):
        '''Get conflicts for fields that have no valid classification.

        Parameters
        ----------
        field : str, optional
            Only get conflicts for this meta data key. By default conflicts
            for all unclassifiable fields are returned.

        Returns
        -------
        conflicts : dict
            Maps each unclassifiable meta data key to a tuple of `Conflict`
            records covering every allowed (or confirmed) classification
            that the observations rule out.
        '''
        if field is not None:
            fields = (field,)
        else:
            fields = self.fields
        result = {}
        for curr_field in fields:
            if self.candidates(curr_field):
                continue
            confirmed = self._confirmed.get(curr_field)
            if confirmed is not None and confirmed not in self.allowed:
                result[curr_field] = (Conflict(curr_field, confirmed, None,
                                               tuple(), 'not allowed'),)
                continue
            check = (confirmed,) if confirmed is not None else self.allowed
            field_conflicts = []
            for classification in check:
                field_conflicts.extend(
                    self.classification_conflicts(curr_field, classification))
            result[curr_field] = tuple(field_conflicts)
        return result

    @property
    def conflicted_fields(self):
        '''The meta data keys with no valid classification.'''
        return tuple(field for field in self.fields
                     if not self.candidates(field))

    def schemes(self):
        '''Generate every classification scheme that explains the observations.

        Yields
        ------
        scheme : dict
            A mapping from meta data keys to classifications. Each scheme
            jointly explains all of the observations. Fields with no valid
            classification are left out, see `conflicts`.
        '''
        fields = tuple(field for field in self.fields
                       if self.candidates(field))
        all_cands = [self.candidates(field) for field in fields]
        for combo in product(*all_cands):
            yield dict(zip(fields, combo))

    @property
    def n_schemes(self):
        '''The number of classification schemes that will be generated.'''
        result = 1
        for field in self.fields:
            cands = self.candidates(field)
            if cands:
                result *= len(cands)
        return result

    def get_values(self, field, classification=None):
        '''Get the values array for a field under a classification.

        Parameters
        ----------
        field : str
            The meta data key.

        classification : tuple, optional
            The classification to use. If None, the first candidate from
            `candidates` is used.

        Returns
        -------
        values
            The values in the same layout used by
            `dcmmeta.DcmMetaExtension`. A single value for
            ('global', 'const'), otherwise a list with one element per slot
            (None for slots with no observation).
        '''
        if classification is None:
            cands = self.candidates(field)
            if not cands:
                raise ValueError('No valid classification for field: %s'
                                 % field)
            classification = cands[0]
        if classification == ('global', 'const'):
            for value in self._obs[field]:
                if value is not None:
                    return value
            return None
        result = [None] * slot_count(classification, self.shape)
        for inst_idx, value in enumerate(self._obs[field]):
            if value is None:
                continue
            slot = slot_index(classification, self._indices[inst_idx],
                              self.shape)
            result[slot] = value
        return result

    def sources_at(self, index):
        '''Get the original instances mapped to the given output index.'''
        index = tuple(index)
        return tuple(source for source, inst_index in self.instances
                     if inst_index == index)

    def lookup(self, field, index, classification=None):
        '''Look up a field at an output index, back to the original instances.

        Parameters
        ----------
        field : str
            The meta data key.

        index : tuple
            The (slice, time, vector) output index.

        classification : tuple, optional
            The classification to use. If None, the first candidate from
            `candidates` is used.

        Returns
        -------
        result : Lookup
            The value at the queried index and the original instances the
            value was observed in.
        '''
        if classification is None:
            cands = self.candidates(field)
            if not cands:
                raise ValueError('No valid classification for field: %s'
                                 % field)
            classification = cands[0]
        slot = slot_index(classification, tuple(index), self.shape)
        value = None
        sources = []
        for inst_idx, inst_index in enumerate(self._indices):
            inst_value = self._obs[field][inst_idx]
            if inst_value is None:
                continue
            if slot_index(classification, inst_index, self.shape) == slot:
                value = inst_value
                sources.append(self._sources[inst_idx])
        return Lookup(value, tuple(sources))

    def reorder(self, time_perm=None, vector_perm=None):
        '''Create a reordered copy of this summary.

        Parameters
        ----------
        time_perm : sequence, optional
            A permutation of the time points, where new position `i` takes
            the values from the old index `time_perm[i]`.

        vector_perm : sequence, optional
            A permutation of the vector components, where new position `i`
            takes the values from the old index `vector_perm[i]`.

        Returns
        -------
        result : MetaSummary
            A new summary where the instance indices, and thus all value
            arrays and lookups, follow the new ordering. Looking up a field
            at a reordered index still leads back to the same original
            instances.
        '''
        n_slices, n_times, n_vectors = self.shape
        if time_perm is None:
            time_perm = tuple(range(n_times))
        else:
            time_perm = tuple(time_perm)
            if sorted(time_perm) != list(range(n_times)):
                raise ValueError('Invalid time permutation: %s'
                                 % (time_perm,))
        if vector_perm is None:
            vector_perm = tuple(range(n_vectors))
        else:
            vector_perm = tuple(vector_perm)
            if sorted(vector_perm) != list(range(n_vectors)):
                raise ValueError('Invalid vector permutation: %s'
                                 % (vector_perm,))
        inv_time = [0] * n_times
        for new_idx, old_idx in enumerate(time_perm):
            inv_time[old_idx] = new_idx
        inv_vector = [0] * n_vectors
        for new_idx, old_idx in enumerate(vector_perm):
            inv_vector[old_idx] = new_idx
        result = MetaSummary((n_slices, n_times, n_vectors), self._allowed)
        result._confirmed = dict(self._confirmed)
        for inst_idx, (slc_idx, time_idx, vec_idx) in \
                enumerate(self._indices):
            new_index = (slc_idx, inv_time[time_idx], inv_vector[vec_idx])
            meta = dict((field, values[inst_idx])
                        for field, values in self._obs.items()
                        if values[inst_idx] is not None)
            result.add(new_index, meta, self._sources[inst_idx])
        return result
