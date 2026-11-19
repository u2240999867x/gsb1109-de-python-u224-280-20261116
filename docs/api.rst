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

Projection
----------

.. autoclass:: cbor2.Projection

.. autoclass:: cbor2.Column

.. autoclass:: cbor2.ProjectionPolicy

.. autoclass:: cbor2.ProjectionPlan
.. autoclass:: cbor2.ProjectionResult
.. autoclass:: cbor2.ColumnSource

.. autofunction:: cbor2.plan_projection
.. autofunction:: cbor2.project
.. autofunction:: cbor2.project_rows
.. autofunction:: cbor2.parse_path

.. autoclass:: cbor2.ExpansionMode
    :members:
    :undoc-members:

.. autoclass:: cbor2.MissingPolicy
    :members:
    :undoc-members:

.. autoclass:: cbor2.EmptyPolicy
    :members:
    :undoc-members:

.. autoclass:: cbor2.TagPolicy
    :members:
    :undoc-members:

.. autoexception:: cbor2.ProjectionError
.. autoexception:: cbor2.ProjectionPathError
.. autoexception:: cbor2.ProjectionBudgetError
.. autoexception:: cbor2.ProjectionCardinalityError
.. autoexception:: cbor2.ProjectionTagError
.. autoexception:: cbor2.ProjectionCycleError

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
