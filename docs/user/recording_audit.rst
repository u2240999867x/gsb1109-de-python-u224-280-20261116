.. _user_recording_audit:

Auditing Recorded Associations
------------------------------

Captured DICOM traffic can be audited offline with
:func:`~pynetdicom.recording.inspect_recording`. The recording is split into
associations at each A-ASSOCIATE request, and each association gets its own
:class:`~pynetdicom.recording.ContextView` built from the A-ASSOCIATE request
and response.

.. code-block:: python

    from pynetdicom.recording import inspect_recording

    recording = inspect_recording("capture.bin")

    for assoc in recording:
        print(assoc.contexts)
        for message in assoc.messages:
            print(message.context_id, message.status, message.issues)

The context view records the SOP Class, accepted transfer syntax and the
negotiated roles for every accepted presentation context. A SOP Class may be
present in more than one context; the context used for a message is determined
solely by the context ID the message was sent under.

Only contexts accepted in the association's A-ASSOCIATE response are used.
Proposed transfer syntaxes that were not accepted are not treated as
alternatives, and contexts from other associations are never used to fill in
missing information.

Message statuses
~~~~~~~~~~~~~~~~

Each message has one of the following statuses:

* ``explainable`` - the command and data set are consistent with the
  negotiated context.
* ``encoding_conflict`` - the command or data set contradicts the negotiated
  context. This includes a data set that is not encoded using the accepted
  transfer syntax, even if a lenient decoder happens to read plausible values.
* ``insufficient_evidence`` - the recording cannot prove the message was valid,
  such as a message sent under a context that was not accepted, a sender using
  an unnegotiated role, or a missing final data set fragment.

The corresponding :class:`~pynetdicom.recording.IssueCode` values identify the
reason, for example ``unaccepted_context``, ``role_not_allowed`` or
``missing_data_set_end``.

Delivering verified materials
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Messages with an ``encoding_conflict`` or ``insufficient_evidence`` status are
not silently dropped from the recording, but they are excluded from
``deliver()``:

.. code-block:: python

    verified = recording.deliver()
    datasets = recording.deliver(datasets=True)

The other verified messages remain deliverable even when one message conflicts
with its context.
