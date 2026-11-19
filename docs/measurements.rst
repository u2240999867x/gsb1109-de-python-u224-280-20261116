Reconciling compressed measurements
===================================

.. py:currentmodule:: cbor2.measurements

A collector can compress a batch of sensor readings with two custom CBOR tags:

* an **absolute** record that pins one sample to a value; and
* an **affine relation** that declares ``y = a * x + b`` between two samples,
  with exact rational coefficients.

Records may arrive out of order, a relation may be delivered more than once,
and independent measurements may disagree.  :func:`reconcile` turns such a
stream into the values that are actually determined, the relations that remain
underdetermined, and explicit conflict records.  Exactness is preserved
throughout: values never pass through binary floating point.

Sample identity
---------------

A sample is identified by ``(sensor, generation, sample)`` and represented by
:class:`SampleId`.  The generation (boot / restart id) is part of the identity,
so samples from two restarts are never joined, even when the sensor and sample
number match.  A relation spanning two generations is rejected as
``cross_generation`` unless ``allow_cross_generation=True`` is passed.

Absolute records
----------------

The absolute tag number defaults to ``47000`` and is exposed as
``TAG_ABSOLUTE``.  The payload can be any of these equivalent shapes::

    ["sensor-a", 7, 12, {"n": 3, "d": 2}]      # sensor, generation, sample, value
    ["sensor-a", 7, 12, 3, 2]                  # last two fields are n, d
    [["sensor-a", 7, 12], 3/2]                  # endpoint tuple, value
    {"sensor": "sensor-a", "generation": 7,
     "sample": 12, "value": 3/2}

A value may be an integer, a :class:`fractions.Fraction`, a
:class:`decimal.Decimal`, a numeric string, a two-element numerator/denominator
pair, or the standard CBOR rational (tag 30), decimal-fraction (tag 4) or
bigfloat (tag 5) payloads.

Affine records
--------------

The relation tag number defaults to ``47001`` and is exposed as
``TAG_AFFINE``.  Equivalent payload shapes include::

    ["sensor-a", 7, 12, "sensor-a", 7, 13, {"n": 2, "d": 1}, {"n": -1, "d": 1}]
    ["sensor-a", 7, 12, "sensor-a", 7, 13, 2, -1]
    [["sensor-a", 7, 12], ["sensor-a", 7, 13], 2, -1]
    {"sensor": "sensor-a", "generation": 7,
     "x": 12, "y": 13, "a": 2, "b": -1}

Useful aliases are accepted in maps: ``src``/``dst``, ``source``/``target``,
``from``/``to``, ``slope``/``intercept``, ``m``/``c`` and ``scale``/``offset``.
The relation is directional; ``y = a*x + b`` also determines
``x = (y - b) / a`` when ``a`` is non-zero.

Reconciling a stream
--------------------

:func:`reconcile` (aliases :func:`resolve`, :func:`reconstruct`,
:func:`decode_measurements`, :func:`reconcile_cbor`) accepts raw CBOR bytes, a
concatenated CBOR sequence, a file object, a decoded list, or a mapping
envelope with ``anchors`` and ``relations`` keys::

    from cbor2.measurements import reconcile

    result = reconcile(open("capture.cbor", "rb").read())
    print(result.confirmed)       # {SampleId(...): Fraction(...)}
    print(result.undetermined)    # canonical surviving equations
    print(result.conflicts)       # contradictory observations

The result also provides:

* ``evidence`` – for every confirmed value, the anchors and relations that
  derive it, with exact coefficients;
* ``expressions`` – each unresolved sample expressed through free samples;
* ``components`` – independent connected groups, so a rejected observation is
  visibly confined to its own group;
* ``rejected`` – malformed or rejected records, including the record itself;
* ``duplicate_relations`` / ``duplicate_anchors`` – repeats that were counted
  once;
* ``to_dict()`` / ``to_json()`` – a stable report with rationals written as
  ``"numerator/denominator"`` strings.

What counts as confirmed
------------------------

The reconciler solves the linear system over :class:`fractions.Fraction` and
uses reduced row echelon form:

* An absolute anchor fixes its sample directly.
* A chain of relations from a fixed sample fixes downstream samples.
* A cycle with no anchor can still have a unique fixed point.  For example,
  ``y = 2x + 1`` together with ``x = 2y + 1`` fixes both at ``-1``.  The
  cycle is solved, not dismissed as underdetermined.
* If an anchorless relation merely ties two samples together, its value stays
  free and the relation is reported in ``undetermined`` with canonical
  coefficients.  ``expressions`` shows the residual freedom.

Conflicts
---------

Independent anchors for the same sample may disagree.  When they do, all of
them are reported in one ``anchor_conflict`` and no value is invented for that
sample.  If an anchor contradicts values already derived, that anchor is
rejected with the observations that witness the contradiction.  If an affine
relation contradicts the rest of its component, only that relation is
rejected; other independent relations, anchors and components remain intact.

Malformed records are collected in ``rejected`` rather than aborting the whole
batch.  Pass ``strict=True`` to raise :class:`MeasurementFormatError` instead.

Duplicates
----------

Repeated delivery of the same relation is one observation, not extra
evidence.  Equations are normalised, so ``y = 2x`` and ``x = y/2`` are the
same relation.  Repeated identical anchors likewise count once.  The counters
``duplicate_relations`` and ``duplicate_anchors`` record how many repeats were
ignored.
