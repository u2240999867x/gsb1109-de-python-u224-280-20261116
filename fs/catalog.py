"""Cost-aware collection of resource catalog metadata.

This module turns a catalog request -- a list of paths plus the fields to
record (size, times, permissions) -- into the cheapest set of backend
requests, executes it, and returns a manifest that points every field
back to the request and version basis that produced it.

The model deliberately keeps three concerns apart:

* :func:`plan` chooses requests.  It searches backend assignments and
  path distributions across backends, including serving one path's
  namespaces from different backends, and merges paths into full batches.
  The returned :class:`Plan` reports whether the search was exact.
* :func:`execute` runs a plan against a :class:`Transport` and records
  what each request returned.
* :class:`Manifest` reports whether the fields for a path form one
  coherent record.  Fields that cannot be tied to a single version are
  delivered as *independent observations* instead of being merged into a
  complete record.

Typical use::

    from fs.catalog import Backend, collect

    backends = [
        Backend("fast", ["details", "access"], max_batch=100,
                batch_cost=2, per_path_cost=1,
                supports_consistency_proof=True),
        Backend("slow", ["access"], max_batch=0,
                batch_cost=5, per_path_cost=3),
    ]
    manifest = collect(paths, fields=("size", "permissions"),
                       backends=backends, transport=my_transport)

    for path in manifest.paths():
        if manifest.is_complete(path):
            publish(manifest.publishable_record(path))
        else:
            publish_observations(path, manifest.independent_fields(path))

Cached values
-------------

:class:`CacheEntry` carries both the observed resource ``version`` and the
backend ``compatibility`` generation that produced it.  An entry reduces
request cost only when its compatibility matches a backend that serves the
field's namespace.  Optionally, :func:`plan` accepts ``current_versions``;
when supplied, an entry must also match the path's current version.

Version changes during execution
--------------------------------

:func:`collect` compares per-path version tokens across responses.  A path
whose fields were observed at different versions is re-planned from
scratch.  Values for unrelated paths that were already verified are
carried into the new plan as cache entries, so only affected paths are
requested again.

A backend that cannot prove cross-request consistency still yields a
coherent record when all of a path's fields came from one atomic request.
When its values are split across requests, those fields are exposed through
:meth:`Manifest.independent_fields` and :meth:`Manifest.is_complete`
returns ``False``.
"""

from __future__ import absolute_import, print_function, unicode_literals

import datetime
import hashlib
import itertools
import json
import typing
from collections import OrderedDict

try:
    from collections.abc import Mapping as MappingABC
except ImportError:  # pragma: no cover
    from collections import Mapping as MappingABC

from .errors import FSError

if typing.TYPE_CHECKING:
    from typing import (  # noqa: F401
        Any,
        Dict,
        Iterable,
        List,
        Mapping,
        Optional,
        Sequence,
        Text,
        Tuple,
    )


__all__ = [
    "BACKEND_NAMESPACES",
    "DEFAULT_FIELDS",
    "FIELD_NAMESPACES",
    "Backend",
    "BatchRequest",
    "CacheEntry",
    "CatalogError",
    "FSInfoTransport",
    "FieldValue",
    "Manifest",
    "Plan",
    "PlanConflict",
    "RequestRecord",
    "Transport",
    "TransportResponse",
    "VersionBasis",
    "collect",
    "execute",
    "plan",
    "replan",
    "verify_manifest",
]


#: Fields requested by default.
DEFAULT_FIELDS = ("size", "modified", "permissions")

#: Which info namespace carries each supported field.
FIELD_NAMESPACES = {
    "size": "details",
    "modified": "details",
    "created": "details",
    "accessed": "details",
    "type": "details",
    "permissions": "access",
    "user": "access",
    "group": "access",
    "uid": "access",
    "gid": "access",
}

#: Every namespace known to the model.
BACKEND_NAMESPACES = frozenset(FIELD_NAMESPACES.values())


class CatalogError(FSError):
    """Base class for catalog collection failures."""

    default_message = "catalog collection failed"


class PlanConflict(CatalogError):
    """Raised when no plan can satisfy the requested fields."""

    def __init__(self, message, paths=None):
        # type: (Text, Optional[Iterable[Text]]) -> None
        self.paths = list(paths or ())
        super(PlanConflict, self).__init__(msg=message)


def _normalize_paths(paths):
    # type: (Iterable[Text]) -> List[Text]
    seen = set()
    result = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _check_fields(fields):
    # type: (Iterable[Text]) -> List[Text]
    result = []
    for field in fields:
        if field not in FIELD_NAMESPACES:
            raise CatalogError("unknown field {!r}".format(field))
        if field not in result:
            result.append(field)
    return result


