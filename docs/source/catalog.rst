.. _catalog:

Collecting Catalog Metadata
===========================

A *catalog* run records a fixed set of fields (size, times, permissions)
for many paths.  Different backends expose different
:ref:`info namespaces <info>`, accept different batch sizes, and charge
different amounts per request, so the cheapest collection strategy is not
always one backend for everything.

:mod:`fs.catalog` models that trade-off explicitly.  It produces a plan,
executes it, and returns a manifest in which every field can be traced to
the request that observed it and the version basis under which it was
observed.

Planning
--------

Describe each backend with :class:`~fs.catalog.Backend`::

    from fs.catalog import Backend, collect, FSInfoTransport

    backends = [
        Backend(
            "bulk",
            namespaces=["details", "access"],
            max_batch=100,
            batch_cost=2,
            per_path_cost=1,
            supports_consistency_proof=True,
        ),
        Backend(
            "permissions-only",
            namespaces=["access"],
            max_batch=0,
            batch_cost=5,
            per_path_cost=3,
        ),
    ]

``max_batch=0`` means the backend accepts any batch size.  ``batch_cost``
is charged once per request and ``per_path_cost`` once per path in the
request.

:func:`~fs.catalog.plan` returns the cheapest set of requests::

    from fs.catalog import plan

    result = plan(
        paths,
        fields=["size", "modified", "permissions"],
        backends=backends,
    )
    print(result.cost)

Paths with the same remaining namespace requirements are grouped, and the
planner searches both backend assignments and path distribution across
backends.  Splitting a namespace across two backends is considered when
their fixed costs and batch limits make that cheaper.

Cached values
-------------

A :class:`~fs.catalog.CacheEntry` records a value together with the
observed resource ``version`` and the backend ``compatibility`` generation
that produced it::

    from fs.catalog import CacheEntry

    cache = [
        CacheEntry(
            path="/data/a.bin",
            field="size",
            value=1024,
            version=41,
            compatibility="2026-11",
            request_id="r7",
        )
    ]

An entry only reduces request cost when its ``compatibility`` matches a
backend that can serve the field's namespace.  An entry produced by an
older incompatible backend is ignored rather than trusted.  Pass
``current_versions={"path": 42}`` to :func:`~fs.catalog.plan` to require
the observed version to still be current as well.

Records and observations
------------------------

:class:`~fs.catalog.Manifest` keeps values per path and field, each with
the ``request_id``, ``backend``, ``version`` and ``source`` that produced
it.  A path is only a *complete* record when every requested field is
present and all fields share a proven version basis::

    for path in manifest.paths():
        if manifest.is_complete(path):
            publish(manifest.publishable_record(path))
        else:
            publish_observations(path, manifest.independent_fields(path))

An atomic request is coherent on its own even when the backend cannot
prove consistency across requests.  If a backend lacks that proof and a
path's fields come from separate requests, those fields are reported as
independent observations.  A cached size and a fresh permissions
observation are never combined into one complete record, even when their
version tokens happen to agree.

Version changes during execution
--------------------------------

:func:`~fs.catalog.collect` executes a plan and compares the version token
each request reports for each path.  A path whose fields were observed at
different versions is re-planned and requested again, up to
``max_replans`` attempts.  Verified values for unrelated paths are carried
into the new plan as cache entries, so they are not requested again.

Traceability
------------

:func:`~fs.catalog.verify_manifest` checks that every recorded field has a
request or cache basis and a version token.  :meth:`Manifest.trace`
returns the complete chain for one field::

    trace = manifest.trace("/data/a.bin", "size")
    trace["request"]["request_id"]
    trace["basis"]["path_version"]
    trace["value"]["backend"]

The manifest serializes with :meth:`Manifest.dumps`; datetimes from the
``details`` namespace are encoded as ISO-8601 strings.

Running against filesystems
---------------------------

:class:`~fs.catalog.FSInfoTransport` adapts ordinary `FS` objects to the
transport interface::

    from fs.tempfs import TempFS

    with TempFS() as temp_fs:
        temp_fs.writetext("a.txt", "hello")
        manifest = collect(
            ["/a.txt"],
            fields=["size", "permissions"],
            backends=[
                Backend(
                    "temp",
                    ["details", "access"],
                    batch_cost=1,
                    supports_consistency_proof=True,
                )
            ],
            transport=FSInfoTransport({"temp": temp_fs}),
        )

The adapter calls ``getinfo`` once per path and uses the ``modified``
value as the version token when the backend exposes it.  It performs no
retries or caching of its own.

Exactness
---------

:func:`~fs.catalog.plan` reports optimality through ``Plan.exact``.  The
planner runs an exact dynamic program while the search fits its work
budget.  For very large mixed catalogs it falls back to a deterministic
coordinate-descent assignment and sets ``exact`` to ``False``; the
returned cost is always the cost of the returned requests, but it may not
be the global minimum.  A plan that cannot be built at all raises
:class:`~fs.catalog.PlanConflict`.
