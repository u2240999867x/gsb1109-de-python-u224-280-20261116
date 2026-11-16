.. include:: /includes/_links.rst

Daily Link Reports
==================

A daily link report answers one question per interface: how many octets
certainly crossed the link today. Raw SNMP counters cannot answer it directly,
because they wrap and because they reset whenever an interface or the whole
device is re-initialised. :mod:`pysnmp.linkreport` reconciles four pieces of
evidence to produce a defensible number:

* the 32-bit octet counters ``ifInOctets``/``ifOutOctets``,
* the 64-bit counters ``ifHCInOctets``/``ifHCOutOctets``,
* the device uptime ``sysUpTime``,
* ``ifCounterDiscontinuityTime``, the per-interface re-initialisation stamp.

The package does not poll devices. Feed it snapshots you already collected,
from JSON, JSONL or CSV, or construct :class:`~pysnmp.linkreport.Snapshot`
objects in code.

Quick start
-----------

.. code-block:: bash

   $ python -m pysnmp.linkreport polls-2026-11-16.json
   $ python -m pysnmp.linkreport polls.csv --json -o report.json

.. code-block:: python

   from pysnmp.linkreport import ReportConfig, build_report, load_snapshots, render_text

   snapshots = load_snapshots("polls-2026-11-16.json")
   report = build_report(snapshots, ReportConfig(max_rate_bps=10_000_000_000))
   print(render_text(report))

Input format
------------

A snapshot is one poll of one device. JSON accepts a list of snapshots, a
``{"snapshots": [...]}`` wrapper, a single snapshot, or a mapping of device
name to snapshots. JSONL holds one snapshot per line. CSV holds one interface
per row; rows sharing ``device`` and ``timestamp`` form a snapshot.

Field names may be either the compact snake_case form or the MIB object
names, so an SNMP walk export needs little editing:

===================  =========================================================
``device``           ``host``, ``hostname``, ``sysName``
``timestamp``        ISO-8601, or epoch seconds; a naive value is read as UTC
``sysUpTime``        TimeTicks, hundredths of a second
``interfaces``       list (or ``ifIndex``-keyed mapping) of interface entries
``ifIndex``          interface index
``ifDescr``          description, used only for display
``ifSpeed``          bits per second; ``ifHighSpeed`` (Mbit/s) is also accepted
``ifInOctets``       Counter32 inbound octets
``ifOutOctets``      Counter32 outbound octets
``ifHCInOctets``     Counter64 inbound octets
``ifHCOutOctets``    Counter64 outbound octets
``ifCounterDiscontinuityTime``  TimeTicks of the last re-initialisation
===================  =========================================================

A missing counter is not a zero. Omit the field, or use ``null``, and the
report says the value was not observed instead of inventing a delta.

How the increment is derived
----------------------------

Device generations
~~~~~~~~~~~~~~~~~~

``sysUpTime`` restarts at zero when the device reboots, so the day is split
into *generations* at every uptime decrease. The split is only made when a
32-bit TimeTicks wrap cannot explain the drop; a genuine wrap of the uptime
clock, which takes about 497 days, does not split the day.

No sampling window ever spans a generation boundary. The window across the
restart is still reported, as a reset with a bounded loss, so the traffic in
that interval is not silently dropped or silently attributed.

Choosing the counter
~~~~~~~~~~~~~~~~~~~~

The 64-bit counter is used for a direction whenever both ends of the window
carry ``ifHCInOctets``/``ifHCOutOctets``. A 64-bit counter cannot wrap inside
a reporting day, so a decrease there is a reset. If the 64-bit counters are
absent, the 32-bit counter is unwrapped against ``2**32``. The choice is made
per direction, so a device that publishes high-capacity inbound but only
32-bit outbound is handled correctly.

When both are present, the 32-bit counter is cross-checked against the 64-bit
one; disagreement is reported as ``counter_mismatch`` and the 64-bit value
wins.

Wrap versus reset
~~~~~~~~~~~~~~~~~

* A **wrap** means the counter kept counting past its modulus. The increment
  is ``current - previous + n * 2**bits`` for some whole number ``n``.
* A **reset** means the counter was re-initialised. Nothing before the reset
  can be attributed, so the confirmed increment is zero and the ceiling is
  the traffic the link could have carried in the window.

Which one happened is decided from the evidence, in this order:

1. ``ifCounterDiscontinuityTime`` changed: **reset**.
2. ``ifCounterDiscontinuityTime`` was read at both ends and did not change:
   **wrap**, even if the stated link rate cannot carry the implied traffic.
3. The counter decreased and the link rate is known: **wrap** only if a whole
   number of cycles fits inside what the link could have carried, otherwise
   **reset**.
4. The counter decreased with no rate and no discontinuity stamp: neither can
   be proven. The report claims zero and leaves the ceiling open.
5. A 64-bit counter decreased inside a window shorter than
   ``counter64_wrap_min_seconds`` (one year by default): **reset**. Wrapping
   ``2**64`` octets in a day needs more than 1.7 Pbit/s.

Bounds instead of guesses
~~~~~~~~~~~~~~~~~~~~~~~~~

When a counter could have wrapped more than once, the report gives a range.
The left number is the increment that is certain; the right number is the
largest increment still compatible with the counter values, the elapsed time
and the link capacity:

* capacity = ``ifSpeed * seconds / 8``, widened by ``rate_tolerance`` (10% by
  default) to absorb ifSpeed rounding and poll jitter;
* the smallest admissible increment is ``current - previous`` plus the fewest
  cycles needed to make it non-negative;
* the largest is the same expression with the most cycles that fit in the
  capacity.

If the link speed is unknown the ceiling cannot be established, and the
result is reported as a lower bound (``>= N``) rather than as a point value.
Pass ``--max-rate`` (or ``ReportConfig(max_rate_bps=...)``) to supply a
fallback ceiling for such interfaces.

Result statuses
---------------

=================  ==========================================================
``confirmed``      One value is possible; ``minimum == maximum``.
``range``          A wrap count is uncertain; both bounds are numbers.
``lower_bound``    The ceiling is unknown; only ``minimum`` is meaningful.
``reset``          The counter was re-initialised; ``minimum`` is zero.
``absent``         The counter was missing at one end of the window.
``unavailable``    The window or reading is unusable (zero length, out of range).
=================  ==========================================================

A reset on one interface is local to that interface. Every interface is
compared against its own previous reading, so an interface that restarts, or
that a walk omits for a while, never shortens or invalidates the arithmetic
of the interfaces around it. A missing poll is bridged across that
interface's own last and next readings, over the longer window.

Configuration
-------------

.. autoclass:: pysnmp.linkreport.ReportConfig
   :members:

API reference
-------------

.. autofunction:: pysnmp.linkreport.load_snapshots

.. autofunction:: pysnmp.linkreport.build_report

.. autofunction:: pysnmp.linkreport.render_text

.. autofunction:: pysnmp.linkreport.render_json

.. autofunction:: pysnmp.linkreport.compare_counters

.. autofunction:: pysnmp.linkreport.capacity_bytes
