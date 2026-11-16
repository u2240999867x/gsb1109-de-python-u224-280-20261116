===========================
Offline replay of browsing
===========================

``fastkml.replay`` simulates how an Earth browser schedules
``<NetworkLink>`` fetches while a user pans and zooms. It works entirely
offline: feed it a parsed KML document and a timestamped viewport
trajectory and it returns, for every link, the instants at which the target
must be requested, reused from the cache or released.

Input model
-----------

* :class:`~fastkml.replay.Viewport` is one timestamped snapshot of the map
  view (bounding box in geographic degrees plus a projected pixel span that
  drives LOD comparisons). Viewports crossing the antimeridian are declared
  literally with ``east < west``.
* :class:`~fastkml.replay.ReplayConfig` supplies the default cache validity
  period and the ``onInterval``/``onStop`` timing defaults when the document
  omits them.
* :class:`~fastkml.replay.ReplayEngine` walks every ``<NetworkLink>`` in the
  document together with its ancestor ``<Region>`` chain.

Each output row is a :class:`~fastkml.replay.LinkEvent` with an
:class:`~fastkml.replay.Action` (``request``, ``reuse`` or ``release``) and
the set of reasons that explain the decision.

.. code-block:: python

    from fastkml import Document, Link, NetworkLink
    from fastkml.replay import ReplayConfig, ReplayEngine, Viewport
    from fastkml.views import LatLonAltBox, Lod, Region

    box = Region(
        lat_lon_alt_box=LatLonAltBox(north=60, south=35, east=30, west=-10),
        lod=Lod(min_lod_pixels=0, max_lod_pixels=-1),
    )
    document = Document(
        features=[
            NetworkLink(
                id="europe",
                region=box,
                link=Link(href="https://example.com/europe.kml"),
            ),
        ],
    )

    trajectory = [
        Viewport(0.0, -10.0, 35.0, 30.0, 60.0, 512.0),
        Viewport(1.0, 40.0, 35.0, 70.0, 60.0, 512.0),
        Viewport(2.0, -10.0, 35.0, 30.0, 60.0, 512.0),
    ]

    events = ReplayEngine(
        document,
        trajectory,
        ReplayConfig(default_cache_seconds=300.0),
    ).run()

The link is requested at ``t=0``, released when the view leaves its region at
``t=1`` and reused (not re-requested) when the view returns at ``t=2``,
because the cached entry is still inside its 300 second validity window.

Evaluation rules
----------------

Region activity
    A link is active only when every region in its ancestor chain is active.
    Each region must intersect the viewport through its declared
    ``<LatLonAltBox>`` and fall inside its ``<Lod>`` pixel ramp. A parent
    region becoming inactive deactivates the whole subtree, even if the
    child's own region still matches.

Antimeridian
    Boxes and viewports with ``east < west`` are treated as crossing the
    antimeridian. The declaration is never normalised, so an
    antimeridian-crossing region only matches viewports that intersect the
    declared wrapping interval.

Cache validity
    When a link leaves its region the cached target is kept until it expires.
    A quick move out and back in therefore yields a ``reuse`` event when the
    expiry time has not been reached and a new ``request`` (reason
    ``expired``) when it has. Server-provided ``<expires>`` values are fed in
    with :meth:`~fastkml.replay.ReplayEngine.set_expiry`.

Refresh settings
    ``onInterval`` re-requests once ``<refreshInterval>`` has elapsed,
    ``onStop`` re-requests ``<viewRefreshTime>`` seconds after the viewport
    stops changing, and ``onRegion`` is driven purely by region activity and
    ignores ``<viewRefreshTime>``.

Shared targets
    A target href referenced by several network links keeps one activation
    reason per reference. Releasing one reference only frees the target when
    the last reference goes away; an active reference elsewhere keeps the
    cached entry valid.
