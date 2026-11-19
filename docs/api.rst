API reference
=============

Encoding
--------

.. autofunction:: cbor2.dumps
.. autofunction:: cbor2.dump
.. autoclass:: cbor2.CBOREncoder
.. autodecorator:: cbor2.shareable_encoder

Decoding
--------

.. autofunction:: cbor2.loads
.. autofunction:: cbor2.load
.. autoclass:: cbor2.CBORDecoder

Types
-----

.. autoclass:: cbor2.CBORSimpleValue
.. autoclass:: cbor2.CBORTag
.. data:: cbor2.undefined
    A singleton representing the CBOR "undefined" value.

Tag rule replay
---------------

.. autoclass:: cbor2.TagRule
.. autoclass:: cbor2.RuleSet
.. autoclass:: cbor2.RuleChange
.. autoclass:: cbor2.RuleSetDiff
.. autofunction:: cbor2.builtin_rules
.. autoclass:: cbor2.TaggedValue
.. autoclass:: cbor2.Replay
.. autofunction:: cbor2.replay_records
.. autoclass:: cbor2.RecordResult
.. autoclass:: cbor2.FieldResult
.. autoclass:: cbor2.Node
.. autoclass:: cbor2.Dependency
.. autofunction:: cbor2.pending_tickets

Exceptions
----------

.. autoexception:: cbor2.CBORError
.. autoexception:: cbor2.CBOREncodeError
.. autoexception:: cbor2.CBOREncodeTypeError
.. autoexception:: cbor2.CBOREncodeValueError
.. autoexception:: cbor2.CBORDecodeError
.. autoexception:: cbor2.CBORDecodeValueError
.. autoexception:: cbor2.CBORDecodeEOF
