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

String reference archive indexing
---------------------------------

.. autoclass:: cbor2.StringRefArchiveIndex
    :members:
.. autoclass:: cbor2.ObjectInfo
.. autoclass:: cbor2.StringRefSource
.. autoclass:: cbor2.ResolvedReference
.. autoclass:: cbor2.FileFingerprint

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
.. autoexception:: cbor2.StringRefIndexError
.. autoexception:: cbor2.StringRefContextError
.. autoexception:: cbor2.StringRefRangeError
.. autoexception:: cbor2.IndexFileChangedError