class Backend(object):
    """A metadata backend and its request economics.

    Arguments:
        name (str): identifier used in plans and manifests.
        namespaces (iterable): info namespaces this backend can serve in
            one request (for example ``("details", "access")``).
        max_batch (int): maximum number of paths in one request.  ``0``
            (the default) means the backend accepts any batch size.
        batch_cost (int): fixed price of each request.
        per_path_cost (int): additional price for each path in a request.
        supports_consistency_proof (bool): whether the backend can return
            a token that proves two *different* requests observed the
            same version.  A single atomic response is coherent even when
            this is false.
        version (str): backend implementation version.
        compatibility (str): compatibility generation.  Cache entries may
            only offset cost for a backend with the same value; it
            defaults to ``version``.

    """

    def __init__(
        self,
        name,
        namespaces,
        max_batch=0,
        batch_cost=1,
        per_path_cost=0,
        supports_consistency_proof=False,
        version="1",
        compatibility=None,
    ):
        # type: (Text, Iterable[Text], int, int, int, bool, Text,
        #        Optional[Text]) -> None
        if max_batch < 0:
            raise ValueError("max_batch must be >= 0")
        if batch_cost < 0 or per_path_cost < 0:
            raise ValueError("request costs must be >= 0")
        unknown = set(namespaces) - BACKEND_NAMESPACES
        if unknown:
            raise CatalogError(
                "backend {!r} declares unknown namespaces {}".format(
                    name, sorted(unknown)
                )
            )
        self.name = name
        self.namespaces = frozenset(namespaces)
        self.max_batch = max_batch
        self.batch_cost = batch_cost
        self.per_path_cost = per_path_cost
        self.supports_consistency_proof = bool(supports_consistency_proof)
        self.version = version
        self.compatibility = compatibility or version

    def __repr__(self):
        # type: () -> Text
        return "Backend({!r}, namespaces={!r}, max_batch={!r})".format(
            self.name, sorted(self.namespaces), self.max_batch
        )

    def supports(self, namespace):
        # type: (Text) -> bool
        """Return whether this backend serves ``namespace``."""
        return namespace in self.namespaces

    def supports_field(self, field):
        # type: (Text) -> bool
        """Return whether this backend can serve ``field``."""
        namespace = FIELD_NAMESPACES.get(field)
        if namespace is None:
            raise CatalogError("unknown field {!r}".format(field))
        return namespace in self.namespaces

    def batch_size(self, path_count):
        # type: (int) -> int
        """Largest batch this backend accepts for ``path_count`` paths."""
        if path_count <= 0:
            return 0
        if self.max_batch == 0:
            return path_count
        return min(self.max_batch, path_count)

    def request_count(self, path_count):
        # type: (int) -> int
        """Number of requests needed to serve ``path_count`` paths."""
        if path_count <= 0:
            return 0
        if self.max_batch == 0:
            return 1
        return (path_count + self.max_batch - 1) // self.max_batch

    def request_cost(self, path_count):
        # type: (int) -> int
        """Total cost of serving ``path_count`` paths, including batching."""
        if path_count <= 0:
            return 0
        return (
            self.batch_cost * self.request_count(path_count)
            + self.per_path_cost * path_count
        )

    def single_request_cost(self, path_count):
        # type: (int) -> int
        """Cost of one request carrying ``path_count`` paths."""
        if path_count <= 0:
            return 0
        return self.batch_cost + self.per_path_cost * path_count

    def compatible(self, entry):
        # type: (CacheEntry) -> bool
        """Return whether ``entry`` may offset this backend's cost."""
        return entry.compatibility == self.compatibility

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "name": self.name,
            "namespaces": sorted(self.namespaces),
            "max_batch": self.max_batch,
            "batch_cost": self.batch_cost,
            "per_path_cost": self.per_path_cost,
            "supports_consistency_proof": self.supports_consistency_proof,
            "version": self.version,
            "compatibility": self.compatibility,
        }


class CacheEntry(object):
    """A previously collected field value.

    ``version`` is the observed resource version (timestamp, etag, or
    whatever the backend reports).  ``compatibility`` identifies the
    backend generation that produced the value; it must match an available
    backend serving the field before the entry can reduce request cost.

    """

    __slots__ = [
        "path",
        "field",
        "value",
        "version",
        "compatibility",
        "request_id",
        "backend",
    ]

    def __init__(
        self,
        path,
        field,
        value,
        version,
        compatibility,
        request_id=None,
        backend=None,
    ):
        # type: (Text, Text, Any, Any, Text, Optional[Text], Optional[Text]) -> None
        if field not in FIELD_NAMESPACES:
            raise CatalogError("unknown field {!r}".format(field))
        self.path = path
        self.field = field
        self.value = value
        self.version = version
        self.compatibility = compatibility
        self.request_id = request_id
        self.backend = backend

    def __repr__(self):
        # type: () -> Text
        return "CacheEntry({!r}, {!r}, version={!r}, compatibility={!r})".format(
            self.path, self.field, self.version, self.compatibility
        )

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "path": self.path,
            "field": self.field,
            "value": self.value,
            "version": self.version,
            "compatibility": self.compatibility,
            "request_id": self.request_id,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> CacheEntry
        return cls(
            raw["path"],
            raw["field"],
            raw.get("value"),
            raw.get("version"),
            raw["compatibility"],
            raw.get("request_id"),
            raw.get("backend"),
        )


class FieldValue(object):
    """One observed field plus the evidence that supports it."""

    __slots__ = [
        "path",
        "field",
        "value",
        "version",
        "request_id",
        "backend",
        "compatibility",
        "source",
        "consistent",
        "proof",
    ]

    def __init__(
        self,
        path,
        field,
        value,
        version,
        request_id,
        backend,
        source,
        consistent,
        proof=False,
        compatibility=None,
    ):
        # type: (Text, Text, Any, Any, Text, Text, Text, bool, bool,
        #        Optional[Text]) -> None
        self.path = path
        self.field = field
        self.value = value
        self.version = version
        self.request_id = request_id
        self.backend = backend
        self.compatibility = compatibility
        self.source = source
        self.consistent = consistent
        self.proof = proof

    @property
    def is_independent_observation(self):
        # type: () -> bool
        """`bool`: whether this field is only an independent observation."""
        return not self.consistent

    def __repr__(self):
        # type: () -> Text
        return "FieldValue({!r}, {!r}, source={!r}, version={!r})".format(
            self.path, self.field, self.source, self.version
        )

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "path": self.path,
            "field": self.field,
            "value": self.value,
            "version": self.version,
            "request_id": self.request_id,
            "backend": self.backend,
            "compatibility": self.compatibility,
            "source": self.source,
            "consistent": self.consistent,
            "proof": self.proof,
        }

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> FieldValue
        return cls(
            raw["path"],
            raw["field"],
            raw.get("value"),
            raw.get("version"),
            raw["request_id"],
            raw.get("backend", ""),
            raw["source"],
            raw["consistent"],
            raw.get("proof", False),
            raw.get("compatibility"),
        )


class BatchRequest(object):
    """One request to one backend for a set of paths and namespaces."""

    __slots__ = ["request_id", "backend", "namespaces", "paths", "cost"]

    def __init__(self, request_id, backend, namespaces, paths, cost):
        # type: (Text, Backend, Sequence[Text], Sequence[Text], int) -> None
        self.request_id = request_id
        self.backend = backend
        self.namespaces = tuple(sorted(namespaces))
        self.paths = list(paths)
        self.cost = cost

    def __repr__(self):
        # type: () -> Text
        return "BatchRequest({!r}, backend={!r}, namespaces={!r}, paths={})".format(
            self.request_id, self.backend.name, self.namespaces, len(self.paths)
        )

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "request_id": self.request_id,
            "backend": self.backend.name,
            "namespaces": list(self.namespaces),
            "paths": list(self.paths),
            "cost": self.cost,
        }


