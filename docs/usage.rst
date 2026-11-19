Basic usage
===========

.. py:currentmodule:: cbor2

Serializing and deserializing with cbor2 is pretty straightforward::

    from cbor2 import dumps, loads, load

    # Serialize an object as a bytestring
    data = dumps(['hello', 'world'])

    # Deserialize a bytestring
    obj = loads(data)

    # Efficiently deserialize from a file
    with open('input.cbor', 'rb') as fp:
        obj = load(fp)

    # Efficiently serialize an object to a file
    with open('output.cbor', 'wb') as fp:
        dump(obj, fp)

Some data types, however, require extra considerations, as detailed below.

Date/time handling
------------------

The CBOR specification does not support naïve datetimes (that is, datetimes where ``tzinfo`` is
missing). When the encoder encounters such a datetime, it needs to know which timezone it belongs
to. To this end, you can specify a default timezone by passing a :class:`~datetime.tzinfo` instance
to :func:`dump`/:func:`dumps` call as the ``timezone`` argument.
Decoded datetimes are always timezone aware.

By default, datetimes are serialized in a manner that retains their timezone offsets. You can
optimize the data stream size by passing ``datetime_as_timestamp=False`` to
:func:`dump`/:func:`dumps`, but this causes the timezone offset
information to be lost.

In versions prior to 4.2 the encoder would convert a ``datetime.date`` object into a
``datetime.datetime`` prior to writing. This can cause confusion on decoding so this has been
disabled by default in the next version. The behaviour can be re-enabled as follows::

    from cbor2 import dumps
    from datetime import date, timezone

    # Serialize dates as datetimes
    encoded = dumps(date(2019, 10, 28), timezone=timezone.utc, date_as_datetime=True)

A default timezone offset must be provided also.

Cyclic (recursive) data structures
----------------------------------

If the encoder encounters a shareable object (ie. list or dict) that it has seen before, it will
by default raise :exc:`CBOREncodeError` indicating that a cyclic reference has been
detected and value sharing was not enabled. CBOR has, however, an extension specification that
allows the encoder to reference a previously encoded value without processing it again. This makes
it possible to serialize such cyclic references, but value sharing has to be enabled by passing
``value_sharing=True`` to :func:`dump`/:func:`dumps`.

.. warning:: Support for value sharing is rare in other CBOR implementations, so think carefully
    whether you want to enable it. It also causes some line overhead, as all potentially shareable
    values must be tagged as such.

String references
-----------------

When ``string_referencing=True`` is passed to
:func:`dump`/:func:`dumps`, if the encoder would encode a string that
it has previously encoded and where a reference would be shorter than the encoded string, it
instead encodes a reference to the nth sufficiently long string already encoded.

.. warning:: Support for string referencing is rare in other CBOR implementations, so think carefully
    whether you want to enable it.

Tag support
-----------

In addition to all standard CBOR tags, this library supports many extended tags:

===== ======================================== ====================================================
Tag   Semantics                                Python type(s)
===== ======================================== ====================================================
0     Standard date/time string                datetime.date / datetime.datetime
1     Epoch-based date/time                    datetime.date / datetime.datetime
2     Positive bignum                          int / long
3     Negative bignum                          int / long
4     Decimal fraction                         decimal.Decimal
5     Bigfloat                                 decimal.Decimal
25    String reference                         str / bytes
28    Mark shared value                        N/A
29    Reference shared value                   N/A
30    Rational number                          fractions.Fraction
35    Regular expression                       re.Pattern (result of ``re.compile(...)``)
36    MIME message                             email.message.Message
37    Binary UUID                              uuid.UUID
256   String reference namespace               N/A
258   Set of unique items                      set
260   Network address                          :class:`ipaddress.IPv4Address` (or IPv6)
261   Network prefix                           :class:`ipaddress.IPv4Network` (or IPv6)
55799 Self-Described CBOR                      object
===== ======================================== ====================================================

