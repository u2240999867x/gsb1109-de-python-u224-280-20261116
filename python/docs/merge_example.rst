*****************************
Merging Captures and MCAP Files
*****************************

:py:mod:`mcap.merge` combines several MCAP files — or the files of a capture
directory — into one output. It is exposed as a Python API; there is no CLI in this
package.

Merging the files in a capture directory
----------------------------------------
.. literalinclude:: ../examples/merge/capture.py

One output per profile
----------------------
Files with different header profiles are never written into one file. To merge a
directory that may contain several profiles, write into a destination directory; each
profile produces its own output with that profile in its header.

.. literalinclude:: ../examples/merge/per_profile.py

What is preserved
-----------------
``merge_mcaps`` renumbers schema and channel ids from 1, then records how every output
declaration maps back to its source:

* a schema is shared only when its name, encoding and data bytes are identical;
* every source channel gets its own output channel, so each merged message can be traced
  back to its original channel — and through it to its original file and profile — using
  :class:`mcap.merge.MergeResult` or the manifest written to the output;
* message payloads are copied without decoding, so channels with an unknown profile or
  message encoding keep their bytes;
* metadata, attachments, message log/publish times and sequence numbers are preserved,
  and messages are ordered by log time across inputs.

Reading the written manifest back
---------------------------------
.. literalinclude:: ../examples/merge/read_manifest.py

The manifest is stored as a metadata record named
:data:`mcap.merge.MERGE_METADATA_NAME` and is also returned by the merge functions as
``result.manifest``. Its ``sources``, ``schemas`` and ``channels`` entries list the
source file and original id for every output declaration, and
``schema_equivalence`` / ``channel_equivalence`` list declarations that are equivalent
even though they were kept distinct.