class Plan(object):
    """A costed set of requests produced by :func:`plan`.

    ``cost`` is the sum of ``request.cost`` for the returned requests.
    ``exact`` is true when the planner proved that no cheaper plan exists
    under the modeled costs.  ``required`` maps each path to the
    namespaces still being requested for it after cache reuse.
    """

    __slots__ = [
        "fields",
        "paths",
        "backends",
        "required",
        "requests",
        "cost",
        "reused",
        "exact",
    ]

    def __init__(
        self, fields, paths, backends, required, requests, cost, reused, exact=True
    ):
        # type: (Sequence[Text], Sequence[Text], Sequence[Backend],
        #        Mapping[Text, Sequence[Text]], Sequence[BatchRequest],
        #        int, Sequence[CacheEntry], bool) -> None
        self.fields = list(fields)
        self.paths = list(paths)
        self.backends = list(backends)
        self.required = {
            path: tuple(sorted(namespaces))
            for path, namespaces in required.items()
        }
        self.requests = list(requests)
        self.cost = cost
        self.reused = list(reused)
        self.exact = exact

    def __repr__(self):
        # type: () -> Text
        return "Plan({} paths, {} requests, cost={})".format(
            len(self.paths), len(self.requests), self.cost
        )

    def requests_for(self, path):
        # type: (Text) -> List[BatchRequest]
        """Return every request that covers ``path``."""
        return [request for request in self.requests if path in request.paths]

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "fields": list(self.fields),
            "paths": list(self.paths),
            "backends": [backend.to_dict() for backend in self.backends],
            "required": {
                path: list(namespaces)
                for path, namespaces in self.required.items()
            },
            "requests": [request.to_dict() for request in self.requests],
            "cost": self.cost,
            "reused": [entry.to_dict() for entry in self.reused],
            "exact": self.exact,
        }

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> Plan
        backends = [
            Backend(
                item["name"],
                item["namespaces"],
                item.get("max_batch", 0),
                item.get("batch_cost", 1),
                item.get("per_path_cost", 0),
                item.get("supports_consistency_proof", False),
                item.get("version", "1"),
                item.get("compatibility"),
            )
            for item in raw["backends"]
        ]
        backend_map = {backend.name: backend for backend in backends}
        requests = [
            BatchRequest(
                item["request_id"],
                backend_map[item["backend"]],
                item["namespaces"],
                item["paths"],
                item["cost"],
            )
            for item in raw["requests"]
        ]
        required = raw.get("required")
        if required is None:
            required = {
                path: request.namespaces
                for request in requests
                for path in request.paths
            }
        return cls(
            raw["fields"],
            raw.get("paths", ()),
            backends,
            required,
            requests,
            raw["cost"],
            [CacheEntry.from_dict(item) for item in raw.get("reused", ())],
            raw.get("exact", True),
        )


def _cover_options(signature, backends):
    """Return the distinct backend subsets that can cover ``signature``.

    An option is a tuple of ``(backend, namespaces)`` pairs describing
    which namespaces one path sends to each backend.  Options are
    deduplicated by backend subset: the cost model only sees how many
    paths a backend serves, not which namespaces travel with them.
    """
    signature = frozenset(signature)
    namespace_list = sorted(signature)
    per_namespace = []
    for namespace in namespace_list:
        candidates = [
            backend for backend in backends if backend.supports(namespace)
        ]
        if not candidates:
            raise PlanConflict(
                "no backend serves namespace {!r}".format(namespace)
            )
        per_namespace.append(sorted(candidates, key=lambda backend: backend.name))

    options = OrderedDict()
    for combination in itertools.product(*per_namespace):
        grouped = OrderedDict()
        for namespace, backend in zip(namespace_list, combination):
            grouped.setdefault(backend.name, (backend, set()))[1].add(namespace)
        subset = frozenset(grouped)
        option = tuple(
            (backend, frozenset(namespaces))
            for backend, namespaces in sorted(
                grouped.values(), key=lambda item: item[0].name
            )
        )
        # A backend that would carry no namespace can never help, so the
        # assignment cannot contain one.
        if any(not namespaces for _, namespaces in option):
            continue
        if subset not in options:
            options[subset] = option

    # A cover that contains another cover's backend set is never cheaper:
    # the smaller cover already serves every namespace, so the extra
    # backends only add paths and requests.
    minimal = []
    subsets = list(options)
    for subset in subsets:
        if any(other < subset for other in subsets):
            continue
        minimal.append(options[subset])
    return minimal


def _allocations(total, parts):
    # type: (int, int) -> Iterator[Tuple[int, ...]]
    """Yield non-negative tuples of length ``parts`` summing to ``total``."""
    if parts == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for rest in _allocations(total - first, parts - 1):
            yield (first,) + rest


def _add_metric(backends, metric, before, after):
    # type: (Sequence[Backend], Tuple[int, int, int], Sequence[int],
    #        Sequence[int]) -> Tuple[int, int, int]
    cost, requests, unproven = metric
    for backend, old, new in zip(backends, before, after):
        if old == new:
            continue
        cost += backend.request_cost(new) - backend.request_cost(old)
        requests += backend.request_count(new) - backend.request_count(old)
        if old == 0 and new > 0 and not backend.supports_consistency_proof:
            unproven += 1
    return cost, requests, unproven


def _prune_states(states, limit=2500):
    """Drop states dominated by another state at the same layer.

    ``states`` maps a per-backend usage vector to ``(metric, previous,
    allocation)``.  Higher usage and a worse metric can never lead to a
    better final plan, because both request cost and request count are
    nondecreasing in each backend's path count.

    Dominance checking is quadratic, so it is skipped for large layers;
    keeping extra states only costs time, never optimality.
    """
    if len(states) > limit:
        return OrderedDict(states)
    items = sorted(states.items(), key=lambda item: (item[1][0], item[0]))
    kept = []
    kept_items = []
    for counts, value in items:
        metric = value[0]
        dominated = False
        for other_counts, other_metric in kept:
            if other_metric <= metric and all(
                old <= new for old, new in zip(other_counts, counts)
            ):
                dominated = True
                break
        if not dominated:
            kept.append((counts, metric))
            kept_items.append((counts, value))
    return OrderedDict(kept_items)


