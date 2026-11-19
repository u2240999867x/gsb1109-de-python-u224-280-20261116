.. _api_transcode:

.. py:module:: pynetdicom.transcode

Transfer Syntax Conversion (:mod:`pynetdicom.transcode`)
========================================================

.. currentmodule:: pynetdicom.transcode

This module converts DICOM datasets between the three uncompressed,
unencapsulated transfer syntaxes:

* *Implicit VR Little Endian* (``1.2.840.10008.1.2``)
* *Explicit VR Little Endian* (``1.2.840.10008.1.2.1``)
* *Explicit VR Big Endian* (``1.2.840.10008.1.2.2``)

Compressed (encapsulated) and deflated transfer syntaxes are not supported and
are rejected. Every conversion re-encodes the dataset, the File Meta
Information and the actual binary payloads rather than only rewriting the
(0002,0010) *Transfer Syntax UID*.

Functions
---------

.. autosummary::
   :toctree: generated/

   transcode_dataset
   transcode_message
   transcode_file
   transcode_instances
   successful_reports

Exceptions
----------

.. autosummary::
   :toctree: generated/

   TranscodeError
   UnsupportedTransferSyntaxError
   EncapsulatedDatasetError
   UnverifiableDatasetError
