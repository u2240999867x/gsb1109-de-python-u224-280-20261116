===============
Using SLCDriver
===============

Introduction
------------
``SLCDriver`` reads and writes data file addresses in SLC 500 and MicroLogix
PLCs over Ethernet/IP.  Addresses are declared directly in the style used by
RSLogix, for example ``N7:0``, ``T4:0``, ``C5:0`` or ``ST9:0``.

Addresses
---------
Data file addresses use the file letter, file number, element number and an
optional sub-element:

+-----------------+------------------------------------------------------+
| Address         | Meaning                                              |
+=================+======================================================+
| ``N7:0``        | integer file 7, element 0                            |
+-----------------+------------------------------------------------------+
| ``T4:0``        | timer file 4, element 0, whole element               |
+-----------------+------------------------------------------------------+
| ``T4:0.PRE``    | timer preset value                                   |
+-----------------+------------------------------------------------------+
| ``T4:0.ACC``    | timer accumulated value                              |
+-----------------+------------------------------------------------------+
| ``T4:0.DN``     | timer done status bit                                |
+-----------------+------------------------------------------------------+
| ``C5:0.ACC``    | counter accumulated value                            |
+-----------------+------------------------------------------------------+
| ``C5:0.CU``     | counter count-up status bit                          |
+-----------------+------------------------------------------------------+
| ``ST9:0``       | string file 9, element 0                             |
+-----------------+------------------------------------------------------+
| ``ST9:0.DATA``  | string character data                                |
+-----------------+------------------------------------------------------+
| ``N7:0{10}``    | 10 consecutive words                                 |
+-----------------+------------------------------------------------------+
| ``T4:0{2}``     | 2 consecutive timer elements                         |
+-----------------+------------------------------------------------------+

Timers, counters and strings
----------------------------
Timer and counter elements are three words: a status word followed by the
preset and accumulated values.  Status bits and numeric values therefore have
different responsibilities and are decoded from their own words; a status bit
is never taken from a numeric value.  String elements are a length word
followed by character data.

Reading a whole timer or counter element returns a ``dict`` of its fields.
Reading a whole string element returns its text, matching earlier releases.

.. code-block:: python

    >>> plc.read('T4:0')
    Tag(tag='T4:0', value={'EN': True, 'TT': False, 'DN': False, 'PRE': 30000, 'ACC': 1234}, type='T', error=None)
    >>> plc.read('ST9:0')
    Tag(tag='ST9:0', value='HELLO', type='ST', error=None)

Reading a sub-element returns just that field.

.. code-block:: python

    >>> plc.read('T4:0.PRE')
    Tag(tag='T4:0.PRE', value=30000, type='T', error=None)
    >>> plc.read('ST9:0.LEN')
    Tag(tag='ST9:0.LEN', value=5, type='ST', error=None)

When reading several elements at once, each element is cut from the reply at
a fixed element stride.  Values are never carried over from the following
element into the element before it.

Collecting elements
-------------------
``collect`` reads timer, counter and string elements as records.  It keeps
the identity of the element separate from the identity of its fields.

.. code-block:: python

    >>> collection = plc.collect('T4:0{2}', 'C5:0', 'ST9:0')
    >>> for record in collection:
    ...     print(record.address, record.value)
    T4:0 {'EN': True, 'TT': False, 'DN': False, 'PRE': 30000, 'ACC': 1234}
    T4:1 {'EN': False, 'TT': False, 'DN': False, 'PRE': 0, 'ACC': 0}
    C5:0 {'CU': False, 'CD': False, 'DN': False, 'OV': False, 'UN': False, 'UA': False, 'PRE': 10, 'ACC': 3}
    ST9:0 {'LEN': 5, 'DATA': 'HELLO'}

Each :class:`~pycomm3.slc_collect.ElementRecord` has:

- ``identity`` - ``(file_type, file_number, element_number)``
- ``address`` - canonical element address rebuilt from the parts
- ``value`` - mapping of field name to value for fields that were read
- ``fields`` - per-field results, including each field's canonical address
- ``complete`` - every field of the element was declared and read
- ``partial`` - some fields were read but the element is not complete
- ``failed`` - no field could be read
- ``errors`` / ``missing`` - failed fields and their error messages

A failed subfield only closes that field.  The element, its identity and the
other field values remain available.  Complete and partial elements are
reported separately (``collection.complete`` and ``collection.partial``);
failed elements are kept in ``collection.failed``.

Field addresses are always rebuilt from the parsed file, element and subfield
parts, never from a display name.  A field address can be passed straight back
to ``read`` or ``collect``.

Export
~~~~~~
``export`` returns the same records as JSON-serializable data.

.. code-block:: python

    >>> data = plc.export('T4:0{2}')
    >>> data['complete']
    ['T4:0', 'T4:1']
    >>> data['partial']
    []
    >>> data['records'][0]['fields']['PRE']['address']
    'T4:0.PRE'

Each record includes ``address``, ``identity``, ``declared``, ``complete``,
``partial``, ``failed``, ``value``, ``fields`` and ``errors``.  Because a
subfield error is recorded per field, a partial element is never presented as
a complete one.