def _materialize(option_groups, allocations, backends):
    """Build requests from per-group option allocations."""
    backend_list = sorted(backends, key=lambda backend: backend.name)
    paths_by_backend = OrderedDict(
        (backend.name, []) for backend in backend_list
    )
    namespaces_by_backend = {backend.name: set() for backend in backend_list}
    for (signature, paths, options), allocation in zip(option_groups, allocations):
        cursor = 0
        for option, amount in zip(options, allocation):
            for _ in range(amount):
                path = paths[cursor]
                cursor += 1
                for backend, namespaces in option:
                    paths_by_backend[backend.name].append(path)
                    namespaces_by_backend[backend.name].update(namespaces)

    requests = []
    counter = itertools.count(1)
    for backend in backend_list:
        paths = paths_by_backend[backend.name]
        if not paths:
            continue
        namespaces = tuple(sorted(namespaces_by_backend[backend.name]))
        batch_size = backend.batch_size(len(paths))
        for start in range(0, len(paths), batch_size):
            chunk = paths[start : start + batch_size]
            requests.append(
                BatchRequest(
                    "r{}".format(next(counter)),
                    backend,
                    namespaces,
                    chunk,
                    backend.single_request_cost(len(chunk)),
                )
            )
    return requests


def _total_cost(counts, backends):
    return sum(
        backend.request_cost(count) for backend, count in zip(backends, counts)
    )


def _approximate_allocations(option_groups, backends):
    """Coordinate-descent fallback when the exact search is too large.

    Each group is assigned wholly to one cover option.  Groups are then
    re-optimized one at a time until no move improves the total, which is
    cheap and deterministic but not guaranteed optimal.
    """
    backend_list = sorted(backends, key=lambda backend: backend.name)
    index = {backend.name: position for position, backend in enumerate(backend_list)}

    choice = []
    counts = [0] * len(backend_list)
    for _signature, paths, options in option_groups:
        size = len(paths)
        best_index = min(
            range(len(options)),
            key=lambda position: (
                sum(
                    backend.request_cost(size)
                    for backend, _namespaces in options[position]
                ),
                tuple(
                    backend.name
                    for backend, _namespaces in options[position]
                ),
            ),
        )
        choice.append(best_index)
        for backend, _namespaces in options[best_index]:
            counts[index[backend.name]] += size

    for _round in range(100):
        improved = False
        for group_position, (_signature, paths, options) in enumerate(option_groups):
            size = len(paths)
            current = choice[group_position]
            for candidate in range(len(options)):
                if candidate == current:
                    continue
                trial = list(counts)
                for backend, _namespaces in options[current]:
                    trial[index[backend.name]] -= size
                for backend, _namespaces in options[candidate]:
                    trial[index[backend.name]] += size
                if _total_cost(trial, backend_list) < _total_cost(
                    counts, backend_list
                ):
                    counts = trial
                    choice[group_position] = candidate
                    current = candidate
                    improved = True
        if not improved:
            break

    allocations = []
    for (_signature, paths, options), selected in zip(option_groups, choice):
        allocation = [0] * len(options)
        allocation[selected] = len(paths)
        allocations.append(allocation)
    return allocations


def _plan_workload(workload, backends, work_limit=600000):
    """Return the cheapest requests for ``workload``.

    ``workload`` maps each path to the namespaces it still needs.  Paths
    with the same namespace signature are interchangeable for costing, so
    the planner groups them and runs a dynamic program over per-backend
    path counts.  A state is kept only if no other state has both lower
    usage and a lower (cost, requests, unproven-backend) metric, which
    preserves the optimum while keeping the search small.

    When the exact search would exceed ``work_limit`` transitions, the
    planner switches to a deterministic coordinate-descent fallback and
    reports ``exact=False``.  It never silently presents a heuristic plan
    as optimal.
    """
    namespace_order = sorted(
        {namespace for needed in workload.values() for namespace in needed}
    )
    if not namespace_order:
        return [], 0, True

    for namespace in namespace_order:
        if not any(backend.supports(namespace) for backend in backends):
            raise PlanConflict(
                "no backend serves namespace {!r} needed by {}".format(
                    namespace,
                    sorted(
                        path
                        for path, needed in workload.items()
                        if namespace in needed
                    ),
                )
            )

    groups = OrderedDict()
    for path in workload:
        signature = frozenset(workload[path])
        groups.setdefault(signature, []).append(path)

    backend_list = sorted(backends, key=lambda backend: backend.name)
    backend_index = {
        backend.name: index for index, backend in enumerate(backend_list)
    }

    option_groups = []
    for signature, paths in groups.items():
        options = _cover_options(signature, backend_list)
        if not options:
            raise PlanConflict(
                "no backend combination serves namespaces {}".format(
                    sorted(signature)
                )
            )
        option_groups.append((signature, paths, options))

    # Reject any group whose allocation list alone would be enormous.
    for _signature, paths, options in option_groups:
        if len(options) > 1:
            combinations = 1
            for step in range(1, len(options)):
                combinations = combinations * (len(paths) + step) // step
                if combinations > work_limit:
                    allocations = _approximate_allocations(
                        option_groups, backend_list
                    )
                    requests = _materialize(
                        option_groups, allocations, backend_list
                    )
                    return requests, sum(r.cost for r in requests), False

    zero = (0,) * len(backend_list)
    states = OrderedDict({zero: ((0, 0, 0), None, None)})
    layers = []
    for signature, paths, options in option_groups:
        size = len(paths)
        allocation_list = list(_allocations(size, len(options)))
        if len(states) * len(allocation_list) > work_limit:
            allocations = _approximate_allocations(option_groups, backend_list)
            requests = _materialize(option_groups, allocations, backend_list)
            return requests, sum(request.cost for request in requests), False

        new_states = OrderedDict()
        for counts, (metric, _, _) in states.items():
            for allocation in allocation_list:
                increments = [0] * len(backend_list)
                for option_index, amount in enumerate(allocation):
                    if not amount:
                        continue
                    for backend, _namespaces in options[option_index]:
                        increments[backend_index[backend.name]] += amount
                key = tuple(
                    count + increment
                    for count, increment in zip(counts, increments)
                )
                candidate = _add_metric(backend_list, metric, counts, key)
                if key not in new_states or candidate < new_states[key][0]:
                    new_states[key] = (candidate, counts, allocation)
        new_states = _prune_states(new_states)
        layers.append(new_states)
        states = new_states

    best_counts, (best_metric, _, _) = min(
        states.items(), key=lambda item: (item[1][0], item[0])
    )

    allocations = []
    counts = best_counts
    for layer in reversed(layers):
        _metric, previous, allocation = layer[counts]
        allocations.append(allocation)
        counts = previous
    allocations.reverse()

    requests = _materialize(option_groups, allocations, backend_list)
    return requests, best_metric[0], True


