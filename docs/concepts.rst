Concepts
========

This page explains pyx12's mental model — the moving parts a developer
sees when reading the API or debugging output. It's the layer between
:doc:`quickstart` ("how do I run it?") and :doc:`api` ("what's the
signature of method X?"): why does the package look the way it does,
and how do its pieces fit together?

The X12 hierarchy
-----------------

An X12 EDI document has a fixed envelope hierarchy. From outermost in:

* **Interchange** — wrapped by ``ISA`` … ``IEA``. One physical file may
  contain one or more interchanges. Identifies sender / receiver and
  carries a control number.
* **Functional group** — wrapped by ``GS`` … ``GE``. Groups one or more
  transactions of the same type (e.g. all the 837s) under a common
  application sender / receiver.
* **Transaction set** — wrapped by ``ST`` … ``SE``. One business
  document: a single 837 claim batch, a 270 eligibility request, a 999
  acknowledgement, etc.
* **Loops and segments** — the body of the transaction. A *segment* is
  one line (``NM1*82*1*DOE*JOHN``); a *loop* is a named group of
  segments and/or sub-loops. Loops aren't delimited in the wire format —
  the receiver figures out where each loop starts and stops by matching
  segment IDs (and, when ambiguous, qualifier values like ``NM1``'s
  first element) against the implementation guide.
* **Elements and composites** — fields within a segment, separated by
  ``*``. A *composite* is a multi-part field whose sub-parts are
  separated by ``:``.

pyx12 mirrors this hierarchy throughout: error reports, the context
reader's tree, and the 997 / 999 acknowledgement structure all follow
the same five layers (see :doc:`errors`).

Implementation guide maps
-------------------------

An X12 Implementation Guide (IG) tells you, for each version of each
transaction, exactly which loops are required, which elements may be
present, what counts are allowed, and which code lists apply.
Implementation guides are published by `X12.org <https://x12.org/products>`_.

pyx12 encodes those Implementation Guide rules as XML "map" files under
``pyx12/map/``. These maps are the heart of the package: they drive
validation, error reporting, and the context reader's parent / child
traversal.

When the bundled maps don't fit your need — validating against a newer
guide, or a non-HIPAA transaction — copy ``pyx12/map/`` to a local
directory, edit or add map files there, and point pyx12 at it via the
``map_path`` parameter (see :ref:`concepts:parameters` below).

pyx12 is focused on X12N Healthcare HIPAA transactions, but the map-based
design is flexible enough to support any X12 transaction with a known
structure. To add a new transaction type, drop a new XML map file into
``pyx12/map/`` following the existing conventions.


.. code-block:: text

   837.4010.X098.A1.xml      ← 837 Professional, version 4010
   837.5010.X222.A1.xml      ← 837 Professional, version 5010
   835.5010.X221.A1.xml      ← 835 Remittance Advice, 5010
   270.4010.X092.A1.xml      ← Eligibility request, 4010
   …
   codes.xml                 ← External code lists (Place of Service, state codes, claim status, …)
   dataele.xml               ← Data element catalog (lengths, types)
   maps.xml                  ← Index from transaction/version → map file

The map XML is a tree of ``<loop>``, ``<segment>``, ``<element>``, and
``<composite>`` nodes. Each carries the rules pyx12 will enforce:

.. code-block:: xml

   <segment xid="NM1">
     <name>Subscriber Name</name>
     <usage>R</usage>          <!-- R=required, S=situational, N=not used -->
     <pos>015</pos>             <!-- ordinal within parent loop -->
     <max_use>1</max_use>       <!-- max times this segment may repeat -->
     <element xid="NM101">
       <data_ele>98</data_ele>  <!-- look up type/length in dataele.xml -->
       <usage>R</usage>
       <valid_codes>
         <code>IL</code>
       </valid_codes>
     </element>
     …
   </segment>

At runtime, :class:`pyx12.map_if.map_if` parses the right map for the
incoming transaction (chosen by :class:`pyx12.map_index.map_index` from
``ISA12`` / ``GS08``) and hands the in-memory tree to the walker.

Readers: raw, streaming, context-aware
--------------------------------------

pyx12 ships three reader interfaces at increasing levels of abstraction:

:class:`pyx12.rawx12file.RawX12File`
   The lowest level. Splits an X12 stream into segments by detecting
   the segment / element / sub-element terminators from the fixed length ``ISA`` segment, but
   does **not** validate against any map. Useful for transformations
   that don't care about HIPAA rules (e.g. switching line endings).

:class:`pyx12.x12file.X12Reader`
   Streaming validator. Iterates segments one at a time and emits
   structural errors against the envelope (control numbers, IEA / GE /
   SE counts, HL counting, line-level structural issues). Doesn't
   know about loops or implementation guides.

:class:`pyx12.x12context.X12ContextReader`
   Loop-aware. Wraps an ``X12Reader`` and a :class:`pyx12.map_walker.walk_tree`,
   loads the right implementation-guide map, and yields a tree of
   ``X12DataNode`` objects representing each loop and segment in
   context. This is what you want when you need to *modify* segments
   (set a value, delete a child loop, insert a new sibling) — it tracks
   parent / child relationships and the map node for each piece of data.
   Also useful for complex parsing that require loop context (e.g. "if this 
   segment is inside an 2000A loop, then this element means X; but if it's 
   inside a 2000B loop, the same element means Y"). See the iter_segments example in :doc:`quickstart`.

The walker
----------

The walker (:class:`pyx12.map_walker.walk_tree`) is what turns a stream
of segments into a validated tree. As the reader yields segments, the
walker:

1. **Finds the matching node** in the current loop (or pops up to a
   parent loop, or descends into a child loop) for each incoming
   segment — this is where loop boundaries are inferred from segment
   IDs.
2. **Tracks counts** for every loop and segment via
   :class:`pyx12.nodeCounter.NodeCounter` so it can detect
   max-repeat violations.
3. **Tracks pending required-segment misses** in
   ``mandatory_segs_missing`` and emits a ``seg_error '3'``
   ("mandatory segment missing") only once it's certain the loop has
   moved on without seeing the required segment.
4. **Calls ``errh.seg_error(code, msg, …)``** for each problem found,
   feeding the error handler that ultimately drives the 997 / 999
   acknowledgement.

Each call returns the matched map node plus two lists: which loops
were popped (because the new segment ended them) and which were
pushed (because the new segment started them). The context reader uses
those lists to keep its data tree in sync.

Error tree
----------

Every problem the walker, reader, or element validator finds is
recorded against an *error handler*, an instance of
:class:`pyx12.error_handler.err_handler` (tree-shaped, used by the
997 / 999 generators) or :class:`pyx12.error_handler.errh_list` (flat
per-level lists, easier for programmatic access).

The tree handler mirrors the X12 hierarchy: ``ROOT`` → ``ISA`` →
``GS`` → ``ST`` → ``segment`` → ``element``. The 997 / 999 generators
walk that tree and emit one acknowledgement segment per error (AK3
for segment-level, AK4 for element-level, etc).

For the catalog of every code that can appear, see :doc:`errors`.

Parameters
----------

Runtime knobs live on a :class:`pyx12.params.params` instance, passed
through to readers, writers, and the walker. Common settings:

* ``map_path`` — where to look for map XML files (defaults to bundled
  package resources). Override via the ``--map-path`` / ``-m`` CLI flag
  on the bundled scripts, or ``params.set('map_path', '/path/to/maps')``
  from Python, to point at a directory of custom maps.
* ``charset`` — ``'B'`` (basic X12 character set) or ``'E'`` (extended,
  needed for some 5010 transactions).
* ``exclude_external_codes`` — comma-separated list of external code
  list names to skip during validation, useful when the bundled list
  is out of date.
* ``simple_dtd`` — DTD for the "simple" XML rendering of the document.
* ``xmlout`` — ``'simple'`` is the only value the bundled scripts use.

``params`` also reads an XML config file from
``$PREFIX/etc/pyx12.conf.xml`` and ``~/.pyx12.conf.xml`` on Unix (no
home-dir lookup on Windows). See ``bin/pyx12.conf.xml.sample`` in the
source tree for the schema.

Payer companion guides
----------------------

A claim has to satisfy **two** independent rule sets: the HIPAA
implementation guide (the base map shipped in ``pyx12/map``) and the
payer's own companion guide. These are not alternatives — the same
transaction is evaluated against both, and each verdict is kept.

Enable the second pass with ``--companion guide.xml`` on ``x12valid`` or
by setting ``params.set("companion_file", path)``. The companion file is
a small XML document whose ``rule`` elements target a base-map location:

.. code-block:: xml

   <companion_guide>
     <name>Acme Payer Companion Guide</name>
     <payer>ACME</payer>
     <rule id="ACME-NM108-XX" applies_to="837">
       <location>/ISA_LOOP/GS_LOOP/ST_LOOP/DETAIL/2000A/2010AA/NM1</location>
       <element>NM108</element>
       <expect><allow><code>XX</code></allow></expect>
     </rule>
   </companion_guide>

``applies_to`` selects the transaction (the map's ``xid``); ``location``
is an absolute map path to a segment and ``element`` names one of its
children (use ``subelement`` for a composite component). ``expect`` is
exactly one of:

* ``allow`` — the present value must be in the listed codes;
* ``disallow`` — the present value must not be in the listed codes;
* ``require`` — the element must be present and non-empty.

The companion pass is **read-only**. It re-runs the element validators to
obtain the base verdict and applies the payer rule separately, so neither
rule set can overwrite the other regardless of evaluation order, and the
source data and base map are never modified. Each finding records both
sources (the base map file plus any base error codes, and the companion
file plus rule id) and the applicable loop — its path and the
1-based occurrence of that loop in the transaction.

Two verdicts that disagree fall into one of three mutually exclusive
buckets:

* **base_invalid** — the base guide rejects the position. A payer rule
  can never legalize a base-illegal value, so this dominates even when
  the payer would have accepted it. The base pyx12 error codes are
  preserved on the finding.
* **payer_rejected** — the base guide accepts the value but the
  companion rule rejects it (a payer-only tightening).
* **rule_conflict** — the two rule sets themselves contradict at the
  same position, independent of the data. Reported once per rule under
  ``rule_conflicts`` (for example, the companion disallows a code the
  base code list includes, or requires an element the base marks
  ``usage="N"``). A self-contradictory rule does not also emit
  per-data findings.

Findings and rule conflicts are emitted under a separate
``companion_validation`` key in the JSON output. The 997/999
acknowledgement and the base ``interchanges`` error tree are byte-for-byte
identical to a companion-free run — companion verdicts never leak into an
X12 ack, and when no companion is configured the output is unchanged.

Putting it together
-------------------

When you run ``x12valid claim.x12``, this is the call chain:

1. :func:`pyx12.x12n_document.x12n_document` constructs a
   :class:`pyx12.params.params` and an
   :class:`pyx12.error_handler.err_handler`, then opens the file.
2. It instantiates an :class:`pyx12.x12file.X12Reader` over the input
   for envelope-level checks.
3. For each ``ST`` it sees, it asks
   :class:`pyx12.map_index.map_index` which IG map applies (based on
   ``ICVN`` / ``VRIIC`` / ``GS08`` codes), loads it via
   :class:`pyx12.map_if.map_if`, and starts a fresh
   :class:`pyx12.map_walker.walk_tree` for that transaction.
4. As segments arrive, the walker walks the map, the element validator
   on each map node checks values against ``data_type`` / ``min_len`` /
   ``max_len`` / ``valid_codes``, and every problem hits the error
   handler.
5. After EOF, the 997 (4010) / 999 (5010) generator walks the error
   tree and writes an acknowledgement segment per error to ``fd_997``.
   An optional HTML rendering of the document plus errors goes to
   ``fd_html``.

For a code-level walk through these calls, see
:doc:`api/pyx12/x12n_document/index`.
