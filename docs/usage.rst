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

Lossless map handling
---------------------

By default, maps are decoded into :class:`dict` objects. This is convenient but lossy:
duplicate keys are collapsed (the last value wins) and key types that compare equal (such as
``1``, ``True`` and ``1.0``) cannot be distinguished. When every member and the original key
types matter, pass ``map_pairs=True``::

    from cbor2 import loads, CBORMap

    value = loads(data, map_pairs=True)
    assert isinstance(value, CBORMap)
    for key, member_value in value:
        print(key, member_value)

A :class:`CBORMap` is an ordered sequence of ``(key, value)`` pairs which preserves every
occurrence and the decoded type of each key. It can be encoded back with :func:`dumps` without
losing duplicates or order.

If you only need to inspect the members before a :class:`dict` is built, use
``object_pairs_hook`` instead::

    def pairs_hook(decoder, pairs):
        return pairs

    pairs = loads(data, object_pairs_hook=pairs_hook)

``object_pairs_hook`` receives the ordered list of ``(key, value)`` pairs for each map and cannot
be combined with ``object_hook`` or ``map_pairs``.

Converting map keys to strings
------------------------------

Some storage formats only accept string keys. Passing ``key_to_str=True`` converts every map key
recursively while preserving the distinction between key types as far as possible. Text keys are
kept as-is, byte strings are decoded as UTF-8 with backslash escapes, simple values become
``cbor_simple:N`` and other keys are rendered with :func:`repr`.

If two distinct original keys in the same map would produce the same string (for example the byte
string ``b"a"`` and the text string ``"a"``), :exc:`CBORMapKeyCollisionError` is raised. Its
``collisions`` attribute lists each colliding string together with all the original keys, so no
value is silently dropped::

    from cbor2 import loads, CBORMapKeyCollisionError

    try:
        loads(data, key_to_str=True)
    except CBORMapKeyCollisionError as exc:
        for string_key, original_keys in exc.collisions:
            print(string_key, original_keys)

Use Cases
---------

Here are some things that the cbor2 library could be (and in some cases, is being) used for:

- Experimenting with network protocols based on CBOR encoding
- Designing new data storage formats
- Submitting binary documents to ElasticSearch without base64 encoding overhead
- Storing and validating file metadata in a secure backup system
- RPC which supports Decimals with low overhead