def plan(paths, fields=None, backends=None, cache=None, current_versions=None):
    # type: (Iterable[Text], Optional[Iterable[Text]], Iterable[Backend],
    #        Optional[Iterable[CacheEntry]],
    #        Optional[Mapping[Text, Any]]) -> Plan
    """Choose the cheapest set of requests for ``paths``.

    Arguments:
        paths (iterable): resource paths to collect.
        fields (iterable): fields to collect; defaults to
            :data:`DEFAULT_FIELDS`.
        backends (iterable): available :class:`Backend` objects.
        cache (iterable): previous :class:`CacheEntry` values.  An entry is
            reused only when its compatibility matches an available
            backend that serves the field.
        current_versions (mapping): optional ``path -> version`` map.  When
            given, a cache entry is reused only if its ``version`` matches
            the current value for that path.

    Returns:
        Plan: requests, their total cost, and the cache entries reused.

    Raises:
        PlanConflict: if a requested field has no serving backend.

    """
    fields = _check_fields(fields if fields is not None else DEFAULT_FIELDS)
    paths = _normalize_paths(paths)
    backends = list(backends or ())
    if not backends:
        raise PlanConflict("at least one backend is required")

    cache_index = {}
    for entry in cache or ():
        cache_index[(entry.path, entry.field)] = entry

    reused = []
    workload = OrderedDict()
    for path in paths:
        needed = []
        for field in fields:
            namespace = FIELD_NAMESPACES[field]
            candidates = [b for b in backends if b.supports(namespace)]
            entry = cache_index.get((path, field))
            if entry is not None and _cache_is_usable(
                entry, candidates, path, current_versions, backends
            ):
                if entry not in reused:
                    reused.append(entry)
                continue
            if namespace not in needed:
                needed.append(namespace)
        if needed:
            workload[path] = needed

    if workload:
        requests, cost, exact = _plan_workload(workload, backends)
    else:
        requests, cost, exact = [], 0, True

    return Plan(
        fields, paths, backends, workload, requests, cost, reused, exact
    )


def _cache_is_usable(
    entry, candidates, path, current_versions, all_backends
):
    # type: (CacheEntry, Sequence[Backend], Text,
    #        Optional[Mapping[Text, Any]], Sequence[Backend]) -> bool
    serving = candidates or all_backends
    if not serving:
        return False
    if not any(backend.compatible(entry) for backend in serving):
        return False
    if current_versions is not None:
        if path not in current_versions:
            return False
        if current_versions[path] != entry.version:
            return False
    return True


class TransportResponse(object):
    """Response returned by a :class:`Transport`.

    ``values`` maps ``path -> {field: value}``.  ``versions`` may map each
    path to the resource version observed in this response.  If it is
    omitted, ``version`` is used for every path in the batch.

    ``consistent`` may be set by a transport that inspected more than one
    response and can prove the batch reflects one coherent version.  It
    supplements, but never overrides, the backend's capability flag.
    """

    __slots__ = ["values", "versions", "version", "consistent"]

    def __init__(self, values, versions=None, version=None, consistent=None):
        # type: (Mapping[Text, Mapping[Text, Any]],
        #        Optional[Mapping[Text, Any]], Any,
        #        Optional[bool]) -> None
        self.values = values
        self.versions = dict(versions or {})
        self.version = version
        self.consistent = consistent


class Transport(object):
    """Interface used by :func:`execute`.

    Implementations receive the backend, the namespaces requested in the
    call and the batch of paths, and return a :class:`TransportResponse`
    (or a compatible mapping).
    """

    def request(self, backend, namespaces, paths):
        # type: (Backend, Sequence[Text], Sequence[Text]) -> TransportResponse
        raise NotImplementedError


class FSInfoTransport(Transport):
    """Transport that serves catalog fields from `FS` objects.

    ``filesystems`` maps a backend name to an `~fs.base.FS` instance.  Each
    request calls ``getinfo`` once per path with the requested namespaces
    and converts the resulting `~fs.info.Info` into raw field values.  The
    version token is the resource's ``modified`` time when the backend
    exposes one, otherwise the response has no token and fields from
    separate requests cannot be proven coherent.

    This adapter does not retry, cache or merge requests; those decisions
    belong to :func:`plan` and :func:`collect`.
    """

    _FIELD_GETTERS = {
        "size": lambda info: info.size,
        "modified": lambda info: info.modified,
        "created": lambda info: info.created,
        "accessed": lambda info: info.accessed,
        "type": lambda info: int(info.type) if info.has_namespace("details") else None,
        "permissions": lambda info: (
            info.permissions.as_str() if info.permissions is not None else None
        ),
        "user": lambda info: info.user,
        "group": lambda info: info.group,
        "uid": lambda info: info.uid,
        "gid": lambda info: info.gid,
    }

    def __init__(self, filesystems):
        # type: (Mapping[Text, Any]) -> None
        self.filesystems = dict(filesystems)

    def request(self, backend, namespaces, paths):
        # type: (Backend, Sequence[Text], Sequence[Text]) -> TransportResponse
        filesystem = self.filesystems[backend.name]
        requested = [namespace for namespace in namespaces]
        values = {}
        versions = {}
        for path in paths:
            info = filesystem.getinfo(path, namespaces=requested)
            path_values = {}
            for field in FIELD_NAMESPACES:
                if FIELD_NAMESPACES[field] not in namespaces:
                    continue
                if not info.has_namespace(FIELD_NAMESPACES[field]):
                    continue
                getter = self._FIELD_GETTERS[field]
                path_values[field] = getter(info)
            values[path] = path_values
            versions[path] = info.get("details", "modified")
        return TransportResponse(values, versions)


def _coerce_response(response, paths):
    # type: (Any, Sequence[Text]) -> TransportResponse
    if isinstance(response, TransportResponse):
        result = response
    elif isinstance(response, MappingABC):
        result = TransportResponse(
            response.get("values", {}),
            response.get("versions"),
            response.get("version"),
            response.get("consistent"),
        )
    else:
        raise CatalogError(
            "transport returned unsupported response {!r}".format(response)
        )
    if not result.versions and result.version is not None:
        result.versions = {path: result.version for path in paths}
    return result


