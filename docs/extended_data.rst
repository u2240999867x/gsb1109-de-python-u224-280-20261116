Extended Data
=============

KML has two metadata paths, and ``fastkml`` models both. ``Data`` stores simple untyped
name-value pairs, while ``Schema``, ``SchemaData``, ``SimpleField``, and the ``gx`` array
classes model typed feature metadata. These abstractions exist because real KML documents
often need application-specific fields such as asset IDs, classifications, measurements, or
time series that the base KML schema does not include.

The relevant code lives in ``fastkml/data.py`` and ``fastkml/gx/data.py``. ``ExtendedData``
is the main wrapper and can contain both ``Data`` and ``SchemaData``. ``Schema`` is usually
attached to a ``Document``, while ``SchemaData`` is attached to a feature and points back to
the schema through ``schema_url``. This mirrors how the KML spec separates schema
declaration from schema-backed values.

How It Relates To Other Concepts
---------------------------------

* ``ExtendedData`` is a regular feature attribute on ``_Feature``, so it travels with
  placemarks and overlays.
* ``gx:Track`` can also hold ``ExtendedData``, and its special registration is completed in
  ``_registry_setup.py``.
* Validation matters more here than in plain text metadata because typed schemas are easier
  to misuse.

How It Works Internally
------------------------

``ExtendedData.elements`` is a mixed list of ``Data`` and ``SchemaData``. ``Schema.append()``
decides whether a field goes into ``fields`` or ``array_fields``, while
``SchemaData.append_data()`` does the same for regular and array values. These methods are
tiny because the registry does the actual XML work. The important implementation detail is
the boolean behavior: many of these classes define ``__bool__()`` to suppress empty
elements, so blank metadata objects often disappear from serialized output instead of
producing empty XML tags.

Basic Usage
-----------

.. code-block:: pycon

    >>> from fastkml import Data, ExtendedData, Placemark
    >>> from pygeoif import Point

    >>> placemark = Placemark(
    ...     name="Substation",
    ...     geometry=Point(7.1, 50.7, 0),
    ...     extended_data=ExtendedData(
    ...         elements=[
    ...             Data(name="asset_id", value="SS-14"),
    ...             Data(name="owner", value="Grid Ops"),
    ...         ]
    ...     ),
    ... )

    >>> print(placemark.extended_data.elements[0].name)
    asset_id

Advanced Usage
--------------

.. code-block:: pycon

    >>> from fastkml import (
    ...     Document,
    ...     ExtendedData,
    ...     Placemark,
    ...     Schema,
    ...     SchemaData,
    ...     SimpleData,
    ...     SimpleField,
    ... )
    >>> from fastkml.enums import DataType
    >>> from fastkml.gx import SimpleArrayData, SimpleArrayField
    >>> from pygeoif import Point

    >>> schema = Schema(
    ...     id="asset-schema",
    ...     name="Asset",
    ...     fields=[
    ...         SimpleField(name="voltage", type_=DataType.int_),
    ...         SimpleField(name="status", type_=DataType.string),
    ...     ],
    ...     array_fields=[
    ...         SimpleArrayField(name="reading", type_=DataType.float_),
    ...     ],
    ... )

    >>> placemark = Placemark(
    ...     name="Transformer",
    ...     geometry=Point(11.57, 48.14, 0),
    ...     extended_data=ExtendedData(
    ...         elements=[
    ...             SchemaData(
    ...                 schema_url="#asset-schema",
    ...                 data=[
    ...                     SimpleData(name="voltage", value="110"),
    ...                     SimpleData(name="status", value="active"),
    ...                 ],
    ...                 array_data=[
    ...                     SimpleArrayData(name="reading", data=["10.3", "10.7", "10.5"]),
    ...                 ],
    ...             )
    ...         ]
    ...     ),
    ... )

    >>> doc = Document(id="assets", schemata=[schema], features=[placemark])
    >>> print(doc.schemata[0].id)
    asset-schema

