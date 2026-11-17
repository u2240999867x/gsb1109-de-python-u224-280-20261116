.. _cache-eviction:

Reclaiming cache space
======================

When the disk holding a Pooch cache fills up, Pooch can plan which cached
materials to delete. The plan minimizes the cost of fetching the deleted
materials again and reports exactly how many bytes are actually freed.

The planner understands that a cache is more than a flat list of files:

* **Originals and derived artifacts.** A downloaded archive and the files
  extracted from it are linked. Evicting the archive forces the extracted
  files out too, because they cannot be rebuilt without it. The extracted
  files can be evicted on their own while keeping the archive.
* **Hard links.** Several names can point to the same physical file. Bytes are
  counted once, and only when the last link is deleted.
* **Files that do not belong to the cache.** They are never deleted and never
  counted as reclaimed space, even if a cache entry names them.
* **Shared paths.** When several materials list the same file, it is only
  deleted together with all of them, so the planner treats them as one
  decision.
* **Leases and retained versions.** Materials in use by a running job or
  pinned to a retained version are never evicted.

Basic usage
-----------

Describe the materials, ask for the number of bytes you need, and run the
plan::

    import pooch

    materials = [
        pooch.CacheMaterial(
            name="archive.zip",
            size=1_048_576,
            refetch_cost=12.5,
            paths=("archive.zip",),
            derived=("archive.zip.unzip/data.csv",),
        ),
        pooch.CacheMaterial(
            name="archive.zip.unzip/data.csv",
            size=8_388_608,
            refetch_cost=0,
            paths=("archive.zip.unzip/data.csv",),
            originals=("archive.zip",),
            version="v1.2.0",
        ),
        pooch.CacheMaterial(
            name="caribbean-bathymetry.csv.xz",
            size=524_288,
            refetch_cost=3.0,
            paths=("caribbean-bathymetry.csv.xz",),
        ),
    ]

    plan = pooch.plan_eviction(
        materials,
        requested=2_000_000,
        cache_root=pooch.os_cache("myproject"),
        retained_versions=["v1.2.0"],
    )
    print(plan.summary())
    print("released", plan.released, "cost", plan.cost)

    result = pooch.execute_eviction(plan, materials)
    print("actually released", result.released)

If the request cannot be met, :func:`pooch.plan_eviction` still returns a
plan. It has ``feasible=False``, the number of missing bytes in ``deficit``,
the best achievable amount in ``max_releasable`` and an explanation in
``reason``::

    >>> plan = pooch.plan_eviction(materials, requested=10**12)
    >>> plan.feasible
    False
    >>> plan.deficit > 0
    True

.. note::

    ``size`` is informational. The bytes that a plan reports as released are
    always measured from the files listed in ``paths``. A material whose
    files do not exist releases nothing.

Leases and revalidation
-----------------------

A plan is a snapshot. Another process may download, lease or replace a file
between planning and execution. Pass the material descriptions as a *callable*
that returns their current state, and :func:`pooch.execute_eviction` will call
it again right before each deletion::

    def current_materials():
        return registry.materials()

    plan = pooch.plan_eviction(current_materials(), requested=2_000_000)
    result = pooch.execute_eviction(plan, current_materials)

A material that became leased, pinned, moved to a retained version or whose
files were replaced is skipped, and everything that depends on it is skipped
too. The result reports ``released`` from the disk before and after the
deletions, so it always reflects what really happened.

Use :func:`pooch.revalidate_eviction` to inspect the effect without deleting
anything. Both functions accept ``retained_versions``, ``retained_paths`` and
``keep_materials``.

API reference
-------------

.. autosummary::
    :toctree: generated/

    pooch.CacheMaterial
    pooch.EvictionPlan
    pooch.EvictionResult
    pooch.plan_eviction
    pooch.revalidate_eviction
    pooch.execute_eviction