class RequestRecord(object):
    """What happened to one planned request during execution."""

    __slots__ = [
        "request_id",
        "backend",
        "namespaces",
        "paths",
        "cost",
        "versions",
        "supports_consistency_proof",
        "error",
    ]

    def __init__(
        self,
        request_id,
        backend,
        namespaces,
        paths,
        cost,
        versions=None,
        supports_consistency_proof=False,
        error=None,
    ):
        # type: (Text, Text, Sequence[Text], Sequence[Text], int,
        #        Optional[Mapping[Text, Any]], bool, Optional[Text]) -> None
        self.request_id = request_id
        self.backend = backend
        self.namespaces = tuple(namespaces)
        self.paths = list(paths)
        self.cost = cost
        self.versions = dict(versions or {})
        self.supports_consistency_proof = bool(supports_consistency_proof)
        self.error = error

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        data = {
            "request_id": self.request_id,
            "backend": self.backend,
            "namespaces": list(self.namespaces),
            "paths": list(self.paths),
            "cost": self.cost,
            "versions": self.versions,
            "supports_consistency_proof": self.supports_consistency_proof,
        }
        if self.error is not None:
            data["error"] = self.error
        return data

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> RequestRecord
        return cls(
            raw["request_id"],
            raw["backend"],
            raw.get("namespaces", ()),
            raw["paths"],
            raw["cost"],
            raw.get("versions", {}),
            raw.get("supports_consistency_proof", False),
            raw.get("error"),
        )


class VersionBasis(object):
    """Version tokens and where they came from.

    ``versions`` maps each path covered by the request to the token the
    response reported.  A single ``version`` is exposed when every path in
    the request observed the same token.
    """

    __slots__ = ["request_id", "backend", "versions", "kind", "cross_request"]

    def __init__(self, request_id, backend, versions, kind, cross_request):
        # type: (Text, Text, Mapping[Text, Any], Text, bool) -> None
        self.request_id = request_id
        self.backend = backend
        self.versions = dict(versions or {})
        self.kind = kind
        self.cross_request = cross_request

    @property
    def version(self):
        # type: () -> Any
        """A single version token, or ``None`` when paths disagree."""
        tokens = {
            token for token in self.versions.values() if token is not None
        }
        if len(tokens) == 1 and len(self.versions) == 1:
            return tokens.pop()
        return None

    def __repr__(self):
        # type: () -> Text
        return "VersionBasis({!r}, version={!r}, kind={!r})".format(
            self.request_id, self.version, self.kind
        )

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "request_id": self.request_id,
            "backend": self.backend,
            "version": self.version,
            "versions": self.versions,
            "kind": self.kind,
            "cross_request": self.cross_request,
        }

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> VersionBasis
        versions = raw.get("versions")
        if versions is None:
            versions = {
                path: raw.get("version") for path in raw.get("paths", ())
            }
        return cls(
            raw["request_id"],
            raw["backend"],
            versions,
            raw["kind"],
            raw["cross_request"],
        )


