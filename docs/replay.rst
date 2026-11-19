======================
Offline replay of FTP
======================

``pyftpdlib.replay`` replays a captured FTP download offline and reports
three independent pieces of evidence:

* which byte ranges were actually observed on the data connection;
* how the data connection was closed (FIN or RST, and by which side);
* which control reply terminated the transfer.

The verdict is ``complete`` only when all three agree.  A missing
terminal reply keeps the transfer ``unknown``: no success reply is
inferred from a clean data close, and a late reply is never used to
complete a later, unrelated command.

Command line
============

.. code-block:: sh

    $ python3 -m pyftpdlib.replay capture.pcap
    transfer: t1 (f.bin)
    state: complete (success)
    expected: 0-1000 (1000 bytes, from SIZE)
    received: 0-1000 (1000 bytes, offset basis=stream)
    gaps: (none) (0 missing)
    data close: fin by server (clean=True)
    terminal reply: 226 complete

The capture may be a ``pcap``/``pcapng`` file or a JSON/JSON Lines event
stream.  ``--json`` emits the full analysis; ``--size`` supplies the
expected file size when the capture has no ``SIZE`` reply.

Exit status is ``0`` for a complete download, ``1`` for an incomplete
one, ``2`` when the evidence is insufficient (unknown), and ``3`` for
input errors or ``--strict`` warnings.

Event schema
============

A JSON event stream is a list of objects or one object per line::

    {"ts": 1.00, "channel": "control", "direction": "c2s",
     "kind": "command", "data": "RETR f.bin"}
    {"ts": 1.01, "channel": "control", "direction": "s2c",
     "kind": "reply", "data": "150 ready"}
    {"ts": 1.10, "channel": "data", "direction": "s2c",
     "kind": "segment", "conn": "d1", "offset": 0, "length": 800}
    {"ts": 1.20, "channel": "data", "direction": "s2c",
     "kind": "close", "conn": "d1", "who": "server", "how": "fin"}

Offsets are absolute in the file being downloaded, so a resumed
download starts at its ``REST`` offset.  When an ``offset`` comes from a
per-connection TCP stream counter, add
``"extra": {"offset_basis": "stream"}`` and the analyzer adds the
``REST`` offset.  Omitting ``offset`` appends bytes in capture order.

Replies are matched per control session using reply-code semantics.  A
``125``/``150`` starts a transfer; ``226``/``250`` and the failure codes
terminate it.  An abnormal ending can produce two replies (``426`` then
``226``); both stay with the original transfer, and the last one is
reported as ``final_reply``.

Python API
==========

.. code-block:: python

    from pyftpdlib.replay import load_events, analyze_events

    result = analyze_events(load_events("trace.jsonl"), expected_size=1024)
    summary = result.primary
    print(summary.state, summary.ranges.as_list())

``analyze_events`` accepts :class:`pyftpdlib.replay.Event` objects;
``replay_capture(path)`` converts a pcap/pcapng file into events using a
dependency-free TCP reassembler.