Arbitary tags can be represented with the :class:`CBORTag` class.

If you want to write a file that is detected as CBOR by the Unix ``file`` utility, wrap your data in
a :class:`CBORTag` object like so::

    from cbor2 import dump, CBORTag

    with open('output.cbor', 'wb') as fp:
        dump(CBORTag(55799, obj), fp)

This will be ignored on decode and the original data content will be returned.

Row projections
---------------

Decoded CBOR messages often contain nested arrays that need to become rows.
:class:`~cbor2.Projection` maps each output column to a path and expands the
arrays on that path.  Paths share their common prefix, so two columns that both
start with ``orders[]`` are correlated by order::

    from cbor2 import Projection

    value = {
        "orders": [
            {"id": 1, "items": [{"sku": "a"}, {"sku": "b"}]},
            {"id": 2, "items": [{"sku": "c"}]},
        ]
    }

    projection = Projection({
        "order": "orders[].id",
        "sku": "orders[].items[].sku",
    })

    plan = projection.plan(value)
    assert plan.row_count == 3
    assert plan.sources == {
        "order": "orders[].id",
        "sku": "orders[].items[].sku",
    }

    result = projection.project(value, max_rows=100)
    print(result.rows)

A path uses dots between mapping keys, ``[N]`` for an array index, ``[]`` (or
``[*]``) to expand one array level, and ``[**]`` to expand nested arrays
recursively.  A sequence of keys and indices can also be passed directly, which
supports non-string keys.  Leading ``$`` is optional.

The plan is exact.  :meth:`~cbor2.Projection.plan` reports the row count and,
for each column, its canonical source path, the number of occurrences, and how
many missing values, empty arrays and tags were resolved by policy.
:meth:`~cbor2.Projection.project` computes the plan first and only then builds
records.  If ``max_rows`` is given and the exact row count is larger, it raises
:class:`~cbor2.ProjectionBudgetError` and returns no partial records.

**Combining modes**

``paired`` (the default) aligns divergent branches at the same path by index,
like :func:`zip`.  A branch that does not expand is a scalar and is broadcast to
every row of its sibling.  If two expanded branches have different occurrence
counts, the missing policy decides what happens to the extra occurrences of the
longer one.

``cross`` forms the Cartesian product of divergent branches, with the
rightmost branch varying fastest.  Shared prefixes are still evaluated once, so
orders are not crossed with each other.

**Policies**

Each policy is set on the projection or overridden per column:

- ``missing`` controls a path step that does not exist: ``null`` adds one
  occurrence holding ``missing_value``, ``skip`` adds nothing, and ``error``
  aborts the plan.
- ``empty`` controls an empty array at an expansion point: ``zero`` adds no
  occurrences, ``null`` adds one ``missing_value`` occurrence, and ``error``
  aborts the plan.
- ``tag`` controls a tag encountered before the end of a path: ``opaque`` keeps
  the tag as one value, ``unwrap`` continues into ``tag.value``, ``zero`` drops
  it, and ``error`` aborts the plan.  A ``tag_resolver`` callable may decode the
  tag first; returning the tag itself means it could not be resolved.

**Shared values and cycles**

Shared values (CBOR tags 28 and 29) are counted by projection occurrence, not by
object identity.  If the same object is referenced from two places, both
references expand and both contribute rows; the projection never deduplicates
rows by object id.  A cycle that a normal finite path merely passes through is
not an error.  Only recursive expansion that actually follows a cycle forever
raises :class:`~cbor2.ProjectionCycleError`, which names both the path where
the cycle was followed and the earlier path where the same object was entered.

Use Cases
---------

Here are some things that the cbor2 library could be (and in some cases, is being) used for:

- Experimenting with network protocols based on CBOR encoding
- Designing new data storage formats
- Submitting binary documents to ElasticSearch without base64 encoding overhead
- Storing and validating file metadata in a secure backup system
- RPC which supports Decimals with low overhead
