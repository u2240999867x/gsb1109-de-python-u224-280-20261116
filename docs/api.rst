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

Exceptions
----------

.. autoexception:: cbor2.CBORError
.. autoexception:: cbor2.CBOREncodeError
.. autoexception:: cbor2.CBOREncodeTypeError
.. autoexception:: cbor2.CBOREncodeValueError
.. autoexception:: cbor2.CBORDecodeError
.. autoexception:: cbor2.CBORDecodeValueError
.. autoexception:: cbor2.CBORDecodeEOF

Deferred label interpretation
------------------------------

.. autoclass:: cbor2.LabelRule
.. autoclass:: cbor2.LabelDictionary
.. autoclass:: cbor2.InterpretationSession
    :members:
.. autoclass:: cbor2.BatchOutcome
    :members:
.. autoclass:: cbor2.PendingObject
    :members:
.. autoclass:: cbor2.PendingStatus
    :members:
.. autoclass:: cbor2.InterpretedObject
    :members:
.. autoclass:: cbor2.InterpretationFailure
    :members:
.. autoexception:: cbor2.InterpretationError
.. autoexception:: cbor2.MissingDictionaryError
.. autoexception:: cbor2.LabelRuleRejected
.. autoexception:: cbor2.CorruptEncodingError
.. autoexception:: cbor2.CyclicInterpretationError