class Manifest(object):
    """Collected field values with per-field request/version evidence."""

    def __init__(self, values, requests, basis, fields):
        # type: (Iterable[FieldValue], Iterable[RequestRecord],
        #        Iterable[VersionBasis], Sequence[Text]) -> None
        self._values = OrderedDict()
        for value in values:
            self._values.setdefault(value.path, {})[value.field] = value
        self.requests = list(requests)
        self.basis = list(basis)
        self.fields = list(fields)

    def paths(self):
        # type: () -> List[Text]
        """Return paths present in the manifest in catalog order."""
        return list(self._values)

    def field(self, path, field):
        # type: (Text, Text) -> Optional[FieldValue]
        """Return the evidence for one field, or ``None``."""
        return self._values.get(path, {}).get(field)

    def value(self, path, field, default=None):
        # type: (Text, Text, Any) -> Any
        """Return one field value, or ``default`` when absent."""
        entry = self.field(path, field)
        return default if entry is None else entry.value

    def values_for(self, path):
        # type: (Text) -> Dict[Text, FieldValue]
        """Return every recorded field for ``path`` keyed by field name."""
        return dict(self._values.get(path, {}))

    def missing(self, path):
        # type: (Text) -> List[Text]
        """Return requested fields that were not recorded for ``path``."""
        present = self._values.get(path, {})
        return [field for field in self.fields if field not in present]

    def has_all_fields(self, path):
        # type: (Text) -> bool
        """Return whether every requested field was recorded."""
        return not self.missing(path)

    def independent_fields(self, path):
        # type: (Text) -> List[Text]
        """Return fields that may only be delivered as observations."""
        return [
            field
            for field, entry in sorted(self._values.get(path, {}).items())
            if entry.is_independent_observation
        ]

    def is_complete(self, path):
        # type: (Text) -> bool
        """Return whether ``path`` forms one publishable record.

        A record is complete only when every requested field is present
        and all fields share a proven version basis.  An old cached size
        combined with a fresh permissions observation is therefore never
        reported as complete.
        """
        return self.has_all_fields(path) and not self.independent_fields(path)

    def state(self, path):
        # type: (Text) -> Text
        """Describe a path as ``complete``, ``independent`` or ``incomplete``."""
        if not self.has_all_fields(path):
            return "incomplete"
        if self.independent_fields(path):
            return "independent"
        return "complete"

    def record(self, path):
        # type: (Text) -> Dict[Text, Any]
        """Return raw field values, regardless of coherence."""
        return {
            field: entry.value
            for field, entry in sorted(self._values.get(path, {}).items())
        }

    def publishable_record(self, path):
        # type: (Text) -> Optional[Dict[Text, Any]]
        """Return a record only when it is complete, else ``None``."""
        if not self.is_complete(path):
            return None
        return self.record(path)

    def request(self, request_id):
        # type: (Text) -> RequestRecord
        """Look up a request record by identifier."""
        for record in self.requests:
            if record.request_id == request_id:
                return record
        raise KeyError(request_id)

    def basis_for(self, request_id):
        # type: (Text) -> VersionBasis
        """Look up the version basis for a request identifier."""
        for item in self.basis:
            if item.request_id == request_id:
                return item
        raise KeyError(request_id)

    def trace(self, path, field):
        # type: (Text, Text) -> Dict[Text, Any]
        """Return the request and version basis behind one field.

        For cache-sourced values, ``request`` is the original request when
        the manifest carried that history, otherwise ``None`` and
        ``basis.kind`` is ``"cache"``.
        """
        value = self.field(path, field)
        if value is None:
            return {"path": path, "field": field, "value": None}
        basis = self.basis_for(value.request_id).to_dict()
        basis["path_version"] = value.version
        trace = {
            "path": path,
            "field": field,
            "value": value.to_dict(),
            "basis": basis,
        }
        if value.source == "request":
            trace["request"] = self.request(value.request_id).to_dict()
        else:
            origin = value.request_id
            try:
                if origin.startswith("cache:"):
                    origin = origin[len("cache:"):]
                trace["request"] = self.request(origin).to_dict()
            except KeyError:
                trace["request"] = None
        return trace

    def merge_history(self, previous):
        # type: (Manifest) -> None
        """Carry earlier request records into this manifest.

        Re-planning reuses values from an earlier run, so their request
        and basis entries live in ``previous``.  Merging keeps
        :meth:`trace` and :func:`verify_manifest` able to resolve every
        field back to the request that observed it.
        """
        request_ids = {record.request_id for record in self.requests}
        basis_ids = {item.request_id for item in self.basis}
        for record in previous.requests:
            if record.request_id not in request_ids:
                self.requests.append(record)
                request_ids.add(record.request_id)
        for item in previous.basis:
            if item.request_id not in basis_ids:
                self.basis.append(item)
                basis_ids.add(item.request_id)

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        records = OrderedDict()
        for path in self._values:
            records[path] = {
                field: entry.to_dict()
                for field, entry in sorted(self._values[path].items())
            }
        return {
            "fields": list(self.fields),
            "records": records,
            "requests": [record.to_dict() for record in self.requests],
            "basis": [item.to_dict() for item in self.basis],
        }

    @classmethod
    def from_dict(cls, raw):
        # type: (Mapping[Text, Any]) -> Manifest
        values = [
            FieldValue.from_dict(item)
            for record in raw["records"].values()
            for item in record.values()
        ]
        requests = [
            RequestRecord.from_dict(item) for item in raw.get("requests", ())
        ]
        basis = [
            VersionBasis.from_dict(item) for item in raw.get("basis", ())
        ]
        return cls(values, requests, basis, raw["fields"])

    def dumps(self):
        # type: () -> Text
        """Serialize the manifest to JSON.

        `~datetime.datetime` values (times reported by ``details``) are
        encoded as ISO-8601 strings.
        """
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=False, default=_json_default
        )

    def digest(self):
        # type: () -> Text
        """Return a stable SHA-256 digest of the manifest contents."""
        payload = json.dumps(
            self.to_dict(), sort_keys=True, default=_json_default
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _json_default(value):
    # type: (Any) -> Any
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise TypeError(
        "manifest value {!r} is not JSON serializable".format(value)
    )


def _mark_consistency(path_values, request_map):
    # type: (Sequence[FieldValue], Mapping[Text, RequestRecord]) -> None
    """Set ``consistent`` on every field of one path.

    One atomic request is coherent on its own.  Multiple requests are
    coherent only when each contributing backend (including the backend
    that produced a cached value) can prove cross-request consistency,
    every value carries a version token, and all tokens agree.  A cached
    value whose backend cannot prove consistency may never be merged with
    a fresh observation: an old size beside new permissions is exactly
    the record this rule excludes.
    """
    if not path_values:
        return

    request_ids = []
    for value in path_values:
        if value.request_id not in request_ids:
            request_ids.append(value.request_id)

    versions = {value.version for value in path_values if value.version is not None}
    has_missing_version = any(value.version is None for value in path_values)
    sources = {value.source for value in path_values}

    if sources == {"cache"}:
        coherent = (
            not has_missing_version
            and len(versions) == 1
            and all(value.proof for value in path_values)
        )
        for value in path_values:
            value.consistent = coherent
        return

    if sources == {"request"} and len(request_ids) == 1:
        for value in path_values:
            value.consistent = True
        return

    coherent = not has_missing_version and len(versions) == 1
    if coherent:
        if any(value.source == "cache" and not value.proof for value in path_values):
            coherent = False
    if coherent:
        for value in path_values:
            if value.source == "request":
                record = request_map.get(value.request_id)
                if record is None or not record.supports_consistency_proof:
                    coherent = False
                    break

    for value in path_values:
        value.consistent = coherent


def execute(plan, transport):
    # type: (Plan, Transport) -> Manifest
    """Run ``plan`` through ``transport`` and build a :class:`Manifest`.

    Transport errors are recorded on the corresponding request; other
    requests continue.  Missing fields remain visible through
    :meth:`Manifest.missing`.
    """
    values = []
    requests = []
    basis = []

    for request in plan.requests:
        try:
            response = _coerce_response(
                transport.request(
                    request.backend, request.namespaces, request.paths
                ),
                request.paths,
            )
        except Exception as error:  # pragma: no cover - defensive
            requests.append(
                RequestRecord(
                    request.request_id,
                    request.backend.name,
                    request.namespaces,
                    request.paths,
                    request.cost,
                    supports_consistency_proof=(
                        request.backend.supports_consistency_proof
                    ),
                    error=str(error),
                )
            )
            continue

        versions = dict(response.versions)
        if not versions and response.version is not None:
            versions = {path: response.version for path in request.paths}
        proof = request.backend.supports_consistency_proof
        if response.consistent:
            # A transport may assert coherence it verified itself, but it
            # cannot downgrade the backend's own capability.
            proof = True

        for path in request.paths:
            required = set(plan.required.get(path, ()))
            path_values = response.values.get(path, {})
            for field, value in path_values.items():
                if field not in plan.fields:
                    continue
                namespace = FIELD_NAMESPACES.get(field)
                if namespace not in request.namespaces:
                    continue
                if required and namespace not in required:
                    continue
                values.append(
                    FieldValue(
                        path,
                        field,
                        value,
                        versions.get(path),
                        request.request_id,
                        request.backend.name,
                        "request",
                        False,
                        proof,
                        request.backend.compatibility,
                    )
                )

        requests.append(
            RequestRecord(
                request.request_id,
                request.backend.name,
                request.namespaces,
                request.paths,
                request.cost,
                versions,
                proof,
            )
        )
        basis.append(
            VersionBasis(
                request.request_id,
                request.backend.name,
                versions,
                "request",
                proof,
            )
        )

    for index, entry in enumerate(plan.reused):
        serving = _serving_backend(plan.backends, entry)
        proof = bool(serving and serving.supports_consistency_proof)
        origin = entry.request_id or "entry{}".format(index)
        request_id = origin if origin.startswith("cache:") else "cache:" + origin
        values.append(
            FieldValue(
                entry.path,
                entry.field,
                entry.value,
                entry.version,
                request_id,
                entry.backend or (serving.name if serving else ""),
                "cache",
                False,
                proof,
                entry.compatibility,
            )
        )
        basis.append(
            VersionBasis(
                request_id,
                entry.backend or (serving.name if serving else ""),
                {entry.path: entry.version},
                "cache",
                proof,
            )
        )

    request_map = {record.request_id: record for record in requests}
    by_path = OrderedDict()
    for value in values:
        by_path.setdefault(value.path, []).append(value)
    for path_values in by_path.values():
        _mark_consistency(path_values, request_map)

    return Manifest(values, requests, basis, plan.fields)


def _serving_backend(backends, entry):
    # type: (Sequence[Backend], CacheEntry) -> Optional[Backend]
    namespace = FIELD_NAMESPACES.get(entry.field)
    # Prefer the backend that originally produced the value: its
    # consistency guarantees are the ones the value was observed under.
    for backend in backends:
        if (
            backend.name == entry.backend
            and backend.supports(namespace)
            and backend.compatible(entry)
        ):
            return backend
    for backend in backends:
        if backend.supports(namespace) and backend.compatible(entry):
            return backend
    return None


def _conflicted_paths(manifest):
    # type: (Manifest) -> List[Text]
    """Return paths whose recorded fields disagree on version."""
    affected = []
    for path in manifest.paths():
        versions = {
            entry.version
            for entry in manifest._values.get(path, {}).values()
            if entry.version is not None
        }
        if len(versions) > 1:
            affected.append(path)
    return affected


def _rebase_request_ids(plan_object, prefix):
    # type: (Plan, Text) -> Plan
    """Return ``plan_object`` with request ids prefixed for global history."""
    requests = [
        BatchRequest(
            prefix + request.request_id,
            request.backend,
            request.namespaces,
            request.paths,
            request.cost,
        )
        for request in plan_object.requests
    ]
    return Plan(
        plan_object.fields,
        plan_object.paths,
        plan_object.backends,
        plan_object.required,
        requests,
        plan_object.cost,
        plan_object.reused,
        plan_object.exact,
    )


def replan(previous, changed_paths, manifest=None, cache=None):
    # type: (Plan, Iterable[Text], Optional[Manifest],
    #        Optional[Iterable[CacheEntry]]) -> Plan
    """Re-plan ``changed_paths`` and reuse verified unrelated results.

    Values from ``manifest`` for paths outside ``changed_paths`` are
    converted to cache entries when they have a coherent version basis.
    Entries from ``cache`` are preserved as well.
    """
    changed = set(_normalize_paths(changed_paths))
    entries = []
    seen = set()

    def _add(entry):
        # type: (CacheEntry) -> None
        key = (entry.path, entry.field)
        if key in seen:
            return
        seen.add(key)
        entries.append(entry)

    for entry in cache or ():
        _add(entry)
    if manifest is not None:
        backend_map = {backend.name: backend for backend in previous.backends}
        for path in previous.paths:
            if path in changed:
                continue
            for field in previous.fields:
                value = manifest.field(path, field)
                if value is None or not value.consistent:
                    continue
                backend = backend_map.get(value.backend)
                if backend is not None:
                    compatibility = backend.compatibility
                else:
                    # The value came from a cache origin whose backend is
                    # no longer available; keep its original compatibility
                    # only when the manifest recorded one.
                    compatibility = value.compatibility
                if compatibility is None:
                    continue
                _add(
                    CacheEntry(
                        path,
                        field,
                        value.value,
                        value.version,
                        compatibility,
                        value.request_id,
                        value.backend,
                    )
                )
    return plan(
        previous.paths,
        fields=previous.fields,
        backends=previous.backends,
        cache=entries,
    )


def collect(
    paths,
    fields=None,
    backends=None,
    transport=None,
    cache=None,
    current_versions=None,
    max_replans=2,
):
    # type: (Iterable[Text], Optional[Iterable[Text]], Iterable[Backend],
    #        Transport, Optional[Iterable[CacheEntry]],
    #        Optional[Mapping[Text, Any]], int) -> Manifest
    """Plan, execute, and re-plan paths whose version changes mid-run.

    The first plan covers every path.  If a path's fields are observed at
    different versions, that path is re-collected from scratch while
    verified values for unrelated paths are reused as cache entries.
    Re-planning is bounded by ``max_replans``.
    """
    if transport is None:
        raise CatalogError("a transport is required")
    fields = _check_fields(fields if fields is not None else DEFAULT_FIELDS)
    paths = _normalize_paths(paths)
    current_plan = plan(
        paths,
        fields=fields,
        backends=backends,
        cache=cache,
        current_versions=current_versions,
    )
    manifest = execute(current_plan, transport)
    for _attempt in range(max_replans):
        affected = _conflicted_paths(manifest)
        if not affected:
            break
        previous = manifest
        current_plan = replan(current_plan, affected, manifest=previous)
        current_plan = _rebase_request_ids(
            current_plan, "g{}:".format(_attempt + 1)
        )
        manifest = execute(current_plan, transport)
        manifest.merge_history(previous)
    return manifest


def verify_manifest(manifest, paths=None, fields=None):
    # type: (Manifest, Optional[Iterable[Text]], Optional[Iterable[Text]]) -> List[Text]
    """Check that every field traces to a request and a version basis.

    Returns a list of human-readable issues; an empty list means every
    recorded field has a live request or cache basis entry and a version.
    """
    issues = []
    expected_paths = _normalize_paths(paths) if paths is not None else manifest.paths()
    expected_fields = (
        _check_fields(fields) if fields is not None else list(manifest.fields)
    )
    basis_ids = {item.request_id for item in manifest.basis}
    request_ids = {record.request_id for record in manifest.requests}
    for path in expected_paths:
        for field in expected_fields:
            value = manifest.field(path, field)
            if value is None:
                issues.append("{}:{} is missing".format(path, field))
                continue
            if value.source == "request" and value.request_id not in request_ids:
                issues.append(
                    "{}:{} references unknown request {!r}".format(
                        path, field, value.request_id
                    )
                )
            if value.request_id not in basis_ids:
                issues.append(
                    "{}:{} has no version basis for {!r}".format(
                        path, field, value.request_id
                    )
                )
            if value.version is None:
                issues.append("{}:{} has no version".format(path, field))
    return issues