.. warning::

    ``Schema`` requires an ``id``, and ``SchemaData.schema_url`` needs to reference that
    ``id`` with a fragment such as ``#asset-schema``. If those do not line up, viewers may
    accept the KML but your typed data will be effectively detached from its schema.

Untyped Data versus SchemaData
----------------------------------

Use ``Data`` when you just need a couple of simple fields and you do not care about formal
typing. It is shorter to write, easier to inspect, and enough for many viewer-only
workflows. Use ``SchemaData`` when you need explicit field declarations, array values, or a
contract that other systems can rely on. The extra ceremony is worthwhile when the KML file
is part of a data pipeline rather than just a display artifact.

Named column contracts
----------------------

The same field name can appear in several documents through different mechanisms: one
document may declare ``temperature`` in a ``Schema`` as a ``double`` and reach it through
``schemaUrl``, while another only carries it as free-text ``Data``. The helper functions in
``fastkml.contracts`` flatten those values into :class:`fastkml.ColumnContract` objects that
keep the namespace, the verbatim ``schemaUrl``, the declared
:class:`fastkml.enums.DataType`, and the raw value together instead of merging columns by
name alone.

Schema references are resolved per document. A ``schemaUrl`` that points at a schema which
is not declared in the same document keeps its URL but reports no declared type; the field
is never explained by a same-named ``Schema`` living in another document.

.. code-block:: pycon

    >>> from fastkml import KML, column_contracts, column_values
    >>> kml_doc = '''<kml xmlns="http://www.opengis.net/kml/2.2">
    ... <Document>
    ...   <Schema id="weather">
    ...     <SimpleField name="temperature" type="double"/>
    ...   </Schema>
    ...   <Placemark>
    ...     <ExtendedData>
    ...       <SchemaData schemaUrl="#weather">
    ...         <SimpleData name="temperature">21.5</SimpleData>
    ...       </SchemaData>
    ...       <Data name="temperature"><value>warm</value></Data>
    ...     </ExtendedData>
    ...   </Placemark>
    ... </Document>
    ... </kml>'''
    >>> document = KML.from_string(kml_doc, ns=None).features[0]
    >>> [(c.name, c.schema_url, c.declared_type, c.array)
    ...  for c in column_contracts(document)]
    [('temperature', None, None, False), ('temperature', '#weather', <DataType.double: 'double'>, False)]
    >>> [(v.raw, v.value, v.error) for v in column_values(document)]
    [('warm', 'warm', None), ('21.5', 21.5, None)]

Values are converted according to the declared type one at a time. If a conversion fails,
only that value is isolated: its raw text is retained, ``value`` is ``None``, and ``error``
explains the failure, while sibling values (including the other elements of a ``gx`` array)
are still converted.

.. code-block:: pycon

    >>> bad_doc = '''<kml xmlns="http://www.opengis.net/kml/2.2">
    ... <Document>
    ...   <Schema id="weather">
    ...     <SimpleField name="temperature" type="double"/>
    ...   </Schema>
    ...   <Placemark>
    ...     <ExtendedData>
    ...       <SchemaData schemaUrl="#weather">
    ...         <SimpleData name="temperature">oops</SimpleData>
    ...       </SchemaData>
    ...     </ExtendedData>
    ...   </Placemark>
    ... </Document>
    ... </kml>'''
    >>> bad = column_values(KML.from_string(bad_doc, ns=None).features[0])[0]
    >>> bad.raw
    'oops'
    >>> bad.value is None
    True
    >>> bad.error is not None
    True

Why gx array fields are separate classes
--------------------------------------------

The KML base schema does not provide native array field declarations, so the Google ``gx``
extension models them separately with ``SimpleArrayField`` and ``SimpleArrayData``. fastkml
keeps those in ``fastkml.gx.data`` rather than merging them into the base classes because the
namespace, XML shape, and viewer support are different. That separation makes the XML output
correct and keeps the object model honest about what is standard KML and what is an
extension. The trade-off is that mixed metadata documents need imports from both ``fastkml``
and ``fastkml.gx``.
